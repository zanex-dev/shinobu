# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core.runtime.classifier."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from shinobu_core.engines.agy_engines import AgySdkEngine
from shinobu_core.engines.openai_compat import GCP_METADATA_API_KEY, OpenAICompatEngine
from shinobu_core.protocol.engine import TurnChunk
from shinobu_core.runtime.classifier import (
    EFFORT_RANK,
    GEMINI_DEFAULT_CLASSIFIER_MODEL,
    VERTEX_DEFAULT_CLASSIFIER_MODEL,
    ClassificationResult,
    ModelComplexityClassifier,
    build_classifier_from_env,
    parse_classified_effort,
    ratchet_effort,
)


class _FakeHttpResponse(io.BytesIO):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False


class TestRatchetAndParsing(unittest.TestCase):
    def test_effort_rank_order(self) -> None:
        self.assertEqual(
            EFFORT_RANK,
            {"minimal": 0, "low": 1, "medium": 2, "high": 3, "max": 4},
        )

    def test_ratchet_effort_monotonic_and_clamping(self) -> None:
        # Upgrade within bounds
        self.assertEqual(ratchet_effort("low", "medium"), "medium")
        self.assertEqual(ratchet_effort("low", "high"), "high")

        # Monotonic: never downgrade
        self.assertEqual(ratchet_effort("high", "low"), "high")
        self.assertEqual(ratchet_effort("medium", "low"), "medium")

        # Clamp upper and lower bounds
        self.assertEqual(
            ratchet_effort("low", "max", min_effort="low", max_effort="high"),
            "high",
        )
        self.assertEqual(
            ratchet_effort("low", "minimal", min_effort="low", max_effort="high"),
            "low",
        )
        # Preserve current if already above max_effort
        self.assertEqual(
            ratchet_effort("max", "low", min_effort="low", max_effort="high"),
            "max",
        )

        with self.assertRaises(ValueError):
            ratchet_effort("low", "high", min_effort="high", max_effort="low")

    def test_parse_classified_effort_variants(self) -> None:
        self.assertEqual(parse_classified_effort("high"), ("high", ""))
        self.assertEqual(parse_classified_effort("  MEDIUM.\n"), ("medium", ""))
        self.assertEqual(
            parse_classified_effort('```json\n{"effort": "high", "reason": "math"}\n```'),
            ("high", "math"),
        )
        self.assertEqual(
            parse_classified_effort("Classification result: low"),
            ("low", ""),
        )
        with self.assertRaises(ValueError):
            parse_classified_effort("uncertain complexity")


class TestModelComplexityClassifier(unittest.TestCase):
    def test_short_circuit_ceiling_zero_ms(self) -> None:
        calls = 0

        def _never_called(_sys: str, _usr: str, _timeout: float) -> str:
            nonlocal calls
            calls += 1
            return "high"

        clf = ModelComplexityClassifier(api_caller=_never_called, max_effort="high")
        # Even when user_text is smalltalk, ceiling check comes first.
        res = clf.classify("你好", current_effort="high")
        self.assertIsInstance(res, ClassificationResult)
        self.assertEqual(res.effort, "high")
        self.assertEqual(res.backend, "short_circuit_ceiling")
        self.assertEqual(res.elapsed_ms, 0)
        self.assertFalse(res.upgraded)
        self.assertEqual(calls, 0)

        res_max = clf.classify("design a distributed consensus protocol", current_effort="max")
        self.assertEqual(res_max.effort, "max")
        self.assertEqual(res_max.backend, "short_circuit_ceiling")
        self.assertEqual(res_max.elapsed_ms, 0)
        self.assertEqual(calls, 0)

    def test_short_circuit_smalltalk_zero_ms(self) -> None:
        calls = 0

        def _never_called(_sys: str, _usr: str, _timeout: float) -> str:
            nonlocal calls
            calls += 1
            return "high"

        clf = ModelComplexityClassifier(api_caller=_never_called)
        for phrase in ("好的！", "  谢谢你~ ", "hello!", "嗯嗯", "   "):
            res = clf.classify(phrase, current_effort="low")
            self.assertEqual(res.effort, "low")
            self.assertEqual(res.backend, "short_circuit_smalltalk")
            self.assertEqual(res.elapsed_ms, 0)
            self.assertFalse(res.upgraded)
        self.assertEqual(calls, 0)

    def test_api_lean_request_payload_and_truncation(self) -> None:
        captured: dict[str, Any] = {}

        def _fake_urlopen(req: Any, timeout: float = 0.0) -> _FakeHttpResponse:
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeHttpResponse(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": '{"effort":"high","reason":"arch"}'}}
                    ]
                }
            )

        clf = ModelComplexityClassifier(
            backend="api",
            api_base_url="https://api.example.test/v1",
            api_key="test-token-123",
            api_model="gemini-2.5-flash-lite",
        )
        long_ctx = "C" * 350
        long_user = "U" * 750

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = clf.classify(long_user, current_effort="low", recent_context=long_ctx)

        self.assertEqual(res.effort, "high")
        self.assertEqual(res.backend, "api")
        self.assertTrue(res.upgraded)
        self.assertEqual(res.reason, "arch")

        self.assertEqual(captured["url"], "https://api.example.test/v1/chat/completions")
        self.assertLessEqual(captured["timeout"], 0.6)
        body = captured["body"]
        self.assertEqual(body["model"], "gemini-2.5-flash-lite")
        self.assertEqual(body["max_tokens"], 16)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["reasoning_effort"], "minimal")
        self.assertFalse(body["stream"])

        user_prompt = body["messages"][1]["content"]
        self.assertIn("C" * 200, user_prompt)
        self.assertNotIn("C" * 201, user_prompt)
        self.assertIn("U" * 500, user_prompt)
        self.assertNotIn("U" * 501, user_prompt)

    def test_gcp_metadata_token_fetching_and_cache(self) -> None:
        urls_called: list[str] = []

        def _fake_urlopen(req: Any, timeout: float = 0.0) -> _FakeHttpResponse:
            urls_called.append(req.full_url)
            if "metadata.google.internal" in req.full_url:
                return _FakeHttpResponse({"access_token": "ya29.mock_gcp", "expires_in": 3600})
            auth = req.get_header("Authorization")
            self.assertEqual(auth, "Bearer ya29.mock_gcp")
            return _FakeHttpResponse(
                {"choices": [{"message": {"content": "medium"}}]}
            )

        clf = ModelComplexityClassifier(
            backend="api",
            api_base_url="https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/l/endpoints/openapi",
            api_key=GCP_METADATA_API_KEY,
            api_model=VERTEX_DEFAULT_CLASSIFIER_MODEL,
        )
        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            r1 = clf.classify("Explain lock-free ring buffer memory ordering", current_effort="low")
            r2 = clf.classify("Compare Raft vs Paxos leader election", current_effort="low")

        self.assertEqual(r1.effort, "medium")
        self.assertEqual(r2.effort, "medium")
        # Metadata token fetched only once due to caching.
        self.assertEqual(sum("metadata.google.internal" in u for u in urls_called), 1)
        self.assertEqual(sum("/chat/completions" in u for u in urls_called), 2)

    def test_auto_ema_prefers_faster_channel_and_switches_on_slowdown(self) -> None:
        def _slow_api(_sys: str, _usr: str, _timeout: float) -> str:
            time.sleep(0.06)
            return "medium"

        def _fast_agy(_sys: str, _usr: str) -> str:
            return "high"

        clf = ModelComplexityClassifier(
            backend="auto",
            api_caller=_slow_api,
            agy_callable=_fast_agy,
            ema_alpha=0.8,
            initial_api_ema_ms=20.0,
            initial_agy_ema_ms=40.0,
        )
        # First call routes to API (20ms < 40ms), but actual latency ~60ms pushes API EMA > 40ms.
        first = clf.classify("Analyze deadlock in async pool", current_effort="low")
        self.assertEqual(first.backend, "api")
        self.assertEqual(first.effort, "medium")
        self.assertGreater(clf.ema_ms["api"], clf.ema_ms["agy"])

        # Subsequent call automatically selects faster AGY channel.
        second = clf.classify("Prove soundness of type inference", current_effort="low")
        self.assertEqual(second.backend, "agy")
        self.assertEqual(second.effort, "high")

    def test_channel_fallback_and_hard_timeout_fail_open(self) -> None:
        def _broken_api(_sys: str, _usr: str, _timeout: float) -> str:
            raise urllib.error.URLError("connection refused")

        def _working_agy(_sys: str, _usr: str) -> str:
            return "high"

        clf_fallback = ModelComplexityClassifier(
            backend="auto",
            api_caller=_broken_api,
            agy_callable=_working_agy,
        )
        res = clf_fallback.classify("Design zero-downtime schema migration", current_effort="low")
        self.assertEqual(res.backend, "agy")
        self.assertEqual(res.effort, "high")
        self.assertTrue(res.upgraded)

        # Both channels fail / AGY hangs past hard timeout -> fail-open to current_effort promptly.
        def _hanging_agy(_sys: str, _usr: str) -> str:
            time.sleep(0.4)
            return "high"

        clf_timeout = ModelComplexityClassifier(
            backend="auto",
            api_caller=_broken_api,
            agy_callable=_hanging_agy,
            agy_timeout_sec=0.05,
        )
        t0 = time.perf_counter()
        res_open = clf_timeout.classify("Refactor distributed scheduler", current_effort="medium")
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.25)
        self.assertEqual(res_open.backend, "fallback")
        self.assertEqual(res_open.effort, "medium")
        self.assertFalse(res_open.upgraded)

    def test_agy_engine_uses_no_tools_and_no_persistence(self) -> None:
        recorded: dict[str, Any] = {}

        class _StubAgyEngine:
            def _stream_with_config(self, req: Any, *, use_persistence: bool):
                recorded["episode_id"] = req.episode_id
                recorded["tools"] = req.tools
                recorded["use_persistence"] = use_persistence
                yield TurnChunk(delta="high", done=True)

        clf = ModelComplexityClassifier(backend="agy", agy_engine=_StubAgyEngine())
        res = clf.classify("Write a formal TLA+ specification", current_effort="low")
        self.assertEqual(res.backend, "agy")
        self.assertEqual(res.effort, "high")
        self.assertEqual(recorded["episode_id"], "")
        self.assertEqual(recorded["tools"], [])
        self.assertFalse(recorded["use_persistence"])


class TestBuildClassifierFromEnv(unittest.TestCase):
    def _clean_classifier_env(self) -> dict[str, str]:
        return {
            k: v for k, v in os.environ.items() if not k.startswith("SHINOBU_CLASSIFIER_")
        }

    def test_disabled_or_no_backend_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = self._clean_classifier_env()
            env["SHINOBU_CLASSIFIER_ENABLED"] = "false"
            main_eng = OpenAICompatEngine(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-2.5-pro",
                api_key="key-1",
            )
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(build_classifier_from_env(home, main_eng))

            env_empty = self._clean_classifier_env()
            with (
                mock.patch.dict(os.environ, env_empty, clear=True),
                mock.patch(
                    "shinobu_core.runtime.classifier._is_agy_sdk_available",
                    return_value=False,
                ),
            ):
                self.assertIsNone(build_classifier_from_env(home, None))

    def test_inherits_vertex_and_gemini_compat_defaults_from_main_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = self._clean_classifier_env()

            vertex_engine = OpenAICompatEngine(
                base_url="https://aiplatform.googleapis.com/v1/projects/demo/locations/global/endpoints/openapi",
                model="google/gemini-2.5-pro",
                api_key=GCP_METADATA_API_KEY,
            )
            gemini_engine = OpenAICompatEngine(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-2.5-pro",
                api_key="ai-studio-key",
            )

            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch(
                    "shinobu_core.runtime.classifier._is_agy_sdk_available",
                    return_value=False,
                ),
            ):
                clf_vertex = build_classifier_from_env(home, vertex_engine)
                self.assertIsNotNone(clf_vertex)
                assert clf_vertex is not None
                self.assertEqual(clf_vertex.api_model, VERTEX_DEFAULT_CLASSIFIER_MODEL)
                self.assertEqual(clf_vertex.api_key, GCP_METADATA_API_KEY)
                self.assertTrue(clf_vertex.has_api_backend)
                self.assertFalse(clf_vertex.has_agy_backend)

                clf_gemini = build_classifier_from_env(home, gemini_engine)
                self.assertIsNotNone(clf_gemini)
                assert clf_gemini is not None
                self.assertEqual(clf_gemini.api_model, GEMINI_DEFAULT_CLASSIFIER_MODEL)
                self.assertEqual(clf_gemini.api_key, "ai-studio-key")

    def test_env_overrides_and_agy_sdk_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = self._clean_classifier_env()
            env.update(
                {
                    "SHINOBU_CLASSIFIER_BACKEND": "auto",
                    "SHINOBU_CLASSIFIER_BASE_URL": "https://custom.gateway.test/v1",
                    "SHINOBU_CLASSIFIER_API_KEY": "override-key",
                    "SHINOBU_CLASSIFIER_MODEL": "custom-fast-classifier",
                    "SHINOBU_CLASSIFIER_TIMEOUT_SEC": "0.45",
                    "SHINOBU_CLASSIFIER_MIN_EFFORT": "minimal",
                    "SHINOBU_CLASSIFIER_MAX_EFFORT": "max",
                }
            )
            agy_main = AgySdkEngine(
                mode="gemini",
                save_dir=str(home / "sessions"),
                session_map_path=str(home / "map.json"),
            )
            with mock.patch.dict(os.environ, env, clear=True):
                clf = build_classifier_from_env(home, agy_main)

            self.assertIsNotNone(clf)
            assert clf is not None
            self.assertTrue(clf.has_api_backend)
            self.assertTrue(clf.has_agy_backend)
            self.assertEqual(clf.api_base_url, "https://custom.gateway.test/v1")
            self.assertEqual(clf.api_key, "override-key")
            self.assertEqual(clf.api_model, "custom-fast-classifier")
            self.assertAlmostEqual(clf.api_timeout_sec, 0.45)
            self.assertAlmostEqual(clf.agy_timeout_sec, 0.45)
            self.assertEqual(clf.min_effort, "minimal")
            self.assertEqual(clf.max_effort, "max")
            # Cloned AGY classifier engine must strip persistence paths.
            self.assertIsNone(clf._agy_engine.save_dir)
            self.assertIsNone(clf._agy_engine.session_map_path)
            self.assertTrue(clf._agy_engine.disable_builtin_tools)


if __name__ == "__main__":
    unittest.main()
