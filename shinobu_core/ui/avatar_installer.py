# -*- coding: utf-8 -*-
"""
Non-destructive local avatar pack installer for `shinobu-core`.

Scans a user-supplied local `--source` directory (or an existing local pack
directory under `<home>/avatars/<name>/`) for state/mood differential sprites,
copies assets into `<home>/avatars/<name>/` with non-destructive `_versions/`
backups (never storing user media inside any code repository), generates
`<home>/avatars/<name>/manifest.json`, and updates `<home>/avatars/active.json`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shinobu_core.core.kernel import default_home
from shinobu_core.runtime.state_machine import VALID_MOODS, VALID_STATES
from shinobu_core.ui.avatar import (
    ACTIVE_POINTER_FILENAME,
    ALLOWED_AVATAR_EXTENSIONS,
    AVATARS_DIR_NAME,
    MANIFEST_FILENAME,
)

VERSIONS_DIR_NAME = "_versions"
DEFAULT_AVATAR_PACK_NAME = "custom"

_SAFE_PACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EXT_PRIORITY: dict[str, int] = {
    ext: idx for idx, ext in enumerate(ALLOWED_AVATAR_EXTENSIONS)
}

VALID_AVATAR_KEYS: frozenset[str] = frozenset(
    list(VALID_STATES)
    + [f"{state}_{mood}" for state in VALID_STATES for mood in VALID_MOODS]
)

__all__ = [
    "DEFAULT_AVATAR_PACK_NAME",
    "VALID_AVATAR_KEYS",
    "VERSIONS_DIR_NAME",
    "apply_avatar_pack",
]


def _validate_pack_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        cleaned = DEFAULT_AVATAR_PACK_NAME
    if not _SAFE_PACK_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid avatar pack name {name!r}: only [A-Za-z0-9_-] allowed (max 64 chars)."
        )
    return cleaned


def _normalize_candidate_key(stem: str) -> str | None:
    normalized = re.sub(r"[\s-]+", "_", stem.strip().lower())
    return normalized if normalized in VALID_AVATAR_KEYS else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_file_non_destructive(
    target: Path,
    versions_dir: Path,
    backup_prefix: str,
) -> Path:
    """Copy `target` into `<home>/_versions/<backup_prefix>_vN<suffix>`."""
    versions_dir.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix
    version = 1
    while True:
        candidate = versions_dir / f"{backup_prefix}_v{version}{suffix}"
        if not candidate.exists():
            shutil.copy2(target, candidate)
            return candidate
        version += 1


def _write_json_non_destructive(
    target: Path,
    payload: dict[str, Any],
    versions_dir: Path,
    backup_prefix: str,
) -> tuple[bool, Path | None]:
    """Write formatted JSON, backing up any differing existing file first."""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    backup_path: Path | None = None
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if existing == text:
            return False, None
        backup_path = _backup_file_non_destructive(target, versions_dir, backup_prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return True, backup_path


def _scan_source_directory(source_dir: Path) -> tuple[dict[str, Path], list[str]]:
    """Scan `source_dir` (and optional immediate `assets/` subdir) for state sprites.

    Returns `(matched_by_key, ignored_filenames)`. Never recurses arbitrarily.
    """
    scan_dirs: list[Path] = [source_dir]
    assets_sub = source_dir / "assets"
    if assets_sub.is_dir():
        scan_dirs.append(assets_sub)

    matched: dict[str, Path] = {}
    ignored: list[str] = []

    for directory in scan_dirs:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.lower() in {MANIFEST_FILENAME, ACTIVE_POINTER_FILENAME}:
                continue
            ext = entry.suffix.lower()
            if ext not in _EXT_PRIORITY:
                ignored.append(entry.name)
                continue
            key = _normalize_candidate_key(entry.stem)
            if key is None:
                ignored.append(entry.name)
                continue
            existing = matched.get(key)
            if existing is None or _EXT_PRIORITY[ext] < _EXT_PRIORITY[existing.suffix.lower()]:
                matched[key] = entry

    return matched, sorted(set(ignored))


def apply_avatar_pack(
    source: Path | str | None = None,
    *,
    name: str = DEFAULT_AVATAR_PACK_NAME,
    home: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Scan local avatar sprites and install/activate `<home>/avatars/<name>/manifest.json`.

    Rules:
      - Never deletes or overwrites differing files without first backing them up
        under `<home>/_versions/`.
      - Preserves existing target assets unless `force=True`.
      - Writes strictly relative paths inside `manifest.json` (compatible with
        `AvatarResolver` traversal protection).
    """
    safe_name = _validate_pack_name(name)
    resolved_home = Path(home).expanduser() if home is not None else default_home()
    avatars_root = resolved_home / AVATARS_DIR_NAME
    pack_dir = avatars_root / safe_name
    versions_dir = resolved_home / VERSIONS_DIR_NAME

    if source is not None and str(source).strip():
        source_dir = Path(source).expanduser()
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Avatar source directory not found: {source_dir}")
    else:
        source_dir = pack_dir

    pack_dir.mkdir(parents=True, exist_ok=True)

    same_dir = source_dir.resolve() == pack_dir.resolve()
    matched_files, ignored_files = (
        _scan_source_directory(source_dir) if source_dir.is_dir() else ({}, [])
    )

    copied: list[str] = []
    unchanged: list[str] = []
    protected: list[str] = []
    backed_up: list[str] = []
    states_map: dict[str, str] = {}

    pack_root_resolved = pack_dir.resolve()

    for key in sorted(matched_files):
        src_file = matched_files[key]
        ext = src_file.suffix.lower()

        if same_dir:
            rel_posix = src_file.resolve().relative_to(pack_root_resolved).as_posix()
            states_map[key] = rel_posix
            unchanged.append(rel_posix)
            continue

        target_name = f"{key}{ext}"
        target_file = pack_dir / target_name
        if target_file.is_file():
            if _file_sha256(src_file) == _file_sha256(target_file):
                unchanged.append(target_name)
            elif force:
                backup = _backup_file_non_destructive(
                    target_file,
                    versions_dir,
                    f"avatars_{safe_name}_{key}",
                )
                backed_up.append(str(backup))
                shutil.copy2(src_file, target_file)
                copied.append(target_name)
            else:
                protected.append(target_name)
        else:
            shutil.copy2(src_file, target_file)
            copied.append(target_name)

        states_map[key] = target_name

    # Preserve already-installed valid assets in `pack_dir` that were not in `source_dir`
    if not same_dir:
        existing_in_pack, _ = _scan_source_directory(pack_dir)
        for key, existing_file in sorted(existing_in_pack.items()):
            if key not in states_map:
                rel_posix = existing_file.resolve().relative_to(pack_root_resolved).as_posix()
                states_map[key] = rel_posix

    manifest_path = pack_dir / MANIFEST_FILENAME
    sorted_states = dict(sorted(states_map.items()))
    existing_manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            loaded_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded_raw, dict):
                existing_manifest = loaded_raw
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing_manifest = None

    manifest_unchanged = (
        existing_manifest is not None
        and existing_manifest.get("schema_version") == 1
        and existing_manifest.get("name") == safe_name
        and existing_manifest.get("states") == sorted_states
    )
    if not manifest_unchanged:
        manifest_payload: dict[str, Any] = {
            "schema_version": 1,
            "name": safe_name,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "states": sorted_states,
        }
        _, manifest_backup = _write_json_non_destructive(
            manifest_path,
            manifest_payload,
            versions_dir,
            f"avatars_{safe_name}_manifest",
        )
        if manifest_backup is not None:
            backed_up.append(str(manifest_backup))

    active_path = avatars_root / ACTIVE_POINTER_FILENAME
    activated = False
    if states_map or force:
        active_payload: dict[str, Any] = {
            "schema_version": 1,
            "active_pack": safe_name,
            "manifest": f"{safe_name}/{MANIFEST_FILENAME}",
        }
        _, active_backup = _write_json_non_destructive(
            active_path,
            active_payload,
            versions_dir,
            "avatars_active",
        )
        if active_backup is not None:
            backed_up.append(str(active_backup))
        activated = True

    return {
        "home": str(resolved_home),
        "pack_name": safe_name,
        "pack_dir": str(pack_dir),
        "source_dir": str(source_dir) if source is not None else None,
        "manifest_path": str(manifest_path),
        "active_path": str(active_path),
        "activated": activated,
        "states": dict(sorted(states_map.items())),
        "missing_states": [state for state in VALID_STATES if state not in states_map],
        "copied": copied,
        "unchanged": unchanged,
        "protected": protected,
        "backed_up": backed_up,
        "ignored_files": ignored_files,
    }
