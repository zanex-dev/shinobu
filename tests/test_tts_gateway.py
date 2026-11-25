from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 stdlib fallback
    tomllib = None  # type: ignore[assignment]
import types
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from shinobu_core.cloud import tts_gateway

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    recorded_requests: list[dict[str, object]] = []
    fail_mode: bool = False

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(max(0, length))
        body = json.loads(raw.decode("utf-8")) if raw else {}
        type(self).recorded_requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": body,
            }
        )
        if type(self).fail_mode:
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.end_headers()
            self.wfile.write(b"upstream offline")
            return
        audio = b"RIFFFAKEWAVDATA"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


class TestTTSGatewayUnit(unittest.TestCase):
    def test_pyproject_optional_tts_edge_and_zero_core_deps(self) -> None:
        raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if tomllib is not None:
            data = tomllib.loads(raw)
            self.assertEqual(data["project"]["dependencies"], [])
            optional = data["project"]["optional-dependencies"]
            self.assertEqual(optional.get("tts-edge"), ["edge-tts>=6.1"])
        else:
            self.assertIn("dependencies = []", raw)
            self.assertIn('tts-edge = ["edge-tts>=6.1"]', raw)

    def test_text_voice_rate_and_url_helpers(self) -> None:
        cleaned = tts_gateway.clean_speech_text("Hello `world` [link](https://example.com) ```code``` **bold**")
        self.assertEqual(cleaned, "Hello world link bold")
        self.assertEqual(tts_gateway.resolve_voice("shinobu"), "zh-CN-XiaoyiNeural")
        self.assertEqual(tts_gateway.resolve_voice("ja-JP-NanamiNeural"), "ja-JP-NanamiNeural")
        self.assertEqual(tts_gateway.resolve_voice("unknown-voice"), tts_gateway.DEFAULT_VOICE)
        self.assertEqual(tts_gateway.speed_to_rate(1.25), "+25%")
        self.assertEqual(tts_gateway.speed_to_rate(0.1), "-50%")
        self.assertEqual(
            tts_gateway.build_upstream_speech_url("https://gpu.example.com"),
            "https://gpu.example.com/v1/audio/speech",
        )
        self.assertEqual(
            tts_gateway.build_upstream_speech_url("https://gpu.example.com/v1"),
            "https://gpu.example.com/v1/audio/speech",
        )
        self.assertEqual(
            tts_gateway.build_upstream_speech_url("https://gpu.example.com/v1/audio/speech"),
            "https://gpu.example.com/v1/audio/speech",
        )

    def test_default_upstream_path_uses_shinobu_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom_home = Path(tmp) / "custom_home"
            with mock.patch.dict(os.environ, {"SHINOBU_HOME": str(custom_home)}):
                self.assertEqual(
                    tts_gateway.default_upstream_path(),
                    custom_home / "tts" / "upstream.json",
                )

    def test_bind_host_validation_rejects_wildcard_and_public_ips(self) -> None:
        for bad_host in ("0.0.0.0", "::", "8.8.8.8", "1.1.1.1", "example.com"):
            with self.subTest(host=bad_host):
                with self.assertRaises(ValueError):
                    tts_gateway.EdgeTTSServer((bad_host, 0))

        with tempfile.TemporaryDirectory() as tmp:
            srv = tts_gateway.EdgeTTSServer(
                ("localhost", 0),
                upstream_path=Path(tmp) / "upstream.json",
            )
            try:
                self.assertEqual(srv.server_address[0], "127.0.0.1")
            finally:
                srv.server_close()


class TestTTSGatewayHTTPAndFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.upstream_cfg_path = Path(self._tmp.name) / "tts" / "upstream.json"

        _FakeUpstreamHandler.recorded_requests = []
        _FakeUpstreamHandler.fail_mode = False
        self.upstream_srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.upstream_port = self.upstream_srv.server_address[1]
        self.upstream_thread = threading.Thread(target=self.upstream_srv.serve_forever, daemon=True)
        self.upstream_thread.start()

        self.gw = tts_gateway.EdgeTTSServer(
            ("127.0.0.1", 0),
            upstream_path=self.upstream_cfg_path,
        )
        self.gw_port = self.gw.server_address[1]
        self.gw_thread = threading.Thread(target=self.gw.serve_forever, daemon=True)
        self.gw_thread.start()
        self.base_url = f"http://127.0.0.1:{self.gw_port}"

    def tearDown(self) -> None:
        self.gw.shutdown()
        self.gw.server_close()
        self.upstream_srv.shutdown()
        self.upstream_srv.server_close()
        self._tmp.cleanup()

    def test_health_models_voices_and_upstream_get_without_edge_tts(self) -> None:
        with mock.patch.dict(sys.modules, {"edge_tts": None}):
            status, _, raw = _http_request(f"{self.base_url}/health")
            self.assertEqual(status, HTTPStatus.OK)
            health = json.loads(raw.decode("utf-8"))
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["engine"], "edge-tts")
            self.assertFalse(health["upstream_enabled"])

            status, _, raw = _http_request(f"{self.base_url}/v1/models")
            self.assertEqual(status, HTTPStatus.OK)
            model_ids = {item["id"] for item in json.loads(raw.decode("utf-8"))["data"]}
            self.assertIn("tts-1", model_ids)
            self.assertIn("edge-tts", model_ids)

            status, _, raw = _http_request(f"{self.base_url}/v1/audio/voices")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertTrue(json.loads(raw.decode("utf-8"))["voices"])

            status, _, raw = _http_request(f"{self.base_url}/api/upstream")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertFalse(json.loads(raw.decode("utf-8"))["enabled"])

    def test_gpu_upstream_forward_works_without_edge_tts(self) -> None:
        # Configure upstream via /api/upstream
        status, _, raw = _http_request(
            f"{self.base_url}/api/upstream",
            method="POST",
            payload={
                "enabled": True,
                "base_url": f"http://127.0.0.1:{self.upstream_port}",
                "api_key": "secret-gpu-token",
            },
        )
        self.assertEqual(status, HTTPStatus.OK)
        updated = json.loads(raw.decode("utf-8"))
        self.assertTrue(updated["enabled"])
        self.assertTrue(updated["has_api_key"])

        # Simulate edge_tts not installed
        with mock.patch.dict(sys.modules, {"edge_tts": None}):
            status, headers, audio = _http_request(
                f"{self.base_url}/v1/audio/speech",
                method="POST",
                payload={"input": "Hello **Shinobu**", "voice": "shinobu"},
            )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(audio, b"RIFFFAKEWAVDATA")
        self.assertEqual(headers.get("X-Shinobu-TTS-Backend"), "gpu-upstream")
        self.assertEqual(len(_FakeUpstreamHandler.recorded_requests), 1)
        recorded = _FakeUpstreamHandler.recorded_requests[0]
        self.assertEqual(recorded["path"], "/v1/audio/speech")
        self.assertEqual(recorded["auth"], "Bearer secret-gpu-token")
        self.assertEqual(recorded["body"], {"input": "Hello Shinobu", "voice": "shinobu"})

    def test_missing_edge_tts_returns_install_hint_on_fallback(self) -> None:
        # Upstream disabled -> direct local edge-tts path
        with mock.patch.dict(sys.modules, {"edge_tts": None}):
            status, _, raw = _http_request(
                f"{self.base_url}/v1/audio/speech",
                method="POST",
                payload={"input": "Testing fallback"},
            )
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        err = json.loads(raw.decode("utf-8"))["error"]["message"]
        self.assertIn(".[tts-edge]", err)
        self.assertIn("edge-tts is not installed", err)

    def test_upstream_failure_falls_back_to_mock_edge_tts(self) -> None:
        tts_gateway.save_upstream_config(
            self.upstream_cfg_path,
            {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{self.upstream_port}",
                "api_key": "",
            },
        )
        _FakeUpstreamHandler.fail_mode = True

        fake_mod = types.ModuleType("edge_tts")

        class _FakeCommunicate:
            def __init__(self, text: str, voice: str, rate: str, pitch: str) -> None:
                self.text = text
                self.voice = voice
                self.rate = rate
                self.pitch = pitch

            async def stream(self):
                yield {"type": "audio", "data": b"ID3MOCKMP3"}

        fake_mod.Communicate = _FakeCommunicate  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"edge_tts": fake_mod}):
            status, headers, audio = _http_request(
                f"{self.base_url}/v1/audio/speech",
                method="POST",
                payload={"input": "Fallback check", "voice": "shinobu", "speed": 1.1},
            )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(audio, b"ID3MOCKMP3")
        self.assertEqual(headers.get("X-Shinobu-TTS-Backend"), "edge-tts-fallback")
        self.assertEqual(headers.get("X-Shinobu-TTS-Voice"), "zh-CN-XiaoyiNeural")
        self.assertTrue(headers.get("X-Shinobu-TTS-Fallback-Reason"))


class TestTTSGatewayCLI(unittest.TestCase):
    def _run_cli(self, argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["SHINOBU_HOME"] = str(home)
        env["SHINOBU_DISABLE_PLUGINS"] = "1"
        env["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "shinobu_core", *argv],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )

    def test_cli_help_and_upstream_crud_and_host_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            res_help = self._run_cli(["tts-gateway", "--help"], home)
            self.assertEqual(res_help.returncode, 0, res_help.stderr)
            for flag in (
                "--host",
                "--port",
                "--voice",
                "--upstream-config",
                "--set-upstream",
                "--upstream-key",
                "--api-key",
                "--clear-upstream",
                "--show-upstream",
                "--json",
            ):
                self.assertIn(flag, res_help.stdout)

            res_set = self._run_cli(
                [
                    "tts-gateway",
                    "--set-upstream",
                    "https://gpu-tunnel.example.com",
                    "--upstream-key",
                    "super-secret-key",
                    "--json",
                ],
                home,
            )
            self.assertEqual(res_set.returncode, 0, res_set.stderr)
            set_out = json.loads(res_set.stdout)
            self.assertTrue(set_out["enabled"])
            self.assertEqual(set_out["base_url"], "https://gpu-tunnel.example.com")
            self.assertTrue(set_out["has_api_key"])
            self.assertNotIn("super-secret-key", res_set.stdout)
            self.assertTrue((home / "tts" / "upstream.json").is_file())

            res_show = self._run_cli(["tts-gateway", "--show-upstream", "--json"], home)
            self.assertEqual(res_show.returncode, 0, res_show.stderr)
            show_out = json.loads(res_show.stdout)
            self.assertTrue(show_out["enabled"])
            self.assertTrue(show_out["has_api_key"])
            self.assertNotIn("super-secret-key", res_show.stdout)

            res_set_alias = self._run_cli(
                [
                    "tts-gateway",
                    "--set-upstream",
                    "https://gpu-alias.example.com",
                    "--api-key",
                    "alias-secret-key",
                    "--json",
                ],
                home,
            )
            self.assertEqual(res_set_alias.returncode, 0, res_set_alias.stderr)
            alias_out = json.loads(res_set_alias.stdout)
            self.assertEqual(alias_out["base_url"], "https://gpu-alias.example.com")
            self.assertTrue(alias_out["has_api_key"])
            self.assertNotIn("alias-secret-key", res_set_alias.stdout)

            res_clear = self._run_cli(["tts-gateway", "--clear-upstream", "--json"], home)
            self.assertEqual(res_clear.returncode, 0, res_clear.stderr)
            clear_out = json.loads(res_clear.stdout)
            self.assertFalse(clear_out["enabled"])
            self.assertEqual(clear_out["base_url"], "")
            self.assertFalse(clear_out["has_api_key"])

            res_bad_host = self._run_cli(["tts-gateway", "--host", "0.0.0.0"], home)
            self.assertNotEqual(res_bad_host.returncode, 0)
            self.assertIn("0.0.0.0", res_bad_host.stderr)


if __name__ == "__main__":
    unittest.main()
