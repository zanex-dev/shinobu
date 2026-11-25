# -*- coding: utf-8 -*-
"""Hot-pluggable worker registry with heartbeat keepalive and TTL expiry."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from shinobu_core.protocol.events import Event, EventKind


def _normalize_capabilities(capabilities: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(capabilities, str):
        item = capabilities.strip()
        return (item,) if item else ()
    seen: set[str] = set()
    result: list[str] = []
    for raw in capabilities:
        item = str(raw).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


@dataclass
class WorkerRecord:
    """Metadata and liveness record for a hot-pluggable compute or edge worker."""

    worker_id: str
    role: str
    base_url: str
    capabilities: tuple[str, ...] = ()
    token: str = ""
    ttl_sec: float = 45.0
    last_seen: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("WorkerRecord.worker_id must be a non-empty string")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("WorkerRecord.role must be a non-empty string")
        self.worker_id = self.worker_id.strip()
        self.role = self.role.strip()
        self.base_url = str(self.base_url).strip().rstrip("/")
        self.capabilities = _normalize_capabilities(self.capabilities)
        self.token = str(self.token)
        self.ttl_sec = float(self.ttl_sec)
        if self.ttl_sec <= 0:
            raise ValueError("WorkerRecord.ttl_sec must be greater than 0")
        self.last_seen = float(self.last_seen)
        self.meta = dict(self.meta) if isinstance(self.meta, Mapping) else {}

    def has_capability(self, capability: str) -> bool:
        """Return True if this worker advertises the given capability."""
        return capability in self.capabilities

    def is_expired(self, now: float | None = None) -> bool:
        """Return True when the worker has exceeded its heartbeat TTL."""
        current = time.time() if now is None else float(now)
        return (current - self.last_seen) >= self.ttl_sec

    def to_dict(
        self,
        now: float | None = None,
        *,
        include_token: bool = False,
    ) -> dict[str, Any]:
        """Serialize worker status into a JSON-compatible dictionary."""
        current = time.time() if now is None else float(now)
        expired = self.is_expired(current)
        data: dict[str, Any] = {
            "worker_id": self.worker_id,
            "role": self.role,
            "base_url": self.base_url,
            "capabilities": list(self.capabilities),
            "has_token": bool(self.token),
            "ttl_sec": self.ttl_sec,
            "last_seen": self.last_seen,
            "age_sec": max(0.0, current - self.last_seen),
            "online": not expired,
            "expired": expired,
            "meta": dict(self.meta),
        }
        if include_token:
            data["token"] = self.token
        return data


class WorkerRegistry:
    """Thread-safe in-memory registry for dynamic edge/GPU workers."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock: Callable[[], float] = clock or time.time
        self._on_event = on_event
        self._workers: dict[str, WorkerRecord] = {}

    def _emit(self, action: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        event = Event(
            kind=EventKind.STATE_CHANGED,
            payload={"source": "worker_registry", "action": action, **payload},
            ts=self._clock(),
        )
        try:
            self._on_event(event)
        except Exception:
            # Event observer errors must never break registry operations.
            pass

    def register(self, record: WorkerRecord) -> WorkerRecord:
        """Register or replace a worker record and return a stored snapshot."""
        if not isinstance(record, WorkerRecord):
            raise TypeError("record must be a WorkerRecord instance")
        now = self._clock()
        last_seen = record.last_seen if record.last_seen > 0 else now
        stored = replace(record, last_seen=last_seen, meta=dict(record.meta))
        with self._lock:
            self._workers[stored.worker_id] = stored
            snapshot = replace(stored, meta=dict(stored.meta))
        self._emit("worker_registered", snapshot.to_dict(now))
        return snapshot

    def heartbeat(
        self,
        worker_id: str,
        meta: Mapping[str, Any] | None = None,
    ) -> bool:
        """Refresh a worker's heartbeat timestamp and optionally merge metadata."""
        now = self._clock()
        with self._lock:
            existing = self._workers.get(worker_id)
            if existing is None:
                return False
            merged_meta = dict(existing.meta)
            if meta is not None:
                merged_meta.update(dict(meta))
            updated = replace(existing, last_seen=now, meta=merged_meta)
            self._workers[worker_id] = updated
        return True

    def unregister(self, worker_id: str) -> bool:
        """Remove a worker from the registry. Returns True if it existed."""
        now = self._clock()
        with self._lock:
            removed = self._workers.pop(worker_id, None)
        if removed is None:
            return False
        self._emit("worker_unregistered", removed.to_dict(now))
        return True

    def prune_expired(self) -> list[str]:
        """Physically evict all expired workers and return their worker IDs."""
        now = self._clock()
        evicted: list[WorkerRecord] = []
        with self._lock:
            for worker_id in list(self._workers.keys()):
                rec = self._workers[worker_id]
                if rec.is_expired(now):
                    evicted.append(self._workers.pop(worker_id))
        for rec in evicted:
            self._emit("worker_expired", rec.to_dict(now))
        return [rec.worker_id for rec in evicted]

    def get(self, worker_id: str) -> WorkerRecord | None:
        """Fetch a snapshot of a worker record by ID regardless of TTL status."""
        with self._lock:
            rec = self._workers.get(worker_id)
            if rec is None:
                return None
            return replace(rec, meta=dict(rec.meta))

    def list_workers(self, include_expired: bool = False) -> list[dict[str, Any]]:
        """List registered workers as JSON-safe dicts (online only by default)."""
        now = self._clock()
        with self._lock:
            records = list(self._workers.values())
        items: list[dict[str, Any]] = []
        for rec in records:
            if not include_expired and rec.is_expired(now):
                continue
            items.append(rec.to_dict(now))
        items.sort(key=lambda item: (-float(item["last_seen"]), str(item["worker_id"])))
        return items

    def get_online(
        self,
        capability: str,
        role: str | None = None,
    ) -> WorkerRecord | None:
        """Select the freshest online worker matching capability and optional role."""
        cap = str(capability).strip()
        role_filter = role.strip() if isinstance(role, str) else None
        now = self._clock()
        with self._lock:
            candidates = [
                rec
                for rec in self._workers.values()
                if not rec.is_expired(now)
                and (not cap or rec.has_capability(cap))
                and (role_filter is None or rec.role == role_filter)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda rec: (-rec.last_seen, rec.worker_id))
            chosen = candidates[0]
            return replace(chosen, meta=dict(chosen.meta))


__all__ = [
    "WorkerRecord",
    "WorkerRegistry",
]
