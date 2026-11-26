# -*- coding: utf-8 -*-
"""Zero-dependency Telegram Bot API bridge with 4-layer physical isolation.

Implements official Telegram Bot API long-polling over Python stdlib only
(no MTProto/Userbot and no third-party Telegram libraries).

Four-layer physical isolation lock (`filter_update`):
1. Private-chat only: any non-private chat (`group`/`supergroup`/`channel`) is
   silently dropped.
2. Single-user allowlist: any chat/sender not matching `allowed_chat_id` is
   silently dropped.
3. No forwarded messages: messages carrying `forward_origin`, `forward_date`,
   `forward_from`, `forward_from_chat` (or other forward markers) are blocked
   with a plain-text notice and never forwarded to the model.
4. Zero-media blindfold shield: messages carrying `photo`, `video`,
   `animation`, `sticker`, `document`, or `video_note` are blocked with a
   plain-text notice, never download files via `getFile`, and never enter model
   context (including captions).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import re
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid


logger = logging.getLogger(__name__)

PROACTIVE_SOURCES: tuple[str, ...] = ("timemap", "presence", "briefing")


def parse_quiet_hours(
    spec: str,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Parse a quiet-hours window like ``"01:00-08:00"`` or ``"23:30-07:00"``.

    Returns ``((start_hour, start_min), (end_hour, end_min))``, or ``None``
    when *spec* is empty / ``"off"`` / ``"none"`` / ``"disabled"``.
    Raises ``ValueError`` on malformed specifications.
    """
    if spec is None:
        return None
    raw = str(spec).strip()
    if not raw or raw.lower() in ("off", "none", "disabled"):
        return None

    parts = [p.strip() for p in raw.split("-")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid quiet_hours format (expected HH:MM-HH:MM): {spec!r}")

    parsed: list[tuple[int, int]] = []
    for part in parts:
        hm = part.split(":")
        if len(hm) != 2:
            raise ValueError(f"Invalid quiet_hours time (expected HH:MM): {spec!r}")
        try:
            hour = int(hm[0])
            minute = int(hm[1])
        except ValueError as exc:
            raise ValueError(f"Invalid quiet_hours numbers: {spec!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"quiet_hours out of range (00:00-23:59): {spec!r}")
        parsed.append((hour, minute))

    return (parsed[0], parsed[1])


def is_in_quiet_hours(
    now_dt: datetime,
    quiet_window: tuple[tuple[int, int], tuple[int, int]] | None,
) -> bool:
    """Return True if *now_dt*'s local time falls within *quiet_window*.

    Supports both same-day windows (e.g. ``01:00-08:00``) and windows crossing
    midnight (e.g. ``23:00-07:00``). Start is inclusive, end is exclusive.
    """
    if quiet_window is None:
        return False
    (start_h, start_m), (end_h, end_m) = quiet_window
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    if start_min == end_min:
        return False
    cur_min = now_dt.hour * 60 + now_dt.minute
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min

# Layer 3: forwarded message fields (Telegram Bot API legacy + v7+ forward_origin)
FORWARD_BLOCKED_FIELDS: tuple[str, ...] = (
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "is_automatic_forward",
)

# Layer 4: zero-media blindfold fields (never download via getFile, never feed LLM)
MEDIA_BLOCKED_FIELDS: tuple[str, ...] = (
    "photo",
    "video",
    "animation",
    "sticker",
    "document",
    "video_note",
)

# Message container keys checked by the isolation filter
_MESSAGE_UPDATE_KEYS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "edited_business_message",
)

FORWARD_BLOCKED_NOTICE = (
    "[Isolation] Forwarded messages are blocked and were not sent to the model. "
    "Please send plain text directly."
)

MEDIA_BLOCKED_NOTICE = (
    "[Isolation] Media and file attachments are blocked by the zero-media shield. "
    "No file was downloaded and nothing entered the model context. "
    "Please send plain text only."
)

_MAX_TELEGRAM_TEXT_LEN = 4096


def _normalize_chat_id(chat_id: Any) -> str:
    if chat_id is None or isinstance(chat_id, bool):
        return ""
    return str(chat_id).strip()


def _extract_update_message(update: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for key in _MESSAGE_UPDATE_KEYS:
        value = update.get(key)
        if isinstance(value, dict):
            return key, value
    return "", None


def filter_update(update: dict[str, Any], allowed_chat_id: int | str) -> tuple[bool, str]:
    """Apply the 4-layer physical isolation lock to a Telegram Update dict.

    Returns ``(allowed, reason)``:
    - ``(True, "ok_message")`` / ``(True, "ok_callback_query")`` when allowed.
    - ``(False, "non_private_chat")`` or ``(False, "unauthorized_chat")`` when
      the update must be silently dropped.
    - ``(False, "blocked_forward:<field>")`` when blocked by Layer 3.
    - ``(False, "blocked_media:<field>")`` when blocked by Layer 4.
    """
    if not isinstance(update, dict):
        return False, "invalid_update"

    expected_id = _normalize_chat_id(allowed_chat_id)
    if not expected_id:
        return False, "unauthorized_chat"

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        cb_message = callback_query.get("message")
        chat = cb_message.get("chat") if isinstance(cb_message, dict) else None
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return False, "non_private_chat"

        chat_id_str = _normalize_chat_id(chat.get("id"))
        if not chat_id_str or chat_id_str != expected_id:
            return False, "unauthorized_chat"

        cb_from = callback_query.get("from")
        if isinstance(cb_from, dict) and cb_from.get("id") is not None:
            if _normalize_chat_id(cb_from.get("id")) != expected_id:
                return False, "unauthorized_chat"

        return True, "ok_callback_query"

    _, msg = _extract_update_message(update)
    if msg is None:
        return False, "non_private_chat"

    chat = msg.get("chat")
    # Layer 1: private chat only
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return False, "non_private_chat"

    # Layer 2: strict single-user allowlist
    chat_id_str = _normalize_chat_id(chat.get("id"))
    if not chat_id_str or chat_id_str != expected_id:
        return False, "unauthorized_chat"

    sender = msg.get("from")
    if isinstance(sender, dict) and sender.get("id") is not None:
        if _normalize_chat_id(sender.get("id")) != expected_id:
            return False, "unauthorized_chat"

    # Layer 3: forbid forwarded messages
    for field_name in FORWARD_BLOCKED_FIELDS:
        if field_name not in msg:
            continue
        val = msg[field_name]
        if field_name == "is_automatic_forward":
            if bool(val):
                return False, f"blocked_forward:{field_name}"
        elif val is not None:
            return False, f"blocked_forward:{field_name}"

    # Layer 4: zero-media blindfold shield
    for field_name in MEDIA_BLOCKED_FIELDS:
        if field_name in msg and msg[field_name]:
            return False, f"blocked_media:{field_name}"

    return True, "ok_message"


def _encode_multipart_form_data(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str = "audio/ogg",
) -> tuple[bytes, str]:
    """Build a standard RFC 7578 multipart/form-data payload using stdlib only."""
    boundary = f"----ShinobuTelegramBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(chunks)
    header_content_type = f"multipart/form-data; boundary={boundary}"
    return body, header_content_type


def _split_long_text(text: str, limit: int = _MAX_TELEGRAM_TEXT_LEN) -> list[str]:
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts or [""]


def _coerce_text(value: Any, *, empty_fallback: str = "(empty)") -> str:
    if value is None:
        return empty_fallback
    if isinstance(value, str):
        return value if value.strip() else empty_fallback
    if isinstance(value, dict):
        for key in ("reply", "text", "message", "summary"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _format_status_response(raw: Any) -> str:
    if isinstance(raw, str):
        return raw if raw.strip() else "Status: OK"
    if isinstance(raw, dict):
        lines = ["[Status]"]
        for key, value in raw.items():
            if "token" in str(key).lower() or "secret" in str(key).lower():
                continue
            lines.append(f"- {key}: {value}")
        return "\n".join(lines) if len(lines) > 1 else "Status: OK"
    return _coerce_text(raw, empty_fallback="Status: OK")


def _format_nodes_response(raw: Any) -> str:
    if isinstance(raw, str):
        return raw if raw.strip() else "No online nodes."
    items: list[Any] | None = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("workers", "nodes", "items"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        if items is None:
            return _format_status_response(raw)

    if not items:
        return "No registered nodes."

    lines = [f"[Nodes] ({len(items)})"]
    for item in items:
        if isinstance(item, dict):
            wid = item.get("worker_id") or item.get("id") or "unknown"
            kind = item.get("kind") or "worker"
            online = item.get("online")
            state = item.get("status")
            if state is None:
                state = "online" if online is not False else "offline"
            caps = item.get("capabilities")
            caps_str = ""
            if isinstance(caps, (list, tuple)) and caps:
                caps_str = f" | caps={','.join(str(c) for c in caps)}"
            lines.append(f"- {wid} ({kind}) [{state}]{caps_str}")
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _parse_command(text: str) -> tuple[str, str]:
    """Return ``(normalized_command, args)`` if text starts with ``/``."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", ""
    parts = stripped.split(None, 1)
    cmd_token = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    if "@" in cmd_token:
        cmd_token = cmd_token.split("@", 1)[0]
    return cmd_token.lower(), args


def _invoke_approval_callback(
    callback: Callable[..., Any],
    approval_id: str,
    approved: bool,
    actor: str,
) -> Any:
    """Invoke approval decision callback supporting 2-arg or 3+-arg signatures."""
    try:
        sig = inspect.signature(callback)
        params = list(sig.parameters.values())
        accepts_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
        )
        positional_count = sum(
            1
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        )
        if not accepts_varargs and positional_count <= 2:
            return callback(approval_id, approved)
    except (TypeError, ValueError):
        pass
    return callback(approval_id, approved, actor)


class TelegramApiError(RuntimeError):
    """Raised when the Telegram Bot API returns an error or transport fails."""


class TelegramBotBridge:
    """Stdlib-only Telegram Bot API long-polling adapter with 4-layer isolation."""

    _BRAIN_MODE_ALIASES: dict[str, str] = {
        "auto": "auto",
        "hybrid": "auto",
        "cloud": "cloud",
        "api": "cloud",
        "local": "local",
        "agy": "local",
        "edge": "local",
    }
    _BRAIN_MODE_LABELS: dict[str, str] = {
        "auto": "⚡ 自动混动",
        "cloud": "☁️ 云端 API",
        "local": "💻 仅本机 AGY",
    }
    _THINK_EFFORT_ALIASES: dict[str, str] = {
        "min": "minimal",
        "minimal": "minimal",
        "low": "low",
        "med": "medium",
        "medium": "medium",
        "high": "high",
        "max": "max",
        "default": "default",
        "auto": "default",
        "none": "default",
        "off": "default",
    }
    _THINK_EFFORT_LABELS: dict[str, str] = {
        "minimal": "⚡ 极速",
        "low": "🪶 轻量",
        "medium": "⚖️ 均衡",
        "high": "🧠 深度",
        "max": "🔥 极限",
        "default": "🔄 默认",
        "": "🔄 默认",
    }

    _BOT_COMMANDS: tuple[dict[str, str], ...] = (
        {"command": "session", "description": "对话管理与归档面板"},
        {"command": "new", "description": "秒开新对话（快速归档旧对话）"},
        {"command": "sleep", "description": "整理记忆并归档当前对话"},
        {"command": "brain", "description": "切换推理引擎（云端/混合/本地）"},
        {"command": "think", "description": "调整思考深度（关闭/低/中/高）"},
        {"command": "status", "description": "查看系统与引擎运行状态"},
        {"command": "voice", "description": "开关语音回复"},
        {"command": "nodes", "description": "查看在线边缘/算力节点"},
        {"command": "remind", "description": "查看或新建定时提醒"},
        {"command": "help", "description": "查看所有可用指令与用法"},
    )

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: int | str,
        *,
        on_chat: Callable[[str], Any],
        on_approval_decision: Callable[..., Any],
        on_status: Callable[[], Any],
        on_nodes: Callable[[], Any],
        on_tts: Callable[[str], bytes | None] | None = None,
        on_route: Callable[[str | None], Any] | None = None,
        on_reasoning: Callable[[str | None], Any] | None = None,
        on_sleep: Callable[[], Any] | None = None,
        on_session: Callable[[], Any] | None = None,
        on_new_session: Callable[[], Any] | None = None,
        on_remind: Callable[[str | None, str | None], Any] | None = None,
        register_commands: bool = False,
        api_base: str = "https://api.telegram.org",
        poll_timeout_sec: int = 25,
        http_timeout_sec: float = 35.0,
        backoff_initial_sec: float = 1.0,
        backoff_max_sec: float = 30.0,
        voice_enabled_default: bool = False,
        typing_indicator: bool = False,
        typing_refresh_sec: float = 4.0,
        quiet_hours: str = "01:00-08:00",
        tz_offset_hours: float = 8.0,
        proactive_cooldown_sec: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        token = str(bot_token or "").strip()
        if not token:
            raise ValueError("bot_token must not be empty")
        chat_id_str = _normalize_chat_id(allowed_chat_id)
        if not chat_id_str:
            raise ValueError("allowed_chat_id must not be empty")

        self._bot_token = token
        self._allowed_chat_id_str = chat_id_str
        self._allowed_chat_id: int | str = (
            int(chat_id_str)
            if chat_id_str.lstrip("-").isdigit()
            else chat_id_str
        )
        self._on_chat = on_chat
        self._on_approval_decision = on_approval_decision
        self._on_status = on_status
        self._on_nodes = on_nodes
        self._on_tts = on_tts
        self._on_route = on_route
        self._on_reasoning = on_reasoning
        self._on_sleep = on_sleep
        self._on_session = on_session
        self._on_new_session = on_new_session
        self._on_remind = on_remind
        self._register_commands = bool(register_commands)
        self._api_base = str(api_base or "https://api.telegram.org").rstrip("/")

        self._poll_timeout_sec = max(0, int(poll_timeout_sec))
        self._http_timeout_sec = max(1.0, float(http_timeout_sec))
        self._backoff_initial_sec = max(0.05, float(backoff_initial_sec))
        self._backoff_max_sec = max(self._backoff_initial_sec, float(backoff_max_sec))

        self._state_lock = threading.RLock()
        self._voice_enabled = bool(voice_enabled_default)
        self._typing_indicator = bool(typing_indicator)
        self._typing_refresh_sec = max(0.05, float(typing_refresh_sec))
        self._offset: int = 0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

        self._quiet_hours_spec: str = str(quiet_hours or "").strip()
        self._quiet_window = parse_quiet_hours(self._quiet_hours_spec)
        self._tz_offset_hours: float = float(tz_offset_hours)
        self._tz = timezone(timedelta(hours=self._tz_offset_hours))
        self._proactive_cooldown_sec: float = max(0.0, float(proactive_cooldown_sec))
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._sent_schedule_ids: dict[str, float] = {}
        self._proactive_last_sent: dict[tuple[str, str], float] = {}

    @property
    def allowed_chat_id(self) -> int | str:
        return self._allowed_chat_id

    def _parse_remind_time(self, spec: str) -> tuple[str, datetime] | None:
        token = str(spec or "").strip()
        if not token:
            return None

        now_utc = datetime.fromtimestamp(float(self._clock()), tz=timezone.utc)
        now_local = now_utc.astimezone(self._tz)

        m_min = re.fullmatch(r"(\d+)[mM]", token)
        if m_min:
            mins = int(m_min.group(1))
            if mins <= 0:
                return None
            target_utc = (now_utc + timedelta(minutes=mins)).replace(microsecond=0)
            return (
                target_utc.isoformat().replace("+00:00", "Z"),
                target_utc.astimezone(self._tz),
            )

        m_hour = re.fullmatch(r"(\d+)[hH]", token)
        if m_hour:
            hours = int(m_hour.group(1))
            if hours <= 0:
                return None
            target_utc = (now_utc + timedelta(hours=hours)).replace(microsecond=0)
            return (
                target_utc.isoformat().replace("+00:00", "Z"),
                target_utc.astimezone(self._tz),
            )

        m_hm = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
        if m_hm:
            hour = int(m_hm.group(1))
            minute = int(m_hm.group(2))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            candidate_local = now_local.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if candidate_local <= now_local:
                candidate_local = candidate_local + timedelta(days=1)
            target_utc = candidate_local.astimezone(timezone.utc).replace(microsecond=0)
            return (
                target_utc.isoformat().replace("+00:00", "Z"),
                candidate_local,
            )

        if "-" in token and ("T" in token or "t" in token):
            normalized = (
                token[:-1] + "+00:00" if token.endswith(("Z", "z")) else token
            )
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self._tz)
            target_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
            return (
                target_utc.isoformat().replace("+00:00", "Z"),
                target_utc.astimezone(self._tz),
            )

        return None

    def _format_remind_local_time(self, trigger_at: str) -> str:
        raw = str(trigger_at or "").strip()
        if not raw:
            return "未知时间"
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")

    def _format_remind_list(self, raw: Any) -> str:
        if isinstance(raw, str):
            text = raw.strip()
            return text if text else "⏰ 当前没有待触发的提醒。\n用法：/remind <10m|2h|HH:MM|ISO> <内容>"
        if isinstance(raw, Mapping):
            if raw.get("ok") is False:
                err = self._redact_text(str(raw.get("error") or "未知错误"))
                return f"⚠️ 获取提醒列表失败：{err}"
            items = raw.get("schedules")
            if not isinstance(items, list):
                items = raw.get("reminders")
        elif isinstance(raw, list):
            items = raw
        else:
            items = None

        if not isinstance(items, list) or not items:
            return "⏰ 当前没有待触发的提醒。\n用法：/remind <10m|2h|HH:MM|ISO> <内容>"

        lines = [f"⏰ 待触发提醒（共 {len(items)} 条）："]
        for idx, item in enumerate(items, start=1):
            if isinstance(item, Mapping):
                trig = str(item.get("trigger_at") or "").strip()
                lbl = str(item.get("label") or "").strip() or "(无内容)"
                sid = item.get("id")
                when_str = self._format_remind_local_time(trig)
                prefix = f"#{sid} " if sid is not None else f"{idx}. "
                lines.append(f"• {prefix}[{when_str}] {lbl}")
            else:
                lines.append(f"• {idx}. {item}")
        return "\n".join(lines)

    def _handle_remind_command(self, args: str) -> dict[str, Any]:
        if self._on_remind is None:
            reply = "⚠️ 当前未启用定时提醒功能（/remind）。"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_remind",
                "reply": reply,
            }

        cleaned = str(args or "").strip()
        if not cleaned or cleaned.lower() in ("list", "ls"):
            try:
                raw = self._on_remind(None, None)
                reply = self._format_remind_list(raw)
            except Exception as exc:
                err = self._redact_text(str(exc))
                logger.warning("on_remind list callback failed: %s", err)
                reply = f"⚠️ 获取提醒列表失败：{err}"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_remind",
                "reply": reply,
            }

        parts = cleaned.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            reply = (
                "⚠️ 用法：/remind <10m|2h|HH:MM|ISO> <提醒内容>\n"
                "示例：/remind 15m 喝水 或 /remind 21:30 准备休息"
            )
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_remind",
                "reply": reply,
            }

        time_spec = parts[0].strip()
        label = parts[1].strip()
        parsed = self._parse_remind_time(time_spec)
        if parsed is None:
            reply = (
                f"⚠️ 无法识别时间「{time_spec}」。"
                "支持格式：10m、2h、HH:MM 或 ISO 时间（如 2026-12-01T09:00:00+08:00）。"
            )
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_remind",
                "reply": reply,
            }

        trigger_iso, local_dt = parsed
        try:
            raw = self._on_remind(trigger_iso, label)
            if isinstance(raw, Mapping) and raw.get("ok") is False:
                err = self._redact_text(str(raw.get("error") or "未知错误"))
                reply = f"⚠️ 创建提醒失败：{err}"
            else:
                sid = raw.get("id") if isinstance(raw, Mapping) else None
                id_tag = f"（#{sid}）" if sid is not None else ""
                when_str = local_dt.strftime("%Y-%m-%d %H:%M")
                reply = f"⏰ 已设置提醒{id_tag}：{when_str} — {label}"
        except Exception as exc:
            err = self._redact_text(str(exc))
            logger.warning("on_remind create callback failed: %s", err)
            reply = f"⚠️ 创建提醒失败：{err}"

        self.push_text(reply)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_remind",
            "reply": reply,
        }

    @property
    def voice_enabled(self) -> bool:
        with self._state_lock:
            return self._voice_enabled

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._poll_thread is not None and self._poll_thread.is_alive()

    def _redact_text(self, text: str) -> str:
        if not self._bot_token:
            return text
        return text.replace(self._bot_token, "<redacted>")

    def _build_api_url(self, method: str) -> str:
        clean_method = str(method or "").strip().lstrip("/")
        if not clean_method:
            raise ValueError("Telegram API method must not be empty")
        # Physical guard: getFile is permanently forbidden by Layer 4 isolation.
        if "getfile" in clean_method.lower():
            raise PermissionError(
                "Telegram API 'getFile' is permanently disabled by zero-media isolation"
            )
        return f"{self._api_base}/bot{self._bot_token}/{clean_method}"

    def _call_json_api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._build_api_url(method)
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        return self._execute_request(req, method=method, timeout=timeout)

    def _call_multipart_api(
        self,
        method: str,
        fields: dict[str, str],
        *,
        file_field: str,
        filename: str,
        file_bytes: bytes,
        content_type: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._build_api_url(method)
        body, header_content_type = _encode_multipart_form_data(
            fields=fields,
            file_field=file_field,
            filename=filename,
            file_bytes=file_bytes,
            content_type=content_type,
        )
        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": header_content_type},
            method="POST",
        )
        return self._execute_request(req, method=method, timeout=timeout)

    def _execute_request(
        self,
        req: urllib.request.Request,
        *,
        method: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        req_timeout = self._http_timeout_sec if timeout is None else float(timeout)
        try:
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(exc)
            raise TelegramApiError(
                self._redact_text(
                    f"Telegram API {method} failed with HTTP {exc.code}: {err_body}"
                )
            ) from None
        except urllib.error.URLError as exc:
            raise TelegramApiError(
                self._redact_text(f"Telegram API {method} network error: {exc.reason}")
            ) from None
        except OSError as exc:
            raise TelegramApiError(
                self._redact_text(f"Telegram API {method} transport error: {exc}")
            ) from None

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramApiError(
                f"Telegram API {method} returned invalid JSON: {exc}"
            ) from None

        if not isinstance(data, dict) or not data.get("ok"):
            desc = (
                data.get("description", "unknown error")
                if isinstance(data, dict)
                else "non-object response"
            )
            raise TelegramApiError(
                self._redact_text(f"Telegram API {method} returned ok=false: {desc}")
            )
        return data

    def push_text(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a plain-text message to the single allowlisted private chat."""
        parts = _split_long_text(str(text))
        last_resp: dict[str, Any] = {}
        for idx, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": self._allowed_chat_id,
                "text": part,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None and idx == len(parts) - 1:
                payload["reply_markup"] = reply_markup
            last_resp = self._call_json_api("sendMessage", payload)
        return last_resp

    def push_proactive_event(
        self,
        event: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        """Push an eligible proactive bubble event to the allowlisted private chat.

        Only handles ``kind == "bubble"`` with ``payload.source`` in
        ``("timemap", "presence", "briefing")``. Enforces:
        - ``schedule_id`` once-only deduplication (when present),
        - quiet-hours suppression (default on for ``presence``/``briefing``,
          default off for explicit ``timemap`` reminders unless ``respect_quiet=True``),
        - per-``(source, label)`` anti-spam cooldown throttling.
        """
        if isinstance(event, Mapping):
            raw_kind = event.get("kind")
            raw_payload = event.get("payload")
        else:
            raw_kind = getattr(event, "kind", "")
            raw_payload = getattr(event, "payload", None)

        if hasattr(raw_kind, "value"):
            raw_kind = raw_kind.value
        kind = str(raw_kind or "").strip()
        if kind != "bubble":
            return {"sent": False, "reason": "ignored_kind"}

        payload: dict[str, Any] = (
            dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        )
        source = str(payload.get("source") or "").strip().lower()
        if source not in PROACTIVE_SOURCES:
            return {"sent": False, "reason": "ignored_source"}

        text = str(
            payload.get("text") or payload.get("message") or payload.get("label") or ""
        ).strip()
        if not text:
            return {"sent": False, "reason": "empty_text"}

        raw_sid = payload.get("schedule_id")
        schedule_id = (
            str(raw_sid).strip()
            if raw_sid is not None and str(raw_sid).strip()
            else ""
        )
        label = str(payload.get("label") or "").strip()
        cooldown_key = (source, label)

        data_map = payload.get("data")
        respect_quiet_raw = payload.get("respect_quiet")
        if respect_quiet_raw is None and isinstance(data_map, Mapping):
            respect_quiet_raw = data_map.get("respect_quiet")
        if respect_quiet_raw is None:
            respect_quiet = source in ("presence", "briefing")
        else:
            respect_quiet = bool(respect_quiet_raw)

        now_ts = float(self._clock())
        now_dt = datetime.fromtimestamp(now_ts, tz=self._tz)

        with self._state_lock:
            if schedule_id and schedule_id in self._sent_schedule_ids:
                return {"sent": False, "reason": "duplicate_schedule"}
            if respect_quiet and is_in_quiet_hours(now_dt, self._quiet_window):
                return {"sent": False, "reason": "quiet_hours"}
            if self._proactive_cooldown_sec > 0.0:
                last_ts = self._proactive_last_sent.get(cooldown_key)
                if (
                    last_ts is not None
                    and 0.0 <= (now_ts - last_ts) < self._proactive_cooldown_sec
                ):
                    return {"sent": False, "reason": "cooldown"}

            # Reserve slot before network I/O to avoid concurrent duplicate pushes.
            prev_cooldown_ts = self._proactive_last_sent.get(cooldown_key)
            self._proactive_last_sent[cooldown_key] = now_ts
            if schedule_id:
                self._sent_schedule_ids[schedule_id] = now_ts

        if source == "timemap" and not text.startswith("⏰"):
            formatted_text = f"⏰ {text}"
        elif source == "briefing" and not text.startswith("☀️"):
            formatted_text = f"☀️ {text}"
        else:
            formatted_text = text

        try:
            self.push_text(formatted_text)
        except Exception as exc:
            with self._state_lock:
                if schedule_id:
                    self._sent_schedule_ids.pop(schedule_id, None)
                if prev_cooldown_ts is None:
                    self._proactive_last_sent.pop(cooldown_key, None)
                else:
                    self._proactive_last_sent[cooldown_key] = prev_cooldown_ts
            logger.warning(
                "Proactive Telegram push failed: %s",
                self._redact_text(str(exc)),
            )
            return {"sent": False, "reason": "send_failed"}

        with self._state_lock:
            if len(self._sent_schedule_ids) > 4096:
                oldest_sid = next(iter(self._sent_schedule_ids))
                self._sent_schedule_ids.pop(oldest_sid, None)
            if len(self._proactive_last_sent) > 2048:
                oldest_ck = next(iter(self._proactive_last_sent))
                self._proactive_last_sent.pop(oldest_ck, None)

        return {"sent": True, "reason": "ok"}

    def push_approval_prompt(self, approval: dict[str, Any]) -> dict[str, Any]:
        """Push an L1 approval request with [✅ 批准] / [❌ 拒绝] Inline Keyboard."""
        if not isinstance(approval, dict):
            raise ValueError("approval must be a dict")
        approval_id = str(
            approval.get("approval_id") or approval.get("id") or ""
        ).strip()
        if not approval_id:
            raise ValueError("approval must include a non-empty 'approval_id'")

        action = str(approval.get("action") or "terminal_exec")
        target = str(approval.get("target") or "")
        detail = str(approval.get("detail") or "")
        timeout_sec = approval.get("remaining_sec")
        if timeout_sec is None:
            timeout_sec = approval.get("timeout_sec")

        lines = [
            "[L1 Approval Required]",
            f"ID: {approval_id}",
            f"Action: {action}",
        ]
        if target:
            lines.append(f"Target: {target}")
        if detail:
            lines.append(f"Detail: {detail}")
        if timeout_sec is not None:
            try:
                lines.append(f"Timeout: {float(timeout_sec):.0f}s")
            except (TypeError, ValueError):
                lines.append(f"Timeout: {timeout_sec}")

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ 批准", "callback_data": f"approve:{approval_id}"},
                    {"text": "❌ 拒绝", "callback_data": f"reject:{approval_id}"},
                ]
            ]
        }
        return self.push_text("\n".join(lines), reply_markup=reply_markup)

    def push_voice(self, ogg_bytes: bytes, caption: str = "") -> dict[str, Any]:
        """Send an OGG Opus voice note via stdlib multipart/form-data sendVoice."""
        if not isinstance(ogg_bytes, (bytes, bytearray)) or len(ogg_bytes) == 0:
            raise ValueError("ogg_bytes must be non-empty bytes")
        fields: dict[str, str] = {"chat_id": self._allowed_chat_id_str}
        if caption:
            fields["caption"] = str(caption)
        return self._call_multipart_api(
            "sendVoice",
            fields=fields,
            file_field="voice",
            filename="reply.ogg",
            file_bytes=bytes(ogg_bytes),
            content_type="audio/ogg",
        )

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> dict[str, Any]:
        callback_id = str(callback_query.get("id") or "").strip()
        data = str(callback_query.get("data") or "").strip()
        sender = callback_query.get("from")
        sender_id = (
            _normalize_chat_id(sender.get("id"))
            if isinstance(sender, dict)
            else self._allowed_chat_id_str
        )
        actor = f"telegram:{sender_id or self._allowed_chat_id_str}"

        verb = ""
        approval_id = ""
        if ":" in data:
            prefix, rest = data.split(":", 1)
            verb = prefix.strip().lower()
            approval_id = rest.strip()

        if verb == "brain":
            return self._handle_brain_callback(
                callback_id=callback_id,
                raw_mode=approval_id,
                callback_query=callback_query,
            )

        if verb == "think":
            return self._handle_think_callback(
                callback_id=callback_id,
                raw_effort=approval_id,
                callback_query=callback_query,
            )

        if verb == "session":
            return self._handle_session_callback(
                callback_id=callback_id,
                raw_action=approval_id,
                callback_query=callback_query,
            )

        if verb not in ("approve", "reject") or not approval_id:
            if callback_id:
                self._safe_answer_callback(callback_id, "无效的审批指令")
            return {
                "allowed": True,
                "handled": False,
                "action": "invalid_callback",
                "reason": "unsupported_callback_data",
            }

        approved = verb == "approve"
        decision_result = _invoke_approval_callback(
            self._on_approval_decision,
            approval_id,
            approved,
            actor,
        )
        resolved = True if decision_result is None else bool(decision_result)

        if resolved:
            status_label = "✅ 已批准" if approved else "❌ 已拒绝"
            toast_text = "已批准该操作" if approved else "已拒绝该操作"
        else:
            status_label = "⚠️ 审批已过期或已处理"
            toast_text = "该审批已过期或已被处理"

        if callback_id:
            self._safe_answer_callback(callback_id, toast_text)

        cb_message = callback_query.get("message")
        updated_message = False
        if isinstance(cb_message, dict) and cb_message.get("message_id") is not None:
            message_id = cb_message["message_id"]
            original_text = str(cb_message.get("text") or "").strip()
            new_text = (
                f"{original_text}\n\n{status_label} ({approval_id})"
                if original_text
                else f"{status_label} ({approval_id})"
            )
            self._call_json_api(
                "editMessageText",
                {
                    "chat_id": self._allowed_chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
            updated_message = True
        else:
            self.push_text(f"{status_label} ({approval_id})")

        return {
            "allowed": True,
            "handled": True,
            "action": "approval_callback",
            "approval_id": approval_id,
            "approved": approved,
            "resolved": resolved,
            "message_updated": updated_message,
        }

    @classmethod
    def _build_brain_keyboard(cls) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": cls._BRAIN_MODE_LABELS["auto"], "callback_data": "brain:auto"},
                ],
                [
                    {"text": cls._BRAIN_MODE_LABELS["cloud"], "callback_data": "brain:cloud"},
                    {"text": cls._BRAIN_MODE_LABELS["local"], "callback_data": "brain:local"},
                ],
            ]
        }

    def _format_brain_response(
        self,
        result: Any,
        *,
        switched: bool = False,
    ) -> tuple[str, str, bool]:
        if isinstance(result, str):
            text = self._redact_text(result.strip() or "🧠 大脑路由状态未知")
            return text, "", True

        if not isinstance(result, dict):
            text = self._redact_text(str(result or "🧠 大脑路由状态未知"))
            return text, "", True

        ok = bool(result.get("ok", True))
        raw_mode = str(result.get("mode") or result.get("route_mode") or "").strip().lower()
        resolved_mode = self._BRAIN_MODE_ALIASES.get(raw_mode, raw_mode or "unknown")
        mode_label = self._BRAIN_MODE_LABELS.get(resolved_mode, resolved_mode)
        prev_mode = str(result.get("previous_mode") or "").strip().lower()
        engine = str(result.get("engine") or "").strip()
        workers = result.get("online_brain_workers")
        last_route = result.get("last_route")
        supports_switch = result.get("supports_route_switch", True)

        lines: list[str] = []
        if not ok:
            err = str(result.get("error") or result.get("message") or "切换失败").strip()
            lines.append(f"⚠️ 大脑策略切换失败：{err}")
        elif switched:
            prev_suffix = (
                f"（原：{prev_mode}）"
                if prev_mode and prev_mode != resolved_mode
                else ""
            )
            lines.append(f"🧠 已切换大脑策略：{mode_label} ({resolved_mode}){prev_suffix}")
        else:
            lines.append(f"🧠 当前大脑策略：{mode_label} ({resolved_mode})")

        if engine:
            lines.append(f"- 当前引擎：{engine}")
        if isinstance(workers, list):
            worker_str = ", ".join(str(w) for w in workers) if workers else "无（纯云端兜底）"
            lines.append(f"- 在线大脑节点：{worker_str}")
        if isinstance(last_route, dict) and last_route:
            target = str(last_route.get("target") or "unknown")
            wid = str(last_route.get("worker_id") or "").strip()
            elapsed = last_route.get("elapsed_ms")
            route_desc = target
            if wid:
                route_desc += f" ({wid})"
            if elapsed is not None:
                route_desc += f" · {elapsed}ms"
            lines.append(f"- 最近路由：{route_desc}")
        if supports_switch is False:
            lines.append("- 提示：当前后端引擎不支持热切换路由")
        else:
            lines.append("点击下方按钮可随时热切换：")

        return self._redact_text("\n".join(lines)), resolved_mode, ok

    def _send_brain_panel(self, text: str, *, include_keyboard: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._allowed_chat_id,
            "text": text,
        }
        if include_keyboard:
            payload["reply_markup"] = self._build_brain_keyboard()
        return self._call_json_api("sendMessage", payload)

    def _handle_brain_command(self, args: str) -> dict[str, Any]:
        if self._on_route is None:
            reply_text = "⚠️ 当前未启用大脑路由切换（on_route 未配置）。"
            self.push_text(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_brain",
                "switched": False,
                "reason": "route_handler_unavailable",
                "reply": reply_text,
            }

        raw_arg = args.strip().split()[0].lower() if args.strip() else ""
        if not raw_arg:
            try:
                info = self._on_route(None)
            except Exception as exc:
                reply_text = self._redact_text(f"⚠️ 查询大脑路由失败：{exc}")
                self._send_brain_panel(reply_text)
                return {
                    "allowed": True,
                    "handled": True,
                    "action": "command_brain",
                    "switched": False,
                    "ok": False,
                    "reply": reply_text,
                }
            reply_text, resolved_mode, ok = self._format_brain_response(info, switched=False)
            include_kb = not (isinstance(info, dict) and info.get("supports_route_switch") is False)
            self._send_brain_panel(reply_text, include_keyboard=include_kb)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_brain",
                "mode": resolved_mode,
                "switched": False,
                "ok": ok,
                "reply": reply_text,
            }

        canonical = self._BRAIN_MODE_ALIASES.get(raw_arg)
        if not canonical:
            reply_text = (
                f"⚠️ 不支持的大脑模式：{raw_arg}\n"
                "可用参数：/brain [auto|hybrid|cloud|api|local|agy]\n"
                "或直接点击下方按钮一键切换："
            )
            self._send_brain_panel(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_brain",
                "switched": False,
                "ok": False,
                "reason": "invalid_mode",
                "reply": reply_text,
            }

        try:
            result = self._on_route(canonical)
        except Exception as exc:
            reply_text = self._redact_text(f"⚠️ 切换大脑策略失败：{exc}")
            self._send_brain_panel(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_brain",
                "mode": canonical,
                "switched": False,
                "ok": False,
                "reply": reply_text,
            }

        reply_text, resolved_mode, ok = self._format_brain_response(result, switched=True)
        self._send_brain_panel(reply_text, include_keyboard=True)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_brain",
            "mode": resolved_mode or canonical,
            "switched": ok,
            "ok": ok,
            "reply": reply_text,
        }

    def _handle_brain_callback(
        self,
        *,
        callback_id: str,
        raw_mode: str,
        callback_query: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = self._BRAIN_MODE_ALIASES.get(raw_mode.strip().lower())
        if not canonical:
            if callback_id:
                self._safe_answer_callback(callback_id, "无效的大脑模式指令")
            return {
                "allowed": True,
                "handled": False,
                "action": "invalid_callback",
                "reason": "unsupported_brain_mode",
            }

        if self._on_route is None:
            if callback_id:
                self._safe_answer_callback(callback_id, "当前未启用大脑路由切换")
            return {
                "allowed": True,
                "handled": False,
                "action": "brain_callback",
                "mode": canonical,
                "ok": False,
                "reason": "route_handler_unavailable",
            }

        try:
            result = self._on_route(canonical)
        except Exception as exc:
            err_msg = self._redact_text(str(exc))
            if callback_id:
                self._safe_answer_callback(callback_id, f"切换失败：{err_msg[:120]}")
            return {
                "allowed": True,
                "handled": True,
                "action": "brain_callback",
                "mode": canonical,
                "ok": False,
                "message_updated": False,
                "error": err_msg,
            }

        new_text, resolved_mode, ok = self._format_brain_response(result, switched=True)
        final_mode = resolved_mode or canonical
        mode_label = self._BRAIN_MODE_LABELS.get(final_mode, final_mode)
        if ok:
            toast_text = f"已切换为 {mode_label}"
        else:
            err = (
                str(result.get("error") or result.get("message") or "切换失败")
                if isinstance(result, dict)
                else "切换失败"
            )
            toast_text = f"切换失败：{err[:120]}"

        if callback_id:
            self._safe_answer_callback(callback_id, toast_text)

        keyboard = self._build_brain_keyboard()
        cb_message = callback_query.get("message")
        updated_message = False
        if isinstance(cb_message, dict) and cb_message.get("message_id") is not None:
            message_id = cb_message["message_id"]
            try:
                self._call_json_api(
                    "editMessageText",
                    {
                        "chat_id": self._allowed_chat_id,
                        "message_id": message_id,
                        "text": new_text,
                        "reply_markup": keyboard,
                    },
                )
                updated_message = True
            except TelegramApiError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        else:
            self._send_brain_panel(new_text, include_keyboard=True)

        return {
            "allowed": True,
            "handled": True,
            "action": "brain_callback",
            "mode": final_mode,
            "ok": ok,
            "message_updated": updated_message,
            "reply": new_text,
        }

    @classmethod
    def _build_think_keyboard(cls) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": cls._THINK_EFFORT_LABELS["minimal"], "callback_data": "think:minimal"},
                    {"text": cls._THINK_EFFORT_LABELS["low"], "callback_data": "think:low"},
                    {"text": cls._THINK_EFFORT_LABELS["medium"], "callback_data": "think:medium"},
                ],
                [
                    {"text": cls._THINK_EFFORT_LABELS["high"], "callback_data": "think:high"},
                    {"text": cls._THINK_EFFORT_LABELS["max"], "callback_data": "think:max"},
                    {"text": cls._THINK_EFFORT_LABELS["default"], "callback_data": "think:default"},
                ],
            ]
        }

    def _format_think_response(
        self,
        result: Any,
        *,
        switched: bool,
    ) -> tuple[str, str, bool]:
        if not isinstance(result, dict):
            return self._redact_text(_coerce_text(result)), "", True

        ok = bool(result.get("ok", True))
        raw_effort = str(result.get("reasoning_effort") or "").strip().lower()
        prev_effort = str(result.get("previous_reasoning_effort") or "").strip().lower()
        engine = str(result.get("engine") or "").strip()
        model = str(result.get("model") or "").strip()
        supports_switch = result.get("supports_reasoning_switch")

        label = self._THINK_EFFORT_LABELS.get(raw_effort, raw_effort or "🔄 默认")
        prev_label = self._THINK_EFFORT_LABELS.get(prev_effort, prev_effort or "🔄 默认")

        if not ok:
            err = str(result.get("error") or result.get("detail") or result.get("message") or "切换失败").strip()
            lines = [f"⚠️ 切换思考深度失败：{err}"]
            if engine:
                lines.append(f"- 当前引擎：{engine}")
            return self._redact_text("\n".join(lines)), raw_effort, False

        header = f"✅ 已切换思考深度：{label}" if switched else "🧠 思考深度面板"
        lines = [header]
        if engine and model:
            lines.append(f"- 当前引擎：{engine} ({model})")
        elif engine:
            lines.append(f"- 当前引擎：{engine}")
        elif model:
            lines.append(f"- 当前模型：{model}")
        lines.append(f"- 当前档位：{label} ({raw_effort or 'default'})")
        if switched and prev_effort != raw_effort:
            lines.append(f"- 变更：{prev_label} → {label}")
        if supports_switch is False:
            lines.append("- 提示：当前后端引擎不支持热切换思考深度")
        else:
            lines.append("点击下方按钮可随时热切换：")

        return self._redact_text("\n".join(lines)), raw_effort, True

    def _send_think_panel(self, text: str, *, include_keyboard: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._allowed_chat_id,
            "text": text,
        }
        if include_keyboard:
            payload["reply_markup"] = self._build_think_keyboard()
        return self._call_json_api("sendMessage", payload)

    def _handle_think_command(self, args: str) -> dict[str, Any]:
        if self._on_reasoning is None:
            reply_text = "⚠️ 当前未启用思考深度切换（on_reasoning 未配置）。"
            self.push_text(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_think",
                "switched": False,
                "reason": "reasoning_handler_unavailable",
                "reply": reply_text,
            }

        raw_arg = args.strip().split()[0].lower() if args.strip() else ""
        if not raw_arg:
            try:
                info = self._on_reasoning(None)
            except Exception as exc:
                reply_text = self._redact_text(f"⚠️ 查询思考深度失败：{exc}")
                self._send_think_panel(reply_text)
                return {
                    "allowed": True,
                    "handled": True,
                    "action": "command_think",
                    "switched": False,
                    "ok": False,
                    "reply": reply_text,
                }
            reply_text, resolved_effort, ok = self._format_think_response(info, switched=False)
            include_kb = not (
                isinstance(info, dict) and info.get("supports_reasoning_switch") is False
            )
            self._send_think_panel(reply_text, include_keyboard=include_kb)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_think",
                "effort": resolved_effort,
                "switched": False,
                "ok": ok,
                "reply": reply_text,
            }

        canonical = self._THINK_EFFORT_ALIASES.get(raw_arg)
        if not canonical:
            reply_text = (
                f"⚠️ 不支持的思考深度：{raw_arg}\n"
                "可用参数：/think [minimal|low|medium|high|max|default]\n"
                "或直接点击下方按钮一键切换："
            )
            self._send_think_panel(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_think",
                "switched": False,
                "ok": False,
                "reason": "invalid_effort",
                "reply": reply_text,
            }

        try:
            result = self._on_reasoning(canonical)
        except Exception as exc:
            reply_text = self._redact_text(f"⚠️ 切换思考深度失败：{exc}")
            self._send_think_panel(reply_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_think",
                "effort": "" if canonical == "default" else canonical,
                "switched": False,
                "ok": False,
                "reply": reply_text,
            }

        reply_text, resolved_effort, ok = self._format_think_response(result, switched=True)
        self._send_think_panel(reply_text, include_keyboard=True)
        final_effort = resolved_effort if ok else ("" if canonical == "default" else canonical)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_think",
            "effort": final_effort,
            "switched": ok,
            "ok": ok,
            "reply": reply_text,
        }

    def _handle_think_callback(
        self,
        *,
        callback_id: str,
        raw_effort: str,
        callback_query: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = self._THINK_EFFORT_ALIASES.get(raw_effort.strip().lower())
        if not canonical:
            if callback_id:
                self._safe_answer_callback(callback_id, "无效的思考深度指令")
            return {
                "allowed": True,
                "handled": False,
                "action": "invalid_callback",
                "reason": "unsupported_reasoning_effort",
            }

        if self._on_reasoning is None:
            if callback_id:
                self._safe_answer_callback(callback_id, "当前未启用思考深度切换")
            return {
                "allowed": True,
                "handled": False,
                "action": "think_callback",
                "effort": "" if canonical == "default" else canonical,
                "ok": False,
                "reason": "reasoning_handler_unavailable",
            }

        try:
            result = self._on_reasoning(canonical)
        except Exception as exc:
            err_msg = self._redact_text(str(exc))
            if callback_id:
                self._safe_answer_callback(callback_id, f"切换失败：{err_msg[:120]}")
            return {
                "allowed": True,
                "handled": True,
                "action": "think_callback",
                "effort": "" if canonical == "default" else canonical,
                "ok": False,
                "message_updated": False,
                "error": err_msg,
            }

        new_text, resolved_effort, ok = self._format_think_response(result, switched=True)
        final_effort = resolved_effort if ok else ("" if canonical == "default" else canonical)
        effort_label = self._THINK_EFFORT_LABELS.get(final_effort, final_effort or "🔄 默认")
        if ok:
            toast_text = f"已切换为 {effort_label}"
        else:
            err = (
                str(result.get("error") or result.get("message") or "切换失败")
                if isinstance(result, dict)
                else "切换失败"
            )
            toast_text = f"切换失败：{err[:120]}"

        if callback_id:
            self._safe_answer_callback(callback_id, toast_text)

        keyboard = self._build_think_keyboard()
        cb_message = callback_query.get("message")
        updated_message = False
        if isinstance(cb_message, dict) and cb_message.get("message_id") is not None:
            message_id = cb_message["message_id"]
            try:
                self._call_json_api(
                    "editMessageText",
                    {
                        "chat_id": self._allowed_chat_id,
                        "message_id": message_id,
                        "text": new_text,
                        "reply_markup": keyboard,
                    },
                )
                updated_message = True
            except TelegramApiError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        else:
            self._send_think_panel(new_text, include_keyboard=True)

        return {
            "allowed": True,
            "handled": True,
            "action": "think_callback",
            "effort": final_effort,
            "ok": ok,
            "message_updated": updated_message,
            "reply": new_text,
        }

    def _safe_send_typing(self) -> None:
        try:
            self._call_json_api(
                "sendChatAction",
                {
                    "chat_id": self._allowed_chat_id,
                    "action": "typing",
                },
            )
        except Exception as exc:
            logger.debug(
                "sendChatAction(typing) failed (ignored): %s",
                self._redact_text(str(exc)),
            )

    def _safe_answer_callback(self, callback_query_id: str, text: str) -> None:
        try:
            self._call_json_api(
                "answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text},
            )
        except TelegramApiError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def _handle_voice_command(self, args: str) -> dict[str, Any]:
        token = args.strip().lower()
        with self._state_lock:
            if token in ("on", "1", "true", "enable"):
                self._voice_enabled = True
                if self._on_tts is None:
                    reply = "语音回复已开启（当前未配置 TTS 引擎，暂仅发送文字）。"
                else:
                    reply = "语音回复已开启：后续文字回复将附带语音条。"
            elif token in ("off", "0", "false", "disable"):
                self._voice_enabled = False
                reply = "语音回复已关闭：后续仅发送纯文字回复。"
            else:
                state = "开启" if self._voice_enabled else "关闭"
                reply = f"当前语音回复状态：{state}。用法：/voice on 或 /voice off"
            current_state = self._voice_enabled

        self.push_text(reply)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_voice",
            "voice_enabled": current_state,
            "reply": reply,
        }

    def _handle_help_command(self) -> dict[str, Any]:
        with self._state_lock:
            voice_state = "开启" if self._voice_enabled else "关闭"
        reply = "\n".join(
            [
                "📖 Shinobu Telegram 指令速查",
                "",
                "💬 对话与记忆",
                "• /session（别名 /sessions、/archive）— 对话管理与归档面板",
                "• /new — 秒开新对话（快速归档旧对话）",
                "• /sleep — 整理记忆并归档当前对话",
                "",
                "🧠 大脑与思考",
                "• /brain [cloud|hybrid|local]（别名 /mode）— 查看或切换推理引擎",
                "• /think [off|low|medium|high]（别名 /effort）— 查看或调整思考深度",
                "",
                "⏰ 提醒与语音",
                "• /remind [10m|2h|HH:MM|ISO 内容]（别名 /reminders）— 查看待触发提醒或新建定时提醒",
                f"• /voice [on|off] — 开关语音回复（当前：{voice_state}）",
                "",
                "📡 运维与帮助",
                "• /status — 查看系统、活跃引擎与待审批状态",
                "• /nodes — 查看在线边缘 / 算力节点",
                "• /help（别名 /commands）— 查看全部可用指令与用法",
            ]
        )
        self.push_text(reply)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_help",
            "reply": reply,
        }

    @staticmethod
    def _format_sleep_result(target: Any) -> str:
        def _get(obj: Any, key: str) -> Any:
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        summary = _get(target, "summary")
        new_facts = _get(target, "new_facts")
        journal_path = _get(target, "journal_path")

        lines = ["🌙 记忆归档完成："]
        summary_text = str(summary).strip() if summary is not None else ""
        if summary_text:
            lines.append(f"• 摘要：{summary_text}")

        if isinstance(new_facts, (list, tuple, set)):
            lines.append(f"• 新增事实：{len(new_facts)} 条")
        elif isinstance(new_facts, int):
            lines.append(f"• 新增事实：{new_facts} 条")

        journal_text = str(journal_path).strip() if journal_path is not None else ""
        if journal_text:
            lines.append(f"• 日记路径：{journal_text}")

        lines.append("下条消息自动开启新对话。")
        return "\n".join(lines)

    def _handle_sleep_command(self) -> dict[str, Any]:
        if self._on_sleep is None:
            reply = "当前未启用记忆归档（/sleep），无法执行手动休眠归档。"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_sleep",
                "ok": False,
                "status": "disabled",
                "reply": reply,
            }

        try:
            raw = self._on_sleep()
        except Exception as exc:
            err = self._redact_text(str(exc))
            logger.warning("on_sleep callback failed: %s", err)
            reply = f"记忆归档失败：{err}"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_sleep",
                "ok": False,
                "status": "error",
                "reply": reply,
            }

        ok = True
        status = "consolidated"

        if raw is None:
            status = "idle"
            reply = "当前没有活跃对话需要归档，下条消息自动开启新对话。"
        elif isinstance(raw, str):
            body = raw.strip() or "记忆归档已完成。"
            status = "ok"
            if "新对话" not in body:
                reply = f"{body}\n下条消息自动开启新对话。"
            else:
                reply = body
        elif isinstance(raw, dict):
            raw_status = str(raw.get("status") or "").strip().lower()
            raw_ok = raw.get("ok")
            if raw_status == "idle":
                ok = True if raw_ok is None else bool(raw_ok)
                status = "idle"
                msg = str(raw.get("message") or "").strip()
                reply = msg or "当前没有活跃对话需要归档，下条消息自动开启新对话。"
            elif raw_ok is False:
                ok = False
                status = raw_status or "error"
                err = str(raw.get("error") or raw.get("message") or "未知错误").strip()
                reply = f"记忆归档失败：{self._redact_text(err)}"
            else:
                ok = True
                status = raw_status or "consolidated"
                res = raw.get("result")
                target = res if res is not None else raw
                reply = self._format_sleep_result(target)
        else:
            reply = self._format_sleep_result(raw)

        self.push_text(reply)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_sleep",
            "ok": ok,
            "status": status,
            "reply": reply,
        }

    @classmethod
    def _build_session_keyboard(cls) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🌙 整理记忆并新开", "callback_data": "session:sleep"},
                ],
                [
                    {"text": "🆕 秒开新对话（仅归档）", "callback_data": "session:new"},
                ],
                [
                    {"text": "🔄 刷新", "callback_data": "session:refresh"},
                ],
            ]
        }

    def _send_session_panel(
        self, text: str, *, include_keyboard: bool = True
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._allowed_chat_id,
            "text": text,
        }
        if include_keyboard:
            payload["reply_markup"] = self._build_session_keyboard()
        return self._call_json_api("sendMessage", payload)

    @staticmethod
    def _short_episode_id(raw_id: Any) -> str:
        val = str(raw_id or "").strip()
        if not val:
            return "?"
        return val[:8] if len(val) > 8 else val

    def _format_session_panel(
        self, raw: Any, *, notice: str | None = None
    ) -> tuple[str, bool]:
        if isinstance(raw, str):
            body = raw.strip() or "💬 对话管理面板"
            if notice:
                body = f"{body}\n\n{notice}"
            return body, True

        if not isinstance(raw, dict):
            lines = [
                "💬 对话管理面板",
                "当前活跃对话：暂无活跃对话（下条消息自动开启新对话）",
                "",
                "📚 最近归档：暂无归档记录",
            ]
            if notice:
                lines.extend(["", notice])
            return "\n".join(lines), True

        if raw.get("ok") is False:
            err = self._redact_text(
                str(raw.get("error") or raw.get("message") or "查询失败").strip()
            )
            lines = [
                "💬 对话管理面板",
                f"⚠️ 无法读取对话状态：{err}",
            ]
            if notice:
                lines.extend(["", notice])
            return "\n".join(lines), False

        lines = ["💬 对话管理面板"]
        active = raw.get("active_episode")
        if active is None:
            active = raw.get("active")

        if isinstance(active, dict) and str(active.get("id") or "").strip():
            short_id = self._short_episode_id(active.get("id"))
            tc = active.get("turn_count")
            tc_text = f"{int(tc)} 条消息" if isinstance(tc, int) else "进行中"
            lines.append(f"当前活跃对话：#{short_id}（已聊 {tc_text}）")
        else:
            lines.append("当前活跃对话：暂无活跃对话（下条消息自动开启新对话）")

        recent = raw.get("recent_closed")
        if not isinstance(recent, list):
            recent = raw.get("recent") or raw.get("episodes") or []
        closed_items = [item for item in recent if isinstance(item, dict)][:3]

        lines.append("")
        if closed_items:
            lines.append("📚 最近归档（最多 3 条）：")
            for item in closed_items:
                short_id = self._short_episode_id(item.get("id"))
                tc = item.get("turn_count")
                tc_tag = f"[{int(tc)}条] " if isinstance(tc, int) else ""
                summary = " ".join(str(item.get("summary") or "").split())
                if not summary:
                    summary = "（快速归档，无摘要）"
                elif len(summary) > 60:
                    summary = summary[:59].rstrip() + "…"
                lines.append(f"• #{short_id} {tc_tag}{summary}")
        else:
            lines.append("📚 最近归档：暂无归档记录")

        if notice:
            lines.extend(["", notice])
        return "\n".join(lines), True

    @staticmethod
    def _format_chat_error_notice(redacted: str) -> str:
        lower = redacted.lower()
        if "http 429" in lower or "resource_exhausted" in lower:
            return (
                "⚠️ 对话暂时失败：云端模型当前限流拥堵（HTTP 429），"
                "已自动重试与降级仍繁忙，请稍候再试或发 /brain 切回自动混动。"
            )
        if "http 503" in lower or "unavailable" in lower:
            return (
                "⚠️ 对话暂时失败：云端模型服务暂时不可用（HTTP 503），"
                "请稍候再试或发 /brain 切回自动混动。"
            )
        return f"⚠️ 对话暂时失败：{redacted[:240]}"

    def _parse_new_session_result(self, raw: Any) -> tuple[bool, str, str]:
        if raw is None:
            return True, "idle", "当前已是干净新对话，直接发消息即可开始。"

        if isinstance(raw, str):
            body = raw.strip() or "🆕 已归档旧对话，下条消息开启新对话。"
            return True, "archived", body

        if isinstance(raw, dict):
            raw_ok = raw.get("ok")
            raw_status = str(raw.get("status") or "").strip().lower()
            if raw_ok is False:
                err = self._redact_text(
                    str(raw.get("error") or raw.get("message") or "未知错误").strip()
                )
                return False, raw_status or "error", f"快速归档失败：{err}"
            if raw_status == "idle":
                msg = str(raw.get("message") or "").strip()
                return True, "idle", msg or "当前已是干净新对话，直接发消息即可开始。"

            closed_id = str(raw.get("closed_episode_id") or "").strip()
            turn_count = raw.get("turn_count")
            details: list[str] = []
            if closed_id:
                details.append(f"#{self._short_episode_id(closed_id)}")
            if isinstance(turn_count, int) and turn_count > 0:
                details.append(f"{turn_count} 条消息")
            reply = "🆕 已归档旧对话，下条消息开启新对话。"
            if details:
                reply += f"\n（归档记录：{' · '.join(details)}）"
            return True, raw_status or "archived", reply

        return True, "archived", "🆕 已归档旧对话，下条消息开启新对话。"

    def _handle_new_session_command(self) -> dict[str, Any]:
        assert self._on_new_session is not None
        try:
            raw = self._on_new_session()
        except Exception as exc:
            err = self._redact_text(str(exc))
            logger.warning("on_new_session callback failed: %s", err)
            reply = f"快速归档失败：{err}"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_new",
                "ok": False,
                "status": "error",
                "reply": reply,
            }

        ok, status, reply = self._parse_new_session_result(raw)
        self.push_text(reply)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_new",
            "ok": ok,
            "status": status,
            "reply": reply,
        }

    def _handle_session_command(self) -> dict[str, Any]:
        if self._on_session is None:
            reply = "当前未启用对话管理面板（/session）。"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_session",
                "ok": False,
                "status": "disabled",
                "reply": reply,
            }

        try:
            raw = self._on_session()
        except Exception as exc:
            err = self._redact_text(str(exc))
            logger.warning("on_session callback failed: %s", err)
            reply = f"无法读取对话状态：{err}"
            self.push_text(reply)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_session",
                "ok": False,
                "status": "error",
                "reply": reply,
            }

        panel_text, panel_ok = self._format_session_panel(raw)
        self._send_session_panel(panel_text, include_keyboard=True)
        return {
            "allowed": True,
            "handled": True,
            "action": "command_session",
            "ok": panel_ok,
            "reply": panel_text,
        }

    def _handle_session_callback(
        self,
        *,
        callback_id: str,
        raw_action: str,
        callback_query: dict[str, Any],
    ) -> dict[str, Any]:
        sub_action = str(raw_action or "").strip().lower()
        if sub_action not in ("sleep", "new", "refresh"):
            if callback_id:
                self._safe_answer_callback(callback_id, "无效的对话操作")
            return {
                "allowed": True,
                "handled": False,
                "action": "invalid_callback",
                "reason": "unsupported_session_action",
            }

        action_ok = True
        toast_text = "🔄 已刷新对话列表"
        notice: str | None = None

        if sub_action == "sleep":
            if self._on_sleep is None:
                action_ok = False
                toast_text = "未启用记忆归档（/sleep）"
                notice = "⚠️ 当前未启用记忆归档（/sleep）。"
            else:
                try:
                    raw_sleep = self._on_sleep()
                    if raw_sleep is None:
                        toast_text = "当前无活跃对话可归档"
                        notice = "ℹ️ 当前没有活跃对话需要归档，下条消息自动开启新对话。"
                    elif isinstance(raw_sleep, dict):
                        st = str(raw_sleep.get("status") or "").strip().lower()
                        if st == "idle":
                            toast_text = "当前无活跃对话可归档"
                            notice = str(raw_sleep.get("message") or "").strip() or (
                                "ℹ️ 当前没有活跃对话需要归档，下条消息自动开启新对话。"
                            )
                        elif raw_sleep.get("ok") is False:
                            action_ok = False
                            err = self._redact_text(
                                str(raw_sleep.get("error") or raw_sleep.get("message") or "未知错误")
                            )
                            toast_text = f"归档失败：{err[:100]}"
                            notice = f"⚠️ 记忆归档失败：{err}"
                        else:
                            toast_text = "🌙 已整理记忆并开启新对话"
                            notice = "🌙 已完成记忆整理与归档，下条消息开启新对话。"
                    else:
                        toast_text = "🌙 已整理记忆并开启新对话"
                        notice = "🌙 已完成记忆整理与归档，下条消息开启新对话。"
                except Exception as exc:
                    action_ok = False
                    err = self._redact_text(str(exc))
                    logger.warning("session:sleep callback failed: %s", err)
                    toast_text = f"归档失败：{err[:100]}"
                    notice = f"⚠️ 记忆归档失败：{err}"

        elif sub_action == "new":
            if self._on_new_session is not None:
                try:
                    raw_new = self._on_new_session()
                    ok_new, status_new, reply_new = self._parse_new_session_result(raw_new)
                    action_ok = ok_new
                    if not ok_new:
                        toast_text = reply_new[:120]
                        notice = f"⚠️ {reply_new}"
                    elif status_new == "idle":
                        toast_text = "当前已是干净新对话"
                        notice = f"ℹ️ {reply_new}"
                    else:
                        toast_text = "🆕 已归档旧对话，下条消息开启新对话"
                        notice = reply_new
                except Exception as exc:
                    action_ok = False
                    err = self._redact_text(str(exc))
                    logger.warning("session:new callback failed: %s", err)
                    toast_text = f"快速归档失败：{err[:100]}"
                    notice = f"⚠️ 快速归档失败：{err}"
            elif self._on_sleep is not None:
                try:
                    self._on_sleep()
                    toast_text = "🆕 已归档旧对话，下条消息开启新对话"
                    notice = "🆕 已归档旧对话，下条消息开启新对话。"
                except Exception as exc:
                    action_ok = False
                    err = self._redact_text(str(exc))
                    toast_text = f"归档失败：{err[:100]}"
                    notice = f"⚠️ 归档失败：{err}"
            else:
                action_ok = False
                toast_text = "未启用快速归档（/new）"
                notice = "⚠️ 当前未启用快速归档（/new）。"

        # Refresh session panel text
        if self._on_session is not None:
            try:
                raw_panel = self._on_session()
                new_text, panel_ok = self._format_session_panel(raw_panel, notice=notice)
                if sub_action == "refresh" and not panel_ok:
                    action_ok = False
                    toast_text = "刷新失败，请稍后重试"
            except Exception as exc:
                err = self._redact_text(str(exc))
                logger.warning("session panel refresh failed: %s", err)
                if sub_action == "refresh":
                    action_ok = False
                    toast_text = f"刷新失败：{err[:100]}"
                new_text = f"💬 对话管理面板\n⚠️ 刷新失败：{err}"
                if notice:
                    new_text += f"\n\n{notice}"
        else:
            new_text = notice or "💬 对话管理面板"

        if callback_id:
            self._safe_answer_callback(callback_id, toast_text)

        keyboard = self._build_session_keyboard()
        cb_message = callback_query.get("message")
        updated_message = False
        if isinstance(cb_message, dict) and cb_message.get("message_id") is not None:
            message_id = cb_message["message_id"]
            try:
                self._call_json_api(
                    "editMessageText",
                    {
                        "chat_id": self._allowed_chat_id,
                        "message_id": message_id,
                        "text": new_text,
                        "reply_markup": keyboard,
                    },
                )
                updated_message = True
            except TelegramApiError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        else:
            self._send_session_panel(new_text, include_keyboard=True)

        return {
            "allowed": True,
            "handled": True,
            "action": "session_callback",
            "sub_action": sub_action,
            "ok": action_ok,
            "message_updated": updated_message,
            "reply": new_text,
        }

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Process a single Telegram Update after enforcing 4-layer isolation."""
        allowed, reason = filter_update(update, self._allowed_chat_id_str)
        if not allowed:
            if reason.startswith("blocked_forward"):
                self.push_text(FORWARD_BLOCKED_NOTICE)
                return {
                    "allowed": False,
                    "handled": True,
                    "action": "blocked_forward",
                    "reason": reason,
                    "notice_sent": True,
                }
            if reason.startswith("blocked_media"):
                self.push_text(MEDIA_BLOCKED_NOTICE)
                return {
                    "allowed": False,
                    "handled": True,
                    "action": "blocked_media",
                    "reason": reason,
                    "notice_sent": True,
                }
            # Layer 1 (non-private) & Layer 2 (unauthorized): silent drop
            return {
                "allowed": False,
                "handled": False,
                "action": "dropped",
                "reason": reason,
            }

        if reason == "ok_callback_query":
            return self._handle_callback_query(update["callback_query"])

        update_key, msg = _extract_update_message(update)
        if update_key != "message" or msg is None:
            return {
                "allowed": True,
                "handled": False,
                "action": "ignored_update",
                "reason": update_key or "no_message",
            }

        raw_text = msg.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return {
                "allowed": True,
                "handled": False,
                "action": "ignored_non_text",
                "reason": "no_text",
            }

        text = raw_text.strip()
        cmd, args = _parse_command(text)

        if cmd == "/status":
            status_text = _format_status_response(self._on_status())
            self.push_text(status_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_status",
                "reply": status_text,
            }

        if cmd == "/nodes":
            nodes_text = _format_nodes_response(self._on_nodes())
            self.push_text(nodes_text)
            return {
                "allowed": True,
                "handled": True,
                "action": "command_nodes",
                "reply": nodes_text,
            }

        if cmd == "/voice":
            return self._handle_voice_command(args)

        if cmd in ("/brain", "/mode"):
            return self._handle_brain_command(args)

        if cmd in ("/think", "/effort"):
            return self._handle_think_command(args)

        if cmd == "/sleep":
            return self._handle_sleep_command()

        if cmd == "/new":
            if self._on_new_session is not None:
                return self._handle_new_session_command()
            return self._handle_sleep_command()

        if cmd in ("/session", "/sessions", "/archive"):
            return self._handle_session_command()

        if cmd in ("/remind", "/reminders"):
            return self._handle_remind_command(args)

        if cmd in ("/help", "/commands"):
            return self._handle_help_command()

        typing_stop: threading.Event | None = None
        typing_thread: threading.Thread | None = None
        if self._typing_indicator:
            self._safe_send_typing()
            typing_stop = threading.Event()
            refresh_sec = self._typing_refresh_sec

            def _typing_loop() -> None:
                assert typing_stop is not None
                while not typing_stop.wait(refresh_sec):
                    if self._stop_event.is_set():
                        break
                    self._safe_send_typing()

            typing_thread = threading.Thread(
                target=_typing_loop,
                name="shinobu-tg-typing",
                daemon=True,
            )
            typing_thread.start()

        try:
            reply_raw = self._on_chat(text)
        except Exception as exc:
            if typing_stop is not None:
                typing_stop.set()
            if typing_thread is not None:
                typing_thread.join(timeout=1.0)
            typing_stop = None
            typing_thread = None
            redacted = self._redact_text(str(exc)).strip() or type(exc).__name__
            logger.exception("Telegram chat handler failed: %s", redacted)
            try:
                self.push_text(self._format_chat_error_notice(redacted))
            except Exception:
                pass
            return {
                "allowed": True,
                "handled": True,
                "action": "chat_error",
                "error": redacted[:240],
            }
        finally:
            if typing_stop is not None:
                typing_stop.set()
            if typing_thread is not None:
                typing_thread.join(timeout=1.0)
        reply_text = _coerce_text(reply_raw)
        self.push_text(reply_text)

        voice_sent = False
        if self.voice_enabled and self._on_tts is not None and reply_text.strip():
            try:
                ogg_bytes = self._on_tts(reply_text)
                if isinstance(ogg_bytes, (bytes, bytearray)) and len(ogg_bytes) > 0:
                    self.push_voice(bytes(ogg_bytes))
                    voice_sent = True
            except Exception as exc:
                logger.warning("TTS synthesis or sendVoice failed: %s", exc)

        return {
            "allowed": True,
            "handled": True,
            "action": "chat",
            "reply": reply_text,
            "voice_sent": voice_sent,
        }

    def poll_once(self, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """Fetch one batch of updates via getUpdates and handle them sequentially."""
        poll_timeout = self._poll_timeout_sec if timeout is None else max(0, int(timeout))
        with self._state_lock:
            offset = self._offset

        payload: dict[str, Any] = {
            "offset": offset,
            "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        req_timeout = max(self._http_timeout_sec, float(poll_timeout) + 5.0)
        resp = self._call_json_api("getUpdates", payload, timeout=req_timeout)
        updates = resp.get("result")
        if not isinstance(updates, list):
            return []

        outcomes: list[dict[str, Any]] = []
        for item in updates:
            if not isinstance(item, dict):
                continue
            update_id = item.get("update_id")
            if isinstance(update_id, int):
                with self._state_lock:
                    if update_id + 1 > self._offset:
                        self._offset = update_id + 1
            try:
                outcomes.append(self.handle_update(item))
            except Exception as exc:
                logger.exception(
                    "Unhandled error while processing Telegram update %s: %s",
                    update_id,
                    self._redact_text(str(exc)),
                )
                outcomes.append(
                    {
                        "allowed": False,
                        "handled": False,
                        "action": "error",
                        "reason": self._redact_text(str(exc)),
                    }
                )
        return outcomes

    def _polling_loop(self) -> None:
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                self.poll_once()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                delay = min(
                    self._backoff_max_sec,
                    self._backoff_initial_sec * (2 ** (consecutive_errors - 1)),
                )
                logger.warning(
                    "Telegram polling error (#%d): %s; retrying in %.2fs",
                    consecutive_errors,
                    self._redact_text(str(exc)),
                    delay,
                )
                if self._stop_event.wait(delay):
                    break

    def register_bot_commands(self) -> bool:
        """Register Telegram bot `/` menu commands via setMyCommands (fault-isolated)."""
        try:
            self._call_json_api(
                "setMyCommands",
                {"commands": [dict(item) for item in self._BOT_COMMANDS]},
            )
            return True
        except Exception as exc:
            logger.warning(
                "setMyCommands failed (ignored): %s",
                self._redact_text(str(exc)),
            )
            return False

    def start_polling(self) -> bool:
        """Start the background long-polling daemon thread (idempotent)."""
        with self._state_lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return False
            if self._register_commands:
                self.register_bot_commands()
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._polling_loop,
                name="shinobu-telegram-poller",
                daemon=True,
            )
            self._poll_thread = thread
            thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background polling thread to stop and wait for exit."""
        self._stop_event.set()
        with self._state_lock:
            thread = self._poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._state_lock:
            if self._poll_thread is thread and (thread is None or not thread.is_alive()):
                self._poll_thread = None


__all__ = [
    "FORWARD_BLOCKED_FIELDS",
    "FORWARD_BLOCKED_NOTICE",
    "MEDIA_BLOCKED_FIELDS",
    "MEDIA_BLOCKED_NOTICE",
    "TelegramApiError",
    "TelegramBotBridge",
    "filter_update",
]
