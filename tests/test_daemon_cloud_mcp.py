# -*- coding: utf-8 -*-
"""Unit tests for DaemonServer cloud control-plane APIs, remote POST /mcp, and hybrid engine factory."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from shinobu_core.cloud.approval import ApprovalGate
from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.cloud.tts_router import TtsResult, TtsRouter, TtsUnavailableError
from shinobu_core.engines import (
    ENGINE_REGISTRY,
    HybridRouterEngine,
    MockEngine,
    create_engine,
)
from shinobu_core.mcp.server import ShinobuBackendProtocol
from shinobu_core.memory.store import MemoryStore
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.runtime.state_machine import PetStateMachine
from shinobu_core.server.daemon import DaemonServer


_NO_PROXY_OPENER = urllib_request.build_opener(urllib_request.ProxyHandler({}))


def _http_call(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any | None = None,
    raw_bytes: bytes | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    headers: dict[str, str] = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data: bytes | None = raw_bytes
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    last_exc: Exception | None = None
    for _ in range(3):
        req = urllib_request.Request(url, data=data, headers=headers, method=method)
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp_headers, resp.read()
        except urllib_error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()}
            return exc.code, resp_headers, exc.read()
        except (ConnectionAbortedError, ConnectionResetError) as exc:
            last_exc = exc
            time.sleep(0.01)
    assert last_exc is not None
    raise last_exc


class _StubTtsHandler(BaseHTTPRequestHandler):
    received_requests: list[dict[str, Any]] = []
    audio_payload: bytes = b"OggS-fake-colab-opus-audio"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        parsed = json.loads(raw.decode("utf-8"))
        type(self).received_requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": parsed,
            }
        )
        payload = type(self).audio_payload
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/ogg")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestDaemonCloudAndRemoteMcp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "shinobu_home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.home / "memory.db")
        self.engine = MockEngine(
            scripted_replies=["云端守护回声收到：星河常亮。"]
        )
        self.orchestrator = Orchestrator(
            home=self.home,
            store=self.store,
            engine=self.engine,
        )
        self.state_machine = PetStateMachine()
        self.worker_registry = WorkerRegistry()
        self.approval_gate = ApprovalGate()
        self.tts_router = TtsRouter(self.worker_registry)
        self.daemon = DaemonServer(
            self.orchestrator,
            self.state_machine,
            port=0,
            token="cloud-test-token-12345",
            home=self.home,
            worker_registry=self.worker_registry,
            approval_gate=self.approval_gate,
            tts_router=self.tts_router,
        )
        self.daemon.start()

    def tearDown(self) -> None:
        self.approval_gate.cancel_all()
        self.daemon.stop()
        if hasattr(self.store, "close"):
            self.store.close()
        self._tmp.cleanup()

    def test_loopback_constraint_and_bearer_auth_on_all_new_routes(self) -> None:
        with self.assertRaises(ValueError):
            DaemonServer(
                self.orchestrator,
                self.state_machine,
                host="0.0.0.0",
                token="x",
                home=self.home,
            )

        routes = [
            ("GET", "/api/workers", None),
            ("POST", "/api/workers/register", {"worker_id": "w1", "role": "home_pc", "base_url": "http://127.0.0.1:9999"}),
            ("POST", "/api/workers/heartbeat", {"worker_id": "w1"}),
            ("DELETE", "/api/workers/w1", None),
            ("GET", "/api/approvals", None),
            ("POST", "/api/approvals/a1/decide", {"approved": True}),
            ("POST", "/api/tts", {"text": "hello"}),
            ("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ]
        for method, path, payload in routes:
            code, headers, _ = _http_call(method, f"{self.daemon.url}{path}", body=payload)
            self.assertEqual(code, 401, f"{method} {path} should require Bearer auth")
            self.assertEqual(headers.get("www-authenticate"), "Bearer")

            bad_code, _, _ = _http_call(
                method,
                f"{self.daemon.url}{path}",
                token="wrong-token",
                body=payload,
            )
            self.assertEqual(bad_code, 401, f"{method} {path} should reject wrong token")

    def test_worker_register_list_heartbeat_delete_and_token_redaction(self) -> None:
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/workers/register",
            token=self.daemon.token,
            body={
                "worker_id": "home-pc-01",
                "role": "home_pc",
                "base_url": "http://100.64.0.10:18900",
                "capabilities": ["brain", "exec"],
                "token": "super-secret-worker-token",
                "ttl_sec": 60,
                "meta": {"gpu": "none"},
            },
        )
        self.assertEqual(code, 200)
        reg_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(reg_data["status"], "registered")
        self.assertEqual(reg_data["worker"]["worker_id"], "home-pc-01")
        self.assertTrue(reg_data["worker"]["has_token"])
        self.assertNotIn("token", reg_data["worker"])

        # List workers
        code, _, raw = _http_call(
            "GET",
            f"{self.daemon.url}/api/workers",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        listed = json.loads(raw.decode("utf-8"))
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["workers"][0]["worker_id"], "home-pc-01")
        self.assertNotIn("token", listed["workers"][0])

        # Heartbeat updates metadata
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/workers/heartbeat",
            token=self.daemon.token,
            body={"worker_id": "home-pc-01", "meta": {"load": 0.42}},
        )
        self.assertEqual(code, 200)
        hb_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(hb_data["status"], "ok")
        self.assertEqual(hb_data["worker"]["meta"]["load"], 0.42)

        # Missing worker heartbeat returns 404
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/workers/heartbeat",
            token=self.daemon.token,
            body={"worker_id": "nonexistent"},
        )
        self.assertEqual(code, 404)

        # Invalid register payload returns 400
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/workers/register",
            token=self.daemon.token,
            body={"worker_id": "bad", "role": "home_pc", "base_url": "http://127.0.0.1:9", "ttl_sec": -5},
        )
        self.assertEqual(code, 400)

        # Delete worker
        code, _, raw = _http_call(
            "DELETE",
            f"{self.daemon.url}/api/workers/home-pc-01",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw.decode("utf-8"))["status"], "unregistered")

        # Second delete returns 404
        code, _, _ = _http_call(
            "DELETE",
            f"{self.daemon.url}/api/workers/home-pc-01",
            token=self.daemon.token,
        )
        self.assertEqual(code, 404)

    def test_approvals_list_and_decide_approve_and_reject(self) -> None:
        result_holder: dict[str, Any] = {}

        def _wait_approval() -> None:
            result_holder["decision"] = self.approval_gate.request_approval(
                action="cloud_exec",
                target="pip install foo",
                detail={"argv": ["pip", "install", "foo"]},
                timeout_sec=5.0,
            )

        t = threading.Thread(target=_wait_approval, daemon=True)
        t.start()

        # Poll briefly until pending appears
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            code, _, raw = _http_call(
                "GET",
                f"{self.daemon.url}/api/approvals",
                token=self.daemon.token,
            )
            self.assertEqual(code, 200)
            pending = json.loads(raw.decode("utf-8"))["pending"]
            if pending:
                break
            time.sleep(0.02)

        self.assertEqual(len(pending), 1)
        approval_id = pending[0]["approval_id"]

        # Reject non-boolean approved field
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/approvals/{approval_id}/decide",
            token=self.daemon.token,
            body={"approved": "yes", "actor": "owner"},
        )
        self.assertEqual(code, 400)

        # Approve via HTTP API
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/approvals/{approval_id}/decide",
            token=self.daemon.token,
            body={"approved": True, "actor": "telegram_owner"},
        )
        self.assertEqual(code, 200)
        decided = json.loads(raw.decode("utf-8"))
        self.assertTrue(decided["approved"])
        self.assertEqual(decided["actor"], "telegram_owner")

        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        decision = result_holder["decision"]
        self.assertTrue(decision.approved)
        self.assertEqual(decision.actor, "telegram_owner")

        # Duplicate decision returns 404
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/approvals/{approval_id}/decide",
            token=self.daemon.token,
            body={"approved": False, "actor": "late"},
        )
        self.assertEqual(code, 404)

    def test_api_tts_unavailable_and_live_worker_routing(self) -> None:
        # Validation error on blank text
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/tts",
            token=self.daemon.token,
            body={"text": "   "},
        )
        self.assertEqual(code, 400)

        # No TTS worker and no GCP/fallback configured -> 503
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/tts",
            token=self.daemon.token,
            body={"text": "测试语音", "mood": "calm", "fmt": "ogg_opus"},
        )
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"], "tts_unavailable")

        # Start stub Colab TTS server and register it via HTTP /api/workers/register
        _StubTtsHandler.received_requests = []
        tts_srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubTtsHandler)
        tts_port = tts_srv.server_address[1]
        tts_thread = threading.Thread(target=tts_srv.serve_forever, daemon=True)
        tts_thread.start()
        try:
            reg_code, _, _ = _http_call(
                "POST",
                f"{self.daemon.url}/api/workers/register",
                token=self.daemon.token,
                body={
                    "worker_id": "colab-l4-tts",
                    "role": "colab_gpu",
                    "base_url": f"http://127.0.0.1:{tts_port}",
                    "capabilities": ["tts"],
                    "token": "colab-secret",
                    "ttl_sec": 60,
                },
            )
            self.assertEqual(reg_code, 200)

            code, headers, body = _http_call(
                "POST",
                f"{self.daemon.url}/api/tts",
                token=self.daemon.token,
                body={"text": "欢迎回来", "mood": "happy", "fmt": "ogg_opus"},
            )
            self.assertEqual(code, 200)
            self.assertEqual(body, _StubTtsHandler.audio_payload)
            self.assertIn("audio/ogg", headers.get("content-type", ""))
            self.assertTrue(headers.get("x-shinobu-tts-provider"))
            self.assertEqual(len(_StubTtsHandler.received_requests), 1)
            self.assertEqual(
                _StubTtsHandler.received_requests[0]["auth"],
                "Bearer colab-secret",
            )
        finally:
            tts_srv.shutdown()
            tts_srv.server_close()

    def test_remote_mcp_post_lifecycle_and_tool_calls(self) -> None:
        self.assertIsInstance(self.daemon._mcp_backend, ShinobuBackendProtocol)

        # Invalid JSON-RPC version -> 400
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={"jsonrpc": "1.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(code, 400)
        err_resp = json.loads(raw.decode("utf-8"))
        self.assertEqual(err_resp["error"]["code"], -32600)

        # initialize -> 200
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(code, 200)
        init_resp = json.loads(raw.decode("utf-8"))
        self.assertEqual(init_resp["id"], 1)
        self.assertIn("tools", init_resp["result"]["capabilities"])

        # notifications/initialized -> 204 No Content
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertEqual(code, 204)
        self.assertEqual(raw, b"")

        # tools/list -> 200 with 4 Shinobu tools
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(code, 200)
        tools = json.loads(raw.decode("utf-8"))["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        self.assertEqual(
            tool_names,
            {
                "shinobu_bubble",
                "shinobu_state",
                "shinobu_recall",
                "shinobu_chat",
                "shinobu_remember",
            },
        )

        # tools/call shinobu_state (set then get)
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_state",
                    "arguments": {"state": "idle", "mood": "happy", "reason": "mcp_test"},
                },
            },
        )
        self.assertEqual(code, 200)
        call_resp = json.loads(raw.decode("utf-8"))["result"]
        self.assertFalse(call_resp["isError"], f"unexpected error: {call_resp}")

        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "shinobu_state", "arguments": {}},
            },
        )
        self.assertEqual(code, 200)
        state_payload = json.loads(
            json.loads(raw.decode("utf-8"))["result"]["content"][0]["text"]
        )
        self.assertEqual(state_payload["mood"], "happy")

        # tools/call shinobu_bubble
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_bubble",
                    "arguments": {"text": "远程 MCP 气泡", "mood": "happy"},
                },
            },
        )
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(raw.decode("utf-8"))["result"]["isError"])

        # tools/call shinobu_chat
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_chat",
                    "arguments": {"text": "你好，今天星河状态如何？"},
                },
            },
        )
        self.assertEqual(code, 200)
        chat_result = json.loads(raw.decode("utf-8"))["result"]
        self.assertFalse(chat_result["isError"])
        chat_payload = json.loads(chat_result["content"][0]["text"])
        self.assertIn("星河常亮", chat_payload["text"])

        # tools/call shinobu_recall
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_recall",
                    "arguments": {"query": "星河", "limit": 5},
                },
            },
        )
        self.assertEqual(code, 200)
        recall_result = json.loads(raw.decode("utf-8"))["result"]
        self.assertFalse(recall_result["isError"])

        # tools/call shinobu_remember -> persists to facts table with active episode
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_remember",
                    "arguments": {
                        "key": "user.favorite_snack",
                        "value": "金色甜甜圈",
                        "confidence": 0.95,
                    },
                },
            },
        )
        self.assertEqual(code, 200)
        rem_result = json.loads(raw.decode("utf-8"))["result"]
        self.assertFalse(rem_result["isError"], f"unexpected error: {rem_result}")
        rem_payload = json.loads(rem_result["content"][0]["text"])
        self.assertEqual(rem_payload["status"], "saved")
        self.assertEqual(rem_payload["fact"]["key"], "user.favorite_snack")
        self.assertEqual(rem_payload["fact"]["value"], "金色甜甜圈")
        self.assertAlmostEqual(rem_payload["fact"]["confidence"], 0.95)
        self.assertEqual(
            rem_payload["fact"]["source_episode"],
            self.daemon.orchestrator.active_episode_id or "",
        )

        # Verify persisted via GET /api/memory/facts
        code, _, raw = _http_call(
            "GET",
            f"{self.daemon.url}/api/memory/facts?limit=20",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        facts = json.loads(raw.decode("utf-8"))["facts"]
        by_key = {f["key"]: f for f in facts}
        self.assertIn("user.favorite_snack", by_key)
        self.assertEqual(by_key["user.favorite_snack"]["value"], "金色甜甜圈")

    def test_api_memory_facts_post_validation_and_upsert(self) -> None:
        # Missing key -> 400
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/memory/facts",
            token=self.daemon.token,
            body={"value": "only_value"},
        )
        self.assertEqual(code, 400)

        # Missing value -> 400
        code, _, _ = _http_call(
            "POST",
            f"{self.daemon.url}/api/memory/facts",
            token=self.daemon.token,
            body={"key": "only_key"},
        )
        self.assertEqual(code, 400)

        # Valid POST -> 200 {"status": "saved", "fact": ...}
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/memory/facts",
            token=self.daemon.token,
            body={
                "key": "user.timezone",
                "value": "Asia/Shanghai",
                "confidence": 0.9,
                "source_episode": "ep-manual",
            },
        )
        self.assertEqual(code, 200)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["status"], "saved")
        self.assertEqual(payload["fact"]["key"], "user.timezone")
        self.assertEqual(payload["fact"]["value"], "Asia/Shanghai")
        self.assertAlmostEqual(payload["fact"]["confidence"], 0.9)
        self.assertEqual(payload["fact"]["source_episode"], "ep-manual")

        # Upsert same key with default confidence=0.8
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/memory/facts",
            token=self.daemon.token,
            body={"key": "user.timezone", "value": "UTC+8"},
        )
        self.assertEqual(code, 200)
        payload2 = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload2["fact"]["value"], "UTC+8")
        self.assertAlmostEqual(payload2["fact"]["confidence"], 0.8)

    def test_hybrid_engine_registered_in_factory(self) -> None:
        self.assertIs(ENGINE_REGISTRY.get("hybrid"), HybridRouterEngine)
        fallback = MockEngine(scripted_replies=["fallback reply"])
        registry = WorkerRegistry()
        engine = create_engine("hybrid", cloud_fallback=fallback, registry=registry)
        self.assertIsInstance(engine, HybridRouterEngine)
        self.assertEqual(engine.name, "hybrid")

    def test_engine_status_and_route_switch_non_hybrid(self) -> None:
        code, _, raw = _http_call(
            "GET",
            f"{self.daemon.url}/api/engine",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["route_mode"], "")
        self.assertFalse(data["supports_route_switch"])
        self.assertIsNone(data["last_route"])
        self.assertEqual(data["cloud_fallback_engine"], "")
        self.assertEqual(data["online_brain_workers"], [])

        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/engine/route",
            token=self.daemon.token,
            body={"mode": "cloud"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"], "route_switch_unsupported")

    def test_engine_status_and_route_hot_switch_hybrid(self) -> None:
        hybrid = HybridRouterEngine(self.engine, self.worker_registry)
        self.orchestrator.engine = hybrid
        self.worker_registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url="http://127.0.0.1:9",
                capabilities=("brain", "pc_control"),
            )
        )
        self.worker_registry.register(
            WorkerRecord(
                worker_id="tts_only",
                role="edge_node",
                base_url="http://127.0.0.1:10",
                capabilities=("tts",),
            )
        )

        # Initial GET /api/engine on hybrid.
        code, _, raw = _http_call(
            "GET",
            f"{self.daemon.url}/api/engine",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        info = json.loads(raw.decode("utf-8"))
        self.assertEqual(info["engine"], "hybrid")
        self.assertEqual(info["route_mode"], "auto")
        self.assertTrue(info["supports_route_switch"])
        self.assertEqual(info["cloud_fallback_engine"], "mock")
        self.assertEqual(info["online_brain_workers"], ["home_pc"])
        self.assertIsNone(info["last_route"])

        # Hot-switch to cloud via 'api' alias.
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/engine/route",
            token=self.daemon.token,
            body={"mode": "api"},
        )
        self.assertEqual(code, 200)
        switched = json.loads(raw.decode("utf-8"))
        self.assertTrue(switched["ok"])
        self.assertEqual(switched["engine"], "hybrid")
        self.assertEqual(switched["mode"], "cloud")
        self.assertEqual(switched["previous_mode"], "auto")
        self.assertEqual(switched["online_brain_workers"], ["home_pc"])

        # Execute a turn via MCP chat -> cloud mode routes directly to MockEngine & populates last_route.
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/mcp",
            token=self.daemon.token,
            body={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "shinobu_chat",
                    "arguments": {"text": "云端直连检查"},
                },
            },
        )
        self.assertEqual(code, 200)
        chat_result = json.loads(raw.decode("utf-8"))["result"]
        self.assertFalse(chat_result["isError"])

        code, _, raw = _http_call(
            "GET",
            f"{self.daemon.url}/api/engine",
            token=self.daemon.token,
        )
        self.assertEqual(code, 200)
        after = json.loads(raw.decode("utf-8"))
        self.assertEqual(after["route_mode"], "cloud")
        self.assertIsNotNone(after["last_route"])
        self.assertEqual(after["last_route"]["route_mode"], "cloud")
        self.assertEqual(after["last_route"]["target"], "cloud")
        self.assertIn("elapsed_ms", after["last_route"])
        self.assertIn("ts", after["last_route"])
        self.assertIsNotNone(self.worker_registry.get_online("pc_control"))

        # Hot-switch to local via 'agy' alias.
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/engine/route",
            token=self.daemon.token,
            body={"mode": "agy"},
        )
        self.assertEqual(code, 200)
        switched_local = json.loads(raw.decode("utf-8"))
        self.assertEqual(switched_local["mode"], "local")
        self.assertEqual(switched_local["previous_mode"], "cloud")

        # Invalid mode returns 400 and keeps current mode unchanged.
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/engine/route",
            token=self.daemon.token,
            body={"mode": "invalid_mode"},
        )
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"], "invalid_route_mode")
        self.assertEqual(hybrid.route_mode, "local")

        # Empty mode returns 400.
        code, _, raw = _http_call(
            "POST",
            f"{self.daemon.url}/api/engine/route",
            token=self.daemon.token,
            body={"mode": "   "},
        )
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
