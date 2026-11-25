# -*- coding: utf-8 -*-
"""Protocol contracts shared across Shinobu engines, hooks, and extensions."""

from __future__ import annotations

from shinobu_core.protocol.engine import (
    EngineAdapter,
    ToolSpec,
    TurnChunk,
    TurnRequest,
)
from shinobu_core.protocol.events import (
    VALID_EVENT_KINDS,
    Event,
    EventKind,
    EventKindLiteral,
)

__all__ = [
    "EngineAdapter",
    "Event",
    "EventKind",
    "EventKindLiteral",
    "ToolSpec",
    "TurnChunk",
    "TurnRequest",
    "VALID_EVENT_KINDS",
]
