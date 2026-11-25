# -*- coding: utf-8 -*-
"""Offline mock engine adapter for deterministic local testing and demos."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator, Sequence

from shinobu_core.protocol.engine import TurnChunk, TurnRequest

ReplySpec = str | dict[str, Any] | Callable[[TurnRequest], Any]
ToolTriggerSpec = (
    dict[str, dict[str, Any] | Callable[[TurnRequest], dict[str, Any] | Sequence[dict[str, Any]] | None]]
    | Callable[[TurnRequest], dict[str, Any] | Sequence[dict[str, Any]] | None]
)


def _normalize_tool_call(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Normalize a tool call dictionary into {id, name, arguments}."""
    name = str(raw.get("name") or "")
    call_id = str(raw.get("id") or f"call_mock_{index + 1}")
    args = raw.get("arguments", {})
    if isinstance(args, str):
        text = args.strip()
        if not text:
            args = {}
        else:
            try:
                parsed = json.loads(text)
                args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
            except json.JSONDecodeError:
                args = {"_raw": text}
    elif not isinstance(args, dict):
        args = {"_raw": args}
    return {"id": call_id, "name": name, "arguments": args}


def _extract_json_dict(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a JSON object from a dict or string (handling markdown fences)."""
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Failed to parse JSON object from output: {text[:200]!r}") from None
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object (dict), got {type(parsed).__name__}")
    return parsed


class MockEngine:
    """Deterministic offline EngineAdapter supporting streaming, tool calls, and JSON completion."""

    def __init__(
        self,
        scripted_replies: Sequence[ReplySpec] | None = None,
        tool_trigger: ToolTriggerSpec | None = None,
        *,
        chunk_size: int = 8,
        json_replies: Sequence[dict[str, Any] | str] | Callable[[str, str], dict[str, Any] | str] | None = None,
        name: str = "mock",
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self.name: str = name
        self._scripted_replies: list[ReplySpec] = list(scripted_replies) if scripted_replies is not None else []
        self._tool_trigger: ToolTriggerSpec | None = tool_trigger
        self._chunk_size: int = chunk_size
        self._json_replies: list[dict[str, Any] | str] | Callable[[str, str], dict[str, Any] | str] | None = (
            list(json_replies) if isinstance(json_replies, Sequence) and not isinstance(json_replies, (str, bytes)) else json_replies
        )
        self._turn_cursor: int = 0
        self._json_cursor: int = 0
        self.requests: list[TurnRequest] = []
        self.json_requests: list[tuple[str, str]] = []

    def _match_tool_trigger(self, req: TurnRequest) -> list[dict[str, Any]]:
        if self._tool_trigger is None:
            return []

        if callable(self._tool_trigger):
            triggered = self._tool_trigger(req)
        else:
            # Avoid infinite trigger loops immediately after a tool result is fed back.
            if req.history and req.history[-1].get("role") == "tool":
                return []
            triggered = None
            haystack = req.user_text or ""
            for keyword, spec in self._tool_trigger.items():
                if keyword and keyword in haystack:
                    triggered = spec(req) if callable(spec) else spec
                    break

        if not triggered:
            return []
        if isinstance(triggered, dict):
            items: Sequence[dict[str, Any]] = [triggered]
        else:
            items = list(triggered)
        return [_normalize_tool_call(item, idx) for idx, item in enumerate(items)]

    def _next_scripted_reply(self, req: TurnRequest) -> str | dict[str, Any]:
        if not self._scripted_replies:
            base = req.user_text.strip()
            return f"[mock] {base}" if base else "[mock] ok"
        idx = min(self._turn_cursor, len(self._scripted_replies) - 1)
        self._turn_cursor += 1
        spec = self._scripted_replies[idx]
        result = spec(req) if callable(spec) else spec
        if isinstance(result, dict):
            return result
        return str(result)

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        self.requests.append(req)

        triggered_calls = self._match_tool_trigger(req)
        if triggered_calls:
            usage = {
                "prompt_tokens": max(1, len(req.system_prefix) // 4 + len(req.user_text) // 4),
                "completion_tokens": 8 * len(triggered_calls),
            }
            yield TurnChunk(delta="", tool_calls=triggered_calls, usage=usage, done=True)
            return

        reply = self._next_scripted_reply(req)
        text = ""
        tool_calls: list[dict[str, Any]] = []
        custom_usage: dict[str, Any] | None = None

        if isinstance(reply, dict):
            text = str(reply.get("text", ""))
            raw_calls = reply.get("tool_calls") or ()
            tool_calls = [_normalize_tool_call(item, idx) for idx, item in enumerate(raw_calls)]
            if isinstance(reply.get("usage"), dict):
                custom_usage = dict(reply["usage"])
        else:
            text = reply

        for offset in range(0, len(text), self._chunk_size):
            yield TurnChunk(delta=text[offset : offset + self._chunk_size])

        usage = custom_usage or {
            "prompt_tokens": max(1, (len(req.system_prefix) + len(req.user_text)) // 4),
            "completion_tokens": max(1, len(text) // 4) if text else 0,
        }
        yield TurnChunk(delta="", tool_calls=tool_calls, usage=usage, done=True)

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        self.json_requests.append((system, prompt))
        if callable(self._json_replies):
            return _extract_json_dict(self._json_replies(system, prompt))
        if isinstance(self._json_replies, list) and self._json_replies:
            idx = min(self._json_cursor, len(self._json_replies) - 1)
            self._json_cursor += 1
            return _extract_json_dict(self._json_replies[idx])
        return {
            "ok": True,
            "engine": self.name,
            "summary": prompt.strip()[:120],
            "facts": [],
        }


__all__ = ["MockEngine"]
