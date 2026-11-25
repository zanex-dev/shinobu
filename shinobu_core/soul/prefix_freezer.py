# -*- coding: utf-8 -*-
"""
Per-episode L0 static system prefix freezer for Shinobu.

Assembles a deterministic, LF-normalized L0 prefix from a `SoulBundle`, signs it
with SHA-256, and locks it per episode inside `PrefixFreezer` so mid-episode
edits on disk never mutate the active prompt prefix and break Prefix KV Cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import threading

from shinobu_core.soul.loader import SoulBundle, load_soul, normalize_markdown

L0_HEADER = "<!-- SHINOBU:L0_STATIC_PREFIX -->"


@dataclass(frozen=True)
class FrozenPrefix:
    """Immutable L0 static prefix snapshot locked to a single episode."""

    episode_id: str
    text: str
    sha256: str
    created_at: str


def compute_sha256(text: str) -> str:
    """Return lowercase hexadecimal SHA-256 digest of UTF-8 encoded `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assemble_l0_prefix(bundle: SoulBundle) -> str:
    """
    Deterministically assemble the L0 static system prefix from `bundle`.

    All sections are LF-normalized (`\\n`) with trailing blank lines trimmed.
    The `YESTERDAY` section is omitted when empty so equivalent empty inputs
    always hash identically.
    """
    sections: list[tuple[str, str]] = [
        ("SOUL", normalize_markdown(bundle.soul_md)),
        ("STYLE", normalize_markdown(bundle.style_md)),
        ("USER", normalize_markdown(bundle.user_md)),
        ("MEMORY", normalize_markdown(bundle.memory_md)),
    ]
    yesterday = normalize_markdown(bundle.yesterday_summary)
    if yesterday:
        sections.append(("YESTERDAY", yesterday))

    blocks: list[str] = [L0_HEADER]
    for title, body in sections:
        if body:
            blocks.append(f"## {title}\n{body}")
        else:
            blocks.append(f"## {title}")
    return "\n\n".join(blocks) + "\n"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def freeze_prefix(episode_id: str, bundle: SoulBundle) -> FrozenPrefix:
    """
    Build a `FrozenPrefix` for `episode_id` by normalizing newlines to `\\n`,
    deterministically assembling L0, and computing its SHA-256 digest.
    """
    ep = str(episode_id or "").strip()
    if not ep:
        raise ValueError("episode_id must be a non-empty string")
    text = assemble_l0_prefix(bundle)
    digest = compute_sha256(text)
    return FrozenPrefix(
        episode_id=ep,
        text=text,
        sha256=digest,
        created_at=_utc_now_iso(),
    )


def verify_frozen(prefix: FrozenPrefix) -> bool:
    """Verify that `prefix.text`'s UTF-8 SHA-256 matches `prefix.sha256`."""
    if not isinstance(prefix, FrozenPrefix):
        return False
    if not isinstance(prefix.text, str) or not isinstance(prefix.sha256, str):
        return False
    if not prefix.sha256:
        return False
    actual = compute_sha256(prefix.text)
    return hmac.compare_digest(actual, prefix.sha256.lower())


class PrefixFreezer:
    """
    Thread-safe episode registry that locks each episode's L0 `FrozenPrefix`.

    Once an `episode_id` is frozen, repeated freeze calls for the same episode
    return the originally locked `FrozenPrefix` regardless of subsequent disk
    edits or updated `SoulBundle` instances, preserving Prefix KV Cache stability.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._frozen: dict[str, FrozenPrefix] = {}

    def freeze(self, episode_id: str, bundle: SoulBundle) -> FrozenPrefix:
        """Freeze `bundle` for `episode_id`, or return the existing locked prefix."""
        ep = str(episode_id or "").strip()
        if not ep:
            raise ValueError("episode_id must be a non-empty string")
        with self._lock:
            existing = self._frozen.get(ep)
            if existing is not None:
                return existing
            frozen = freeze_prefix(ep, bundle)
            self._frozen[ep] = frozen
            return frozen

    def freeze_from_home(
        self,
        episode_id: str,
        home: Path | str | None = None,
        yesterday_summary: str = "",
    ) -> FrozenPrefix:
        """
        Return the locked prefix for `episode_id`, loading from `<home>/soul/`
        only if `episode_id` has not yet been frozen.
        """
        ep = str(episode_id or "").strip()
        if not ep:
            raise ValueError("episode_id must be a non-empty string")
        with self._lock:
            existing = self._frozen.get(ep)
            if existing is not None:
                return existing
            bundle = load_soul(home, yesterday_summary=yesterday_summary)
            frozen = freeze_prefix(ep, bundle)
            self._frozen[ep] = frozen
            return frozen

    def get(self, episode_id: str) -> FrozenPrefix | None:
        """Return the frozen prefix for `episode_id`, or `None` if not frozen."""
        ep = str(episode_id or "").strip()
        with self._lock:
            return self._frozen.get(ep)

    def is_frozen(self, episode_id: str) -> bool:
        """Return True if `episode_id` currently has a frozen prefix."""
        ep = str(episode_id or "").strip()
        with self._lock:
            return ep in self._frozen

    def verify(self, episode_id: str) -> bool:
        """Verify the locked prefix for `episode_id`; returns False if absent."""
        frozen = self.get(episode_id)
        if frozen is None:
            return False
        return verify_frozen(frozen)

    def release(self, episode_id: str) -> FrozenPrefix | None:
        """Remove and return the frozen prefix for `episode_id` (e.g. on episode close)."""
        ep = str(episode_id or "").strip()
        with self._lock:
            return self._frozen.pop(ep, None)

    def episodes(self) -> tuple[str, ...]:
        """Return currently tracked episode IDs in insertion order."""
        with self._lock:
            return tuple(self._frozen.keys())


__all__ = [
    "L0_HEADER",
    "FrozenPrefix",
    "PrefixFreezer",
    "compute_sha256",
    "assemble_l0_prefix",
    "freeze_prefix",
    "verify_frozen",
]
