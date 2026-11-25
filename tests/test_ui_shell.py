# -*- coding: utf-8 -*-
"""Headless unit & integration tests for the loopback SSE client and lazy PySide6 pet shell."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import queue
import socket
import ssl
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Iterator
import unittest
from unittest import mock

import shinobu_core
from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.runtime.state_machine import PetStateMachine
from shinobu_core.server.daemon import DaemonServer
from shinobu_core.ui.client import (
    DaemonAuthError,
    DaemonClient,
    DaemonClientError,
    EventStreamWorker,
    discover_endpoint,
    validate_daemon_url,
    validate_loopback_url,
)
from shinobu_core.ui.pet_window import (
    MAX_BUBBLE_DURATION_SEC,
    MIN_BUBBLE_DURATION_SEC,
    BubbleQueue,
    PetShellController,
    PetShellWindow,
    PySide6NotInstalledError,
    clamp_bubble_duration,
    clamp_position,
    load_pet_position,
    load_ui_prefs,
    require_pyside6,
    run_pet_shell,
    save_pet_position,
    save_ui_prefs,
    ui_prefs_path,
)


@dataclass
class _StubChunk:
    delta: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    done: bool = False


class _NeutralStubEngine:
    """Zero-network neutral engine stub for testing real DaemonServer round-trips."""

    name = "stub-ui-shell-engine"

    def __init__(self, deltas: list[str] | None = None) -> None:
        self.deltas = deltas or ["Hello ", "from ", "local daemon!"]
        self.call_count = 0

    def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return {
            "summary": "Tested desktop pet shell loopback client.",
            "journal_entry": "Completed a clean loopback UI integration check.",
            "journal": "Completed a clean loopback UI integration check.",
            "facts": [{"key": "ui_mode", "value": "desktop_shell"}],
            "timemap": [],
            "memory_append": ["Verified desktop shell client."],
            "memory_updates": ["Verified desktop shell client."],
        }

    def stream_turn(self, req: Any) -> Iterator[_StubChunk]:
        self.call_count += 1
        for idx, piece in enumerate(self.deltas):
            yield _StubChunk(delta=piece, done=(idx == len(self.deltas) - 1))


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestDaemonClientIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "companion_home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.home / "memory.sqlite")
        self.engine = _NeutralStubEngine()
        self.orch = Orchestrator(home=self.home, store=self.store, engine=self.engine)
        self.sm = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_validate_loopback_url_rejects_external_hosts(self) -> None:
        self.assertEqual(
            validate_loopback_url("http://127.0.0.1:8765/"),
            "http://127.0.0.1:8765",
        )
        for bad_url in (
            "",
            "https://127.0.0.1:8765",
            "http://0.0.0.0:8765",
            "http://192.168.1.10:8765",
            "http://example.com:80",
            "http://127.0.0.1",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject {bad_url!r}"):
                validate_loopback_url(bad_url)

    def test_validate_daemon_url_local_and_remote_modes(self) -> None:
        # Default (allow_remote=False) delegates strictly to validate_loopback_url.
        self.assertEqual(
            validate_daemon_url("http://127.0.0.1:8765/"),
            "http://127.0.0.1:8765",
        )
        with self.assertRaises(ValueError):
            validate_daemon_url("https://shinobu.example.com")

        # allow_remote=True permits loopback http:// and remote https:// (defaulting to 443).
        self.assertEqual(
            validate_daemon_url("http://127.0.0.1:8765/", allow_remote=True),
            "http://127.0.0.1:8765",
        )
        self.assertEqual(
            validate_daemon_url("https://shinobu.example.com/", allow_remote=True),
            "https://shinobu.example.com",
        )
        self.assertEqual(
            validate_daemon_url("https://shinobu.example.com:8443/api/", allow_remote=True),
            "https://shinobu.example.com:8443/api",
        )
        self.assertEqual(
            validate_daemon_url("https://127.0.0.1:8443", allow_remote=True),
            "https://127.0.0.1:8443",
        )

        # Plaintext http:// to any non-loopback host or invalid URLs must still be rejected.
        for bad_url in (
            "",
            "http://example.com:80",
            "http://192.168.1.10:8765",
            "http://0.0.0.0:8765",
            "http://127.0.0.1",
            "ftp://shinobu.example.com",
            "https://",
            "https://shinobu.example.com:0",
            "https://shinobu.example.com:99999",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject {bad_url!r}"):
                validate_daemon_url(bad_url, allow_remote=True)

    def test_remote_https_client_chat_stream_and_sse_tls(self) -> None:
        client = DaemonClient(
            "https://shinobu.example.com/",
            "cloud-token",
            allow_remote=True,
        )
        self.assertTrue(client._is_https)
        self.assertEqual(client._scheme, "https")
        self.assertEqual(client._host, "shinobu.example.com")
        self.assertEqual(client._port, 443)
        self.assertIsNotNone(client._ssl_ctx)
        self.assertEqual(client._http_host_header(), "shinobu.example.com")

        custom_port_client = DaemonClient(
            "https://shinobu.example.com:8443",
            "cloud-token",
            allow_remote=True,
        )
        self.assertEqual(custom_port_client._port, 8443)
        self.assertEqual(custom_port_client._http_host_header(), "shinobu.example.com:8443")

        # chat_stream uses HTTPSConnection with client._ssl_ctx
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        sse_lines = [
            b'event: token_delta\n',
            b'data: {"delta": "Hi TLS"}\n',
            b'\n',
            b'event: done\n',
            b'data: {"text": "Hi TLS"}\n',
            b'\n',
            b'',
        ]
        fake_resp.readline.side_effect = sse_lines
        fake_conn = mock.MagicMock()
        fake_conn.getresponse.return_value = fake_resp

        with mock.patch.object(http.client, "HTTPSConnection", return_value=fake_conn) as https_cls:
            events = list(client.chat_stream("hello cloud"))
        https_cls.assert_called_once_with(
            "shinobu.example.com",
            443,
            timeout=client.stream_timeout,
            context=client._ssl_ctx,
        )
        self.assertEqual([ev.kind for ev in events], [EventKind.TOKEN_DELTA, EventKind.DONE])

        # _open_sse_socket wraps TCP socket with TLS SNI and omits :443 in Host header
        raw_sock = mock.MagicMock()
        tls_sock = mock.MagicMock()
        sent_chunks: list[bytes] = []
        tls_sock.sendall.side_effect = lambda data: sent_chunks.append(bytes(data))
        tls_sock.recv.side_effect = [
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n: keepalive\n",
        ]
        fake_ctx = mock.MagicMock()
        fake_ctx.wrap_socket.return_value = tls_sock
        client._ssl_ctx = fake_ctx

        with mock.patch.object(socket, "create_connection", return_value=raw_sock) as conn_mock:
            out_sock, leftover, is_chunked = client._open_sse_socket(connect_timeout=2.0)

        conn_mock.assert_called_once_with(("shinobu.example.com", 443), timeout=2.0)
        fake_ctx.wrap_socket.assert_called_once_with(raw_sock, server_hostname="shinobu.example.com")
        self.assertIs(out_sock, tls_sock)
        self.assertEqual(leftover, b": keepalive\n")
        self.assertFalse(is_chunked)
        req_str = b"".join(sent_chunks).decode("utf-8")
        self.assertIn("Host: shinobu.example.com\r\n", req_str)
        self.assertNotIn("Host: shinobu.example.com:443", req_str)

        # iter_events recovers from ssl.SSLError and reconnects
        stop_event = threading.Event()
        disconnects: list[Exception | None] = []
        attempts = {"n": 0}

        def _flaky_open(*, connect_timeout: float):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ssl.SSLError("simulated TLS drop")
            s = mock.MagicMock()
            s.recv.side_effect = [b""]
            return (
                s,
                b'event: state_changed\ndata: {"state": "idle", "mood": "happy"}\n\n',
                False,
            )

        got: list[Event] = []
        with mock.patch.object(client, "_open_sse_socket", side_effect=_flaky_open):
            for ev in client.iter_events(
                stop_event,
                initial_backoff=0.05,
                max_backoff=0.1,
                on_disconnect=lambda err: disconnects.append(err),
            ):
                got.append(ev)
                stop_event.set()
                break
        self.assertEqual(len(disconnects), 1)
        self.assertIsInstance(disconnects[0], ssl.SSLError)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, EventKind.STATE_CHANGED)

    def test_daemon_client_extended_rest_methods(self) -> None:
        daemon = DaemonServer(self.orch, self.sm, host="127.0.0.1", port=0)
        daemon.start(start_heartbeat=False)
        try:
            client = DaemonClient(daemon.url, daemon.token)

            # Seed a chat turn so turns & episodes are populated
            list(client.chat_stream("hello extended client"))
            ep_id = self.orch.active_episode_id
            self.assertIsNotNone(ep_id)

            turns_resp = client.get_turns(episode_id=ep_id, limit=20)
            self.assertGreaterEqual(len(turns_resp.get("turns", [])), 2)

            eps_resp = client.list_episodes(limit=8)
            self.assertIsNotNone(eps_resp.get("active_episode"))

            new_ep_resp = client.new_episode(summary="unit test archive")
            self.assertTrue(new_ep_resp.get("ok"))

            eng_resp = client.get_engine()
            self.assertIn("engine", eng_resp)

            workers_resp = client.list_workers()
            self.assertIn("workers", workers_resp)

            # Facts CRUD: upsert -> list -> delete -> 404 on second delete
            saved = client.upsert_fact("favorite_treat", "golden donut", confidence=0.95)
            self.assertEqual(saved["status"], "saved")
            fact_id = int(saved["fact"]["id"])

            facts_resp = client.list_facts(limit=50)
            self.assertTrue(any(int(f["id"]) == fact_id for f in facts_resp["facts"]))

            deleted = client.delete_fact(fact_id)
            self.assertEqual(deleted, {"status": "deleted", "id": fact_id})

            with self.assertRaises(DaemonClientError) as ctx:
                client.delete_fact(fact_id)
            self.assertEqual(ctx.exception.status, 404)
        finally:
            daemon.stop()

    def test_discover_endpoint_and_rest_methods(self) -> None:
        self.assertIsNone(discover_endpoint(self.home))

        daemon = DaemonServer(self.orch, self.sm, host="127.0.0.1", port=0)
        daemon.start(start_heartbeat=False)
        try:
            discovered = discover_endpoint(self.home)
            self.assertIsNotNone(discovered)
            assert discovered is not None
            self.assertEqual(discovered["url"], daemon.url)
            self.assertEqual(discovered["token"], daemon.token)
            self.assertEqual(discovered["health"]["status"], "ok")

            client = DaemonClient(discovered["url"], discovered["token"])
            health = client.health()
            self.assertEqual(health["status"], "ok")

            snap = client.get_state()
            self.assertEqual(snap["state"], "idle")
            self.assertEqual(snap["mood"], "calm")

            updated = client.set_state(state="donut", mood="happy", reason="test_update")
            self.assertEqual(updated["state"], "donut")
            self.assertEqual(updated["mood"], "happy")

            # Stream chat over real SSE endpoint
            chat_events = list(client.chat_stream("ping daemon"))
            kinds = [ev.kind for ev in chat_events]
            self.assertIn(EventKind.TOKEN_DELTA, kinds)
            self.assertIn(EventKind.DONE, kinds)
            joined = "".join(
                str(ev.payload.get("delta") or ev.payload.get("text") or "")
                for ev in chat_events
                if ev.kind == EventKind.TOKEN_DELTA
            )
            self.assertEqual(joined, "Hello from local daemon!")

            # Trigger sleep consolidation
            sleep_resp = client.sleep()
            self.assertEqual(sleep_resp["status"], "consolidated")
            self.assertEqual(sleep_resp["state"]["state"], "sleepy")

            # Wrong token raises DaemonAuthError
            bad_client = DaemonClient(daemon.url, "invalid-token")
            with self.assertRaises(DaemonAuthError):
                bad_client.health()
            with self.assertRaises(DaemonAuthError):
                list(bad_client.chat_stream("should fail"))
        finally:
            daemon.stop()

        # After stop, endpoint.json is cleaned up -> None
        self.assertIsNone(discover_endpoint(self.home))

        # Stale / corrupt / external endpoint.json -> None
        ep_path = self.home / "runtime" / "endpoint.json"
        ep_path.parent.mkdir(parents=True, exist_ok=True)
        ep_path.write_text("{corrupt json", encoding="utf-8")
        self.assertIsNone(discover_endpoint(self.home))

        ep_path.write_text(
            json.dumps({"url": "http://192.168.1.25:9000", "token": "abc"}),
            encoding="utf-8",
        )
        self.assertIsNone(discover_endpoint(self.home))

        dead_port = _pick_free_loopback_port()
        ep_path.write_text(
            json.dumps({"url": f"http://127.0.0.1:{dead_port}", "token": "abc"}),
            encoding="utf-8",
        )
        self.assertIsNone(discover_endpoint(self.home, timeout=0.4))

    def test_sse_iter_events_reconnects_with_backoff_after_restart(self) -> None:
        fixed_port = _pick_free_loopback_port()
        shared_token = "stable-reconnect-token"
        daemon1 = DaemonServer(
            self.orch,
            self.sm,
            host="127.0.0.1",
            port=fixed_port,
            token=shared_token,
            sse_keepalive_sec=0.3,
        )
        daemon1.start(start_heartbeat=False)

        client = DaemonClient(daemon1.url, shared_token)
        stop_event = threading.Event()
        received: queue.Queue[Event] = queue.Queue()
        connect_events: list[float] = []
        disconnect_events: list[Exception | None] = []
        first_connected = threading.Event()
        reconnected = threading.Event()

        def _on_connect() -> None:
            connect_events.append(time.monotonic())
            if len(connect_events) == 1:
                first_connected.set()
            elif len(connect_events) >= 2:
                reconnected.set()

        def _on_disconnect(err: Exception | None) -> None:
            disconnect_events.append(err)

        def _consumer() -> None:
            for ev in client.iter_events(
                stop_event,
                initial_backoff=0.1,
                max_backoff=0.4,
                poll_interval=0.05,
                on_connect=_on_connect,
                on_disconnect=_on_disconnect,
            ):
                received.put(ev)

        t = threading.Thread(target=_consumer, daemon=True)
        t.start()

        daemon2: DaemonServer | None = None
        try:
            self.assertTrue(first_connected.wait(timeout=3.0), "Initial SSE connect timed out")
            daemon1.publish_event(Event(kind=EventKind.BUBBLE, payload={"text": "first bubble"}))
            ev1 = received.get(timeout=3.0)
            self.assertEqual(ev1.kind, EventKind.BUBBLE)
            self.assertEqual(ev1.payload["text"], "first bubble")

            # Stop first daemon to force disconnect + backoff reconnect
            daemon1.stop()
            time.sleep(0.25)
            self.assertGreaterEqual(len(disconnect_events), 1)

            # Restart a second daemon on the same port and token
            daemon2 = DaemonServer(
                self.orch,
                self.sm,
                host="127.0.0.1",
                port=fixed_port,
                token=shared_token,
                sse_keepalive_sec=0.3,
            )
            daemon2.start(start_heartbeat=False)
            self.assertTrue(reconnected.wait(timeout=4.0), "SSE reconnect timed out")

            daemon2.publish_event(
                Event(
                    kind=EventKind.STATE_CHANGED,
                    payload={"state": "thinking", "mood": "smug"},
                )
            )
            ev2 = received.get(timeout=3.0)
            self.assertEqual(ev2.kind, EventKind.STATE_CHANGED)
            self.assertEqual(ev2.payload["state"], "thinking")
        finally:
            stop_event.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "SSE consumer thread did not exit promptly")
            daemon1.stop()
            if daemon2 is not None:
                daemon2.stop()

    def test_event_stream_worker_background_thread(self) -> None:
        daemon = DaemonServer(self.orch, self.sm, host="127.0.0.1", port=0, sse_keepalive_sec=0.3)
        daemon.start(start_heartbeat=False)
        events_q: queue.Queue[Event] = queue.Queue()
        connected_flag = threading.Event()

        def _on_status(ok: bool, _detail: str) -> None:
            if ok:
                connected_flag.set()

        client = DaemonClient(daemon.url, daemon.token)
        worker = EventStreamWorker(
            client,
            on_event=events_q.put,
            on_status=_on_status,
            initial_backoff=0.1,
            poll_interval=0.05,
        )
        try:
            worker.start()
            self.assertTrue(connected_flag.wait(timeout=3.0))
            self.assertTrue(worker.is_running)

            daemon.publish_event(Event(kind=EventKind.BUBBLE, payload={"text": "worker ping"}))
            ev = events_q.get(timeout=3.0)
            self.assertEqual(ev.payload["text"], "worker ping")
        finally:
            worker.stop(timeout=2.0)
            self.assertFalse(worker.is_running)
            daemon.stop()

    def test_headless_controller_sync_history_controls_and_error_notices(self) -> None:
        daemon = DaemonServer(self.orch, self.sm, host="127.0.0.1", port=0)
        daemon.start(start_heartbeat=False)
        try:
            client = DaemonClient(daemon.url, daemon.token, allow_remote=False)
            ctrl = PetShellController(home=self.home, url=daemon.url)
            ctrl.connected = True

            # Produce one real conversation turn over SSE chat_stream
            chat_events = list(client.chat_stream("sync test message"))
            self.assertTrue(any(ev.kind == EventKind.DONE for ev in chat_events))

            # Sync active episode history and format transcript
            ok_hist, transcript, err_hist = ctrl.sync_history_from_client(client, limit=30)
            self.assertTrue(ok_hist, f"history sync failed: {err_hist}")
            self.assertIn("You: sync test message", transcript)
            self.assertIn("Companion: Hello from local daemon!", transcript)

            # Sync engine status from real daemon endpoint
            ok_eng, eng_info, err_eng = ctrl.sync_engine_from_client(client)
            self.assertTrue(ok_eng, f"engine sync failed: {err_eng}")
            self.assertIsInstance(eng_info, dict)

            # Single-LLM fake daemon rejects route switch -> surfaced as notice bubble, not swallowed
            ok_rt_unsup, _, rt_unsup_notice = ctrl.execute_client_action(
                "切换大脑路由",
                lambda: client.set_route("cloud"),
            )
            self.assertFalse(ok_rt_unsup)
            self.assertIsNotNone(rt_unsup_notice)
            assert rt_unsup_notice is not None
            self.assertIn("切换大脑路由失败", rt_unsup_notice)

            # Router-capable client route & reasoning switches update controller state and badge
            stub_client = mock.Mock()
            stub_client.set_route.return_value = {"status": "ok", "route": "cloud"}
            stub_client.set_reasoning.return_value = {"status": "ok", "reasoning_effort": "high"}

            ok_rt, rt_resp, rt_notice = ctrl.execute_client_action(
                "切换大脑路由",
                lambda: stub_client.set_route("cloud"),
                success_notice="🧠 大脑路由已切换: Cloud",
            )
            self.assertTrue(ok_rt, f"set_route failed: {rt_notice}")
            ctrl.apply_engine_info(rt_resp if isinstance(rt_resp, dict) else {"route": "cloud"})
            self.assertEqual(ctrl.current_route, "cloud")

            ok_rs, rs_resp, rs_notice = ctrl.execute_client_action(
                "切换思考深度",
                lambda: stub_client.set_reasoning("high"),
                success_notice="💭 思考深度已切换: high",
            )
            self.assertTrue(ok_rs, f"set_reasoning failed: {rs_notice}")
            ctrl.apply_engine_info(
                rs_resp if isinstance(rs_resp, dict) else {"reasoning_effort": "high"}
            )
            self.assertEqual(ctrl.current_reasoning, "high")
            badge = ctrl.connection_badge()
            self.assertIn("Route: Cloud", badge)
            self.assertIn("Think: high", badge)

            # Fast reset episode clears active episode turns via real daemon endpoint
            ok_ep, _, ep_notice = ctrl.execute_client_action(
                "开启新对话",
                lambda: client.new_episode(),
                success_notice="🆕 已开启新对话",
            )
            self.assertTrue(ok_ep, f"new_episode failed: {ep_notice}")
            ok_hist2, transcript2, _ = ctrl.sync_history_from_client(client, limit=30)
            self.assertTrue(ok_hist2)
            self.assertEqual(transcript2, "")

            # Sleep consolidation via real daemon endpoint (after a new turn exists)
            list(client.chat_stream("message before sleep"))
            ok_slp, _, slp_notice = ctrl.execute_client_action(
                "归档记忆",
                lambda: client.sleep(),
                success_notice="🌙 记忆已归档",
            )
            self.assertTrue(ok_slp, f"sleep failed: {slp_notice}")

            # SSE state_changed (e.g. from Telegram) requests history & engine refresh
            ev_res = ctrl.handle_event(
                Event(
                    kind=EventKind.STATE_CHANGED,
                    payload={"reason": "episode_new", "route": "auto", "reasoning_effort": "medium"},
                )
            )
            self.assertTrue(ev_res["refresh_history"])
            self.assertTrue(ev_res["refresh_engine"])
            self.assertEqual(ctrl.current_route, "auto")
            self.assertEqual(ctrl.current_reasoning, "medium")

            # Auth error must surface as user-visible bubble notice instead of silent swallow
            bad_client = DaemonClient(daemon.url, "wrong-token")
            ok_bad, _, err_notice = ctrl.execute_client_action(
                "切换大脑路由",
                lambda: bad_client.set_route("local"),
            )
            self.assertFalse(ok_bad)
            self.assertIsNotNone(err_notice)
            assert err_notice is not None
            self.assertIn("切换大脑路由失败", err_notice)
            msg = ctrl.push_notice(err_notice)
            self.assertIsNotNone(msg)
            self.assertEqual(ctrl.bubbles.current().text, err_notice)  # type: ignore[union-attr]
        finally:
            daemon.stop()
        self.assertNotIn("PySide6", sys.modules)


class TestUiPrefsAndPosition(unittest.TestCase):
    def test_position_persistence_and_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertIsNone(load_pet_position(home))
            self.assertEqual(load_ui_prefs(home), {})

            save_ui_prefs(home, {"theme": "light"})
            saved_file = save_pet_position(home, 640, 420)
            self.assertEqual(saved_file, ui_prefs_path(home))
            self.assertTrue(saved_file.is_file())
            self.assertEqual(load_pet_position(home), (640, 420))
            self.assertEqual(load_ui_prefs(home)["theme"], "light")

            # Corrupt file returns empty safely
            saved_file.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_ui_prefs(home), {})
            self.assertIsNone(load_pet_position(home))

        # Clamping keeps window strictly inside screen bounds
        self.assertEqual(clamp_position(-50, -30, 200, 220, (0, 0, 1920, 1080)), (0, 0))
        self.assertEqual(clamp_position(2500, 1500, 200, 220, (0, 0, 1920, 1080)), (1720, 860))
        self.assertEqual(clamp_position(500, 300, 200, 220, (0, 0, 1920, 1080)), (500, 300))


class TestBubbleQueueAndController(unittest.TestCase):
    def test_bubble_queue_duration_bounds_fifo_and_auto_hide(self) -> None:
        now = [100.0]
        bq = BubbleQueue(clock=lambda: now[0])

        # Blank messages are ignored
        self.assertIsNone(bq.enqueue("   "))
        self.assertIsNone(bq.current())

        # Durations are clamped strictly within [6.0, 10.0]
        self.assertEqual(clamp_bubble_duration("hi", 1.5), MIN_BUBBLE_DURATION_SEC)
        self.assertEqual(clamp_bubble_duration("hi", 42.0), MAX_BUBBLE_DURATION_SEC)
        self.assertGreaterEqual(clamp_bubble_duration("short"), MIN_BUBBLE_DURATION_SEC)
        self.assertLessEqual(clamp_bubble_duration("x" * 500), MAX_BUBBLE_DURATION_SEC)

        m1 = bq.enqueue("Take a short break!", duration_sec=6.0)
        m2 = bq.enqueue("Hydration reminder.", duration_sec=8.0)
        assert m1 is not None and m2 is not None
        self.assertEqual(bq.current(), m1)
        self.assertEqual(bq.pending_count, 1)

        now[0] += 5.9
        self.assertEqual(bq.current(), m1)
        self.assertAlmostEqual(bq.remaining_seconds(), 0.1, places=5)

        # At t+6.0s, m1 auto-hides and m2 becomes active
        now[0] += 0.2
        self.assertEqual(bq.current(), m2)
        self.assertEqual(bq.pending_count, 0)

        # After m2's 8.0s elapses, bubble auto-hides completely
        now[0] += 8.0
        self.assertIsNone(bq.current())
        self.assertEqual(bq.remaining_seconds(), 0.0)

        # Dismiss immediately promotes next bubble
        m3 = bq.enqueue("First queued", duration_sec=7.0)
        m4 = bq.enqueue("Second queued", duration_sec=7.0)
        self.assertEqual(bq.current(), m3)
        self.assertEqual(bq.dismiss(), m4)
        bq.clear()
        self.assertIsNone(bq.current())

    def test_controller_routes_bubble_and_state_events(self) -> None:
        now = [50.0]
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = PetShellController(home=Path(tmp), clock=lambda: now[0])
            init_asset = ctrl.apply_snapshot({"state": "idle", "mood": "calm"})
            self.assertEqual(init_asset.kind, "svg_inline")
            self.assertEqual(init_asset.state, "idle")

            # Bubble event -> enqueues zero-token bubble without changing avatar
            res_bubble = ctrl.handle_event(
                Event(kind=EventKind.BUBBLE, payload={"text": "Zero-token notice"})
            )
            self.assertFalse(res_bubble["avatar_changed"])
            self.assertIsNotNone(res_bubble["bubble"])
            self.assertEqual(ctrl.bubbles.current().text, "Zero-token notice")  # type: ignore[union-attr]

            # state_changed event -> refreshes avatar SVG
            res_state = ctrl.handle_event(
                Event(
                    kind=EventKind.STATE_CHANGED,
                    payload={"state": "donut", "mood": "happy"},
                )
            )
            self.assertTrue(res_state["avatar_changed"])
            asset = res_state["asset"]
            self.assertEqual(asset.state, "donut")
            self.assertEqual(asset.mood, "happy")
            self.assertIn("<svg", asset.svg_text)

            # mood_changed event -> updates mood while keeping state
            res_mood = ctrl.handle_event(
                Event(kind=EventKind.MOOD_CHANGED, payload={"mood": "smug"})
            )
            self.assertTrue(res_mood["avatar_changed"])
            self.assertEqual(res_mood["asset"].state, "donut")
            self.assertEqual(res_mood["asset"].mood, "smug")


class TestLazyPySide6AndPackaging(unittest.TestCase):
    def test_zero_dep_imports_do_not_load_pyside6(self) -> None:
        self.assertIsNotNone(shinobu_core)
        self.assertNotIn("PySide6", sys.modules)

    def test_missing_pyside6_gives_friendly_install_hint(self) -> None:
        fake_client = DaemonClient("http://127.0.0.1:8765", "dummy-token")
        with mock.patch.dict(sys.modules, {"PySide6": None}):
            with self.assertRaises(PySide6NotInstalledError) as ctx1:
                require_pyside6()
            self.assertIsInstance(ctx1.exception, ImportError)
            self.assertIn("PySide6", str(ctx1.exception))
            self.assertIn("shinobu-core[desktop]", str(ctx1.exception))

            with self.assertRaises(PySide6NotInstalledError) as ctx2:
                PetShellWindow(fake_client)
            self.assertIn("shinobu-core[desktop]", str(ctx2.exception))

            with self.assertRaises(PySide6NotInstalledError) as ctx3:
                run_pet_shell(url="http://127.0.0.1:8765", token="dummy-token")
            self.assertIn("PySide6", str(ctx3.exception))

    def test_pyproject_optional_desktop_extra_keeps_core_zero_dep(self) -> None:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["dependencies"], [])
        desktop_deps = project["optional-dependencies"]["desktop"]
        self.assertTrue(any(dep.startswith("PySide6") for dep in desktop_deps))


class TestPetCloudSyncAndControls(unittest.TestCase):
    def test_format_turns_transcript_filters_tool_noise(self) -> None:
        from shinobu_core.ui.pet_window import extract_turns_list, format_turns_transcript

        raw_payload = {
            "turns": [
                {"role": "system", "content": "hidden system prompt"},
                {"role": "user", "content": "  Hey Shinobu!  "},
                {"role": "tool", "content": '{"status": "ok", "tool_call": "memory_search"}'},
                {"role": "assistant", "content": "Donut time?"},
                {"role": "user", "content": "   "},
                {"role": "assistant", "content": None},
                "not-a-dict",
            ]
        }
        turns = extract_turns_list(raw_payload)
        self.assertEqual(len(turns), 6)
        transcript = format_turns_transcript(turns)
        self.assertEqual(transcript, "You: Hey Shinobu!\n\nCompanion: Donut time?")
        self.assertNotIn("tool_call", transcript)
        self.assertNotIn("hidden system prompt", transcript)
        self.assertEqual(format_turns_transcript([]), "")

    def test_format_connection_badge_cloud_vs_local(self) -> None:
        from shinobu_core.ui.pet_window import format_connection_badge

        cloud_online = format_connection_badge(
            "https://shinobu.example.com",
            True,
            {"route": "cloud", "reasoning_effort": "high"},
        )
        self.assertIn("Cloud", cloud_online)
        self.assertIn("Online", cloud_online)
        self.assertIn("Route: Cloud", cloud_online)
        self.assertIn("Think: high", cloud_online)

        local_offline = format_connection_badge(
            "http://127.0.0.1:8765",
            False,
            {"route_mode": "auto", "reasoning": "off"},
        )
        self.assertIn("Local", local_offline)
        self.assertIn("Offline", local_offline)
        self.assertIn("Route: Auto", local_offline)
        self.assertIn("Think: off", local_offline)

        minimal = format_connection_badge("http://localhost:8765", True, None)
        self.assertEqual(minimal, "Local · Online")

    def test_remote_pet_client_skips_local_discovery_and_enforces_allow_remote(self) -> None:
        from shinobu_core.ui.pet_window import create_pet_daemon_client

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            # Explicit remote URL + token with allow_remote=True must NOT probe endpoint.json
            with mock.patch(
                "shinobu_core.ui.pet_window.discover_endpoint",
                side_effect=AssertionError("discover_endpoint must not be called for explicit remote credentials"),
            ):
                resolved_home, client = create_pet_daemon_client(
                    home=home,
                    url="https://shinobu.example.com",
                    token="remote-secret-token",
                    allow_remote=True,
                )
                self.assertEqual(resolved_home, home)
                self.assertEqual(client.url, "https://shinobu.example.com")

                # Same remote URL with allow_remote=False must be rejected by DaemonClient
                with self.assertRaises(ValueError):
                    create_pet_daemon_client(
                        home=home,
                        url="https://shinobu.example.com",
                        token="remote-secret-token",
                        allow_remote=False,
                    )

        # run_pet_shell accepts allow_remote without importing PySide6
        with mock.patch.dict(sys.modules, {"PySide6": None}):
            with self.assertRaises(PySide6NotInstalledError):
                run_pet_shell(
                    url="https://shinobu.example.com",
                    token="remote-secret-token",
                    allow_remote=True,
                )
        self.assertNotIn("PySide6", sys.modules)

    def test_daemon_client_synthesize_tts_success_and_errors(self) -> None:
        import io
        import json
        from urllib import error as urllib_error

        from shinobu_core.ui.client import DaemonAuthError, DaemonClientError

        client = DaemonClient("http://127.0.0.1:8765", "secret-token")

        with self.assertRaises(ValueError):
            client.synthesize_tts("   ")

        captured: dict[str, object] = {}

        class _FakeResp:
            def __init__(self, body: bytes, content_type: str) -> None:
                self._body = body
                self.headers = {"Content-Type": content_type}

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        def _fake_open_ok(req: object, timeout: float = 0.0) -> _FakeResp:
            captured["url"] = getattr(req, "full_url")
            captured["method"] = getattr(req, "get_method")()
            captured["data"] = json.loads(getattr(req, "data").decode("utf-8"))
            captured["auth"] = getattr(req, "get_header")("Authorization")
            return _FakeResp(b"RIFF\x24\x00\x00\x00WAVE", "audio/wav")

        with mock.patch.object(client._opener, "open", side_effect=_fake_open_ok):
            audio, ct = client.synthesize_tts("  Hello Shinobu  ", mood="happy", fmt="wav")
        self.assertEqual(audio, b"RIFF\x24\x00\x00\x00WAVE")
        self.assertEqual(ct, "audio/wav")
        self.assertEqual(captured["url"], "http://127.0.0.1:8765/api/tts")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["auth"], "Bearer secret-token")
        self.assertEqual(
            captured["data"],
            {"text": "Hello Shinobu", "mood": "happy", "fmt": "wav"},
        )

        err_401 = urllib_error.HTTPError(
            "http://127.0.0.1:8765/api/tts",
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "unauthorized"}'),
        )
        with mock.patch.object(client._opener, "open", side_effect=err_401):
            with self.assertRaises(DaemonAuthError) as ctx_auth:
                client.synthesize_tts("hello")
        self.assertEqual(ctx_auth.exception.status, 401)

        err_503 = urllib_error.HTTPError(
            "http://127.0.0.1:8765/api/tts",
            503,
            "Service Unavailable",
            hdrs={},
            fp=io.BytesIO(b'{"error": "tts_unavailable"}'),
        )
        with mock.patch.object(client._opener, "open", side_effect=err_503):
            with self.assertRaises(DaemonClientError) as ctx_503:
                client.synthesize_tts("hello")
        self.assertEqual(ctx_503.exception.status, 503)
        self.assertEqual(ctx_503.exception.detail, {"error": "tts_unavailable"})

    def test_extract_last_assistant_text_and_play_audio_bytes(self) -> None:
        import types

        from shinobu_core.ui.pet_window import (
            extract_last_assistant_text,
            play_audio_bytes,
        )

        payload = {
            "turns": [
                {"role": "system", "content": "hidden"},
                {"role": "assistant", "content": "First reply"},
                {"role": "user", "content": "Next question"},
                {"role": "tool", "content": '{"ok": true}'},
                {"role": "assistant", "text": "  Latest reply  "},
                {"role": "assistant", "content": "   "},
                {"role": "user", "content": "Trailing user message"},
            ]
        }
        self.assertEqual(extract_last_assistant_text(payload), "Latest reply")
        self.assertEqual(extract_last_assistant_text([]), "")

        ok_empty, msg_empty = play_audio_bytes(b"", "audio/wav")
        self.assertFalse(ok_empty)
        self.assertTrue(msg_empty)

        with mock.patch.object(sys, "platform", "linux"):
            ok_linux, msg_linux = play_audio_bytes(b"RIFF1234", "audio/wav")
        self.assertFalse(ok_linux)
        self.assertIn("Web", msg_linux)

        with mock.patch.object(sys, "platform", "win32"):
            ok_mp3, msg_mp3 = play_audio_bytes(b"ID3fake", "audio/mpeg")
            self.assertFalse(ok_mp3)
            self.assertIn("audio/mpeg", msg_mp3)

            called: list[tuple[bytes, int]] = []
            fake_winsound = types.SimpleNamespace(
                SND_MEMORY=4,
                PlaySound=lambda data, flags: called.append((data, flags)),
            )
            with mock.patch.dict(sys.modules, {"winsound": fake_winsound}):
                ok_wav, msg_wav = play_audio_bytes(b"RIFF1234", "audio/wav")
            self.assertTrue(ok_wav)
            self.assertEqual(msg_wav, "")
            self.assertEqual(called, [(b"RIFF1234", 4)])

            def _boom(_data: bytes, _flags: int) -> None:
                raise RuntimeError("device busy")

            failing_winsound = types.SimpleNamespace(SND_MEMORY=4, PlaySound=_boom)
            with mock.patch.dict(sys.modules, {"winsound": failing_winsound}):
                ok_err, msg_err = play_audio_bytes(b"RIFF1234", "audio/wav")
            self.assertFalse(ok_err)
            self.assertIn("device busy", msg_err)

        self.assertNotIn("PySide6", sys.modules)


if __name__ == "__main__":
    unittest.main()
