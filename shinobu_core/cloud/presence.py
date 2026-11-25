# -*- coding: utf-8 -*-
"""
Zero-privacy local activity sensing and cloud-side presence evaluation.

Only numeric idle/activity telemetry (idle seconds, continuous active streak,
local hour, sample timestamp) is collected and transmitted. No screenshots,
window titles, process names, or keystroke contents are ever read or emitted.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timedelta, timezone
import math
import sys
import threading
import time
from typing import Any, Callable, Mapping

from shinobu_core.protocol.events import Event, EventKind


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint32),
    ]


def get_system_idle_seconds() -> float | None:
    """
    Return OS keyboard/mouse idle duration in seconds on Windows.

    Uses ``user32.GetLastInputInfo`` together with ``kernel32.GetTickCount64``
    with 32-bit wrap-around handling. Returns ``None`` on non-Windows platforms
    or when the Win32 call fails.
    """
    if sys.platform != "win32":
        return None
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        ok = windll.user32.GetLastInputInfo(ctypes.byref(lii))
        if not ok:
            return None
        get_tick64 = windll.kernel32.GetTickCount64
        try:
            get_tick64.restype = ctypes.c_uint64
        except Exception:
            pass
        tick64 = int(get_tick64())
        elapsed_ms = (tick64 - int(lii.dwTime)) & 0xFFFFFFFF
        # Guard against sub-second race where dwTime is slightly ahead of tick64.
        if elapsed_ms >= 0xFFFFF000:
            return 0.0
        return round(elapsed_ms / 1000.0, 3)
    except Exception:
        return None


def _coerce_non_negative_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return max(0.0, num)


def _is_hour_in_window(hour: int, window: tuple[int, int]) -> bool:
    start = int(window[0]) % 24
    end = int(window[1]) % 24
    h = int(hour) % 24
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


class LocalPresenceTracker:
    """
    Tracks sanitized local user presence without inspecting windows or screens.

    Emits strictly numeric/enum telemetry:
        {
            "state": "active" | "away" | "unknown",
            "idle_sec": float | None,
            "active_streak_sec": float,
            "local_hour": int,
            "sampled_at": float,
        }
    """

    def __init__(
        self,
        *,
        away_threshold_sec: float = 300.0,
        idle_fn: Callable[[], float | None] | None = None,
        clock: Callable[[], float] | None = None,
        tz_offset_hours: float = 8.0,
    ) -> None:
        threshold = float(away_threshold_sec)
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("away_threshold_sec must be a positive finite number")
        tz_hours = float(tz_offset_hours)
        if not math.isfinite(tz_hours) or tz_hours < -24.0 or tz_hours > 24.0:
            raise ValueError("tz_offset_hours must be within [-24, 24]")

        self._away_threshold_sec: float = threshold
        self._idle_fn: Callable[[], float | None] = (
            idle_fn if idle_fn is not None else get_system_idle_seconds
        )
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._tz: timezone = timezone(timedelta(hours=tz_hours))
        self._lock = threading.RLock()
        self._session_start: float | None = None

    def reset(self) -> None:
        """Reset accumulated continuous active streak state."""
        with self._lock:
            self._session_start = None

    def sample(self) -> dict[str, Any]:
        """Collect one sanitized presence snapshot."""
        now = float(self._clock())
        local_hour = datetime.fromtimestamp(now, tz=self._tz).hour

        raw_idle: Any = None
        try:
            raw_idle = self._idle_fn()
        except Exception:
            raw_idle = None
        idle_sec = _coerce_non_negative_float(raw_idle)

        with self._lock:
            if idle_sec is None:
                self._session_start = None
                state = "unknown"
                active_streak_sec = 0.0
            elif idle_sec >= self._away_threshold_sec:
                self._session_start = None
                state = "away"
                active_streak_sec = 0.0
            else:
                state = "active"
                if self._session_start is None or now < self._session_start:
                    self._session_start = now
                active_streak_sec = round(max(0.0, now - self._session_start), 3)

        return {
            "state": state,
            "idle_sec": round(idle_sec, 3) if idle_sec is not None else None,
            "active_streak_sec": active_streak_sec,
            "local_hour": int(local_hour),
            "sampled_at": round(now, 3),
        }


class PresenceEvaluator:
    """
    Zero-token HeartbeatLoop sensor that evaluates edge presence telemetry.

    Reads ``meta["presence"]`` from online workers in ``WorkerRegistry`` and
    emits ``EventKind.BUBBLE`` events for long continuous active sessions or
    late-night activity, subject to per-(worker, label) cooldown throttling.
    """

    def __init__(
        self,
        registry: Any,
        *,
        long_session_sec: float = 9000.0,
        late_night_hours: tuple[int, int] = (1, 5),
        cooldown_sec: float = 3600.0,
        clock: Callable[[], float] | None = None,
        respect_quiet_long_session: bool = True,
        respect_quiet_late_night: bool = False,
    ) -> None:
        long_sec = float(long_session_sec)
        if not math.isfinite(long_sec) or long_sec <= 0:
            raise ValueError("long_session_sec must be a positive finite number")
        cd_sec = float(cooldown_sec)
        if not math.isfinite(cd_sec) or cd_sec < 0:
            raise ValueError("cooldown_sec must be a non-negative finite number")
        if len(late_night_hours) != 2:
            raise ValueError("late_night_hours must be a 2-tuple of (start_hour, end_hour)")

        self._registry = registry
        self._long_session_sec: float = long_sec
        self._late_night_hours: tuple[int, int] = (
            int(late_night_hours[0]),
            int(late_night_hours[1]),
        )
        self._cooldown_sec: float = cd_sec
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._respect_quiet_long_session: bool = bool(respect_quiet_long_session)
        self._respect_quiet_late_night: bool = bool(respect_quiet_late_night)
        self._lock = threading.RLock()
        self._last_triggered: dict[tuple[str, str], float] = {}

    def _can_trigger(self, worker_id: str, label: str, now: float) -> bool:
        key = (worker_id, label)
        with self._lock:
            last = self._last_triggered.get(key)
            if last is not None and (now - last) < self._cooldown_sec:
                return False
            self._last_triggered[key] = now
            return True

    def __call__(self) -> list[Event]:
        """Inspect online edge workers and return due presence reminder events."""
        if self._registry is None or not hasattr(self._registry, "list_workers"):
            return []

        now = float(self._clock())
        workers = self._registry.list_workers(include_expired=False)
        events: list[Event] = []

        for worker in workers:
            if not isinstance(worker, Mapping):
                continue
            worker_id = str(worker.get("worker_id") or "").strip()
            if not worker_id:
                continue
            meta = worker.get("meta")
            if not isinstance(meta, Mapping):
                continue
            presence = meta.get("presence")
            if not isinstance(presence, Mapping):
                continue
            if presence.get("state") != "active":
                continue

            raw_hour = presence.get("local_hour")
            local_hour: int | None = None
            if not isinstance(raw_hour, bool) and isinstance(raw_hour, (int, float)):
                if math.isfinite(float(raw_hour)):
                    local_hour = int(raw_hour) % 24

            active_streak_sec = _coerce_non_negative_float(
                presence.get("active_streak_sec")
            )

            # Late-night anti-stay-up alert takes precedence during late-night hours.
            if local_hour is not None and _is_hour_in_window(
                local_hour, self._late_night_hours
            ):
                if self._can_trigger(worker_id, "late_night", now):
                    events.append(
                        Event(
                            kind=EventKind.BUBBLE,
                            payload={
                                "source": "presence",
                                "label": "late_night",
                                "text": (
                                    f"已经凌晨 {local_hour:02d} 点多了，还在电脑前忙呀？"
                                    "早点收尾休息吧，熬夜太伤身体了。"
                                ),
                                "worker_id": worker_id,
                                "local_hour": local_hour,
                                "active_streak_sec": round(active_streak_sec or 0.0, 1),
                                "respect_quiet": self._respect_quiet_late_night,
                            },
                        )
                    )
                continue

            if (
                active_streak_sec is not None
                and active_streak_sec >= self._long_session_sec
            ):
                if self._can_trigger(worker_id, "long_session", now):
                    hours = active_streak_sec / 3600.0
                    events.append(
                        Event(
                            kind=EventKind.BUBBLE,
                            payload={
                                "source": "presence",
                                "label": "long_session",
                                "text": (
                                    f"你已经连续在电脑前活跃约 {hours:.1f} 小时啦，"
                                    "起身伸个懒腰、喝口水休息几分钟吧。"
                                ),
                                "worker_id": worker_id,
                                "local_hour": local_hour,
                                "active_streak_sec": round(active_streak_sec, 1),
                                "respect_quiet": self._respect_quiet_long_session,
                            },
                        )
                    )

        return events


__all__ = [
    "LocalPresenceTracker",
    "PresenceEvaluator",
    "get_system_idle_seconds",
]
