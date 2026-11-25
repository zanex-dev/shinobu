# -*- coding: utf-8 -*-
"""Append-only trailing LLM tools backed by MemoryStore (never mutate L0 prefix)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Callable

from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.engine import ToolSpec


def _validate_iso_timestamp(raw: str) -> str:
    cleaned = str(raw or "").strip()
    if not cleaned:
        raise ValueError("trigger_at must be a non-empty ISO-8601 timestamp")
    normalized = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"trigger_at must be a valid ISO-8601 timestamp: {cleaned!r}") from exc
    return cleaned


def build_memory_tools(
    store: MemoryStore,
    episode_id: str,
    on_mood: Callable[[str, str], None] | None = None,
) -> list[ToolSpec]:
    """Build trailing memory/mood tools that leave the L0 system prefix untouched."""

    def _recall_memory(query: str, limit: int = 6) -> str:
        clamped = max(1, min(20, int(limit)))
        hits = store.search(query=query, limit=clamped)
        return json.dumps(
            {
                "query": query,
                "count": len(hits),
                "results": hits,
            },
            ensure_ascii=False,
        )

    def _note_memory(key: str, value: str, confidence: float = 0.8) -> str:
        fact = store.upsert_fact(
            key=key,
            value=value,
            confidence=confidence,
            source_episode=episode_id,
        )
        return json.dumps(
            {
                "status": "saved",
                "fact": fact,
            },
            ensure_ascii=False,
        )

    def _set_mood(mood: str, reason: str = "") -> str:
        mood_clean = mood.strip()
        if not mood_clean:
            raise ValueError("mood must be a non-empty string")
        if on_mood is not None:
            on_mood(mood_clean, reason)
        return json.dumps(
            {
                "status": "ok",
                "episode_id": episode_id,
                "mood": mood_clean,
                "reason": reason,
            },
            ensure_ascii=False,
        )

    def _schedule_reminder(
        label: str,
        trigger_at: str = "",
        delay_minutes: int | None = None,
        message: str = "",
    ) -> str:
        label_clean = str(label or "").strip()
        if not label_clean:
            raise ValueError("label must be a non-empty string")

        if delay_minutes is not None:
            if isinstance(delay_minutes, bool):
                raise ValueError("delay_minutes must be a positive integer")
            delay_val = int(delay_minutes)
            if delay_val <= 0:
                raise ValueError("delay_minutes must be > 0")
            resolved_trigger = (
                (datetime.now(timezone.utc) + timedelta(minutes=delay_val))
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        else:
            resolved_trigger = _validate_iso_timestamp(trigger_at)

        msg_clean = str(message or "").strip()
        payload = {
            "message": msg_clean or label_clean,
            "source": "timemap",
            "created_by": "tool",
        }
        schedule_id = store.add_schedule(
            trigger_at=resolved_trigger,
            label=label_clean,
            payload=payload,
        )
        return json.dumps(
            {
                "status": "scheduled",
                "id": schedule_id,
                "trigger_at": resolved_trigger,
                "label": label_clean,
                "payload": payload,
            },
            ensure_ascii=False,
        )

    def _list_reminders(include_done: bool = False, limit: int = 20) -> str:
        clamped = max(1, min(100, int(limit)))
        reminders = store.list_schedules(
            include_done=bool(include_done),
            limit=clamped,
        )
        return json.dumps(
            {
                "count": len(reminders),
                "include_done": bool(include_done),
                "reminders": reminders,
            },
            ensure_ascii=False,
        )

    return [
        ToolSpec(
            name="recall_memory",
            description="Search past conversation turns and saved memory facts without altering the active L0 prefix.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords or phrase to search in memory.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (1-20, default 6).",
                        "default": 6,
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
            },
            handler=_recall_memory,
        ),
        ToolSpec(
            name="note_memory",
            description="Store or update a distilled long-term memory fact for future recall without altering the active L0 prefix.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Concise canonical key for the memory fact.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Fact content or preference detail to record.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence level between 0.0 and 1.0 (default 0.8).",
                        "default": 0.8,
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": ["key", "value"],
            },
            handler=_note_memory,
        ),
        ToolSpec(
            name="set_mood",
            description="Emit a trailing mood/emotion state update for the companion without altering the active L0 prefix.",
            parameters={
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "description": "Mood label (e.g. calm, warm, playful, curious, concerned).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional brief explanation for the mood shift.",
                        "default": "",
                    },
                },
                "required": ["mood"],
            },
            handler=_set_mood,
        ),
        ToolSpec(
            name="schedule_reminder",
            description="Schedule a future reminder in the timemap store without altering the active L0 prefix. Prefer delay_minutes (>0) for relative delays or provide an ISO-8601 trigger_at timestamp.",
            parameters={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short human-readable reminder label.",
                    },
                    "trigger_at": {
                        "type": "string",
                        "description": "Optional ISO-8601 timestamp when delay_minutes is not provided.",
                        "default": "",
                    },
                    "delay_minutes": {
                        "type": "integer",
                        "description": "Optional positive delay in minutes from current UTC time (takes precedence over trigger_at).",
                        "minimum": 1,
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional detailed reminder text (defaults to label).",
                        "default": "",
                    },
                },
                "required": ["label"],
            },
            handler=_schedule_reminder,
        ),
        ToolSpec(
            name="list_reminders",
            description="List pending (or all) scheduled reminders from the timemap store without altering the active L0 prefix.",
            parameters={
                "type": "object",
                "properties": {
                    "include_done": {
                        "type": "boolean",
                        "description": "Whether to include completed reminders (default false).",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of reminders to return (1-100, default 20).",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": [],
            },
            handler=_list_reminders,
        ),
    ]


__all__ = ["build_memory_tools"]
