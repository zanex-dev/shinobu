# -*- coding: utf-8 -*-
"""Canonical event contracts for the Shinobu runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Literal, Mapping, Tuple

EventKindLiteral = Literal[
    "state_changed",
    "token_delta",
    "mood_changed",
    "bubble",
    "tool_call",
    "error",
    "done",
]


class EventKind:
    """Canonical runtime event kinds emitted across Shinobu modules."""

    STATE_CHANGED: str = "state_changed"
    TOKEN_DELTA: str = "token_delta"
    MOOD_CHANGED: str = "mood_changed"
    BUBBLE: str = "bubble"
    TOOL_CALL: str = "tool_call"
    ERROR: str = "error"
    DONE: str = "done"

    ALL: Tuple[str, ...] = (
        STATE_CHANGED,
        TOKEN_DELTA,
        MOOD_CHANGED,
        BUBBLE,
        TOOL_CALL,
        ERROR,
        DONE,
    )


VALID_EVENT_KINDS: frozenset[str] = frozenset(EventKind.ALL)


@dataclass(frozen=True, slots=True)
class Event:
    """Structured runtime event envelope shared by engines, hooks, and UI surfaces."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    episode_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Event.kind must be a non-empty string")
        if not isinstance(self.payload, dict):
            object.__setattr__(self, "payload", dict(self.payload))

    @property
    def is_standard_kind(self) -> bool:
        """Return True if `kind` belongs to the canonical `EventKind.ALL` set."""
        return self.kind in VALID_EVENT_KINDS

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event into a JSON-compatible dictionary."""
        return {
            "kind": self.kind,
            "payload": dict(self.payload),
            "ts": self.ts,
            "episode_id": self.episode_id,
        }

    def to_json(self) -> str:
        """Serialize the event into a compact UTF-8 JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        """Deserialize an `Event` instance from a mapping."""
        payload = data.get("payload")
        ts = data.get("ts")
        return cls(
            kind=str(data["kind"]),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            ts=float(ts) if ts is not None else time.time(),
            episode_id=str(data.get("episode_id", "")),
        )


__all__ = [
    "Event",
    "EventKind",
    "EventKindLiteral",
    "VALID_EVENT_KINDS",
]
