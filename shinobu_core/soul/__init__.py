# -*- coding: utf-8 -*-
"""Soul stack loader and L0 prefix freezer package."""

from shinobu_core.soul.loader import (
    DEFAULT_SOUL_TEMPLATES,
    SOUL_DIR_NAME,
    SOUL_FILENAMES,
    SoulBundle,
    ensure_soul_files,
    load_soul,
    normalize_markdown,
)
from shinobu_core.soul.prefix_freezer import (
    L0_HEADER,
    FrozenPrefix,
    PrefixFreezer,
    assemble_l0_prefix,
    compute_sha256,
    freeze_prefix,
    verify_frozen,
)

__all__ = [
    "SOUL_DIR_NAME",
    "SOUL_FILENAMES",
    "DEFAULT_SOUL_TEMPLATES",
    "SoulBundle",
    "normalize_markdown",
    "ensure_soul_files",
    "load_soul",
    "L0_HEADER",
    "FrozenPrefix",
    "PrefixFreezer",
    "compute_sha256",
    "assemble_l0_prefix",
    "freeze_prefix",
    "verify_frozen",
]
