# -*- coding: utf-8 -*-
"""Single-file SQLite (WAL + FTS5 trigram) memory store for Shinobu."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Characters that carry special syntax meaning in FTS5 MATCH queries or can
# disrupt trigram phrase parsing; queries containing these degrade safely to LIKE.
_FTS_UNSAFE_RE = re.compile(r'["\'*():^\-+\\/\[\]{};!?~@#$%&=<>,.|`]')

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    prefix_text TEXT NOT NULL,
    prefix_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TRIGGER IF NOT EXISTS episodes_immutable_prefix
BEFORE UPDATE OF prefix_text, prefix_sha256 ON episodes
WHEN old.prefix_text != new.prefix_text OR old.prefix_sha256 != new.prefix_sha256
BEGIN
    SELECT RAISE(ABORT, 'episodes.prefix_text and prefix_sha256 are immutable once written');
END;

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL REFERENCES episodes(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_episode_id ON turns(episode_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    content,
    content='turns',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    source_episode TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    key,
    value,
    content='facts',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, key, value) VALUES('delete', old.id, old.key, old.value);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, key, value) VALUES('delete', old.id, old.key, old.value);
    INSERT INTO facts_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;

CREATE TABLE IF NOT EXISTS timemap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_at TEXT NOT NULL,
    label TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    done INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_timemap_due ON timemap(done, trigger_at);
CREATE INDEX IF NOT EXISTS idx_episodes_status_closed ON episodes(status, closed_at);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_iso_dt(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_due(trigger_at: str, now_iso: str) -> bool:
    dt_trigger = _parse_iso_dt(trigger_at)
    dt_now = _parse_iso_dt(now_iso)
    if dt_trigger is not None and dt_now is not None:
        if (dt_trigger.tzinfo is not None) == (dt_now.tzinfo is not None):
            return dt_trigger <= dt_now
    return trigger_at <= now_iso


def _can_use_fts_trigram(query: str) -> bool:
    if len(query) < 3:
        return False
    if _FTS_UNSAFE_RE.search(query):
        return False
    tokens = query.split()
    return bool(tokens) and all(len(tok) >= 3 for tok in tokens)


class MemoryStore:
    """Zero-vector-DB single-file SQLite memory store with WAL and FTS5 trigram."""

    def __init__(self, db_path: Path | str) -> None:
        raw_path = str(db_path)
        self._is_memory = raw_path == ":memory:"
        self.db_path: Path | str = raw_path if self._is_memory else Path(db_path)
        if not self._is_memory:
            assert isinstance(self.db_path, Path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(raw_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            mode_row = self._conn.execute("PRAGMA journal_mode=WAL;").fetchone()
            self.journal_mode: str = str(mode_row[0]).lower() if mode_row else ""
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Episodes (immutable L0 prefix per active episode)
    # ------------------------------------------------------------------

    def get_episode(self, id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, prefix_text, prefix_sha256, status, summary, created_at, closed_at "
                "FROM episodes WHERE id = ?",
                (id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def create_or_get_active_episode(
        self,
        id: str,
        prefix_text: str,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        episode_id = id.strip()
        if not episode_id:
            raise ValueError("episode id must be a non-empty string")
        digest = (
            sha256
            if sha256
            else hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()
        )

        with self._lock:
            existing = self._conn.execute(
                "SELECT id, prefix_text, prefix_sha256, status, summary, created_at, closed_at "
                "FROM episodes WHERE id = ?",
                (episode_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] != "active":
                    raise ValueError(f"Episode {episode_id!r} is already {existing['status']}")
                # Never overwrite prefix_text or prefix_sha256 of an existing active episode.
                return dict(existing)

            created_at = _utc_now_iso()
            with self._conn:
                self._conn.execute(
                    "INSERT INTO episodes "
                    "(id, prefix_text, prefix_sha256, status, summary, created_at, closed_at) "
                    "VALUES (?, ?, ?, 'active', '', ?, NULL)",
                    (episode_id, prefix_text, digest, created_at),
                )
            return {
                "id": episode_id,
                "prefix_text": prefix_text,
                "prefix_sha256": digest,
                "status": "active",
                "summary": "",
                "created_at": created_at,
                "closed_at": None,
            }

    def close_episode(self, id: str, summary: str) -> dict[str, Any]:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM episodes WHERE id = ?",
                (id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"Episode not found: {id}")

            closed_at = _utc_now_iso()
            with self._conn:
                self._conn.execute(
                    "UPDATE episodes SET status = 'closed', summary = ?, closed_at = ? "
                    "WHERE id = ?",
                    (summary, closed_at, id),
                )
            row = self._conn.execute(
                "SELECT id, prefix_text, prefix_sha256, status, summary, created_at, closed_at "
                "FROM episodes WHERE id = ?",
                (id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def latest_closed_summary(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM episodes "
                "WHERE status = 'closed' "
                "ORDER BY closed_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            return str(row["summary"]) if row is not None else ""

    def get_active_episode(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, prefix_text, prefix_sha256, status, summary, created_at, closed_at "
                "FROM episodes WHERE status = 'active' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def append_turn(self, episode_id: str, role: str, content: str) -> int:
        role_clean = role.strip()
        if not role_clean:
            raise ValueError("turn role must be a non-empty string")
        created_at = _utc_now_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO turns (episode_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (episode_id, role_clean, content, created_at),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def get_turns(self, episode_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, episode_id, role, content, created_at FROM ("
                "  SELECT id, episode_id, role, content, created_at "
                "  FROM turns WHERE episode_id = ? "
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (episode_id, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_turns(self, episode_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM turns WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            return int(row["cnt"]) if row is not None else 0

    def list_episodes(self, limit: int = 30) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodes ORDER BY rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Facts & Search (FTS5 trigram + safe LIKE fallback)
    # ------------------------------------------------------------------

    def upsert_fact(
        self,
        key: str,
        value: str,
        confidence: float = 0.8,
        source_episode: str = "",
    ) -> dict[str, Any]:
        key_clean = key.strip()
        if not key_clean:
            raise ValueError("fact key must be a non-empty string")
        conf = max(0.0, min(1.0, float(confidence)))
        updated_at = _utc_now_iso()

        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO facts (key, value, confidence, source_episode, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "  value = excluded.value, "
                    "  confidence = excluded.confidence, "
                    "  source_episode = CASE "
                    "    WHEN excluded.source_episode != '' THEN excluded.source_episode "
                    "    ELSE facts.source_episode "
                    "  END, "
                    "  updated_at = excluded.updated_at",
                    (key_clean, value, conf, source_episode, updated_at),
                )
            row = self._conn.execute(
                "SELECT id, key, value, confidence, source_episode, updated_at "
                "FROM facts WHERE key = ?",
                (key_clean,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def list_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, key, value, confidence, source_episode, updated_at "
                "FROM facts ORDER BY updated_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_fact(self, fact_id: int) -> bool:
        fid = int(fact_id)
        if fid <= 0:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
            self._conn.commit()
            return cur.rowcount > 0

    def search(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        q = query.strip()
        if not q or limit <= 0:
            return []
        max_items = int(limit)

        with self._lock:
            if _can_use_fts_trigram(q):
                try:
                    return self._search_fts(q, max_items)
                except sqlite3.OperationalError:
                    pass
            return self._search_like(q, max_items)

    def _search_fts(self, query: str, limit: int) -> list[dict[str, Any]]:
        match_expr = " ".join(f'"{tok}"' for tok in query.split())
        fact_rows = self._conn.execute(
            "SELECT f.id, f.key, f.value, f.confidence, f.source_episode, f.updated_at, "
            "       bm25(facts_fts) AS rank "
            "FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? "
            "ORDER BY rank ASC, f.updated_at DESC, f.id DESC "
            "LIMIT ?",
            (match_expr, limit),
        ).fetchall()

        turn_rows = self._conn.execute(
            "SELECT t.id, t.episode_id, t.role, t.content, t.created_at, "
            "       bm25(turns_fts) AS rank "
            "FROM turns_fts "
            "JOIN turns t ON t.id = turns_fts.rowid "
            "WHERE turns_fts MATCH ? "
            "ORDER BY rank ASC, t.created_at DESC, t.id DESC "
            "LIMIT ?",
            (match_expr, limit),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in fact_rows:
            results.append(
                {
                    "source": "fact",
                    "id": int(row["id"]),
                    "key": row["key"],
                    "value": row["value"],
                    "content": f"{row['key']}: {row['value']}",
                    "confidence": float(row["confidence"]),
                    "episode_id": row["source_episode"],
                    "timestamp": row["updated_at"],
                    "score": float(row["rank"]),
                    "match_mode": "fts5",
                }
            )
        for row in turn_rows:
            results.append(
                {
                    "source": "turn",
                    "id": int(row["id"]),
                    "episode_id": row["episode_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["created_at"],
                    "score": float(row["rank"]),
                    "match_mode": "fts5",
                }
            )
        return results[:limit]

    def _search_like(self, query: str, limit: int) -> list[dict[str, Any]]:
        tokens = query.split()
        patterns = [f"%{_escape_like(tok)}%" for tok in tokens]

        fact_where = " AND ".join(
            "(key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')" for _ in patterns
        )
        fact_params: list[Any] = []
        for pat in patterns:
            fact_params.extend((pat, pat))
        fact_params.append(limit)

        fact_rows = self._conn.execute(
            "SELECT id, key, value, confidence, source_episode, updated_at "
            f"FROM facts WHERE {fact_where} "
            "ORDER BY confidence DESC, updated_at DESC, id DESC "
            "LIMIT ?",
            fact_params,
        ).fetchall()

        turn_where = " AND ".join("content LIKE ? ESCAPE '\\'" for _ in patterns)
        turn_params: list[Any] = [*patterns, limit]

        turn_rows = self._conn.execute(
            "SELECT id, episode_id, role, content, created_at "
            f"FROM turns WHERE {turn_where} "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?",
            turn_params,
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in fact_rows:
            results.append(
                {
                    "source": "fact",
                    "id": int(row["id"]),
                    "key": row["key"],
                    "value": row["value"],
                    "content": f"{row['key']}: {row['value']}",
                    "confidence": float(row["confidence"]),
                    "episode_id": row["source_episode"],
                    "timestamp": row["updated_at"],
                    "match_mode": "like",
                }
            )
        for row in turn_rows:
            results.append(
                {
                    "source": "turn",
                    "id": int(row["id"]),
                    "episode_id": row["episode_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["created_at"],
                    "match_mode": "like",
                }
            )
        return results[:limit]

    # ------------------------------------------------------------------
    # Timemap schedules
    # ------------------------------------------------------------------

    def add_schedule(
        self,
        trigger_at: str,
        label: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        trigger_clean = trigger_at.strip()
        label_clean = label.strip()
        if not trigger_clean:
            raise ValueError("trigger_at must be a non-empty ISO timestamp string")
        if not label_clean:
            raise ValueError("schedule label must be a non-empty string")
        payload_json = json.dumps(payload if payload is not None else {}, ensure_ascii=False)

        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO timemap (trigger_at, label, payload_json, done) "
                "VALUES (?, ?, ?, 0)",
                (trigger_clean, label_clean, payload_json),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def pop_due_schedules(self, now_iso: str) -> list[dict[str, Any]]:
        now_clean = now_iso.strip()
        if not now_clean:
            raise ValueError("now_iso must be a non-empty ISO timestamp string")

        with self._lock, self._conn:
            pending = self._conn.execute(
                "SELECT id, trigger_at, label, payload_json, done "
                "FROM timemap WHERE done = 0 "
                "ORDER BY trigger_at ASC, id ASC"
            ).fetchall()

            due_rows = [row for row in pending if _is_due(str(row["trigger_at"]), now_clean)]
            if not due_rows:
                return []

            due_ids = [int(row["id"]) for row in due_rows]
            placeholders = ",".join("?" for _ in due_ids)
            self._conn.execute(
                f"UPDATE timemap SET done = 1 WHERE id IN ({placeholders})",
                due_ids,
            )

            return [
                {
                    "id": int(row["id"]),
                    "trigger_at": row["trigger_at"],
                    "label": row["label"],
                    "payload": json.loads(row["payload_json"]),
                    "done": True,
                }
                for row in due_rows
            ]

    def list_schedules(
        self,
        include_done: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            if include_done:
                rows = self._conn.execute(
                    "SELECT id, trigger_at, label, payload_json, done "
                    "FROM timemap ORDER BY done ASC, trigger_at ASC, id ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, trigger_at, label, payload_json, done "
                    "FROM timemap WHERE done = 0 "
                    "ORDER BY trigger_at ASC, id ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "trigger_at": row["trigger_at"],
                    "label": row["label"],
                    "payload": json.loads(row["payload_json"]),
                    "done": bool(row["done"]),
                }
                for row in rows
            ]

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM timemap WHERE id = ?",
                (int(schedule_id),),
            )
            return cur.rowcount > 0


__all__ = ["MemoryStore"]
