# -*- coding: utf-8 -*-
"""
Zero-dependency white-hat SVG companion sprite renderer and local avatar pack resolver.

Resolution precedence:
  1. Explicit `avatar_dir` argument (when provided to `AvatarResolver`)
  2. `SHINOBU_AVATAR_DIR` environment variable
  3. `<home>/avatars/active.json` pointing to a local avatar pack directory
  4. Built-in neutral geometric SVG sprite (`render_builtin_svg`)

State/mood fallback chain inside a pack manifest:
  `states[f"{state}_{mood}"]` -> `states[state]` -> `states["idle"]` -> built-in SVG

Security guarantees:
  - `manifest.json` asset entries must be relative paths strictly contained within
    the pack root (no `..` traversal, no absolute paths, no symlink escape).
  - Only `.svg`, `.png`, `.webp`, and `.gif` extensions are accepted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from shinobu_core.core.kernel import default_home
from shinobu_core.runtime.state_machine import VALID_MOODS, VALID_STATES

AVATAR_ENV_VAR = "SHINOBU_AVATAR_DIR"
AVATARS_DIR_NAME = "avatars"
ACTIVE_POINTER_FILENAME = "active.json"
MANIFEST_FILENAME = "manifest.json"
BUILTIN_PACK_NAME = "builtin_svg"
ALLOWED_AVATAR_EXTENSIONS: tuple[str, ...] = (".svg", ".png", ".webp", ".gif")


@dataclass(frozen=True)
class AvatarAsset:
    """Resolved avatar payload returned by :class:`AvatarResolver`."""

    kind: Literal["svg_inline", "file"]
    state: str
    mood: str
    svg_text: str = ""
    file_path: Path | None = None
    pack_name: str = BUILTIN_PACK_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "mood": self.mood,
            "svg_text": self.svg_text,
            "file_path": str(self.file_path) if self.file_path is not None else None,
            "pack_name": self.pack_name,
        }


_MOOD_PALETTES: dict[str, dict[str, str]] = {
    "calm": {
        "bg_start": "#E8F1FF",
        "bg_end": "#D5E4FA",
        "body_top": "#FFFFFF",
        "body_bottom": "#E4EEFF",
        "stroke": "#334E68",
        "accent": "#4F86F7",
        "accent_soft": "#AFCBFF",
        "cheek": "#F7B2BD",
    },
    "happy": {
        "bg_start": "#FFF6E5",
        "bg_end": "#FDE6C4",
        "body_top": "#FFFFFF",
        "body_bottom": "#FFF2DC",
        "stroke": "#4A3728",
        "accent": "#F59E0B",
        "accent_soft": "#FCD34D",
        "cheek": "#FDA4AF",
    },
    "smug": {
        "bg_start": "#F3EBFF",
        "bg_end": "#E2D4FA",
        "body_top": "#FFFFFF",
        "body_bottom": "#F0E7FF",
        "stroke": "#3B2C59",
        "accent": "#8B5CF6",
        "accent_soft": "#C4B5FD",
        "cheek": "#F9A8D4",
    },
    "sleepy": {
        "bg_start": "#E6EEF8",
        "bg_end": "#D3DFEE",
        "body_top": "#F8FAFC",
        "body_bottom": "#DCE6F2",
        "stroke": "#334155",
        "accent": "#64748B",
        "accent_soft": "#94A3B8",
        "cheek": "#E2B6C4",
    },
    "annoyed": {
        "bg_start": "#FFEAEA",
        "bg_end": "#FAD4D4",
        "body_top": "#FFFBFB",
        "body_bottom": "#FCE5E5",
        "stroke": "#5B2333",
        "accent": "#E11D48",
        "accent_soft": "#FDA4AF",
        "cheek": "#FB7185",
    },
}


def _normalize_state(state: str) -> str:
    candidate = str(state or "").strip().lower()
    return candidate if candidate in VALID_STATES else "idle"


def _normalize_mood(mood: str) -> str:
    candidate = str(mood or "").strip().lower()
    return candidate if candidate in VALID_MOODS else "calm"


def _face_markup(state: str, mood: str, stroke: str, accent: str) -> str:
    """Return SVG elements for eyes, brows, and mouth matching state & mood."""
    if state == "sleepy" or mood == "sleepy":
        eyes = (
            f'<path d="M77 109 Q86 116 95 109" fill="none" stroke="{stroke}" '
            'stroke-width="3.2" stroke-linecap="round"/>'
            f'<path d="M125 109 Q134 116 143 109" fill="none" stroke="{stroke}" '
            'stroke-width="3.2" stroke-linecap="round"/>'
        )
        mouth = (
            f'<ellipse cx="110" cy="127" rx="4.5" ry="3.5" fill="none" '
            f'stroke="{stroke}" stroke-width="2.6"/>'
        )
        return eyes + mouth

    if mood == "happy":
        eyes = (
            f'<path d="M77 110 Q86 100 95 110" fill="none" stroke="{stroke}" '
            'stroke-width="3.4" stroke-linecap="round"/>'
            f'<path d="M125 110 Q134 100 143 110" fill="none" stroke="{stroke}" '
            'stroke-width="3.4" stroke-linecap="round"/>'
        )
    elif mood == "smug":
        eyes = (
            f'<path d="M76 105 L96 108" fill="none" stroke="{stroke}" '
            'stroke-width="3" stroke-linecap="round"/>'
            f'<circle cx="87" cy="112" r="4.5" fill="{stroke}"/>'
            '<circle cx="89" cy="110" r="1.4" fill="#FFFFFF"/>'
            f'<path d="M124 108 L144 105" fill="none" stroke="{stroke}" '
            'stroke-width="3" stroke-linecap="round"/>'
            f'<circle cx="133" cy="112" r="4.5" fill="{stroke}"/>'
            '<circle cx="135" cy="110" r="1.4" fill="#FFFFFF"/>'
        )
    elif mood == "annoyed":
        eyes = (
            f'<path d="M76 100 L96 106" fill="none" stroke="{stroke}" '
            'stroke-width="3.2" stroke-linecap="round"/>'
            f'<circle cx="86" cy="112" r="4.8" fill="{stroke}"/>'
            f'<path d="M144 100 L124 106" fill="none" stroke="{stroke}" '
            'stroke-width="3.2" stroke-linecap="round"/>'
            f'<circle cx="134" cy="112" r="4.8" fill="{stroke}"/>'
        )
    else:  # calm
        eyes = (
            f'<circle cx="86" cy="109" r="5.5" fill="{stroke}"/>'
            '<circle cx="88" cy="107" r="1.8" fill="#FFFFFF"/>'
            f'<circle cx="134" cy="109" r="5.5" fill="{stroke}"/>'
            '<circle cx="136" cy="107" r="1.8" fill="#FFFFFF"/>'
        )

    if state == "speaking":
        mouth = (
            f'<path d="M101 124 Q110 135 119 124 Z" fill="{accent}" '
            f'stroke="{stroke}" stroke-width="2.4" stroke-linejoin="round"/>'
        )
    elif state == "thinking":
        mouth = (
            f'<path d="M104 127 Q110 124 116 127" fill="none" stroke="{stroke}" '
            'stroke-width="2.8" stroke-linecap="round"/>'
        )
    elif mood == "happy":
        mouth = (
            f'<path d="M99 123 Q110 134 121 123" fill="none" stroke="{stroke}" '
            'stroke-width="3" stroke-linecap="round"/>'
        )
    elif mood == "smug":
        mouth = (
            f'<path d="M101 126 Q112 130 121 122" fill="none" stroke="{stroke}" '
            'stroke-width="3" stroke-linecap="round"/>'
        )
    elif mood == "annoyed":
        mouth = (
            f'<path d="M100 129 Q110 122 120 129" fill="none" stroke="{stroke}" '
            'stroke-width="3" stroke-linecap="round"/>'
        )
    else:
        mouth = (
            f'<path d="M102 125 Q110 130 118 125" fill="none" stroke="{stroke}" '
            'stroke-width="2.8" stroke-linecap="round"/>'
        )
    return eyes + mouth


def _state_prop_markup(state: str, stroke: str, accent: str, accent_soft: str) -> str:
    """Return distinct state-specific vector props around the neutral companion."""
    if state == "donut":
        return (
            '<g id="prop-donut" transform="translate(154, 140)">'
            '<circle cx="0" cy="0" r="22" fill="#E6A96B" stroke="#7C4A21" stroke-width="2.5"/>'
            '<path d="M-20 -4 C-14 -19 14 -19 20 -4 C16 4 10 0 6 4 C2 8 -4 2 -10 5 C-15 7 -19 2 -20 -4 Z" '
            'fill="#F472B6" opacity="0.92"/>'
            '<circle cx="0" cy="0" r="7.5" fill="#FFF6E5" stroke="#7C4A21" stroke-width="2"/>'
            '<line x1="-10" y1="-11" x2="-6" y2="-8" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round"/>'
            '<line x1="6" y1="-12" x2="10" y2="-9" stroke="#FDE047" stroke-width="2.2" stroke-linecap="round"/>'
            '<line x1="-1" y1="-15" x2="2" y2="-11" stroke="#38BDF8" stroke-width="2.2" stroke-linecap="round"/>'
            "</g>"
        )
    if state == "sleepy":
        return (
            '<g id="prop-sleepy">'
            f'<path d="M156 58 A14 14 0 1 0 172 74 A10 10 0 1 1 156 58 Z" fill="{accent_soft}" opacity="0.9"/>'
            f'<text x="150" y="96" font-family="sans-serif" font-size="15" font-weight="700" fill="{stroke}" opacity="0.72">z</text>'
            f'<text x="163" y="84" font-family="sans-serif" font-size="20" font-weight="700" fill="{stroke}" opacity="0.86">Z</text>'
            "</g>"
        )
    if state == "thinking":
        return (
            '<g id="prop-thinking">'
            f'<circle cx="152" cy="84" r="4" fill="{accent_soft}" stroke="{stroke}" stroke-width="1.8"/>'
            f'<circle cx="163" cy="72" r="6" fill="{accent_soft}" stroke="{stroke}" stroke-width="1.8"/>'
            f'<rect x="164" y="42" width="38" height="24" rx="12" fill="#FFFFFF" stroke="{stroke}" stroke-width="2"/>'
            f'<circle cx="176" cy="54" r="2.4" fill="{accent}"/>'
            f'<circle cx="183" cy="54" r="2.4" fill="{accent}"/>'
            f'<circle cx="190" cy="54" r="2.4" fill="{accent}"/>'
            "</g>"
        )
    if state == "speaking":
        return (
            '<g id="prop-speaking">'
            f'<path d="M166 98 Q177 110 166 122" fill="none" stroke="{accent}" stroke-width="3.2" stroke-linecap="round"/>'
            f'<path d="M176 90 Q192 110 176 130" fill="none" stroke="{accent_soft}" stroke-width="3" stroke-linecap="round"/>'
            f'<path d="M156 50 H196 A8 8 0 0 1 204 58 V70 A8 8 0 0 1 196 78 H172 L162 86 V78 H156 A8 8 0 0 1 148 70 V58 A8 8 0 0 1 156 50 Z" '
            f'fill="#FFFFFF" stroke="{stroke}" stroke-width="2"/>'
            f'<line x1="160" y1="62" x2="192" y2="62" stroke="{accent}" stroke-width="2.4" stroke-linecap="round"/>'
            f'<line x1="160" y1="69" x2="182" y2="69" stroke="{accent_soft}" stroke-width="2.4" stroke-linecap="round"/>'
            "</g>"
        )
    # idle
    return (
        '<g id="prop-idle">'
        f'<path d="M168 62 L171 69 L178 72 L171 75 L168 82 L165 75 L158 72 L165 69 Z" fill="{accent}"/>'
        f'<circle cx="52" cy="76" r="4" fill="{accent_soft}" opacity="0.85"/>'
        f'<circle cx="176" cy="148" r="3.5" fill="{accent_soft}" opacity="0.75"/>'
        "</g>"
    )


def render_builtin_svg(state: str = "idle", mood: str = "calm") -> str:
    """Render a zero-dependency neutral geometric companion sprite as inline SVG."""
    norm_state = _normalize_state(state)
    norm_mood = _normalize_mood(mood)
    palette = _MOOD_PALETTES[norm_mood]
    face = _face_markup(norm_state, norm_mood, palette["stroke"], palette["accent"])
    prop = _state_prop_markup(
        norm_state,
        palette["stroke"],
        palette["accent"],
        palette["accent_soft"],
    )
    tilt = ' transform="rotate(-4 110 118)"' if norm_state == "thinking" else ""

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 220" '
        f'width="220" height="220" role="img" '
        f'aria-label="Desktop companion sprite ({norm_state}, {norm_mood})" '
        f'data-state="{norm_state}" data-mood="{norm_mood}">'
        "<defs>"
        f'<radialGradient id="haloGrad" cx="50%" cy="45%" r="55%">'
        f'<stop offset="0%" stop-color="{palette["bg_start"]}"/>'
        f'<stop offset="100%" stop-color="{palette["bg_end"]}"/>'
        "</radialGradient>"
        f'<linearGradient id="bodyGrad" x1="0%" y1="0%" x2="0%" y2="100%">'
        f'<stop offset="0%" stop-color="{palette["body_top"]}"/>'
        f'<stop offset="100%" stop-color="{palette["body_bottom"]}"/>'
        "</linearGradient>"
        "</defs>"
        '<circle cx="110" cy="112" r="90" fill="url(#haloGrad)"/>'
        f'<ellipse cx="110" cy="182" rx="46" ry="9" fill="{palette["stroke"]}" opacity="0.12"/>'
        f"<g{tilt}>"
        f'<path d="M110 40 L110 56" stroke="{palette["stroke"]}" stroke-width="3.2" stroke-linecap="round"/>'
        f'<circle cx="110" cy="34" r="7.5" fill="{palette["accent"]}" stroke="{palette["stroke"]}" stroke-width="2.4"/>'
        f'<ellipse cx="55" cy="112" rx="10" ry="18" fill="{palette["accent_soft"]}" stroke="{palette["stroke"]}" stroke-width="2.6"/>'
        f'<ellipse cx="165" cy="112" rx="10" ry="18" fill="{palette["accent_soft"]}" stroke="{palette["stroke"]}" stroke-width="2.6"/>'
        f'<rect x="58" y="58" width="104" height="106" rx="44" fill="url(#bodyGrad)" stroke="{palette["stroke"]}" stroke-width="3.2"/>'
        '<rect x="68" y="72" width="84" height="72" rx="30" fill="#FFFFFF" opacity="0.72"/>'
        f'<ellipse cx="76" cy="122" rx="7.5" ry="4.5" fill="{palette["cheek"]}" opacity="0.62"/>'
        f'<ellipse cx="144" cy="122" rx="7.5" ry="4.5" fill="{palette["cheek"]}" opacity="0.62"/>'
        f"{face}"
        f'<path d="M90 163 Q110 173 130 163" fill="none" stroke="{palette["accent"]}" stroke-width="3.5" stroke-linecap="round"/>'
        "</g>"
        f"{prop}"
        "</svg>"
    )


def safe_resolve_pack_file(pack_dir: Path, rel_path: str) -> Path | None:
    """Resolve a manifest-relative asset path strictly within `pack_dir`.

    Rejects empty strings, null bytes, absolute paths, `..` traversal segments,
    symlink escapes outside `pack_dir`, disallowed extensions, and missing files.
    """
    if not isinstance(rel_path, str):
        return None
    cleaned = rel_path.strip()
    if not cleaned or "\x00" in cleaned:
        return None
    normalized = cleaned.replace("\\", "/")
    rel = Path(normalized)
    if rel.is_absolute() or rel.drive or normalized.startswith("/"):
        return None
    if any(part in ("..", "") for part in rel.parts):
        return None
    if rel.suffix.lower() not in ALLOWED_AVATAR_EXTENSIONS:
        return None

    try:
        pack_root = pack_dir.resolve()
        candidate = (pack_root / rel).resolve()
        candidate.relative_to(pack_root)
    except (OSError, ValueError):
        return None

    if candidate == pack_root or not candidate.is_file():
        return None
    return candidate


def _load_manifest(pack_dir: Path) -> tuple[str, dict[str, str]] | None:
    manifest_path = pack_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    states_raw = raw.get("states")
    if not isinstance(states_raw, dict):
        return None
    states: dict[str, str] = {}
    for key, value in states_raw.items():
        if isinstance(key, str) and isinstance(value, str):
            states[key.strip().lower()] = value
    pack_name = str(raw.get("name") or pack_dir.name).strip() or pack_dir.name
    return pack_name, states


def _resolve_active_pack_dir(home: Path) -> Path | None:
    avatars_root = home / AVATARS_DIR_NAME
    active_file = avatars_root / ACTIVE_POINTER_FILENAME
    if not active_file.is_file():
        return None
    try:
        data = json.loads(active_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    # Prefer relative pack name under `<home>/avatars/<pack_name>`
    for field in ("active_pack", "pack", "name"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            rel = Path(value.strip().replace("\\", "/"))
            if not rel.is_absolute() and ".." not in rel.parts and len(rel.parts) == 1:
                candidate = avatars_root / rel
                if candidate.is_dir():
                    return candidate

    # Fallback: relative `manifest` pointer under `<home>/avatars/`
    manifest_ref = data.get("manifest")
    if isinstance(manifest_ref, str) and manifest_ref.strip():
        rel = Path(manifest_ref.strip().replace("\\", "/"))
        if not rel.is_absolute() and ".." not in rel.parts:
            try:
                root_resolved = avatars_root.resolve()
                cand_manifest = (root_resolved / rel).resolve()
                cand_manifest.relative_to(root_resolved)
                if cand_manifest.is_file():
                    return cand_manifest.parent
            except (OSError, ValueError):
                pass

    # Optional explicit local directory pointer (`pack_dir` or `path`)
    for field in ("pack_dir", "path"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            cand = Path(value.strip()).expanduser()
            if not cand.is_absolute():
                if ".." in cand.parts:
                    continue
                cand = avatars_root / cand
            if cand.is_dir():
                return cand
    return None


class AvatarResolver:
    """Resolve companion avatars with local pack override and built-in SVG fallback."""

    def __init__(
        self,
        home: Path | str | None = None,
        avatar_dir: Path | str | None = None,
    ) -> None:
        self.home: Path = Path(home).expanduser() if home is not None else default_home()
        self._explicit_avatar_dir: Path | None = (
            Path(avatar_dir).expanduser() if avatar_dir is not None and str(avatar_dir).strip() else None
        )

    def _candidate_pack_dirs(self) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = []
        if self._explicit_avatar_dir is not None:
            candidates.append(("explicit", self._explicit_avatar_dir))
        env_dir = os.environ.get(AVATAR_ENV_VAR, "").strip()
        if env_dir:
            candidates.append(("env", Path(env_dir).expanduser()))
        active_dir = _resolve_active_pack_dir(self.home)
        if active_dir is not None:
            candidates.append(("active_json", active_dir))
        return candidates

    def _select_pack(self) -> tuple[str, Path, str, dict[str, str]] | None:
        for source, pack_dir in self._candidate_pack_dirs():
            if not pack_dir.is_dir():
                continue
            loaded = _load_manifest(pack_dir)
            if loaded is not None:
                pack_name, states = loaded
                return source, pack_dir, pack_name, states
        return None

    def describe(self) -> dict[str, Any]:
        """Return diagnostic metadata about the currently active avatar source."""
        selected = self._select_pack()
        if selected is None:
            return {
                "source": "builtin",
                "pack_name": BUILTIN_PACK_NAME,
                "pack_dir": None,
                "mapped_keys": [],
            }
        source, pack_dir, pack_name, states = selected
        valid_keys = sorted(
            key
            for key, rel_path in states.items()
            if safe_resolve_pack_file(pack_dir, rel_path) is not None
        )
        return {
            "source": source,
            "pack_name": pack_name,
            "pack_dir": str(pack_dir),
            "mapped_keys": valid_keys,
        }

    def resolve(self, state: str = "idle", mood: str = "calm") -> AvatarAsset:
        """Resolve an avatar asset for `(state, mood)` following the fallback chain."""
        norm_state = _normalize_state(state)
        norm_mood = _normalize_mood(mood)

        selected = self._select_pack()
        if selected is not None:
            _, pack_dir, pack_name, states = selected
            lookup_keys = (f"{norm_state}_{norm_mood}", norm_state, "idle")
            for key in lookup_keys:
                rel_path = states.get(key)
                if not rel_path:
                    continue
                resolved_file = safe_resolve_pack_file(pack_dir, rel_path)
                if resolved_file is not None:
                    return AvatarAsset(
                        kind="file",
                        state=norm_state,
                        mood=norm_mood,
                        svg_text="",
                        file_path=resolved_file,
                        pack_name=pack_name,
                    )

        return AvatarAsset(
            kind="svg_inline",
            state=norm_state,
            mood=norm_mood,
            svg_text=render_builtin_svg(norm_state, norm_mood),
            file_path=None,
            pack_name=BUILTIN_PACK_NAME,
        )
