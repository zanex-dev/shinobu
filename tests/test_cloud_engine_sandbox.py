# -*- coding: utf-8 -*-
"""Unit tests for HybridRouterEngine and CloudSandboxTerminal (T02)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_PUBLIC_ROOT = Path(__file__).resolve().parent.parent
if str(_PUBLIC_ROOT) not in sys.path:
    sys.path.insert(0, str(_PUBLIC_ROOT))

from shinobu_core.cloud.approval import ApprovalGate
from shinobu_core.cloud.hybrid_engine import HybridRouterEngine
from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.cloud.sandbox import CloudSandboxTerminal, SandboxPathError
from shinobu_core.protocol.engine import EngineAdapter, ToolSpec, TurnChunk, TurnRequest


class _FakeCloudEngine:
    """Minimal EngineAdapter stub representing the cloud flagship fallback API."""

    def __init__(self, model: str = "cloud-flagship-v1") -> None:
        self.name = "fake_cloud"
        self.model = model
        self.stream_calls: list[TurnRequest] = []
        self.json_calls: list[tuple[str, str]] = []

    def stream_turn(self, req: TurnRequest):
        self.stream_calls.append(req)
        yield TurnChunk(delta=f"cloud:{self.model}:{req.user_text}")
        yield TurnChunk(delta="", usage={"source": "cloud"}, done=True)

    def complete_json(self, system: str, prompt: str) -> dict[str, object]:
        self.json_calls.append((system, prompt))
        return {"source": "cloud", "model": self.model, "prompt": prompt}


class _MockEdgeNodeServer:
    """Lightweight local HTTP server simulating a remote /node/turn edge worker."""

    def __init__(self, handler_fn) -> None:
        self._handler_fn = handler_fn
        self.requests: list[dict[str, object]] = []
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                auth = self.headers.get("Authorization", "")
                parent.requests.append(
                    {
                        "path": self.path,
                        "auth": auth,
                        "payload": payload,
                    }
                )
                parent._handler_fn(self, payload, auth)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestHybridRouterEngine(unittest.TestCase):
    def test_protocol_and_model_property_passthrough(self) -> None:
        cloud = _FakeCloudEngine(model="flagship-alpha")
        registry = WorkerRegistry()
        engine = HybridRouterEngine(cloud, registry)

        self.assertIsInstance(engine, EngineAdapter)
        self.assertEqual(engine.name, "hybrid")
        self.assertEqual(engine.model, "flagship-alpha")

        engine.model = "flagship-beta"
        self.assertEqual(cloud.model, "flagship-beta")
        self.assertEqual(engine.model, "flagship-beta")

    def test_online_worker_stream_turn_sse_with_bearer_token(self) -> None:
        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            if auth != "Bearer edge-token-99":
                req_handler.send_response(401)
                req_handler.end_headers()
                return
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            req_handler.end_headers()
            events = [
                json.dumps({"delta": "edge-hello "}),
                json.dumps({"delta": "world", "usage": {"tokens": 7}}),
                "[DONE]",
            ]
            for ev in events:
                req_handler.wfile.write(f"data: {ev}\n\n".encode("utf-8"))
                req_handler.wfile.flush()

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine(model="flagship-v1")
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
                token="edge-token-99",
                ttl_sec=60.0,
            )
        )
        engine = HybridRouterEngine(cloud, registry, primary_capability="brain", remote_timeout_sec=5.0)
        tool = ToolSpec(
            name="ping",
            description="ping tool",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "pong",
        )
        req = TurnRequest(
            episode_id="ep-1",
            system_prefix="sys",
            history=[{"role": "user", "content": "prev"}],
            user_text="hi edge",
            tools=[tool],
        )

        chunks = list(engine.stream_turn(req))
        text = "".join(c.delta for c in chunks)
        self.assertEqual(text, "edge-hello world")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(len(cloud.stream_calls), 0)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["path"], "/node/turn")
        self.assertEqual(server.requests[0]["auth"], "Bearer edge-token-99")
        self.assertEqual(server.requests[0]["payload"]["mode"], "stream_turn")
        self.assertEqual(server.requests[0]["payload"]["model"], "flagship-v1")
        self.assertIsNotNone(engine.last_route)
        self.assertEqual(engine.last_route["target"], "remote")
        self.assertEqual(engine.last_route["worker_id"], "home_pc")

    def test_online_worker_json_turn_and_complete_json(self) -> None:
        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "application/json")
            req_handler.end_headers()
            if payload.get("mode") == "complete_json":
                body = {"result": {"source": "edge", "ok": True}}
            else:
                body = {
                    "delta": "edge-json-reply",
                    "tool_calls": [{"id": "tc1", "name": "cloud_exec", "arguments": {"argv": ["ls"]}}],
                    "usage": {"total_tokens": 12},
                    "done": True,
                }
            req_handler.wfile.write(json.dumps(body).encode("utf-8"))

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine()
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
                token="tok",
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=5.0)

        chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-2", system_prefix="", user_text="q")))
        self.assertEqual("".join(c.delta for c in chunks), "edge-json-reply")
        self.assertEqual(chunks[-1].tool_calls[0]["name"], "cloud_exec")
        self.assertEqual(chunks[-1].usage, {"total_tokens": 12})

        parsed = engine.complete_json("sys", "extract")
        self.assertEqual(parsed, {"source": "edge", "ok": True})
        self.assertEqual(len(cloud.stream_calls), 0)
        self.assertEqual(len(cloud.json_calls), 0)

    def test_offline_or_expired_worker_routes_to_cloud_fallback(self) -> None:
        now = [1000.0]
        cloud = _FakeCloudEngine(model="flagship-cloud")
        registry = WorkerRegistry(clock=lambda: now[0])
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url="http://127.0.0.1:1",
                capabilities=("brain",),
                ttl_sec=10.0,
                last_seen=1000.0,
            )
        )
        # Expire the worker beyond TTL.
        now[0] = 1020.0

        engine = HybridRouterEngine(cloud, registry)
        chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-3", system_prefix="", user_text="ping")))
        self.assertEqual("".join(c.delta for c in chunks), "cloud:flagship-cloud:ping")
        self.assertEqual(len(cloud.stream_calls), 1)
        self.assertEqual(engine.last_route["target"], "cloud_fallback")

        res = engine.complete_json("sys", "p1")
        self.assertEqual(res["source"], "cloud")
        self.assertEqual(len(cloud.json_calls), 1)

    def test_remote_http_error_and_pre_first_token_sse_error_fallback_seamlessly(self) -> None:
        mode = ["http503"]

        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            if mode[0] == "http503":
                req_handler.send_response(503)
                req_handler.send_header("Content-Type", "application/json")
                req_handler.end_headers()
                req_handler.wfile.write(b'{"error":"overloaded"}')
                return
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "text/event-stream")
            req_handler.end_headers()
            req_handler.wfile.write(b'data: {"error":"edge quota exhausted"}\n\n')
            req_handler.wfile.flush()

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine(model="cloud-pro")
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=5.0)

        # Case 1: HTTP 503 -> seamless cloud fallback for both stream_turn & complete_json.
        chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-4", system_prefix="", user_text="hello")))
        self.assertEqual("".join(c.delta for c in chunks), "cloud:cloud-pro:hello")
        self.assertEqual(engine.last_route["target"], "cloud_fallback")
        self.assertIn("503", engine.last_route["fallback_reason"])

        jres = engine.complete_json("sys", "json-q")
        self.assertEqual(jres["source"], "cloud")

        # Case 2: SSE error emitted before any token -> seamless cloud fallback.
        mode[0] = "sse_error"
        chunks2 = list(engine.stream_turn(TurnRequest(episode_id="ep-5", system_prefix="", user_text="again")))
        self.assertEqual("".join(c.delta for c in chunks2), "cloud:cloud-pro:again")
        self.assertEqual(len(cloud.stream_calls), 2)

    def test_remote_timeout_and_unreachable_fallback_seamlessly(self) -> None:
        release = threading.Event()

        def handle_slow(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            release.wait(timeout=2.0)
            try:
                req_handler.send_response(200)
                req_handler.end_headers()
            except OSError:
                pass

        server = _MockEdgeNodeServer(handle_slow)
        self.addCleanup(lambda: (release.set(), server.close()))

        cloud = _FakeCloudEngine(model="cloud-fast")
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="slow_node",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=0.15)
        chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-6", system_prefix="", user_text="slow")))
        self.assertEqual("".join(c.delta for c in chunks), "cloud:cloud-fast:slow")
        self.assertEqual(engine.last_route["target"], "cloud_fallback")
        release.set()

        # Unreachable port -> immediate fallback.
        dead_port = _unused_local_port()
        registry.unregister("slow_node")
        registry.register(
            WorkerRecord(
                worker_id="dead_node",
                role="edge_node",
                base_url=f"http://127.0.0.1:{dead_port}",
                capabilities=("brain",),
            )
        )
        chunks2 = list(engine.stream_turn(TurnRequest(episode_id="ep-7", system_prefix="", user_text="dead")))
        self.assertEqual("".join(c.delta for c in chunks2), "cloud:cloud-fast:dead")

    def test_connect_timeout_fast_fallback_without_waiting_remote_timeout(self) -> None:
        import socket
        from unittest import mock

        cloud = _FakeCloudEngine(model="cloud-fast")
        registry = WorkerRegistry()
        with self.assertRaises(ValueError):
            HybridRouterEngine(cloud, registry, connect_timeout_sec=0)

        registry.register(
            WorkerRecord(
                worker_id="blackhole_node",
                role="edge_node",
                base_url="http://100.64.0.99:9876",
                capabilities=("brain",),
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=90.0, connect_timeout_sec=0.25)
        with (
            mock.patch(
                "shinobu_core.cloud.hybrid_engine.socket.create_connection",
                side_effect=socket.timeout("blackhole"),
            ) as mock_probe,
            mock.patch("shinobu_core.cloud.hybrid_engine.urllib.request.urlopen") as mock_urlopen,
        ):
            chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-bh", system_prefix="", user_text="ping")))
        self.assertEqual("".join(c.delta for c in chunks), "cloud:cloud-fast:ping")
        self.assertEqual(engine.last_route["target"], "cloud_fallback")
        mock_probe.assert_called_once_with(("100.64.0.99", 9876), timeout=0.25)
        mock_urlopen.assert_not_called()

        # Default HTTPS port 443 and cap probe timeout by remote_timeout_sec
        registry.unregister("blackhole_node")
        registry.register(
            WorkerRecord(
                worker_id="https_blackhole",
                role="edge_node",
                base_url="https://edge.example.com",
                capabilities=("brain",),
            )
        )
        engine2 = HybridRouterEngine(cloud, registry, remote_timeout_sec=1.5, connect_timeout_sec=3.0)
        with mock.patch(
            "shinobu_core.cloud.hybrid_engine.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ) as mock_probe2:
            chunks2 = list(engine2.stream_turn(TurnRequest(episode_id="ep-bh2", system_prefix="", user_text="hi")))
        self.assertEqual("".join(c.delta for c in chunks2), "cloud:cloud-fast:hi")
        mock_probe2.assert_called_once_with(("edge.example.com", 443), timeout=1.5)

    def test_mid_stream_error_after_first_token_raises_instead_of_duplicating(self) -> None:
        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "text/event-stream")
            req_handler.end_headers()
            req_handler.wfile.write(b'data: {"delta":"first-part"}\n\n')
            req_handler.wfile.flush()
            req_handler.wfile.write(b'data: {"error":"stream reset"}\n\n')
            req_handler.wfile.flush()

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine()
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=5.0)
        it = engine.stream_turn(TurnRequest(episode_id="ep-8", system_prefix="", user_text="mid"))
        first = next(it)
        self.assertEqual(first.delta, "first-part")
        with self.assertRaises(RuntimeError):
            list(it)
        self.assertEqual(len(cloud.stream_calls), 0)

    def test_route_mode_hot_switch_aliases_and_cloud_mode_preserves_pc_worker(self) -> None:
        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "application/json")
            req_handler.end_headers()
            if payload.get("mode") == "complete_json":
                body = {"result": {"source": "edge", "ok": True}}
            else:
                body = {"delta": "edge-live", "done": True}
            req_handler.wfile.write(json.dumps(body).encode("utf-8"))

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine(model="cloud-pro")
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain", "pc_control"),
            )
        )

        with self.assertRaises(ValueError):
            HybridRouterEngine(cloud, registry, route_mode="unsupported")

        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=5.0)
        self.assertEqual(engine.route_mode, "auto")

        # Alias normalization & invalid setter leaves previous mode intact.
        engine.route_mode = "api"
        self.assertEqual(engine.route_mode, "cloud")
        with self.assertRaises(ValueError):
            engine.route_mode = "nope"
        self.assertEqual(engine.route_mode, "cloud")

        # In cloud mode: brain skips online home_pc worker, while pc_control stays online.
        chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-c1", system_prefix="", user_text="hi")))
        self.assertEqual("".join(c.delta for c in chunks), "cloud:cloud-pro:hi")
        self.assertEqual(len(server.requests), 0)
        self.assertEqual(len(cloud.stream_calls), 1)
        self.assertIsNotNone(engine.last_route)
        self.assertEqual(engine.last_route["route_mode"], "cloud")
        self.assertEqual(engine.last_route["target"], "cloud")
        self.assertIsInstance(engine.last_route["elapsed_ms"], int)
        self.assertGreaterEqual(engine.last_route["elapsed_ms"], 0)
        self.assertGreater(engine.last_route["ts"], 0)

        jres = engine.complete_json("sys", "q")
        self.assertEqual(jres["source"], "cloud")
        self.assertEqual(len(server.requests), 0)
        self.assertEqual(engine.last_route["route_mode"], "cloud")
        self.assertEqual(engine.last_route["target"], "cloud")

        pc_worker = registry.get_online("pc_control")
        self.assertIsNotNone(pc_worker)
        self.assertEqual(pc_worker.worker_id, "home_pc")

        # Hot-switch back to auto via 'hybrid' alias -> hits remote edge worker again.
        engine.route_mode = "hybrid"
        self.assertEqual(engine.route_mode, "auto")
        chunks2 = list(engine.stream_turn(TurnRequest(episode_id="ep-c2", system_prefix="", user_text="back")))
        self.assertEqual("".join(c.delta for c in chunks2), "edge-live")
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(engine.last_route["route_mode"], "auto")
        self.assertEqual(engine.last_route["target"], "remote")

    def test_local_mode_raises_without_cloud_fallback_and_emits_logs(self) -> None:
        fail_remote = [False]

        def handle(req_handler: BaseHTTPRequestHandler, payload: dict[str, object], auth: str) -> None:
            if fail_remote[0]:
                req_handler.send_response(503)
                req_handler.send_header("Content-Type", "application/json")
                req_handler.end_headers()
                req_handler.wfile.write(b'{"error":"busy"}')
                return
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "application/json")
            req_handler.end_headers()
            body = {"result": {"source": "edge"}} if payload.get("mode") == "complete_json" else {"delta": "edge-ok", "done": True}
            req_handler.wfile.write(json.dumps(body).encode("utf-8"))

        server = _MockEdgeNodeServer(handle)
        self.addCleanup(server.close)

        cloud = _FakeCloudEngine(model="cloud-pro")
        registry = WorkerRegistry()
        registry.register(
            WorkerRecord(
                worker_id="home_pc",
                role="edge_node",
                base_url=server.base_url,
                capabilities=("brain",),
            )
        )
        engine = HybridRouterEngine(cloud, registry, remote_timeout_sec=5.0)

        # Remote hit -> INFO log + elapsed_ms/ts recorded.
        with self.assertLogs("shinobu_core.cloud.hybrid_engine", level="INFO") as cm_ok:
            chunks = list(engine.stream_turn(TurnRequest(episode_id="ep-l1", system_prefix="", user_text="ok")))
        self.assertEqual("".join(c.delta for c in chunks), "edge-ok")
        self.assertTrue(any("INFO:" in line and "target=remote" in line for line in cm_ok.output))
        self.assertEqual(engine.last_route["route_mode"], "auto")
        self.assertGreaterEqual(engine.last_route["elapsed_ms"], 0)
        self.assertGreater(engine.last_route["ts"], 0)

        # Auto mode fallback on 503 -> WARNING log + falls back to cloud.
        fail_remote[0] = True
        with self.assertLogs("shinobu_core.cloud.hybrid_engine", level="WARNING") as cm_fb:
            chunks_fb = list(engine.stream_turn(TurnRequest(episode_id="ep-l2", system_prefix="", user_text="fb")))
        self.assertEqual("".join(c.delta for c in chunks_fb), "cloud:cloud-pro:fb")
        self.assertTrue(any("WARNING:" in line and "target=cloud_fallback" in line for line in cm_fb.output))
        self.assertEqual(len(cloud.stream_calls), 1)

        # Switch to local via 'agy' alias -> 503 raises RuntimeError without cloud fallback.
        engine.route_mode = "agy"
        self.assertEqual(engine.route_mode, "local")
        with self.assertLogs("shinobu_core.cloud.hybrid_engine", level="WARNING") as cm_loc:
            with self.assertRaises(RuntimeError):
                list(engine.stream_turn(TurnRequest(episode_id="ep-l3", system_prefix="", user_text="strict")))
        self.assertTrue(any("WARNING:" in line and "target=local_error" in line for line in cm_loc.output))
        self.assertEqual(engine.last_route["route_mode"], "local")
        self.assertEqual(engine.last_route["target"], "local_error")
        self.assertEqual(len(cloud.stream_calls), 1)

        with self.assertRaises(RuntimeError):
            engine.complete_json("sys", "strict-json")
        self.assertEqual(len(cloud.json_calls), 0)

        # Offline worker under 'edge' alias -> raises RuntimeError, never touches cloud.
        engine.route_mode = "edge"
        self.assertEqual(engine.route_mode, "local")
        registry.unregister("home_pc")
        with self.assertRaises(RuntimeError):
            list(engine.stream_turn(TurnRequest(episode_id="ep-l4", system_prefix="", user_text="offline")))
        with self.assertRaises(RuntimeError):
            engine.complete_json("sys", "offline-json")
        self.assertEqual(len(cloud.stream_calls), 1)
        self.assertEqual(len(cloud.json_calls), 0)


class TestCloudSandboxTerminal(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "sandbox_ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "sub").mkdir(exist_ok=True)
        (self.workspace / "notes.txt").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
        self.outside_file = self.root / "outside_secret.txt"
        self.outside_file.write_text("do-not-read", encoding="utf-8")

    def test_rejects_raw_string_argv_and_forces_shell_false(self) -> None:
        gate = ApprovalGate()
        sandbox = CloudSandboxTerminal(self.workspace, gate)

        with self.assertRaises(TypeError):
            sandbox.exec_command("whoami")  # type: ignore[arg-type]

        tools = {t.name: t for t in sandbox.build_tools()}
        raw_res = json.loads(tools["cloud_exec"].handler(argv="whoami"))
        self.assertFalse(raw_res["ok"])
        self.assertEqual(raw_res["status"], "invalid_args")

        fake_proc = mock.Mock(returncode=0, stdout="sandbox-user\n", stderr="")
        with mock.patch("shinobu_core.cloud.sandbox.subprocess.run", return_value=fake_proc) as run_mock:
            res = sandbox.exec_command(["whoami"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["tier"], "L0")
        self.assertFalse(run_mock.call_args.kwargs["shell"])

    def test_path_escape_blocked_for_cwd_argv_and_read_file(self) -> None:
        events = []
        gate = ApprovalGate(on_event=events.append)
        sandbox = CloudSandboxTerminal(self.workspace, gate)

        # Direct resolve_path checks
        with self.assertRaises(SandboxPathError):
            sandbox.resolve_path("..")
        with self.assertRaises(SandboxPathError):
            sandbox.resolve_path(str(self.outside_file))
        with self.assertRaises(SandboxPathError):
            sandbox.resolve_path("~/secret")

        # cwd escape
        res_cwd = sandbox.exec_command(["whoami"], cwd="..")
        self.assertFalse(res_cwd["ok"])
        self.assertEqual(res_cwd["status"], "path_denied")

        res_abs_cwd = sandbox.exec_command(["whoami"], cwd=str(self.root))
        self.assertFalse(res_abs_cwd["ok"])
        self.assertEqual(res_abs_cwd["status"], "path_denied")

        # argv traversal escape (L0 and L1)
        res_arg_l0 = sandbox.exec_command(["cat", "../outside_secret.txt"])
        self.assertFalse(res_arg_l0["ok"])
        self.assertEqual(res_arg_l0["status"], "path_denied")

        res_arg_l1 = sandbox.exec_command([sys.executable, "../outside_secret.txt"])
        self.assertFalse(res_arg_l1["ok"])
        self.assertEqual(res_arg_l1["status"], "path_denied")

        # read_file escape
        res_read = sandbox.read_file("../outside_secret.txt")
        self.assertFalse(res_read["ok"])
        self.assertEqual(res_read["status"], "path_denied")

        res_read_abs = sandbox.read_file(str(self.outside_file))
        self.assertFalse(res_read_abs["ok"])
        self.assertEqual(res_read_abs["status"], "path_denied")

        # None of the blocked escapes should ever trigger an approval request.
        self.assertEqual(len(events), 0)

    def test_l0_readonly_runs_without_approval(self) -> None:
        events = []
        gate = ApprovalGate(on_event=events.append)
        sandbox = CloudSandboxTerminal(self.workspace, gate)

        res = sandbox.exec_command(["whoami"], cwd="sub")
        self.assertTrue(res["ok"])
        self.assertEqual(res["tier"], "L0")
        self.assertEqual(res["returncode"], 0)
        self.assertTrue(res["stdout"].strip())
        self.assertEqual(len(events), 0)

    def test_l1_approval_approve_deny_and_timeout(self) -> None:
        # 1. Approved L1 execution
        def approve_immediately(event) -> None:
            if event.payload.get("phase") == "approval_requested":
                gate_approve.decide(event.payload["approval_id"], True, actor="owner", reason="ok")

        gate_approve = ApprovalGate(on_event=approve_immediately)
        sandbox_ok = CloudSandboxTerminal(self.workspace, gate_approve)
        res_ok = sandbox_ok.exec_command([sys.executable, "-c", "print('hello-l1')"])
        self.assertTrue(res_ok["ok"])
        self.assertEqual(res_ok["tier"], "L1")
        self.assertIn("hello-l1", res_ok["stdout"])
        self.assertTrue(res_ok.get("approval_id", "").startswith("apr_"))
        self.assertEqual(res_ok.get("actor"), "owner")

        # 2. Denied L1 execution must NOT run the command
        sentinel = self.workspace / "should_not_exist.txt"

        def deny_immediately(event) -> None:
            if event.payload.get("phase") == "approval_requested":
                gate_deny.decide(event.payload["approval_id"], False, actor="owner", reason="rejected")

        gate_deny = ApprovalGate(on_event=deny_immediately)
        sandbox_deny = CloudSandboxTerminal(self.workspace, gate_deny)
        res_deny = sandbox_deny.exec_command(
            [sys.executable, "-c", "open('should_not_exist.txt', 'w').write('x')"]
        )
        self.assertFalse(res_deny["ok"])
        self.assertEqual(res_deny["status"], "denied")
        self.assertEqual(res_deny["tier"], "L1")
        self.assertFalse(sentinel.exists())

        # 3. Timed-out L1 approval must auto-deny and NOT run
        gate_timeout = ApprovalGate()
        sandbox_to = CloudSandboxTerminal(self.workspace, gate_timeout, approval_timeout_sec=0.05)
        res_to = sandbox_to.exec_command(
            [sys.executable, "-c", "open('should_not_exist.txt', 'w').write('x')"]
        )
        self.assertFalse(res_to["ok"])
        self.assertEqual(res_to["status"], "timeout")
        self.assertEqual(res_to["tier"], "L1")
        self.assertFalse(sentinel.exists())

    def test_l2_forbidden_blocked_unconditionally_without_approval(self) -> None:
        events = []
        gate = ApprovalGate(on_event=events.append)
        sandbox = CloudSandboxTerminal(self.workspace, gate)

        for cmd in (["sudo", "id"], ["rm", "-rf", "/"], ["cat", ".allow_media"]):
            res = sandbox.exec_command(cmd)
            self.assertFalse(res["ok"], f"expected L2 block for {cmd}")
            self.assertEqual(res["status"], "forbidden")
            self.assertEqual(res["tier"], "L2")
        self.assertEqual(len(events), 0)

    def test_truncation_timeout_and_tool_specs(self) -> None:
        def auto_approve(event) -> None:
            if event.payload.get("phase") == "approval_requested":
                gate.decide(event.payload["approval_id"], True, actor="owner")

        gate = ApprovalGate(on_event=auto_approve)
        sandbox = CloudSandboxTerminal(self.workspace, gate, timeout_sec=0.25, max_output_chars=100)

        # Output truncation
        res_big = sandbox.exec_command([sys.executable, "-c", "print('A' * 500)"])
        self.assertTrue(res_big["ok"])
        self.assertTrue(res_big["truncated"])
        self.assertLessEqual(len(res_big["stdout"]) + len(res_big["stderr"]), 100)

        # Command execution timeout
        res_slow = sandbox.exec_command([sys.executable, "-c", "import time; time.sleep(2.0)"])
        self.assertFalse(res_slow["ok"])
        self.assertEqual(res_slow["status"], "timeout")

        # ToolSpec exposure and execution
        tools = sandbox.build_tools()
        tool_map = {t.name: t for t in tools}
        self.assertEqual(set(tool_map.keys()), {"cloud_exec", "cloud_read_file"})
        self.assertEqual(tool_map["cloud_exec"].to_schema()["name"], "cloud_exec")
        self.assertEqual(tool_map["cloud_read_file"].to_schema()["name"], "cloud_read_file")

        read_payload = json.loads(
            tool_map["cloud_read_file"].handler(path="notes.txt", start_line=2, max_lines=2)
        )
        self.assertTrue(read_payload["ok"])
        self.assertEqual(read_payload["content"], "line2\nline3")
        self.assertEqual(read_payload["start_line"], 2)
        self.assertEqual(read_payload["end_line"], 3)
        self.assertTrue(read_payload["truncated"])

        # Protected privacy marker filename inside sandbox is also refused
        (self.workspace / ".allow_media").write_text("nope", encoding="utf-8")
        priv_payload = json.loads(tool_map["cloud_read_file"].handler(path=".allow_media"))
        self.assertFalse(priv_payload["ok"])
        self.assertEqual(priv_payload["status"], "forbidden")


if __name__ == "__main__":
    unittest.main()
