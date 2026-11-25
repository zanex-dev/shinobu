# -*- coding: utf-8 -*-
"""Unit tests for hot-pluggable TtsRouter and tiered fallback behavior."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from typing import Any

from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.cloud.tts_router import (
    TtsResult,
    TtsRouter,
    TtsUnavailableError,
)


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _StubHttpServer:
    """Lightweight real localhost HTTP server for end-to-end urllib testing."""

    def __init__(self, handler_fn) -> None:
        self.requests: list[dict[str, Any]] = []
        requests_log = self.requests

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                headers_lower = {k.lower(): v for k, v in self.headers.items()}
                record = {
                    "path": self.path,
                    "headers": headers_lower,
                    "body": raw,
                }
                requests_log.append(record)
                status, content_type, payload = handler_fn(record)
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class TestTtsRouter(unittest.TestCase):
    def test_prefers_online_colab_gpu_worker_end_to_end(self) -> None:
        expected_audio = b"OggS-fake-gpu-opus-bytes"

        def worker_handler(req: dict[str, Any]) -> tuple[int, str, bytes]:
            self.assertEqual(req["path"], "/v1/audio/speech")
            self.assertEqual(req["headers"].get("authorization"), "Bearer gpu-secret")
            body = json.loads(req["body"].decode("utf-8"))
            self.assertEqual(body["input"], "Hello cloud router")
            self.assertEqual(body["response_format"], "opus")
            self.assertEqual(body["mood"], "excited")
            self.assertGreater(body["speed"], 1.0)
            return (200, "audio/ogg; codecs=opus", expected_audio)

        srv = _StubHttpServer(worker_handler)
        self.addCleanup(srv.close)

        clock = _FakeClock()
        registry = WorkerRegistry(clock=clock)
        registry.register(
            WorkerRecord(
                worker_id="edge-tts-1",
                role="edge_tts",
                base_url="http://127.0.0.1:9",
                capabilities=("tts",),
                ttl_sec=60.0,
                last_seen=clock(),
            )
        )
        clock.advance(1.0)
        registry.register(
            WorkerRecord(
                worker_id="colab-gpu-1",
                role="colab_gpu",
                base_url=srv.base_url,
                capabilities=("tts",),
                token="gpu-secret",
                ttl_sec=60.0,
                last_seen=clock(),
                meta={"model": "indextts-2.5"},
            )
        )

        router = TtsRouter(
            registry,
            fallback_url="http://127.0.0.1:9",
            gcp_token_provider=lambda: "unused-token",
            timeout_sec=5.0,
        )
        result = router.synthesize("Hello cloud router", mood="excited", fmt="ogg_opus")
        self.assertIsInstance(result, TtsResult)
        self.assertEqual(result.audio, expected_audio)
        self.assertEqual(result.content_type, "audio/ogg")
        self.assertEqual(result.provider, "worker:colab-gpu-1")
        self.assertEqual(len(srv.requests), 1)

    def test_worker_failure_falls_back_to_gcp_chirp3_hd_iam(self) -> None:
        gcp_audio = b"OggS-gcp-chirp3-hd-payload"
        gcp_calls: list[dict[str, Any]] = []

        def fake_post(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> tuple[int, dict[str, str], bytes]:
            if url.endswith("/v1/audio/speech"):
                return (503, {"content-type": "application/json"}, b'{"error":"gpu busy"}')
            gcp_calls.append({"url": url, "headers": dict(headers), "body": json.loads(body.decode("utf-8"))})
            resp = json.dumps({"audioContent": base64.b64encode(gcp_audio).decode("ascii")}).encode("utf-8")
            return (200, {"content-type": "application/json"}, resp)

        clock = _FakeClock()
        registry = WorkerRegistry(clock=clock)
        registry.register(
            WorkerRecord(
                worker_id="colab-gpu-1",
                role="colab_gpu",
                base_url="https://worker.example.test",
                capabilities=("tts",),
                token="tok",
                ttl_sec=60.0,
                last_seen=clock(),
            )
        )

        router = TtsRouter(
            registry,
            gcp_voice="cmn-CN-Chirp3-HD-Aoede",
            gcp_token_provider=lambda: "ya29.fake-iam-token",
            http_post=fake_post,
        )
        result = router.synthesize("测试云端语音合成", mood="calm", fmt="ogg_opus")
        self.assertEqual(result.audio, gcp_audio)
        self.assertEqual(result.content_type, "audio/ogg")
        self.assertEqual(result.provider, "gcp:cmn-CN-Chirp3-HD-Aoede")
        self.assertEqual(len(gcp_calls), 1)
        self.assertEqual(gcp_calls[0]["headers"]["Authorization"], "Bearer ya29.fake-iam-token")
        self.assertEqual(gcp_calls[0]["body"]["voice"]["languageCode"], "cmn-CN")
        self.assertEqual(gcp_calls[0]["body"]["voice"]["name"], "cmn-CN-Chirp3-HD-Aoede")
        self.assertEqual(gcp_calls[0]["body"]["audioConfig"]["audioEncoding"], "OGG_OPUS")

    def test_gcp_api_key_auth_mode(self) -> None:
        gcp_audio = b"ID3-fake-mp3"
        seen_headers: list[dict[str, str]] = []

        def fake_post(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> tuple[int, dict[str, str], bytes]:
            seen_headers.append(dict(headers))
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["audioConfig"]["audioEncoding"], "MP3")
            resp = json.dumps({"audioContent": base64.b64encode(gcp_audio).decode("ascii")}).encode("utf-8")
            return (200, {"content-type": "application/json"}, resp)

        registry = WorkerRegistry()
        router = TtsRouter(
            registry,
            gcp_voice="ja-JP-Chirp3-HD-Aoede",
            gcp_api_key="AIza-test-key",
            http_post=fake_post,
        )
        result = router.synthesize("テスト音声", fmt="mp3")
        self.assertEqual(result.audio, gcp_audio)
        self.assertEqual(result.content_type, "audio/mpeg")
        self.assertEqual(result.provider, "gcp:ja-JP-Chirp3-HD-Aoede")
        self.assertEqual(seen_headers[0].get("X-Goog-Api-Key"), "AIza-test-key")
        self.assertNotIn("Authorization", seen_headers[0])

    def test_worker_expired_and_gcp_failed_falls_back_to_openai_gateway(self) -> None:
        fallback_audio = b"RIFF-fallback-wav"

        def fallback_handler(req: dict[str, Any]) -> tuple[int, str, bytes]:
            self.assertEqual(req["path"], "/v1/audio/speech")
            self.assertEqual(req["headers"].get("authorization"), "Bearer edge-key")
            body = json.loads(req["body"].decode("utf-8"))
            self.assertEqual(body["response_format"], "wav")
            return (200, "audio/wav", fallback_audio)

        fb_srv = _StubHttpServer(fallback_handler)
        self.addCleanup(fb_srv.close)

        clock = _FakeClock(100.0)
        registry = WorkerRegistry(clock=clock)
        registry.register(
            WorkerRecord(
                worker_id="colab-gpu-expired",
                role="colab_gpu",
                base_url="http://127.0.0.1:9",
                capabilities=("tts",),
                ttl_sec=30.0,
                last_seen=100.0,
            )
        )
        # Expire the GPU worker so Tier 1 is skipped automatically.
        clock.advance(45.0)

        def broken_token_provider() -> str:
            raise RuntimeError("metadata server unreachable")

        router = TtsRouter(
            registry,
            fallback_url=f"{fb_srv.base_url}/v1",
            fallback_api_key="edge-key",
            gcp_token_provider=broken_token_provider,
            timeout_sec=5.0,
        )
        result = router.synthesize("Fallback voice check", fmt="wav")
        self.assertEqual(result.audio, fallback_audio)
        self.assertEqual(result.content_type, "audio/wav")
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(len(fb_srv.requests), 1)

    def test_hot_plug_recovery_switches_back_to_worker_on_register(self) -> None:
        calls: list[str] = []

        def fake_post(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> tuple[int, dict[str, str], bytes]:
            calls.append(url)
            if "gpu-worker" in url:
                return (200, {"content-type": "audio/ogg"}, b"gpu-voice")
            return (200, {"content-type": "audio/ogg"}, b"fallback-voice")

        clock = _FakeClock(500.0)
        registry = WorkerRegistry(clock=clock)
        router = TtsRouter(
            registry,
            fallback_url="https://edge-fallback.example.test",
            http_post=fake_post,
        )

        first = router.synthesize("Before GPU joins")
        self.assertEqual(first.provider, "fallback")
        self.assertEqual(first.audio, b"fallback-voice")

        registry.register(
            WorkerRecord(
                worker_id="colab-t4",
                role="colab_gpu",
                base_url="https://gpu-worker.example.test",
                capabilities=("tts",),
                ttl_sec=60.0,
                last_seen=clock(),
            )
        )
        second = router.synthesize("After GPU joins")
        self.assertEqual(second.provider, "worker:colab-t4")
        self.assertEqual(second.audio, b"gpu-voice")

        registry.unregister("colab-t4")
        third = router.synthesize("After GPU leaves")
        self.assertEqual(third.provider, "fallback")
        self.assertEqual(third.audio, b"fallback-voice")

    def test_all_backends_unavailable_raises_error_and_validates_inputs(self) -> None:
        registry = WorkerRegistry()
        router = TtsRouter(registry)

        with self.assertRaises(ValueError):
            router.synthesize("   ")

        with self.assertRaises(TtsUnavailableError) as ctx:
            router.synthesize("Nobody home")
        self.assertIn("no TTS backend", str(ctx.exception))

        def always_fail(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> tuple[int, dict[str, str], bytes]:
            raise ConnectionError("network down")

        registry.register(
            WorkerRecord(
                worker_id="w1",
                role="colab_gpu",
                base_url="https://w1.example.test",
                capabilities=("tts",),
            )
        )
        failing_router = TtsRouter(
            registry,
            fallback_url="https://fb.example.test",
            gcp_token_provider=lambda: "tok",
            http_post=always_fail,
        )
        with self.assertRaises(TtsUnavailableError) as ctx2:
            failing_router.synthesize("Should fail all tiers")
        self.assertEqual(len(ctx2.exception.errors), 3)

        with self.assertRaises(TypeError):
            TtsRouter(object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TtsRouter(registry, timeout_sec=0)


if __name__ == "__main__":
    unittest.main()
