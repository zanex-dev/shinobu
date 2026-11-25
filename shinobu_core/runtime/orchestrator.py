# -*- coding: utf-8 -*-
"""
Core conversation orchestrator for Shinobu.

Connects `PrefixFreezer`, `MemoryStore`, append-only trailing memory tools,
`EngineAdapter`, `ShinobuKernel` / `HookBus`, and `Consolidator` while strictly
preserving per-episode L0 prefix immutability (Prefix KV Cache safety).
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import re
from typing import Any, Iterator
import uuid

from shinobu_core.core.kernel import ShinobuKernel, default_home
from shinobu_core.memory.consolidator import ConsolidationResult, consolidate_episode
from shinobu_core.memory.store import MemoryStore
from shinobu_core.memory.tools import build_memory_tools
from shinobu_core.protocol.engine import EngineAdapter, ToolSpec, TurnRequest
from shinobu_core.protocol.events import Event
from shinobu_core.soul.loader import ensure_soul_files, load_soul
from shinobu_core.soul.prefix_freezer import (
    FrozenPrefix,
    PrefixFreezer,
    freeze_prefix,
    verify_frozen,
)


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

        self._freezer: PrefixFreezer = PrefixFreezer()
        self._active_prefix: FrozenPrefix | None = None
        self._state: str = "idle"
        self._mood: str = "calm"

        ensure_soul_files(self.home)

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

        try:
            for round_idx in range(self.max_tool_rounds + 1):
                req_user_text = initial_user_text if round_idx == 0 else ""
                req = TurnRequest(
                    episode_id=ep_id,
                    system_prefix=frozen_prefix_text,
                    history=list(working_history),
                    user_text=req_user_text,
                    tools=tools,
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

        return {
            "status": "archived",
            "closed_episode_id": ep_id,
            "turn_count": turn_count,
        }


__all__ = ["Orchestrator"]
