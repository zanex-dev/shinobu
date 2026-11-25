# -*- coding: utf-8 -*-
"""Neutral UI helpers and avatar resolution for the desktop companion kernel."""

from __future__ import annotations

from shinobu_core.ui.avatar import (
    ACTIVE_POINTER_FILENAME,
    ALLOWED_AVATAR_EXTENSIONS,
    AVATAR_ENV_VAR,
    AVATARS_DIR_NAME,
    MANIFEST_FILENAME,
    AvatarAsset,
    AvatarResolver,
    render_builtin_svg,
    safe_resolve_pack_file,
)

__all__ = [
    "ACTIVE_POINTER_FILENAME",
    "ALLOWED_AVATAR_EXTENSIONS",
    "AVATAR_ENV_VAR",
    "AVATARS_DIR_NAME",
    "MANIFEST_FILENAME",
    "AvatarAsset",
    "AvatarResolver",
    "render_builtin_svg",
    "safe_resolve_pack_file",
]
