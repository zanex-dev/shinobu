# -*- coding: utf-8 -*-
"""Comprehensive pure-stdlib tests for bidirectional MCP bridge (`shinobu_core.mcp`)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from urllib.parse import parse_qs, urlparse

SHINOBU_PUBLIC_DIR = Path(__file__).resolve().parents[1]
if str(SHINOBU_PUBLIC_DIR) not in sys.path:
    sys.path.insert(0, str(SHINOBU_PUBLIC_DIR))

from shinobu_core.core.kernel import ShinobuKernel
from shinobu_core.mcp import (
    MCP_CONFIG_FILENAME,
    MCP_PROTOCOL_VERSION,
    McpClientManager,
    McpStdioClient,
    McpTimeoutError,
    OUTBOUND_TOOLS,
    run_mcp_stdio_server,
)
from shinobu_core.protocol.events import Event, EventKind


FAKE_MCP_SERVER_SCRIPT = r"""# -*- coding: utf-8 -*-
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "good"

if mode == "crash":
    sys.stderr.write("intentional startup crash\n")
    sys.stderr.flush()
    sys.exit(2)

if mode == "hang_init":
    # Read stdin but never respond to initialize.
    for _ in sys.stdin:
        time.sleep(5)
    sys.exit(0)

# Emit a non-JSON diagnostic line first in 'noisy' mode to verify framing resilience.
if mode == "good":
    sys.stdout.write("non-json boot banner should be ignored\n")
    sys.stdout.flush()

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "notifications/initialized" or msg_id is None:
        continue

    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "fake-mcp",
                    "version": "1.0.0",
                    "env_tag": os.environ.get("FAKE_MCP_TAG", ""),
                },
            },
        }
    elif method == "ping":
        resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo back input text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "slow_op",
                        "description": "Sleep before responding.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"delay": {"type": "number"}},
                        },
                    },
                ]
            },
        }
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            tag = os.environ.get("FAKE_MCP_TAG", "")
            out = f"echo:{text}" + (f"[{tag}]" if tag else "")
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": out}],
                    "isError": False,
                },
            }
        elif name == "slow_op":
            delay = float(args.get("delay", 2.0))
            time.sleep(delay)
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "done"}],
                    "isError": False,
                },
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"unknown tool: {name}"},
            }
    else:
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()
"""


class _MockDaemonClient:
    """In-memory mock implementing the 5-method outbound backend contract."""

    def __init__(self, *, fail_next: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next = fail_next
        self.state = {"state": "idle", "mood": "calm"}

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("mock daemon offline")

    def bubble(
        self,
        text: str,
        *,
        mood: str | None = None,
        duration_sec: float | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(
            ("bubble", {"text": text, "mood": mood, "duration_sec": duration_sec})
        )
        return {"ok": True, "text": text, "mood": mood, "duration_sec": duration_sec}

    def get_state(self) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("get_state", {}))
        return dict(self.state)

    def set_state(
        self,
        state: str | None = None,
        mood: str | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(
            ("set_state", {"state": state, "mood": mood, "reason": reason})
        )
        if state is not None:
            self.state["state"] = state
        if mood is not None:
            self.state["mood"] = mood
        return {"ok": True, **self.state, "reason": reason}

    def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("recall", {"query": query, "limit": limit}))
        return {
            "query": query,
            "results": [{"id": "ep-1", "snippet": f"remembered {query}"}],
        }

    def chat(self, text: str) -> Any:
        self._maybe_fail()
        self.calls.append(("chat", {"text": text}))
        if text == "__events_mode__":
            return [
                Event(kind=EventKind.TOKEN_DELTA, payload={"delta": "你"}, episode_id="ep-ev"),
                Event(kind=EventKind.TOKEN_DELTA, payload={"delta": "好"}, episode_id="ep-ev"),
                Event(kind=EventKind.DONE, payload={"text": "你好，吾乃忍野忍"}, episode_id="ep-ev"),
            ]
        return {"episode_id": "ep-42", "text": f"reply:{text}", "events": []}


class TestInboundMcpClient(unittest.TestCase):
    """Real-subprocess tests for `McpClientManager` and `McpStdioClient`."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.server_script = self.home / "fake_mcp_server.py"
        self.server_script.write_text(FAKE_MCP_SERVER_SCRIPT, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_config(self, payload: dict[str, Any]) -> None:
        (self.home / MCP_CONFIG_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_inbound_connect_register_call_isolation_and_cleanup(self) -> None:
        self._write_config(
            {
                "mcpServers": {
                    "good": {
                        "command": sys.executable,
                        "args": ["-u", str(self.server_script), "good"],
                        "env": {"FAKE_MCP_TAG": "ok-env"},
                        "timeout_sec": 3.0,
                    },
                    "disabled_srv": {
                        "command": sys.executable,
                        "args": ["-u", str(self.server_script), "good"],
                        "disabled": True,
                    },
                    "crashed": {
                        "command": sys.executable,
                        "args": ["-u", str(self.server_script), "crash"],
                        "timeout_sec": 2.0,
                    },
                    "hung": {
                        "command": sys.executable,
                        "args": ["-u", str(self.server_script), "hang_init"],
                        "timeout_sec": 0.35,
                    },
                    "missing_cmd": {
                        "command": "definitely_nonexistent_shinobu_mcp_bin_xyz",
                        "timeout_sec": 1.0,
                    },
                }
            }
        )

        manager = McpClientManager(self.home)
        try:
            configs = manager.load_config()
            self.assertEqual(len(configs), 5)

            statuses = manager.connect_all()
            by_name = {s.name: s for s in statuses}

            self.assertTrue(by_name["good"].connected)
            self.assertEqual(by_name["good"].protocol_version, MCP_PROTOCOL_VERSION)
            self.assertEqual(by_name["good"].server_info.get("env_tag"), "ok-env")
            self.assertIn("echo", by_name["good"].tools)

            self.assertFalse(by_name["disabled_srv"].connected)
            self.assertTrue(by_name["disabled_srv"].disabled)

            self.assertFalse(by_name["crashed"].connected)
            self.assertIsNotNone(by_name["crashed"].error)

            self.assertFalse(by_name["hung"].connected)
            self.assertIn("timed out", (by_name["hung"].error or "").lower())

            self.assertFalse(by_name["missing_cmd"].connected)
            self.assertIn("not found", (by_name["missing_cmd"].error or "").lower())

            # Register onto a real ShinobuKernel and invoke tool with Unicode.
            kernel = ShinobuKernel(home=self.home)
            registered = manager.register_onto(kernel)
            names = {spec.name for spec in registered}
            self.assertEqual(names, {"mcp_good_echo", "mcp_good_slow_op"})

            echo_spec = kernel.get_tool("mcp_good_echo")
            self.assertIsNotNone(echo_spec)
            assert echo_spec is not None
            schema = echo_spec.to_schema()
            self.assertEqual(schema["name"], "mcp_good_echo")
            self.assertIn("properties", schema["parameters"])

            output = echo_spec.handler(text="甜甜圈测试")
            self.assertEqual(output, "echo:甜甜圈测试[ok-env]")

            # Verify direct timeout handling on slow_op and fault-isolated handler fallback.
            good_client = manager.get_client("good")
            self.assertIsNotNone(good_client)
            assert good_client is not None
            proc = good_client._proc
            self.assertIsNotNone(proc)
            assert proc is not None
            self.assertIsNone(proc.poll())

            with self.assertRaises(McpTimeoutError):
                good_client.call_tool("slow_op", {"delay": 1.5}, timeout=0.25)

            # Confirm the subprocess is cleanly reaped on close().
            manager.close()
            self.assertIsNotNone(proc.poll())
            self.assertFalse(good_client.is_alive)
        finally:
            manager.close()

    def test_tool_handler_isolates_runtime_timeout(self) -> None:
        self._write_config(
            {
                "mcpServers": {
                    "fast_timeout": {
                        "command": sys.executable,
                        "args": ["-u", str(self.server_script), "good"],
                        "timeout_sec": 0.4,
                    }
                }
            }
        )
        with McpClientManager(self.home) as manager:
            specs = {s.name: s for s in manager.build_tool_specs()}
            self.assertIn("mcp_fast_timeout_slow_op", specs)
            err_text = specs["mcp_fast_timeout_slow_op"].handler(delay=1.5)
            self.assertIn("error", err_text.lower())


class TestOutboundMcpServer(unittest.TestCase):
    """Tests for `run_mcp_stdio_server` with mock client and real loopback end-to-end."""

    def _run_session(
        self,
        messages: list[str | dict[str, Any]],
        client: Any,
        *,
        binary: bool = False,
    ) -> list[dict[str, Any]]:
        lines = [
            msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
            for msg in messages
        ]
        raw_input = "\n".join(lines) + "\n"
        if binary:
            stdin = io.BytesIO(raw_input.encode("utf-8"))
            stdout = io.BytesIO()
        else:
            stdin = io.StringIO(raw_input)
            stdout = io.StringIO()

        rc = run_mcp_stdio_server(stdin=stdin, stdout=stdout, client=client)
        self.assertEqual(rc, 0)

        out_text = (
            stdout.getvalue().decode("utf-8")
            if binary
            else stdout.getvalue()
        )
        return [json.loads(line) for line in out_text.splitlines() if line.strip()]

    def test_outbound_protocol_and_all_four_tools(self) -> None:
        mock = _MockDaemonClient()
        responses = self._run_session(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_bubble",
                        "arguments": {"text": "早安", "mood": "happy", "duration_sec": 5},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "shinobu_state", "arguments": {}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_state",
                        "arguments": {"state": "working", "mood": "focused", "reason": "test"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_recall",
                        "arguments": {"query": "donut", "limit": 5},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_chat",
                        "arguments": {"text": "你好"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_chat",
                        "arguments": {"text": "__events_mode__"},
                    },
                },
            ],
            client=mock,
            binary=True,
        )

        # notifications/initialized must produce no response -> 9 total responses.
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5, 6, 7, 8, 9])

        # initialize
        self.assertEqual(responses[0]["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "shinobu")

        # ping
        self.assertEqual(responses[1]["result"], {})

        # tools/list
        listed_names = [t["name"] for t in responses[2]["result"]["tools"]]
        self.assertEqual(listed_names, [t["name"] for t in OUTBOUND_TOOLS])
        self.assertEqual(
            listed_names,
            [
                "shinobu_bubble",
                "shinobu_state",
                "shinobu_recall",
                "shinobu_chat",
                "shinobu_remember",
            ],
        )

        # shinobu_bubble
        bubble_payload = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertTrue(bubble_payload["ok"])
        self.assertEqual(bubble_payload["text"], "早安")
        self.assertEqual(bubble_payload["mood"], "happy")

        # shinobu_state (read)
        state_read = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(state_read, {"state": "idle", "mood": "calm"})

        # shinobu_state (write)
        state_write = json.loads(responses[5]["result"]["content"][0]["text"])
        self.assertEqual(state_write["state"], "working")
        self.assertEqual(state_write["mood"], "focused")

        # shinobu_recall
        recall_payload = json.loads(responses[6]["result"]["content"][0]["text"])
        self.assertEqual(recall_payload["query"], "donut")
        self.assertEqual(len(recall_payload["results"]), 1)

        # shinobu_chat (dict)
        chat_payload = json.loads(responses[7]["result"]["content"][0]["text"])
        self.assertEqual(chat_payload["episode_id"], "ep-42")
        self.assertEqual(chat_payload["text"], "reply:你好")

        # shinobu_chat (Event list aggregation)
        chat_ev_payload = json.loads(responses[8]["result"]["content"][0]["text"])
        self.assertEqual(chat_ev_payload["episode_id"], "ep-ev")
        self.assertEqual(chat_ev_payload["text"], "你好，吾乃忍野忍")

    def test_outbound_shinobu_remember(self) -> None:
        class _RememberMockClient(_MockDaemonClient):
            def __init__(self) -> None:
                super().__init__()
                self.remember_calls: list[tuple[str, str, float]] = []

            def remember(
                self,
                key: str,
                value: str,
                *,
                confidence: float = 0.8,
            ) -> dict[str, Any]:
                self.remember_calls.append((key, value, confidence))
                return {
                    "status": "saved",
                    "fact": {
                        "key": key,
                        "value": value,
                        "confidence": confidence,
                    },
                }

        mock_client = _RememberMockClient()
        responses = self._run_session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_remember",
                        "arguments": {"key": "user.snack", "value": "donut"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_remember",
                        "arguments": {
                            "key": "user.lang",
                            "value": "zh-CN",
                            "confidence": 0.95,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_remember",
                        "arguments": {"key": "bad.conf", "value": "x", "confidence": 1.5},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "shinobu_remember",
                        "arguments": {"key": "", "value": "x"},
                    },
                },
            ],
            client=mock_client,
        )
        self.assertFalse(responses[0]["result"]["isError"])
        p0 = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(p0["status"], "saved")
        self.assertEqual(p0["fact"]["confidence"], 0.8)

        self.assertFalse(responses[1]["result"]["isError"])
        p1 = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(p1["fact"]["key"], "user.lang")
        self.assertEqual(p1["fact"]["confidence"], 0.95)
        self.assertEqual(
            mock_client.remember_calls,
            [("user.snack", "donut", 0.8), ("user.lang", "zh-CN", 0.95)],
        )

        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertEqual(responses[3]["error"]["code"], -32602)

        # Verify LoopbackDaemonBackend.remember sends POST /api/memory/facts
        from shinobu_core.mcp.server import LoopbackDaemonBackend

        backend = LoopbackDaemonBackend(url="http://127.0.0.1:9999", token="tok")
        captured: list[tuple[str, str, dict[str, Any] | None]] = []

        def fake_req(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            captured.append((method, path, body))
            return {"status": "saved", "fact": body}

        backend._request_json = fake_req  # type: ignore[method-assign]
        res = backend.remember(" user.pref ", " tea ", confidence=0.9)
        self.assertEqual(res["status"], "saved")
        self.assertEqual(
            captured,
            [
                (
                    "POST",
                    "/api/memory/facts",
                    {"key": "user.pref", "value": "tea", "confidence": 0.9},
                )
            ],
        )

    def test_outbound_errors_and_backend_fault_isolation(self) -> None:
        mock = _MockDaemonClient(fail_next=True)
        responses = self._run_session(
            [
                "{broken-json",
                {"jsonrpc": "2.0", "id": 10, "method": "nonexistent/method"},
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "unknown_tool", "arguments": {}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {"name": "shinobu_state", "arguments": {}},
                },
            ],
            client=mock,
        )

        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["error"]["code"], -32601)
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertIn("mock daemon offline", responses[3]["result"]["content"][0]["text"])

    def test_bidirectional_end_to_end_inbound_to_outbound_to_loopback_daemon(self) -> None:
        """Full cross-process loop: McpClientManager -> run_mcp_stdio_server -> Loopback HTTP daemon."""
        expected_token = "tok-shinobu-e2e"
        state_box: dict[str, Any] = {"state": "idle", "mood": "calm"}
        received_bubbles: list[dict[str, Any]] = []

        class _FakeDaemonHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, code: int, data: dict[str, Any]) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _check_auth(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {expected_token}":
                    self._send_json(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self) -> None:
                if not self._check_auth():
                    return
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send_json(200, {"status": "ok"})
                elif parsed.path == "/state":
                    self._send_json(200, dict(state_box))
                elif parsed.path == "/api/memory/search":
                    qs = parse_qs(parsed.query)
                    q = qs.get("q", [""])[0]
                    limit = int(qs.get("limit", ["10"])[0])
                    self._send_json(
                        200,
                        {
                            "query": q,
                            "results": [{"text": f"hit:{q}", "limit": limit}],
                        },
                    )
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._check_auth():
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw)
                if self.path == "/api/bubble":
                    received_bubbles.append(body)
                    self._send_json(200, {"ok": True, "bubble": body})
                elif self.path == "/api/state":
                    if "state" in body:
                        state_box["state"] = body["state"]
                    if "mood" in body:
                        state_box["mood"] = body["mood"]
                    self._send_json(200, {"ok": True, **state_box})
                elif self.path == "/chat":
                    self.assertFalse(body.get("stream", True))
                    self._send_json(
                        200,
                        {
                            "episode_id": "ep-e2e",
                            "text": f"daemon:{body['text']}",
                            "events": [],
                        },
                    )
                else:
                    self._send_json(404, {"error": "not found"})

            def assertFalse(self, val: Any) -> None:
                if val:
                    raise AssertionError("expected stream=False")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDaemonHandler)
        port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            runtime_dir = home / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "endpoint.json").write_text(
                json.dumps(
                    {"url": f"http://127.0.0.1:{port}", "token": expected_token},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # Configure inbound McpClientManager to launch our own outbound MCP server!
            entry_code = (
                "import sys; "
                "from shinobu_core.mcp.server import run_mcp_stdio_server; "
                "raise SystemExit(run_mcp_stdio_server(home=sys.argv[1]))"
            )
            existing_pp = os.environ.get("PYTHONPATH", "")
            py_path = (
                f"{SHINOBU_PUBLIC_DIR}{os.pathsep}{existing_pp}"
                if existing_pp
                else str(SHINOBU_PUBLIC_DIR)
            )
            (home / MCP_CONFIG_FILENAME).write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "self_shinobu": {
                                "command": sys.executable,
                                "args": ["-u", "-c", entry_code, str(home)],
                                "env": {"PYTHONPATH": py_path},
                                "timeout_sec": 5.0,
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            manager = McpClientManager(home)
            try:
                statuses = manager.connect_all()
                self.assertEqual(len(statuses), 1)
                self.assertTrue(statuses[0].connected, statuses[0].error)

                specs = {s.name: s for s in manager.build_tool_specs()}
                self.assertEqual(
                    set(specs.keys()),
                    {
                        "mcp_self_shinobu_shinobu_bubble",
                        "mcp_self_shinobu_shinobu_state",
                        "mcp_self_shinobu_shinobu_recall",
                        "mcp_self_shinobu_shinobu_chat",
                        "mcp_self_shinobu_shinobu_remember",
                    },
                )

                bubble_res = json.loads(
                    specs["mcp_self_shinobu_shinobu_bubble"].handler(
                        text="跨进程气泡", mood="excited", duration_sec=3.5
                    )
                )
                self.assertTrue(bubble_res["ok"])
                self.assertEqual(received_bubbles[0]["text"], "跨进程气泡")

                state_res = json.loads(
                    specs["mcp_self_shinobu_shinobu_state"].handler(
                        state="speaking", mood="happy"
                    )
                )
                self.assertEqual(state_res["state"], "speaking")
                self.assertEqual(state_box["mood"], "happy")

                recall_res = json.loads(
                    specs["mcp_self_shinobu_shinobu_recall"].handler(
                        query="甜甜圈", limit=3
                    )
                )
                self.assertEqual(recall_res["results"][0]["text"], "hit:甜甜圈")
                self.assertEqual(recall_res["results"][0]["limit"], 3)

                chat_res = json.loads(
                    specs["mcp_self_shinobu_shinobu_chat"].handler(text="在吗")
                )
                self.assertEqual(chat_res["episode_id"], "ep-e2e")
                self.assertEqual(chat_res["text"], "daemon:在吗")

                client = manager.get_client("self_shinobu")
                assert client is not None and client._proc is not None
                proc = client._proc
                manager.close()
                self.assertIsNotNone(proc.poll())
            finally:
                manager.close()
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
