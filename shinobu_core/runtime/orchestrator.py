# -*- coding: utf-8 -*-
"""
Core conversation orchestrator for Shinobu.

Connects `PrefixFreezer`, `MemoryStore`, append-only trailing memory tools,
`EngineAdapter`, `ShinobuKernel` / `HookBus`, and `Consolidator` while strictly
preserving per-episode L0 prefix immutability (Prefix KV Cache safety).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterator
import uuid

from shinobu_core.core.kernel import ShinobuKernel, default_home
from shinobu_core.engines.openai_compat import normalize_reasoning_effort
from shinobu_core.memory.consolidator import ConsolidationResult, consolidate_episode
from shinobu_core.memory.store import MemoryStore
from shinobu_core.memory.tools import build_memory_tools
from shinobu_core.protocol.engine import EngineAdapter, ToolSpec, TurnRequest
from shinobu_core.protocol.events import Event
from shinobu_core.runtime.classifier import EFFORT_RANK, ratchet_effort
from shinobu_core.soul.loader import ensure_soul_files, load_soul
from shinobu_core.soul.prefix_freezer import (
    FrozenPrefix,
    PrefixFreezer,
    freeze_prefix,
    verify_frozen,
)

logger = logging.getLogger(__name__)


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _classification_to_dict(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    effort = getattr(result, "effort", None)
    if isinstance(effort, str):
        return {
            "effort": effort,
            "backend": str(getattr(result, "backend", "") or ""),
            "elapsed_ms": int(getattr(result, "elapsed_ms", 0) or 0),
            "upgraded": bool(getattr(result, "upgraded", False)),
            "reason": str(getattr(result, "reason", "") or ""),
        }
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _generate_episode_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ep-{stamp}-{uuid.uuid4().hex[:6]}"


def _invoke_tool(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """Invoke a ToolSpec handler cleanly and coerce its output to string."""
    args = dict(arguments) if isinstance(arguments, dict) else {}
    sig = inspect.signature(spec.handler)
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_kw:
        result = spec.handler(**args)
    else:
        filtered = {k: v for k, v in args.items() if k in sig.parameters}
        result = spec.handler(**filtered)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


_RECALL_SMALLTALK: frozenset[str] = frozenset(
    {
        "嗯",
        "嗯嗯",
        "哦",
        "哦哦",
        "啊",
        "好",
        "好的",
        "好呀",
        "好吧",
        "行",
        "行吧",
        "可以",
        "收到",
        "明白",
        "知道",
        "知道了",
        "谢",
        "谢谢",
        "谢谢你",
        "感谢",
        "哈哈",
        "哈哈哈",
        "呵呵",
        "嘻嘻",
        "晚安",
        "早安",
        "早上好",
        "中午好",
        "晚上好",
        "你好",
        "您好",
        "在吗",
        "在不在",
        "没事",
        "拜拜",
        "再见",
        "hi",
        "hey",
        "hello",
        "ok",
        "okay",
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "lol",
        "haha",
        "thanks",
        "thx",
        "ty",
        "bye",
        "goodnight",
        "morning",
    }
)

_EN_RECALL_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "did", "get", "has", "him", "his",
        "how", "its", "let", "may", "now", "old", "see", "two", "way", "who",
        "she", "too", "use", "what", "when", "where", "which", "why", "with",
        "that", "this", "from", "have", "were", "been", "they", "them", "their",
        "will", "would", "could", "should", "about", "into", "just", "like",
        "then", "than", "very", "also", "still", "back", "well", "here", "there",
        "some", "more", "only", "your", "mine", "does", "dont", "didnt", "cant",
        "wont", "is", "am", "be", "to", "of", "in", "on", "at", "by", "it",
        "as", "or", "if", "my", "me", "we", "us", "he", "so", "up", "do", "go",
        "no", "ok", "hi", "hey", "hello", "please", "tell", "remember", "still",
    }
)

_RE_EDGE_PUNCT = re.compile(r"^[\s\W_]+|[\s\W_]+$", re.UNICODE)
_RE_LATIN_OR_CJK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]+")
_RE_CJK_SPLIT = re.compile(
    r"(?:我们还|你们还|他们还|还记得|不记得|记不记得|是不是|有没有|能不能|可不可以|为什么|怎么样|"
    r"帮我查|帮我看|帮我|请问|关于|记得|之前|以前|上次|昨天|昨晚|今天|今晚|明天|明晚|最近|现在|目前|"
    r"刚刚|刚才|什么|怎么|哪里|哪个|哪些|多少|如何|怎样|来着|一下|一点|这个|那个|这些|那些|"
    r"可以|应该|需要|喜欢|想要|提到|说过|聊过|问过|我们|你们|他们|"
    r"[的了呢吗吧啊呀哦嘛呗么我你您他她它咱是在有没不很太真也都就还再又把被给让说问聊喝吃用做看去来到过着])+"
)


def _extract_recall_queries(user_text: str) -> list[str]:
    """
    Extract 1~3 meaningful Chinese phrases or English keywords for local recall.

    Filters out empty input and short small-talk greetings/acknowledgements.
    """
    text = str(user_text or "").strip()
    if not text:
        return []

    cleaned = _RE_EDGE_PUNCT.sub("", text)
    if len(cleaned) < 2:
        return []
    if cleaned.lower() in _RECALL_SMALLTALK:
        return []

    queries: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        item = candidate.strip()
        if not item:
            return
        key = item.lower()
        if key in seen or key in _RECALL_SMALLTALK:
            return
        seen.add(key)
        queries.append(item)

    for match in _RE_LATIN_OR_CJK.finditer(text):
        token = match.group(0)
        if "\u4e00" <= token[0] <= "\u9fff":
            for seg in _RE_CJK_SPLIT.split(token):
                seg = seg.strip()
                if len(seg) < 2:
                    continue
                _add(seg)
                if len(seg) >= 6 and len(queries) < 3:
                    _add(seg[:4])
                if len(queries) >= 3:
                    return queries[:3]
        else:
            norm = token.strip("._-")
            low = norm.lower()
            if len(norm) < 2 or low in _EN_RECALL_STOPWORDS:
                continue
            if norm.isdigit() and len(norm) < 3:
                continue
            _add(norm)
            if len(queries) >= 3:
                return queries[:3]

    return queries[:3]


def _normalize_recall_content(item: dict[str, Any], source: str) -> str:
    raw = item.get("content")
    if not raw and source == "fact":
        key = str(item.get("key") or "").strip()
        val = str(item.get("value") or "").strip()
        raw = f"{key}: {val}" if key and val else (val or key)
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _build_auto_recall_block(
    store: MemoryStore,
    user_text: str,
    *,
    active_episode_id: str,
    limit: int,
    max_chars: int,
) -> str:
    """
    Build a lightweight read-only memory recall hint block via local 0ms FTS5/LIKE search.

    - Skips turns belonging to `active_episode_id` (and tool audit turns).
    - Prioritizes `facts` and historical `episodes` summaries over past turns.
    - Truncates strictly within `max_chars` and returns `""` on no hits or errors.
    """
    if limit <= 0 or max_chars <= 0:
        return ""
    header = "\n\n[Auto Memory Recall (read-only hints)]"
    if max_chars <= len(header) + 5:
        return ""

    queries = _extract_recall_queries(user_text)
    if not queries:
        return ""

    per_query_limit = max(int(limit) * 2, 6)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    seen_ids: set[tuple[str, Any]] = set()
    seen_texts: set[str] = set()
    order_idx = 0

    for query in queries:
        try:
            hits = store.search(query, limit=per_query_limit)
        except Exception:
            continue
        if not hits:
            continue
        for item in hits:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip().lower()
            ep_id = item.get("episode_id")
            if source == "turn":
                if active_episode_id and ep_id == active_episode_id:
                    continue
                if str(item.get("role") or "").strip().lower() == "tool":
                    continue

            content = _normalize_recall_content(item, source)
            if not content:
                continue

            item_id = item.get("id")
            if item_id is not None:
                dedup_id = (source, item_id)
                if dedup_id in seen_ids:
                    continue
                seen_ids.add(dedup_id)

            norm_key = content.lower()
            if norm_key in seen_texts:
                continue
            seen_texts.add(norm_key)

            if source == "fact":
                priority = 0
            elif source in {"episode", "episode_summary", "summary"}:
                priority = 1
            else:
                priority = 2

            candidates.append(
                (
                    priority,
                    order_idx,
                    {"source": source, "role": item.get("role"), "content": content},
                )
            )
            order_idx += 1

    if not candidates:
        return ""

    candidates.sort(key=lambda row: (row[0], row[1]))

    lines: list[str] = [header]
    used = len(header)
    added = 0
    for _, _, entry in candidates:
        if added >= limit:
            break
        source = entry["source"]
        content = entry["content"]
        if source == "fact":
            prefix = "- [fact] "
        elif source in {"episode", "episode_summary", "summary"}:
            prefix = "- [episode] "
        else:
            role = str(entry.get("role") or "past").strip() or "past"
            prefix = f"- [past {role}] "
        line = f"{prefix}{content}"

        remaining = max_chars - used - 1
        if remaining < len(prefix) + 4:
            break
        if len(line) > remaining:
            cut = remaining - 3
            if cut <= len(prefix):
                break
            line = line[:cut].rstrip() + "..."

        lines.append(line)
        used += 1 + len(line)
        added += 1

    if added == 0:
        return ""
    return "\n".join(lines)


class Orchestrator:
    """
    Streaming multi-turn conversation orchestrator with L0 prefix freezing,
    append-only trailing tool loops (up to 4 rounds), and sleep consolidation.
    """

    DEFAULT_MAX_TOOL_ROUNDS = 4
    DEFAULT_AUTO_RECALL_LIMIT = 4
    DEFAULT_AUTO_RECALL_MAX_CHARS = 480

    def __init__(
        self,
        home: Path | str | None,
        store: MemoryStore,
        engine: EngineAdapter,
        kernel: ShinobuKernel | None = None,
        consolidator_engine: EngineAdapter | None = None,
        *,
        history_limit: int = 50,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        auto_recall_limit: int = DEFAULT_AUTO_RECALL_LIMIT,
        auto_recall_max_chars: int = DEFAULT_AUTO_RECALL_MAX_CHARS,
        classifier: Any | None = None,
        inactivity_archive_sec: float = 0.0,
        on_episode_reset: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.home: Path = Path(home) if home is not None else default_home()
        self.store: MemoryStore = store
        self.engine: EngineAdapter = engine
        self.kernel: ShinobuKernel | None = kernel
        self.consolidator_engine: EngineAdapter = consolidator_engine or engine
        self.history_limit: int = max(1, int(history_limit))
        self.max_tool_rounds: int = max(1, int(max_tool_rounds))
        self.auto_recall_limit: int = max(0, int(auto_recall_limit))
        self.auto_recall_max_chars: int = max(0, int(auto_recall_max_chars))
        self.classifier: Any | None = classifier
        self.inactivity_archive_sec: float = max(0.0, float(inactivity_archive_sec))
        self._on_episode_reset: Callable[[dict[str, Any]], Any] | None = on_episode_reset
        self._has_custom_clock: bool = clock is not None
        self._clock: Callable[[], float] = clock or time.monotonic

        self._freezer: PrefixFreezer = PrefixFreezer()
        self._active_prefix: FrozenPrefix | None = None
        self._state: str = "idle"
        self._mood: str = "calm"

        min_eff = self._resolve_classifier_bound("min_effort", "low")
        max_eff = self._resolve_classifier_bound("max_effort", "high")
        if EFFORT_RANK[min_eff] > EFFORT_RANK[max_eff]:
            min_eff, max_eff = "low", "high"
        self._min_effort: str = min_eff
        self._max_effort: str = max_eff
        self._episode_effort: str = self._min_effort
        self._manual_effort: str = ""
        self._last_classification: dict[str, Any] | None = None
        self._last_activity_mono: float | None = None
        self._last_activity_episode_id: str | None = None

        ensure_soul_files(self.home)

    def _resolve_classifier_bound(self, attr: str, default: str) -> str:
        raw = getattr(self.classifier, attr, None) if self.classifier is not None else None
        if isinstance(raw, str) and raw.strip():
            try:
                norm = normalize_reasoning_effort(raw)
                if norm in EFFORT_RANK:
                    return norm
            except ValueError:
                pass
        return default

    def _touch_activity(self, episode_id: str) -> None:
        self._last_activity_mono = float(self._clock())
        self._last_activity_episode_id = episode_id

    def set_manual_reasoning_effort(self, effort: str | None) -> str:
        """Lock reasoning effort to a specific tier, or clear lock back to auto/default."""
        raw = "" if effort is None else str(effort).strip().lower()
        if raw in ("", "default", "auto"):
            self._manual_effort = ""
            return ""
        norm = normalize_reasoning_effort(raw)
        if not norm or norm not in EFFORT_RANK:
            raise ValueError(f"Unsupported reasoning effort: {effort!r}")
        self._manual_effort = norm
        return self._manual_effort

    def reasoning_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of current reasoning effort state."""
        if self._manual_effort:
            mode = "manual"
            effective_effort = self._manual_effort
        elif self.classifier is not None:
            mode = "auto"
            effective_effort = self._episode_effort
        else:
            mode = "default"
            effective_effort = ""
        return {
            "mode": mode,
            "effective_effort": effective_effort,
            "episode_effort": self._episode_effort,
            "manual_effort": self._manual_effort,
            "last_classification": (
                dict(self._last_classification)
                if self._last_classification is not None
                else None
            ),
        }

    def _reset_episode_reasoning(
        self,
        *,
        reason: str,
        closed_episode_id: str | None = None,
        new_episode_id: str | None = None,
    ) -> None:
        prev_episode_effort = self._episode_effort
        prev_manual_effort = self._manual_effort
        self._manual_effort = ""
        self._episode_effort = self._min_effort
        self._last_classification = None
        if self._on_episode_reset is not None:
            payload = {
                "reason": reason,
                "closed_episode_id": closed_episode_id,
                "episode_id": new_episode_id,
                "previous_episode_effort": prev_episode_effort,
                "previous_manual_effort": prev_manual_effort,
                "episode_effort": self._episode_effort,
                "manual_effort": self._manual_effort,
                "mode": "auto" if self.classifier is not None else "default",
            }
            try:
                self._on_episode_reset(payload)
            except Exception as exc:
                logger.warning("Orchestrator on_episode_reset callback failed: %s", exc)

    def _invoke_classifier(
        self,
        user_text: str,
        current_effort: str,
        recent_history: list[dict[str, Any]],
    ) -> Any:
        classify_fn = getattr(self.classifier, "classify", None)
        if not callable(classify_fn):
            if callable(self.classifier):
                classify_fn = self.classifier
            else:
                raise TypeError("classifier must expose a callable classify() method")

        sig = inspect.signature(classify_fn)
        params = sig.parameters
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs: dict[str, Any] = {}
        if "current_effort" in params or accepts_var_kw:
            kwargs["current_effort"] = current_effort
        if "recent_context" in params:
            ann = str(params["recent_context"].annotation or "")
            recent_slice = recent_history[-4:]
            if any(tok in ann for tok in ("list", "List", "Sequence", "Iterable")):
                kwargs["recent_context"] = list(recent_slice)
            else:
                formatted = "\n".join(
                    f"{m.get('role', '')}: {str(m.get('content', ''))[:120]}"
                    for m in recent_slice
                ).strip()
                kwargs["recent_context"] = formatted[:360]
        return classify_fn(user_text, **kwargs)

    def _resolve_turn_effort(
        self,
        user_text: str,
        recent_history: list[dict[str, Any]],
    ) -> str:
        """Resolve effective reasoning effort once per user turn (round_idx == 0)."""
        if self._manual_effort:
            return self._manual_effort
        if self.classifier is None:
            return ""

        current = self._episode_effort or self._min_effort
        try:
            raw_result = self._invoke_classifier(user_text, current, recent_history)
            cls_dict = _classification_to_dict(raw_result)
            if cls_dict is not None:
                candidate = str(cls_dict.get("effort") or current)
            elif isinstance(raw_result, str) and raw_result.strip():
                candidate = raw_result.strip()
                cls_dict = {
                    "effort": candidate,
                    "backend": "custom",
                    "elapsed_ms": 0,
                    "upgraded": False,
                    "reason": "",
                }
            else:
                candidate = current
                cls_dict = {
                    "effort": current,
                    "backend": "fallback",
                    "elapsed_ms": 0,
                    "upgraded": False,
                    "reason": "unrecognized_classifier_result",
                }
            new_effort = ratchet_effort(
                current,
                candidate,
                min_effort=self._min_effort,
                max_effort=self._max_effort,
            )
            upgraded = EFFORT_RANK[new_effort] > EFFORT_RANK[current]
            self._episode_effort = new_effort
            cls_dict["effort"] = new_effort
            cls_dict["upgraded"] = upgraded
            self._last_classification = cls_dict
        except Exception as exc:
            logger.warning(
                "Complexity classifier failed; retaining effort %s: %s", current, exc
            )
            self._episode_effort = current
            self._last_classification = {
                "effort": current,
                "backend": "fallback",
                "elapsed_ms": 0,
                "upgraded": False,
                "reason": f"error: {exc}",
            }
        return self._episode_effort

    def check_and_archive_inactive(self) -> dict[str, Any] | None:
        """Zero-token lazy archive if the active episode has >=1 turn and exceeded inactivity TTL."""
        if self.inactivity_archive_sec <= 0:
            return None

        ep_id: str | None = (
            self._active_prefix.episode_id if self._active_prefix is not None else None
        )
        if ep_id is not None:
            existing = self.store.get_episode(ep_id)
            if existing is None or existing["status"] != "active":
                ep_id = None
        if ep_id is None:
            active_ep = self.store.get_active_episode()
            if active_ep is not None:
                ep_id = str(active_ep["id"])
        if ep_id is None:
            return None

        latest_turns = self.store.get_turns(ep_id, limit=1)
        if not latest_turns:
            return None

        mono_elapsed: float | None = None
        if (
            self._last_activity_mono is not None
            and self._last_activity_episode_id == ep_id
        ):
            mono_elapsed = max(0.0, float(self._clock()) - self._last_activity_mono)

        db_elapsed: float | None = None
        if mono_elapsed is None or not self._has_custom_clock:
            turn_dt = _parse_utc_timestamp(latest_turns[-1].get("created_at"))
            if turn_dt is not None:
                db_elapsed = max(
                    0.0, (datetime.now(timezone.utc) - turn_dt).total_seconds()
                )

        elapsed_candidates = [v for v in (mono_elapsed, db_elapsed) if v is not None]
        if not elapsed_candidates:
            return None

        if max(elapsed_candidates) >= self.inactivity_archive_sec:
            return self.archive_and_reset(summary="(超时无活跃自动归档)")
        return None

    @property
    def active_episode_id(self) -> str | None:
        return self._active_prefix.episode_id if self._active_prefix is not None else None

    @property
    def active_prefix(self) -> FrozenPrefix | None:
        return self._active_prefix

    @property
    def state(self) -> str:
        return self._state

    @property
    def mood(self) -> str:
        return self._mood

    # ------------------------------------------------------------------
    # Internal helpers (HookBus & Kernel tools)
    # ------------------------------------------------------------------

    def _get_hook_bus(self) -> Any:
        if self.kernel is None:
            return None
        for attr in ("hooks", "hook_bus", "bus"):
            bus = getattr(self.kernel, attr, None)
            if bus is not None and callable(getattr(bus, "emit", None)):
                return bus
        return None

    def _emit_hook(self, event_name: str, payload: Any) -> None:
        bus = self._get_hook_bus()
        if bus is None:
            return
        bus.emit(event_name, payload)

    def _make_event(
        self,
        kind: str,
        payload: dict[str, Any],
        episode_id: str,
    ) -> Event:
        event = Event(
            kind=kind,
            payload=payload,
            ts=_utc_now_iso(),
            episode_id=episode_id,
        )
        self._emit_hook(kind, event)
        return event

    def _transition_state(self, new_state: str, episode_id: str) -> Event | None:
        if self._state == new_state:
            return None
        prev = self._state
        self._state = new_state
        return self._make_event(
            "state_changed",
            {"from": prev, "to": new_state},
            episode_id,
        )

    def _collect_tools(
        self,
        episode_id: str,
        mood_queue: list[Event],
    ) -> list[ToolSpec]:
        def _on_mood(mood_value: str, reason: str) -> None:
            self._mood = mood_value
            mood_queue.append(
                self._make_event(
                    "mood_changed",
                    {"mood": mood_value, "reason": reason},
                    episode_id,
                )
            )

        tools: list[ToolSpec] = list(
            build_memory_tools(self.store, episode_id, on_mood=_on_mood)
        )
        seen_names = {t.name for t in tools}

        if self.kernel is not None:
            raw_tools: Any = None
            if callable(getattr(self.kernel, "list_tools", None)):
                raw_tools = self.kernel.list_tools()
            elif hasattr(self.kernel, "tools"):
                raw_tools = getattr(self.kernel, "tools")

            extra_iter: list[Any] = []
            if isinstance(raw_tools, dict):
                extra_iter = list(raw_tools.values())
            elif isinstance(raw_tools, (list, tuple, set)):
                extra_iter = list(raw_tools)

            for item in extra_iter:
                if isinstance(item, ToolSpec) and item.name not in seen_names:
                    tools.append(item)
                    seen_names.add(item.name)

        return tools

    # ------------------------------------------------------------------
    # Episode lifecycle & L0 prefix freezing
    # ------------------------------------------------------------------

    def ensure_episode(self, episode_id: str | None = None) -> FrozenPrefix:
        """
        Ensure an active episode exists and return its immutable `FrozenPrefix`.

        - If an active episode is already frozen and matches `episode_id` (or
          `episode_id` is None), reuse the exact frozen prefix without re-reading
          disk files or recomputing the hash.
        - If resuming an active episode already stored in SQLite, restore its
          stored `prefix_text` and verify its SHA-256 integrity.
        - For a brand new episode, read `store.latest_closed_summary()`, assemble
          and freeze L0 via `load_soul` + `freeze_prefix`, and persist it.
        """
        requested_id = episode_id.strip() if isinstance(episode_id, str) and episode_id.strip() else None
        prev_episode_id = (
            self._active_prefix.episode_id if self._active_prefix is not None else None
        )

        if self._active_prefix is not None:
            if requested_id is None or requested_id == self._active_prefix.episode_id:
                existing = self.store.get_episode(self._active_prefix.episode_id)
                if existing is not None and existing["status"] == "active":
                    return self._active_prefix
                # Active episode was closed externally; drop cached handle.
                self._active_prefix = None

        target_id = requested_id or _generate_episode_id()
        existing_row = self.store.get_episode(target_id)
        if existing_row is not None:
            if existing_row["status"] != "active":
                raise ValueError(f"Episode {target_id!r} is already {existing_row['status']}")
            restored = FrozenPrefix(
                episode_id=existing_row["id"],
                text=existing_row["prefix_text"],
                sha256=existing_row["prefix_sha256"],
                created_at=existing_row["created_at"],
            )
            if not verify_frozen(restored):
                raise ValueError(f"Stored prefix SHA-256 mismatch for episode {target_id!r}")
            self._active_prefix = restored
            if prev_episode_id is not None and restored.episode_id != prev_episode_id:
                self._last_activity_mono = None
                self._last_activity_episode_id = None
                self._reset_episode_reasoning(
                    reason="episode_switch",
                    closed_episode_id=prev_episode_id,
                    new_episode_id=restored.episode_id,
                )
            return restored

        yesterday_summary = self.store.latest_closed_summary()
        ensure_soul_files(self.home)
        bundle = load_soul(self.home, yesterday_summary=yesterday_summary)
        frozen = self._freezer.freeze(target_id, bundle)
        if not verify_frozen(frozen):
            raise RuntimeError(f"Prefix verification failed for new episode {target_id!r}")

        row = self.store.create_or_get_active_episode(
            target_id,
            frozen.text,
            frozen.sha256,
        )
        locked = FrozenPrefix(
            episode_id=row["id"],
            text=row["prefix_text"],
            sha256=row["prefix_sha256"],
            created_at=row["created_at"],
        )
        if not verify_frozen(locked):
            raise ValueError(f"Persisted prefix SHA-256 mismatch for episode {target_id!r}")

        self._active_prefix = locked
        if prev_episode_id is not None and locked.episode_id != prev_episode_id:
            self._last_activity_mono = None
            self._last_activity_episode_id = None
            self._reset_episode_reasoning(
                reason="episode_switch",
                closed_episode_id=prev_episode_id,
                new_episode_id=locked.episode_id,
            )
        return locked

    # ------------------------------------------------------------------
    # Streaming conversation + append-only trailing tool loop
    # ------------------------------------------------------------------

    def send_message(self, user_text: str) -> Iterator[Event]:
        """
        Send a user message in the active episode and yield streaming `Event`s.

        Supports up to `max_tool_rounds` (default 4) append-only trailing tool
        execution rounds while keeping the episode's `system_prefix` untouched.
        """
        text = str(user_text or "").strip()
        if not text:
            raise ValueError("user_text must be a non-empty string")

        self.check_and_archive_inactive()

        frozen = self.ensure_episode()
        ep_id = frozen.episode_id
        frozen_prefix_text = frozen.text
        frozen_sha256 = frozen.sha256

        prior_rows = self.store.get_turns(ep_id, limit=self.history_limit)
        working_history: list[dict[str, Any]] = [
            {"role": row["role"], "content": row["content"]}
            for row in prior_rows
            if row["role"] != "tool"
        ]

        # Persist incoming user turn immediately (append-only, raw text only).
        self.store.append_turn(ep_id, "user", text)
        self._touch_activity(ep_id)

        recall_block = _build_auto_recall_block(
            self.store,
            text,
            active_episode_id=ep_id,
            limit=self.auto_recall_limit,
            max_chars=self.auto_recall_max_chars,
        )
        initial_user_text = f"{text}{recall_block}" if recall_block else text

        mood_queue: list[Event] = []
        tools = self._collect_tools(ep_id, mood_queue)
        tool_map: dict[str, ToolSpec] = {t.name: t for t in tools}

        ev = self._transition_state("thinking", ep_id)
        if ev is not None:
            yield ev

        assistant_parts: list[str] = []
        tool_rounds = 0
        user_pushed_to_history = False
        effective_effort = ""

        try:
            for round_idx in range(self.max_tool_rounds + 1):
                if round_idx == 0:
                    effective_effort = self._resolve_turn_effort(text, working_history)
                req_user_text = initial_user_text if round_idx == 0 else ""
                req = TurnRequest(
                    episode_id=ep_id,
                    system_prefix=frozen_prefix_text,
                    history=list(working_history),
                    user_text=req_user_text,
                    tools=tools,
                    reasoning_effort=effective_effort,
                )

                round_text_parts: list[str] = []
                round_tool_calls: list[dict[str, Any]] = []

                for chunk in self.engine.stream_turn(req):
                    if chunk.delta:
                        speaking_ev = self._transition_state("speaking", ep_id)
                        if speaking_ev is not None:
                            yield speaking_ev
                        round_text_parts.append(chunk.delta)
                        assistant_parts.append(chunk.delta)
                        yield self._make_event(
                            "token_delta",
                            {"delta": chunk.delta, "round": round_idx},
                            ep_id,
                        )
                    if chunk.tool_calls:
                        round_tool_calls.extend(chunk.tool_calls)

                round_text = "".join(round_text_parts)

                if not round_tool_calls:
                    break

                if round_idx >= self.max_tool_rounds:
                    break

                tool_rounds += 1
                if not user_pushed_to_history:
                    working_history.append({"role": "user", "content": text})
                    user_pushed_to_history = True

                normalized_calls: list[dict[str, Any]] = []
                for call_idx, raw_call in enumerate(round_tool_calls):
                    call_copy = dict(raw_call)
                    fallback_id = (
                        f"call_{round_idx + 1}_{call_idx}"
                        if len(round_tool_calls) > 1
                        else f"call_{round_idx + 1}"
                    )
                    call_copy["id"] = str(call_copy.get("id") or fallback_id)
                    normalized_calls.append(call_copy)

                working_history.append(
                    {
                        "role": "assistant",
                        "content": round_text,
                        "tool_calls": normalized_calls,
                    }
                )

                tool_state_ev = self._transition_state("thinking", ep_id)
                if tool_state_ev is not None:
                    yield tool_state_ev

                for call in normalized_calls:
                    call_id = str(call["id"])
                    tool_name = str(call.get("name") or "").strip()
                    raw_args = call.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                        except json.JSONDecodeError:
                            parsed_args = {"_raw": raw_args}
                    elif isinstance(raw_args, dict):
                        parsed_args = dict(raw_args)
                    else:
                        parsed_args = {}

                    spec = tool_map.get(tool_name)
                    ok = True
                    error_msg: str | None = None
                    if spec is None:
                        ok = False
                        error_msg = f"Unknown tool: {tool_name}"
                        tool_output = json.dumps(
                            {"status": "error", "error": error_msg},
                            ensure_ascii=False,
                        )
                    else:
                        try:
                            tool_output = _invoke_tool(spec, parsed_args)
                        except Exception as exc:  # pragma: no cover - defensive tool isolation
                            ok = False
                            error_msg = str(exc)
                            tool_output = json.dumps(
                                {"status": "error", "tool": tool_name, "error": error_msg},
                                ensure_ascii=False,
                            )

                    # Append-only trailing role="tool" message in SQLite & working history.
                    tool_record = json.dumps(
                        {
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "arguments": parsed_args,
                            "result": tool_output,
                        },
                        ensure_ascii=False,
                    )
                    self.store.append_turn(ep_id, "tool", tool_record)
                    working_history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": tool_output,
                        }
                    )

                    yield self._make_event(
                        "tool_call",
                        {
                            "id": call_id,
                            "name": tool_name,
                            "arguments": parsed_args,
                            "result": tool_output,
                            "ok": ok,
                            "error": error_msg,
                            "round": tool_rounds,
                        },
                        ep_id,
                    )

                    # Flush any mood_changed events emitted by `set_mood`.
                    while mood_queue:
                        yield mood_queue.pop(0)

            final_text = "".join(assistant_parts)
            if final_text:
                self.store.append_turn(ep_id, "assistant", final_text)
            self._touch_activity(ep_id)

            idle_ev = self._transition_state("idle", ep_id)
            if idle_ev is not None:
                yield idle_ev

            yield self._make_event(
                "done",
                {
                    "episode_id": ep_id,
                    "text": final_text,
                    "prefix_sha256": frozen_sha256,
                    "tool_rounds": tool_rounds,
                    "mood": self._mood,
                },
                ep_id,
            )
        except Exception as exc:
            yield self._make_event("error", {"error": str(exc)}, ep_id)
            idle_ev = self._transition_state("idle", ep_id)
            if idle_ev is not None:
                yield idle_ev
            raise

    # ------------------------------------------------------------------
    # Sleep-time consolidation & episode rotation
    # ------------------------------------------------------------------

    def sleep_and_consolidate(
        self,
        *,
        date_str: str | None = None,
    ) -> ConsolidationResult:
        """
        Close and consolidate the currently active episode into journals,
        facts, timemap, and version-backed MEMORY.md; next turn opens a new episode.
        """
        ep_id: str | None = (
            self._active_prefix.episode_id if self._active_prefix is not None else None
        )
        if ep_id is None:
            active_ep = self.store.get_active_episode()
            if active_ep is not None:
                ep_id = str(active_ep["id"])
        if ep_id is None:
            raise RuntimeError("No active episode to consolidate")

        result = consolidate_episode(
            home=self.home,
            store=self.store,
            engine=self.consolidator_engine,
            episode_id=ep_id,
            date_str=date_str,
            close_episode=True,
        )

        self._active_prefix = None
        if callable(getattr(self._freezer, "release", None)):
            self._freezer.release(ep_id)
        self._last_activity_mono = None
        self._last_activity_episode_id = None
        self._reset_episode_reasoning(
            reason="sleep_and_consolidate",
            closed_episode_id=ep_id,
        )

        self._emit_hook("episode_consolidated", result)
        return result

    def archive_and_reset(self, *, summary: str = "") -> dict[str, Any]:
        """
        Fast-archive the active episode without invoking any LLM consolidator,
        preserving all turns in SQLite so the next message starts a clean episode.
        """
        ep_id: str | None = (
            self._active_prefix.episode_id if self._active_prefix is not None else None
        )
        if ep_id is None:
            active_ep = self.store.get_active_episode()
            if active_ep is not None:
                ep_id = str(active_ep["id"])
        if ep_id is None:
            return {
                "status": "idle",
                "closed_episode_id": None,
                "turn_count": 0,
            }

        turn_count = self.store.count_turns(ep_id)
        final_summary = summary.strip() or "(手动快速归档)"
        self.store.close_episode(ep_id, final_summary)

        self._active_prefix = None
        if callable(getattr(self._freezer, "release", None)):
            self._freezer.release(ep_id)
        self._last_activity_mono = None
        self._last_activity_episode_id = None
        self._reset_episode_reasoning(
            reason="archive_and_reset",
            closed_episode_id=ep_id,
        )

        return {
            "status": "archived",
            "closed_episode_id": ep_id,
            "turn_count": turn_count,
        }


__all__ = ["Orchestrator"]
