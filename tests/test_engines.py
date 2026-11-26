# -*- coding: utf-8 -*-
"""Unit tests for built-in Shinobu engine adapters."""

from __future__ import annotations

import enum
import inspect
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from shinobu_core.engines import (
    AgyCliEngine,
    AgySdkEngine,
    MockEngine,
    OpenAICompatEngine,
    create_engine,
)
from shinobu_core.protocol.engine import EngineAdapter, ToolSpec, TurnRequest


def _sample_tool() -> ToolSpec:
    return ToolSpec(
        name="lookup_weather",
        description="Look up weather by city name.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        handler=lambda city: f"{city}: sunny 24C",
    )


class TestEngineFactoryAndProtocol(unittest.TestCase):
    def test_lazy_import_does_not_load_google_antigravity(self) -> None:
        self.assertNotIn("google.antigravity", sys.modules)
        eng_sdk = create_engine("agy_sdk", mode="gemini", model="gemini-2.5-flash")
        eng_cli = create_engine("agy_cli", binary="agy")
        self.assertNotIn("google.antigravity", sys.modules)
        self.assertIsInstance(eng_sdk, EngineAdapter)
        self.assertIsInstance(eng_cli, EngineAdapter)

    def test_factory_creates_all_builtin_engines(self) -> None:
        e_mock = create_engine("mock")
        e_openai = create_engine("openai_compat", base_url="http://127.0.0.1:11434/v1", model="llama3")
        e_ollama = create_engine("ollama", base_url="http://127.0.0.1:11434/v1", model="qwen3")
        e_sdk = create_engine("agy-sdk", mode="openai_local", base_url="http://localhost:11434/v1", model="gemma")
        e_cli = create_engine("agy-cli")

        for eng in (e_mock, e_openai, e_ollama, e_sdk, e_cli):
            self.assertIsInstance(eng, EngineAdapter)

        self.assertIsInstance(e_mock, MockEngine)
        self.assertIsInstance(e_openai, OpenAICompatEngine)
        self.assertIsInstance(e_ollama, OpenAICompatEngine)
        self.assertIsInstance(e_sdk, AgySdkEngine)
        self.assertIsInstance(e_cli, AgyCliEngine)

        with self.assertRaises(ValueError):
            create_engine("nonexistent_engine")


class TestMockEngine(unittest.TestCase):
    def test_stream_turn_scripted_and_chunked(self) -> None:
        engine = MockEngine(scripted_replies=["Hello, Shinobu world!", "Second turn"], chunk_size=6)
        req1 = TurnRequest(episode_id="ep1", system_prefix="sys", user_text="hi")
        chunks1 = list(engine.stream_turn(req1))

        deltas = [c.delta for c in chunks1 if c.delta]
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(deltas), "Hello, Shinobu world!")
        self.assertTrue(chunks1[-1].done)
        self.assertIsNotNone(chunks1[-1].usage)

        req2 = TurnRequest(episode_id="ep1", system_prefix="sys", user_text="next")
        chunks2 = list(engine.stream_turn(req2))
        self.assertEqual("".join(c.delta for c in chunks2), "Second turn")

    def test_tool_trigger_and_followup_after_tool_result(self) -> None:
        engine = MockEngine(
            scripted_replies=["Tokyo is sunny 24C."],
            tool_trigger={"weather": {"name": "lookup_weather", "arguments": {"city": "Tokyo"}}},
        )
        tool = _sample_tool()
        req1 = TurnRequest(episode_id="ep2", system_prefix="sys", user_text="check weather", tools=[tool])
        chunks1 = list(engine.stream_turn(req1))
        self.assertEqual(len(chunks1), 1)
        self.assertTrue(chunks1[0].done)
        self.assertEqual(len(chunks1[0].tool_calls), 1)
        call = chunks1[0].tool_calls[0]
        self.assertEqual(call["name"], "lookup_weather")
        self.assertEqual(call["arguments"], {"city": "Tokyo"})

        req2 = TurnRequest(
            episode_id="ep2",
            system_prefix="sys",
            history=[{"role": "tool", "name": "lookup_weather", "content": "Tokyo: sunny 24C"}],
            user_text="check weather",
            tools=[tool],
        )
        chunks2 = list(engine.stream_turn(req2))
        self.assertEqual("".join(c.delta for c in chunks2), "Tokyo is sunny 24C.")
        self.assertEqual(chunks2[-1].tool_calls, [])

    def test_complete_json(self) -> None:
        engine = MockEngine(json_replies=['```json\n{"mood": "calm", "score": 9}\n```'])
        parsed = engine.complete_json("extract", "hello")
        self.assertEqual(parsed, {"mood": "calm", "score": 9})

        default_engine = MockEngine()
        default_parsed = default_engine.complete_json("sys", "summarize me")
        self.assertTrue(default_parsed["ok"])
        self.assertEqual(default_parsed["engine"], "mock")


class TestOpenAICompatEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.received_requests: list[dict[str, Any]] = []
        self.scenario: str = "sse_text"
        self.custom_responder: Any = None
        outer = self

        class FakeOpenAIHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
                outer.received_requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "payload": payload,
                    }
                )

                if outer.custom_responder is not None:
                    status, headers, body_bytes = outer.custom_responder(payload)
                    self.send_response(status)
                    for header_key, header_val in headers.items():
                        self.send_header(header_key, header_val)
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    self.wfile.flush()
                    return

                if outer.scenario == "sse_text":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    events = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Kaka"}}]},
                        {"choices": [{"index": 0, "delta": {"content": "!"}}]},
                        {
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                        },
                    ]
                    for ev in events:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return

                if outer.scenario == "sse_tool_calls":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    events = [
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_abc",
                                                "type": "function",
                                                "function": {"name": "lookup_weather", "arguments": '{"ci'},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {"arguments": 'ty": "Kyoto"}'},
                                            },
                                            {
                                                "index": 1,
                                                "id": "call_def",
                                                "type": "function",
                                                "function": {"name": "lookup_weather", "arguments": '{"city":"Osaka"}'},
                                            },
                                        ]
                                    },
                                }
                            ]
                        },
                        {
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 11},
                        },
                    ]
                    for ev in events:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return

                if outer.scenario == "json_fallback":
                    if "response_format" in payload:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"response_format unsupported"}')
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    body = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": '```json\n{"facts": ["likes donuts"], "importance": 5}\n```',
                                }
                            }
                        ]
                    }
                    self.wfile.write(json.dumps(body).encode("utf-8"))
                    return

                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"simulated server failure")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)

    def test_sse_streaming_text_and_request_headers(self) -> None:
        self.scenario = "sse_text"
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="test-model",
            api_key="sk-secret",
            timeout=10.0,
        )
        req = TurnRequest(
            episode_id="ep-sse",
            system_prefix="You are Shinobu.",
            history=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}],
            user_text="laugh",
        )
        chunks = list(engine.stream_turn(req))
        self.assertEqual("".join(c.delta for c in chunks), "Kaka!")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(chunks[-1].usage, {"prompt_tokens": 12, "completion_tokens": 3})

        self.assertEqual(len(self.received_requests), 1)
        sent = self.received_requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer sk-secret")
        self.assertEqual(sent["payload"]["model"], "test-model")
        self.assertTrue(sent["payload"]["stream"])
        self.assertEqual(sent["payload"]["messages"][0], {"role": "system", "content": "You are Shinobu."})
        self.assertEqual(sent["payload"]["messages"][-1], {"role": "user", "content": "laugh"})

    def test_sse_chunked_tool_calls_reassembly(self) -> None:
        self.scenario = "sse_tool_calls"
        engine = OpenAICompatEngine(base_url=f"http://127.0.0.1:{self.port}/v1", model="tool-model")
        req = TurnRequest(
            episode_id="ep-tools",
            system_prefix="sys",
            user_text="weather in Kyoto and Osaka",
            tools=[_sample_tool()],
        )
        chunks = list(engine.stream_turn(req))
        self.assertTrue(chunks[-1].done)
        self.assertEqual(
            chunks[-1].tool_calls,
            [
                {"id": "call_abc", "name": "lookup_weather", "arguments": {"city": "Kyoto"}},
                {"id": "call_def", "name": "lookup_weather", "arguments": {"city": "Osaka"}},
            ],
        )
        sent_tools = self.received_requests[0]["payload"]["tools"]
        self.assertEqual(sent_tools[0]["type"], "function")
        self.assertEqual(sent_tools[0]["function"]["name"], "lookup_weather")

    def test_vertex_extra_content_preserved_and_omitted_when_absent(self) -> None:
        from shinobu_core.engines.openai_compat import _finalize_tool_buffers

        engine = OpenAICompatEngine(base_url=f"http://127.0.0.1:{self.port}/v1", model="gemini-2.5-pro")
        sig_payload = {"google": {"thought_signature": "sig-vertex-abc"}}
        non_stream_data = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_sig",
                                "type": "function",
                                "function": {"name": "lookup_weather", "arguments": '{"city": "Kyoto"}'},
                                "extra_content": sig_payload,
                            },
                            {
                                "id": "call_plain",
                                "type": "function",
                                "function": {"name": "lookup_weather", "arguments": '{"city": "Osaka"}'},
                            },
                        ],
                    }
                }
            ]
        }
        chunks = list(engine._parse_non_stream_payload(non_stream_data))
        self.assertEqual(len(chunks[-1].tool_calls), 2)
        self.assertEqual(chunks[-1].tool_calls[0]["extra_content"], sig_payload)
        self.assertNotIn("extra_content", chunks[-1].tool_calls[1])
        self.assertEqual(set(chunks[-1].tool_calls[1].keys()), {"id", "name", "arguments"})

        sse_calls = _finalize_tool_buffers(
            {
                0: {
                    "id": "call_sse_sig",
                    "name": "lookup_weather",
                    "arguments_parts": ['{"city":', '"Kyoto"}'],
                    "extra_content": sig_payload,
                },
                1: {
                    "id": "call_sse_plain",
                    "name": "lookup_weather",
                    "arguments_parts": ['{"city":"Osaka"}'],
                    "extra_content": None,
                },
            }
        )
        self.assertEqual(sse_calls[0]["extra_content"], sig_payload)
        self.assertNotIn("extra_content", sse_calls[1])
        self.assertEqual(set(sse_calls[1].keys()), {"id", "name", "arguments"})

    def test_build_messages_normalizes_tool_calls_and_filters_orphan_tools(self) -> None:
        sig_payload = {"google": {"thought_signature": "sig-xyz"}}
        req = TurnRequest(
            episode_id="ep-guard",
            system_prefix="sys",
            history=[
                # Leading orphan tool audit row (no tool_call_id, no preceding assistant)
                {"role": "tool", "content": '{"name": "old_tool", "ok": true}'},
                {"role": "user", "content": "hi"},
                # Orphan tool row after user message
                {"role": "tool", "tool_call_id": "call_orphan", "content": "stale"},
                # Valid assistant with compact tool_call + extra_content and already-standard call
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "lookup_weather",
                            "arguments": {"city": "Kyoto"},
                            "extra_content": sig_payload,
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "lookup_weather", "arguments": '{"city": "Osaka"}'},
                        },
                    ],
                },
                # Valid paired tool results
                {"role": "tool", "tool_call_id": "call_1", "content": {"temp": 22}},
                # Duplicate tool_call_id must be dropped
                {"role": "tool", "tool_call_id": "call_1", "content": "dup"},
                # Unmatched tool_call_id must be dropped without breaking call_2
                {"role": "tool", "tool_call_id": "call_unknown", "content": "bad"},
                {"role": "tool", "tool_call_id": "call_2", "content": "sunny"},
                {"role": "user", "content": "thanks"},
                # Tool after user resets pending_tool_ids
                {"role": "tool", "tool_call_id": "call_2", "content": "late"},
            ],
            user_text="next",
        )
        msgs = OpenAICompatEngine._build_messages(req)
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool", "user", "user"])

        asst_calls = msgs[2]["tool_calls"]
        self.assertEqual(asst_calls[0]["type"], "function")
        self.assertEqual(asst_calls[0]["id"], "call_1")
        self.assertEqual(asst_calls[0]["function"]["name"], "lookup_weather")
        self.assertEqual(asst_calls[0]["function"]["arguments"], '{"city": "Kyoto"}')
        self.assertEqual(asst_calls[0]["extra_content"], sig_payload)
        self.assertEqual(asst_calls[1]["function"]["name"], "lookup_weather")
        self.assertEqual(asst_calls[1]["function"]["arguments"], '{"city": "Osaka"}')
        self.assertNotIn("extra_content", asst_calls[1])

        self.assertEqual(msgs[3]["tool_call_id"], "call_1")
        self.assertIsInstance(msgs[3]["content"], str)
        self.assertEqual(msgs[4]["tool_call_id"], "call_2")
        self.assertEqual(msgs[4]["content"], "sunny")

    def test_complete_json_with_response_format_fallback(self) -> None:
        self.scenario = "json_fallback"
        engine = OpenAICompatEngine(base_url=f"http://127.0.0.1:{self.port}/v1", model="json-model")
        result = engine.complete_json("return json", "extract facts")
        self.assertEqual(result, {"facts": ["likes donuts"], "importance": 5})
        self.assertEqual(len(self.received_requests), 2)
        self.assertIn("response_format", self.received_requests[0]["payload"])
        self.assertNotIn("response_format", self.received_requests[1]["payload"])

    def test_http_error_raises_runtime_error(self) -> None:
        self.scenario = "error_500"
        engine = OpenAICompatEngine(base_url=f"http://127.0.0.1:{self.port}/v1", model="err-model")
        req = TurnRequest(episode_id="ep-err", system_prefix="sys", user_text="boom")
        with self.assertRaises(RuntimeError) as ctx:
            list(engine.stream_turn(req))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_extra_body_passthrough_and_core_fields_protected(self) -> None:
        from shinobu_core.engines.openai_compat import build_vertex_safety_extra_body

        safety = build_vertex_safety_extra_body("OFF")
        injected = {
            **safety,
            "top_p": 0.9,
            "model": "hijacked-model",
            "messages": [{"role": "user", "content": "hijacked"}],
            "stream": False,
            "tools": [{"type": "function", "function": {"name": "evil"}}],
            "response_format": {"type": "text"},
        }
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="safe-model",
            extra_body=injected,
        )

        self.scenario = "sse_text"
        req = TurnRequest(episode_id="ep-extra", system_prefix="sys", user_text="hi")
        chunks = list(engine.stream_turn(req))
        self.assertEqual("".join(c.delta for c in chunks), "Kaka!")
        sse_payload = self.received_requests[0]["payload"]
        self.assertEqual(sse_payload["model"], "safe-model")
        self.assertTrue(sse_payload["stream"])
        self.assertNotIn("tools", sse_payload)
        self.assertNotIn("response_format", sse_payload)
        self.assertEqual(sse_payload["messages"][-1], {"role": "user", "content": "hi"})
        self.assertEqual(sse_payload["top_p"], 0.9)
        self.assertEqual(sse_payload["google"], safety["google"])

        self.scenario = "json_fallback"
        result = engine.complete_json("return json", "extract")
        self.assertEqual(result, {"facts": ["likes donuts"], "importance": 5})
        self.assertEqual(len(self.received_requests), 3)
        json_first = self.received_requests[1]["payload"]
        json_retry = self.received_requests[2]["payload"]
        self.assertEqual(json_first["response_format"], {"type": "json_object"})
        self.assertFalse(json_first["stream"])
        self.assertEqual(json_first["google"], safety["google"])
        self.assertNotIn("response_format", json_retry)
        self.assertEqual(json_retry["google"], safety["google"])

        with self.assertRaises(ValueError):
            OpenAICompatEngine(
                base_url=f"http://127.0.0.1:{self.port}/v1",
                model="safe-model",
                extra_body="not-a-dict",  # type: ignore[arg-type]
            )

    def test_build_vertex_safety_extra_body_normalization(self) -> None:
        from shinobu_core.engines.openai_compat import (
            VERTEX_SAFETY_CATEGORIES,
            build_vertex_safety_extra_body,
        )

        cases = [
            ("off", "OFF"),
            ("OFF", "OFF"),
            ("none", "OFF"),
            ("block_none", "OFF"),
            ("BLOCK-NONE", "OFF"),
            ("lowest", "OFF"),
            ("high", "BLOCK_ONLY_HIGH"),
            ("block_only_high", "BLOCK_ONLY_HIGH"),
            ("medium", "BLOCK_MEDIUM_AND_ABOVE"),
            ("block_medium_and_above", "BLOCK_MEDIUM_AND_ABOVE"),
            ("low", "BLOCK_LOW_AND_ABOVE"),
            ("block_low_and_above", "BLOCK_LOW_AND_ABOVE"),
        ]
        for raw, expected in cases:
            body = build_vertex_safety_extra_body(raw)
            settings = body["google"]["safety_settings"]
            self.assertEqual([item["category"] for item in settings], list(VERTEX_SAFETY_CATEGORIES))
            self.assertTrue(all(item["threshold"] == expected for item in settings))

        for bad in ("", "   ", "invalid_level"):
            with self.assertRaises(ValueError):
                build_vertex_safety_extra_body(bad)

    def test_cli_build_engine_vertex_safety_and_extra_body_env(self) -> None:
        import os
        from unittest import mock

        from shinobu_core.engines.openai_compat import build_vertex_safety_extra_body
        from shinobu_core.server.cli import _build_engine

        base_env = {
            "SHINOBU_BASE_URL": f"http://127.0.0.1:{self.port}/v1",
            "SHINOBU_MODEL": "vertex-model",
            "SHINOBU_API_KEY": "tok",
        }
        dummy_kernel = mock.Mock()
        dummy_kernel.get_engine.return_value = None

        with mock.patch.dict(
            os.environ,
            {**base_env, "SHINOBU_VERTEX_SAFETY": "OFF", "SHINOBU_EXTRA_BODY_JSON": ""},
            clear=False,
        ):
            eng = _build_engine(dummy_kernel, "openai_compat")
            self.assertIsInstance(eng, OpenAICompatEngine)
            self.assertEqual(eng._extra_body, build_vertex_safety_extra_body("OFF"))

        with mock.patch.dict(
            os.environ,
            {
                **base_env,
                "SHINOBU_VERTEX_SAFETY": "high",
                "SHINOBU_EXTRA_BODY_JSON": '{"top_p": 0.8, "google": {"thinking_config": {"thinking_budget": 0}}}',
            },
            clear=False,
        ):
            eng = _build_engine(dummy_kernel, "openai_compat")
            self.assertIsInstance(eng, OpenAICompatEngine)
            self.assertEqual(eng._extra_body["top_p"], 0.8)
            self.assertEqual(eng._extra_body["google"]["thinking_config"], {"thinking_budget": 0})
            self.assertEqual(
                eng._extra_body["google"]["safety_settings"],
                build_vertex_safety_extra_body("high")["google"]["safety_settings"],
            )

        with mock.patch.dict(
            os.environ,
            {**base_env, "SHINOBU_VERTEX_SAFETY": "", "SHINOBU_EXTRA_BODY_JSON": '["not", "dict"]'},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                _build_engine(dummy_kernel, "openai_compat")

    def test_retry_and_fallback_config_validation_and_normalization(self) -> None:
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="primary-model",
            fallback_models=["  ", "primary-model", "fb-1", " fb-1 ", "fb-2"],
            retry_max_attempts=4,
            retry_base_delay_sec=0.5,
            retry_max_delay_sec=6.0,
        )
        self.assertEqual(engine.fallback_models, ("fb-1", "fb-2"))
        self.assertEqual(engine.last_model_used, "primary-model")

        for bad_attempts in (0, -2, True):
            with self.assertRaises(ValueError):
                OpenAICompatEngine(
                    base_url=f"http://127.0.0.1:{self.port}/v1",
                    model="primary-model",
                    retry_max_attempts=bad_attempts,  # type: ignore[arg-type]
                )
        with self.assertRaises(ValueError):
            OpenAICompatEngine(
                base_url=f"http://127.0.0.1:{self.port}/v1",
                model="primary-model",
                retry_base_delay_sec=-0.1,
            )
        with self.assertRaises(ValueError):
            OpenAICompatEngine(
                base_url=f"http://127.0.0.1:{self.port}/v1",
                model="primary-model",
                retry_max_delay_sec=-1.0,
            )

    def test_stream_turn_429_and_503_retry_with_retry_after_and_backoff(self) -> None:
        sse_ok = (
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Recovered'}}]})}\n\n"
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            "data: [DONE]\n\n"
        ).encode("utf-8")
        responses = [
            (429, {"Content-Type": "application/json", "Retry-After": "2.5"}, b'{"error":"rate limit 1"}'),
            (429, {"Content-Type": "application/json", "Retry-After": "99"}, b'{"error":"rate limit 2"}'),
            (503, {"Content-Type": "application/json"}, b'{"error":"service unavailable"}'),
            (200, {"Content-Type": "text/event-stream"}, sse_ok),
        ]
        idx = 0

        def responder(_payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
            nonlocal idx
            resp = responses[idx]
            idx += 1
            return resp

        self.custom_responder = responder
        sleeps: list[float] = []
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="primary-model",
            retry_max_attempts=4,
            retry_base_delay_sec=1.0,
            retry_max_delay_sec=4.0,
            _sleep=sleeps.append,
        )
        req = TurnRequest(episode_id="ep-retry", system_prefix="sys", user_text="hi")
        chunks = list(engine.stream_turn(req))
        text = "".join(c.delta for c in chunks if c.delta)

        self.assertEqual(text, "Recovered")
        self.assertEqual(engine.last_model_used, "primary-model")
        self.assertEqual([r["payload"]["model"] for r in self.received_requests], ["primary-model"] * 4)
        self.assertEqual(len(sleeps), 3)
        self.assertAlmostEqual(sleeps[0], 2.5)
        self.assertAlmostEqual(sleeps[1], 4.0)
        self.assertGreaterEqual(sleeps[2], 4.0)
        self.assertLessEqual(sleeps[2], 5.0)

    def test_stream_turn_429_exhausted_falls_back_in_order(self) -> None:
        sse_ok = (
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Fallback OK'}}]})}\n\n"
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            "data: [DONE]\n\n"
        ).encode("utf-8")
        fb2_attempts = 0

        def responder(payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
            nonlocal fb2_attempts
            model = payload.get("model")
            if model == "primary-model":
                return (429, {"Content-Type": "application/json"}, b'{"error":"primary quota"}')
            if model == "fallback-1":
                return (503, {"Content-Type": "application/json"}, b'{"error":"fb1 overloaded"}')
            fb2_attempts += 1
            if fb2_attempts == 1:
                return (429, {"Content-Type": "application/json"}, b'{"error":"fb2 transient"}')
            return (200, {"Content-Type": "text/event-stream"}, sse_ok)

        self.custom_responder = responder
        sleeps: list[float] = []
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="primary-model",
            fallback_models=["fallback-1", "fallback-2"],
            retry_max_attempts=2,
            retry_base_delay_sec=0.25,
            retry_max_delay_sec=2.0,
            _sleep=sleeps.append,
        )
        req = TurnRequest(episode_id="ep-fb", system_prefix="sys", user_text="hi")
        with self.assertLogs("shinobu_core.engines.openai_compat", level="WARNING") as logs:
            chunks = list(engine.stream_turn(req))
        text = "".join(c.delta for c in chunks if c.delta)

        self.assertEqual(text, "Fallback OK")
        self.assertEqual(engine.last_model_used, "fallback-2")
        self.assertEqual(
            [r["payload"]["model"] for r in self.received_requests],
            ["primary-model", "primary-model", "fallback-1", "fallback-1", "fallback-2", "fallback-2"],
        )
        self.assertEqual(len(sleeps), 3)
        joined_logs = "\n".join(logs.output)
        self.assertIn("falling back to model=fallback-1", joined_logs)
        self.assertIn("falling back to model=fallback-2", joined_logs)

    def test_complete_json_429_retries_and_falls_back(self) -> None:
        def responder(payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
            if payload.get("model") == "primary-json":
                return (429, {"Content-Type": "application/json"}, b'{"error":"rate limited"}')
            body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"ok": true, "source": "fallback"}',
                        }
                    }
                ]
            }
            return (200, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8"))

        self.custom_responder = responder
        sleeps: list[float] = []
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="primary-json",
            fallback_models=["fallback-json"],
            retry_max_attempts=2,
            retry_base_delay_sec=0.1,
            retry_max_delay_sec=1.0,
            _sleep=sleeps.append,
        )
        result = engine.complete_json("return json", "extract")
        self.assertEqual(result, {"ok": True, "source": "fallback"})
        self.assertEqual(engine.last_model_used, "fallback-json")
        self.assertEqual(
            [r["payload"]["model"] for r in self.received_requests],
            ["primary-json", "primary-json", "fallback-json"],
        )
        self.assertEqual(len(sleeps), 1)

    def test_open_request_does_not_mutate_caller_payload_and_raises_on_exhaustion(self) -> None:
        def responder(payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
            return (
                429,
                {"Content-Type": "application/json"},
                f'{{"error":"exhausted on {payload.get("model")}"}}'.encode("utf-8"),
            )

        self.custom_responder = responder
        sleeps: list[float] = []
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="primary-model",
            fallback_models=["fallback-1"],
            retry_max_attempts=2,
            retry_base_delay_sec=0.1,
            retry_max_delay_sec=1.0,
            _sleep=sleeps.append,
        )
        caller_payload = {"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(RuntimeError) as ctx:
            engine._open_request(caller_payload, stream=False)

        self.assertEqual(caller_payload["model"], "primary-model")
        self.assertIn("OpenAICompatEngine HTTP 429 from", str(ctx.exception))
        self.assertIn("exhausted on fallback-1", str(ctx.exception))
        self.assertEqual(
            [r["payload"]["model"] for r in self.received_requests],
            ["primary-model", "primary-model", "fallback-1", "fallback-1"],
        )
        self.assertEqual(len(sleeps), 2)

    def test_non_retryable_400_and_500_fail_immediately_without_retry_or_fallback(self) -> None:
        for status_code in (400, 500):
            self.received_requests.clear()
            sleeps: list[float] = []

            def responder(_payload: dict[str, Any], code: int = status_code) -> tuple[int, dict[str, str], bytes]:
                return (code, {"Content-Type": "text/plain"}, f"fatal {code}".encode("utf-8"))

            self.custom_responder = responder
            engine = OpenAICompatEngine(
                base_url=f"http://127.0.0.1:{self.port}/v1",
                model="primary-model",
                fallback_models=["fallback-1"],
                retry_max_attempts=3,
                retry_base_delay_sec=0.5,
                retry_max_delay_sec=4.0,
                _sleep=sleeps.append,
            )
            req = TurnRequest(episode_id=f"ep-{status_code}", system_prefix="sys", user_text="boom")
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(req))
            self.assertIn(f"HTTP {status_code}", str(ctx.exception))
            self.assertEqual(len(self.received_requests), 1)
            self.assertEqual(self.received_requests[0]["payload"]["model"], "primary-model")
            self.assertEqual(sleeps, [])

    def test_single_turn_reasoning_effort_overrides_without_mutating_extra_body(self) -> None:
        self.scenario = "sse_text"
        engine = OpenAICompatEngine(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="gemini-3-pro",
            extra_body={"reasoning_effort": "low", "custom_flag": True},
        )
        self.assertEqual(engine.reasoning_effort, "low")

        # 1. Single-turn override normalizes and wins over extra_body without mutating _extra_body.
        list(
            engine.stream_turn(
                TurnRequest(
                    episode_id="ep-eff-1",
                    system_prefix="sys",
                    user_text="hard task",
                    reasoning_effort="HIGH",
                )
            )
        )
        self.assertEqual(self.received_requests[-1]["payload"]["reasoning_effort"], "high")
        self.assertEqual(self.received_requests[-1]["payload"]["custom_flag"], True)
        self.assertEqual(engine.reasoning_effort, "low")
        self.assertEqual(engine._extra_body["reasoning_effort"], "low")

        # 2. Empty TurnRequest.reasoning_effort falls back to engine's default extra_body.
        list(
            engine.stream_turn(
                TurnRequest(
                    episode_id="ep-eff-1",
                    system_prefix="sys",
                    user_text="easy task",
                )
            )
        )
        self.assertEqual(self.received_requests[-1]["payload"]["reasoning_effort"], "low")

        # 3. Alias normalization ("med" -> "medium") and per-turn clear ("default").
        list(
            engine.stream_turn(
                TurnRequest(
                    episode_id="ep-eff-1",
                    system_prefix="sys",
                    user_text="med task",
                    reasoning_effort="med",
                )
            )
        )
        self.assertEqual(self.received_requests[-1]["payload"]["reasoning_effort"], "medium")

        list(
            engine.stream_turn(
                TurnRequest(
                    episode_id="ep-eff-1",
                    system_prefix="sys",
                    user_text="clear task",
                    reasoning_effort="default",
                )
            )
        )
        self.assertNotIn("reasoning_effort", self.received_requests[-1]["payload"])
        self.assertEqual(engine.reasoning_effort, "low")


class TestAgySdkEngine(unittest.TestCase):
    def _build_fake_sdk(self) -> tuple[dict[str, Any], dict[str, types.ModuleType]]:
        recorded: dict[str, Any] = {"configs": [], "prompts": [], "config_classes": []}

        class FakeBuiltinTools(enum.Enum):
            RUN_COMMAND = "run_command"
            EDIT_FILE = "edit_file"
            VIEW_FILE = "view_file"

        class FakeCustomSystemInstructions:
            def __init__(self, text: str) -> None:
                self.text = text

        class BaseFakeConfig:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                recorded["config_classes"].append(type(self).__name__)
                recorded["configs"].append(kwargs)

        class FakeLocalAgentConfig(BaseFakeConfig):
            pass

        class FakeLocalOpenAIAgentConfig(BaseFakeConfig):
            pass

        class FakeLiteRTAgentConfig(BaseFakeConfig):
            pass

        class FakeResponse:
            def __init__(self, text_value: str, streamed: list[str] | None = None) -> None:
                self._text = text_value
                self._streamed = streamed
                self.usage = {"prompt_tokens": 5, "completion_tokens": 4}

            async def text(self) -> str:
                return self._text

            async def _gen(self):
                assert self._streamed is not None
                for piece in self._streamed:
                    yield piece

            def stream(self):
                if self._streamed is not None:
                    return self._gen()
                return []

        class FakeAgent:
            def __init__(self, config: Any) -> None:
                self.config = config
                self.conversation_id = config.kwargs.get("conversation_id") or "conv-persisted-001"

            async def __aenter__(self) -> "FakeAgent":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            async def chat(self, prompt: str) -> FakeResponse:
                recorded["prompts"].append(prompt)
                if "JSON_TEST" in prompt:
                    return FakeResponse('{"status": "ok", "count": 2}')
                return FakeResponse("hello sdk", streamed=["hello ", "sdk"])

        google_pkg = types.ModuleType("google")
        agy_mod = types.ModuleType("google.antigravity")
        agy_types_mod = types.ModuleType("google.antigravity.types")

        agy_mod.Agent = FakeAgent  # type: ignore[attr-defined]
        agy_mod.LocalAgentConfig = FakeLocalAgentConfig  # type: ignore[attr-defined]
        agy_mod.LocalOpenAIAgentConfig = FakeLocalOpenAIAgentConfig  # type: ignore[attr-defined]
        agy_mod.LiteRTAgentConfig = FakeLiteRTAgentConfig  # type: ignore[attr-defined]

        agy_types_mod.CustomSystemInstructions = FakeCustomSystemInstructions  # type: ignore[attr-defined]
        agy_types_mod.BuiltinTools = FakeBuiltinTools  # type: ignore[attr-defined]

        fake_modules = {
            "google": google_pkg,
            "google.antigravity": agy_mod,
            "google.antigravity.types": agy_types_mod,
        }
        return recorded, fake_modules

    def test_invalid_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            AgySdkEngine(mode="unknown_mode")

    def test_missing_sdk_raises_friendly_runtime_error(self) -> None:
        engine = AgySdkEngine(mode="gemini", model="gemini-2.5-flash")
        req = TurnRequest(episode_id="ep-miss", system_prefix="sys", user_text="hi")
        with mock.patch("importlib.import_module", side_effect=ModuleNotFoundError("google.antigravity")):
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(req))
        self.assertIn("google-antigravity", str(ctx.exception))

    def test_sdk_gemini_persistence_custom_si_and_tools(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()
        with mock.patch.dict(sys.modules, fake_modules):
            engine = AgySdkEngine(
                mode="gemini",
                model="gemini-2.5-flash",
                save_dir="/tmp/shinobu_save",
            )
            tool = _sample_tool()
            req1 = TurnRequest(
                episode_id="ep-persist",
                system_prefix="Frozen Shinobu Persona",
                user_text="first hello",
                tools=[tool],
            )
            chunks1 = list(engine.stream_turn(req1))
            self.assertEqual("".join(c.delta for c in chunks1), "hello sdk")
            self.assertTrue(chunks1[-1].done)

            cfg1 = recorded["configs"][0]
            self.assertEqual(recorded["config_classes"][0], "FakeLocalAgentConfig")
            self.assertEqual(cfg1["model"], "gemini-2.5-flash")
            self.assertEqual(cfg1["save_dir"], "/tmp/shinobu_save")
            self.assertNotIn("conversation_id", cfg1)
            self.assertEqual(cfg1["system_instructions"].text, "Frozen Shinobu Persona")
            self.assertGreaterEqual(len(cfg1["disabled_tools"]), 3)
            self.assertEqual(len(cfg1["tools"]), 1)
            self.assertEqual(cfg1["tools"][0](city="Sendai"), "Sendai: sunny 24C")

            # Second turn with same episode_id must resume conversation_id.
            req2 = TurnRequest(
                episode_id="ep-persist",
                system_prefix="Frozen Shinobu Persona",
                user_text="second hello",
            )
            list(engine.stream_turn(req2))
            cfg2 = recorded["configs"][1]
            self.assertEqual(cfg2["conversation_id"], "conv-persisted-001")

            # complete_json should parse JSON and skip persistence.
            data = engine.complete_json("json sys", "JSON_TEST")
            self.assertEqual(data, {"status": "ok", "count": 2})
            self.assertNotIn("save_dir", recorded["configs"][2])

    def test_sdk_openai_local_and_litert_modes(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()
        with mock.patch.dict(sys.modules, fake_modules):
            eng_local = AgySdkEngine(
                mode="openai_local",
                model="gemma3",
                base_url="http://127.0.0.1:11434/v1",
            )
            list(eng_local.stream_turn(TurnRequest(episode_id="ep-l", system_prefix="s", user_text="hi")))
            self.assertEqual(recorded["config_classes"][0], "FakeLocalOpenAIAgentConfig")
            self.assertEqual(recorded["configs"][0]["base_url"], "http://127.0.0.1:11434/v1")

            eng_litert = AgySdkEngine(mode="litert", model_path="/models/gemma.litertlm")
            list(eng_litert.stream_turn(TurnRequest(episode_id="ep-r", system_prefix="s", user_text="hi")))
            self.assertEqual(recorded["config_classes"][1], "FakeLiteRTAgentConfig")
            self.assertEqual(recorded["configs"][1]["model_path"], "/models/gemma.litertlm")

    def test_sdk_app_data_dir_isolation_policies_and_tool_signature_reflection(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()

        hooks_pkg = types.ModuleType("google.antigravity.hooks")
        policy_mod = types.ModuleType("google.antigravity.hooks.policy")
        policy_mod.deny_all = lambda: ("deny_all",)  # type: ignore[attr-defined]
        policy_mod.allow = lambda tool_name: ("allow", tool_name)  # type: ignore[attr-defined]
        fake_modules["google.antigravity.hooks"] = hooks_pkg
        fake_modules["google.antigravity.hooks.policy"] = policy_mod

        def typed_weather(city: str, unit: str = "C") -> str:
            """Original weather docstring."""
            return f"{city}:{unit}"

        def typed_note(title: str, body: str) -> str:
            return f"{title}={body}"

        tool1 = ToolSpec(
            name="lookup_weather",
            description="Look up weather with unit.",
            parameters={"type": "object", "properties": {"city": {"type": "string"}}},
            handler=typed_weather,
        )
        tool2 = ToolSpec(
            name="save_note",
            description="Save a short note.",
            parameters={"type": "object", "properties": {"title": {"type": "string"}}},
            handler=typed_note,
        )

        with mock.patch.dict(sys.modules, fake_modules):
            rel_data_dir = os.path.join("isolated_agy_runtime", "brain_root")
            engine = AgySdkEngine(
                mode="gemini",
                model="gemini-2.5-flash",
                app_data_dir=rel_data_dir,
                disable_builtin_tools=True,
            )
            req = TurnRequest(
                episode_id="ep-iso",
                system_prefix="Persona",
                user_text="check weather",
                tools=[tool1, tool2],
            )
            list(engine.stream_turn(req))

            cfg = recorded["configs"][0]
            self.assertTrue(os.path.isabs(cfg["app_data_dir"]))
            self.assertEqual(cfg["app_data_dir"], os.path.abspath(rel_data_dir))
            self.assertEqual(
                cfg["policies"],
                [("deny_all",), ("allow", "lookup_weather"), ("allow", "save_note")],
            )

            wrapped = cfg["tools"][0]
            sig = inspect.signature(wrapped)
            self.assertEqual(list(sig.parameters.keys()), ["city", "unit"])
            self.assertEqual(sig.parameters["unit"].default, "C")
            self.assertEqual(wrapped.__name__, "lookup_weather")
            self.assertEqual(wrapped.__doc__, "Look up weather with unit.")
            self.assertEqual(wrapped("Tokyo"), "Tokyo:C")
            self.assertEqual(wrapped(city="Osaka", unit="F"), "Osaka:F")

            # disable_builtin_tools=False omits policies and disabled_tools.
            engine_open = AgySdkEngine(mode="gemini", disable_builtin_tools=False)
            list(engine_open.stream_turn(TurnRequest(episode_id="ep-open", system_prefix="s", user_text="hi")))
            cfg_open = recorded["configs"][1]
            self.assertNotIn("policies", cfg_open)
            self.assertNotIn("disabled_tools", cfg_open)

    def test_sdk_native_aiter_response_async_tool_calls_not_reemitted_and_total_usage(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()
        drained_calls: list[str] = []

        class FakeTotalUsage:
            prompt_token_count = 120
            cached_content_token_count = 80
            candidates_token_count = 30
            thoughts_token_count = 15
            total_token_count = 165

        class FakeConversation:
            id = "conv-native-42"
            total_usage = FakeTotalUsage()

        class FakeNativeResponse:
            def __init__(self) -> None:
                self._tokens = ["Hello ", "native ", "SDK"]

            def __aiter__(self):
                async def _iter():
                    for tok in self._tokens:
                        yield tok

                return _iter()

            @property
            def tool_calls(self):
                async def _calls():
                    for name in ("lookup_weather", "save_note"):
                        drained_calls.append(name)
                        yield types.SimpleNamespace(name=name, arguments={"k": "v"})

                return _calls()

        class FakeNativeAgent:
            def __init__(self, config: Any) -> None:
                self.config = config
                self.conversation = FakeConversation()

            async def __aenter__(self) -> "FakeNativeAgent":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            async def chat(self, prompt: str) -> FakeNativeResponse:
                recorded["prompts"].append(prompt)
                return FakeNativeResponse()

        fake_modules["google.antigravity"].Agent = FakeNativeAgent  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, fake_modules):
            engine = AgySdkEngine(mode="gemini", model="gemini-2.5-pro")
            chunks = list(
                engine.stream_turn(
                    TurnRequest(episode_id="ep-native", system_prefix="sys", user_text="hi")
                )
            )

        self.assertEqual("".join(c.delta for c in chunks), "Hello native SDK")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(drained_calls, ["lookup_weather", "save_note"])
        # SDK already executed tools internally; must NOT return tool_calls to Orchestrator.
        self.assertEqual(list(chunks[-1].tool_calls), [])
        self.assertEqual(
            chunks[-1].usage,
            {
                "prompt_token_count": 120,
                "cached_content_token_count": 80,
                "candidates_token_count": 30,
                "thoughts_token_count": 15,
                "total_token_count": 165,
            },
        )

    def test_sdk_session_map_persistence_across_restart(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "state" / "sdk_sessions.json"
            brain_dir = Path(tmpdir) / "brain"

            with mock.patch.dict(sys.modules, fake_modules):
                eng1 = AgySdkEngine(
                    mode="gemini",
                    model="gemini-2.5-flash",
                    app_data_dir=str(brain_dir),
                    session_map_path=str(session_map),
                )
                list(
                    eng1.stream_turn(
                        TurnRequest(
                            episode_id="ep-restart",
                            system_prefix="Persona",
                            history=[{"role": "user", "content": "old turn"}],
                            user_text="first turn",
                        )
                    )
                )
                self.assertTrue(session_map.is_file())
                persisted = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(persisted, {"ep-restart": "conv-persisted-001"})
                self.assertIn("old turn", recorded["prompts"][0])

                # Fresh engine instance simulates process restart and resumes conversation_id.
                eng2 = AgySdkEngine(
                    mode="gemini",
                    model="gemini-2.5-flash",
                    app_data_dir=str(brain_dir),
                    session_map_path=str(session_map),
                )
                list(
                    eng2.stream_turn(
                        TurnRequest(
                            episode_id="ep-restart",
                            system_prefix="Persona",
                            history=[{"role": "user", "content": "old turn"}],
                            user_text="second turn",
                        )
                    )
                )
                self.assertEqual(recorded["configs"][1]["conversation_id"], "conv-persisted-001")
                self.assertEqual(recorded["prompts"][1], "second turn")

    def test_sdk_reasoning_effort_property_and_single_turn_override(self) -> None:
        recorded, fake_modules = self._build_fake_sdk()
        with mock.patch.dict(sys.modules, fake_modules):
            engine = AgySdkEngine(
                mode="gemini",
                model="gemini-2.5-pro",
                effort="low",
            )
            self.assertEqual(engine.reasoning_effort, "low")

            # Turn 1: single-turn override to "high" does not mutate engine default.
            list(
                engine.stream_turn(
                    TurnRequest(
                        episode_id="ep-sdk-eff",
                        system_prefix="sys",
                        user_text="analyze",
                        reasoning_effort="high",
                    )
                )
            )
            self.assertEqual(recorded["configs"][0]["reasoning_effort"], "high")
            self.assertEqual(recorded["configs"][0]["effort"], "high")
            self.assertEqual(engine.reasoning_effort, "low")

            # Turn 2: empty TurnRequest.reasoning_effort falls back to engine.reasoning_effort.
            list(
                engine.stream_turn(
                    TurnRequest(
                        episode_id="ep-sdk-eff",
                        system_prefix="sys",
                        user_text="chat",
                    )
                )
            )
            self.assertEqual(recorded["configs"][1]["reasoning_effort"], "low")

            # Runtime hot-switch & clear via property setter.
            engine.reasoning_effort = "med"
            self.assertEqual(engine.reasoning_effort, "medium")
            engine.reasoning_effort = "default"
            self.assertEqual(engine.reasoning_effort, "")


class _BlockingFakeStdout:
    def __init__(self, lines: list[str], release_event: threading.Event) -> None:
        self._lines = list(lines)
        self._release_event = release_event
        self._index = 0
        self.closed = False

    def __iter__(self) -> "_BlockingFakeStdout":
        return self

    def __next__(self) -> str:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        self._release_event.wait(timeout=5.0)
        if self.closed:
            raise ValueError("I/O operation on closed file")
        raise StopIteration

    def close(self) -> None:
        self.closed = True
        self._release_event.set()


class _FakePopenProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_text: str = "",
        returncode: int = 0,
        *,
        hang_stdout: bool = False,
    ) -> None:
        self._release_event = threading.Event()
        self.stdout: Any = (
            _BlockingFakeStdout(stdout_lines, self._release_event)
            if hang_stdout
            else io.StringIO("".join(stdout_lines))
        )
        self.stderr = io.StringIO(stderr_text)
        self._returncode = returncode
        self._waited = False
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode if self._waited else None

    def wait(self, timeout: float | None = None) -> int:
        self._waited = True
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._waited = True
        self._release_event.set()

    def kill(self) -> None:
        self.killed = True
        self._waited = True
        self._release_event.set()


class TestAgyCliEngine(unittest.TestCase):
    def test_missing_binary_raises_friendly_runtime_error(self) -> None:
        engine = AgyCliEngine(binary="agy")
        req = TurnRequest(episode_id="ep-cli-miss", system_prefix="sys", user_text="hello")
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(req))
            self.assertIn("agy", str(ctx.exception))

            with self.assertRaises(RuntimeError):
                engine.complete_json("sys", "prompt")

    def test_stream_json_parsing_and_cli_flags(self) -> None:
        lines = [
            json.dumps({"type": "chunk", "delta": "Hello "}) + "\n",
            json.dumps({"type": "chunk", "delta": "from agy"}) + "\n",
            json.dumps(
                {
                    "type": "tool_call",
                    "tool_call": {"id": "tc_1", "name": "lookup_weather", "arguments": '{"city":"Tokyo"}'},
                }
            )
            + "\n",
            json.dumps({"type": "done", "usage": {"prompt_tokens": 9, "completion_tokens": 4}}) + "\n",
        ]
        fake_proc = _FakePopenProcess(lines, returncode=0)

        with (
            mock.patch("shutil.which", return_value="/usr/local/bin/agy"),
            mock.patch("subprocess.Popen", return_value=fake_proc) as popen_mock,
        ):
            engine = AgyCliEngine(binary="agy", cwd="C:/work")
            req = TurnRequest(episode_id="conv-42", system_prefix="Persona", user_text="say hi")
            chunks = list(engine.stream_turn(req))

        cmd = popen_mock.call_args.args[0]
        self.assertEqual(cmd[0], "/usr/local/bin/agy")
        self.assertEqual(cmd[1], "-p")
        self.assertIn("Persona", cmd[2])
        self.assertIn("say hi", cmd[2])
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        # First turn must NOT pass synthetic episode_id as --conversation before agy returns a real id.
        self.assertNotIn("--conversation", cmd)
        self.assertEqual(popen_mock.call_args.kwargs["cwd"], "C:/work")

        self.assertEqual("".join(c.delta for c in chunks), "Hello from agy")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(chunks[-1].usage, {"prompt_tokens": 9, "completion_tokens": 4})
        self.assertEqual(
            chunks[-1].tool_calls,
            [{"id": "tc_1", "name": "lookup_weather", "arguments": {"city": "Tokyo"}}],
        )

    def test_cli_model_flag_captured_conversation_and_cross_restart_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "cli_sessions.json"

            turn1_lines = [
                json.dumps({"type": "init", "conversation_id": "agy-real-conv-777"}) + "\n",
                json.dumps({"type": "chunk", "delta": "First reply"}) + "\n",
                json.dumps({"type": "done"}) + "\n",
            ]
            turn2_lines = [
                json.dumps({"type": "chunk", "delta": "Second reply"}) + "\n",
                json.dumps({"type": "done", "session_id": "agy-real-conv-777"}) + "\n",
            ]
            turn3_lines = [
                json.dumps({"type": "result", "session_id": "agy-session-888", "result": "Third reply"}) + "\n",
            ]

            with (
                mock.patch("shutil.which", return_value="/usr/local/bin/agy"),
                mock.patch(
                    "subprocess.Popen",
                    side_effect=[
                        _FakePopenProcess(turn1_lines, returncode=0),
                        _FakePopenProcess(turn2_lines, returncode=0),
                        _FakePopenProcess(turn3_lines, returncode=0),
                    ],
                ) as popen_mock,
            ):
                eng1 = AgyCliEngine(
                    binary="agy",
                    model="gemini-2.5-flash",
                    session_map_path=str(session_map),
                )
                req1 = TurnRequest(
                    episode_id="ep-cli-resume",
                    system_prefix="Frozen Persona",
                    history=[{"role": "user", "content": "prior context"}],
                    user_text="first question",
                )
                chunks1 = list(eng1.stream_turn(req1))
                self.assertEqual("".join(c.delta for c in chunks1), "First reply")

                cmd1 = popen_mock.call_args_list[0].args[0]
                self.assertIn("--model", cmd1)
                self.assertEqual(cmd1[cmd1.index("--model") + 1], "gemini-2.5-flash")
                self.assertNotIn("--conversation", cmd1)
                self.assertIn("Frozen Persona", cmd1[2])
                self.assertIn("prior context", cmd1[2])
                self.assertIn("first question", cmd1[2])

                persisted = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(persisted, {"ep-cli-resume": "agy-real-conv-777"})

                # Fresh engine instance simulates restart; must resume with --conversation and send only user_text.
                eng2 = AgyCliEngine(
                    binary="agy",
                    model="gemini-2.5-flash",
                    session_map_path=str(session_map),
                )
                req2 = TurnRequest(
                    episode_id="ep-cli-resume",
                    system_prefix="Frozen Persona",
                    history=[{"role": "user", "content": "prior context"}],
                    user_text="follow-up only",
                )
                chunks2 = list(eng2.stream_turn(req2))
                self.assertEqual("".join(c.delta for c in chunks2), "Second reply")

                cmd2 = popen_mock.call_args_list[1].args[0]
                self.assertIn("--model", cmd2)
                self.assertEqual(cmd2[cmd2.index("--model") + 1], "gemini-2.5-flash")
                self.assertIn("--conversation", cmd2)
                self.assertEqual(cmd2[cmd2.index("--conversation") + 1], "agy-real-conv-777")
                self.assertEqual(cmd2[2], "follow-up only")
                self.assertNotIn("Frozen Persona", cmd2[2])
                self.assertNotIn("prior context", cmd2[2])

                # Also verifies session_id capture for another episode_id.
                req3 = TurnRequest(
                    episode_id="ep-cli-session-id",
                    system_prefix="Persona",
                    user_text="hello session",
                )
                chunks3 = list(eng2.stream_turn(req3))
                self.assertEqual("".join(c.delta for c in chunks3), "Third reply")
                persisted_after = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(
                    persisted_after,
                    {
                        "ep-cli-resume": "agy-real-conv-777",
                        "ep-cli-session-id": "agy-session-888",
                    },
                )

    def test_complete_json_and_nonzero_exit(self) -> None:
        json_lines = [json.dumps({"type": "result", "result": '{"ok": true, "via": "cli"}'}) + "\n"]
        with (
            mock.patch("shutil.which", return_value="/usr/local/bin/agy"),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(json_lines, returncode=0)),
        ):
            engine = AgyCliEngine(binary="agy")
            parsed = engine.complete_json("sys", "give json")
            self.assertEqual(parsed, {"ok": True, "via": "cli"})

        err_proc = _FakePopenProcess([], stderr_text="auth failed", returncode=2)
        with (
            mock.patch("shutil.which", return_value="/usr/local/bin/agy"),
            mock.patch("subprocess.Popen", return_value=err_proc),
        ):
            engine = AgyCliEngine(binary="agy")
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(TurnRequest(episode_id="c1", system_prefix="", user_text="hi")))
            self.assertIn("code 2", str(ctx.exception))
            self.assertIn("auth failed", str(ctx.exception))

    def test_nested_live_stream_json_events_and_path_fallback(self) -> None:
        from shinobu_core.engines.agy_engines import _augmented_agy_path

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "/tmp/localappdata", "PATH": "/usr/bin"}, clear=False):
            augmented = _augmented_agy_path()
            self.assertIn(str(Path("/tmp/localappdata") / "agy" / "bin"), augmented)
            self.assertIn(str(Path.home() / ".local" / "bin"), augmented)

        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "live_cli_sessions.json"
            turn1_lines = [
                json.dumps({"event": "init", "conversation_id": "agy-live-conv-1", "init": {"mode": "cli"}}) + "\n",
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "agy-live-conv-1",
                            "text_delta": "Hello ",
                            "tool_info": {"name": "internal_read", "status": "done"},
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "agy-live-conv-1",
                            "text_delta": "Master.",
                        },
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "agy-live-conv-1",
                            "status": "SUCCESS",
                            "response": "Hello Master.",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        },
                    }
                )
                + "\n",
            ]
            turn2_lines = [
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "agy-live-conv-1",
                            "status": "SUCCESS",
                            "response": "Fallback response only.",
                            "usage": {"input_tokens": 6, "output_tokens": 3},
                        },
                    }
                )
                + "\n",
            ]
            turn3_lines = [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": "agy-step-only-conv",
                            "text_delta": "Captured via step_update",
                        },
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "agy-step-only-conv",
                            "status": "SUCCESS",
                            "response": "Captured via step_update",
                        },
                    }
                )
                + "\n",
            ]

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy") as which_mock,
                mock.patch(
                    "subprocess.Popen",
                    side_effect=[
                        _FakePopenProcess(turn1_lines, returncode=0),
                        _FakePopenProcess(turn2_lines, returncode=0),
                        _FakePopenProcess(turn3_lines, returncode=0),
                    ],
                ) as popen_mock,
            ):
                engine = AgyCliEngine(binary="agy", session_map_path=str(session_map))
                chunks1 = list(
                    engine.stream_turn(
                        TurnRequest(episode_id="ep-live", system_prefix="Persona", user_text="turn 1")
                    )
                )
                self.assertEqual("".join(c.delta for c in chunks1), "Hello Master.")
                self.assertTrue(chunks1[-1].done)
                self.assertEqual(chunks1[-1].tool_calls, [])
                self.assertEqual(chunks1[-1].usage, {"input_tokens": 10, "output_tokens": 5})

                cmd1 = popen_mock.call_args_list[0].args[0]
                self.assertIn("--disable-slash-commands", cmd1)
                self.assertIn("path", which_mock.call_args.kwargs)

                # Second turn reuses captured conversation_id and tests result.response fallback.
                chunks2 = list(
                    engine.stream_turn(
                        TurnRequest(episode_id="ep-live", system_prefix="Persona", user_text="turn 2")
                    )
                )
                self.assertEqual("".join(c.delta for c in chunks2), "Fallback response only.")
                self.assertEqual(chunks2[-1].usage, {"input_tokens": 6, "output_tokens": 3})
                cmd2 = popen_mock.call_args_list[1].args[0]
                self.assertIn("--conversation", cmd2)
                self.assertEqual(cmd2[cmd2.index("--conversation") + 1], "agy-live-conv-1")

                # Third turn verifies conversation_id capture from nested step_update/result dicts.
                chunks3 = list(
                    engine.stream_turn(
                        TurnRequest(episode_id="ep-nested-id", system_prefix="", user_text="turn 3")
                    )
                )
                self.assertEqual("".join(c.delta for c in chunks3), "Captured via step_update")
                persisted = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(
                    persisted,
                    {
                        "ep-live": "agy-live-conv-1",
                        "ep-nested-id": "agy-step-only-conv",
                    },
                )

    def test_nested_live_result_error_raises(self) -> None:
        error_lines = [
            json.dumps({"event": "init", "conversation_id": "agy-err-conv", "init": {}}) + "\n",
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "agy-err-conv",
                        "status": "ERROR",
                        "error": "upstream quota exhausted",
                    },
                }
            )
            + "\n",
        ]
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(error_lines, returncode=0)),
        ):
            engine = AgyCliEngine(binary="agy")
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(TurnRequest(episode_id="ep-err", system_prefix="", user_text="boom")))
            self.assertIn("upstream quota exhausted", str(ctx.exception))

    def test_transient_cli_error_retries_with_exponential_backoff(self) -> None:
        processes = [
            _FakePopenProcess([], stderr_text="unexpected EOF during handshake", returncode=1),
            _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "status": "ERROR",
                                "error": "connection reset by peer",
                            },
                        }
                    )
                    + "\n",
                ],
                returncode=0,
            ),
            _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {"text_delta": "recovered reply"},
                        }
                    )
                    + "\n",
                    json.dumps({"event": "result", "result": {"status": "SUCCESS"}}) + "\n",
                ],
                returncode=0,
            ),
        ]
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", side_effect=processes) as popen_mock,
            mock.patch("shinobu_core.engines.agy_engines.time.sleep") as sleep_mock,
        ):
            engine = AgyCliEngine(binary="agy", max_retries=2, retry_backoff_sec=0.5)
            chunks = list(
                engine.stream_turn(
                    TurnRequest(episode_id="ep-retry", system_prefix="sys", user_text="hi")
                )
            )
            self.assertEqual("".join(c.delta for c in chunks), "recovered reply")
            self.assertTrue(chunks[-1].done)
            self.assertEqual(popen_mock.call_count, 3)
            self.assertEqual(sleep_mock.call_args_list, [mock.call(0.5), mock.call(1.0)])

    def test_non_retryable_429_and_auth_errors_do_not_retry(self) -> None:
        for err_text in (
            "HTTP 429 Too Many Requests: timeout waiting for rate limit",
            "401 unauthorized: invalid_api_key",
        ):
            with self.subTest(err_text=err_text):
                with (
                    mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                    mock.patch(
                        "subprocess.Popen",
                        return_value=_FakePopenProcess([], stderr_text=err_text, returncode=1),
                    ) as popen_mock,
                    mock.patch("shinobu_core.engines.agy_engines.time.sleep") as sleep_mock,
                ):
                    engine = AgyCliEngine(binary="agy", max_retries=2, retry_backoff_sec=0.5)
                    with self.assertRaises(RuntimeError) as ctx:
                        list(
                            engine.stream_turn(
                                TurnRequest(episode_id="ep-429", system_prefix="", user_text="hi")
                            )
                        )
                    self.assertIn(err_text, str(ctx.exception))
                    self.assertEqual(popen_mock.call_count, 1)
                    sleep_mock.assert_not_called()

    def test_transient_error_after_emitted_text_does_not_retry(self) -> None:
        partial_proc = _FakePopenProcess(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {"text_delta": "partial "},
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "status": "ERROR",
                            "error": "connection reset by peer",
                        },
                    }
                )
                + "\n",
            ],
            returncode=0,
        )
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", return_value=partial_proc) as popen_mock,
            mock.patch("shinobu_core.engines.agy_engines.time.sleep") as sleep_mock,
        ):
            engine = AgyCliEngine(binary="agy", max_retries=2, retry_backoff_sec=0.5)
            received: list[str] = []
            with self.assertRaises(RuntimeError) as ctx:
                for chunk in engine.stream_turn(
                    TurnRequest(episode_id="ep-mid", system_prefix="", user_text="hi")
                ):
                    if chunk.delta:
                        received.append(chunk.delta)
            self.assertEqual("".join(received), "partial ")
            self.assertIn("connection reset", str(ctx.exception))
            self.assertEqual(popen_mock.call_count, 1)
            sleep_mock.assert_not_called()

    def test_watchdog_timeout_terminates_blocked_process(self) -> None:
        blocked_proc = _FakePopenProcess([], hang_stdout=True)
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", return_value=blocked_proc) as popen_mock,
        ):
            engine = AgyCliEngine(binary="agy", timeout=0.1, max_retries=0)
            with self.assertRaises(RuntimeError) as ctx:
                list(
                    engine.stream_turn(
                        TurnRequest(episode_id="ep-hang", system_prefix="", user_text="stuck")
                    )
                )
            self.assertIn("agy CLI timed out after 0.1s", str(ctx.exception))
            self.assertEqual(popen_mock.call_count, 1)
            self.assertTrue(blocked_proc.terminated)
            self.assertTrue(blocked_proc.stdout.closed)

    def test_stale_conversation_clears_session_map_and_rebuilds_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "sessions.json"
            session_map.write_text(
                json.dumps({"ep-stale": "stale-conv-1"}),
                encoding="utf-8",
            )
            processes = [
                _FakePopenProcess(
                    [],
                    stderr_text="conversation not found: stale-conv-1",
                    returncode=1,
                ),
                _FakePopenProcess(
                    [
                        json.dumps(
                            {
                                "event": "init",
                                "conversation_id": "fresh-conv-2",
                                "init": {},
                            }
                        )
                        + "\n",
                        json.dumps(
                            {
                                "event": "step_update",
                                "step_update": {"text_delta": "rebuilt ok"},
                            }
                        )
                        + "\n",
                        json.dumps({"event": "result", "result": {"status": "SUCCESS"}}) + "\n",
                    ],
                    returncode=0,
                ),
            ]
            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=processes) as popen_mock,
            ):
                engine = AgyCliEngine(
                    binary="agy",
                    session_map_path=str(session_map),
                    max_retries=0,
                )
                req = TurnRequest(
                    episode_id="ep-stale",
                    system_prefix="SYS_RULES",
                    history=[{"role": "user", "content": "prev"}],
                    user_text="hello again",
                )
                chunks = list(engine.stream_turn(req))
                self.assertEqual("".join(c.delta for c in chunks), "rebuilt ok")
                self.assertEqual(popen_mock.call_count, 2)
                first_cmd = popen_mock.call_args_list[0].args[0]
                second_cmd = popen_mock.call_args_list[1].args[0]
                self.assertIn("--conversation", first_cmd)
                self.assertIn("stale-conv-1", first_cmd)
                self.assertNotIn("--conversation", second_cmd)
                rebuilt_prompt = second_cmd[second_cmd.index("-p") + 1]
                self.assertIn("SYS_RULES", rebuilt_prompt)
                self.assertIn("prev", rebuilt_prompt)
                self.assertIn("hello again", rebuilt_prompt)
                persisted = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(persisted, {"ep-stale": "fresh-conv-2"})

    def test_stale_conversation_recovery_runs_at_most_once_and_persists_cleared_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "sessions.json"
            session_map.write_text(
                json.dumps({"ep-bad": "bad-conv-1"}),
                encoding="utf-8",
            )
            processes = [
                _FakePopenProcess([], stderr_text="session expired", returncode=1),
                _FakePopenProcess([], stderr_text="session invalid", returncode=1),
            ]
            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=processes) as popen_mock,
            ):
                engine = AgyCliEngine(
                    binary="agy",
                    session_map_path=str(session_map),
                    max_retries=2,
                    retry_backoff_sec=0.0,
                )
                with self.assertRaises(RuntimeError) as ctx:
                    list(
                        engine.stream_turn(
                            TurnRequest(episode_id="ep-bad", system_prefix="sys", user_text="hi")
                        )
                    )
                self.assertIn("session invalid", str(ctx.exception))
                self.assertEqual(popen_mock.call_count, 2)
                persisted = json.loads(session_map.read_text(encoding="utf-8"))
                self.assertEqual(persisted, {})

    @staticmethod
    def _make_persistent_proc(
        turn_outputs: list[list[str] | str],
        *,
        fail_write_on_turn: dict[int, Exception] | None = None,
        stderr_text: str = "",
        returncode: int = 0,
    ):
        import io
        import queue
        import threading

        class _FakePersistentStdin:
            def __init__(self, owner) -> None:
                self._owner = owner
                self.closed = False

            def write(self, data: str) -> int:
                if self.closed:
                    raise ValueError("I/O operation on closed file.")
                turn_idx = self._owner._turn_index
                self._owner._turn_index += 1
                err = self._owner._fail_write_on_turn.get(turn_idx)
                if err is not None:
                    raise err
                self._owner.written_lines.append(data)
                if turn_idx < len(self._owner._turn_outputs):
                    plan = self._owner._turn_outputs[turn_idx]
                    if plan == "EOF":
                        self._owner._returncode = self._owner._configured_returncode
                        self._owner._stdout_queue.put("")
                    elif plan == "HANG":
                        pass
                    else:
                        for item in plan:
                            if item == "EOF":
                                self._owner._returncode = self._owner._configured_returncode
                                self._owner._stdout_queue.put("")
                            else:
                                self._owner._stdout_queue.put(item)
                else:
                    self._owner._stdout_queue.put("")
                return len(data)

            def flush(self) -> None:
                if self.closed:
                    raise ValueError("I/O operation on closed file.")

            def close(self) -> None:
                self.closed = True

        class _FakePersistentStdout:
            def __init__(self, owner) -> None:
                self._owner = owner
                self.closed = False

            def readline(self) -> str:
                if self.closed:
                    raise ValueError("I/O operation on closed file.")
                item = self._owner._stdout_queue.get()
                if item is None:
                    return ""
                return item

            def close(self) -> None:
                if not self.closed:
                    self.closed = True
                    self._owner._stdout_queue.put(None)

        class _FakePersistentProc:
            def __init__(self) -> None:
                self._turn_outputs = list(turn_outputs)
                self._fail_write_on_turn = dict(fail_write_on_turn or {})
                self._turn_index = 0
                self._stdout_queue: queue.Queue[str | None] = queue.Queue()
                self._configured_returncode = returncode
                self._returncode: int | None = None
                self.written_lines: list[str] = []
                self.terminated = False
                self.killed = False
                self.stdin = _FakePersistentStdin(self)
                self.stdout = _FakePersistentStdout(self)
                self.stderr = io.StringIO(stderr_text)
                self._lock = threading.Lock()

            def poll(self) -> int | None:
                with self._lock:
                    return self._returncode

            def wait(self, timeout: float | None = None) -> int:
                with self._lock:
                    if self._returncode is None:
                        self._returncode = self._configured_returncode
                    return self._returncode

            def terminate(self) -> None:
                with self._lock:
                    self.terminated = True
                    if self._returncode is None:
                        self._returncode = 0
                self.stdout.close()

            def kill(self) -> None:
                with self._lock:
                    self.killed = True
                    if self._returncode is None:
                        self._returncode = -9
                self.stdout.close()

        return _FakePersistentProc()

    def test_effort_and_agent_mode_cli_flags_in_oneshot_mode(self) -> None:
        lines = [
            json.dumps({"event": "step_update", "step_update": {"text_delta": "fast"}}) + "\n",
            json.dumps({"event": "result", "result": {"status": "SUCCESS"}}) + "\n",
        ]
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(lines, returncode=0)) as popen_mock,
        ):
            engine = AgyCliEngine(
                binary="agy",
                model="gemini-3-flash",
                effort="low",
                agent_mode="fast",
                extra_args=("--no-color",),
            )
            chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-flags", system_prefix="sys", user_text="hi")))
            self.assertEqual("".join(c.delta for c in chunks), "fast")
            cmd = popen_mock.call_args.args[0]
            self.assertEqual(cmd[1], "-p")
            self.assertIn("--model", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3-flash")
            self.assertIn("--effort", cmd)
            self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
            self.assertIn("--mode", cmd)
            self.assertEqual(cmd[cmd.index("--mode") + 1], "fast")
            self.assertIn("--no-color", cmd)

    def test_add_dirs_skip_permissions_settings_flags_and_windows_no_window(self) -> None:
        import shinobu_core.engines.agy_engines as agy_mod

        # 1. One-shot command flags + Mapping settings compact JSON + Windows CREATE_NO_WINDOW
        lines = [
            json.dumps({"event": "step_update", "step_update": {"text_delta": "ok"}}) + "\n",
            json.dumps({"event": "result", "result": {"status": "SUCCESS"}}) + "\n",
        ]
        extra_b_path = Path("/opt/extra_b")
        expected_extra_b = str(extra_b_path)
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch.object(agy_mod.os, "name", "nt"),
            mock.patch.object(agy_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(lines, returncode=0)) as popen_mock,
        ):
            engine = AgyCliEngine(
                binary="agy",
                add_dirs=("/opt/extra_a", extra_b_path, "/opt/extra_a", "   "),
                skip_permissions=True,
                settings={"toolPermission": "allow", "allowNonWorkspaceAccess": True},
                extra_args=("--tail",),
            )
            chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-newflags", system_prefix="sys", user_text="hi")))
            self.assertEqual("".join(c.delta for c in chunks), "ok")
            cmd = popen_mock.call_args.args[0]
            add_dir_values = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
            self.assertEqual(add_dir_values, ["/opt/extra_a", expected_extra_b])
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("--settings", cmd)
            self.assertEqual(
                cmd[cmd.index("--settings") + 1],
                '{"toolPermission":"allow","allowNonWorkspaceAccess":true}',
            )
            self.assertEqual(cmd[-1], "--tail")
            self.assertEqual(popen_mock.call_args.kwargs.get("creationflags"), 0x08000000)

        # 2. Persistent mode flags + string settings + Windows CREATE_NO_WINDOW
        persistent_proc = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "step_update", "step_update": {"text_delta": "p-ok"}}) + "\n",
                    json.dumps({"event": "result", "result": {"status": "SUCCESS"}}) + "\n",
                ]
            ]
        )
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch.object(agy_mod.os, "name", "nt"),
            mock.patch.object(agy_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch("subprocess.Popen", return_value=persistent_proc) as popen_persist_mock,
        ):
            p_engine = AgyCliEngine(
                binary="agy",
                persistent=True,
                add_dirs=("/opt/p_dir",),
                skip_permissions=True,
                settings="  /opt/custom_settings.json  ",
                timeout=5.0,
            )
            try:
                p_chunks = list(
                    p_engine.stream_turn(TurnRequest(episode_id="ep-pflags", system_prefix="sys", user_text="hi"))
                )
                self.assertEqual("".join(c.delta for c in p_chunks), "p-ok")
                p_cmd = popen_persist_mock.call_args.args[0]
                self.assertIn("--add-dir", p_cmd)
                self.assertEqual(p_cmd[p_cmd.index("--add-dir") + 1], "/opt/p_dir")
                self.assertIn("--dangerously-skip-permissions", p_cmd)
                self.assertIn("--settings", p_cmd)
                self.assertEqual(p_cmd[p_cmd.index("--settings") + 1], "/opt/custom_settings.json")
                self.assertEqual(popen_persist_mock.call_args.kwargs.get("creationflags"), 0x08000000)
            finally:
                p_engine.close()

        # 3. Non-Windows platform or zero CREATE_NO_WINDOW omits creationflags
        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch.object(agy_mod, "_augmented_agy_path", return_value=""),
            mock.patch.object(agy_mod.os, "name", "posix"),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(lines, returncode=0)) as popen_posix_mock,
        ):
            posix_engine = AgyCliEngine(binary="agy")
            list(posix_engine.stream_turn(TurnRequest(episode_id="ep-posix", system_prefix="sys", user_text="hi")))
            self.assertNotIn("creationflags", popen_posix_mock.call_args.kwargs)

        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch.object(agy_mod.os, "name", "nt"),
            mock.patch.object(agy_mod.subprocess, "CREATE_NO_WINDOW", 0, create=True),
            mock.patch("subprocess.Popen", return_value=_FakePopenProcess(lines, returncode=0)) as popen_zero_mock,
        ):
            zero_flag_engine = AgyCliEngine(binary="agy")
            list(zero_flag_engine.stream_turn(TurnRequest(episode_id="ep-zero", system_prefix="sys", user_text="hi")))
            self.assertNotIn("creationflags", popen_zero_mock.call_args.kwargs)

    def test_persistent_multiturn_reuses_single_process_and_keeps_complete_json_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "persistent_sessions.json"
            persistent_proc = self._make_persistent_proc(
                [
                    [
                        json.dumps({"event": "init", "conversation_id": "agy-hot-1", "init": {}}) + "\n",
                        json.dumps({"event": "user", "message": {"content": "should be ignored"}}) + "\n",
                        json.dumps(
                            {
                                "event": "step_update",
                                "step_update": {"text_delta": "Turn1 ", "usage": {"output_tokens": 1}},
                            }
                        )
                        + "\n",
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-hot-1",
                                    "status": "SUCCESS",
                                    "response": "Turn1 ",
                                    "usage": {"input_tokens": 12, "output_tokens": 3},
                                },
                            }
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {
                                "event": "step_update",
                                "step_update": {"text_delta": "Turn2 ok"},
                            }
                        )
                        + "\n",
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-hot-1",
                                    "status": "SUCCESS",
                                    "usage": {"input_tokens": 5, "output_tokens": 2},
                                },
                            }
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-hot-1",
                                    "status": "SUCCESS",
                                    "response": "Turn3 fallback",
                                },
                            }
                        )
                        + "\n",
                    ],
                ]
            )
            oneshot_json_proc = _FakePopenProcess(
                [json.dumps({"type": "result", "result": '{"summary": "ok"}'}) + "\n"],
                returncode=0,
            )

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=[persistent_proc, oneshot_json_proc]) as popen_mock,
            ):
                engine = AgyCliEngine(
                    binary="agy",
                    model="gemini-3-flash",
                    effort="low",
                    agent_mode="chat",
                    persistent=True,
                    session_map_path=str(session_map),
                    extra_args=("--extra",),
                    timeout=5.0,
                )
                try:
                    chunks1 = list(
                        engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-hot",
                                system_prefix="Frozen Persona",
                                history=[{"role": "user", "content": "prior msg"}],
                                user_text="hello 1",
                            )
                        )
                    )
                    self.assertEqual("".join(c.delta for c in chunks1), "Turn1 ")
                    self.assertTrue(chunks1[-1].done)
                    self.assertEqual(chunks1[-1].usage, {"input_tokens": 12, "output_tokens": 3})
                    self.assertFalse(persistent_proc.terminated)
                    self.assertEqual(popen_mock.call_count, 1)

                    cmd_persist = popen_mock.call_args_list[0].args[0]
                    self.assertNotIn("-p", cmd_persist)
                    self.assertIn("--input-format", cmd_persist)
                    self.assertEqual(cmd_persist[cmd_persist.index("--input-format") + 1], "stream-json")
                    self.assertIn("--output-format", cmd_persist)
                    self.assertEqual(cmd_persist[cmd_persist.index("--output-format") + 1], "stream-json")
                    self.assertIn("--disable-slash-commands", cmd_persist)
                    self.assertEqual(cmd_persist[cmd_persist.index("--model") + 1], "gemini-3-flash")
                    self.assertEqual(cmd_persist[cmd_persist.index("--effort") + 1], "low")
                    self.assertEqual(cmd_persist[cmd_persist.index("--mode") + 1], "chat")
                    self.assertIn("--extra", cmd_persist)
                    self.assertNotIn("--conversation", cmd_persist)

                    # Turn 2 on same episode_id reuses the exact same process and sends only user_text.
                    chunks2 = list(
                        engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-hot",
                                system_prefix="Frozen Persona",
                                history=[{"role": "user", "content": "prior msg"}],
                                user_text="follow-up 2",
                            )
                        )
                    )
                    self.assertEqual("".join(c.delta for c in chunks2), "Turn2 ok")
                    self.assertEqual(popen_mock.call_count, 1)

                    # complete_json runs as an independent one-shot `-p` call without disturbing persistent proc.
                    parsed = engine.complete_json("JSON_SYS", "give summary")
                    self.assertEqual(parsed, {"summary": "ok"})
                    self.assertEqual(popen_mock.call_count, 2)
                    cmd_json = popen_mock.call_args_list[1].args[0]
                    self.assertEqual(cmd_json[1], "-p")
                    self.assertFalse(persistent_proc.terminated)

                    # Turn 3 STILL reuses the original persistent process.
                    chunks3 = list(
                        engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-hot",
                                system_prefix="Frozen Persona",
                                user_text="follow-up 3",
                            )
                        )
                    )
                    self.assertEqual("".join(c.delta for c in chunks3), "Turn3 fallback")
                    self.assertEqual(popen_mock.call_count, 2)

                    written_payloads = [json.loads(line) for line in persistent_proc.written_lines]
                    self.assertEqual(len(written_payloads), 3)
                    self.assertEqual(written_payloads[0]["event"], "user")
                    self.assertIn("Frozen Persona", written_payloads[0]["message"]["content"])
                    self.assertIn("prior msg", written_payloads[0]["message"]["content"])
                    self.assertIn("hello 1", written_payloads[0]["message"]["content"])
                    self.assertEqual(written_payloads[1], {"event": "user", "message": {"content": "follow-up 2"}})
                    self.assertEqual(written_payloads[2], {"event": "user", "message": {"content": "follow-up 3"}})

                    persisted = json.loads(session_map.read_text(encoding="utf-8"))
                    self.assertEqual(persisted, {"ep-hot": "agy-hot-1"})
                finally:
                    engine.close()

                self.assertTrue(persistent_proc.terminated)
                self.assertTrue(persistent_proc.stdin.closed)
                self.assertTrue(persistent_proc.stdout.closed)

    def test_persistent_episode_switch_restarts_process_without_crosstalk(self) -> None:
        proc_a = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "init", "conversation_id": "conv-A", "init": {}}) + "\n",
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "reply-A1"}}) + "\n",
                ]
            ]
        )
        proc_b = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "init", "conversation_id": "conv-B", "init": {}}) + "\n",
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "reply-B1"}}) + "\n",
                ]
            ]
        )
        proc_a2 = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "reply-A2"}}) + "\n",
                ]
            ]
        )

        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", side_effect=[proc_a, proc_b, proc_a2]) as popen_mock,
        ):
            engine = AgyCliEngine(binary="agy", persistent=True, timeout=5.0)
            try:
                r1 = "".join(
                    c.delta
                    for c in engine.stream_turn(TurnRequest(episode_id="ep-A", system_prefix="SYS_A", user_text="hi A"))
                )
                self.assertEqual(r1, "reply-A1")
                self.assertFalse(proc_a.terminated)

                # Switch to ep-B -> must terminate proc_a and spawn clean proc_b without conv-A.
                r2 = "".join(
                    c.delta
                    for c in engine.stream_turn(TurnRequest(episode_id="ep-B", system_prefix="SYS_B", user_text="hi B"))
                )
                self.assertEqual(r2, "reply-B1")
                self.assertTrue(proc_a.terminated)
                self.assertFalse(proc_b.terminated)
                cmd_b = popen_mock.call_args_list[1].args[0]
                self.assertNotIn("--conversation", cmd_b)
                self.assertIn("SYS_B", json.loads(proc_b.written_lines[0])["message"]["content"])

                # Switch back to ep-A -> terminates proc_b and resumes conv-A.
                r3 = "".join(
                    c.delta
                    for c in engine.stream_turn(TurnRequest(episode_id="ep-A", system_prefix="SYS_A", user_text="back A"))
                )
                self.assertEqual(r3, "reply-A2")
                self.assertTrue(proc_b.terminated)
                cmd_a2 = popen_mock.call_args_list[2].args[0]
                self.assertIn("--conversation", cmd_a2)
                self.assertEqual(cmd_a2[cmd_a2.index("--conversation") + 1], "conv-A")
                self.assertEqual(json.loads(proc_a2.written_lines[0])["message"]["content"], "back A")
            finally:
                engine.close()
            self.assertTrue(proc_a2.terminated)

    def test_persistent_broken_pipe_and_eof_rebuild_safely_before_first_token(self) -> None:
        # Proc1 succeeds on turn 1, then raises BrokenPipeError on turn 2 write.
        proc1 = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "init", "conversation_id": "conv-rebuild", "init": {}}) + "\n",
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "t1"}}) + "\n",
                ]
            ],
            fail_write_on_turn={1: BrokenPipeError("pipe closed")},
        )
        # Proc2 succeeds on turn 2, then hits unexpected EOF before emitting anything on turn 3.
        proc2 = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "t2-rebuilt"}}) + "\n",
                ],
                "EOF",
            ]
        )
        # Proc3 succeeds on turn 3.
        proc3 = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "t3-rebuilt"}}) + "\n",
                ]
            ]
        )

        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", side_effect=[proc1, proc2, proc3]) as popen_mock,
        ):
            engine = AgyCliEngine(binary="agy", persistent=True, max_retries=0, timeout=5.0)
            try:
                r1 = "".join(
                    c.delta for c in engine.stream_turn(TurnRequest(episode_id="ep-bp", system_prefix="S", user_text="u1"))
                )
                self.assertEqual(r1, "t1")

                r2 = "".join(
                    c.delta for c in engine.stream_turn(TurnRequest(episode_id="ep-bp", system_prefix="S", user_text="u2"))
                )
                self.assertEqual(r2, "t2-rebuilt")
                self.assertTrue(proc1.terminated)
                cmd2 = popen_mock.call_args_list[1].args[0]
                self.assertIn("--conversation", cmd2)
                self.assertEqual(cmd2[cmd2.index("--conversation") + 1], "conv-rebuild")

                r3 = "".join(
                    c.delta for c in engine.stream_turn(TurnRequest(episode_id="ep-bp", system_prefix="S", user_text="u3"))
                )
                self.assertEqual(r3, "t3-rebuilt")
                self.assertTrue(proc2.stdin.closed)
                self.assertTrue(proc2.stdout.closed)
                self.assertEqual(popen_mock.call_count, 3)
            finally:
                engine.close()
            self.assertTrue(proc3.terminated)

    def test_persistent_timeout_and_midstream_eof_cleanup_and_close_idempotent(self) -> None:
        import threading

        hung_proc = self._make_persistent_proc(["HANG"])
        partial_eof_proc = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "step_update", "step_update": {"text_delta": "half "}}) + "\n",
                    "EOF",
                ]
            ]
        )
        fresh_proc = self._make_persistent_proc(
            [
                [
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "clean-after-reset"}}) + "\n",
                ]
            ]
        )

        with (
            mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
            mock.patch("subprocess.Popen", side_effect=[hung_proc, partial_eof_proc, fresh_proc]) as popen_mock,
        ):
            engine = AgyCliEngine(binary="agy", persistent=True, timeout=0.1, max_retries=0)
            # 1) Watchdog timeout cleans up hung_proc.
            with self.assertRaises(RuntimeError) as ctx:
                list(engine.stream_turn(TurnRequest(episode_id="ep-to", system_prefix="", user_text="hang")))
            self.assertIn("agy CLI timed out after 0.1s", str(ctx.exception))
            self.assertTrue(hung_proc.terminated)
            self.assertTrue(hung_proc.stdout.closed)

            # 2) Partial text then EOF raises without retrying, and resets broken process.
            received: list[str] = []
            with self.assertRaises(RuntimeError):
                for chunk in engine.stream_turn(TurnRequest(episode_id="ep-to", system_prefix="", user_text="partial")):
                    if chunk.delta:
                        received.append(chunk.delta)
            self.assertEqual("".join(received), "half ")
            self.assertTrue(partial_eof_proc.stdin.closed)
            self.assertTrue(partial_eof_proc.stdout.closed)
            self.assertIsNone(engine._persistent_proc)

            # 3) Next turn spawns fresh_proc cleanly.
            res = "".join(
                c.delta for c in engine.stream_turn(TurnRequest(episode_id="ep-to", system_prefix="", user_text="ok"))
            )
            self.assertEqual(res, "clean-after-reset")
            self.assertEqual(popen_mock.call_count, 3)

            # 4) Concurrent / repeated close() is thread-safe and idempotent.
            threads = [threading.Thread(target=engine.close) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)
            engine.close()
            self.assertTrue(fresh_proc.terminated)
            self.assertTrue(fresh_proc.stdin.closed)
            self.assertTrue(fresh_proc.stdout.closed)

    def test_delta_context_sync_persistent_zero_overhead_and_cloud_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "agy_sessions.json"
            sync_state_file = Path(tmpdir) / "agy_cli_sync_state.json"
            persistent_proc = self._make_persistent_proc(
                [
                    [
                        json.dumps({"event": "init", "conversation_id": "conv-dual", "init": {}}) + "\n",
                        json.dumps(
                            {"event": "result", "result": {"status": "SUCCESS", "response": "local-reply-1"}}
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {"event": "result", "result": {"status": "SUCCESS", "response": "local-reply-2"}}
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {"event": "result", "result": {"status": "SUCCESS", "response": "local-reply-3"}}
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {"event": "result", "result": {"status": "SUCCESS", "response": "local-reply-4"}}
                        )
                        + "\n",
                    ],
                ]
            )

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=[persistent_proc]),
            ):
                engine = AgyCliEngine(
                    binary="agy",
                    persistent=True,
                    session_map_path=str(session_map),
                    timeout=5.0,
                )
                try:
                    # Turn 1: fresh episode -> full L0 prefix.
                    r1 = "".join(
                        c.delta
                        for c in engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-dual",
                                system_prefix="Frozen L0",
                                history=[],
                                user_text="ok",
                            )
                        )
                    )
                    self.assertEqual(r1, "local-reply-1")

                    # Turn 2: consecutive local AGY turn -> zero delta, raw user_text only (KV cache safe).
                    hist_after_t1 = [
                        {"role": "user", "content": "ok"},
                        {"role": "assistant", "content": "local-reply-1"},
                    ]
                    r2 = "".join(
                        c.delta
                        for c in engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-dual",
                                system_prefix="Frozen L0",
                                history=hist_after_t1,
                                user_text="second local q",
                            )
                        )
                    )
                    self.assertEqual(r2, "local-reply-2")

                    # Simulate 2 cloud brain turns (including repeated "ok" & ignored system/tool/empty entries).
                    hist_after_cloud = hist_after_t1 + [
                        {"role": "user", "content": "second local q"},
                        {"role": "assistant", "content": "local-reply-2"},
                        {"role": "system", "content": "ignored system note"},
                        {"role": "tool", "content": "ignored tool output"},
                        {"role": "user", "content": "   "},
                        {"role": "user", "content": "ok"},
                        {"role": "assistant", "content": "cloud-reply-1"},
                        {"role": "user", "content": "cloud q2"},
                        {"role": "assistant", "content": "cloud-reply-2"},
                    ]

                    # Turn 3: back to local AGY -> only cloud delta is prepended.
                    r3 = "".join(
                        c.delta
                        for c in engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-dual",
                                system_prefix="Frozen L0",
                                history=hist_after_cloud,
                                user_text="back to local",
                            )
                        )
                    )
                    self.assertEqual(r3, "local-reply-3")

                    # Turn 4: consecutive local turn after resync -> back to zero-overhead raw user_text.
                    hist_after_t3 = hist_after_cloud + [
                        {"role": "user", "content": "back to local"},
                        {"role": "assistant", "content": "local-reply-3"},
                    ]
                    r4 = "".join(
                        c.delta
                        for c in engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-dual",
                                system_prefix="Frozen L0",
                                history=hist_after_t3,
                                user_text="steady local",
                            )
                        )
                    )
                    self.assertEqual(r4, "local-reply-4")
                finally:
                    engine.close()

            payloads = [json.loads(line)["message"]["content"] for line in persistent_proc.written_lines]
            self.assertEqual(len(payloads), 4)
            self.assertIn("[System Instructions]\nFrozen L0", payloads[0])
            self.assertEqual(payloads[1], "second local q")
            self.assertEqual(
                payloads[2],
                "[Synced Context from Cloud Brain]\n"
                "user: ok\n"
                "assistant: cloud-reply-1\n"
                "user: cloud q2\n"
                "assistant: cloud-reply-2\n\n"
                "[Current User Message]\n"
                "back to local",
            )
            self.assertEqual(payloads[3], "steady local")

            self.assertTrue(sync_state_file.is_file())
            sync_data = json.loads(sync_state_file.read_text(encoding="utf-8"))
            self.assertIn("ep-dual", sync_data)
            self.assertEqual(len(sync_data["ep-dual"]), 12)
            for role, digest in sync_data["ep-dual"]:
                self.assertIn(role, ("user", "assistant"))
                self.assertEqual(len(digest), 16)

    def test_delta_context_sync_oneshot_cross_restart_and_episode_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "agy_sessions.json"
            proc1 = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-ep1",
                                "status": "SUCCESS",
                                "response": "ep1-a1",
                            },
                        }
                    )
                    + "\n"
                ]
            )
            proc2 = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-ep1",
                                "status": "SUCCESS",
                                "response": "ep1-a3",
                            },
                        }
                    )
                    + "\n"
                ]
            )
            proc3 = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-ep1",
                                "status": "SUCCESS",
                                "response": "ep1-a4",
                            },
                        }
                    )
                    + "\n"
                ]
            )
            proc4 = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-ep2",
                                "status": "SUCCESS",
                                "response": "ep2-a1",
                            },
                        }
                    )
                    + "\n"
                ]
            )

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=[proc1, proc2, proc3, proc4]) as popen_mock,
            ):
                # Process 1: Turn 1 on ep-1 persists both session map and sync state.
                engine1 = AgyCliEngine(
                    binary="agy",
                    persistent=False,
                    session_map_path=str(session_map),
                    timeout=5.0,
                )
                r1 = "".join(
                    c.delta
                    for c in engine1.stream_turn(
                        TurnRequest(
                            episode_id="ep-1",
                            system_prefix="SYS-EP1",
                            history=[],
                            user_text="ep1-u1",
                        )
                    )
                )
                self.assertEqual(r1, "ep1-a1")

                # Process 2 (after restart): cloud handled turn 2, now resume ep-1 on AGY.
                engine2 = AgyCliEngine(
                    binary="agy",
                    persistent=False,
                    session_map_path=str(session_map),
                    timeout=5.0,
                )
                hist_with_cloud = [
                    {"role": "user", "content": "ep1-u1"},
                    {"role": "assistant", "content": "ep1-a1"},
                    {"role": "user", "content": "cloud-u2"},
                    {"role": "assistant", "content": "cloud-a2"},
                ]
                r2 = "".join(
                    c.delta
                    for c in engine2.stream_turn(
                        TurnRequest(
                            episode_id="ep-1",
                            system_prefix="SYS-EP1",
                            history=hist_with_cloud,
                            user_text="ep1-u3",
                        )
                    )
                )
                self.assertEqual(r2, "ep1-a3")

                # Turn 3 on ep-1: no new cloud delta -> raw user_text only.
                hist_synced = hist_with_cloud + [
                    {"role": "user", "content": "ep1-u3"},
                    {"role": "assistant", "content": "ep1-a3"},
                ]
                r3 = "".join(
                    c.delta
                    for c in engine2.stream_turn(
                        TurnRequest(
                            episode_id="ep-1",
                            system_prefix="SYS-EP1",
                            history=hist_synced,
                            user_text="ep1-u4",
                        )
                    )
                )
                self.assertEqual(r3, "ep1-a4")

                # /sleep switches to a new episode ep-2 -> fresh full L0 prefix, no --conversation.
                r4 = "".join(
                    c.delta
                    for c in engine2.stream_turn(
                        TurnRequest(
                            episode_id="ep-2",
                            system_prefix="SYS-EP2-NEW",
                            history=[{"role": "user", "content": "carryover"}],
                            user_text="ep2-u1",
                        )
                    )
                )
                self.assertEqual(r4, "ep2-a1")

            cmds = [call.args[0] for call in popen_mock.call_args_list]
            # Call 2 resumes conv-ep1 and injects only cloud-u2 / cloud-a2.
            self.assertIn("--conversation", cmds[1])
            self.assertEqual(cmds[1][cmds[1].index("--conversation") + 1], "conv-ep1")
            prompt2 = cmds[1][cmds[1].index("-p") + 1]
            self.assertEqual(
                prompt2,
                "[Synced Context from Cloud Brain]\n"
                "user: cloud-u2\n"
                "assistant: cloud-a2\n\n"
                "[Current User Message]\n"
                "ep1-u3",
            )
            # Call 3 resumes conv-ep1 with 0 delta.
            self.assertEqual(cmds[2][cmds[2].index("-p") + 1], "ep1-u4")
            # Call 4 starts fresh ep-2 with full L0 prefix and without --conversation.
            self.assertNotIn("--conversation", cmds[3])
            prompt4 = cmds[3][cmds[3].index("-p") + 1]
            self.assertIn("[System Instructions]\nSYS-EP2-NEW", prompt4)
            self.assertNotIn("[Synced Context from Cloud Brain]", prompt4)

    def test_delta_context_sync_stale_session_clears_cursor_and_caps_tail_64(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "agy_sessions.json"
            sync_state_file = Path(tmpdir) / "agy_cli_sync_state.json"

            # 1) Seed > 64 entries and verify persisted tail is capped at 64.
            long_history = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
                for i in range(80)
            ]
            seed_proc = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-stale",
                                "status": "SUCCESS",
                                "response": "seeded-ok",
                            },
                        }
                    )
                    + "\n"
                ]
            )
            # 2) Next turn hits stale conversation error, clears session + sync cursor, and rebuilds fresh.
            stale_proc = _FakePopenProcess([], stderr_text="conversation not found: conv-stale", returncode=1)
            rebuilt_proc = _FakePopenProcess(
                [
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "conversation_id": "conv-fresh",
                                "status": "SUCCESS",
                                "response": "rebuilt-ok",
                            },
                        }
                    )
                    + "\n"
                ]
            )

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=[seed_proc, stale_proc, rebuilt_proc]) as popen_mock,
            ):
                engine = AgyCliEngine(
                    binary="agy",
                    persistent=False,
                    session_map_path=str(session_map),
                    timeout=5.0,
                )
                r1 = "".join(
                    c.delta
                    for c in engine.stream_turn(
                        TurnRequest(
                            episode_id="ep-stale",
                            system_prefix="SYS-L0",
                            history=long_history,
                            user_text="seed-q",
                        )
                    )
                )
                self.assertEqual(r1, "seeded-ok")
                persisted_after_seed = json.loads(sync_state_file.read_text(encoding="utf-8"))
                self.assertEqual(len(persisted_after_seed["ep-stale"]), 64)

                # Trigger stale recovery with cloud delta present in history.
                r2 = "".join(
                    c.delta
                    for c in engine.stream_turn(
                        TurnRequest(
                            episode_id="ep-stale",
                            system_prefix="SYS-L0",
                            history=[
                                {"role": "user", "content": "cloud-before-stale"},
                                {"role": "assistant", "content": "cloud-ans-before-stale"},
                            ],
                            user_text="after-stale",
                        )
                    )
                )
                self.assertEqual(r2, "rebuilt-ok")

            cmds = [call.args[0] for call in popen_mock.call_args_list]
            # Third call (rebuilt) must have no --conversation and full [System Instructions] + [History].
            self.assertNotIn("--conversation", cmds[2])
            rebuilt_prompt = cmds[2][cmds[2].index("-p") + 1]
            self.assertIn("[System Instructions]\nSYS-L0", rebuilt_prompt)
            self.assertIn("cloud-before-stale", rebuilt_prompt)
            self.assertNotIn("[Synced Context from Cloud Brain]", rebuilt_prompt)

            # Sync state was cleared during stale recovery and rebuilt with only the 4 fresh entries.
            persisted_after_rebuild = json.loads(sync_state_file.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted_after_rebuild["ep-stale"]), 4)

    def test_cli_single_turn_effort_and_persistent_upgrade_respawns_with_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = Path(tmpdir) / "ratchet_sessions.json"

            # 1. One-shot CLI single-turn override without mutating engine.effort.
            oneshot_proc = _FakePopenProcess(
                [json.dumps({"type": "result", "result": "oneshot-high"}) + "\n"],
                returncode=0,
            )
            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", return_value=oneshot_proc) as popen_oneshot,
            ):
                oneshot_engine = AgyCliEngine(binary="agy", effort="low", persistent=False, timeout=5.0)
                self.assertEqual(oneshot_engine.reasoning_effort, "low")
                out = "".join(
                    c.delta
                    for c in oneshot_engine.stream_turn(
                        TurnRequest(
                            episode_id="ep-oneshot-eff",
                            system_prefix="SYS",
                            user_text="deep-q",
                            reasoning_effort="high",
                        )
                    )
                )
                self.assertEqual(out, "oneshot-high")
                cmd_one = popen_oneshot.call_args.args[0]
                self.assertEqual(cmd_one[cmd_one.index("--effort") + 1], "high")
                self.assertEqual(oneshot_engine.effort, "low")
                self.assertEqual(oneshot_engine.reasoning_effort, "low")

            # 2. Persistent CLI: Turn 1 (low) -> Turn 2 (low, reuse) -> Turn 3 (high, terminate & respawn with --conversation) -> Turn 4 (high, reuse).
            low_proc = self._make_persistent_proc(
                [
                    [
                        json.dumps({"event": "init", "conversation_id": "agy-ratchet-conv-1"}) + "\n",
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-ratchet-conv-1",
                                    "status": "SUCCESS",
                                    "response": "low-turn-1",
                                },
                            }
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-ratchet-conv-1",
                                    "status": "SUCCESS",
                                    "response": "low-turn-2",
                                },
                            }
                        )
                        + "\n",
                    ],
                ]
            )
            high_proc = self._make_persistent_proc(
                [
                    [
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-ratchet-conv-1",
                                    "status": "SUCCESS",
                                    "response": "high-turn-3",
                                },
                            }
                        )
                        + "\n",
                    ],
                    [
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "conversation_id": "agy-ratchet-conv-1",
                                    "status": "SUCCESS",
                                    "response": "high-turn-4",
                                },
                            }
                        )
                        + "\n",
                    ],
                ]
            )

            with (
                mock.patch("shutil.which", return_value="/opt/agy/bin/agy"),
                mock.patch("subprocess.Popen", side_effect=[low_proc, high_proc]) as popen_persist,
            ):
                p_engine = AgyCliEngine(
                    binary="agy",
                    effort="low",
                    persistent=True,
                    session_map_path=str(session_map),
                    timeout=5.0,
                )
                try:
                    t1 = "".join(
                        c.delta
                        for c in p_engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-ratchet",
                                system_prefix="SYS",
                                user_text="hello",
                            )
                        )
                    )
                    self.assertEqual(t1, "low-turn-1")
                    self.assertEqual(popen_persist.call_count, 1)
                    self.assertEqual(p_engine._persistent_effort, "low")

                    # Same episode + same effective effort ("low") reuses low_proc.
                    t2 = "".join(
                        c.delta
                        for c in p_engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-ratchet",
                                system_prefix="SYS",
                                history=[
                                    {"role": "user", "content": "hello"},
                                    {"role": "assistant", "content": "low-turn-1"},
                                ],
                                user_text="small talk",
                                reasoning_effort="low",
                            )
                        )
                    )
                    self.assertEqual(t2, "low-turn-2")
                    self.assertEqual(popen_persist.call_count, 1)
                    self.assertFalse(low_proc.terminated)

                    # Upgrade to "high" terminates low_proc and spawns high_proc resuming --conversation.
                    t3 = "".join(
                        c.delta
                        for c in p_engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-ratchet",
                                system_prefix="SYS",
                                history=[
                                    {"role": "user", "content": "hello"},
                                    {"role": "assistant", "content": "low-turn-1"},
                                    {"role": "user", "content": "small talk"},
                                    {"role": "assistant", "content": "low-turn-2"},
                                ],
                                user_text="design distributed architecture",
                                reasoning_effort="high",
                            )
                        )
                    )
                    self.assertEqual(t3, "high-turn-3")
                    self.assertTrue(low_proc.terminated)
                    self.assertEqual(popen_persist.call_count, 2)
                    self.assertEqual(p_engine._persistent_effort, "high")

                    cmd_high = popen_persist.call_args_list[1].args[0]
                    self.assertEqual(cmd_high[cmd_high.index("--effort") + 1], "high")
                    self.assertEqual(
                        cmd_high[cmd_high.index("--conversation") + 1],
                        "agy-ratchet-conv-1",
                    )

                    # Subsequent "high" turn reuses high_proc without respawning.
                    t4 = "".join(
                        c.delta
                        for c in p_engine.stream_turn(
                            TurnRequest(
                                episode_id="ep-ratchet",
                                system_prefix="SYS",
                                user_text="follow-up detail",
                                reasoning_effort="high",
                            )
                        )
                    )
                    self.assertEqual(t4, "high-turn-4")
                    self.assertEqual(popen_persist.call_count, 2)
                    self.assertFalse(high_proc.terminated)
                finally:
                    p_engine.close()


if __name__ == "__main__":
    unittest.main()
