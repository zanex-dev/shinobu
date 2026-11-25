# -*- coding: utf-8 -*-
"""
File-backed soul stack loader for Shinobu (`SOUL.md`, `STYLE.md`, `USER.md`, `MEMORY.md`).

Pure standard library (Python >= 3.10). Initializes neutral white-hat placeholder
templates on first run without overwriting existing files, and normalizes line
endings and trailing blank lines on read for stable L0 prefix hashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from shinobu_core.core.kernel import default_home

SOUL_DIR_NAME = "soul"

SOUL_FILENAMES: tuple[str, ...] = (
    "SOUL.md",
    "STYLE.md",
    "USER.md",
    "MEMORY.md",
)

DEFAULT_SOUL_TEMPLATES: Mapping[str, str] = {
    "SOUL.md": (
        "# Soul\n"
        "\n"
        "- Role: Lightweight, calm desktop companion and assistant.\n"
        "- Principles: Helpful, honest, concise, and respectful of user boundaries.\n"
        "- Scope: Keep core state portable, inspectable, and free of hidden side effects.\n"
    ),
    "STYLE.md": (
        "# Style\n"
        "\n"
        "- Tone: Warm, clear, and direct without unnecessary filler.\n"
        "- Format: Prefer short paragraphs and structured lists when helpful.\n"
        "- Pacing: Match the user's level of detail and ask before destructive actions.\n"
    ),
    "USER.md": (
        "# User Profile\n"
        "\n"
        "- Preferred Name: (unset)\n"
        "- Preferred Language: (unset)\n"
        "- Working Style: (customize your preferences here)\n"
    ),
    "MEMORY.md": (
        "# Long-Term Memory\n"
        "\n"
        "- Stable facts and curated preferences consolidated across episodes appear here.\n"
    ),
}


@dataclass(frozen=True)
class SoulBundle:
    """Immutable snapshot of the loaded file-based soul stack."""

    soul_md: str
    style_md: str
    user_md: str
    memory_md: str
    yesterday_summary: str = ""


def normalize_markdown(text: str) -> str:
    """
    Normalize markdown text for deterministic hashing:
    - Strip leading UTF-8 BOM if present.
    - Convert CRLF (`\\r\\n`) and CR (`\\r`) to LF (`\\n`).
    - Remove trailing whitespace-only lines so extra trailing blank lines do not drift hashes.
    """
    if not text:
        return ""
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _resolve_soul_dir(home: Path | str | None) -> Path:
    base = Path(home) if home is not None else default_home()
    return base / SOUL_DIR_NAME


def ensure_soul_files(home: Path | str | None = None) -> Path:
    """
    Ensure `<home>/soul/` exists with default white-hat template files
    (`SOUL.md`, `STYLE.md`, `USER.md`, `MEMORY.md`). Existing files are never overwritten.
    """
    soul_dir = _resolve_soul_dir(home)
    soul_dir.mkdir(parents=True, exist_ok=True)
    for filename in SOUL_FILENAMES:
        target = soul_dir / filename
        if not target.exists():
            target.write_text(
                DEFAULT_SOUL_TEMPLATES[filename],
                encoding="utf-8",
                newline="\n",
            )
    return soul_dir


def _read_soul_file(path: Path) -> str:
    return normalize_markdown(path.read_text(encoding="utf-8"))


def load_soul(home: Path | str | None = None, yesterday_summary: str = "") -> SoulBundle:
    """
    Initialize missing default soul templates under `<home>/soul/` and load the
    complete `SoulBundle` as UTF-8 with normalized line endings and trimmed trailing blanks.
    """
    soul_dir = ensure_soul_files(home)
    return SoulBundle(
        soul_md=_read_soul_file(soul_dir / "SOUL.md"),
        style_md=_read_soul_file(soul_dir / "STYLE.md"),
        user_md=_read_soul_file(soul_dir / "USER.md"),
        memory_md=_read_soul_file(soul_dir / "MEMORY.md"),
        yesterday_summary=normalize_markdown(yesterday_summary),
    )


__all__ = [
    "SOUL_DIR_NAME",
    "SOUL_FILENAMES",
    "DEFAULT_SOUL_TEMPLATES",
    "SoulBundle",
    "normalize_markdown",
    "ensure_soul_files",
    "load_soul",
]
