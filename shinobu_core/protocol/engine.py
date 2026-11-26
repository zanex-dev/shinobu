# -*- coding: utf-8 -*-
"""Model engine & tool invocation protocol contracts for Shinobu."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol, Sequence, runtime_checkable


@dataclass(slots=True)
class ToolSpec:
    """Declarative specification and executable handler for a runtime tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolSpec.name must be a non-empty string")
        if not callable(self.handler):
            raise TypeError("ToolSpec.handler must be callable")

    def to_schema(self) -> dict[str, Any]:
        """Return a JSON-serializable declaration omitting the Python callable."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(slots=True)
class TurnRequest:
    """Normalized single-turn inference request sent to an `EngineAdapter`."""

    episode_id: str
    system_prefix: str
    history: list[dict[str, Any]] = field(default_factory=list)
    user_text: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    reasoning_effort: str = ""


@dataclass(slots=True)
class TurnChunk:
    """Incremental chunk yielded by `EngineAdapter.stream_turn`."""

    delta: str = ""
    tool_calls: Sequence[dict[str, Any]] = ()
    usage: dict[str, Any] | None = None
    done: bool = False


@runtime_checkable
class EngineAdapter(Protocol):
    """Neutral protocol implemented by LLM / runtime engine backends."""

    name: str

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]: ...

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]: ...


__all__ = [
    "EngineAdapter",
    "ToolSpec",
    "TurnChunk",
    "TurnRequest",
]
