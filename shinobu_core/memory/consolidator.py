# -*- coding: utf-8 -*-
"""
Sleep-time episode consolidator for Shinobu.

Performs an isolated short JSON completion call against an episode's transcript
without mutating any active L0 prefix, then:
1. Upserts distilled long-term facts into `MemoryStore.facts`.
2. Records open loops / follow-up reminders into `MemoryStore.timemap`.
3. Appends a human-readable journal entry to `<home>/journals/YYYY-MM-DD.md`.
4. Backs up `<home>/soul/MEMORY.md` to `<home>/_versions/MEMORY_vN.md` before
   non-destructively appending newly consolidated memory items.
5. Closes the episode in `MemoryStore` with the distilled summary so the next
   episode automatically loads it into the `## YESTERDAY` L0 section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from shinobu_core.core.kernel import default_home
from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.engine import EngineAdapter
from shinobu_core.soul.loader import ensure_soul_files, normalize_markdown

_VERSION_RE = re.compile(r"^MEMORY_v(\d+)\.md$")

_CONSOLIDATOR_SYSTEM_PROMPT = (
    "You are Shinobu's offline sleep-time memory consolidator.\n"
    "Distill the completed conversation episode into a single JSON object with keys:\n"
    '- "summary": concise recap (<= 300 chars) suitable for next episode YESTERDAY context.\n'
    '- "new_facts": list of {"key": str, "value": str, "confidence": float(0..1)}.\n'
    '- "open_loops": list of {"label": str, "trigger_at": ISO-8601 str or "", "payload": dict}.\n'
    '- "memory_entries": optional list of short bullet strings worth keeping in MEMORY.md.\n'
    "Return ONLY valid JSON."
)

_COMPACT_SYSTEM_PROMPT = (
    "You are Shinobu's offline long-term memory compactor and health reviewer.\n"
    "Inspect the current MEMORY.md and structured facts list, then deduplicate, prune expired/obsolete "
    "items, and merge conflicting entries without inventing unverified facts.\n"
    "Return a single JSON object with keys:\n"
    '- "summary": concise recap (<= 300 chars) of what was merged, pruned, or retained.\n'
    '- "compacted_memory_md": clean, consolidated full Markdown for MEMORY.md (non-empty if updating).\n'
    '- "upsert_facts": list of {"key": str, "value": str, "confidence": float(0..1)} to add or update.\n'
    '- "remove_fact_ids": list of integer fact IDs to delete because they are duplicate, obsolete, or superseded.\n'
    "Return ONLY valid JSON."
)


@dataclass(frozen=True)
class ConsolidationResult:
    """Structured output of consolidating a finished episode."""

    episode_id: str
    summary: str
    new_facts: list[dict[str, Any]]
    open_loops: list[dict[str, Any]]
    journal_path: Path


@dataclass(frozen=True)
class CompactionResult:
    """Structured output of previewing or applying long-term memory compaction."""

    applied: bool
    dry_run: bool
    summary: str
    before_chars: int
    after_chars: int
    backup_path: Path | None
    updated_facts: list[dict[str, Any]]
    removed_fact_ids: list[int]
    preview_memory_md: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_home(home: Path | str | None) -> Path:
    if home is None:
        return default_home()
    return Path(home)


def _format_transcript(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return "(no turns recorded)"
    lines: list[str] = []
    for turn in turns:
        role = str(turn.get("role") or "unknown").strip()
        content = str(turn.get("content") or "").strip()
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _build_user_prompt(
    episode_id: str,
    turns: list[dict[str, Any]],
    existing_memory_md: str,
) -> str:
    mem_preview = normalize_markdown(existing_memory_md)
    if len(mem_preview) > 2000:
        mem_preview = mem_preview[-2000:]
    transcript = _format_transcript(turns)
    return (
        f"Episode ID: {episode_id}\n\n"
        f"Current MEMORY.md:\n{mem_preview or '(empty)'}\n\n"
        f"Episode Transcript:\n{transcript}\n"
    )


def _fallback_summary(turns: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for turn in turns:
        role = str(turn.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = " ".join(str(turn.get("content") or "").split())
        if content:
            snippets.append(f"{role}: {content[:80]}")
    if not snippets:
        return "Completed episode with no dialogue turns."
    return " / ".join(snippets[-4:])[:300]


def _normalize_facts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            continue
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        confidence = max(0.0, min(1.0, confidence))
        out.append({"key": key, "value": value, "confidence": confidence})
    return out


def _normalize_open_loops(raw: Any, default_trigger_at: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            label = item.strip()
            if not label:
                continue
            out.append({"label": label, "trigger_at": default_trigger_at, "payload": {}})
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("title") or item.get("task") or "").strip()
        if not label:
            continue
        trigger_at = str(item.get("trigger_at") or "").strip() or default_trigger_at
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = {"raw": payload} if payload is not None else {}
        out.append({"label": label, "trigger_at": trigger_at, "payload": payload})
    return out


def _normalize_memory_entries(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = " ".join(str(item or "").split()).strip()
        if text:
            out.append(text)
    return out


def _backup_memory_md(memory_path: Path, versions_dir: Path) -> Path | None:
    """Copy existing MEMORY.md to `_versions/MEMORY_vN.md` without overwriting prior backups."""
    if not memory_path.exists():
        return None
    versions_dir.mkdir(parents=True, exist_ok=True)
    max_version = 0
    for entry in versions_dir.iterdir():
        if not entry.is_file():
            continue
        match = _VERSION_RE.match(entry.name)
        if match:
            max_version = max(max_version, int(match.group(1)))
    backup_path = versions_dir / f"MEMORY_v{max_version + 1}.md"
    shutil.copy2(memory_path, backup_path)
    return backup_path


def _append_journal(
    home_path: Path,
    episode_id: str,
    date_label: str,
    summary: str,
    new_facts: list[dict[str, Any]],
    open_loops: list[dict[str, Any]],
) -> Path:
    journals_dir = home_path / "journals"
    journals_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journals_dir / f"{date_label}.md"

    now_iso = _utc_now_iso()
    lines: list[str] = []
    if not journal_path.exists():
        lines.append(f"# Journal {date_label}")
        lines.append("")

    lines.append(f"## Episode `{episode_id}` ({now_iso})")
    lines.append("")
    lines.append(f"**Summary**: {summary}")
    if new_facts:
        lines.append("")
        lines.append("### New Facts")
        for fact in new_facts:
            lines.append(
                f"- `{fact['key']}`: {fact['value']} (confidence: {fact['confidence']:.2f})"
            )
    if open_loops:
        lines.append("")
        lines.append("### Open Loops")
        for loop in open_loops:
            lines.append(f"- [{loop['trigger_at']}] {loop['label']}")
    lines.append("")

    block = "\n".join(lines) + "\n"
    with journal_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(block)
    return journal_path


def _update_memory_md(
    home_path: Path,
    episode_id: str,
    date_label: str,
    summary: str,
    new_facts: list[dict[str, Any]],
    memory_entries: list[str],
) -> Path | None:
    """Backup MEMORY.md to `_versions/MEMORY_vN.md` and append new consolidated memories."""
    soul_dir = ensure_soul_files(home_path)
    memory_path = soul_dir / "MEMORY.md"
    existing_raw = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    existing_norm = normalize_markdown(existing_raw)

    bullets: list[str] = []
    for entry in memory_entries:
        bullets.append(f"- {entry}")
    for fact in new_facts:
        bullets.append(f"- {fact['key']}: {fact['value']}")
    if not bullets and summary:
        bullets.append(f"- {summary}")

    if not bullets:
        return None

    # Deduplicate bullets while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for line in bullets:
        if line not in seen:
            seen.add(line)
            deduped.append(line)

    _backup_memory_md(memory_path, home_path / "_versions")

    section_lines = [f"## Consolidated {date_label} ({episode_id})", *deduped]
    addition = "\n".join(section_lines)
    updated = f"{existing_norm}\n\n{addition}\n" if existing_norm else f"{addition}\n"
    memory_path.write_text(updated, encoding="utf-8", newline="\n")
    return memory_path


def consolidate_episode(
    home: Path | str | None,
    store: MemoryStore,
    engine: EngineAdapter,
    episode_id: str,
    *,
    date_str: str | None = None,
    turn_limit: int = 200,
    close_episode: bool = True,
) -> ConsolidationResult:
    """
    Distill an episode via an isolated `engine.complete_json(...)` call and persist outputs.

    Writes facts & timemap schedules to SQLite, appends `<home>/journals/YYYY-MM-DD.md`,
    backs up and updates `<home>/soul/MEMORY.md` via `<home>/_versions/MEMORY_vN.md`,
    and closes the episode with its distilled summary.
    """
    ep_id = str(episode_id or "").strip()
    if not ep_id:
        raise ValueError("episode_id must be a non-empty string")

    episode = store.get_episode(ep_id)
    if episode is None:
        raise KeyError(f"Episode not found: {ep_id}")

    home_path = _resolve_home(home)
    soul_dir = ensure_soul_files(home_path)
    memory_path = soul_dir / "MEMORY.md"
    existing_memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    turns = store.get_turns(ep_id, limit=max(1, int(turn_limit)))
    user_prompt = _build_user_prompt(ep_id, turns, existing_memory)

    raw_output = engine.complete_json(_CONSOLIDATOR_SYSTEM_PROMPT, user_prompt)
    if isinstance(raw_output, str):
        parsed: dict[str, Any] = json.loads(raw_output)
    elif isinstance(raw_output, dict):
        parsed = dict(raw_output)
    else:
        parsed = {}

    now_iso = _utc_now_iso()
    date_label = (date_str or "").strip() or datetime.now().strftime("%Y-%m-%d")

    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        summary = _fallback_summary(turns)

    new_facts = _normalize_facts(parsed.get("new_facts"))
    open_loops = _normalize_open_loops(parsed.get("open_loops"), default_trigger_at=now_iso)
    memory_entries = _normalize_memory_entries(parsed.get("memory_entries"))

    saved_facts: list[dict[str, Any]] = []
    for fact in new_facts:
        store.upsert_fact(
            key=fact["key"],
            value=fact["value"],
            confidence=fact["confidence"],
            source_episode=ep_id,
        )
        saved_facts.append(fact)

    saved_loops: list[dict[str, Any]] = []
    for loop in open_loops:
        payload = dict(loop.get("payload") or {})
        payload.setdefault("source_episode", ep_id)
        store.add_schedule(
            trigger_at=loop["trigger_at"],
            label=loop["label"],
            payload=payload,
        )
        saved_loops.append(
            {
                "label": loop["label"],
                "trigger_at": loop["trigger_at"],
                "payload": payload,
            }
        )

    journal_path = _append_journal(
        home_path=home_path,
        episode_id=ep_id,
        date_label=date_label,
        summary=summary,
        new_facts=saved_facts,
        open_loops=saved_loops,
    )

    _update_memory_md(
        home_path=home_path,
        episode_id=ep_id,
        date_label=date_label,
        summary=summary,
        new_facts=saved_facts,
        memory_entries=memory_entries,
    )

    if close_episode:
        store.close_episode(ep_id, summary)

    return ConsolidationResult(
        episode_id=ep_id,
        summary=summary,
        new_facts=saved_facts,
        open_loops=saved_loops,
        journal_path=journal_path,
    )


def _build_compaction_user_prompt(
    existing_memory: str,
    facts: list[dict[str, Any]],
) -> str:
    memory_block = existing_memory.strip() or "(empty)"
    if facts:
        fact_lines: list[str] = []
        for fact in facts:
            fid = fact.get("id")
            key = str(fact.get("key") or "")
            value = str(fact.get("value") or "")
            try:
                conf = float(fact.get("confidence", 0.8))
            except (TypeError, ValueError):
                conf = 0.8
            updated_at = str(fact.get("updated_at") or "")
            suffix = f", updated_at={updated_at}" if updated_at else ""
            fact_lines.append(
                f"- [id={fid}] {key}: {value} (confidence={conf:.2f}{suffix})"
            )
        facts_block = "\n".join(fact_lines)
    else:
        facts_block = "(none)"

    return (
        "## CURRENT MEMORY.md\n"
        f"{memory_block}\n\n"
        "## CURRENT STRUCTURED FACTS\n"
        f"{facts_block}\n\n"
        "Review and compact the long-term memory above into the required JSON object."
    )


def _normalize_remove_fact_ids(raw: Any, valid_ids: set[int]) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if item <= 0 or item not in valid_ids or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def compact_memory(
    home: Path | str | None,
    store: MemoryStore,
    engine: EngineAdapter,
    *,
    dry_run: bool = False,
) -> CompactionResult:
    """
    Review, deduplicate, prune, and merge long-term memory (`MEMORY.md` + `facts`).

    Calls an isolated `engine.complete_json(...)` pass without mutating active L0 prefixes
    or touching `journals/`. When `dry_run=True`, performs zero disk/database writes and
    returns a full preview. When `dry_run=False`, backs up `soul/MEMORY.md` to
    `<home>/_versions/MEMORY_vN.md` before atomically writing compacted content and
    applying validated fact deletions/upserts.
    """
    home_path = _resolve_home(home)
    soul_dir = ensure_soul_files(home_path)
    memory_path = soul_dir / "MEMORY.md"
    existing_raw = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    before_chars = len(existing_raw)

    facts = store.list_facts(limit=300)
    valid_fact_ids: set[int] = {
        f["id"]
        for f in facts
        if isinstance(f, dict)
        and isinstance(f.get("id"), int)
        and not isinstance(f.get("id"), bool)
        and f["id"] > 0
    }

    user_prompt = _build_compaction_user_prompt(existing_raw, facts)
    raw_output = engine.complete_json(_COMPACT_SYSTEM_PROMPT, user_prompt)
    if isinstance(raw_output, str):
        try:
            decoded = json.loads(raw_output)
        except json.JSONDecodeError:
            decoded = {}
        parsed: dict[str, Any] = decoded if isinstance(decoded, dict) else {}
    elif isinstance(raw_output, dict):
        parsed = dict(raw_output)
    else:
        parsed = {}

    raw_compacted = parsed.get("compacted_memory_md")
    compacted_norm = (
        normalize_markdown(raw_compacted)
        if isinstance(raw_compacted, str)
        else ""
    )
    has_memory_update = bool(compacted_norm)
    preview_memory_md = f"{compacted_norm}\n" if has_memory_update else existing_raw
    after_chars = len(preview_memory_md)

    upsert_facts = _normalize_facts(parsed.get("upsert_facts"))
    remove_fact_ids = _normalize_remove_fact_ids(
        parsed.get("remove_fact_ids"),
        valid_fact_ids,
    )

    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        if has_memory_update or upsert_facts or remove_fact_ids:
            summary = (
                f"Compacted MEMORY.md ({before_chars} -> {after_chars} chars), "
                f"upsert {len(upsert_facts)} facts, remove {len(remove_fact_ids)} facts."
            )
        else:
            summary = "No valid compaction changes returned; original memory retained."

    if dry_run:
        return CompactionResult(
            applied=False,
            dry_run=True,
            summary=summary,
            before_chars=before_chars,
            after_chars=after_chars,
            backup_path=None,
            updated_facts=upsert_facts,
            removed_fact_ids=remove_fact_ids,
            preview_memory_md=preview_memory_md,
        )

    backup_path: Path | None = None
    memory_written = False
    if has_memory_update:
        backup_path = _backup_memory_md(memory_path, home_path / "_versions")
        _atomic_write_text(memory_path, preview_memory_md)
        memory_written = True

    removed_ids: list[int] = []
    for fid in remove_fact_ids:
        if store.delete_fact(fid):
            removed_ids.append(fid)

    saved_facts: list[dict[str, Any]] = []
    for fact in upsert_facts:
        saved = store.upsert_fact(
            key=fact["key"],
            value=fact["value"],
            confidence=fact["confidence"],
            source_episode="compaction",
        )
        saved_facts.append(saved)

    applied = memory_written or bool(saved_facts) or bool(removed_ids)
    return CompactionResult(
        applied=applied,
        dry_run=False,
        summary=summary,
        before_chars=before_chars,
        after_chars=after_chars,
        backup_path=backup_path,
        updated_facts=saved_facts,
        removed_fact_ids=removed_ids,
        preview_memory_md=preview_memory_md,
    )


__all__ = [
    "CompactionResult",
    "ConsolidationResult",
    "compact_memory",
    "consolidate_episode",
]
