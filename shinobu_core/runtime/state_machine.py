# -*- coding: utf-8 -*-
"""
Zero-token local desktop pet state machine for Shinobu.

Manages finite pet states (`idle`, `donut`, `sleepy`, `thinking`, `speaking`)
and moods (`calm`, `happy`, `smug`, `sleepy`, `annoyed`) with thread-safe
transitions, idle-time tracking, and canonical `Event` emission.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import Any, Callable, Tuple

from shinobu_core.protocol.events import Event, EventKind

VALID_STATES: Tuple[str, ...] = (
    "idle",
    "donut",
    "sleepy",
    "thinking",
    "speaking",
)

VALID_MOODS: Tuple[str, ...] = (
    "calm",
    "happy",
    "smug",
    "sleepy",
    "annoyed",
)

_VALID_STATE_SET: frozenset[str] = frozenset(VALID_STATES)
_VALID_MOOD_SET: frozenset[str] = frozenset(VALID_MOODS)
_ACTIVE_STATES: frozenset[str] = frozenset(("thinking", "speaking"))


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class PetStateMachine:
    """Thread-safe zero-token finite state machine for the desktop companion."""

    def __init__(
        self,
        state: str = "idle",
        mood: str = "calm",
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        state_norm = self._validate_state(state)
        mood_norm = self._validate_mood(mood)

        self._lock = threading.RLock()
        self._clock: Callable[[], float] = clock or time.monotonic
        self._state: str = state_norm
        self._mood: str = mood_norm
        self._reason: str = "init"
        self._updated_at: str = _utc_now_iso()
        self._last_activity_ts: float = float(self._clock())

    @staticmethod
    def _validate_state(state: str) -> str:
        norm = str(state or "").strip().lower()
        if norm not in _VALID_STATE_SET:
            raise ValueError(
                f"Invalid pet state {state!r}; expected one of {VALID_STATES}"
            )
        return norm

    @staticmethod
    def _validate_mood(mood: str) -> str:
        norm = str(mood or "").strip().lower()
        if norm not in _VALID_MOOD_SET:
            raise ValueError(
                f"Invalid pet mood {mood!r}; expected one of {VALID_MOODS}"
            )
        return norm

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def mood(self) -> str:
        with self._lock:
            return self._mood

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def updated_at(self) -> str:
        with self._lock:
            return self._updated_at

    @property
    def last_activity_ts(self) -> float:
        with self._lock:
            return self._last_activity_ts

    def touch(self) -> None:
        """Record interactive user/assistant activity and reset idle timer."""
        with self._lock:
            self._last_activity_ts = float(self._clock())

    def idle_seconds(self) -> float:
        """Return elapsed seconds since last recorded interactive activity."""
        with self._lock:
            elapsed = float(self._clock()) - self._last_activity_ts
            return max(0.0, elapsed)

    def transition(
        self,
        state: str,
        mood: str | None = None,
        reason: str = "",
        *,
        episode_id: str = "",
        touch_activity: bool | None = None,
    ) -> Event:
        """
        Transition to `state` (and optionally `mood`) and emit a `state_changed` Event.

        By default, transitions into active states (`thinking`, `speaking`) or
        explicit non-idle transitions refresh the idle timer; internal heartbeat
        idle transitions pass `touch_activity=False`.
        """
        next_state = self._validate_state(state)
        next_mood = self._validate_mood(mood) if mood is not None else None
        reason_clean = str(reason or "")

        with self._lock:
            prev_state = self._state
            prev_mood = self._mood
            effective_mood = next_mood if next_mood is not None else prev_mood
            changed = (next_state != prev_state) or (effective_mood != prev_mood)

            self._state = next_state
            self._mood = effective_mood
            self._reason = reason_clean
            self._updated_at = _utc_now_iso()

            should_touch = (
                touch_activity
                if touch_activity is not None
                else (
                    next_state in _ACTIVE_STATES
                    or (next_state == "idle" and prev_state in _ACTIVE_STATES)
                )
            )
            if should_touch:
                self._last_activity_ts = float(self._clock())

            payload = {
                "from": prev_state,
                "to": next_state,
                "prev_state": prev_state,
                "state": next_state,
                "prev_mood": prev_mood,
                "mood": effective_mood,
                "reason": reason_clean,
                "changed": changed,
                "updated_at": self._updated_at,
            }

        return Event(
            kind=EventKind.STATE_CHANGED,
            payload=payload,
            episode_id=episode_id,
        )

    def set_mood(
        self,
        mood: str,
        reason: str = "",
        *,
        episode_id: str = "",
    ) -> Event:
        """Update mood without altering the current state and emit `mood_changed`."""
        next_mood = self._validate_mood(mood)
        reason_clean = str(reason or "")

        with self._lock:
            prev_mood = self._mood
            self._mood = next_mood
            if reason_clean:
                self._reason = reason_clean
            self._updated_at = _utc_now_iso()
            payload = {
                "prev_mood": prev_mood,
                "mood": next_mood,
                "state": self._state,
                "reason": reason_clean,
                "changed": next_mood != prev_mood,
                "updated_at": self._updated_at,
            }

        return Event(
            kind=EventKind.MOOD_CHANGED,
            payload=payload,
            episode_id=episode_id,
        )

    def apply_event(self, event: Event) -> None:
        """Synchronize local state/mood from an orchestrator or runtime Event."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind == EventKind.STATE_CHANGED:
            candidate_state = (
                payload.get("state")
                or payload.get("to")
                or payload.get("to_state")
                or payload.get("new_state")
            )
            candidate_mood = payload.get("mood")
            reason = str(payload.get("reason") or "")
            if isinstance(candidate_state, str) and candidate_state.strip().lower() in _VALID_STATE_SET:
                mood_arg = (
                    candidate_mood
                    if isinstance(candidate_mood, str)
                    and candidate_mood.strip().lower() in _VALID_MOOD_SET
                    else None
                )
                is_idle_auto = reason in ("idle_donut", "idle_sleepy")
                self.transition(
                    candidate_state,
                    mood=mood_arg,
                    reason=reason,
                    episode_id=event.episode_id,
                    touch_activity=False if is_idle_auto else None,
                )
        elif event.kind == EventKind.MOOD_CHANGED:
            candidate_mood = (
                payload.get("mood")
                or payload.get("to")
                or payload.get("to_mood")
            )
            if isinstance(candidate_mood, str) and candidate_mood.strip().lower() in _VALID_MOOD_SET:
                self.set_mood(
                    candidate_mood,
                    reason=str(payload.get("reason") or ""),
                    episode_id=event.episode_id,
                )

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the current pet state."""
        with self._lock:
            return {
                "state": self._state,
                "mood": self._mood,
                "reason": self._reason,
                "updated_at": self._updated_at,
                "idle_seconds": round(self.idle_seconds(), 3),
            }


__all__ = [
    "PetStateMachine",
    "VALID_MOODS",
    "VALID_STATES",
]
