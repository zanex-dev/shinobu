# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core.cloud.edge_node (EdgeNodeServer, EdgeNodeClient, EdgeNodeAgent)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Iterator
import unittest
import urllib.error
import urllib.parse
import urllib.request

from shinobu_core.cloud.edge_node import (
    EdgeNodeAgent,
    EdgeNodeClient,
    EdgeNodeServer,
    validate_bind_host,
)
from shinobu_core.cloud.hybrid_engine import HybridRouterEngine
from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.protocol.engine import TurnChunk, TurnRequest

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with _NO_PROXY_OPENER.open(req, timeout=10.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), json.loads(raw) if raw.strip() else {}


class _FakeLocalEngine:
    def __init__(self, *, name: str = "fake_local_agy", model: str = "local-flagship") -> None:
        self.name = name
        self.model = model
        self.turn_requests: list[TurnRequest] = []
        self.json_requests: list[tuple[str, str]] = []

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        self.turn_requests.append(req)
        yield TurnChunk(delta="hello ")
        yield TurnChunk(delta=f"from local ({req.user_text})")
        yield TurnChunk(
            delta="",
            tool_calls=[{"id": "tc_1", "name": "demo_tool", "arguments": {"k": "v"}}],
            usage={"prompt_tokens": 12, "completion_tokens": 8},
            done=True,
        )

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        self.json_requests.append((system, prompt))
        return {"source": "local_edge", "echo": prompt}


class _FakeCloudFallbackEngine:
    def __init__(self) -> None:
        self.name = "cloud_fallback"
        self.model = "cloud-model"
        self.calls: int = 0

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        self.calls += 1
        yield TurnChunk(delta=f"cloud-fallback:{req.user_text}")
        yield TurnChunk(delta="", done=True)

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        self.calls += 1
        return {"source": "cloud_fallback", "echo": prompt}


class _MockCloudControlPlane:
    """In-memory HTTP mock implementing /api/workers/{register,heartbeat,<id>}."""

    def __init__(self, *, expected_token: str = "cloud-secret") -> None:
        self.expected_token = expected_token
        self.registry = WorkerRegistry()
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _reply(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth_ok(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {parent.expected_token}"

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(raw.decode("utf-8"))

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth_ok():
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return
                body = self._read_json()
                path = urllib.parse.urlparse(self.path).path
                with parent._lock:
                    parent.events.append(("POST", path, body))
                if path == "/api/workers/register":
                    rec = WorkerRecord(
                        worker_id=str(body["worker_id"]),
                        role=str(body.get("role", "edge_node")),
                        base_url=str(body["base_url"]),
                        capabilities=tuple(body.get("capabilities", ())),
                        token=str(body.get("token", "")),
                        ttl_sec=float(body.get("ttl_sec", 45.0)),
                        meta=dict(body.get("meta", {})),
                    )
                    stored = parent.registry.register(rec)
                    self._reply(200, {"ok": True, "worker": stored.to_dict()})
                    return
                if path == "/api/workers/heartbeat":
                    wid = str(body.get("worker_id", ""))
                    found = parent.registry.heartbeat(wid, body.get("meta"))
                    if not found:
                        self._reply(404, {"ok": False, "found": False})
                        return
                    self._reply(200, {"ok": True, "found": True})
                    return
                self._reply(404, {"ok": False})

            def do_DELETE(self) -> None:  # noqa: N802
                if not self._auth_ok():
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return
                path = urllib.parse.urlparse(self.path).path
                with parent._lock:
                    parent.events.append(("DELETE", path, {}))
                prefix = "/api/workers/"
                if path.startswith(prefix):
                    wid = urllib.parse.unquote(path[len(prefix):])
                    removed = parent.registry.unregister(wid)
                    self._reply(200, {"ok": True, "removed": removed})
                    return
                self._reply(404, {"ok": False})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            self._thread = None


class _MockAiriBubbleServer:
    """In-memory local HTTP target verifying /node/bubble forwarding."""

    def __init__(self, *, expected_token: str = "bubble-tok") -> None:
        self.expected_token = expected_token
        self.received: list[dict[str, Any]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/api/bubble"

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {parent.expected_token}":
                    raw = json.dumps({"ok": False, "error": "bad bubble token"}).encode("utf-8")
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                parent.received.append(body)
                resp_raw = json.dumps({"displayed": True, "text": body.get("text")}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_raw)))
                self.end_headers()
                self.wfile.write(resp_raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            self._thread = None


class TestBindHostAndAuth(unittest.TestCase):
    def test_validate_bind_host_allows_loopback_tailscale_and_private(self) -> None:
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_bind_host("localhost"), "127.0.0.1")
        self.assertEqual(validate_bind_host("::1"), "::1")
        self.assertEqual(validate_bind_host("100.64.0.1"), "100.64.0.1")
        self.assertEqual(validate_bind_host("100.100.42.9"), "100.100.42.9")
        self.assertEqual(validate_bind_host("10.0.0.5"), "10.0.0.5")
        self.assertEqual(validate_bind_host("192.168.1.25"), "192.168.1.25")
        self.assertEqual(validate_bind_host("fd7a:115c:a1e0::1"), "fd7a:115c:a1e0::1")

    def test_validate_bind_host_rejects_wildcard_and_public_ips(self) -> None:
        forbidden = [
            "",
            "0.0.0.0",
            "::",
            "[::]",
            "*",
            "8.8.8.8",
            "1.1.1.1",
            "203.0.113.10",
            "2606:4700:4700::1111",
            "169.254.1.1",
            "example.com",
        ]
        for host in forbidden:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    validate_bind_host(host)

    def test_server_rejects_empty_token_and_public_bind_or_bubble_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                EdgeNodeServer(None, node_token="   ", allowed_roots=[Path(tmp)])
            with self.assertRaises(ValueError):
                EdgeNodeServer(None, node_token="tok", allowed_roots=[Path(tmp)], bind_host="0.0.0.0")
            with self.assertRaises(ValueError):
                EdgeNodeServer(None, node_token="tok", allowed_roots=[Path(tmp)], bind_host="8.8.8.8")
            with self.assertRaises(ValueError):
                EdgeNodeServer(
                    None,
                    node_token="tok",
                    allowed_roots=[Path(tmp)],
                    airi_bubble_url="http://8.8.8.8:8080/bubble",
                )

    def test_bearer_auth_enforced_on_all_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = EdgeNodeServer(
                _FakeLocalEngine(),
                node_token="edge-secret-token",
                allowed_roots=[Path(tmp)],
                port=0,
            )
            with server:
                code, body = _http_json("GET", f"{server.base_url}/node/health")
                self.assertEqual(code, 401)
                self.assertFalse(body["ok"])

                code, _ = _http_json(
                    "GET",
                    f"{server.base_url}/node/health",
                    headers={"Authorization": "Bearer wrong-token"},
                )
                self.assertEqual(code, 401)

                code, _ = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={"mode": "stream_turn", "user_text": "hi"},
                    headers={"Authorization": "Basic edge-secret-token"},
                )
                self.assertEqual(code, 401)

                code, health = _http_json(
                    "GET",
                    f"{server.base_url}/node/health",
                    headers={"Authorization": "Bearer edge-secret-token"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(health["ok"])
                self.assertTrue(health["has_local_engine"])
                self.assertIn("brain", health["capabilities"])
                self.assertIn("terminal", health["capabilities"])


class TestNodeTurnAndHybridIntegration(unittest.TestCase):
    def test_turn_endpoint_aggregates_stream_and_complete_json(self) -> None:
        engine = _FakeLocalEngine()
        with tempfile.TemporaryDirectory() as tmp:
            server = EdgeNodeServer(
                engine,
                node_token="tok-turn",
                allowed_roots=[Path(tmp)],
                port=0,
            )
            auth = {"Authorization": "Bearer tok-turn"}
            with server:
                code, data = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={
                        "mode": "stream_turn",
                        "episode_id": "ep_42",
                        "system_prefix": "sys",
                        "history": [{"role": "user", "content": "prev"}],
                        "user_text": "ping",
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "demo_tool",
                                    "description": "demo",
                                    "parameters": {"type": "object", "properties": {}},
                                },
                            }
                        ],
                    },
                    headers=auth,
                )
                self.assertEqual(code, 200)
                self.assertTrue(data["ok"])
                self.assertEqual(data["delta"], "hello from local (ping)")
                self.assertEqual(len(data["tool_calls"]), 1)
                self.assertEqual(data["tool_calls"][0]["name"], "demo_tool")
                self.assertEqual(data["usage"], {"prompt_tokens": 12, "completion_tokens": 8})
                self.assertEqual(len(engine.turn_requests), 1)
                self.assertEqual(engine.turn_requests[0].episode_id, "ep_42")
                self.assertEqual(engine.turn_requests[0].tools[0].name, "demo_tool")

                code, jdata = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={"mode": "complete_json", "system": "s", "prompt": "p"},
                    headers=auth,
                )
                self.assertEqual(code, 200)
                self.assertEqual(jdata["result"], {"source": "local_edge", "echo": "p"})

    def test_turn_without_local_engine_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = EdgeNodeServer(None, node_token="tok", allowed_roots=[Path(tmp)], port=0)
            with server:
                code, data = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={"mode": "stream_turn", "user_text": "hi"},
                    headers={"Authorization": "Bearer tok"},
                )
                self.assertEqual(code, 503)
                self.assertFalse(data["ok"])

    def test_end_to_end_hybrid_router_routes_to_edge_and_falls_back_on_stop(self) -> None:
        cloud_plane = _MockCloudControlPlane(expected_token="cloud-tok")
        cloud_url = cloud_plane.start()
        local_engine = _FakeLocalEngine()
        fallback_engine = _FakeCloudFallbackEngine()
        hybrid = HybridRouterEngine(fallback_engine, cloud_plane.registry, remote_timeout_sec=5.0)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                agent = EdgeNodeAgent(
                    local_engine=local_engine,
                    cloud_url=cloud_url,
                    cloud_token="cloud-tok",
                    node_token="node-tok",
                    allowed_roots=[Path(tmp)],
                    worker_id="home_pc",
                    port=0,
                    heartbeat_sec=0.1,
                )
                _, registered = agent.start(fail_fast=True)
                self.assertTrue(registered)

                # While edge node is online, HybridRouterEngine routes to local_engine.
                chunks = list(
                    hybrid.stream_turn(
                        TurnRequest(episode_id="ep1", system_prefix="sys", user_text="via-edge")
                    )
                )
                combined = "".join(c.delta for c in chunks)
                self.assertEqual(combined, "hello from local (via-edge)")
                self.assertEqual(hybrid.last_route["target"], "remote")
                self.assertEqual(hybrid.last_route["worker_id"], "home_pc")
                self.assertEqual(fallback_engine.calls, 0)

                json_res = hybrid.complete_json("sys", "classify")
                self.assertEqual(json_res["source"], "local_edge")
                self.assertEqual(hybrid.last_route["target"], "remote")

                # Graceful shutdown unregisters from cloud -> instant cloud fallback.
                agent.stop()
                self.assertIsNone(cloud_plane.registry.get("home_pc"))

                fb_chunks = list(
                    hybrid.stream_turn(
                        TurnRequest(episode_id="ep2", system_prefix="sys", user_text="after-stop")
                    )
                )
                self.assertEqual("".join(c.delta for c in fb_chunks), "cloud-fallback:after-stop")
                self.assertEqual(hybrid.last_route["target"], "cloud_fallback")
                self.assertGreaterEqual(fallback_engine.calls, 1)
        finally:
            cloud_plane.stop()

    def test_turn_endpoint_streams_sse_chunks_and_keeps_json_fallback(self) -> None:
        import json as _json
        import urllib.error as _urlerr
        import urllib.request as _urlreq
        from shinobu_core.cloud.edge_node import TurnChunk as _TurnChunk

        class _MultiChunkEngine:
            def stream_turn(self, req):  # type: ignore[no-untyped-def]
                yield _TurnChunk(delta="hello ", done=False)
                yield _TurnChunk(
                    delta=f"{req.user_text}!",
                    tool_calls=[{"name": "demo_tool", "arguments": {"x": 1}}],
                    usage={"prompt_tokens": 5, "completion_tokens": 3},
                    done=True,
                )

            def complete_json(self, system: str, prompt: str):  # type: ignore[no-untyped-def]
                return {"echo": prompt}

        with tempfile.TemporaryDirectory() as tmp:
            server = EdgeNodeServer(
                _MultiChunkEngine(),  # type: ignore[arg-type]
                node_token="tok-sse",
                allowed_roots=[Path(tmp)],
                port=0,
            )
            with server:
                req = _urlreq.Request(
                    f"{server.base_url}/node/turn",
                    data=_json.dumps(
                        {"mode": "stream_turn", "episode_id": "ep_sse", "user_text": "sse"}
                    ).encode("utf-8"),
                    method="POST",
                    headers={
                        "Authorization": "Bearer tok-sse",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                )
                events: list[dict] = []
                with _urlreq.urlopen(req, timeout=5.0) as resp:
                    self.assertEqual(resp.status, 200)
                    ctype = str(resp.headers.get("Content-Type") or "")
                    self.assertTrue(ctype.lower().startswith("text/event-stream"))
                    for raw_line in resp:
                        line = raw_line.decode("utf-8").strip()
                        if line.startswith("data:"):
                            events.append(_json.loads(line[5:].strip()))

                self.assertEqual(len(events), 2)
                self.assertEqual(events[0]["delta"], "hello ")
                self.assertFalse(events[0]["done"])
                self.assertEqual(events[1]["delta"], "sse!")
                self.assertTrue(events[1]["done"])
                self.assertEqual(events[1]["tool_calls"], [{"name": "demo_tool", "arguments": {"x": 1}}])
                self.assertEqual(events[1]["usage"], {"prompt_tokens": 5, "completion_tokens": 3})

                # JSON fallback when Accept is application/json
                code, jdata = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={"mode": "stream_turn", "user_text": "json"},
                    headers={"Authorization": "Bearer tok-sse", "Accept": "application/json"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(jdata["ok"])
                self.assertEqual(jdata["delta"], "hello json!")

                # complete_json stays JSON even with text/event-stream Accept
                code2, cjdata = _http_json(
                    "POST",
                    f"{server.base_url}/node/turn",
                    payload={"mode": "complete_json", "system": "s", "prompt": "p"},
                    headers={"Authorization": "Bearer tok-sse", "Accept": "text/event-stream"},
                )
                self.assertEqual(code2, 200)
                self.assertEqual(cjdata["result"], {"echo": "p"})

            # 503 when local_engine is None even if Accept is text/event-stream
            empty_server = EdgeNodeServer(None, node_token="tok-empty", allowed_roots=[Path(tmp)], port=0)
            with empty_server:
                req503 = _urlreq.Request(
                    f"{empty_server.base_url}/node/turn",
                    data=_json.dumps({"mode": "stream_turn", "user_text": "hi"}).encode("utf-8"),
                    method="POST",
                    headers={
                        "Authorization": "Bearer tok-empty",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                )
                with self.assertRaises(_urlerr.HTTPError) as ctx:
                    _urlreq.urlopen(req503, timeout=5.0)
                self.assertEqual(ctx.exception.code, 503)

    def test_edge_node_server_stop_calls_local_engine_close(self) -> None:
        class _ClosableEngine(_FakeLocalEngine):
            def __init__(self, *, fail_close: bool = False) -> None:
                super().__init__()
                self.close_calls = 0
                self._fail_close = fail_close

            def close(self) -> None:
                self.close_calls += 1
                if self._fail_close:
                    raise RuntimeError("close boom")

        with tempfile.TemporaryDirectory() as tmp:
            engine = _ClosableEngine()
            server = EdgeNodeServer(engine, node_token="tok-close", allowed_roots=[Path(tmp)], port=0)
            with server:
                self.assertEqual(engine.close_calls, 0)
            self.assertEqual(engine.close_calls, 1)

            failing_engine = _ClosableEngine(fail_close=True)
            server2 = EdgeNodeServer(failing_engine, node_token="tok-close2", allowed_roots=[Path(tmp)], port=0)
            server2.start()
            server2.stop()
            self.assertEqual(failing_engine.close_calls, 1)

    def test_node_cli_passes_agy_fastpath_options_and_env_defaults(self) -> None:
        import os as _os
        from unittest import mock as _mock
        from shinobu_core.engines.agy_engines import AgyCliEngine as _AgyCliEngine
        from shinobu_core.server import cli as _cli

        with tempfile.TemporaryDirectory() as tmp:
            kernel = _cli.ShinobuKernel(home=Path(tmp) / "home")
            clean_env = {
                k: v
                for k, v in _os.environ.items()
                if k not in ("SHINOBU_AGY_EFFORT", "SHINOBU_AGY_MODE", "SHINOBU_AGY_PERSISTENT", "SHINOBU_NODE_ENGINE")
            }
            with _mock.patch.dict(_os.environ, clean_env, clear=True):
                # Default node wiring: agy_cli with effort='low' and persistent=True
                server, _ = _cli.build_edge_node(
                    kernel,
                    cloud_url="http://127.0.0.1:8787",
                    cloud_token="tok",
                    allowed_roots=[Path(tmp)],
                )
                self.assertIsInstance(server.local_engine, _AgyCliEngine)
                self.assertEqual(server.local_engine.effort, "low")  # type: ignore[union-attr]
                self.assertIsNone(server.local_engine.agent_mode)  # type: ignore[union-attr]
                self.assertTrue(server.local_engine.persistent)  # type: ignore[union-attr]

                # Explicit CLI overrides
                server2, _ = _cli.build_edge_node(
                    kernel,
                    cloud_url="http://127.0.0.1:8787",
                    cloud_token="tok",
                    allowed_roots=[Path(tmp)],
                    effort="high",
                    agy_mode="auto",
                    persistent=False,
                )
                self.assertIsInstance(server2.local_engine, _AgyCliEngine)
                self.assertEqual(server2.local_engine.effort, "high")  # type: ignore[union-attr]
                self.assertEqual(server2.local_engine.agent_mode, "auto")  # type: ignore[union-attr]
                self.assertFalse(server2.local_engine.persistent)  # type: ignore[union-attr]

                # Regular _build_engine('agy_cli') keeps legacy defaults when env unset
                plain_engine = _cli._build_engine(kernel, "agy_cli")
                self.assertIsInstance(plain_engine, _AgyCliEngine)
                self.assertIsNone(plain_engine.effort)  # type: ignore[union-attr]
                self.assertIsNone(plain_engine.agent_mode)  # type: ignore[union-attr]
                self.assertFalse(plain_engine.persistent)  # type: ignore[union-attr]

            # Env overrides: SHINOBU_AGY_PERSISTENT=0 disables persistent in node
            env_off = dict(clean_env)
            env_off["SHINOBU_AGY_EFFORT"] = "medium"
            env_off["SHINOBU_AGY_MODE"] = "auto"
            env_off["SHINOBU_AGY_PERSISTENT"] = "0"
            with _mock.patch.dict(_os.environ, env_off, clear=True):
                server3, _ = _cli.build_edge_node(
                    kernel,
                    cloud_url="http://127.0.0.1:8787",
                    cloud_token="tok",
                    allowed_roots=[Path(tmp)],
                )
                self.assertIsInstance(server3.local_engine, _AgyCliEngine)
                self.assertEqual(server3.local_engine.effort, "medium")  # type: ignore[union-attr]
                self.assertEqual(server3.local_engine.agent_mode, "auto")  # type: ignore[union-attr]
                self.assertFalse(server3.local_engine.persistent)  # type: ignore[union-attr]

            parser = _cli.build_parser()
            parsed = parser.parse_args(
                [
                    "node",
                    "--cloud-url",
                    "http://127.0.0.1:8787",
                    "--effort",
                    "high",
                    "--agy-mode",
                    "auto",
                    "--no-persistent",
                ]
            )
            self.assertEqual(parsed.effort, "high")
            self.assertEqual(parsed.agy_mode, "auto")
            self.assertTrue(parsed.no_persistent)


class TestNodeExecSecurity(unittest.TestCase):
    def test_exec_l0_allowed_l1_requires_cloud_approval_l2_never_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as parent_tmp:
            workspace = Path(parent_tmp) / "allowed_ws"
            outside = Path(parent_tmp) / "outside_dir"
            workspace.mkdir()
            outside.mkdir()
            outside_file = outside / "secret.txt"
            outside_file.write_text("nope", encoding="utf-8")

            server = EdgeNodeServer(
                None,
                node_token="exec-tok",
                allowed_roots=[workspace],
                port=0,
            )
            auth = {"Authorization": "Bearer exec-tok"}
            with server:
                # 1. L0 readonly command succeeds inside allowed_roots
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": ["whoami"], "cwd": str(workspace)},
                    headers=auth,
                )
                self.assertEqual(code, 200)
                self.assertTrue(res["ok"])
                self.assertEqual(res["tier"], "L0")
                self.assertEqual(res["exit_code"], 0)
                self.assertTrue(res["stdout"].strip())

                # 2. shell=True is strictly rejected
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": ["whoami"], "shell": True},
                    headers=auth,
                )
                self.assertEqual(code, 400)
                self.assertIn("shell=True", res["error"])

                # 3. Raw string argv is strictly rejected
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": "whoami"},
                    headers=auth,
                )
                self.assertEqual(code, 400)

                # 4. cwd outside allowed_roots is rejected
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": ["whoami"], "cwd": str(outside)},
                    headers=auth,
                )
                self.assertEqual(code, 403)
                self.assertIn("outside allowed_roots", res["error"])

                # 5. Path traversal and external absolute path in argv are rejected
                code, _ = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": ["cat", "../outside_dir/secret.txt"], "cwd": str(workspace)},
                    headers=auth,
                )
                self.assertEqual(code, 403)

                code, _ = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": ["cat", str(outside_file)], "cwd": str(workspace)},
                    headers=auth,
                )
                self.assertEqual(code, 403)

                # 6. L1 without cloud approval is rejected
                l1_argv = [sys.executable, "-c", "print('edge-l1-approved-ok')"]
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": l1_argv, "cwd": str(workspace)},
                    headers=auth,
                )
                self.assertEqual(code, 403)
                self.assertEqual(res["tier"], "L1")
                self.assertTrue(res["requires_approval"])

                # 7. L1 with cloud_approved=True but missing approval_id is still rejected
                code, _ = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={"argv": l1_argv, "cwd": str(workspace), "cloud_approved": True},
                    headers=auth,
                )
                self.assertEqual(code, 403)

                # 8. L1 with explicit cloud approval succeeds
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/exec",
                    payload={
                        "argv": l1_argv,
                        "cwd": str(workspace),
                        "cloud_approved": True,
                        "approval_id": "apr_2026_01",
                    },
                    headers=auth,
                )
                self.assertEqual(code, 200)
                self.assertTrue(res["ok"])
                self.assertEqual(res["tier"], "L1")
                self.assertEqual(res["exit_code"], 0)
                self.assertEqual(res["approval_id"], "apr_2026_01")
                self.assertIn("edge-l1-approved-ok", res["stdout"])

                # 9. L2 forbidden commands are ALWAYS denied even with cloud_approved=True
                for bad_argv in (
                    ["sudo", "ls"],
                    ["shutdown", "-h", "now"],
                    ["cat", ".allow_media"],
                ):
                    with self.subTest(bad_argv=bad_argv):
                        code, res = _http_json(
                            "POST",
                            f"{server.base_url}/node/exec",
                            payload={
                                "argv": bad_argv,
                                "cwd": str(workspace),
                                "cloud_approved": True,
                                "approval_id": "apr_forced",
                            },
                            headers=auth,
                        )
                        self.assertEqual(code, 403)
                        self.assertEqual(res["tier"], "L2")


class TestNodeBubbleAndClientLifecycle(unittest.TestCase):
    def test_bubble_forwarding_to_local_airi_endpoint(self) -> None:
        airi = _MockAiriBubbleServer(expected_token="bubble-tok")
        airi_url = airi.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server = EdgeNodeServer(
                    None,
                    node_token="node-tok",
                    allowed_roots=[Path(tmp)],
                    port=0,
                    airi_bubble_url=airi_url,
                    airi_bubble_token="bubble-tok",
                )
                with server:
                    code, res = _http_json(
                        "POST",
                        f"{server.base_url}/node/bubble",
                        payload={"text": "hello desktop", "emotion": "happy"},
                        headers={"Authorization": "Bearer node-tok"},
                    )
                    self.assertEqual(code, 200)
                    self.assertTrue(res["ok"])
                    self.assertTrue(res["forwarded"])
                    self.assertEqual(len(airi.received), 1)
                    self.assertEqual(airi.received[0]["text"], "hello desktop")
                    self.assertEqual(airi.received[0]["emotion"], "happy")
        finally:
            airi.stop()

    def test_bubble_unconfigured_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = EdgeNodeServer(None, node_token="node-tok", allowed_roots=[Path(tmp)], port=0)
            with server:
                code, res = _http_json(
                    "POST",
                    f"{server.base_url}/node/bubble",
                    payload={"text": "hello"},
                    headers={"Authorization": "Bearer node-tok"},
                )
                self.assertEqual(code, 503)
                self.assertFalse(res["ok"])

    def test_client_register_heartbeat_reregister_and_graceful_unregister(self) -> None:
        cloud = _MockCloudControlPlane(expected_token="cloud-secret")
        cloud_url = cloud.start()
        try:
            client = EdgeNodeClient(
                cloud_url,
                "cloud-secret",
                worker_id="home_pc",
                advertise_url="http://100.64.0.10:18792",
                node_token="edge-tok",
                capabilities=("brain", "terminal", "bubble"),
                heartbeat_sec=0.08,
                ttl_sec=30.0,
            )
            ok = client.start(fail_fast=True)
            self.assertTrue(ok)
            self.assertTrue(client.is_registered)

            rec = cloud.registry.get("home_pc")
            self.assertIsNotNone(rec)
            assert rec is not None
            self.assertEqual(rec.base_url, "http://100.64.0.10:18792")
            self.assertEqual(rec.capabilities, ("brain", "terminal", "bubble"))
            first_seen = rec.last_seen

            # Wait for at least two heartbeat cycles.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                hb_count = sum(1 for m, p, _ in cloud.events if m == "POST" and p == "/api/workers/heartbeat")
                if hb_count >= 2:
                    break
                time.sleep(0.03)
            self.assertGreaterEqual(
                sum(1 for m, p, _ in cloud.events if m == "POST" and p == "/api/workers/heartbeat"),
                2,
            )
            refreshed = cloud.registry.get("home_pc")
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual(refreshed.last_seen, first_seen)

            # Simulate cloud restart (registry wiped) -> heartbeat 404 triggers auto-re-register.
            cloud.registry.unregister("home_pc")
            self.assertIsNone(cloud.registry.get("home_pc"))

            deadline = time.time() + 2.0
            while time.time() < deadline:
                if cloud.registry.get("home_pc") is not None:
                    break
                time.sleep(0.03)
            self.assertIsNotNone(cloud.registry.get("home_pc"))

            # Graceful shutdown sends DELETE /api/workers/home_pc.
            unregistered = client.stop()
            self.assertTrue(unregistered)
            self.assertFalse(client.is_registered)
            self.assertIsNone(cloud.registry.get("home_pc"))
            self.assertTrue(
                any(m == "DELETE" and p == "/api/workers/home_pc" for m, p, _ in cloud.events)
            )
        finally:
            cloud.stop()

    def test_client_delayed_cloud_startup_auto_registers_and_fires_callback(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        cloud_url = f"http://127.0.0.1:{port}"
        registered_events: list[bool] = []
        errors: list[tuple[str, Exception]] = []
        seen_paths: list[tuple[str, str]] = []

        class _DelayedCloudHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                seen_paths.append(("POST", self.path))
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self) -> None:
                seen_paths.append(("DELETE", self.path))
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        client = EdgeNodeClient(
            cloud_url,
            "cloud-secret",
            worker_id="home_pc",
            advertise_url="http://100.64.0.10:18792",
            node_token="edge-tok",
            heartbeat_sec=0.06,
            request_timeout_sec=0.5,
            on_error=lambda stage, exc: errors.append((stage, exc)),
            on_registered=lambda is_re: registered_events.append(is_re),
            presence_enabled=False,
        )
        server: ThreadingHTTPServer | None = None
        server_thread: threading.Thread | None = None
        try:
            ok = client.start(fail_fast=False)
            self.assertFalse(ok)
            self.assertFalse(client.is_registered)
            self.assertEqual(registered_events, [])
            self.assertTrue(any(stage == "register" for stage, _ in errors))

            # Start cloud control plane after the edge client is already waiting.
            server = ThreadingHTTPServer(("127.0.0.1", port), _DelayedCloudHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            deadline = time.time() + 2.5
            while time.time() < deadline:
                if client.is_registered and registered_events:
                    break
                time.sleep(0.03)

            self.assertTrue(client.is_registered)
            self.assertEqual(registered_events, [False])
            self.assertIn(("POST", "/api/workers/register"), seen_paths)
        finally:
            client.stop(unregister=server is not None)
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=2.0)

    def test_client_heartbeat_network_error_resets_registered_and_reregisters_on_recovery(self) -> None:
        cloud = _MockCloudControlPlane(expected_token="cloud-secret")
        cloud_url = cloud.start()
        registered_events: list[bool] = []
        errors: list[tuple[str, Exception]] = []
        try:
            client = EdgeNodeClient(
                cloud_url,
                "cloud-secret",
                worker_id="home_pc",
                advertise_url="http://100.64.0.10:18792",
                node_token="edge-tok",
                heartbeat_sec=0.06,
                ttl_sec=30.0,
                on_error=lambda stage, exc: errors.append((stage, exc)),
                on_registered=lambda is_re: registered_events.append(is_re),
                presence_enabled=False,
            )
            self.assertTrue(client.start(fail_fast=True))
            self.assertEqual(registered_events, [False])
            self.assertTrue(client.is_registered)

            orig_request_json = client._request_json
            network_down = threading.Event()

            def _flaky_request_json(
                method: str,
                path: str,
                payload: Any = None,
            ) -> tuple[int, dict[str, Any]]:
                if network_down.is_set() and path == "/api/workers/heartbeat":
                    raise urllib.error.URLError("simulated vpn drop")
                return orig_request_json(method, path, payload)

            client._request_json = _flaky_request_json  # type: ignore[method-assign]

            # Direct heartbeat failure must reset _registered before re-raising.
            network_down.set()
            with self.assertRaises(urllib.error.URLError):
                client.send_heartbeat()
            self.assertFalse(client.is_registered)

            # Once VPN recovers, next heartbeat tick immediately calls register() -> on_registered(True).
            reg_before = sum(1 for m, p, _ in cloud.events if m == "POST" and p == "/api/workers/register")
            network_down.clear()

            deadline = time.time() + 2.0
            while time.time() < deadline:
                if client.is_registered and len(registered_events) >= 2:
                    break
                time.sleep(0.03)

            self.assertTrue(client.is_registered)
            self.assertEqual(registered_events, [False, True])
            reg_after = sum(1 for m, p, _ in cloud.events if m == "POST" and p == "/api/workers/register")
            self.assertGreater(reg_after, reg_before)

            client.stop()
        finally:
            cloud.stop()

    def test_cmd_node_honest_startup_status_and_rate_limited_callbacks(self) -> None:
        import argparse
        import contextlib
        import io
        from unittest import mock

        from shinobu_core.server import cli as cli_mod

        # Verify build_edge_node forwards on_error and on_registered to EdgeNodeClient.
        cb_err = lambda stage, exc: None
        cb_reg = lambda is_re: None
        _srv, real_client = cli_mod.build_edge_node(
            object(),  # type: ignore[arg-type]
            cloud_url="http://127.0.0.1:18790",
            engine_name="none",
            presence_enabled=False,
            on_error=cb_err,
            on_registered=cb_reg,
        )
        self.assertIs(real_client._on_error, cb_err)
        self.assertIs(real_client._on_registered, cb_reg)

        def _make_args() -> argparse.Namespace:
            return argparse.Namespace(
                cloud_url="http://100.64.0.1:18790",
                cloud_token="tok",
                node_token="node-tok",
                worker_id="home_pc",
                bind_host="127.0.0.1",
                port=18792,
                advertise_url="http://100.64.0.10:18792",
                engine="none",
                allowed_root=None,
                airi_bubble_url="",
                heartbeat_sec=15.0,
                effort=None,
                agy_mode=None,
                no_persistent=False,
                no_presence=True,
            )

        class _FakeServer:
            def start(self) -> dict[str, Any]:
                return {"url": "http://100.64.0.10:18792"}

            def stop(self) -> None:
                return

        class _InstantStopEvent:
            def __init__(self, on_wait: Any = None) -> None:
                self._set = False
                self._on_wait = on_wait

            def is_set(self) -> bool:
                return self._set

            def set(self) -> None:
                self._set = True

            def wait(self, timeout: float | None = None) -> bool:
                if self._on_wait is not None:
                    self._on_wait()
                self._set = True
                return True

        # Case 1: Initial registration succeeds -> prints "(registered with ...)".
        class _ConnectedClient:
            def __init__(self, on_registered: Any) -> None:
                self.worker_id = "home_pc"
                self.advertise_url = "http://100.64.0.10:18792"
                self.heartbeat_sec = 15.0
                self._on_registered = on_registered

            def start(self) -> bool:
                if self._on_registered is not None:
                    self._on_registered(False)
                return True

            def stop(self) -> bool:
                return True

        def _build_connected(_kernel: Any, **kwargs: Any) -> tuple[Any, Any]:
            return _FakeServer(), _ConnectedClient(kwargs.get("on_registered"))

        out_ok = io.StringIO()
        err_ok = io.StringIO()
        with (
            mock.patch.object(cli_mod, "build_edge_node", side_effect=_build_connected),
            mock.patch.object(cli_mod.threading, "Event", return_value=_InstantStopEvent()),
            contextlib.redirect_stdout(out_ok),
            contextlib.redirect_stderr(err_ok),
        ):
            rc = cli_mod._cmd_node(_make_args(), object())  # type: ignore[arg-type]

        self.assertEqual(rc, 0)
        self.assertIn("(registered with http://100.64.0.1:18790)", out_ok.getvalue())
        self.assertNotIn("waiting for cloud", out_ok.getvalue())
        self.assertEqual(out_ok.getvalue().count("registered with"), 1)
        self.assertEqual(err_ok.getvalue(), "")

        # Case 2: Initial registration fails -> prints "(waiting for cloud ...)", rate-limits errors, logs reconnect.
        captured_callbacks: dict[str, Any] = {}
        fake_now = [100.0]

        class _WaitingClient:
            def __init__(self, on_error: Any, on_registered: Any) -> None:
                self.worker_id = "home_pc"
                self.advertise_url = "http://100.64.0.10:18792"
                self.heartbeat_sec = 15.0
                self._on_error = on_error
                self._on_registered = on_registered

            def start(self) -> bool:
                if self._on_error is not None:
                    self._on_error("register", OSError("vpn not ready"))
                return False

            def stop(self) -> bool:
                return True

        def _build_waiting(_kernel: Any, **kwargs: Any) -> tuple[Any, Any]:
            captured_callbacks["on_error"] = kwargs.get("on_error")
            captured_callbacks["on_registered"] = kwargs.get("on_registered")
            return _FakeServer(), _WaitingClient(
                captured_callbacks["on_error"],
                captured_callbacks["on_registered"],
            )

        def _simulate_background_events() -> None:
            on_err = captured_callbacks["on_error"]
            on_reg = captured_callbacks["on_registered"]
            # Same error within 60s -> suppressed.
            fake_now[0] = 120.0
            on_err("register", OSError("vpn not ready"))
            # Same error at >=60s -> logged again.
            fake_now[0] = 165.0
            on_err("register", OSError("vpn not ready"))
            # Background late registration & subsequent reconnect.
            on_reg(False)
            on_reg(True)

        out_wait = io.StringIO()
        err_wait = io.StringIO()
        with (
            mock.patch.object(cli_mod, "build_edge_node", side_effect=_build_waiting),
            mock.patch.object(cli_mod.threading, "Event", return_value=_InstantStopEvent(_simulate_background_events)),
            mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: fake_now[0]),
            contextlib.redirect_stdout(out_wait),
            contextlib.redirect_stderr(err_wait),
        ):
            rc = cli_mod._cmd_node(_make_args(), object())  # type: ignore[arg-type]

        self.assertEqual(rc, 0)
        stdout_text = out_wait.getvalue()
        stderr_text = err_wait.getvalue()
        self.assertIn(
            "(waiting for cloud http://100.64.0.1:18790; retrying every 15s)",
            stdout_text,
        )
        self.assertNotIn("(registered with http://100.64.0.1:18790)", stdout_text)
        self.assertIn("registered with http://100.64.0.1:18790", stdout_text)
        self.assertIn("re-registered with http://100.64.0.1:18790", stdout_text)
        self.assertEqual(stderr_text.count("vpn not ready"), 2)


class TestEdgePresenceSensorAndEvaluator(unittest.TestCase):
    def test_get_system_idle_seconds_win32_wrap_and_degradation(self) -> None:
        import ctypes as _ctypes
        from unittest import mock as _mock
        from shinobu_core.cloud import presence as _presence

        # 1. Non-Windows degrades cleanly to None
        with _mock.patch.object(_presence.sys, "platform", "linux"):
            self.assertIsNone(_presence.get_system_idle_seconds())

        # 2. Normal Windows tick diff
        class _FakeUser32:
            def __init__(self, dw_time: int, ok: int = 1) -> None:
                self._dw_time = dw_time
                self._ok = ok

            def GetLastInputInfo(self, ptr: Any) -> int:  # noqa: N802
                if not self._ok:
                    return 0
                struct = _ctypes.cast(ptr, _ctypes.POINTER(_presence._LASTINPUTINFO)).contents
                struct.dwTime = self._dw_time
                return 1

        class _FakeKernel32:
            def __init__(self, tick64: int) -> None:
                self._tick64 = tick64

            def GetTickCount64(self) -> int:  # noqa: N802
                return self._tick64

        class _FakeWinDLL:
            def __init__(self, dw_time: int, tick64: int, ok: int = 1) -> None:
                self.user32 = _FakeUser32(dw_time, ok=ok)
                self.kernel32 = _FakeKernel32(tick64)

        with _mock.patch.object(_presence.sys, "platform", "win32"):
            with _mock.patch.object(_presence.ctypes, "windll", _FakeWinDLL(7500, 10000), create=True):
                self.assertEqual(_presence.get_system_idle_seconds(), 2.5)

            # 32-bit wrap-around: tick64 = 2**32 + 500, dwTime = 2**32 - 500 -> 1.0s
            with _mock.patch.object(
                _presence.ctypes,
                "windll",
                _FakeWinDLL(0xFFFFFE0C, (1 << 32) + 500),
                create=True,
            ):
                self.assertEqual(_presence.get_system_idle_seconds(), 1.0)

            # GetLastInputInfo failure returns None
            with _mock.patch.object(_presence.ctypes, "windll", _FakeWinDLL(0, 1000, ok=0), create=True):
                self.assertIsNone(_presence.get_system_idle_seconds())

            # Exception in Win32 call returns None
            broken = _mock.MagicMock()
            broken.user32.GetLastInputInfo.side_effect = OSError("access denied")
            with _mock.patch.object(_presence.ctypes, "windll", broken, create=True):
                self.assertIsNone(_presence.get_system_idle_seconds())

    def test_local_presence_tracker_streak_away_reset_and_privacy_schema(self) -> None:
        from datetime import datetime, timezone
        from shinobu_core.cloud.presence import LocalPresenceTracker

        # 2026-06-01 06:00:00 UTC == 14:00:00 UTC+8
        base_ts = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc).timestamp()
        now = [base_ts]
        idle = [10.0]

        def fake_idle() -> float | None:
            return idle[0]

        tracker = LocalPresenceTracker(
            away_threshold_sec=300.0,
            idle_fn=fake_idle,
            clock=lambda: now[0],
            tz_offset_hours=8.0,
        )

        # First active sample starts streak at 0.0
        s1 = tracker.sample()
        self.assertEqual(
            set(s1.keys()),
            {"state", "idle_sec", "active_streak_sec", "local_hour", "sampled_at"},
        )
        self.assertEqual(s1["state"], "active")
        self.assertEqual(s1["idle_sec"], 10.0)
        self.assertEqual(s1["active_streak_sec"], 0.0)
        self.assertEqual(s1["local_hour"], 14)

        # Still active after 2.5 hours -> streak accumulates
        now[0] += 9000.0
        idle[0] = 5.0
        s2 = tracker.sample()
        self.assertEqual(s2["state"], "active")
        self.assertEqual(s2["active_streak_sec"], 9000.0)
        self.assertEqual(s2["local_hour"], 16)

        # Idle reaches away threshold -> away & streak resets to 0.0
        now[0] += 360.0
        idle[0] = 300.0
        s3 = tracker.sample()
        self.assertEqual(s3["state"], "away")
        self.assertEqual(s3["active_streak_sec"], 0.0)

        # Returns to active -> fresh streak starts from 0.0 then grows
        now[0] += 60.0
        idle[0] = 1.0
        s4 = tracker.sample()
        self.assertEqual(s4["state"], "active")
        self.assertEqual(s4["active_streak_sec"], 0.0)

        now[0] += 120.0
        idle[0] = 2.0
        s5 = tracker.sample()
        self.assertEqual(s5["state"], "active")
        self.assertEqual(s5["active_streak_sec"], 120.0)

        # Sensor unavailable -> unknown & resets streak
        now[0] += 60.0
        idle[0] = None
        s6 = tracker.sample()
        self.assertEqual(s6["state"], "unknown")
        self.assertIsNone(s6["idle_sec"])
        self.assertEqual(s6["active_streak_sec"], 0.0)

    def test_presence_evaluator_long_session_late_night_and_cooldown(self) -> None:
        from shinobu_core.cloud.presence import PresenceEvaluator
        from shinobu_core.protocol.events import EventKind

        now = [10000.0]
        registry = WorkerRegistry(clock=lambda: now[0])
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url="http://127.0.0.1:18792",
                capabilities=("brain",),
                ttl_sec=600.0,
            )
        )
        evaluator = PresenceEvaluator(
            registry,
            long_session_sec=9000.0,
            late_night_hours=(1, 5),
            cooldown_sec=3600.0,
            clock=lambda: now[0],
        )

        # Below threshold -> no events
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 3.0,
                    "active_streak_sec": 3600.0,
                    "local_hour": 14,
                    "sampled_at": now[0],
                }
            },
        )
        self.assertEqual(evaluator(), [])

        # Reaches 2.5h -> long_session event fires
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 4.0,
                    "active_streak_sec": 9100.0,
                    "local_hour": 15,
                    "sampled_at": now[0],
                }
            },
        )
        ev1 = evaluator()
        self.assertEqual(len(ev1), 1)
        self.assertEqual(ev1[0].kind, EventKind.BUBBLE)
        self.assertEqual(ev1[0].payload["source"], "presence")
        self.assertEqual(ev1[0].payload["label"], "long_session")
        self.assertTrue(ev1[0].payload["text"])

        # Within cooldown -> suppressed
        now[0] += 1800.0
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 2.0,
                    "active_streak_sec": 10900.0,
                    "local_hour": 16,
                    "sampled_at": now[0],
                }
            },
        )
        self.assertEqual(evaluator(), [])

        # After cooldown -> fires again
        now[0] += 1800.0
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 2.0,
                    "active_streak_sec": 12700.0,
                    "local_hour": 16,
                    "sampled_at": now[0],
                }
            },
        )
        ev2 = evaluator()
        self.assertEqual(len(ev2), 1)
        self.assertEqual(ev2[0].payload["label"], "long_session")

        # Away state never triggers even at late-night hours
        now[0] += 100.0
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "away",
                    "idle_sec": 600.0,
                    "active_streak_sec": 0.0,
                    "local_hour": 2,
                    "sampled_at": now[0],
                }
            },
        )
        self.assertEqual(evaluator(), [])

        # Active at 02:00 -> late_night fires with respect_quiet=False and suppresses duplicate long_session
        now[0] += 100.0
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 5.0,
                    "active_streak_sec": 12000.0,
                    "local_hour": 2,
                    "sampled_at": now[0],
                }
            },
        )
        ev3 = evaluator()
        self.assertEqual(len(ev3), 1)
        self.assertEqual(ev3[0].kind, EventKind.BUBBLE)
        self.assertEqual(ev3[0].payload["source"], "presence")
        self.assertEqual(ev3[0].payload["label"], "late_night")
        self.assertFalse(ev3[0].payload["respect_quiet"])

        # Late-night cooldown prevents spam on subsequent ticks
        now[0] += 600.0
        registry.heartbeat(
            "home_pc",
            {
                "presence": {
                    "state": "active",
                    "idle_sec": 5.0,
                    "active_streak_sec": 12600.0,
                    "local_hour": 2,
                    "sampled_at": now[0],
                }
            },
        )
        self.assertEqual(evaluator(), [])

    def test_edge_client_heartbeat_merges_presence_and_cloud_evaluator_consumes_it(self) -> None:
        from datetime import datetime, timezone
        from shinobu_core.cloud.presence import LocalPresenceTracker, PresenceEvaluator
        from shinobu_core.protocol.events import EventKind

        cloud = _MockCloudControlPlane(expected_token="cloud-secret")
        cloud_url = cloud.start()
        try:
            # 2026-06-01 18:00:00 UTC == 02:00:00 UTC+8 (late night)
            base_ts = datetime(2026, 6, 1, 18, 0, 0, tzinfo=timezone.utc).timestamp()
            tracker = LocalPresenceTracker(
                away_threshold_sec=300.0,
                idle_fn=lambda: 8.0,
                clock=lambda: base_ts,
                tz_offset_hours=8.0,
            )
            client = EdgeNodeClient(
                cloud_url,
                "cloud-secret",
                worker_id="home_pc",
                advertise_url="http://100.64.0.10:18792",
                node_token="edge-tok",
                meta={"host_tag": "desktop"},
                presence_enabled=True,
                presence_tracker=tracker,
            )
            client.register()
            client.send_heartbeat()

            rec = cloud.registry.get("home_pc")
            self.assertIsNotNone(rec)
            assert rec is not None
            self.assertEqual(rec.meta.get("host_tag"), "desktop")
            presence = rec.meta.get("presence")
            self.assertIsInstance(presence, dict)
            self.assertEqual(presence["state"], "active")
            self.assertEqual(presence["idle_sec"], 8.0)
            self.assertEqual(presence["local_hour"], 2)

            evaluator = PresenceEvaluator(cloud.registry)
            events = evaluator()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, EventKind.BUBBLE)
            self.assertEqual(events[0].payload["label"], "late_night")
            self.assertFalse(events[0].payload["respect_quiet"])

            # Disabled presence does not emit meta["presence"]
            client_off = EdgeNodeClient(
                cloud_url,
                "cloud-secret",
                worker_id="quiet_pc",
                advertise_url="http://100.64.0.11:18792",
                node_token="edge-tok",
                meta={"host_tag": "headless"},
                presence_enabled=False,
            )
            client_off.register()
            client_off.send_heartbeat()
            rec_off = cloud.registry.get("quiet_pc")
            self.assertIsNotNone(rec_off)
            assert rec_off is not None
            self.assertNotIn("presence", rec_off.meta)
            self.assertEqual(rec_off.meta.get("host_tag"), "headless")
        finally:
            cloud.stop()

    def test_hybrid_to_edge_node_reasoning_effort_passthrough(self) -> None:
        class _EffortCloudFallback(_FakeCloudFallbackEngine):
            def __init__(self) -> None:
                super().__init__()
                self.reasoning_effort: str = ""
                self.seen_efforts: list[str] = []

            def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
                self.seen_efforts.append(req.reasoning_effort)
                yield from super().stream_turn(req)

        with tempfile.TemporaryDirectory() as tmp:
            local_engine = _FakeLocalEngine()
            edge_server = EdgeNodeServer(
                local_engine,
                node_token="edge-eff-tok",
                allowed_roots=[Path(tmp)],
                port=0,
            )
            with edge_server:
                registry = WorkerRegistry()
                registry.register(
                    WorkerRecord(
                        worker_id="pc_eff",
                        role="primary",
                        base_url=edge_server.base_url,
                        capabilities=("agy_cli",),
                        token="edge-eff-tok",
                        ttl_sec=60.0,
                    )
                )
                fallback = _EffortCloudFallback()
                hybrid = HybridRouterEngine(
                    fallback,
                    registry,
                    primary_capability="agy_cli",
                )

                # 1. Engine-level default reasoning_effort flows to EdgeNodeServer when TurnRequest.reasoning_effort is empty.
                hybrid.reasoning_effort = "low"
                self.assertEqual(hybrid.reasoning_effort, "low")
                list(
                    hybrid.stream_turn(
                        TurnRequest(
                            episode_id="ep-hybrid-eff",
                            system_prefix="SYS",
                            user_text="greeting",
                        )
                    )
                )
                self.assertEqual(len(local_engine.turn_requests), 1)
                self.assertEqual(local_engine.turn_requests[0].reasoning_effort, "low")

                # 2. Single-turn TurnRequest.reasoning_effort overrides engine-level default without mutating it.
                list(
                    hybrid.stream_turn(
                        TurnRequest(
                            episode_id="ep-hybrid-eff",
                            system_prefix="SYS",
                            user_text="complex reasoning",
                            reasoning_effort="HIGH",
                        )
                    )
                )
                self.assertEqual(len(local_engine.turn_requests), 2)
                self.assertEqual(local_engine.turn_requests[1].reasoning_effort, "high")
                self.assertEqual(hybrid.reasoning_effort, "low")

                # 3. Direct EdgeNodeServer._build_turn_request alias normalization & empty default.
                built_med = EdgeNodeServer._build_turn_request({"user_text": "q", "reasoning_effort": "med"})
                self.assertEqual(built_med.reasoning_effort, "medium")
                built_empty = EdgeNodeServer._build_turn_request({"user_text": "q"})
                self.assertEqual(built_empty.reasoning_effort, "")


if __name__ == "__main__":
    unittest.main()
