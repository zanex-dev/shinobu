# -*- coding: utf-8 -*-
"""
Lazy-loaded PySide6 transparent desktop pet shell and pure-Python UI state helpers.

Architecture:
  - Zero-business-logic "Dumb Shell": all conversation, memory, and schedule
    logic remains in the local loopback daemon accessed via `DaemonClient`.
  - Zero top-level GUI dependency: `PySide6` is imported strictly inside
    functions so `import shinobu_core` and headless unit tests succeed without
    installing Qt.
  - Privacy guarantees: never captures screenshots and never installs global
    keyboard hooks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from shinobu_core.core.kernel import default_home
from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.runtime.state_machine import VALID_MOODS, VALID_STATES
from shinobu_core.ui.avatar import AvatarAsset, AvatarResolver
from shinobu_core.ui.client import (
    DaemonClient,
    DaemonClientError,
    EventStreamWorker,
    discover_endpoint,
)

UI_PREFS_RELATIVE_PATH = Path("runtime") / "ui_prefs.json"

MIN_BUBBLE_DURATION_SEC: float = 6.0
MAX_BUBBLE_DURATION_SEC: float = 10.0
DEFAULT_BUBBLE_DURATION_SEC: float = 7.5
MAX_BUBBLE_QUEUE_SIZE: int = 16

DEFAULT_PET_WIDTH: int = 200
DEFAULT_PET_HEIGHT: int = 220

PYSIDE6_INSTALL_HINT = (
    "PySide6 is required to launch the desktop pet window, but it is not installed. "
    'Install the optional desktop extra with: pip install "shinobu-core[desktop]" '
    '(or: pip install "PySide6>=6.6").'
)


class PySide6NotInstalledError(ImportError):
    """Raised with an actionable install command when PySide6 is missing."""


def is_pyside6_available() -> bool:
    """Return True if `PySide6` can be located without importing it."""
    try:
        return importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


def require_pyside6() -> dict[str, Any]:
    """Lazily import required PySide6 modules or raise `PySide6NotInstalledError`."""
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]
    except Exception as exc:
        raise PySide6NotInstalledError(PYSIDE6_INSTALL_HINT) from exc

    qt_svg_widgets: Any = None
    try:
        from PySide6 import QtSvgWidgets  # type: ignore[import-not-found]

        qt_svg_widgets = QtSvgWidgets
    except Exception:
        qt_svg_widgets = None

    return {
        "QtCore": QtCore,
        "QtGui": QtGui,
        "QtWidgets": QtWidgets,
        "QtSvgWidgets": qt_svg_widgets,
    }


# ---------------------------------------------------------------------------
# Pure-Python coordinate persistence (`<home>/runtime/ui_prefs.json`)
# ---------------------------------------------------------------------------


def ui_prefs_path(home: Path | str | None = None) -> Path:
    """Return `<home>/runtime/ui_prefs.json`."""
    base = Path(home) if home is not None else default_home()
    return base / UI_PREFS_RELATIVE_PATH


def load_ui_prefs(home: Path | str | None = None) -> dict[str, Any]:
    """Safely load persisted UI preferences; return `{}` if missing or corrupt."""
    path = ui_prefs_path(home)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_ui_prefs(
    home: Path | str | None,
    updates: dict[str, Any],
    *,
    merge: bool = True,
) -> Path:
    """Atomically write UI preferences to `<home>/runtime/ui_prefs.json`."""
    path = ui_prefs_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_ui_prefs(home) if merge else {}
    data.update(updates)

    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    return path


def load_pet_position(home: Path | str | None = None) -> tuple[int, int] | None:
    """Load saved `(x, y)` desktop coordinates, or `None` if unavailable."""
    prefs = load_ui_prefs(home)
    raw_x = prefs.get("pet_x")
    raw_y = prefs.get("pet_y")
    if isinstance(raw_x, bool) or isinstance(raw_y, bool):
        return None
    if isinstance(raw_x, (int, float)) and isinstance(raw_y, (int, float)):
        return int(raw_x), int(raw_y)
    return None


def save_pet_position(home: Path | str | None, x: int, y: int) -> Path:
    """Persist `(x, y)` coordinates into `<home>/runtime/ui_prefs.json`."""
    return save_ui_prefs(home, {"pet_x": int(x), "pet_y": int(y)}, merge=True)


def clamp_position(
    x: int,
    y: int,
    width: int,
    height: int,
    screen_rect: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Clamp a window's top-left `(x, y)` inside `(left, top, screen_w, screen_h)`."""
    left, top, screen_w, screen_h = screen_rect
    max_x = left + max(0, screen_w - max(1, width))
    max_y = top + max(0, screen_h - max(1, height))
    clamped_x = min(max(int(x), left), max_x)
    clamped_y = min(max(int(y), top), max_y)
    return clamped_x, clamped_y


# ---------------------------------------------------------------------------
# Pure-Python zero-token bubble scheduler (6~10s auto-hide)
# ---------------------------------------------------------------------------


def clamp_bubble_duration(text: str, duration_sec: float | None = None) -> float:
    """Compute a bounded auto-hide duration strictly within `[6.0, 10.0]` seconds."""
    if duration_sec is None:
        estimated = MIN_BUBBLE_DURATION_SEC + len(text) * 0.06
        return max(MIN_BUBBLE_DURATION_SEC, min(MAX_BUBBLE_DURATION_SEC, estimated))
    return max(MIN_BUBBLE_DURATION_SEC, min(MAX_BUBBLE_DURATION_SEC, float(duration_sec)))


@dataclass(frozen=True)
class BubbleMessage:
    """Single zero-token bubble payload scheduled for temporary display."""

    text: str
    duration_sec: float
    source: str = "bubble"
    created_at: float = 0.0


class BubbleQueue:
    """Deterministic FIFO scheduler for transient 6~10s pet speech bubbles."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_pending: int = MAX_BUBBLE_QUEUE_SIZE,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._max_pending: int = max(1, int(max_pending))
        self._pending: deque[BubbleMessage] = deque()
        self._active: BubbleMessage | None = None
        self._expires_at: float = 0.0

    def enqueue(
        self,
        text: str,
        duration_sec: float | None = None,
        *,
        source: str = "bubble",
    ) -> BubbleMessage | None:
        """Schedule a bubble message. Blank messages are ignored."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        self.tick()
        now = self._clock()
        bounded_duration = clamp_bubble_duration(cleaned, duration_sec)
        msg = BubbleMessage(
            text=cleaned,
            duration_sec=bounded_duration,
            source=source,
            created_at=now,
        )
        if self._active is None:
            self._active = msg
            self._expires_at = now + msg.duration_sec
        else:
            if len(self._pending) >= self._max_pending:
                self._pending.popleft()
            self._pending.append(msg)
        return msg

    def tick(self) -> BubbleMessage | None:
        """Advance scheduler clock, hiding expired bubbles and promoting queued ones."""
        now = self._clock()
        if self._active is not None and now >= self._expires_at:
            self._active = None
            self._expires_at = 0.0
        if self._active is None and self._pending:
            next_msg = self._pending.popleft()
            self._active = next_msg
            self._expires_at = now + next_msg.duration_sec
        return self._active

    def current(self) -> BubbleMessage | None:
        """Return currently visible bubble (after advancing clock), or `None`."""
        return self.tick()

    def remaining_seconds(self) -> float:
        """Return remaining visibility duration in seconds (`0.0` if none)."""
        if self.tick() is None:
            return 0.0
        return max(0.0, self._expires_at - self._clock())

    def dismiss(self) -> BubbleMessage | None:
        """Immediately hide the active bubble and promote the next queued item."""
        self._active = None
        self._expires_at = 0.0
        return self.tick()

    def clear(self) -> None:
        """Remove active and all pending bubbles."""
        self._active = None
        self._expires_at = 0.0
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# Pure-Python dumb-shell event router (zero Qt dependency)
# ---------------------------------------------------------------------------


VALID_ROUTES: tuple[str, ...] = ("auto", "cloud", "local")
ROUTE_LABELS: dict[str, str] = {
    "auto": "Auto",
    "cloud": "Cloud",
    "local": "Local",
}
VALID_REASONING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high")
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _client_url(client: Any) -> str:
    for attr in ("base_url", "url"):
        val = getattr(client, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _is_remote_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip().lower().strip("[]")
    if not host:
        return False
    return host not in _LOOPBACK_HOSTS


def extract_turns_list(raw: Any) -> list[dict[str, Any]]:
    """Normalize `client.get_turns(...)` payloads (list or `{turns: [...]}`)."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        inner = raw.get("turns")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
    return []


def format_turns_transcript(turns: list[dict[str, Any]]) -> str:
    """Format daemon conversation turns for display, filtering tool noise."""
    blocks: list[str] = []
    for item in turns:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        raw_content = item.get("content")
        if raw_content is None:
            continue
        text = str(raw_content).strip()
        if not text:
            continue
        speaker = "You" if role == "user" else "Companion"
        blocks.append(f"{speaker}: {text}")
    return "\n\n".join(blocks)


def extract_last_assistant_text(turns: Any) -> str:
    """Return the latest non-empty assistant reply from a turns list or payload."""
    for item in reversed(extract_turns_list(turns)):
        role = str(item.get("role") or "").strip().lower()
        if role != "assistant":
            continue
        raw_content = item.get("content")
        if raw_content is None:
            raw_content = item.get("text")
        if raw_content is None:
            continue
        text = str(raw_content).strip()
        if text:
            return text
    return ""


def play_audio_bytes(
    audio_bytes: bytes,
    content_type: str = "audio/wav",
) -> tuple[bool, str]:
    """Play synthesized audio bytes with stdlib-only backend (Windows WAV via winsound)."""
    if not audio_bytes:
        return False, "未收到音频数据"

    import sys as _sys

    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    is_wav = "wav" in ct or audio_bytes[:4] == b"RIFF"
    if _sys.platform != "win32":
        return False, "当前平台暂不支持内置语音播放，请使用 Web 记忆本朗读"
    if not is_wav:
        return False, f"暂不支持内置播放该音频格式：{ct or 'unknown'}"

    try:
        import winsound
    except ImportError:
        return False, "当前环境缺少 winsound 音频模块"

    try:
        winsound.PlaySound(audio_bytes, winsound.SND_MEMORY)
    except (RuntimeError, OSError) as exc:
        return False, f"播放音频失败: {exc}"
    return True, ""


def _normalize_engine_info(engine_info: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(engine_info, dict):
        return None, None
    merged: dict[str, Any] = {}
    nested = engine_info.get("engine")
    if isinstance(nested, dict):
        merged.update(nested)
    merged.update(engine_info)

    raw_route = (
        merged.get("route")
        or merged.get("route_mode")
        or merged.get("active_route")
        or merged.get("mode")
    )
    route = str(raw_route).strip().lower() if isinstance(raw_route, str) and raw_route.strip() else None

    raw_reasoning = (
        merged.get("reasoning_effort")
        if merged.get("reasoning_effort") is not None
        else merged.get("reasoning")
    )
    if raw_reasoning is None:
        raw_reasoning = merged.get("thinking_level")
    reasoning = (
        str(raw_reasoning).strip().lower()
        if isinstance(raw_reasoning, str) and raw_reasoning.strip()
        else None
    )
    return route, reasoning


def format_connection_badge(
    url: str,
    connected: bool,
    engine_info: dict[str, Any] | None = None,
) -> str:
    """Format a compact status badge showing Cloud/Local, link state, and brain settings."""
    mode = "Cloud" if _is_remote_url(url) else "Local"
    status = "Online" if connected else "Offline"
    parts = [f"{mode} · {status}"]
    route, reasoning = _normalize_engine_info(engine_info)
    if route:
        parts.append(f"Route: {ROUTE_LABELS.get(route, route)}")
    if reasoning:
        parts.append(f"Think: {reasoning}")
    return " | ".join(parts)


def create_pet_daemon_client(
    home: Path | str | None = None,
    url: str | None = None,
    token: str | None = None,
    *,
    allow_remote: bool = False,
) -> tuple[Path, DaemonClient]:
    """Resolve daemon endpoint and build `DaemonClient` without probing local files when explicit credentials are supplied."""
    resolved_home = Path(home) if home is not None else default_home()
    resolved_url = (url or "").strip() or None
    resolved_token = (token or "").strip() or None

    if not (resolved_url and resolved_token):
        discovered = discover_endpoint(resolved_home)
        if discovered is None:
            raise DaemonClientError(
                f"No live daemon found via {resolved_home / 'runtime' / 'endpoint.json'}. "
                "Start the local daemon first or pass explicit remote --url and --token."
            )
        resolved_url = resolved_url or str(discovered["url"])
        resolved_token = resolved_token or str(discovered["token"])

    client = DaemonClient(
        url=resolved_url,
        token=resolved_token,
        allow_remote=allow_remote,
    )
    return resolved_home, client


class PetShellController:
    """Headless state/event adapter driving avatar rendering, history sync, and bubble display."""

    def __init__(
        self,
        *,
        home: Path | str | None = None,
        avatar_dir: Path | str | None = None,
        clock: Callable[[], float] | None = None,
        url: str = "",
    ) -> None:
        self.home: Path = Path(home) if home is not None else default_home()
        self.avatar_resolver = AvatarResolver(home=self.home, avatar_dir=avatar_dir)
        self.bubbles = BubbleQueue(clock=clock)
        self.state: str = "idle"
        self.mood: str = "calm"
        self.url: str = url
        self.connected: bool = False
        self.engine_info: dict[str, Any] = {}
        self.last_turns: list[dict[str, Any]] = []

    def current_avatar(self) -> AvatarAsset:
        return self.avatar_resolver.resolve(self.state, self.mood)

    def apply_snapshot(self, snapshot: dict[str, Any]) -> AvatarAsset:
        """Sync local display state from a daemon `GET /state` response."""
        raw_state = snapshot.get("state")
        raw_mood = snapshot.get("mood")
        if isinstance(raw_state, str) and raw_state.strip():
            self.state = raw_state.strip().lower()
        if isinstance(raw_mood, str) and raw_mood.strip():
            self.mood = raw_mood.strip().lower()
        return self.current_avatar()

    def apply_engine_info(self, info: dict[str, Any] | None) -> dict[str, Any]:
        """Store normalized engine routing & reasoning info for badge/menu display."""
        if isinstance(info, dict):
            updated = dict(self.engine_info)
            updated.update(info)
            route, reasoning = _normalize_engine_info(updated)
            if route is not None:
                updated["route"] = route
            if reasoning is not None:
                updated["reasoning_effort"] = reasoning
            self.engine_info = updated
        return dict(self.engine_info)

    @property
    def current_route(self) -> str | None:
        route, _ = _normalize_engine_info(self.engine_info)
        return route

    @property
    def current_reasoning(self) -> str | None:
        _, reasoning = _normalize_engine_info(self.engine_info)
        return reasoning

    def connection_badge(
        self,
        *,
        url: str | None = None,
        connected: bool | None = None,
    ) -> str:
        eff_url = self.url if url is None else url
        eff_connected = self.connected if connected is None else connected
        return format_connection_badge(eff_url, eff_connected, self.engine_info or None)

    def apply_turns(self, raw_turns: Any) -> str:
        """Cache turns returned by the daemon and return a clean display transcript."""
        self.last_turns = extract_turns_list(raw_turns)
        return format_turns_transcript(self.last_turns)

    def push_notice(
        self,
        text: str,
        *,
        duration_sec: float | None = None,
        source: str = "system",
    ) -> BubbleMessage | None:
        """Enqueue a transient pet bubble notice (e.g. control success or API error)."""
        return self.bubbles.enqueue(text, duration_sec=duration_sec, source=source)

    def execute_client_action(
        self,
        label: str,
        action: Callable[[], Any],
        *,
        success_notice: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Run a daemon control call, returning `(ok, result, notice_text)` without swallowing errors."""
        try:
            result = action()
        except Exception as exc:
            return False, None, f"{label}失败: {exc}"
        return True, result, success_notice

    def sync_history_from_client(self, client: Any, *, limit: int = 30) -> tuple[bool, str, str | None]:
        """Fetch active episode turns from daemon and format into clean transcript."""
        ok, raw, err_notice = self.execute_client_action(
            "刷新对话历史",
            lambda: client.get_turns(limit=limit),
        )
        if not ok:
            return False, "", err_notice
        return True, self.apply_turns(raw), None

    def sync_engine_from_client(self, client: Any) -> tuple[bool, dict[str, Any], str | None]:
        """Fetch current brain route / reasoning status from daemon if supported."""
        getter = getattr(client, "get_engine", None)
        if not callable(getter):
            return True, dict(self.engine_info), None
        ok, raw, err_notice = self.execute_client_action("同步引擎状态", getter)
        if not ok:
            return False, dict(self.engine_info), err_notice
        return True, self.apply_engine_info(raw if isinstance(raw, dict) else None), None

    def handle_event(self, event: Event) -> dict[str, Any]:
        """Route a daemon SSE `Event` into pure UI updates (no LLM calls)."""
        payload = event.payload if isinstance(event.payload, dict) else {}

        if event.kind == EventKind.BUBBLE:
            raw_text = payload.get("text")
            raw_dur = payload.get("duration_sec")
            dur = float(raw_dur) if isinstance(raw_dur, (int, float)) else None
            msg = self.bubbles.enqueue(str(raw_text or ""), duration_sec=dur, source="bubble")
            return {
                "avatar_changed": False,
                "asset": None,
                "bubble": msg,
                "refresh_history": False,
                "refresh_engine": False,
            }

        if event.kind in (EventKind.STATE_CHANGED, EventKind.MOOD_CHANGED):
            new_state = payload.get("state") or payload.get("to")
            new_mood = payload.get("mood")
            if isinstance(new_state, str) and new_state.strip():
                self.state = new_state.strip().lower()
            if isinstance(new_mood, str) and new_mood.strip():
                self.mood = new_mood.strip().lower()

            if any(
                k in payload
                for k in ("route", "route_mode", "active_route", "reasoning", "reasoning_effort", "thinking_level")
            ):
                self.apply_engine_info(payload)

            is_state_changed = event.kind == EventKind.STATE_CHANGED
            asset = self.current_avatar()
            return {
                "avatar_changed": True,
                "asset": asset,
                "bubble": None,
                "refresh_history": is_state_changed,
                "refresh_engine": is_state_changed,
            }

        return {
            "avatar_changed": False,
            "asset": None,
            "bubble": None,
            "refresh_history": False,
            "refresh_engine": False,
        }


# ---------------------------------------------------------------------------
# Lazy-loaded PySide6 GUI shell
# ---------------------------------------------------------------------------


def _build_qt_shell_types() -> tuple[type[Any], type[Any]]:
    """Construct concrete PySide6 QWidget subclasses only after PySide6 is loaded."""
    qt = require_pyside6()
    QtCore = qt["QtCore"]
    QtGui = qt["QtGui"]
    QtWidgets = qt["QtWidgets"]
    QtSvgWidgets = qt["QtSvgWidgets"]

    class _EventBridge(QtCore.QObject):
        event_received = QtCore.Signal(object)
        status_changed = QtCore.Signal(bool, str)
        chat_event_received = QtCore.Signal(object)
        chat_finished = QtCore.Signal(str)
        history_loaded = QtCore.Signal(object, str)
        history_refresh_requested = QtCore.Signal(bool)
        engine_loaded = QtCore.Signal(object)
        notice_requested = QtCore.Signal(str)

    class _ChatDrawerWindow(QtWidgets.QWidget):
        """Compact floating chat drawer toggled by double-clicking the pet."""

        def __init__(
            self,
            client: DaemonClient,
            bridge: _EventBridge,
            *,
            open_web_cb: Callable[[], None] | None = None,
            on_refresh_engine: Callable[[], None] | None = None,
            on_new_episode: Callable[[], None] | None = None,
            on_sleep: Callable[[], None] | None = None,
            parent: Any = None,
        ) -> None:
            super().__init__(parent)
            self._client = client
            self._bridge = bridge
            self._open_web_cb = open_web_cb
            self._on_refresh_engine = on_refresh_engine
            self._on_new_episode = on_new_episode
            self._on_sleep = on_sleep
            self._sending = False
            self._assistant_open = False
            self._history_loaded_once = False
            self._loading_history = False
            self._connected = False
            self._connection_detail = ""
            self._engine_info: dict[str, Any] | None = None
            self._last_assistant_text = ""
            self._streaming_assistant_chunks: list[str] = []
            self._tts_playing = False

            self.setWindowTitle("Companion Chat")
            self.setWindowFlags(
                QtCore.Qt.WindowType.Tool
                | QtCore.Qt.WindowType.WindowStaysOnTopHint
            )
            self.resize(360, 420)

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)

            self._status_label = QtWidgets.QLabel(
                format_connection_badge(_client_url(self._client), False, None)
            )
            self._status_label.setStyleSheet("color: #64748b; font-size: 11px;")
            layout.addWidget(self._status_label)

            # Lightweight cloud/session action toolbar.
            toolbar = QtWidgets.QHBoxLayout()
            toolbar.setSpacing(4)
            btn_style = (
                "QPushButton { font-size: 11px; padding: 3px 6px; "
                "border: 1px solid #cbd5e1; border-radius: 6px; background: #ffffff; }"
                "QPushButton:hover { background: #f1f5f9; }"
            )

            self._refresh_btn = QtWidgets.QPushButton("刷新")
            self._refresh_btn.setStyleSheet(btn_style)
            self._refresh_btn.clicked.connect(self._on_refresh_clicked)
            toolbar.addWidget(self._refresh_btn)

            self._new_episode_btn = QtWidgets.QPushButton("🆕 新对话")
            self._new_episode_btn.setStyleSheet(btn_style)
            self._new_episode_btn.clicked.connect(self._on_new_episode_clicked)
            toolbar.addWidget(self._new_episode_btn)

            self._sleep_btn = QtWidgets.QPushButton("🌙 归档记忆")
            self._sleep_btn.setStyleSheet(btn_style)
            self._sleep_btn.clicked.connect(self._on_sleep_clicked)
            toolbar.addWidget(self._sleep_btn)

            self._tts_btn = QtWidgets.QPushButton("🔊 朗读")
            self._tts_btn.setStyleSheet(btn_style)
            self._tts_btn.setToolTip("朗读最新一条伴侣回复")
            self._tts_btn.clicked.connect(self._on_tts_clicked)
            toolbar.addWidget(self._tts_btn)

            self._web_btn = QtWidgets.QPushButton("📖 记忆本")
            self._web_btn.setStyleSheet(btn_style)
            if self._open_web_cb is not None:
                self._web_btn.clicked.connect(self._open_web_cb)
            else:
                self._web_btn.setVisible(False)
            toolbar.addWidget(self._web_btn)
            toolbar.addStretch(1)
            layout.addLayout(toolbar)

            self._history = QtWidgets.QTextEdit()
            self._history.setReadOnly(True)
            self._history.setStyleSheet(
                "QTextEdit { background: #f8fafc; border: 1px solid #cbd5e1; "
                "border-radius: 8px; padding: 6px; font-size: 13px; }"
            )
            layout.addWidget(self._history, 1)

            input_row = QtWidgets.QHBoxLayout()
            self._input = QtWidgets.QLineEdit()
            self._input.setPlaceholderText("Type a message...")
            self._input.returnPressed.connect(self._on_send_clicked)
            self._send_btn = QtWidgets.QPushButton("Send")
            self._send_btn.clicked.connect(self._on_send_clicked)
            input_row.addWidget(self._input, 1)
            input_row.addWidget(self._send_btn)
            layout.addLayout(input_row)

            self._bridge.chat_event_received.connect(self._on_chat_event)
            self._bridge.chat_finished.connect(self._on_chat_finished)
            self._bridge.history_loaded.connect(self._on_history_loaded)
            self._bridge.history_refresh_requested.connect(
                lambda force: self.refresh_history(force=bool(force))
            )

        def is_sending(self) -> bool:
            return self._sending

        def has_loaded_history(self) -> bool:
            return self._history_loaded_once

        def mark_history_stale(self) -> None:
            self._history_loaded_once = False

        def _update_status_badge(self) -> None:
            badge = format_connection_badge(
                _client_url(self._client),
                self._connected,
                self._engine_info,
            )
            if not self._connected and self._connection_detail:
                badge = f"{badge} ({self._connection_detail})"
            self._status_label.setText(badge)

        def set_connection_status(self, connected: bool, detail: str) -> None:
            self._connected = connected
            self._connection_detail = detail
            self._update_status_badge()

        def set_engine_info(self, engine_info: dict[str, Any] | None) -> None:
            self._engine_info = dict(engine_info) if isinstance(engine_info, dict) else None
            self._update_status_badge()

        def _on_refresh_clicked(self) -> None:
            self.refresh_history(force=True)
            if self._on_refresh_engine is not None:
                self._on_refresh_engine()

        def _on_new_episode_clicked(self) -> None:
            if self._on_new_episode is not None:
                self._on_new_episode()
                return

            def _worker() -> None:
                try:
                    self._client.new_episode()
                except Exception as exc:
                    self._bridge.notice_requested.emit(f"开启新对话失败: {exc}")
                    return
                self._bridge.notice_requested.emit("🆕 已开启新对话")
                self._bridge.history_loaded.emit([], "")

            threading.Thread(target=_worker, name="shinobu-new-episode", daemon=True).start()

        def _on_sleep_clicked(self) -> None:
            if self._on_sleep is not None:
                self._on_sleep()
                return

            def _worker() -> None:
                try:
                    self._client.sleep()
                except Exception as exc:
                    self._bridge.notice_requested.emit(f"归档记忆失败: {exc}")
                    return
                self._bridge.notice_requested.emit("🌙 记忆已归档")

            threading.Thread(target=_worker, name="shinobu-sleep", daemon=True).start()

        def _on_tts_clicked(self) -> None:
            if self._tts_playing:
                self._bridge.notice_requested.emit("🔊 正在朗读中，请稍候")
                return
            text = self._last_assistant_text.strip()
            if not text:
                self._bridge.notice_requested.emit("暂无可朗读的伴侣回复")
                return
            self._tts_playing = True

            def _worker() -> None:
                try:
                    try:
                        audio_bytes, content_type = self._client.synthesize_tts(text, fmt="wav")
                    except Exception as exc:
                        if getattr(exc, "status", None) == 503:
                            self._bridge.notice_requested.emit("语音合成暂不可用（请先配置服务端 TTS）")
                        else:
                            self._bridge.notice_requested.emit(f"语音合成失败: {exc}")
                        return

                    ok, err_msg = play_audio_bytes(audio_bytes, content_type)
                    if not ok:
                        self._bridge.notice_requested.emit(err_msg or "无法播放语音")
                finally:
                    self._tts_playing = False

            threading.Thread(target=_worker, name="shinobu-tts-play", daemon=True).start()

        def refresh_history(self, *, force: bool = False) -> None:
            """Load active episode turns in background without overwriting active streaming."""
            if self._sending:
                return
            if self._loading_history and not force:
                return
            self._loading_history = True

            def _worker() -> None:
                try:
                    raw = self._client.get_turns(limit=30)
                    turns = extract_turns_list(raw)
                except Exception as exc:
                    self._bridge.history_loaded.emit(None, str(exc))
                    return
                self._bridge.history_loaded.emit(turns, "")

            threading.Thread(target=_worker, name="shinobu-chat-history", daemon=True).start()

        def _on_history_loaded(self, turns: Any, err_msg: str) -> None:
            self._loading_history = False
            if self._sending:
                return
            if err_msg:
                self._bridge.notice_requested.emit(f"刷新对话历史失败: {err_msg}")
                return
            self._history_loaded_once = True
            normalized_turns = extract_turns_list(turns)
            self._last_assistant_text = extract_last_assistant_text(normalized_turns)
            transcript = format_turns_transcript(normalized_turns)
            rendered = f"{transcript}\n\n" if transcript else ""
            self._history.setPlainText(rendered)
            cursor = self._history.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            self._history.setTextCursor(cursor)
            self._history.ensureCursorVisible()

        def showEvent(self, event: Any) -> None:  # noqa: N802
            super().showEvent(event)
            if not self._history_loaded_once:
                self.refresh_history()
            if self._on_refresh_engine is not None:
                self._on_refresh_engine()

        def _append_line(self, text: str) -> None:
            cursor = self._history.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self._history.setTextCursor(cursor)
            self._history.ensureCursorVisible()

        def _on_send_clicked(self) -> None:
            if self._sending:
                return
            msg = self._input.text().strip()
            if not msg:
                return
            self._input.clear()
            self._sending = True
            self._assistant_open = False
            self._streaming_assistant_chunks = []
            self._send_btn.setEnabled(False)
            self._append_line(f"You: {msg}\n")

            def _worker() -> None:
                err_msg = ""
                try:
                    for ev in self._client.chat_stream(msg):
                        self._bridge.chat_event_received.emit(ev)
                except Exception as exc:
                    err_msg = str(exc)
                finally:
                    self._bridge.chat_finished.emit(err_msg)

            threading.Thread(target=_worker, name="shinobu-chat-send", daemon=True).start()

        def _on_chat_event(self, ev: Event) -> None:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            if ev.kind == EventKind.TOKEN_DELTA:
                delta = str(payload.get("delta") or payload.get("text") or "")
                if delta:
                    if not self._assistant_open:
                        self._append_line("Companion: ")
                        self._assistant_open = True
                    self._streaming_assistant_chunks.append(delta)
                    self._last_assistant_text = "".join(self._streaming_assistant_chunks).strip()
                    self._append_line(delta)
            elif ev.kind == EventKind.DONE:
                if not self._assistant_open:
                    final_text = str(payload.get("text") or "")
                    if final_text:
                        self._append_line(f"Companion: {final_text}")
                        self._assistant_open = True
                        self._last_assistant_text = final_text.strip()
                else:
                    assembled = "".join(self._streaming_assistant_chunks).strip()
                    if assembled:
                        self._last_assistant_text = assembled
                self._streaming_assistant_chunks = []
                if self._assistant_open:
                    self._append_line("\n\n")
                    self._assistant_open = False
            elif ev.kind == EventKind.ERROR:
                err = str(payload.get("error") or "unknown error")
                self._append_line(f"\n[Error] {err}\n\n")
                self._assistant_open = False
                self._streaming_assistant_chunks = []

        def _on_chat_finished(self, err_msg: str) -> None:
            if err_msg:
                self._append_line(f"\n[Request failed] {err_msg}\n\n")
            elif self._assistant_open:
                assembled = "".join(self._streaming_assistant_chunks).strip()
                if assembled:
                    self._last_assistant_text = assembled
                self._append_line("\n\n")
            self._streaming_assistant_chunks = []
            self._assistant_open = False
            self._sending = False
            self._send_btn.setEnabled(True)

    class _QtPetShellWindow(QtWidgets.QWidget):
        """Frameless, always-on-top, translucent desktop pet shell window."""

        def __init__(
            self,
            client: DaemonClient,
            *,
            home: Path | str | None = None,
            open_web_cb: Callable[[], None] | None = None,
            avatar_dir: Path | str | None = None,
        ) -> None:
            super().__init__(None)
            self.client = client
            self.home = Path(home) if home is not None else default_home()
            self.open_web_cb = open_web_cb
            self.controller = PetShellController(
                home=self.home,
                avatar_dir=avatar_dir,
                url=_client_url(self.client),
            )

            self._drag_offset: Any = None
            self._dragging = False
            self._movie: Any = None

            self.setWindowTitle("Shinobu Desktop Shell")
            self.setWindowFlags(
                QtCore.Qt.WindowType.FramelessWindowHint
                | QtCore.Qt.WindowType.WindowStaysOnTopHint
                | QtCore.Qt.WindowType.Tool
            )
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setFixedSize(DEFAULT_PET_WIDTH + 40, DEFAULT_PET_HEIGHT + 90)

            root_layout = QtWidgets.QVBoxLayout(self)
            root_layout.setContentsMargins(8, 8, 8, 8)
            root_layout.setSpacing(6)

            # Transient auto-fade speech bubble (zero-token events).
            self._bubble_label = QtWidgets.QLabel("")
            self._bubble_label.setWordWrap(True)
            self._bubble_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._bubble_label.setStyleSheet(
                "QLabel { background: rgba(255, 255, 255, 238); color: #1e293b; "
                "border: 1px solid #cbd5e1; border-radius: 12px; padding: 6px 10px; "
                "font-size: 12px; }"
            )
            self._bubble_opacity = QtWidgets.QGraphicsOpacityEffect(self._bubble_label)
            self._bubble_label.setGraphicsEffect(self._bubble_opacity)
            self._bubble_label.hide()
            root_layout.addWidget(self._bubble_label, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

            # Avatar sprite surface (SVG inline/file, PNG/WebP pixmap, or GIF movie).
            self._sprite_stack = QtWidgets.QStackedWidget()
            self._sprite_stack.setFixedSize(DEFAULT_PET_WIDTH, DEFAULT_PET_HEIGHT)
            self._sprite_stack.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True
            )

            self._svg_widget: Any = None
            if QtSvgWidgets is not None:
                self._svg_widget = QtSvgWidgets.QSvgWidget()
                self._svg_widget.setStyleSheet("background: transparent;")
                self._sprite_stack.addWidget(self._svg_widget)

            self._pixmap_label = QtWidgets.QLabel()
            self._pixmap_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._pixmap_label.setStyleSheet("background: transparent;")
            self._sprite_stack.addWidget(self._pixmap_label)
            root_layout.addWidget(self._sprite_stack, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

            # Event bridge + SSE background subscriber.
            self._bridge = _EventBridge(self)
            self._bridge.event_received.connect(self._on_daemon_event)
            self._bridge.status_changed.connect(self._on_connection_status)
            self._bridge.engine_loaded.connect(self._on_engine_loaded)
            self._bridge.notice_requested.connect(self._show_notice_bubble)

            self._chat_drawer = _ChatDrawerWindow(
                self.client,
                self._bridge,
                open_web_cb=self.open_web_cb,
                on_refresh_engine=self._refresh_engine_info,
                on_new_episode=self._trigger_new_episode,
                on_sleep=self._trigger_sleep,
            )

            self._bubble_timer = QtCore.QTimer(self)
            self._bubble_timer.setInterval(150)
            self._bubble_timer.timeout.connect(self._refresh_bubble_view)
            self._bubble_timer.start()

            self._sse_worker = EventStreamWorker(
                self.client,
                on_event=self._bridge.event_received.emit,
                on_status=self._bridge.status_changed.emit,
            )

            self._tray_icon: Any = None
            self._setup_tray_icon()

            # Initial state sync & sprite render.
            try:
                snap = self.client.get_state(timeout=2.0)
                asset = self.controller.apply_snapshot(snap)
            except DaemonClientError:
                asset = self.controller.current_avatar()
            self.render_avatar(asset)
            self._restore_or_default_position()
            self._refresh_engine_info()
            self._sse_worker.start()

        def _primary_screen_rect(self) -> tuple[int, int, int, int]:
            screen = QtGui.QGuiApplication.primaryScreen()
            if screen is None:
                return (0, 0, 1280, 720)
            geom = screen.availableGeometry()
            return (geom.x(), geom.y(), geom.width(), geom.height())

        def _restore_or_default_position(self) -> None:
            rect = self._primary_screen_rect()
            saved = load_pet_position(self.home)
            if saved is not None:
                x, y = clamp_position(saved[0], saved[1], self.width(), self.height(), rect)
            else:
                left, top, sw, sh = rect
                x = left + max(0, sw - self.width() - 24)
                y = top + max(0, sh - self.height() - 24)
            self.move(x, y)

        def _reset_position(self) -> None:
            left, top, sw, sh = self._primary_screen_rect()
            x = left + max(0, sw - self.width() - 24)
            y = top + max(0, sh - self.height() - 24)
            self.move(x, y)
            save_pet_position(self.home, x, y)

        def render_avatar(self, asset: AvatarAsset) -> None:
            if self._movie is not None:
                self._movie.stop()
                self._movie = None

            if asset.kind == "svg_inline" and self._svg_widget is not None:
                self._svg_widget.load(asset.svg_text.encode("utf-8"))
                self._sprite_stack.setCurrentWidget(self._svg_widget)
                return

            if asset.kind == "file" and asset.file_path is not None:
                suffix = asset.file_path.suffix.lower()
                if suffix == ".svg" and self._svg_widget is not None:
                    self._svg_widget.load(str(asset.file_path))
                    self._sprite_stack.setCurrentWidget(self._svg_widget)
                    return
                if suffix == ".gif":
                    movie = QtGui.QMovie(str(asset.file_path))
                    movie.setScaledSize(QtCore.QSize(DEFAULT_PET_WIDTH, DEFAULT_PET_HEIGHT))
                    self._pixmap_label.setMovie(movie)
                    movie.start()
                    self._movie = movie
                    self._sprite_stack.setCurrentWidget(self._pixmap_label)
                    return
                pixmap = QtGui.QPixmap(str(asset.file_path))
                if not pixmap.isNull():
                    scaled = pixmap.scaled(
                        DEFAULT_PET_WIDTH,
                        DEFAULT_PET_HEIGHT,
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                    self._pixmap_label.setPixmap(scaled)
                    self._sprite_stack.setCurrentWidget(self._pixmap_label)
                    return

            self._pixmap_label.setText(f"[{asset.state}/{asset.mood}]")
            self._sprite_stack.setCurrentWidget(self._pixmap_label)

        def _refresh_bubble_view(self) -> None:
            active = self.controller.bubbles.current()
            if active is None:
                if self._bubble_label.isVisible():
                    self._bubble_label.hide()
                return
            if self._bubble_label.text() != active.text:
                self._bubble_label.setText(active.text)
            remaining = self.controller.bubbles.remaining_seconds()
            opacity = min(1.0, remaining / 0.8) if remaining < 0.8 else 1.0
            self._bubble_opacity.setOpacity(max(0.0, opacity))
            if not self._bubble_label.isVisible():
                self._bubble_label.show()

        def _show_notice_bubble(self, text: str) -> None:
            if self.controller.push_notice(text) is not None:
                self._refresh_bubble_view()

        def _refresh_engine_info(self) -> None:
            def _worker() -> None:
                ok, info, _ = self.controller.sync_engine_from_client(self.client)
                if ok and info:
                    self._bridge.engine_loaded.emit(info)

            threading.Thread(target=_worker, name="shinobu-engine-sync", daemon=True).start()

        def _on_engine_loaded(self, info: Any) -> None:
            if isinstance(info, dict):
                self.controller.apply_engine_info(info)
            self._chat_drawer.set_engine_info(self.controller.engine_info)
            if self._tray_icon is not None:
                self._tray_icon.setToolTip(
                    f"Shinobu Desktop Shell — {self.controller.connection_badge()}"
                )
                self._tray_icon.setContextMenu(self._build_context_menu())

        def _on_daemon_event(self, ev: Event) -> None:
            outcome = self.controller.handle_event(ev)
            if outcome["avatar_changed"] and outcome["asset"] is not None:
                self.render_avatar(outcome["asset"])
            if outcome["bubble"] is not None:
                self._refresh_bubble_view()
            if outcome.get("refresh_engine"):
                self._chat_drawer.set_engine_info(self.controller.engine_info)
                self._refresh_engine_info()
            if outcome.get("refresh_history"):
                if self._chat_drawer.isVisible() or self._chat_drawer.has_loaded_history():
                    self._chat_drawer.refresh_history()
                else:
                    self._chat_drawer.mark_history_stale()

        def _on_connection_status(self, connected: bool, detail: str) -> None:
            self.controller.connected = connected
            self._chat_drawer.set_connection_status(connected, detail)
            if self._tray_icon is not None:
                self._tray_icon.setToolTip(
                    f"Shinobu Desktop Shell — {self.controller.connection_badge()}"
                )
            if connected:
                self._refresh_engine_info()

        def toggle_chat_drawer(self) -> None:
            if self._chat_drawer.isVisible():
                self._chat_drawer.hide()
                return
            rect = self._primary_screen_rect()
            target_x = self.x() - self._chat_drawer.width() - 12
            if target_x < rect[0]:
                target_x = self.x() + self.width() + 12
            target_y = self.y() + self.height() - self._chat_drawer.height()
            clamped_x, clamped_y = clamp_position(
                target_x,
                target_y,
                self._chat_drawer.width(),
                self._chat_drawer.height(),
                rect,
            )
            self._chat_drawer.move(clamped_x, clamped_y)
            self._chat_drawer.show()
            self._chat_drawer.raise_()
            self._chat_drawer.activateWindow()

        def _trigger_state_update(self, state: str | None = None, mood: str | None = None) -> None:
            def _worker() -> None:
                ok, _, notice = self.controller.execute_client_action(
                    "状态更新",
                    lambda: self.client.set_state(state=state, mood=mood, reason="pet_menu"),
                )
                if not ok and notice:
                    self._bridge.notice_requested.emit(notice)

            threading.Thread(target=_worker, name="shinobu-state-set", daemon=True).start()

        def _trigger_set_route(self, route: str) -> None:
            def _worker() -> None:
                label = ROUTE_LABELS.get(route, route)
                ok, resp, notice = self.controller.execute_client_action(
                    "切换大脑路由",
                    lambda: self.client.set_route(route),
                    success_notice=f"🧠 大脑路由已切换: {label}",
                )
                if ok:
                    info = resp if isinstance(resp, dict) else {"route": route}
                    self._bridge.engine_loaded.emit(info)
                if notice:
                    self._bridge.notice_requested.emit(notice)

            threading.Thread(target=_worker, name="shinobu-route-set", daemon=True).start()

        def _trigger_set_reasoning(self, level: str) -> None:
            def _worker() -> None:
                ok, resp, notice = self.controller.execute_client_action(
                    "切换思考深度",
                    lambda: self.client.set_reasoning(level),
                    success_notice=f"💭 思考深度已切换: {level}",
                )
                if ok:
                    info = resp if isinstance(resp, dict) else {"reasoning_effort": level}
                    self._bridge.engine_loaded.emit(info)
                if notice:
                    self._bridge.notice_requested.emit(notice)

            threading.Thread(target=_worker, name="shinobu-reasoning-set", daemon=True).start()

        def _trigger_new_episode(self) -> None:
            def _worker() -> None:
                ok, _, notice = self.controller.execute_client_action(
                    "开启新对话",
                    lambda: self.client.new_episode(),
                    success_notice="🆕 已开启新对话",
                )
                if notice:
                    self._bridge.notice_requested.emit(notice)
                if ok:
                    self._bridge.history_refresh_requested.emit(True)

            threading.Thread(target=_worker, name="shinobu-new-episode", daemon=True).start()

        def _trigger_sleep(self) -> None:
            def _worker() -> None:
                ok, _, notice = self.controller.execute_client_action(
                    "归档记忆",
                    lambda: self.client.sleep(),
                    success_notice="🌙 记忆已归档",
                )
                if notice:
                    self._bridge.notice_requested.emit(notice)
                if ok:
                    self._bridge.history_refresh_requested.emit(True)

            threading.Thread(target=_worker, name="shinobu-sleep", daemon=True).start()

        def _build_context_menu(self) -> Any:
            menu = QtWidgets.QMenu(self)
            menu.addAction("Toggle Chat Drawer", self.toggle_chat_drawer)
            if self.open_web_cb is not None:
                menu.addAction("📖 Open Web Console", self.open_web_cb)
            menu.addSeparator()

            route_menu = menu.addMenu("大脑路由 (Brain Route)")
            active_route = self.controller.current_route
            for rt in VALID_ROUTES:
                act = route_menu.addAction(
                    ROUTE_LABELS.get(rt, rt),
                    lambda r=rt: self._trigger_set_route(r),
                )
                act.setCheckable(True)
                act.setChecked(active_route == rt)

            reasoning_menu = menu.addMenu("思考深度 (Reasoning)")
            active_reasoning = self.controller.current_reasoning
            for lvl in VALID_REASONING_LEVELS:
                act = reasoning_menu.addAction(
                    lvl,
                    lambda l=lvl: self._trigger_set_reasoning(l),
                )
                act.setCheckable(True)
                act.setChecked(active_reasoning == lvl)

            menu.addSeparator()

            state_menu = menu.addMenu("State")
            for st in VALID_STATES:
                state_menu.addAction(st, lambda s=st: self._trigger_state_update(state=s))

            mood_menu = menu.addMenu("Mood")
            for md in VALID_MOODS:
                mood_menu.addAction(md, lambda m=md: self._trigger_state_update(mood=m))

            menu.addSeparator()
            menu.addAction("🆕 New Episode (Fast Reset)", self._trigger_new_episode)
            menu.addAction("🌙 Sleep & Consolidate", self._trigger_sleep)
            menu.addSeparator()
            menu.addAction("Reset Position", self._reset_position)
            menu.addAction("Quit Pet Shell", self.shutdown_shell)
            return menu

        def _setup_tray_icon(self) -> None:
            if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                return
            icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
            tray = QtWidgets.QSystemTrayIcon(icon, self)
            tray.setToolTip("Shinobu Desktop Shell")
            tray.setContextMenu(self._build_context_menu())
            tray.activated.connect(self._on_tray_activated)
            tray.show()
            self._tray_icon = tray

        def _on_tray_activated(self, reason: Any) -> None:
            if reason == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
                self.show()
                self.raise_()
                self.toggle_chat_drawer()

        def mousePressEvent(self, event: Any) -> None:  # noqa: N802
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._dragging = True
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
            if self._dragging and self._drag_offset is not None:
                target = event.globalPosition().toPoint() - self._drag_offset
                self.move(target)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
            if event.button() == QtCore.Qt.MouseButton.LeftButton and self._dragging:
                self._dragging = False
                self._drag_offset = None
                save_pet_position(self.home, self.x(), self.y())
                event.accept()
                return
            super().mouseReleaseEvent(event)

        def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self.toggle_chat_drawer()
                event.accept()
                return
            super().mouseDoubleClickEvent(event)

        def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
            menu = self._build_context_menu()
            menu.exec(event.globalPos())

        def shutdown_shell(self) -> None:
            self._bubble_timer.stop()
            self._sse_worker.stop(timeout=1.5)
            self._chat_drawer.close()
            if self._tray_icon is not None:
                self._tray_icon.hide()
            self.close()
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.quit()

        def closeEvent(self, event: Any) -> None:  # noqa: N802
            self._bubble_timer.stop()
            self._sse_worker.stop(timeout=1.5)
            super().closeEvent(event)

    return _QtPetShellWindow, _ChatDrawerWindow


class PetShellWindow:
    """Lazy proxy constructing the real PySide6 `QWidget` pet window on demand."""

    def __new__(
        cls,
        client: DaemonClient,
        *,
        home: Path | str | None = None,
        open_web_cb: Callable[[], None] | None = None,
        avatar_dir: Path | str | None = None,
    ) -> Any:
        qt_window_cls, _ = _build_qt_shell_types()
        return qt_window_cls(
            client,
            home=home,
            open_web_cb=open_web_cb,
            avatar_dir=avatar_dir,
        )


def run_pet_shell(
    home: Path | str | None = None,
    url: str | None = None,
    token: str | None = None,
    open_web_cb: Callable[[], None] | None = None,
    *,
    avatar_dir: Path | str | None = None,
    allow_remote: bool = False,
) -> int:
    """Launch the PySide6 transparent desktop pet shell connected to a local or remote daemon."""
    qt = require_pyside6()
    QtWidgets = qt["QtWidgets"]

    resolved_home, client = create_pet_daemon_client(
        home=home,
        url=url,
        token=token,
        allow_remote=allow_remote,
    )

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["shinobu-pet"])
    app.setQuitOnLastWindowClosed(False)

    window = PetShellWindow(
        client,
        home=resolved_home,
        open_web_cb=open_web_cb,
        avatar_dir=avatar_dir,
    )
    window.show()
    return int(app.exec())


__all__ = [
    "BubbleMessage",
    "BubbleQueue",
    "DEFAULT_BUBBLE_DURATION_SEC",
    "DEFAULT_PET_HEIGHT",
    "DEFAULT_PET_WIDTH",
    "MAX_BUBBLE_DURATION_SEC",
    "MAX_BUBBLE_QUEUE_SIZE",
    "MIN_BUBBLE_DURATION_SEC",
    "PYSIDE6_INSTALL_HINT",
    "PetShellController",
    "PetShellWindow",
    "PySide6NotInstalledError",
    "ROUTE_LABELS",
    "UI_PREFS_RELATIVE_PATH",
    "VALID_REASONING_LEVELS",
    "VALID_ROUTES",
    "clamp_bubble_duration",
    "clamp_position",
    "create_pet_daemon_client",
    "extract_turns_list",
    "format_connection_badge",
    "format_turns_transcript",
    "is_pyside6_available",
    "load_pet_position",
    "load_ui_prefs",
    "require_pyside6",
    "run_pet_shell",
    "save_pet_position",
    "save_ui_prefs",
    "ui_prefs_path",
]
