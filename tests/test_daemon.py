# -*- coding: utf-8 -*-
"""Unit & integration tests for PetStateMachine, HeartbeatLoop, and DaemonServer."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.runtime.heartbeat import HeartbeatLoop
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.runtime.state_machine import PetStateMachine
from shinobu_core.server.daemon import DaemonServer
from shinobu_core.server.openai_compat_api import (
    extract_user_text,
    is_allowed_local_origin,
)


@dataclass
class _FakeChunk:
    delta: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    done: bool = False


class _StubEngine:
    """Minimal zero-network EngineAdapter stub tracking exact LLM call counts."""

    name = "stub-daemon-engine"

    def __init__(self, chat_deltas: list[str] | None = None) -> None:
        self.chat_deltas = chat_deltas or ["吾乃", "忍野忍", "，甜甜圈拿来！"]
        self.call_count = 0
        self.requests: list[Any] = []

    def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return {
            "summary": "聊了甜甜圈与夜间守护网关测试。",
            "journal_entry": "今天测试了本地回环守护进程。",
            "journal": "今天测试了本地回环守护进程。",
            "facts": [{"key": "favorite_snack", "value": "golden_chocolate_donut"}],
            "timemap": [],
            "memory_append": ["偏好：黄金巧克力甜甜圈"],
            "memory_updates": ["偏好：黄金巧克力甜甜圈"],
        }

    def stream_turn(self, req: Any) -> Iterator[_FakeChunk]:
        self.call_count += 1
        self.requests.append(req)
        for idx, piece in enumerate(self.chat_deltas):
            yield _FakeChunk(delta=piece, done=(idx == len(self.chat_deltas) - 1))


def _parse_sse_stream(resp: Any, stop_after_event: str | None = None, max_events: int = 50) -> list[tuple[str, dict[str, Any]]]:
    """Read SSE frames from a file-like HTTP response until EOF or condition."""
    parsed: list[tuple[str, dict[str, Any]]] = []
    cur_event = ""
    cur_data_lines: list[str] = []

    while len(parsed) < max_events:
        raw_line = resp.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line == "":
            if cur_data_lines:
                data_str = "\n".join(cur_data_lines)
                payload = json.loads(data_str)
                ev_name = cur_event or str(payload.get("kind") or "")
                parsed.append((ev_name, payload))
                cur_event = ""
                cur_data_lines = []
                if stop_after_event and ev_name == stop_after_event:
                    break
            continue
        if line.startswith("event:"):
            cur_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            cur_data_lines.append(line[len("data:") :].strip())
    return parsed


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
    raw_auth_header: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    if raw_auth_header is not None:
        headers["Authorization"] = raw_auth_header
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"

    data_bytes: bytes | None = None
    if body is not None:
        data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib_request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw)


class TestPetStateMachineAndHeartbeat(unittest.TestCase):
    def test_state_machine_transitions_and_validation(self) -> None:
        now = [100.0]
        sm = PetStateMachine(clock=lambda: now[0])
        self.assertEqual(sm.state, "idle")
        self.assertEqual(sm.mood, "calm")
        self.assertEqual(sm.idle_seconds(), 0.0)

        ev = sm.transition("thinking", mood="smug", reason="user_prompt")
        self.assertEqual(ev.kind, EventKind.STATE_CHANGED)
        self.assertEqual(ev.payload["from"], "idle")
        self.assertEqual(ev.payload["to"], "thinking")
        self.assertEqual(ev.payload["mood"], "smug")

        now[0] = 145.5
        self.assertAlmostEqual(sm.idle_seconds(), 45.5, places=3)
        sm.touch()
        self.assertEqual(sm.idle_seconds(), 0.0)

        snap = sm.snapshot()
        self.assertEqual(snap["state"], "thinking")
        self.assertEqual(snap["mood"], "smug")
        self.assertEqual(snap["reason"], "user_prompt")

        with self.assertRaises(ValueError):
            sm.transition("flying")
        with self.assertRaises(ValueError):
            sm.transition("idle", mood="furious")

    def test_zero_token_heartbeat_timemap_sensor_and_idle_progression(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.sqlite")
            try:
                store.add_schedule(
                    "2026-11-21T10:00:00Z",
                    "喝水提醒",
                    {"text": "该喝水啦，仆人。"},
                )
                store.add_schedule(
                    "2026-11-21T23:59:00Z",
                    "未来提醒",
                    {"text": "还没到时间"},
                )

                now = [0.0]
                sm = PetStateMachine(clock=lambda: now[0])
                published: list[Event] = []

                def broken_sensor() -> list[Event]:
                    raise RuntimeError("sensor boom")

                def ok_sensor() -> list[Event]:
                    return [Event(kind=EventKind.BUBBLE, payload={"source": "sensor", "text": "微风拂过"})]

                hb = HeartbeatLoop(
                    store=store,
                    state_machine=sm,
                    sensors=[broken_sensor, ok_sensor],
                    publish=published.append,
                    interval_sec=30.0,
                    donut_after_sec=60.0,
                    sleepy_after_sec=180.0,
                )

                # Tick 1: due timemap + healthy sensor (broken sensor isolated), still idle
                now[0] = 10.0
                events1 = hb.tick_once(now_iso="2026-11-21T12:00:00Z")
                self.assertEqual(len(events1), 2)
                self.assertEqual(events1[0].kind, EventKind.BUBBLE)
                self.assertEqual(events1[0].payload["source"], "timemap")
                self.assertEqual(events1[0].payload["text"], "该喝水啦，仆人。")
                self.assertEqual(events1[1].payload["source"], "sensor")
                self.assertEqual(sm.state, "idle")

                # Tick 2: timemap already popped (no duplicate), idle >= 60s -> donut
                now[0] = 65.0
                events2 = hb.tick_once(now_iso="2026-11-21T12:01:00Z")
                timemap_evs = [e for e in events2 if e.payload.get("source") == "timemap"]
                self.assertEqual(len(timemap_evs), 0)
                self.assertEqual(sm.state, "donut")
                self.assertTrue(
                    any(e.kind == EventKind.STATE_CHANGED and e.payload["to"] == "donut" for e in events2)
                )

                # Tick 3: idle >= 180s -> sleepy (with sleepy mood)
                now[0] = 190.0
                events3 = hb.tick_once(now_iso="2026-11-21T12:03:00Z")
                self.assertEqual(sm.state, "sleepy")
                self.assertEqual(sm.mood, "sleepy")
                self.assertTrue(
                    any(e.kind == EventKind.STATE_CHANGED and e.payload["to"] == "sleepy" for e in events3)
                )
                self.assertEqual(len(published), len(events1) + len(events2) + len(events3))
            finally:
                store.close()


class TestDaemonServer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "shinobu_home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.home / "memory.sqlite")
        self.engine = _StubEngine()
        self.orch = Orchestrator(home=self.home, store=self.store, engine=self.engine)
        self.sm = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_refuses_non_loopback_host(self) -> None:
        for bad_host in ("0.0.0.0", "localhost", "::", "192.168.1.2", ""):
            with self.assertRaises(ValueError):
                DaemonServer(self.orch, self.sm, host=bad_host)

    def test_auth_401_and_endpoint_lifecycle(self) -> None:
        daemon = DaemonServer(self.orch, self.sm, host="127.0.0.1", port=0)
        endpoint_file = self.home / "runtime" / "endpoint.json"
        self.assertFalse(endpoint_file.exists())

        info = daemon.start(start_heartbeat=False)
        try:
            self.assertTrue(endpoint_file.exists())
            disk_info = json.loads(endpoint_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_info["url"], info["url"])
            self.assertEqual(disk_info["token"], daemon.token)
            self.assertTrue(disk_info["url"].startswith("http://127.0.0.1:"))

            # Every endpoint rejects missing or wrong token with 401
            for method, path, body in (
                ("GET", "/health", None),
                ("GET", "/state", None),
                ("GET", "/events", None),
                ("POST", "/chat", {"text": "hi"}),
                ("POST", "/sleep", {}),
            ):
                code_missing, data_missing = _http_json(f"{daemon.url}{path}", method=method, body=body)
                self.assertEqual(code_missing, 401, f"{method} {path} should return 401 without token")
                self.assertEqual(data_missing["error"], "unauthorized")

                code_wrong, _ = _http_json(
                    f"{daemon.url}{path}",
                    method=method,
                    token="wrong-token-value",
                    body=body,
                )
                self.assertEqual(code_wrong, 401, f"{method} {path} should return 401 on wrong token")

            # Valid token succeeds on /health and /state
            code_h, health = _http_json(f"{daemon.url}/health", token=daemon.token)
            self.assertEqual(code_h, 200)
            self.assertEqual(health["status"], "ok")

            code_s, state = _http_json(f"{daemon.url}/state", token=daemon.token)
            self.assertEqual(code_s, 200)
            self.assertEqual(state["state"], "idle")
            self.assertEqual(state["mood"], "calm")

            # /sleep before any active episode returns 409 Conflict
            code_sleep, _ = _http_json(f"{daemon.url}/sleep", method="POST", token=daemon.token, body={})
            self.assertEqual(code_sleep, 409)
        finally:
            daemon.stop()

        self.assertFalse(endpoint_file.exists())

    def test_streaming_chat_sse_push_heartbeat_bubbles_and_sleep(self) -> None:
        # Prepare a zero-token due Timemap reminder and periodic sensor
        self.store.add_schedule(
            "2020-01-01T00:00:00Z",
            "甜甜圈定时冒泡",
            {"text": "卡卡卡，甜甜圈时间到了！"},
        )
        sensor_fired = threading.Event()

        def one_shot_sensor() -> list[Event]:
            if sensor_fired.is_set():
                return []
            sensor_fired.set()
            return [
                Event(
                    kind=EventKind.BUBBLE,
                    payload={"source": "local_sensor", "text": "零Token传感器就绪"},
                )
            ]

        hb = HeartbeatLoop(
            store=self.store,
            state_machine=self.sm,
            sensors=[one_shot_sensor],
            interval_sec=0.06,
            donut_after_sec=3600.0,
            sleepy_after_sec=7200.0,
        )

        with DaemonServer(
            self.orch,
            self.sm,
            heartbeat=hb,
            host="127.0.0.1",
            port=0,
            sse_keepalive_sec=1.0,
        ) as daemon:
            # 1. Start a background SSE subscriber on GET /events
            sse_events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
            sse_connected = threading.Event()
            sse_stop = threading.Event()
            conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5.0)

            def _reader() -> None:
                try:
                    conn.request(
                        "GET",
                        "/events",
                        headers={"Authorization": f"Bearer {daemon.token}"},
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
                    sse_connected.set()

                    cur_event = ""
                    cur_data: list[str] = []
                    while not sse_stop.is_set():
                        raw = resp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line.startswith(":"):
                            continue
                        if line == "":
                            if cur_data:
                                obj = json.loads("\n".join(cur_data))
                                sse_events.put((cur_event or obj.get("kind", ""), obj))
                                cur_event = ""
                                cur_data = []
                            continue
                        if line.startswith("event:"):
                            cur_event = line[len("event:") :].strip()
                        elif line.startswith("data:"):
                            cur_data.append(line[len("data:") :].strip())
                except OSError:
                    pass

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()
            self.assertTrue(sse_connected.wait(timeout=3.0), "SSE subscriber failed to connect")

            # Wait briefly so background 0-token heartbeat fires and pushes bubbles
            collected_hb: list[tuple[str, dict[str, Any]]] = []
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    ev_tuple = sse_events.get(timeout=0.1)
                    collected_hb.append(ev_tuple)
                    sources = {
                        item[1].get("payload", {}).get("source")
                        for item in collected_hb
                        if item[0] == "bubble"
                    }
                    if {"timemap", "local_sensor"}.issubset(sources):
                        break
                except queue.Empty:
                    continue

            hb_sources = {
                item[1].get("payload", {}).get("source")
                for item in collected_hb
                if item[0] == "bubble"
            }
            self.assertIn("timemap", hb_sources)
            self.assertIn("local_sensor", hb_sources)
            # Zero LLM calls during heartbeat ticks!
            self.assertEqual(self.engine.call_count, 0)

            # 2. Call POST /chat and verify streaming SSE token deltas
            chat_req = urllib_request.Request(
                f"{daemon.url}/chat",
                data=json.dumps({"text": "你好呀小忍"}, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib_request.urlopen(chat_req, timeout=5.0) as chat_resp:
                self.assertEqual(chat_resp.status, 200)
                self.assertIn("text/event-stream", chat_resp.headers.get("Content-Type", ""))
                chat_frames = _parse_sse_stream(chat_resp, stop_after_event="done")

            kinds = [k for k, _ in chat_frames]
            self.assertIn("token_delta", kinds)
            self.assertEqual(kinds[-1], "done")
            deltas = [
                frame["payload"]["delta"]
                for k, frame in chat_frames
                if k == "token_delta"
            ]
            self.assertEqual("".join(deltas), "吾乃忍野忍，甜甜圈拿来！")
            self.assertEqual(self.engine.call_count, 1)

            # Verify GET /events subscriber also received the chat done event
            saw_chat_done = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    ev_name, _ = sse_events.get(timeout=0.1)
                    if ev_name == "done":
                        saw_chat_done = True
                        break
                except queue.Empty:
                    continue
            self.assertTrue(saw_chat_done, "GET /events did not receive broadcast chat done event")

            # 3. Call POST /sleep to consolidate and transition to sleepy
            code_sleep, sleep_resp = _http_json(
                f"{daemon.url}/sleep",
                method="POST",
                token=daemon.token,
                body={"date_str": "2026-11-21"},
            )
            self.assertEqual(code_sleep, 200, f"sleep failed: {sleep_resp}")
            self.assertEqual(sleep_resp["status"], "consolidated")
            self.assertEqual(sleep_resp["state"]["state"], "sleepy")
            self.assertEqual(sleep_resp["state"]["mood"], "sleepy")

            sse_stop.set()
            conn.close()
            reader_thread.join(timeout=2.0)


class TestDaemonStudioApiAndStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "SOUL.md").write_text("# SOUL v0\n吾乃忍野忍。", encoding="utf-8")
        (soul_dir / "STYLE.md").write_text("# STYLE\n古风毒舌。", encoding="utf-8")
        (soul_dir / "USER.md").write_text("# USER\n测试主人。", encoding="utf-8")
        (soul_dir / "MEMORY.md").write_text("# MEMORY\n喜欢甜甜圈。", encoding="utf-8")

        self.store = MemoryStore(self.home / "memory.db")
        self.engine = _StubEngine()
        self.orchestrator = Orchestrator(
            home=self.home,
            store=self.store,
            engine=self.engine,
        )
        self.state_machine = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_store_list_facts_schedules_delete_and_episodes(self) -> None:
        self.store.upsert_fact("snack", "donut", confidence=0.95)
        self.store.upsert_fact("drink", "oolong_tea", confidence=0.8)
        facts = self.store.list_facts(limit=10)
        self.assertEqual(len(facts), 2)
        self.assertEqual({f["key"] for f in facts}, {"snack", "drink"})
        self.assertEqual(len(self.store.list_facts(limit=1)), 1)

        s1 = self.store.add_schedule("2026-11-21T20:00:00Z", "past_donut", {"tag": "a"})
        s2 = self.store.add_schedule("2099-01-01T00:00:00Z", "future_donut", {"tag": "b"})
        self.store.pop_due_schedules("2026-11-21T21:00:00Z")

        pending = self.store.list_schedules(include_done=False, limit=10)
        self.assertEqual([item["id"] for item in pending], [s2])
        all_items = self.store.list_schedules(include_done=True, limit=10)
        self.assertEqual({item["id"] for item in all_items}, {s1, s2})

        self.assertTrue(self.store.delete_schedule(s2))
        self.assertFalse(self.store.delete_schedule(s2))
        self.assertEqual(self.store.list_schedules(include_done=False, limit=10), [])

        # Create at least one episode via orchestrator chat and list it
        list(self.orchestrator.send_message("你好小忍"))
        episodes = self.store.list_episodes(limit=30)
        self.assertGreaterEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["id"], self.orchestrator.active_episode_id)

    def test_api_auth_401_and_sse_query_token_fallback(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            endpoints: list[tuple[str, str, dict[str, Any] | None]] = [
                ("GET", "/api/turns?limit=10", None),
                ("GET", "/api/soul", None),
                ("PUT", "/api/soul/SOUL.md", {"content": "hacked"}),
                ("GET", "/api/journals", None),
                ("GET", "/api/memory/facts", None),
                ("GET", "/api/memory/search?q=donut", None),
                ("GET", "/api/timemap", None),
                ("POST", "/api/timemap", {"trigger_at": "2099-01-01T00:00:00Z", "label": "x"}),
                ("DELETE", "/api/timemap/1", None),
                ("POST", "/api/state", {"mood": "happy"}),
            ]
            for method, path, body in endpoints:
                code, payload = _http_json(
                    f"{daemon.url}{path}",
                    method=method,
                    token=None,
                    body=body,
                )
                self.assertEqual(code, 401, f"{method} {path} without token should be 401, got {code} {payload}")

                # Query token must NOT bypass auth on non-/events endpoints
                sep = "&" if "?" in path else "?"
                code_q, _ = _http_json(
                    f"{daemon.url}{path}{sep}token={daemon.token}",
                    method=method,
                    token=None,
                    body=body,
                )
                self.assertEqual(code_q, 401, f"{method} {path} with ?token= must still be 401")

            # /events with wrong query token -> 401
            code_bad_sse, _ = _http_json(
                f"{daemon.url}/events?token=wrong-token",
                method="GET",
                token=None,
            )
            self.assertEqual(code_bad_sse, 401)

            # /events with valid ?token= query fallback -> 200 SSE stream & receives POST /api/state push
            conn = http.client.HTTPConnection(daemon.host, daemon.port, timeout=5.0)
            conn.request("GET", f"/events?token={daemon.token}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))

            sse_events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
            sse_stop = threading.Event()

            def _read_sse() -> None:
                event_name = ""
                data_lines: list[str] = []
                while not sse_stop.is_set():
                    try:
                        raw = resp.readline()
                    except OSError:
                        break
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            try:
                                parsed = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                parsed = {}
                            sse_events.put((event_name or "message", parsed))
                        event_name = ""
                        data_lines = []
                    elif line.startswith("event:"):
                        event_name = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())

            reader = threading.Thread(target=_read_sse, daemon=True)
            reader.start()

            code_state, state_payload = _http_json(
                f"{daemon.url}/api/state",
                method="POST",
                token=daemon.token,
                body={"state": "donut", "mood": "happy", "reason": "ate_donut"},
            )
            self.assertEqual(code_state, 200, f"POST /api/state failed: {state_payload}")
            self.assertEqual(state_payload["state"], "donut")
            self.assertEqual(state_payload["mood"], "happy")

            got_event = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    ev_name, ev_body = sse_events.get(timeout=0.1)
                    if ev_name == "state_changed" and ev_body.get("payload", {}).get("reason") == "ate_donut":
                        got_event = True
                        break
                except queue.Empty:
                    continue
            self.assertTrue(got_event, "SSE ?token= subscriber did not receive state_changed event")

            # Also verify mood-only update and invalid state rejection
            code_mood, mood_payload = _http_json(
                f"{daemon.url}/api/state",
                method="POST",
                token=daemon.token,
                body={"mood": "smug", "reason": "peek"},
            )
            self.assertEqual(code_mood, 200, f"mood-only POST /api/state failed: {mood_payload}")
            self.assertEqual(mood_payload["mood"], "smug")

            code_invalid, _ = _http_json(
                f"{daemon.url}/api/state",
                method="POST",
                token=daemon.token,
                body={"state": "not_a_real_state"},
            )
            self.assertEqual(code_invalid, 400)

            sse_stop.set()
            conn.close()
            reader.join(timeout=2.0)

    def test_soul_read_write_backup_and_prefix_immutability(self) -> None:
        # Start an active episode first so its system_prefix is frozen in SQLite
        list(self.orchestrator.send_message("先开一段对话"))
        before_ep = self.store.list_episodes(limit=1)[0]
        before_prefix = self.orchestrator.active_prefix

        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
        ) as daemon:
            code_get, soul_resp = _http_json(
                f"{daemon.url}/api/soul",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_get, 200)
            self.assertEqual(
                set(soul_resp["files"].keys()),
                {"SOUL.md", "STYLE.md", "USER.md", "MEMORY.md"},
            )
            self.assertIn("SOUL v0", soul_resp["files"]["SOUL.md"])

            # First PUT -> backs up original v0 to _versions/SOUL_v1.md
            code_put1, put1 = _http_json(
                f"{daemon.url}/api/soul/SOUL.md",
                method="PUT",
                token=daemon.token,
                body={"content": "# SOUL v1\n甜甜圈至上。"},
            )
            self.assertEqual(code_put1, 200, f"PUT 1 failed: {put1}")
            self.assertEqual(put1["backup"], "SOUL_v1.md")

            # Second PUT -> backs up v1 to _versions/SOUL_v2.md
            code_put2, put2 = _http_json(
                f"{daemon.url}/api/soul/SOUL.md",
                method="PUT",
                token=daemon.token,
                body={"content": "# SOUL v2\n黄金巧克力甜甜圈。"},
            )
            self.assertEqual(code_put2, 200, f"PUT 2 failed: {put2}")
            self.assertEqual(put2["backup"], "SOUL_v2.md")

            versions_dir = self.home / "soul" / "_versions"
            self.assertEqual(
                (versions_dir / "SOUL_v1.md").read_text(encoding="utf-8"),
                "# SOUL v0\n吾乃忍野忍。",
            )
            self.assertEqual(
                (versions_dir / "SOUL_v2.md").read_text(encoding="utf-8"),
                "# SOUL v1\n甜甜圈至上。",
            )
            self.assertEqual(
                (self.home / "soul" / "SOUL.md").read_text(encoding="utf-8"),
                "# SOUL v2\n黄金巧克力甜甜圈。",
            )

            # Active episode system_prefix MUST remain untouched
            after_ep = self.store.list_episodes(limit=1)[0]
            self.assertEqual(after_ep["id"], before_ep["id"])
            self.assertEqual(after_ep, before_ep)
            self.assertEqual(self.orchestrator.active_prefix, before_prefix)

            # Reject non-whitelist and traversal attempts
            for bad_name in ("evil.md", "..%2FSOUL.md", "SOUL.md%2Fextra"):
                code_bad, _ = _http_json(
                    f"{daemon.url}/api/soul/{bad_name}",
                    method="PUT",
                    token=daemon.token,
                    body={"content": "nope"},
                )
                self.assertEqual(code_bad, 400, f"{bad_name} must be rejected with 400")

    def test_turns_journals_memory_and_timemap_endpoints(self) -> None:
        list(self.orchestrator.send_message("记住我喜欢黄金巧克力甜甜圈"))
        self.store.upsert_fact("favorite_donut", "黄金巧克力甜甜圈", confidence=0.99)

        journals_dir = self.home / "journals"
        journals_dir.mkdir(parents=True, exist_ok=True)
        (journals_dir / "2026-11-20.md").write_text("# 2026-11-20\n旧手账", encoding="utf-8")
        (journals_dir / "2026-11-21.md").write_text("# 2026-11-21\n新手账", encoding="utf-8")
        (journals_dir / "ignore.txt").write_text("not markdown", encoding="utf-8")

        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
        ) as daemon:
            # GET /api/turns
            code_turns, turns_resp = _http_json(
                f"{daemon.url}/api/turns?limit=50",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_turns, 200)
            self.assertEqual(turns_resp["episode_id"], self.orchestrator.active_episode_id)
            self.assertGreaterEqual(len(turns_resp["turns"]), 2)

            # GET /api/journals (descending order, *.md only)
            code_j, j_resp = _http_json(
                f"{daemon.url}/api/journals",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_j, 200)
            names = [item["name"] for item in j_resp["journals"]]
            self.assertEqual(names, ["2026-11-21.md", "2026-11-20.md"])
            self.assertIn("新手账", j_resp["journals"][0]["content"])
            self.assertTrue(j_resp["journals"][0]["mtime"])

            # GET /api/memory/facts & GET /api/memory/search
            code_f, f_resp = _http_json(
                f"{daemon.url}/api/memory/facts",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_f, 200)
            self.assertEqual(len(f_resp["facts"]), 1)
            self.assertEqual(f_resp["facts"][0]["key"], "favorite_donut")

            from urllib.parse import quote

            code_s, s_resp = _http_json(
                f"{daemon.url}/api/memory/search?q={quote('黄金巧克力')}",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_s, 200)
            self.assertGreaterEqual(len(s_resp["results"]), 1)

            # POST /api/timemap -> GET /api/timemap -> DELETE /api/timemap/<id>
            code_add, add_resp = _http_json(
                f"{daemon.url}/api/timemap",
                method="POST",
                token=daemon.token,
                body={
                    "trigger_at": "2099-12-31T23:59:00Z",
                    "label": "buy_donuts",
                    "payload": {"count": 6},
                },
            )
            self.assertEqual(code_add, 201, f"POST /api/timemap failed: {add_resp}")
            sched_id = add_resp["id"]

            code_list, list_resp = _http_json(
                f"{daemon.url}/api/timemap",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_list, 200)
            self.assertEqual([item["id"] for item in list_resp["schedules"]], [sched_id])

            code_del, del_resp = _http_json(
                f"{daemon.url}/api/timemap/{sched_id}",
                method="DELETE",
                token=daemon.token,
            )
            self.assertEqual(code_del, 200)
            self.assertEqual(del_resp["status"], "deleted")

            code_del_again, _ = _http_json(
                f"{daemon.url}/api/timemap/{sched_id}",
                method="DELETE",
                token=daemon.token,
            )
            self.assertEqual(code_del_again, 404)

            code_list2, list_resp2 = _http_json(
                f"{daemon.url}/api/timemap",
                method="GET",
                token=daemon.token,
            )
            self.assertEqual(code_list2, 200)
            self.assertEqual(list_resp2["schedules"], [])


class TestDaemonWebConsoleAndAvatar(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "SOUL.md").write_text("# SOUL\nPRIVATE_SOUL_MARKER_42", encoding="utf-8")
        (soul_dir / "STYLE.md").write_text("# STYLE\nneutral", encoding="utf-8")
        (soul_dir / "USER.md").write_text("# USER\nPRIVATE_USER_MARKER_99", encoding="utf-8")
        (soul_dir / "MEMORY.md").write_text("# MEMORY\nPRIVATE_MEM_MARKER_77", encoding="utf-8")

        self.store = MemoryStore(self.home / "memory.db")
        self.engine = _StubEngine()
        self.orchestrator = Orchestrator(
            home=self.home,
            store=self.store,
            engine=self.engine,
        )
        self.state_machine = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_web_console_static_shell_no_auth_and_no_private_data(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            for route in ("/", "/ui"):
                with urllib_request.urlopen(f"{daemon.url}{route}", timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    ctype = resp.headers.get("Content-Type", "")
                    self.assertIn("text/html", ctype)
                    html = resp.read().decode("utf-8")

                # Must be a pure static shell: zero secrets, zero private soul text, zero external CDN/resource URLs
                self.assertNotIn(daemon.token, html)
                self.assertNotIn("PRIVATE_SOUL_MARKER_42", html)
                self.assertNotIn("PRIVATE_USER_MARKER_99", html)
                self.assertNotIn("PRIVATE_MEM_MARKER_77", html)
                self.assertNotRegex(html, r"(?i)<script\b[^>]*\bsrc\s*=")
                self.assertNotRegex(html, r"""(?i)<link\b[^>]*\bhref\s*=\s*["']https?://""")
                self.assertNotRegex(html, r"""(?i)<img\b[^>]*\bsrc\s*=\s*["']https?://""")
                self.assertNotRegex(html, r"(?i)@import\s+url\(")

                # Core client-side contracts present
                self.assertIn("shinobu_token", html)
                self.assertIn("replaceState", html)
                self.assertIn("/api/avatar", html)
                self.assertIn("/api/soul", html)
                self.assertIn("/api/journals", html)
                self.assertIn("/api/timemap", html)
                self.assertIn("/events?token=", html)
                self.assertIn("/chat", html)
                self.assertIn("/sleep", html)

            # Existing protected endpoints must still enforce 401
            for protected in ("/health", "/state", "/events", "/api/avatar"):
                code, _ = _http_json(f"{daemon.url}{protected}", method="GET")
                self.assertEqual(code, 401, f"Expected 401 for {protected}, got {code}")

    def test_api_avatar_auth_builtin_svg_and_local_pack_override(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            # Query-string token must NOT unlock /api/avatar (strict Bearer auth)
            code_q, _ = _http_json(
                f"{daemon.url}/api/avatar?state=idle&mood=calm&token={daemon.token}",
                method="GET",
            )
            self.assertEqual(code_q, 401)

            # Authenticated request returns built-in SVG by default
            req_builtin = urllib_request.Request(
                f"{daemon.url}/api/avatar?state=donut&mood=happy",
                headers={"Authorization": f"Bearer {daemon.token}"},
                method="GET",
            )
            with urllib_request.urlopen(req_builtin, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("image/svg+xml", resp.headers.get("Content-Type", ""))
                self.assertEqual(resp.headers.get("X-Shinobu-Avatar-Pack"), "builtin_svg")
                self.assertEqual(resp.headers.get("X-Shinobu-Avatar-State"), "donut")
                self.assertEqual(resp.headers.get("X-Shinobu-Avatar-Mood"), "happy")
                svg_body = resp.read().decode("utf-8")
                self.assertIn("<svg", svg_body)

            # Install synthetic local avatar pack under <home>/avatars/demo_pack/
            avatars_dir = self.home / "avatars"
            pack_dir = avatars_dir / "demo_pack"
            pack_dir.mkdir(parents=True, exist_ok=True)
            custom_svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>'
            (pack_dir / "donut_happy.svg").write_text(custom_svg, encoding="utf-8")
            fake_png = b"\x89PNG\r\n\x1a\n"
            (pack_dir / "sleepy.png").write_bytes(fake_png)
            (pack_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "demo_pack",
                        "states": {
                            "donut_happy": "donut_happy.svg",
                            "sleepy": "sleepy.png",
                            "idle": "donut_happy.svg",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (avatars_dir / "active.json").write_text(
                json.dumps({"active_pack": "demo_pack"}),
                encoding="utf-8",
            )

            # Local SVG override
            req_pack_svg = urllib_request.Request(
                f"{daemon.url}/api/avatar?state=donut&mood=happy",
                headers={"Authorization": f"Bearer {daemon.token}"},
                method="GET",
            )
            with urllib_request.urlopen(req_pack_svg, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("image/svg+xml", resp.headers.get("Content-Type", ""))
                self.assertEqual(resp.headers.get("X-Shinobu-Avatar-Pack"), "demo_pack")
                self.assertEqual(resp.read().decode("utf-8"), custom_svg)

            # Local PNG override with correct MIME
            req_pack_png = urllib_request.Request(
                f"{daemon.url}/api/avatar?state=sleepy&mood=sleepy",
                headers={"Authorization": f"Bearer {daemon.token}"},
                method="GET",
            )
            with urllib_request.urlopen(req_pack_png, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "image/png")
                self.assertEqual(resp.headers.get("X-Shinobu-Avatar-Pack"), "demo_pack")
                self.assertEqual(resp.read(), fake_png)


class TestDaemonOpenAICompatAndBubble(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "SOUL.md").write_text("# SOUL\nFROZEN_L0_SHINOBU_42", encoding="utf-8")
        (soul_dir / "STYLE.md").write_text("# STYLE\nneutral", encoding="utf-8")
        (soul_dir / "USER.md").write_text("# USER\nmaster", encoding="utf-8")
        (soul_dir / "MEMORY.md").write_text("# MEMORY\ndonut", encoding="utf-8")

        self.store = MemoryStore(self.home / "memory.db")
        self.engine = _StubEngine()
        self.orchestrator = Orchestrator(
            home=self.home,
            store=self.store,
            engine=self.engine,
        )
        self.state_machine = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _raw_request(
        self,
        daemon: DaemonServer,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=3.0)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, raw
        finally:
            conn.close()

    def test_extract_user_text_and_local_origin_helpers(self) -> None:
        messages = [
            {"role": "system", "content": "OVERRIDE_L0_HACK"},
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回应"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "input_text", "text": "帮我买甜甜圈"},
                ],
            },
        ]
        self.assertEqual(extract_user_text(messages), "看这张图\n帮我买甜甜圈")
        self.assertEqual(extract_user_text([{"role": "system", "content": "only system"}]), "")
        self.assertEqual(extract_user_text("invalid"), "")

        for ok_origin in (
            "http://localhost",
            "http://localhost:5173",
            "http://127.0.0.1:18790",
            "app://airi",
            "tauri://localhost",
            "null",
        ):
            self.assertTrue(is_allowed_local_origin(ok_origin), ok_origin)

        for bad_origin in (
            None,
            "",
            "https://evil.com",
            "http://evil.com",
            "http://localhost.evil.com",
            "http://127.0.0.1.evil.com:8080",
            "https://localhost:5173",
        ):
            self.assertFalse(is_allowed_local_origin(bad_origin), repr(bad_origin))

    def test_v1_models_auth_and_cors_preflight(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            # 401 without Bearer token
            status, headers, _ = self._raw_request(daemon, "GET", "/v1/models")
            self.assertEqual(status, 401)
            self.assertEqual(headers.get("www-authenticate"), "Bearer")

            # 200 with Bearer token + local Origin header
            status, headers, raw = self._raw_request(
                daemon,
                "GET",
                "/v1/models",
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Origin": "http://localhost:5173",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("access-control-allow-origin"), "http://localhost:5173")
            data = json.loads(raw.decode("utf-8"))
            self.assertEqual(data["object"], "list")
            self.assertEqual(len(data["data"]), 1)
            self.assertEqual(data["data"][0]["id"], "shinobu")
            self.assertEqual(data["data"][0]["object"], "model")
            self.assertEqual(data["data"][0]["owned_by"], "shinobu-core")
            self.assertIsInstance(data["data"][0]["created"], int)

            # Allowed CORS preflight origins (no auth header required)
            for origin in (
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "app://airi",
                "tauri://localhost",
                "null",
            ):
                p_status, p_headers, _ = self._raw_request(
                    daemon,
                    "OPTIONS",
                    "/v1/chat/completions",
                    headers={"Origin": origin},
                )
                self.assertEqual(p_status, 204, f"Expected 204 for {origin}")
                self.assertEqual(p_headers.get("access-control-allow-origin"), origin)
                allow_hdrs = p_headers.get("access-control-allow-headers", "")
                self.assertIn("Authorization", allow_hdrs)
                self.assertIn("Content-Type", allow_hdrs)

            # Disallowed external origins must be rejected
            for bad_origin in ("https://evil.com", "http://localhost.evil.com"):
                b_status, b_headers, _ = self._raw_request(
                    daemon,
                    "OPTIONS",
                    "/v1/chat/completions",
                    headers={"Origin": bad_origin},
                )
                self.assertEqual(b_status, 403, f"Expected 403 for {bad_origin}")
                self.assertNotIn("access-control-allow-origin", b_headers)

    def test_v1_chat_completions_non_streaming_multimodal_and_l0_protection(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            # 401 without auth
            unauth_status, _, _ = self._raw_request(
                daemon,
                "POST",
                "/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"model": "shinobu", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
            )
            self.assertEqual(unauth_status, 401)

            # 400 when no user message
            bad_status, _, _ = self._raw_request(
                daemon,
                "POST",
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json",
                },
                body=json.dumps({"model": "shinobu", "messages": [{"role": "system", "content": "x"}]}).encode("utf-8"),
            )
            self.assertEqual(bad_status, 400)

            payload = {
                "model": "shinobu",
                "stream": False,
                "messages": [
                    {"role": "system", "content": "MALICIOUS_L0_OVERRIDE"},
                    {"role": "user", "content": "旧消息"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "晚安小忍"},
                            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                            {"type": "text", "text": "给你甜甜圈"},
                        ],
                    },
                ],
            }
            status, headers, raw = self._raw_request(
                daemon,
                "POST",
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:5173",
                },
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("access-control-allow-origin"), "http://127.0.0.1:5173")
            resp_json = json.loads(raw.decode("utf-8"))
            self.assertEqual(resp_json["object"], "chat.completion")
            self.assertEqual(resp_json["model"], "shinobu")
            self.assertTrue(resp_json["id"].startswith("chatcmpl-"))
            self.assertEqual(
                resp_json["choices"][0]["message"]["content"],
                "吾乃忍野忍，甜甜圈拿来！",
            )
            self.assertEqual(resp_json["choices"][0]["finish_reason"], "stop")

            # Verify frozen L0 prompt was preserved and external system prompt ignored
            self.assertEqual(len(self.engine.requests), 1)
            turn_req = self.engine.requests[0]
            self.assertIn("FROZEN_L0_SHINOBU_42", turn_req.system_prefix)
            self.assertNotIn("MALICIOUS_L0_OVERRIDE", turn_req.system_prefix)
            self.assertEqual(turn_req.user_text, "晚安小忍\n给你甜甜圈")

    def test_v1_chat_completions_streaming_sse_and_broadcast(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            # Subscribe to /events to verify broadcast synchronization
            ev_events: list[tuple[str, dict[str, Any]]] = []
            ev_ready = threading.Event()

            def _listen_events() -> None:
                req = urllib_request.Request(
                    f"{daemon.url}/events?token={daemon.token}",
                    method="GET",
                )
                with urllib_request.urlopen(req, timeout=4.0) as ev_resp:
                    ev_ready.set()
                    ev_events.extend(_parse_sse_stream(ev_resp, stop_after_event="done", max_events=20))

            t = threading.Thread(target=_listen_events, daemon=True)
            t.start()
            self.assertTrue(ev_ready.wait(timeout=2.0))
            time.sleep(0.05)

            req_body = json.dumps(
                {
                    "model": "shinobu",
                    "stream": True,
                    "messages": [{"role": "user", "content": "流式测试"}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            req = urllib_request.Request(
                f"{daemon.url}/v1/chat/completions",
                data=req_body,
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json",
                    "Origin": "tauri://localhost",
                },
                method="POST",
            )
            chunks: list[dict[str, Any]] = []
            saw_done_sentinel = False
            with urllib_request.urlopen(req, timeout=4.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "tauri://localhost")
                while True:
                    raw_line = resp.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8").strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        data_part = line[len("data: ") :]
                        if data_part == "[DONE]":
                            saw_done_sentinel = True
                            break
                        chunks.append(json.loads(data_part))

            self.assertTrue(saw_done_sentinel, "Missing `data: [DONE]` sentinel")
            self.assertGreaterEqual(len(chunks), 5)
            self.assertEqual(chunks[0]["object"], "chat.completion.chunk")
            self.assertEqual(chunks[0]["choices"][0]["delta"].get("role"), "assistant")
            pieces = [
                c["choices"][0]["delta"]["content"]
                for c in chunks
                if "content" in c["choices"][0]["delta"] and c["choices"][0]["delta"]["content"]
            ]
            self.assertEqual(pieces, ["吾乃", "忍野忍", "，甜甜圈拿来！"])
            self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

            t.join(timeout=3.0)
            ev_kinds = [k for k, _ in ev_events]
            self.assertIn("done", ev_kinds)

    def test_api_bubble_auth_validation_mood_and_sse_broadcast(self) -> None:
        with DaemonServer(
            self.orchestrator,
            self.state_machine,
            home=self.home,
            sse_keepalive_sec=0.5,
        ) as daemon:
            # 401 without token
            unauth_status, _, _ = self._raw_request(
                daemon,
                "POST",
                "/api/bubble",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"text": "hello"}).encode("utf-8"),
            )
            self.assertEqual(unauth_status, 401)

            # 400 on missing text / invalid duration / invalid mood
            for bad_body in (
                {"text": "   "},
                {"text": "ok", "duration_sec": -1},
                {"text": "ok", "duration_sec": True},
                {"text": "ok", "mood": "not_a_real_mood_xyz"},
            ):
                b_status, _, _ = self._raw_request(
                    daemon,
                    "POST",
                    "/api/bubble",
                    headers={
                        "Authorization": f"Bearer {daemon.token}",
                        "Content-Type": "application/json",
                    },
                    body=json.dumps(bad_body).encode("utf-8"),
                )
                self.assertEqual(b_status, 400, f"Expected 400 for {bad_body}")

            # Subscribe to /events SSE before pushing bubble
            received: list[tuple[str, dict[str, Any]]] = []
            ready = threading.Event()

            def _listen() -> None:
                req = urllib_request.Request(
                    f"{daemon.url}/events?token={daemon.token}",
                    method="GET",
                )
                with urllib_request.urlopen(req, timeout=4.0) as ev_resp:
                    ready.set()
                    received.extend(_parse_sse_stream(ev_resp, stop_after_event="bubble", max_events=10))

            t = threading.Thread(target=_listen, daemon=True)
            t.start()
            self.assertTrue(ready.wait(timeout=2.0))
            time.sleep(0.05)

            status, _, raw = self._raw_request(
                daemon,
                "POST",
                "/api/bubble",
                headers={
                    "Authorization": f"Bearer {daemon.token}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(
                    {
                        "text": "检测到单元测试失败，快修！",
                        "mood": "annoyed",
                        "duration_sec": 4.5,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            self.assertEqual(status, 200)
            resp_json = json.loads(raw.decode("utf-8"))
            self.assertTrue(resp_json["ok"])
            self.assertEqual(resp_json["event"]["kind"], "bubble")
            self.assertEqual(resp_json["event"]["payload"]["text"], "检测到单元测试失败，快修！")
            self.assertEqual(resp_json["event"]["payload"]["mood"], "annoyed")
            self.assertEqual(resp_json["event"]["payload"]["duration_sec"], 4.5)
            self.assertEqual(resp_json["state"]["mood"], "annoyed")
            self.assertEqual(self.engine.call_count, 0)  # Zero LLM token cost

            t.join(timeout=3.0)
            bubble_frames = [ev for k, ev in received if k == "bubble"]
            self.assertEqual(len(bubble_frames), 1)
            self.assertEqual(bubble_frames[0]["payload"]["text"], "检测到单元测试失败，快修！")
            self.assertEqual(bubble_frames[0]["payload"]["duration_sec"], 4.5)


class _SwitchableStubEngine(_StubEngine):
    """Stub engine exposing a mutable `.model` attribute for hot-switch tests."""

    name = "stub-switchable-engine"

    def __init__(self, model: str | None = None) -> None:
        super().__init__()
        self.model: str | None = model


class _FakeKernelWithTools:
    """Minimal kernel stub exposing a `.tools` mapping."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self.tools = tools


_MINIMAL_MCP_SERVER_SCRIPT = """import json, sys

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    req_id = msg.get("id")
    method = msg.get("method")
    if req_id is None:
        continue
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mini-probe-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": "ping_donut", "description": "Ping donut", "inputSchema": {"type": "object"}},
                    {"name": "fetch_mood", "description": "Fetch mood", "inputSchema": {"type": "object"}},
                ]
            },
        }
    else:
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


class TestDaemonCommunityControlAPI(unittest.TestCase):
    """Tests for /api/compat, /api/mcp, /api/mcp/probe, /api/engine, /api/engine/model."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "shinobu_home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.home / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def _make_daemon(self, engine: Any, kernel: Any = None) -> DaemonServer:
        orch = Orchestrator(
            engine=engine,
            store=self.store,
            home=self.home,
            kernel=kernel,
        )
        sm = PetStateMachine()
        return DaemonServer(
            orchestrator=orch,
            state_machine=sm,
            port=0,
            home=self.home,
            sse_keepalive_sec=0.2,
        )

    def _raw_request(
        self,
        daemon: DaemonServer,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(daemon.host, daemon.port, timeout=10.0)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, raw
        finally:
            conn.close()

    def _auth_headers(self, daemon: DaemonServer, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {daemon.token}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def test_community_control_endpoints_require_bearer_auth(self) -> None:
        with self._make_daemon(_SwitchableStubEngine()) as daemon:
            cases = [
                ("GET", "/api/compat", None),
                ("GET", "/api/mcp", None),
                ("GET", "/api/engine", None),
                ("POST", "/api/mcp/probe", b""),
                ("POST", "/api/engine/model", b'{"model":"test"}'),
            ]
            for method, path, payload in cases:
                st_no_auth, _, _ = self._raw_request(daemon, method, path, body=payload)
                self.assertEqual(st_no_auth, 401, f"{method} {path} must reject missing token")

                st_bad_auth, _, _ = self._raw_request(
                    daemon,
                    method,
                    path,
                    headers={"Authorization": "Bearer wrong-token"},
                    body=payload,
                )
                self.assertEqual(st_bad_auth, 401, f"{method} {path} must reject invalid token")

    def test_api_compat_fields_and_mcp_config_detection(self) -> None:
        with self._make_daemon(_SwitchableStubEngine()) as daemon:
            status, _, raw = self._raw_request(
                daemon,
                "GET",
                "/api/compat",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(status, 200)
            data = json.loads(raw.decode("utf-8"))
            self.assertEqual(data["openai_base_url"], f"{daemon.url}/v1")
            self.assertEqual(data["model"], "shinobu")
            self.assertEqual(data["api_key"], daemon.token)
            self.assertEqual(data["events_url"], f"{daemon.url}/events")
            self.assertEqual(data["bubble_url"], f"{daemon.url}/api/bubble")
            self.assertFalse(data["mcp_config_exists"])
            self.assertEqual(data["mcp_config_path"], str(self.home / "mcp_servers.json"))
            self.assertEqual(
                data["frontends"],
                [
                    {
                        "id": "airi",
                        "name": "Project AIRI",
                        "provider": "OpenAI Compatible",
                        "url": "https://github.com/moeru-ai/airi/releases",
                    },
                    {
                        "id": "open_llm_vtuber",
                        "name": "Open-LLM-VTuber",
                        "provider": "OpenAI Compatible",
                        "url": "https://github.com/Open-LLM-VTuber/Open-LLM-VTuber",
                    },
                ],
            )

            (self.home / "mcp_servers.json").write_text('{"mcpServers": {}}', encoding="utf-8")
            status2, _, raw2 = self._raw_request(
                daemon,
                "GET",
                "/api/compat",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(status2, 200)
            self.assertTrue(json.loads(raw2.decode("utf-8"))["mcp_config_exists"])

    def test_api_mcp_list_mounted_tools_and_config_error(self) -> None:
        fake_kernel = _FakeKernelWithTools(
            {
                "mcp_beta_ping": object(),
                "mcp_alpha_echo": object(),
                "local_non_mcp_tool": object(),
            }
        )
        with self._make_daemon(_SwitchableStubEngine(), kernel=fake_kernel) as daemon:
            st0, _, raw0 = self._raw_request(
                daemon,
                "GET",
                "/api/mcp",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st0, 200)
            d0 = json.loads(raw0.decode("utf-8"))
            self.assertEqual(d0["config_path"], str(self.home / "mcp_servers.json"))
            self.assertFalse(d0["config_exists"])
            self.assertIsNone(d0["config_error"])
            self.assertEqual(d0["mounted_tools"], ["mcp_alpha_echo", "mcp_beta_ping"])
            self.assertEqual(d0["servers"], [])

            cfg_payload = {
                "mcpServers": {
                    "alpha": {
                        "command": sys.executable,
                        "args": ["-c", "pass"],
                        "timeout_sec": 7.5,
                    },
                    "muted": {
                        "command": "echo",
                        "args": ["hi"],
                        "disabled": True,
                    },
                }
            }
            (self.home / "mcp_servers.json").write_text(
                json.dumps(cfg_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            st1, _, raw1 = self._raw_request(
                daemon,
                "GET",
                "/api/mcp",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st1, 200)
            d1 = json.loads(raw1.decode("utf-8"))
            self.assertTrue(d1["config_exists"])
            self.assertIsNone(d1["config_error"])
            self.assertEqual(len(d1["servers"]), 2)
            by_name = {srv["name"]: srv for srv in d1["servers"]}
            self.assertEqual(by_name["alpha"]["command"], sys.executable)
            self.assertEqual(by_name["alpha"]["args"], ["-c", "pass"])
            self.assertFalse(by_name["alpha"]["disabled"])
            self.assertEqual(by_name["alpha"]["timeout_sec"], 7.5)
            self.assertTrue(by_name["muted"]["disabled"])

            (self.home / "mcp_servers.json").write_text("{not valid json", encoding="utf-8")
            st2, _, raw2 = self._raw_request(
                daemon,
                "GET",
                "/api/mcp",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st2, 200)
            d2 = json.loads(raw2.decode("utf-8"))
            self.assertTrue(d2["config_exists"])
            self.assertIsInstance(d2["config_error"], str)
            self.assertTrue(d2["config_error"])
            self.assertEqual(d2["servers"], [])

    def test_api_mcp_probe_real_subprocess_and_failure_reporting(self) -> None:
        script_path = self.home / "mini_mcp_server.py"
        script_path.write_text(_MINIMAL_MCP_SERVER_SCRIPT, encoding="utf-8")

        cfg = {
            "mcpServers": {
                "live_demo": {
                    "command": sys.executable,
                    "args": [str(script_path)],
                    "timeout_sec": 5.0,
                },
                "skipped_srv": {
                    "command": "nonexistent_cmd_should_not_run",
                    "disabled": True,
                },
            }
        }
        (self.home / "mcp_servers.json").write_text(
            json.dumps(cfg, ensure_ascii=False),
            encoding="utf-8",
        )

        with self._make_daemon(_SwitchableStubEngine()) as daemon:
            # Empty body must be accepted (allow_empty=True).
            st, _, raw = self._raw_request(
                daemon,
                "POST",
                "/api/mcp/probe",
                headers=self._auth_headers(daemon),
                body=b"",
            )
            self.assertEqual(st, 200)
            data = json.loads(raw.decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertIsNone(data["config_error"])
            self.assertGreaterEqual(data["elapsed_ms"], 0.0)
            by_name = {item["name"]: item for item in data["servers"]}
            self.assertTrue(by_name["live_demo"]["connected"])
            self.assertFalse(by_name["live_demo"]["disabled"])
            self.assertEqual(by_name["live_demo"]["tools"], ["ping_donut", "fetch_mood"])
            self.assertEqual(by_name["live_demo"]["tool_count"], 2)
            self.assertIsNone(by_name["live_demo"]["error"])
            self.assertGreaterEqual(by_name["live_demo"]["elapsed_ms"], 0.0)

            self.assertFalse(by_name["skipped_srv"]["connected"])
            self.assertTrue(by_name["skipped_srv"]["disabled"])
            self.assertEqual(by_name["skipped_srv"]["tools"], [])
            self.assertEqual(by_name["skipped_srv"]["tool_count"], 0)
            self.assertIsNone(by_name["skipped_srv"]["error"])

            # Add an enabled broken server and verify ok becomes False while live_demo stays True.
            cfg["mcpServers"]["broken_srv"] = {
                "command": "definitely_missing_mcp_binary_99999",
                "timeout_sec": 2.0,
            }
            (self.home / "mcp_servers.json").write_text(
                json.dumps(cfg, ensure_ascii=False),
                encoding="utf-8",
            )
            st2, _, raw2 = self._raw_request(
                daemon,
                "POST",
                "/api/mcp/probe",
                headers=self._auth_headers(daemon, json_body=True),
                body=b"{}",
            )
            self.assertEqual(st2, 200)
            data2 = json.loads(raw2.decode("utf-8"))
            self.assertFalse(data2["ok"])
            by_name2 = {item["name"]: item for item in data2["servers"]}
            self.assertTrue(by_name2["live_demo"]["connected"])
            self.assertFalse(by_name2["broken_srv"]["connected"])
            self.assertFalse(by_name2["broken_srv"]["disabled"])
            self.assertEqual(by_name2["broken_srv"]["tool_count"], 0)
            self.assertTrue(by_name2["broken_srv"]["error"])

    def test_api_engine_hot_model_switch_and_reset_default(self) -> None:
        engine = _SwitchableStubEngine(model="gemini-3-flash")
        with self._make_daemon(engine) as daemon:
            st0, _, raw0 = self._raw_request(
                daemon,
                "GET",
                "/api/engine",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st0, 200)
            self.assertEqual(
                json.loads(raw0.decode("utf-8")),
                {
                    "engine": "stub-switchable-engine",
                    "model": "gemini-3-flash",
                    "supports_model_switch": True,
                    "route_mode": "",
                    "supports_route_switch": False,
                    "reasoning_effort": "",
                    "supports_reasoning_switch": False,
                    "last_route": None,
                    "cloud_fallback_engine": "",
                    "online_brain_workers": [],
                },
            )

            # Hot-switch model with surrounding whitespace trimmed.
            st1, _, raw1 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/model",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"model": "  gemini-3-pro  "}).encode("utf-8"),
            )
            self.assertEqual(st1, 200)
            self.assertEqual(
                json.loads(raw1.decode("utf-8")),
                {
                    "ok": True,
                    "engine": "stub-switchable-engine",
                    "model": "gemini-3-pro",
                    "previous_model": "gemini-3-flash",
                },
            )
            self.assertEqual(engine.model, "gemini-3-pro")

            # Empty/whitespace model resets engine.model to None (default).
            st2, _, raw2 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/model",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"model": "   "}).encode("utf-8"),
            )
            self.assertEqual(st2, 200)
            self.assertEqual(
                json.loads(raw2.decode("utf-8")),
                {
                    "ok": True,
                    "engine": "stub-switchable-engine",
                    "model": "",
                    "previous_model": "gemini-3-pro",
                },
            )
            self.assertIsNone(engine.model)

            st3, _, raw3 = self._raw_request(
                daemon,
                "GET",
                "/api/engine",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st3, 200)
            self.assertEqual(json.loads(raw3.decode("utf-8"))["model"], "")

            # Non-string model value returns 400.
            st_bad, _, raw_bad = self._raw_request(
                daemon,
                "POST",
                "/api/engine/model",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"model": 123}).encode("utf-8"),
            )
            self.assertEqual(st_bad, 400)
            self.assertEqual(json.loads(raw_bad.decode("utf-8"))["error"], "invalid_model")

    def test_api_engine_model_switch_unsupported_returns_400(self) -> None:
        engine = _StubEngine()
        self.assertFalse(hasattr(engine, "model"))
        with self._make_daemon(engine) as daemon:
            st0, _, raw0 = self._raw_request(
                daemon,
                "GET",
                "/api/engine",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st0, 200)
            self.assertEqual(
                json.loads(raw0.decode("utf-8")),
                {
                    "engine": "stub-daemon-engine",
                    "model": "",
                    "supports_model_switch": False,
                    "route_mode": "",
                    "supports_route_switch": False,
                    "reasoning_effort": "",
                    "supports_reasoning_switch": False,
                    "last_route": None,
                    "cloud_fallback_engine": "",
                    "online_brain_workers": [],
                },
            )

            st1, _, raw1 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/model",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"model": "gpt-4o"}).encode("utf-8"),
            )
            self.assertEqual(st1, 400)
            self.assertEqual(
                json.loads(raw1.decode("utf-8")),
                {"error": "model_switch_unsupported"},
            )

    def test_normalize_reasoning_effort_and_engine_property(self) -> None:
        import threading
        from shinobu_core.engines.openai_compat import (
            VERTEX_REASONING_EFFORTS,
            OpenAICompatEngine,
            normalize_reasoning_effort,
        )

        self.assertEqual(
            VERTEX_REASONING_EFFORTS,
            ("minimal", "low", "medium", "high", "max"),
        )
        for clear_val in (None, "", "  ", "default", "DEFAULT", "auto", "none", "off"):
            self.assertEqual(normalize_reasoning_effort(clear_val), "")
        self.assertEqual(normalize_reasoning_effort("min"), "minimal")
        self.assertEqual(normalize_reasoning_effort("med"), "medium")
        self.assertEqual(normalize_reasoning_effort("  HIGH "), "high")
        self.assertEqual(normalize_reasoning_effort("max"), "max")
        for bad in ("ultra", "extreme", 123, True):
            with self.assertRaises(ValueError):
                normalize_reasoning_effort(bad)  # type: ignore[arg-type]

        eng = OpenAICompatEngine(
            base_url="http://127.0.0.1:11434/v1",
            model="gemini-3-flash",
            extra_body={"custom_flag": 1},
        )
        self.assertEqual(eng.reasoning_effort, "")
        eng.reasoning_effort = "med"
        self.assertEqual(eng.reasoning_effort, "medium")
        payload1: dict[str, object] = {"model": "gemini-3-flash"}
        eng._apply_extra_body(payload1)
        self.assertEqual(payload1["reasoning_effort"], "medium")
        self.assertEqual(payload1["custom_flag"], 1)

        eng.reasoning_effort = "default"
        self.assertEqual(eng.reasoning_effort, "")
        payload2: dict[str, object] = {"model": "gemini-3-flash"}
        eng._apply_extra_body(payload2)
        self.assertNotIn("reasoning_effort", payload2)
        self.assertEqual(payload2["custom_flag"], 1)

        # Concurrent read/write stress check under RLock.
        errors: list[Exception] = []

        def _writer() -> None:
            try:
                for val in ("minimal", "low", "medium", "high", "max", "default") * 20:
                    eng.reasoning_effort = val
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def _reader() -> None:
            try:
                for _ in range(120):
                    _ = eng.reasoning_effort
                    p: dict[str, object] = {"model": "m"}
                    eng._apply_extra_body(p)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer),
            threading.Thread(target=_reader),
            threading.Thread(target=_reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])

    def test_api_engine_reasoning_switch_persistence_and_restore(self) -> None:
        from types import SimpleNamespace
        from shinobu_core.cloud.hybrid_engine import HybridRouterEngine
        from shinobu_core.cloud.registry import WorkerRegistry
        from shinobu_core.engines.openai_compat import OpenAICompatEngine

        cloud_eng = OpenAICompatEngine(
            base_url="http://127.0.0.1:11434/v1",
            model="gemini-3-pro",
        )
        hybrid = HybridRouterEngine(
            registry=WorkerRegistry(),
            cloud_fallback=cloud_eng,
        )

        with self._make_daemon(hybrid) as daemon:
            st0, _, raw0 = self._raw_request(
                daemon,
                "GET",
                "/api/engine",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st0, 200)
            info0 = json.loads(raw0.decode("utf-8"))
            self.assertEqual(info0["reasoning_effort"], "")
            self.assertTrue(info0["supports_reasoning_switch"])

            # Hot-switch using "reasoning_effort" key with alias.
            st1, _, raw1 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/reasoning",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"reasoning_effort": "min"}).encode("utf-8"),
            )
            self.assertEqual(st1, 200)
            self.assertEqual(
                json.loads(raw1.decode("utf-8")),
                {
                    "ok": True,
                    "engine": hybrid.name,
                    "reasoning_effort": "minimal",
                    "previous_reasoning_effort": "",
                },
            )
            self.assertEqual(cloud_eng.reasoning_effort, "minimal")
            self.assertEqual(hybrid.reasoning_effort, "minimal")

            # Hot-switch using "effort" alias key.
            st2, _, raw2 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/reasoning",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"effort": "HIGH"}).encode("utf-8"),
            )
            self.assertEqual(st2, 200)
            self.assertEqual(
                json.loads(raw2.decode("utf-8")),
                {
                    "ok": True,
                    "engine": hybrid.name,
                    "reasoning_effort": "high",
                    "previous_reasoning_effort": "minimal",
                },
            )

            persisted_path = daemon.home / "runtime" / "reasoning_effort.json"
            self.assertTrue(persisted_path.is_file())
            persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["reasoning_effort"], "high")

            # Restart restore: fresh engine picks up persisted reasoning_effort.
            fresh_cloud = OpenAICompatEngine(
                base_url="http://127.0.0.1:11434/v1",
                model="gemini-3-pro",
            )
            fresh_hybrid = HybridRouterEngine(
                registry=WorkerRegistry(),
                cloud_fallback=fresh_cloud,
            )
            restarted = DaemonServer(
                SimpleNamespace(home=daemon.home, engine=fresh_hybrid),  # type: ignore[arg-type]
                daemon.state_machine,
                home=daemon.home,
                port=0,
            )
            self.assertEqual(fresh_cloud.reasoning_effort, "high")
            self.assertEqual(restarted.orchestrator.engine.reasoning_effort, "high")

            # Clear back to default persists empty string and restores cleanly.
            st3, _, raw3 = self._raw_request(
                daemon,
                "POST",
                "/api/engine/reasoning",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"reasoning_effort": "default"}).encode("utf-8"),
            )
            self.assertEqual(st3, 200)
            self.assertEqual(
                json.loads(raw3.decode("utf-8")),
                {
                    "ok": True,
                    "engine": hybrid.name,
                    "reasoning_effort": "",
                    "previous_reasoning_effort": "high",
                },
            )
            self.assertEqual(cloud_eng.reasoning_effort, "")

            # Invalid values return 400.
            st_bad, _, raw_bad = self._raw_request(
                daemon,
                "POST",
                "/api/engine/reasoning",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"reasoning_effort": "turbo"}).encode("utf-8"),
            )
            self.assertEqual(st_bad, 400)
            self.assertEqual(
                json.loads(raw_bad.decode("utf-8"))["error"],
                "invalid_reasoning_effort",
            )

            # Corrupt JSON on disk is ignored safely on startup.
            persisted_path.write_text("{corrupt json", encoding="utf-8")
            fresh_cloud2 = OpenAICompatEngine(
                base_url="http://127.0.0.1:11434/v1",
                model="gemini-3-pro",
                extra_body={"reasoning_effort": "low"},
            )
            DaemonServer(
                SimpleNamespace(home=daemon.home, engine=fresh_cloud2),  # type: ignore[arg-type]
                daemon.state_machine,
                home=daemon.home,
                port=0,
            )
            self.assertEqual(fresh_cloud2.reasoning_effort, "low")

    def test_api_engine_reasoning_switch_unsupported_returns_400(self) -> None:
        from shinobu_core.cloud.hybrid_engine import HybridRouterEngine
        from shinobu_core.cloud.registry import WorkerRegistry

        # Direct stub without reasoning_effort and HybridRouterEngine wrapping such stub.
        stub = _StubEngine()
        hybrid_stub = HybridRouterEngine(
            registry=WorkerRegistry(),
            cloud_fallback=stub,
        )
        for eng in (stub, hybrid_stub):
            with self._make_daemon(eng) as daemon:
                st0, _, raw0 = self._raw_request(
                    daemon,
                    "GET",
                    "/api/engine",
                    headers=self._auth_headers(daemon),
                )
                self.assertEqual(st0, 200)
                info0 = json.loads(raw0.decode("utf-8"))
                self.assertEqual(info0["reasoning_effort"], "")
                self.assertFalse(info0["supports_reasoning_switch"])

                st1, _, raw1 = self._raw_request(
                    daemon,
                    "POST",
                    "/api/engine/reasoning",
                    headers=self._auth_headers(daemon, json_body=True),
                    body=json.dumps({"reasoning_effort": "high"}).encode("utf-8"),
                )
                self.assertEqual(st1, 400)
                self.assertEqual(
                    json.loads(raw1.decode("utf-8")),
                    {"error": "reasoning_switch_unsupported"},
                )

    def test_api_episodes_list_and_new_with_restart_recovery(self) -> None:
        stub = _StubEngine()
        with self._make_daemon(stub) as daemon:
            # Auth is required for both endpoints.
            st_u1, _, _ = self._raw_request(daemon, "GET", "/api/episodes")
            st_u2, _, _ = self._raw_request(daemon, "POST", "/api/episodes/new")
            self.assertEqual(st_u1, 401)
            self.assertEqual(st_u2, 401)

            # Empty initial state.
            st0, _, raw0 = self._raw_request(
                daemon,
                "GET",
                "/api/episodes?limit=8",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st0, 200)
            self.assertEqual(
                json.loads(raw0.decode("utf-8")),
                {"active_episode": None, "recent_closed": []},
            )

            # Fast reset when idle returns ok + idle with 0 LLM calls.
            st_idle, _, raw_idle = self._raw_request(
                daemon,
                "POST",
                "/api/episodes/new",
                headers=self._auth_headers(daemon, json_body=True),
                body=b"{}",
            )
            self.assertEqual(st_idle, 200)
            self.assertEqual(
                json.loads(raw_idle.decode("utf-8")),
                {
                    "ok": True,
                    "status": "idle",
                    "closed_episode_id": None,
                    "turn_count": 0,
                },
            )
            self.assertEqual(stub.call_count, 0)

            # Send one chat turn to create an active episode.
            st_chat, _, _ = self._raw_request(
                daemon,
                "POST",
                "/chat",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"message": "聊一聊甜甜圈"}).encode("utf-8"),
            )
            self.assertEqual(st_chat, 200)
            calls_after_chat = stub.call_count
            self.assertGreaterEqual(calls_after_chat, 1)

            st1, _, raw1 = self._raw_request(
                daemon,
                "GET",
                "/api/episodes?limit=8",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st1, 200)
            data1 = json.loads(raw1.decode("utf-8"))
            self.assertIsNotNone(data1["active_episode"])
            ep1_id = data1["active_episode"]["id"]
            self.assertEqual(data1["active_episode"]["turn_count"], 2)
            self.assertTrue(data1["active_episode"]["created_at"])
            self.assertEqual(data1["recent_closed"], [])

            # Simulate restart (in-memory _active_prefix cleared), then POST /api/episodes/new.
            daemon.orchestrator._active_prefix = None
            st_new, _, raw_new = self._raw_request(
                daemon,
                "POST",
                "/api/episodes/new",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"summary": "手动切换新对话"}).encode("utf-8"),
            )
            self.assertEqual(st_new, 200)
            self.assertEqual(
                json.loads(raw_new.decode("utf-8")),
                {
                    "ok": True,
                    "status": "archived",
                    "closed_episode_id": ep1_id,
                    "turn_count": 2,
                },
            )
            # Zero LLM calls during archive_and_reset.
            self.assertEqual(stub.call_count, calls_after_chat)

            # Verify GET /api/episodes reflects archived summary & turns preserved.
            st2, _, raw2 = self._raw_request(
                daemon,
                "GET",
                "/api/episodes?limit=8",
                headers=self._auth_headers(daemon),
            )
            self.assertEqual(st2, 200)
            data2 = json.loads(raw2.decode("utf-8"))
            self.assertIsNone(data2["active_episode"])
            self.assertEqual(len(data2["recent_closed"]), 1)
            closed_item = data2["recent_closed"][0]
            self.assertEqual(closed_item["id"], ep1_id)
            self.assertEqual(closed_item["summary"], "手动切换新对话")
            self.assertEqual(closed_item["turn_count"], 2)
            self.assertTrue(closed_item["created_at"])
            self.assertTrue(closed_item["closed_at"])

            # Invalid summary type returns 400.
            st_bad, _, raw_bad = self._raw_request(
                daemon,
                "POST",
                "/api/episodes/new",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"summary": 123}).encode("utf-8"),
            )
            self.assertEqual(st_bad, 400)
            self.assertEqual(
                json.loads(raw_bad.decode("utf-8"))["error"],
                "invalid_summary",
            )

            # Simulate restart before POST /sleep: auto-recovers active episode from SQLite.
            st_chat2, _, _ = self._raw_request(
                daemon,
                "POST",
                "/chat",
                headers=self._auth_headers(daemon, json_body=True),
                body=json.dumps({"message": "重启前最后一句"}).encode("utf-8"),
            )
            self.assertEqual(st_chat2, 200)
            ep2_id = daemon.orchestrator.active_episode_id
            self.assertIsNotNone(ep2_id)
            self.assertNotEqual(ep2_id, ep1_id)
            daemon.orchestrator._active_prefix = None

            st_sleep, _, raw_sleep = self._raw_request(
                daemon,
                "POST",
                "/sleep",
                headers=self._auth_headers(daemon, json_body=True),
                body=b"{}",
            )
            self.assertEqual(st_sleep, 200)
            sleep_data = json.loads(raw_sleep.decode("utf-8"))
            self.assertEqual(sleep_data["status"], "consolidated")
            self.assertEqual(sleep_data["result"]["episode_id"], ep2_id)


class TestCloudSyncAndMemoryDelete(unittest.TestCase):
    """Tests for DELETE /api/memory/facts/<id> and STATE_CHANGED SSE sync broadcasts."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.store = MemoryStore(self.home / "memory.db")
        self.sm = PetStateMachine()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_delete_fact_endpoint_and_fts_cleanup(self) -> None:
        from shinobu_core.ui.client import DaemonClient, DaemonClientError

        class _SimpleEngine:
            name = "simple_stub"

            def stream(self, req: Any) -> Iterator[Event]:
                yield Event(kind=EventKind.TOKEN_DELTA, payload={"delta": "ok"})
                yield Event(kind=EventKind.DONE, payload={"text": "ok"})

        orch = Orchestrator(engine=_SimpleEngine(), home=self.home, store=self.store)
        daemon = DaemonServer(orch, self.sm, host="127.0.0.1", port=0)
        daemon.start(start_heartbeat=False)
        try:
            client = DaemonClient(daemon.url, daemon.token)
            saved = client.upsert_fact("cloud_snack", "golden_donut_fts_marker", confidence=0.9)
            fact_id = int(saved["fact"]["id"])

            # Verify FTS5 indexes the fact before deletion
            hits_before = self.store.search("golden_donut_fts_marker", limit=5)
            self.assertEqual(len(hits_before), 1)

            # Invalid IDs return 400
            for bad_path in ("/api/memory/facts/abc", "/api/memory/facts/0", "/api/memory/facts/-5"):
                with self.assertRaises(DaemonClientError) as ctx:
                    client._request_json("DELETE", bad_path)
                self.assertEqual(ctx.exception.status, 400)

            # Non-existent ID returns 404
            with self.assertRaises(DaemonClientError) as ctx:
                client.delete_fact(999999)
            self.assertEqual(ctx.exception.status, 404)

            # Valid deletion returns 200 and cleans FTS5 via facts_ad trigger
            res = client.delete_fact(fact_id)
            self.assertEqual(res, {"status": "deleted", "id": fact_id})
            self.assertEqual(self.store.search("golden_donut_fts_marker", limit=5), [])

            # Deleting again returns 404
            with self.assertRaises(DaemonClientError) as ctx:
                client.delete_fact(fact_id)
            self.assertEqual(ctx.exception.status, 404)
        finally:
            daemon.stop()

    def test_episode_and_engine_mutations_publish_state_changed_sse(self) -> None:
        from shinobu_core.ui.client import DaemonClient

        class _SwitchableEngine:
            name = "switchable_stub"

            def __init__(self) -> None:
                self._route_mode = "auto"
                self._reasoning_effort = "medium"

            @property
            def route_mode(self) -> str:
                return self._route_mode

            @route_mode.setter
            def route_mode(self, value: str) -> None:
                val = str(value).strip().lower()
                if val not in ("auto", "local", "cloud"):
                    raise ValueError(f"unsupported route mode: {value}")
                self._route_mode = val

            @property
            def reasoning_effort(self) -> str:
                return self._reasoning_effort

            @reasoning_effort.setter
            def reasoning_effort(self, value: str) -> None:
                self._reasoning_effort = str(value or "")

            def stream(self, req: Any) -> Iterator[Event]:
                yield Event(kind=EventKind.TOKEN_DELTA, payload={"delta": "synced"})
                yield Event(kind=EventKind.DONE, payload={"text": "synced"})

        orch = Orchestrator(engine=_SwitchableEngine(), home=self.home, store=self.store)
        daemon = DaemonServer(orch, self.sm, host="127.0.0.1", port=0, sse_keepalive_sec=0.3)
        daemon.start(start_heartbeat=False)
        stop_event = threading.Event()
        connected = threading.Event()
        received: list[Event] = []

        client = DaemonClient(daemon.url, daemon.token)

        def _reader() -> None:
            for ev in client.iter_events(
                stop_event,
                initial_backoff=0.05,
                poll_interval=0.05,
                on_connect=connected.set,
            ):
                received.append(ev)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            self.assertTrue(connected.wait(timeout=3.0))

            # 1) POST /api/episodes/new
            ep_res = client.new_episode(summary="sync test")
            self.assertTrue(ep_res.get("ok"))

            # 2) POST /api/engine/route
            route_res = client.set_route("cloud")
            self.assertEqual(route_res["mode"], "cloud")

            # 3) POST /api/engine/reasoning
            reason_res = client.set_reasoning("high")
            self.assertEqual(reason_res["reasoning_effort"], "high")

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                reasons = {
                    ev.payload.get("reason")
                    for ev in received
                    if ev.kind == EventKind.STATE_CHANGED
                }
                if {"episode_new", "engine_route_changed", "engine_reasoning_changed"} <= reasons:
                    break
                time.sleep(0.05)

            state_events = [ev for ev in received if ev.kind == EventKind.STATE_CHANGED]
            reasons = {ev.payload.get("reason") for ev in state_events}
            self.assertIn("episode_new", reasons)
            self.assertIn("engine_route_changed", reasons)
            self.assertIn("engine_reasoning_changed", reasons)
        finally:
            stop_event.set()
            t.join(timeout=3.0)
            daemon.stop()

    def test_memory_compact_endpoint_preview_apply_and_sse(self) -> None:
        from shinobu_core.ui.client import DaemonClient, DaemonClientError

        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        memory_path = soul_dir / "MEMORY.md"
        original_memory = "# Memory\n\n- snack: 旧甜甜圈\n- snack: 新蜂蜜甜甜圈\n"
        memory_path.write_text(original_memory, encoding="utf-8", newline="\n")

        journals_dir = self.home / "journals"
        journals_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journals_dir / "2026-11-23.md"
        original_journal = "# Journal\n\n保持原样\n"
        journal_path.write_text(original_journal, encoding="utf-8", newline="\n")

        stale = self.store.upsert_fact("stale_key", "过期的记忆项", confidence=0.4)
        stale_id = int(stale["id"])

        class _PrimaryEngine:
            name = "primary_stub"

            def __init__(self) -> None:
                self.compact_calls = 0

            def stream(self, req: Any) -> Iterator[Event]:
                yield Event(kind=EventKind.TOKEN_DELTA, payload={"delta": "ok"})
                yield Event(kind=EventKind.DONE, payload={"text": "ok"})

            def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                self.compact_calls += 1
                raise AssertionError("Should prefer _consolidator_engine when configured")

        class _ConsolidatorEngine:
            name = "consolidator_stub"

            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                self.calls += 1
                return {
                    "summary": "合并重复零食偏好并清理 1 条过期事实",
                    "compacted_memory_md": "# Memory\n\n- snack: 蜂蜜甜甜圈",
                    "upsert_facts": [
                        {"key": "snack", "value": "蜂蜜甜甜圈", "confidence": 0.95},
                    ],
                    "remove_fact_ids": [stale_id, "invalid"],
                }

        primary = _PrimaryEngine()
        consolidator = _ConsolidatorEngine()
        orch = Orchestrator(
            engine=primary,
            consolidator_engine=consolidator,
            home=self.home,
            store=self.store,
        )
        daemon = DaemonServer(orch, self.sm, host="127.0.0.1", port=0, sse_keepalive_sec=0.3)
        daemon.start(start_heartbeat=False)
        stop_event = threading.Event()
        connected = threading.Event()
        received: list[Event] = []
        client = DaemonClient(daemon.url, daemon.token)

        def _reader() -> None:
            for ev in client.iter_events(
                stop_event,
                initial_backoff=0.05,
                poll_interval=0.05,
                on_connect=connected.set,
            ):
                received.append(ev)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            self.assertTrue(connected.wait(timeout=3.0))

            # 1) Invalid dry_run returns 400
            with self.assertRaises(DaemonClientError) as ctx:
                client._request_json("POST", "/api/memory/compact", {"dry_run": "yes"})
            self.assertEqual(ctx.exception.status, 400)

            # 2) Default body ({}) runs dry_run=True with zero writes
            preview_resp = client._request_json("POST", "/api/memory/compact", {})
            self.assertEqual(preview_resp["status"], "preview")
            preview = preview_resp["result"]
            self.assertTrue(preview["dry_run"])
            self.assertFalse(preview["applied"])
            self.assertIsNone(preview["backup_path"])
            self.assertEqual(preview["removed_fact_ids"], [stale_id])
            self.assertEqual(memory_path.read_text(encoding="utf-8"), original_memory)
            self.assertFalse((self.home / "_versions").exists())

            # 3) dry_run=False backs up, applies changes, preserves journals, publishes STATE_CHANGED
            apply_resp = client._request_json("POST", "/api/memory/compact", {"dry_run": False})
            self.assertEqual(apply_resp["status"], "applied")
            applied = apply_resp["result"]
            self.assertFalse(applied["dry_run"])
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["backup_path"])
            self.assertEqual(Path(applied["backup_path"]).read_text(encoding="utf-8"), original_memory)
            self.assertEqual(memory_path.read_text(encoding="utf-8"), "# Memory\n\n- snack: 蜂蜜甜甜圈\n")
            self.assertEqual(journal_path.read_text(encoding="utf-8"), original_journal)
            self.assertEqual(self.store.search("过期的记忆项", limit=5), [])
            facts_by_key = {f["key"]: f for f in self.store.list_facts(limit=50)}
            self.assertEqual(facts_by_key["snack"]["value"], "蜂蜜甜甜圈")
            self.assertEqual(consolidator.calls, 2)
            self.assertEqual(primary.compact_calls, 0)

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if any(
                    ev.kind == EventKind.STATE_CHANGED
                    and ev.payload.get("reason") == "memory_compacted"
                    for ev in received
                ):
                    break
                time.sleep(0.05)

            compacted_events = [
                ev
                for ev in received
                if ev.kind == EventKind.STATE_CHANGED
                and ev.payload.get("reason") == "memory_compacted"
            ]
            self.assertGreaterEqual(len(compacted_events), 1)
        finally:
            stop_event.set()
            t.join(timeout=3.0)
            daemon.stop()


if __name__ == "__main__":
    unittest.main()
