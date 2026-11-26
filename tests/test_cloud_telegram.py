# -*- coding: utf-8 -*-
"""Unit tests for the 4-layer isolated stdlib Telegram Bot bridge."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shinobu_core.cloud.approval import ApprovalGate
from shinobu_core.cloud.telegram_bridge import (
    FORWARD_BLOCKED_NOTICE,
    MEDIA_BLOCKED_NOTICE,
    TelegramApiError,
    TelegramBotBridge,
    filter_update,
)


class _MockTelegramServer:
    """In-process fake Telegram Bot API server for stdlib urllib verification."""

    def __init__(self, expected_token: str = "123456:TEST_TOKEN_SECRET") -> None:
        self.expected_token = expected_token
        self.lock = threading.RLock()
        self.requests: list[dict[str, Any]] = []
        self.queued_updates: list[dict[str, Any]] = []
        self.fail_get_updates_remaining: int = 0
        self.get_updates_calls: int = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3.0)

    def clear_requests(self) -> None:
        with self.lock:
            self.requests.clear()

    def methods_called(self) -> list[str]:
        with self.lock:
            return [r["method"] for r in self.requests]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                prefix = f"/bot{parent.expected_token}/"
                if not self.path.startswith(prefix):
                    self._send_json(401, {"ok": False, "description": "Unauthorized"})
                    return

                method = self.path[len(prefix):]
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b""
                content_type = self.headers.get("Content-Type", "")
                parsed_json: dict[str, Any] | None = None
                if "application/json" in content_type and raw_body:
                    parsed_json = json.loads(raw_body.decode("utf-8"))

                with parent.lock:
                    parent.requests.append(
                        {
                            "method": method,
                            "content_type": content_type,
                            "json": parsed_json,
                            "raw_body": raw_body,
                        }
                    )

                    if method == "getUpdates":
                        parent.get_updates_calls += 1
                        if parent.fail_get_updates_remaining > 0:
                            parent.fail_get_updates_remaining -= 1
                            self._send_json(
                                502, {"ok": False, "description": "Bad Gateway"}
                            )
                            return
                        offset = int((parsed_json or {}).get("offset", 0))
                        batch = [
                            u
                            for u in parent.queued_updates
                            if int(u.get("update_id", 0)) >= offset
                        ]
                        parent.queued_updates = [
                            u
                            for u in parent.queued_updates
                            if int(u.get("update_id", 0)) < offset
                        ]
                        self._send_json(200, {"ok": True, "result": batch})
                        return

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "message_id": 1001,
                            "method": method,
                        },
                    },
                )

            def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler


class TestTelegramIsolatedBridge(unittest.TestCase):
    OWNER_ID = 777001
    TOKEN = "123456:TEST_TOKEN_SECRET"

    def setUp(self) -> None:
        self.server = _MockTelegramServer(expected_token=self.TOKEN)
        self.server.start()
        self.chat_inputs: list[str] = []
        self.approval_calls: list[tuple[str, bool, str]] = []
        self.tts_inputs: list[str] = []
        self.tts_output: bytes | None = b"OggS\x00\x02fake-opus-payload"

        self.bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: {
                "uptime_sec": 42,
                "mode": "cloud",
                "api_token": "MUST_NOT_LEAK",
            },
            on_nodes=lambda: [
                {
                    "worker_id": "colab-gpu-1",
                    "kind": "tts",
                    "online": True,
                    "capabilities": ["tts", "ogg"],
                },
                {
                    "worker_id": "home-pc-1",
                    "kind": "edge",
                    "status": "online",
                    "capabilities": ["mcp"],
                },
            ],
            on_tts=self._on_tts,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
            http_timeout_sec=5.0,
            backoff_initial_sec=0.05,
            backoff_max_sec=0.1,
        )

    def tearDown(self) -> None:
        self.bridge.stop(timeout=3.0)
        self.server.close()

    def _on_chat(self, text: str) -> str:
        self.chat_inputs.append(text)
        return f"Echo: {text}"

    def _on_approval(self, approval_id: str, approved: bool, actor: str) -> bool:
        self.approval_calls.append((approval_id, approved, actor))
        return True

    def _on_tts(self, text: str) -> bytes | None:
        self.tts_inputs.append(text)
        return self.tts_output

    def _private_msg(self, chat_id: int | str | None = None, **extra: Any) -> dict[str, Any]:
        cid = self.OWNER_ID if chat_id is None else chat_id
        msg: dict[str, Any] = {
            "message_id": 10,
            "chat": {"id": cid, "type": "private"},
            "from": {"id": cid, "is_bot": False},
        }
        msg.update(extra)
        return {"update_id": 1, "message": msg}

    # --- Layer 1: Private chat only ---

    def test_layer1_non_private_chats_silently_dropped(self) -> None:
        non_private_updates = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": -1001, "type": "group"},
                    "from": {"id": self.OWNER_ID},
                    "text": "hello group",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "chat": {"id": -1002, "type": "supergroup"},
                    "from": {"id": self.OWNER_ID},
                    "text": "hello supergroup",
                },
            },
            {
                "update_id": 3,
                "channel_post": {
                    "message_id": 3,
                    "chat": {"id": -1003, "type": "channel"},
                    "text": "channel broadcast",
                },
            },
            {
                "update_id": 4,
                "callback_query": {
                    "id": "cb_group",
                    "from": {"id": self.OWNER_ID},
                    "message": {
                        "message_id": 4,
                        "chat": {"id": -1001, "type": "group"},
                    },
                    "data": "approve:apr_1",
                },
            },
            {
                "update_id": 5,
                "inline_query": {"id": "iq1", "from": {"id": self.OWNER_ID}, "query": "hi"},
            },
        ]

        for upd in non_private_updates:
            allowed, reason = filter_update(upd, self.OWNER_ID)
            self.assertFalse(allowed)
            self.assertEqual(reason, "non_private_chat")
            result = self.bridge.handle_update(upd)
            self.assertFalse(result["allowed"])
            self.assertEqual(result["action"], "dropped")

        self.assertEqual(self.chat_inputs, [])
        self.assertEqual(self.approval_calls, [])
        self.assertEqual(self.server.methods_called(), [])

    # --- Layer 2: Single-user allowlist ---

    def test_layer2_unauthorized_private_user_silently_dropped(self) -> None:
        stranger_updates = [
            self._private_msg(chat_id=999999, text="who are you"),
            self._private_msg(
                chat_id=999999,
                text="forwarded from stranger",
                forward_origin={"type": "user"},
            ),
            self._private_msg(
                chat_id=999999,
                photo=[{"file_id": "abc"}],
                caption="secret photo",
            ),
            {
                "update_id": 20,
                "callback_query": {
                    "id": "cb_stranger",
                    "from": {"id": 999999},
                    "message": {
                        "message_id": 20,
                        "chat": {"id": 999999, "type": "private"},
                    },
                    "data": "approve:apr_999",
                },
            },
            # Spoofed sender mismatch in private chat
            {
                "update_id": 21,
                "message": {
                    "message_id": 21,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "from": {"id": 999999},
                    "text": "spoofed sender",
                },
            },
        ]

        for upd in stranger_updates:
            allowed, reason = filter_update(upd, self.OWNER_ID)
            self.assertFalse(allowed)
            self.assertEqual(reason, "unauthorized_chat")
            result = self.bridge.handle_update(upd)
            self.assertFalse(result["allowed"])
            self.assertEqual(result["action"], "dropped")

        # Must remain completely silent (zero API calls)
        self.assertEqual(self.chat_inputs, [])
        self.assertEqual(self.approval_calls, [])
        self.assertEqual(self.server.methods_called(), [])

    # --- Layer 3: Forwarded messages blocked ---

    def test_layer3_forwarded_messages_blocked_with_notice(self) -> None:
        forward_variants = [
            ("forward_origin", {"type": "user", "date": 1700000000}),
            ("forward_date", 1700000000),
            ("forward_from", {"id": 555, "first_name": "Alice"}),
            ("forward_from_chat", {"id": -10099, "type": "channel"}),
        ]

        for field_name, field_value in forward_variants:
            self.server.clear_requests()
            upd = self._private_msg(
                text="sensitive forwarded text",
                **{field_name: field_value},
            )
            allowed, reason = filter_update(upd, self.OWNER_ID)
            self.assertFalse(allowed)
            self.assertEqual(reason, f"blocked_forward:{field_name}")

            res = self.bridge.handle_update(upd)
            self.assertFalse(res["allowed"])
            self.assertTrue(res["handled"])
            self.assertEqual(res["action"], "blocked_forward")
            self.assertEqual(self.server.methods_called(), ["sendMessage"])
            sent_text = self.server.requests[0]["json"]["text"]
            self.assertEqual(sent_text, FORWARD_BLOCKED_NOTICE)

        self.assertEqual(self.chat_inputs, [])
        self.assertNotIn("getFile", self.server.methods_called())

    # --- Layer 4: Zero-media blindfold shield ---

    def test_layer4_media_blocked_never_calls_getfile_or_llm(self) -> None:
        media_variants = [
            ("photo", [{"file_id": "photo_1", "width": 100, "height": 100}]),
            ("video", {"file_id": "video_1", "duration": 5}),
            ("animation", {"file_id": "anim_1", "duration": 2}),
            ("sticker", {"file_id": "sticker_1", "emoji": "🙂"}),
            ("document", {"file_id": "doc_1", "file_name": "secret.pdf"}),
            ("video_note", {"file_id": "vnote_1", "length": 240}),
        ]

        for field_name, field_value in media_variants:
            self.server.clear_requests()
            upd = self._private_msg(
                caption="caption must never reach LLM",
                **{field_name: field_value},
            )
            allowed, reason = filter_update(upd, self.OWNER_ID)
            self.assertFalse(allowed)
            self.assertEqual(reason, f"blocked_media:{field_name}")

            res = self.bridge.handle_update(upd)
            self.assertFalse(res["allowed"])
            self.assertTrue(res["handled"])
            self.assertEqual(res["action"], "blocked_media")
            self.assertEqual(self.server.methods_called(), ["sendMessage"])
            self.assertEqual(
                self.server.requests[0]["json"]["text"], MEDIA_BLOCKED_NOTICE
            )
            self.assertNotIn("getFile", self.server.methods_called())

        self.assertEqual(self.chat_inputs, [])

        # Hard physical guard against direct getFile calls
        with self.assertRaises(PermissionError):
            self.bridge._build_api_url("getFile")

    # --- Command dispatch ---

    def test_builtin_commands_status_nodes_voice(self) -> None:
        # /status with @BotName suffix
        res_status = self.bridge.handle_update(
            self._private_msg(text="/status@ShinobuCloudBot")
        )
        self.assertEqual(res_status["action"], "command_status")
        self.assertIn("uptime_sec: 42", res_status["reply"])
        self.assertNotIn("MUST_NOT_LEAK", res_status["reply"])

        # /nodes
        res_nodes = self.bridge.handle_update(self._private_msg(text="/nodes"))
        self.assertEqual(res_nodes["action"], "command_nodes")
        self.assertIn("colab-gpu-1", res_nodes["reply"])
        self.assertIn("home-pc-1", res_nodes["reply"])

        # /voice status / on / off
        self.assertFalse(self.bridge.voice_enabled)
        res_voice_query = self.bridge.handle_update(self._private_msg(text="/voice"))
        self.assertEqual(res_voice_query["action"], "command_voice")
        self.assertFalse(res_voice_query["voice_enabled"])

        res_voice_on = self.bridge.handle_update(self._private_msg(text="/voice on"))
        self.assertTrue(res_voice_on["voice_enabled"])
        self.assertTrue(self.bridge.voice_enabled)

        res_voice_off = self.bridge.handle_update(self._private_msg(text="/voice off"))
        self.assertFalse(res_voice_off["voice_enabled"])
        self.assertFalse(self.bridge.voice_enabled)

        # Built-in commands never enter on_chat
        self.assertEqual(self.chat_inputs, [])

    def test_builtin_help_command_and_aliases(self) -> None:
        # 1. /help default state (voice off)
        self.server.clear_requests()
        res_help = self.bridge.handle_update(self._private_msg(text="/help"))
        self.assertTrue(res_help["allowed"])
        self.assertTrue(res_help["handled"])
        self.assertEqual(res_help["action"], "command_help")
        reply = res_help["reply"]
        for expected in (
            "对话与记忆",
            "大脑与思考",
            "提醒与语音",
            "运维与帮助",
            "/session",
            "/sessions",
            "/archive",
            "/new",
            "/sleep",
            "/brain",
            "/mode",
            "/think",
            "/effort",
            "/remind",
            "/reminders",
            "/voice",
            "/status",
            "/nodes",
            "/help",
            "/commands",
            "当前：关闭",
        ):
            self.assertIn(expected, reply)
        send_reqs = [r for r in self.server.requests if r.get("method") == "sendMessage"]
        self.assertEqual(len(send_reqs), 1)
        self.assertIn("/help", send_reqs[0]["json"]["text"])

        # 2. /help@ShinobuCloudBot reflects toggled voice state (voice on)
        self.bridge.handle_update(self._private_msg(text="/voice on"))
        res_help_mention = self.bridge.handle_update(
            self._private_msg(text="/help@ShinobuCloudBot")
        )
        self.assertTrue(res_help_mention["allowed"])
        self.assertTrue(res_help_mention["handled"])
        self.assertEqual(res_help_mention["action"], "command_help")
        self.assertIn("当前：开启", res_help_mention["reply"])

        # 3. /commands alias
        res_commands = self.bridge.handle_update(self._private_msg(text="/commands"))
        self.assertTrue(res_commands["allowed"])
        self.assertTrue(res_commands["handled"])
        self.assertEqual(res_commands["action"], "command_help")
        self.assertIn("/help", res_commands["reply"])

        # 4. Non-whitelisted user sending /help is silently dropped
        self.server.clear_requests()
        res_stranger = self.bridge.handle_update(
            self._private_msg(chat_id=999999999, text="/help")
        )
        self.assertFalse(res_stranger["allowed"])
        self.assertFalse(res_stranger["handled"])
        self.assertEqual(self.server.requests, [])

        # Built-in /help never triggers on_chat
        self.assertEqual(self.chat_inputs, [])

    def test_brain_command_and_inline_keyboard_switch(self) -> None:
        state: dict[str, Any] = {
            "mode": "auto",
            "calls": [],
        }

        def _on_route(mode: str | None) -> dict[str, Any]:
            state["calls"].append(mode)
            if mode is None:
                return {
                    "ok": True,
                    "engine": "hybrid",
                    "route_mode": state["mode"],
                    "supports_route_switch": True,
                    "online_brain_workers": ["home_pc"],
                    "last_route": {"target": "remote", "worker_id": "home_pc", "elapsed_ms": 420},
                }
            prev = state["mode"]
            state["mode"] = mode
            return {
                "ok": True,
                "engine": "hybrid",
                "mode": mode,
                "previous_mode": prev,
                "online_brain_workers": ["home_pc"],
            }

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_route=_on_route,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        # 1. /brain without args queries current mode and attaches 3 inline buttons
        self.server.clear_requests()
        res_query = bridge.handle_update(self._private_msg(text="/brain"))
        self.assertEqual(res_query["action"], "command_brain")
        self.assertEqual(res_query["mode"], "auto")
        self.assertFalse(res_query["switched"])
        self.assertEqual(state["calls"], [None])
        send_req = self.server.requests[-1]
        self.assertEqual(send_req["method"], "sendMessage")
        self.assertIn("home_pc", send_req["json"]["text"])
        buttons = [
            btn
            for row in send_req["json"]["reply_markup"]["inline_keyboard"]
            for btn in row
        ]
        self.assertEqual(
            [(b["text"], b["callback_data"]) for b in buttons],
            [
                ("⚡ 自动混动", "brain:auto"),
                ("☁️ 云端 API", "brain:cloud"),
                ("💻 仅本机 AGY", "brain:local"),
            ],
        )

        # 2. /mode api and /brain agy aliases switch directly
        self.server.clear_requests()
        res_api = bridge.handle_update(self._private_msg(text="/mode api"))
        self.assertTrue(res_api["switched"])
        self.assertEqual(res_api["mode"], "cloud")
        res_agy = bridge.handle_update(self._private_msg(text="/brain agy"))
        self.assertTrue(res_agy["switched"])
        self.assertEqual(res_agy["mode"], "local")
        self.assertEqual(state["calls"], [None, "cloud", "local"])

        # 3. Invalid /brain argument returns usage hint without calling on_route
        res_bad = bridge.handle_update(self._private_msg(text="/brain invalid_xyz"))
        self.assertFalse(res_bad["switched"])
        self.assertEqual(res_bad["reason"], "invalid_mode")
        self.assertEqual(state["calls"], [None, "cloud", "local"])

        # 4. Inline keyboard callback brain:cloud answers toast and edits text keeping 3 buttons
        self.server.clear_requests()
        cb_update = {
            "update_id": 88,
            "callback_query": {
                "id": "cbq_brain_1",
                "from": {"id": self.OWNER_ID},
                "message": {
                    "message_id": 2002,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "text": "old brain panel",
                },
                "data": "brain:cloud",
            },
        }
        res_cb = bridge.handle_update(cb_update)
        self.assertTrue(res_cb["handled"])
        self.assertEqual(res_cb["action"], "brain_callback")
        self.assertEqual(res_cb["mode"], "cloud")
        self.assertTrue(res_cb["ok"])
        self.assertTrue(res_cb["message_updated"])
        self.assertEqual(
            self.server.methods_called(), ["answerCallbackQuery", "editMessageText"]
        )
        toast_payload = self.server.requests[0]["json"]
        self.assertIn("云端 API", toast_payload["text"])
        edit_payload = self.server.requests[1]["json"]
        self.assertIn("☁️ 云端 API", edit_payload["text"])
        kept_buttons = [
            b["callback_data"]
            for row in edit_payload["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        self.assertEqual(kept_buttons, ["brain:auto", "brain:cloud", "brain:local"])

        # 5. Four-layer isolation blocks non-owner /brain and brain:* callbacks
        self.server.clear_requests()
        before_calls = list(state["calls"])
        blocked_cmd = bridge.handle_update(
            {
                "update_id": 89,
                "message": {
                    "message_id": 2003,
                    "chat": {"id": 111222333, "type": "private"},
                    "from": {"id": 111222333},
                    "text": "/brain cloud",
                },
            }
        )
        self.assertFalse(blocked_cmd["allowed"])
        blocked_cb = bridge.handle_update(
            {
                "update_id": 90,
                "callback_query": {
                    "id": "cbq_evil",
                    "from": {"id": 111222333},
                    "message": {
                        "message_id": 2004,
                        "chat": {"id": self.OWNER_ID, "type": "group"},
                        "text": "group panel",
                    },
                    "data": "brain:cloud",
                },
            }
        )
        self.assertFalse(blocked_cb["allowed"])
        self.assertEqual(state["calls"], before_calls)
        self.assertEqual(self.server.requests, [])

        # 6. Backward compatibility when on_route is not provided
        res_no_handler = self.bridge.handle_update(self._private_msg(text="/brain"))
        self.assertEqual(res_no_handler["action"], "command_brain")
        self.assertFalse(res_no_handler["switched"])
        self.assertEqual(res_no_handler["reason"], "route_handler_unavailable")
        self.assertEqual(self.chat_inputs, [])

    # --- Chat + on-demand sendVoice (multipart/form-data) ---

    def test_chat_and_on_demand_voice_multipart(self) -> None:
        # Default voice=off: sendMessage only, no TTS/sendVoice
        res1 = self.bridge.handle_update(self._private_msg(text="hello cloud"))
        self.assertEqual(res1["action"], "chat")
        self.assertEqual(res1["reply"], "Echo: hello cloud")
        self.assertFalse(res1["voice_sent"])
        self.assertEqual(self.server.methods_called(), ["sendMessage"])
        self.assertEqual(self.tts_inputs, [])

        # Enable voice -> next chat triggers sendMessage + multipart sendVoice
        self.server.clear_requests()
        self.bridge.handle_update(self._private_msg(text="/voice on"))
        self.server.clear_requests()

        res2 = self.bridge.handle_update(self._private_msg(text="speak now"))
        self.assertEqual(res2["action"], "chat")
        self.assertTrue(res2["voice_sent"])
        self.assertEqual(self.tts_inputs, ["Echo: speak now"])
        self.assertEqual(self.server.methods_called(), ["sendMessage", "sendVoice"])

        voice_req = self.server.requests[1]
        self.assertIn("multipart/form-data; boundary=", voice_req["content_type"])
        raw = voice_req["raw_body"]
        self.assertIn(f'name="chat_id"\r\n\r\n{self.OWNER_ID}'.encode("utf-8"), raw)
        self.assertIn(b'name="voice"; filename="reply.ogg"', raw)
        self.assertIn(b"Content-Type: audio/ogg", raw)
        self.assertIn(b"OggS\x00\x02fake-opus-payload", raw)

        # Graceful degradation when TTS fails
        self.server.clear_requests()
        self.tts_output = None
        res3 = self.bridge.handle_update(self._private_msg(text="fallback text"))
        self.assertEqual(res3["reply"], "Echo: fallback text")
        self.assertFalse(res3["voice_sent"])
        self.assertEqual(self.server.methods_called(), ["sendMessage"])

    # --- Inline Keyboard L1 approval prompt & callback with real ApprovalGate ---

    def test_approval_prompt_and_inline_keyboard_callback_with_gate(self) -> None:
        captured_prompts: list[dict[str, Any]] = []

        def _on_gate_event(event: Any) -> None:
            if event.payload.get("phase") == "approval_requested":
                self.bridge.push_approval_prompt(event.payload)
                captured_prompts.append(event.payload)

        gate = ApprovalGate(on_event=_on_gate_event)
        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=gate.decide,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        # Re-bind bridge in closure
        self.bridge = bridge

        decision_holder: list[Any] = []

        def _request_worker() -> None:
            decision_holder.append(
                gate.request_approval(
                    action="terminal_exec",
                    target="git commit -m test",
                    detail="L1 commit requires owner confirmation",
                    timeout_sec=5.0,
                )
            )

        t = threading.Thread(target=_request_worker, daemon=True)
        t.start()

        deadline = time.monotonic() + 2.0
        while not captured_prompts and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(captured_prompts), 1)
        approval_id = captured_prompts[0]["approval_id"]

        # Verify inline keyboard buttons in sendMessage
        send_req = self.server.requests[-1]
        self.assertEqual(send_req["method"], "sendMessage")
        keyboard = send_req["json"]["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "✅ 批准")
        self.assertEqual(keyboard[0][0]["callback_data"], f"approve:{approval_id}")
        self.assertEqual(keyboard[0][1]["text"], "❌ 拒绝")
        self.assertEqual(keyboard[0][1]["callback_data"], f"reject:{approval_id}")

        # Owner clicks approve button
        self.server.clear_requests()
        cb_update = {
            "update_id": 50,
            "callback_query": {
                "id": "cbq_approve_1",
                "from": {"id": self.OWNER_ID},
                "message": {
                    "message_id": 1001,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "text": send_req["json"]["text"],
                },
                "data": f"approve:{approval_id}",
            },
        }
        cb_res = bridge.handle_update(cb_update)
        self.assertTrue(cb_res["handled"])
        self.assertTrue(cb_res["approved"])
        self.assertTrue(cb_res["resolved"])
        self.assertEqual(
            self.server.methods_called(), ["answerCallbackQuery", "editMessageText"]
        )
        edit_payload = self.server.requests[1]["json"]
        self.assertIn("✅ 已批准", edit_payload["text"])
        self.assertEqual(edit_payload["reply_markup"], {"inline_keyboard": []})

        t.join(timeout=2.0)
        self.assertEqual(len(decision_holder), 1)
        self.assertTrue(decision_holder[0].approved)
        self.assertEqual(decision_holder[0].status, "approved")
        self.assertEqual(decision_holder[0].actor, f"telegram:{self.OWNER_ID}")

        # Duplicate click on already-resolved approval returns resolved=False
        self.server.clear_requests()
        dup_res = bridge.handle_update(cb_update)
        self.assertFalse(dup_res["resolved"])
        self.assertIn("已过期或已处理", self.server.requests[1]["json"]["text"])

    def test_approval_reject_callback_denies_gate(self) -> None:
        gate = ApprovalGate()
        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=gate.decide,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        decision_holder: list[Any] = []
        t = threading.Thread(
            target=lambda: decision_holder.append(
                gate.request_approval("terminal_exec", "rm -rf tmp", timeout_sec=5.0)
            ),
            daemon=True,
        )
        t.start()

        deadline = time.monotonic() + 2.0
        pending = []
        while not pending and time.monotonic() < deadline:
            pending = gate.list_pending()
            time.sleep(0.02)
        self.assertEqual(len(pending), 1)
        approval_id = pending[0]["approval_id"]

        cb_update = {
            "update_id": 60,
            "callback_query": {
                "id": "cbq_reject_1",
                "from": {"id": self.OWNER_ID},
                "message": {
                    "message_id": 1002,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "text": "Approve?",
                },
                "data": f"reject:{approval_id}",
            },
        }
        res = bridge.handle_update(cb_update)
        self.assertFalse(res["approved"])
        self.assertTrue(res["resolved"])
        self.assertIn("❌ 已拒绝", self.server.requests[-1]["json"]["text"])

        t.join(timeout=2.0)
        self.assertFalse(decision_holder[0].approved)
        self.assertEqual(decision_holder[0].status, "denied")

    # --- Polling thread + backoff resilience + token redaction ---

    def test_polling_backoff_recovery_and_stop(self) -> None:
        with self.server.lock:
            self.server.fail_get_updates_remaining = 2
            self.server.queued_updates = [
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": -500, "type": "group"},
                        "text": "group noise",
                    },
                },
                {
                    "update_id": 102,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": self.OWNER_ID, "type": "private"},
                        "from": {"id": self.OWNER_ID},
                        "text": "ping from poller",
                    },
                },
            ]

        self.assertTrue(self.bridge.start_polling())
        self.assertFalse(self.bridge.start_polling())  # idempotent
        self.assertTrue(self.bridge.is_running)

        deadline = time.monotonic() + 3.0
        while not self.chat_inputs and time.monotonic() < deadline:
            time.sleep(0.03)

        self.bridge.stop(timeout=3.0)
        self.assertFalse(self.bridge.is_running)
        self.assertEqual(self.chat_inputs, ["ping from poller"])
        self.assertGreaterEqual(self.server.get_updates_calls, 3)

    def test_error_message_redacts_bot_token(self) -> None:
        bad_bridge = TelegramBotBridge(
            bot_token="999:SUPER_SECRET_BOT_TOKEN",
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            http_timeout_sec=2.0,
        )
        with self.assertRaises(TelegramApiError) as ctx:
            bad_bridge.push_text("test")
        self.assertNotIn("SUPER_SECRET_BOT_TOKEN", str(ctx.exception))

    def test_think_command_and_inline_keyboard_switch(self) -> None:
        state: dict[str, Any] = {
            "effort": "minimal",
            "calls": [],
        }

        def _on_reasoning(effort: str | None) -> dict[str, Any]:
            state["calls"].append(effort)
            if effort is None:
                return {
                    "ok": True,
                    "engine": "hybrid",
                    "model": "gemini-3-pro",
                    "reasoning_effort": state["effort"],
                    "supports_reasoning_switch": True,
                }
            prev = state["effort"]
            new_val = "" if effort == "default" else effort
            state["effort"] = new_val
            return {
                "ok": True,
                "engine": "hybrid",
                "reasoning_effort": new_val,
                "previous_reasoning_effort": prev,
            }

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=self._on_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_reasoning=_on_reasoning,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        # 1. /think without args queries current effort and attaches 2x3 inline keyboard
        self.server.clear_requests()
        res_query = bridge.handle_update(self._private_msg(text="/think"))
        self.assertEqual(res_query["action"], "command_think")
        self.assertEqual(res_query["effort"], "minimal")
        self.assertFalse(res_query["switched"])
        self.assertEqual(state["calls"], [None])
        send_req = self.server.requests[-1]
        self.assertEqual(send_req["method"], "sendMessage")
        kb = send_req["json"]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(kb), 2)
        self.assertEqual([len(row) for row in kb], [3, 3])
        buttons = [(b["text"], b["callback_data"]) for row in kb for b in row]
        self.assertEqual(
            buttons,
            [
                ("⚡ 极速", "think:minimal"),
                ("🪶 轻量", "think:low"),
                ("⚖️ 均衡", "think:medium"),
                ("🧠 深度", "think:high"),
                ("🔥 极限", "think:max"),
                ("🔄 默认", "think:default"),
            ],
        )

        # 2. /effort med and /think default aliases switch directly
        self.server.clear_requests()
        res_med = bridge.handle_update(self._private_msg(text="/effort med"))
        self.assertTrue(res_med["switched"])
        self.assertEqual(res_med["effort"], "medium")
        res_def = bridge.handle_update(self._private_msg(text="/think default"))
        self.assertTrue(res_def["switched"])
        self.assertEqual(res_def["effort"], "")
        self.assertEqual(state["calls"], [None, "medium", "default"])

        # 3. Invalid /think arg returns usage hint without calling on_reasoning
        res_bad = bridge.handle_update(self._private_msg(text="/think turbo"))
        self.assertFalse(res_bad["switched"])
        self.assertEqual(res_bad["reason"], "invalid_effort")
        self.assertEqual(state["calls"], [None, "medium", "default"])

        # 4. Inline keyboard callback think:high answers toast and edits text keeping 2x3 buttons
        self.server.clear_requests()
        cb_update = {
            "update_id": 188,
            "callback_query": {
                "id": "cbq_think_1",
                "from": {"id": self.OWNER_ID},
                "message": {
                    "message_id": 3003,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "text": "old think panel",
                },
                "data": "think:high",
            },
        }
        res_cb = bridge.handle_update(cb_update)
        self.assertTrue(res_cb["handled"])
        self.assertEqual(res_cb["action"], "think_callback")
        self.assertEqual(res_cb["effort"], "high")
        self.assertTrue(res_cb["ok"])
        self.assertTrue(res_cb["message_updated"])
        methods = self.server.methods_called()
        self.assertIn("answerCallbackQuery", methods)
        self.assertIn("editMessageText", methods)
        edit_req = [r for r in self.server.requests if r["method"] == "editMessageText"][-1]
        self.assertEqual(edit_req["json"]["message_id"], 3003)
        self.assertEqual(len(edit_req["json"]["reply_markup"]["inline_keyboard"]), 2)

    def test_typing_indicator_sends_chat_action_and_degrades_gracefully(self) -> None:
        def _slow_chat(text: str) -> str:
            time.sleep(0.14)
            return f"reply:{text}"

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=_slow_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
            typing_indicator=True,
            typing_refresh_sec=0.05,
        )

        self.server.clear_requests()
        res = bridge.handle_update(self._private_msg(text="hello slow"))
        self.assertEqual(res["action"], "chat")
        self.assertEqual(res["reply"], "reply:hello slow")
        methods = self.server.methods_called()
        self.assertEqual(methods[0], "sendChatAction")
        self.assertEqual(methods[-1], "sendMessage")
        typing_reqs = [r for r in self.server.requests if r["method"] == "sendChatAction"]
        self.assertGreaterEqual(len(typing_reqs), 2)
        self.assertEqual(typing_reqs[0]["json"]["action"], "typing")

        # Failure in sendChatAction must degrade silently without blocking reply.
        orig_call = bridge._call_json_api

        def _failing_typing(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            if method == "sendChatAction":
                raise TelegramApiError("simulated sendChatAction failure")
            return orig_call(method, payload)

        bridge._call_json_api = _failing_typing  # type: ignore[method-assign]
        self.server.clear_requests()
        res2 = bridge.handle_update(self._private_msg(text="still works"))
        self.assertEqual(res2["action"], "chat")
        self.assertEqual(res2["reply"], "reply:still works")
        self.assertEqual(self.server.methods_called(), ["sendMessage"])

    def test_sleep_and_new_commands(self) -> None:
        # 1. Disabled when on_sleep is not configured
        bridge_disabled = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: f"echo:{text}",
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        self.server.clear_requests()
        res_off = bridge_disabled.handle_update(self._private_msg(text="/sleep"))
        self.assertTrue(res_off["allowed"])
        self.assertTrue(res_off["handled"])
        self.assertEqual(res_off["action"], "command_sleep")
        self.assertFalse(res_off["ok"])
        self.assertEqual(res_off["status"], "disabled")
        self.assertIn("未启用", res_off["reply"])
        self.assertEqual(self.server.methods_called(), ["sendMessage"])

        # 2. Dict result, idle status, ok=False, str reply, and exception isolation
        responses: list[Any] = [
            {
                "ok": True,
                "status": "consolidated",
                "result": {
                    "summary": "讨论了统一记忆架构",
                    "new_facts": [{"key": "arch", "value": "unified"}, {"key": "cmd", "value": "/sleep"}],
                    "journal_path": "journals/2026-11-23.md",
                },
            },
            {"ok": True, "status": "idle"},
            {"ok": False, "error": "consolidator unavailable"},
            "已完成手工归档",
        ]
        calls = {"count": 0}

        def _on_sleep() -> Any:
            idx = calls["count"]
            calls["count"] += 1
            if idx < len(responses):
                return responses[idx]
            raise RuntimeError("boom sleep")

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: f"echo:{text}",
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_sleep=_on_sleep,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        # /sleep with dict result
        self.server.clear_requests()
        r1 = bridge.handle_update(self._private_msg(text="/sleep"))
        self.assertEqual(r1["action"], "command_sleep")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["status"], "consolidated")
        self.assertIn("讨论了统一记忆架构", r1["reply"])
        self.assertIn("2 条", r1["reply"])
        self.assertIn("journals/2026-11-23.md", r1["reply"])
        self.assertIn("下条消息自动开启新对话", r1["reply"])
        self.assertEqual(self.server.methods_called(), ["sendMessage"])

        # /new alias with idle status
        r2 = bridge.handle_update(self._private_msg(text="/new"))
        self.assertEqual(r2["action"], "command_sleep")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["status"], "idle")
        self.assertIn("没有活跃对话", r2["reply"])

        # /sleep with ok=False
        r3 = bridge.handle_update(self._private_msg(text="/sleep"))
        self.assertEqual(r3["action"], "command_sleep")
        self.assertFalse(r3["ok"])
        self.assertIn("consolidator unavailable", r3["reply"])

        # /new with str return
        r4 = bridge.handle_update(self._private_msg(text="/new"))
        self.assertEqual(r4["action"], "command_sleep")
        self.assertTrue(r4["ok"])
        self.assertIn("已完成手工归档", r4["reply"])
        self.assertIn("下条消息自动开启新对话", r4["reply"])

        # /sleep exception is fault-isolated
        r5 = bridge.handle_update(self._private_msg(text="/sleep"))
        self.assertEqual(r5["action"], "command_sleep")
        self.assertFalse(r5["ok"])
        self.assertEqual(r5["status"], "error")
        self.assertIn("boom sleep", r5["reply"])
        self.assertEqual(calls["count"], 5)

    def test_chat_error_sends_redacted_notice_instead_of_silence(self) -> None:
        def _failing_chat(_text: str) -> str:
            raise RuntimeError(f"daemon HTTP 500: upstream boom token={self.TOKEN}")

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=_failing_chat,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        self.server.clear_requests()
        res = bridge.handle_update(self._private_msg(text="hello"))
        self.assertTrue(res["allowed"])
        self.assertTrue(res["handled"])
        self.assertEqual(res["action"], "chat_error")
        self.assertIn("upstream boom", res["error"])
        self.assertNotIn(self.TOKEN, res["error"])

        sent_msgs = [req for req in self.server.requests if req.get("method") == "sendMessage"]
        self.assertEqual(len(sent_msgs), 1)
        sent_text = sent_msgs[0]["json"]["text"]
        self.assertTrue(sent_text.startswith("⚠️ 对话暂时失败："))
        self.assertIn("upstream boom", sent_text)
        self.assertNotIn(self.TOKEN, sent_text)

    def test_chat_error_formats_friendly_429_and_503_notices(self) -> None:
        cases = [
            (
                f"daemon HTTP 429: quota hit token={self.TOKEN}",
                "⚠️ 对话暂时失败：云端模型当前限流拥堵（HTTP 429），已自动重试与降级仍繁忙，请稍候再试或发 /brain 切回自动混动。",
                "HTTP 429",
            ),
            (
                f"upstream resource_exhausted token={self.TOKEN}",
                "⚠️ 对话暂时失败：云端模型当前限流拥堵（HTTP 429），已自动重试与降级仍繁忙，请稍候再试或发 /brain 切回自动混动。",
                "resource_exhausted",
            ),
            (
                f"daemon HTTP 503: backend down token={self.TOKEN}",
                "⚠️ 对话暂时失败：云端模型服务暂时不可用（HTTP 503），请稍候再试或发 /brain 切回自动混动。",
                "HTTP 503",
            ),
            (
                f"upstream UNAVAILABLE token={self.TOKEN}",
                "⚠️ 对话暂时失败：云端模型服务暂时不可用（HTTP 503），请稍候再试或发 /brain 切回自动混动。",
                "UNAVAILABLE",
            ),
        ]
        for err_msg, expected_notice, expected_raw_fragment in cases:
            def _failing_chat(_text: str, msg: str = err_msg) -> str:
                raise RuntimeError(msg)

            bridge = TelegramBotBridge(
                bot_token=self.TOKEN,
                allowed_chat_id=self.OWNER_ID,
                on_chat=_failing_chat,
                on_approval_decision=self._on_approval,
                on_status=lambda: "ok",
                on_nodes=lambda: [],
                api_base=self.server.base_url,
                poll_timeout_sec=0,
            )
            self.server.clear_requests()
            res = bridge.handle_update(self._private_msg(text="hello"))
            self.assertEqual(res["action"], "chat_error")
            self.assertIn(expected_raw_fragment, res["error"])
            self.assertNotIn(self.TOKEN, res["error"])

            sent_msgs = [req for req in self.server.requests if req.get("method") == "sendMessage"]
            self.assertEqual(len(sent_msgs), 1)
            self.assertEqual(sent_msgs[0]["json"]["text"], expected_notice)

    def test_build_telegram_bridge_default_timeout_and_http_500_detail(self) -> None:
        import inspect
        import io
        from unittest import mock
        import urllib.error

        from shinobu_core.server.cli import build_telegram_bridge

        sig = inspect.signature(build_telegram_bridge)
        self.assertEqual(sig.parameters["timeout_sec"].default, 120.0)

        bridge = build_telegram_bridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            daemon_url="http://127.0.0.1:8787",
            daemon_token="secret-token",
            api_base=self.server.base_url,
        )
        http_err = urllib.error.HTTPError(
            "http://127.0.0.1:8787/chat",
            500,
            "Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"chat_failed","detail":"vertex invalid tool call"}'),
        )
        with mock.patch("shinobu_core.server.cli._daemon_http_call", side_effect=http_err):
            with self.assertRaises(RuntimeError) as ctx:
                bridge._on_chat("hello")
        self.assertIn("daemon HTTP 500: vertex invalid tool call", str(ctx.exception))

    def test_session_panel_aliases_and_inline_callbacks(self) -> None:
        state: dict[str, object] = {
            "active_episode": {"id": "ep_active_123456", "turn_count": 6},
            "recent_closed": [
                {"id": "ep_old_111111", "summary": "讨论了云端部署", "turn_count": 4},
                {"id": "ep_old_222222", "summary": "", "turn_count": 2},
                {"id": "ep_old_333333", "summary": "调试 Telegram 面板", "turn_count": 8},
                {"id": "ep_old_444444", "summary": "第四条不应展示", "turn_count": 1},
            ],
        }
        counters = {"sleep": 0, "new": 0, "session": 0}

        def _on_session() -> dict[str, object]:
            counters["session"] += 1
            return dict(state)

        def _on_new_session() -> dict[str, object]:
            counters["new"] += 1
            active = state["active_episode"]
            if active is None:
                return {"ok": True, "status": "idle", "closed_episode_id": None, "turn_count": 0}
            state["active_episode"] = None
            closed = list(state["recent_closed"])  # type: ignore[arg-type]
            closed.insert(0, {"id": "ep_active_123456", "summary": "", "turn_count": 6})
            state["recent_closed"] = closed
            return {
                "ok": True,
                "status": "archived",
                "closed_episode_id": "ep_active_123456",
                "turn_count": 6,
            }

        def _on_sleep() -> dict[str, object]:
            counters["sleep"] += 1
            return {"ok": True, "status": "consolidated", "result": {"summary": "已整理"}}

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_sleep=_on_sleep,
            on_session=_on_session,
            on_new_session=_on_new_session,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        # /session and aliases /sessions, /archive
        for alias in ("/session", "/sessions", "/archive"):
            self.server.clear_requests()
            res = bridge.handle_update(self._private_msg(text=alias))
            self.assertTrue(res["allowed"])
            self.assertTrue(res["handled"])
            self.assertEqual(res["action"], "command_session")
            self.assertTrue(res["ok"])
            self.assertIn("#ep_activ", res["reply"])
            self.assertIn("6 条消息", res["reply"])
            self.assertIn("讨论了云端部署", res["reply"])
            self.assertIn("快速归档，无摘要", res["reply"])
            self.assertIn("调试 Telegram 面板", res["reply"])
            self.assertNotIn("第四条不应展示", res["reply"])

            sent = [r for r in self.server.requests if r.get("method") == "sendMessage"]
            self.assertEqual(len(sent), 1)
            markup = sent[0]["json"]["reply_markup"]["inline_keyboard"]
            cb_datas = [btn["callback_data"] for row in markup for btn in row]
            self.assertEqual(cb_datas, ["session:sleep", "session:new", "session:refresh"])

        def _cb_update(data: str, cb_id: str) -> dict[str, object]:
            return {
                "update_id": 9100,
                "callback_query": {
                    "id": cb_id,
                    "from": {"id": self.OWNER_ID, "is_bot": False, "first_name": "Owner"},
                    "message": {
                        "message_id": 88,
                        "chat": {"id": self.OWNER_ID, "type": "private"},
                        "text": "old panel",
                    },
                    "data": data,
                },
            }

        # Click session:new -> archives active episode, shows toast, edits panel in place
        self.server.clear_requests()
        cb_new = bridge.handle_update(_cb_update("session:new", "cb-new-1"))
        self.assertEqual(cb_new["action"], "session_callback")
        self.assertEqual(cb_new["sub_action"], "new")
        self.assertTrue(cb_new["ok"])
        self.assertTrue(cb_new["message_updated"])
        self.assertEqual(counters["new"], 1)
        self.assertIn("暂无活跃对话", cb_new["reply"])
        self.assertIn("🆕 已归档旧对话，下条消息开启新对话", cb_new["reply"])
        methods = [r.get("method") for r in self.server.requests]
        self.assertIn("answerCallbackQuery", methods)
        self.assertIn("editMessageText", methods)

        # Click session:sleep
        self.server.clear_requests()
        cb_sleep = bridge.handle_update(_cb_update("session:sleep", "cb-sleep-1"))
        self.assertEqual(cb_sleep["action"], "session_callback")
        self.assertEqual(cb_sleep["sub_action"], "sleep")
        self.assertTrue(cb_sleep["ok"])
        self.assertEqual(counters["sleep"], 1)

        # Click session:refresh
        self.server.clear_requests()
        cb_refresh = bridge.handle_update(_cb_update("session:refresh", "cb-refresh-1"))
        self.assertEqual(cb_refresh["action"], "session_callback")
        self.assertEqual(cb_refresh["sub_action"], "refresh")
        self.assertTrue(cb_refresh["ok"])
        self.assertTrue(cb_refresh["message_updated"])

        # Invalid session callback action
        cb_bad = bridge.handle_update(_cb_update("session:unknown", "cb-bad-1"))
        self.assertFalse(cb_bad["handled"])
        self.assertEqual(cb_bad["action"], "invalid_callback")

    def test_new_command_fast_archive_vs_sleep(self) -> None:
        calls = {"sleep": 0, "new": 0}

        def _on_sleep() -> dict[str, object]:
            calls["sleep"] += 1
            return {"ok": True, "status": "consolidated", "result": {"summary": "完整归档"}}

        def _on_new() -> dict[str, object]:
            calls["new"] += 1
            if calls["new"] == 1:
                return {
                    "ok": True,
                    "status": "archived",
                    "closed_episode_id": "ep_fast_999999",
                    "turn_count": 4,
                }
            if calls["new"] == 2:
                return {"ok": True, "status": "idle", "closed_episode_id": None, "turn_count": 0}
            raise RuntimeError(f"reset failed token={self.TOKEN}")

        bridge = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_sleep=_on_sleep,
            on_new_session=_on_new,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )

        r1 = bridge.handle_update(self._private_msg(text="/new"))
        self.assertEqual(r1["action"], "command_new")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["status"], "archived")
        self.assertIn("🆕 已归档旧对话，下条消息开启新对话", r1["reply"])
        self.assertEqual(calls["new"], 1)
        self.assertEqual(calls["sleep"], 0)

        r2 = bridge.handle_update(self._private_msg(text="/new"))
        self.assertEqual(r2["action"], "command_new")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["status"], "idle")

        r3 = bridge.handle_update(self._private_msg(text="/new"))
        self.assertEqual(r3["action"], "command_new")
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["status"], "error")
        self.assertNotIn(self.TOKEN, r3["reply"])

        r4 = bridge.handle_update(self._private_msg(text="/sleep"))
        self.assertEqual(r4["action"], "command_sleep")
        self.assertTrue(r4["ok"])
        self.assertEqual(calls["sleep"], 1)

    def test_start_polling_register_commands_and_fault_isolation(self) -> None:
        from unittest import mock

        # Default register_commands=False -> never calls setMyCommands
        bridge_default = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        self.server.clear_requests()
        self.assertTrue(bridge_default.start_polling())
        bridge_default.stop(timeout=2.0)
        methods_default = [r.get("method") for r in self.server.requests]
        self.assertNotIn("setMyCommands", methods_default)

        # register_commands=True -> registers all 10 commands before polling
        bridge_menu = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            register_commands=True,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        self.server.clear_requests()
        self.assertTrue(bridge_menu.start_polling())
        bridge_menu.stop(timeout=2.0)
        set_cmds = [r for r in self.server.requests if r.get("method") == "setMyCommands"]
        self.assertEqual(len(set_cmds), 1)
        registered = [c["command"] for c in set_cmds[0]["json"]["commands"]]
        self.assertEqual(
            registered,
            ["session", "new", "sleep", "brain", "think", "status", "voice", "nodes", "remind", "help"],
        )

        # /remind command behavior: list, <N>m, <N>h, HH:MM (same day & next day), ISO, invalid & disabled
        from datetime import datetime, timezone

        fixed_ts = datetime(2026, 11, 25, 6, 0, 0, tzinfo=timezone.utc).timestamp()  # 14:00 +08:00
        created_reminders: list[dict[str, object]] = []

        def _fake_on_remind(trigger_iso: str | None, label: str | None):
            if trigger_iso is None and label is None:
                return {"schedules": list(created_reminders)}
            item = {
                "id": len(created_reminders) + 1,
                "trigger_at": trigger_iso,
                "label": label,
                "done": False,
            }
            created_reminders.append(item)
            return item

        bridge_remind = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            on_remind=_fake_on_remind,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
            tz_offset_hours=8.0,
            clock=lambda: fixed_ts,
        )
        self.server.clear_requests()

        res_empty = bridge_remind._handle_remind_command("")
        self.assertEqual(res_empty["action"], "command_remind")
        self.assertIn("没有待触发的提醒", res_empty["reply"])

        res_min = bridge_remind._handle_remind_command("10m 喝水休息")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-11-25T06:10:00Z")
        self.assertEqual(created_reminders[-1]["label"], "喝水休息")
        self.assertIn("14:10", res_min["reply"])

        res_hour = bridge_remind._handle_remind_command("2h 站起来活动")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-11-25T08:00:00Z")
        self.assertIn("16:00", res_hour["reply"])

        res_same_day = bridge_remind._handle_remind_command("18:30 下班打卡")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-11-25T10:30:00Z")
        self.assertIn("2026-11-25 18:30", res_same_day["reply"])

        res_next_day = bridge_remind._handle_remind_command("09:00 晨会")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-11-26T01:00:00Z")
        self.assertIn("2026-11-26 09:00", res_next_day["reply"])

        res_iso = bridge_remind._handle_remind_command("2026-12-01T09:30:00+08:00 月初复盘")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-12-01T01:30:00Z")
        self.assertIn("2026-12-01 09:30", res_iso["reply"])

        res_list = bridge_remind._handle_remind_command("")
        self.assertIn("共 5 条", res_list["reply"])
        self.assertIn("喝水休息", res_list["reply"])
        self.assertIn("月初复盘", res_list["reply"])

        res_bad = bridge_remind._handle_remind_command("abc 无效时间")
        self.assertIn("无法识别时间", res_bad["reply"])
        self.assertEqual(len(created_reminders), 5)

        res_usage = bridge_remind._handle_remind_command("10m")
        self.assertIn("用法", res_usage["reply"])
        self.assertEqual(len(created_reminders), 5)

        res_disabled = bridge_menu._handle_remind_command("")
        self.assertIn("未启用定时提醒", res_disabled["reply"])

        res_routed = bridge_remind.handle_update(
            {
                "update_id": 9901,
                "message": {
                    "message_id": 9901,
                    "chat": {"id": self.OWNER_ID, "type": "private"},
                    "from": {"id": self.OWNER_ID},
                    "text": "/remind 30m 路由校验提醒",
                },
            }
        )
        self.assertEqual(res_routed["action"], "command_remind")
        self.assertEqual(created_reminders[-1]["trigger_at"], "2026-11-25T06:30:00Z")
        self.assertEqual(created_reminders[-1]["label"], "路由校验提醒")

        # setMyCommands failure is fault-isolated and does not block polling
        bridge_err = TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=self._on_approval,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            register_commands=True,
            api_base=self.server.base_url,
            poll_timeout_sec=0,
        )
        orig_call = bridge_err._call_json_api

        def _flaky_call(
            method: str,
            payload: dict[str, object] | None = None,
            **kwargs: object,
        ) -> object:
            if method == "setMyCommands":
                raise RuntimeError("telegram menu rate limited")
            return orig_call(method, payload, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(bridge_err, "_call_json_api", side_effect=_flaky_call):
            self.assertTrue(bridge_err.start_polling())
            bridge_err.stop(timeout=2.0)


class TestTelegramProactiveRelay(unittest.TestCase):
    OWNER_ID = 777001
    TOKEN = "123456:TEST_PROACTIVE_SECRET"

    def setUp(self) -> None:
        self.server = _MockTelegramServer(expected_token=self.TOKEN)
        self.server.start()

    def tearDown(self) -> None:
        self.server.close()

    def _make_bridge(
        self,
        *,
        clock_holder: list[float],
        quiet_hours: str = "01:00-08:00",
        tz_offset_hours: float = 8.0,
        proactive_cooldown_sec: float = 300.0,
    ) -> TelegramBotBridge:
        return TelegramBotBridge(
            bot_token=self.TOKEN,
            allowed_chat_id=self.OWNER_ID,
            on_chat=lambda text: text,
            on_approval_decision=lambda *args, **kwargs: True,
            on_status=lambda: "ok",
            on_nodes=lambda: [],
            api_base=self.server.base_url,
            poll_timeout_sec=0,
            quiet_hours=quiet_hours,
            tz_offset_hours=tz_offset_hours,
            proactive_cooldown_sec=proactive_cooldown_sec,
            clock=lambda: clock_holder[0],
        )

    def test_parse_and_check_quiet_hours_same_day_and_overnight(self) -> None:
        from datetime import datetime, timedelta, timezone
        from shinobu_core.cloud.telegram_bridge import (
            is_in_quiet_hours,
            parse_quiet_hours,
        )

        tz8 = timezone(timedelta(hours=8))

        # Disabled / empty specs
        self.assertIsNone(parse_quiet_hours(""))
        self.assertIsNone(parse_quiet_hours("   "))
        self.assertIsNone(parse_quiet_hours("off"))
        self.assertFalse(is_in_quiet_hours(datetime(2026, 11, 23, 3, 0, tzinfo=tz8), None))

        # Same-day window: 01:00-08:00
        win = parse_quiet_hours("01:00-08:00")
        self.assertEqual(win, ((1, 0), (8, 0)))
        self.assertFalse(is_in_quiet_hours(datetime(2026, 11, 23, 0, 59, tzinfo=tz8), win))
        self.assertTrue(is_in_quiet_hours(datetime(2026, 11, 23, 1, 0, tzinfo=tz8), win))
        self.assertTrue(is_in_quiet_hours(datetime(2026, 11, 23, 7, 59, tzinfo=tz8), win))
        self.assertFalse(is_in_quiet_hours(datetime(2026, 11, 23, 8, 0, tzinfo=tz8), win))

        # Midnight-crossing window: 23:30-07:00
        overnight = parse_quiet_hours("23:30-07:00")
        self.assertEqual(overnight, ((23, 30), (7, 0)))
        self.assertFalse(is_in_quiet_hours(datetime(2026, 11, 23, 23, 29, tzinfo=tz8), overnight))
        self.assertTrue(is_in_quiet_hours(datetime(2026, 11, 23, 23, 30, tzinfo=tz8), overnight))
        self.assertTrue(is_in_quiet_hours(datetime(2026, 11, 24, 0, 0, tzinfo=tz8), overnight))
        self.assertTrue(is_in_quiet_hours(datetime(2026, 11, 24, 6, 59, tzinfo=tz8), overnight))
        self.assertFalse(is_in_quiet_hours(datetime(2026, 11, 24, 7, 0, tzinfo=tz8), overnight))

        # Zero-width window never suppresses
        self.assertFalse(
            is_in_quiet_hours(
                datetime(2026, 11, 23, 8, 0, tzinfo=tz8),
                parse_quiet_hours("08:00-08:00"),
            )
        )

        # Invalid formats raise ValueError
        for bad in ("invalid", "25:00-08:00", "01:60-08:00", "01:00"):
            with self.assertRaises(ValueError):
                parse_quiet_hours(bad)

    def test_proactive_quiet_hours_cooldown_and_schedule_dedup(self) -> None:
        from datetime import datetime, timedelta, timezone
        from shinobu_core.protocol.events import Event, EventKind

        tz8 = timezone(timedelta(hours=8))
        # Start at 02:30+08:00 (inside default 01:00-08:00 quiet hours)
        clock = [datetime(2026, 11, 23, 2, 30, 0, tzinfo=tz8).timestamp()]
        bridge = self._make_bridge(clock_holder=clock, quiet_hours="01:00-08:00", proactive_cooldown_sec=300.0)

        # 1. Non-bubble and unsupported sources are ignored silently
        r_kind = bridge.push_proactive_event({"kind": "state_changed", "payload": {"source": "presence", "text": "hi"}})
        self.assertEqual(r_kind, {"sent": False, "reason": "ignored_kind"})
        r_src = bridge.push_proactive_event({"kind": "bubble", "payload": {"source": "unknown", "text": "hi"}})
        self.assertEqual(r_src, {"sent": False, "reason": "ignored_source"})
        r_empty = bridge.push_proactive_event({"kind": "bubble", "payload": {"source": "presence", "text": "   "}})
        self.assertEqual(r_empty, {"sent": False, "reason": "empty_text"})
        self.assertEqual(self.server.requests, [])

        # 2. Quiet hours suppresses presence and briefing
        r_pres_q = bridge.push_proactive_event(
            Event(kind=EventKind.BUBBLE, payload={"source": "presence", "label": "return", "text": "欢迎回来"})
        )
        self.assertEqual(r_pres_q, {"sent": False, "reason": "quiet_hours"})
        r_brief_q = bridge.push_proactive_event(
            {"kind": "bubble", "payload": {"source": "briefing", "label": "daily_briefing", "text": "早安简报"}}
        )
        self.assertEqual(r_brief_q, {"sent": False, "reason": "quiet_hours"})

        # 3. Explicit timemap reminder bypasses quiet hours by default, unless respect_quiet=True
        r_tm_quiet = bridge.push_proactive_event(
            {
                "kind": "bubble",
                "payload": {
                    "source": "timemap",
                    "schedule_id": "sch_quiet_opt",
                    "label": "soft_nudge",
                    "text": "柔和提醒",
                    "data": {"respect_quiet": True},
                },
            }
        )
        self.assertEqual(r_tm_quiet, {"sent": False, "reason": "quiet_hours"})

        r_tm_ok = bridge.push_proactive_event(
            Event(
                kind=EventKind.BUBBLE,
                payload={
                    "source": "timemap",
                    "schedule_id": "sch_101",
                    "label": "medicine",
                    "text": "记得按时吃药",
                },
            )
        )
        self.assertEqual(r_tm_ok, {"sent": True, "reason": "ok"})
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[-1]["json"]["chat_id"], self.OWNER_ID)
        self.assertIn("记得按时吃药", self.server.requests[-1]["json"]["text"])

        # 4. Move to 09:00+08:00 (outside quiet hours): presence/briefing and previously quiet-skipped schedule send
        clock[0] = datetime(2026, 11, 23, 9, 0, 0, tzinfo=tz8).timestamp()
        r_pres_ok = bridge.push_proactive_event(
            {"kind": "bubble", "payload": {"source": "presence", "label": "return", "text": "欢迎回来"}}
        )
        self.assertEqual(r_pres_ok, {"sent": True, "reason": "ok"})

        r_tm_after_quiet = bridge.push_proactive_event(
            {
                "kind": "bubble",
                "payload": {
                    "source": "timemap",
                    "schedule_id": "sch_quiet_opt",
                    "label": "soft_nudge",
                    "text": "柔和提醒",
                    "data": {"respect_quiet": True},
                },
            }
        )
        self.assertEqual(r_tm_after_quiet, {"sent": True, "reason": "ok"})

        # 5. Cooldown throttles same (source, label) within 300s, while different label is allowed
        clock[0] += 120.0
        r_cool = bridge.push_proactive_event(
            {"kind": "bubble", "payload": {"source": "presence", "label": "return", "text": "又回来啦"}}
        )
        self.assertEqual(r_cool, {"sent": False, "reason": "cooldown"})

        r_other_label = bridge.push_proactive_event(
            {"kind": "bubble", "payload": {"source": "presence", "label": "focus_break", "text": "休息一下吧"}}
        )
        self.assertEqual(r_other_label, {"sent": True, "reason": "ok"})

        # After cooldown expires (>300s from 09:00), same (source, label) sends again
        clock[0] += 200.0
        r_after_cool = bridge.push_proactive_event(
            {"kind": "bubble", "payload": {"source": "presence", "label": "return", "text": "再次欢迎回来"}}
        )
        self.assertEqual(r_after_cool, {"sent": True, "reason": "ok"})

        # 6. Duplicate schedule_id is permanently deduplicated even after cooldown expires
        clock[0] += 600.0
        r_dup = bridge.push_proactive_event(
            {
                "kind": "bubble",
                "payload": {
                    "source": "timemap",
                    "schedule_id": "sch_101",
                    "label": "medicine",
                    "text": "记得按时吃药",
                },
            }
        )
        self.assertEqual(r_dup, {"sent": False, "reason": "duplicate_schedule"})

        # New schedule_id with same label after cooldown succeeds
        r_new_sid = bridge.push_proactive_event(
            {
                "kind": "bubble",
                "payload": {
                    "source": "timemap",
                    "schedule_id": "sch_102",
                    "label": "medicine",
                    "text": "晚间提醒",
                },
            }
        )
        self.assertEqual(r_new_sid, {"sent": True, "reason": "ok"})

    def test_proactive_send_failure_rolls_back_dedup_and_cooldown(self) -> None:
        from datetime import datetime, timedelta, timezone
        from unittest import mock

        tz8 = timezone(timedelta(hours=8))
        clock = [datetime(2026, 11, 23, 10, 0, 0, tzinfo=tz8).timestamp()]
        bridge = self._make_bridge(clock_holder=clock)

        ev = {
            "kind": "bubble",
            "payload": {
                "source": "timemap",
                "schedule_id": "sch_retry_1",
                "label": "standup",
                "text": "站会提醒",
            },
        }
        with mock.patch.object(bridge, "push_text", side_effect=TelegramApiError("network down")):
            res_fail = bridge.push_proactive_event(ev)
        self.assertEqual(res_fail, {"sent": False, "reason": "send_failed"})

        # Retry succeeds because failed push rolled back reservation
        res_retry = bridge.push_proactive_event(ev)
        self.assertEqual(res_retry, {"sent": True, "reason": "ok"})
        self.assertEqual(len(self.server.requests), 1)


if __name__ == "__main__":
    unittest.main()
