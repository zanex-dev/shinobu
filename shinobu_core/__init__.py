# -*- coding: utf-8 -*-
"""Zero-dependency microkernel & protocol contracts for Shinobu."""

from __future__ import annotations

from shinobu_core.core.kernel import (
    DISABLE_PLUGINS_ENV,
    DISABLED_PLUGINS_ENV,
    HOME_ENV,
    PLUGIN_GROUP,
    HookBus,
    PluginLoadRecord,
    ShinobuKernel,
    ShinobuPlugin,
    default_home,
)
from shinobu_core.protocol.engine import (
    EngineAdapter,
    ToolSpec,
    TurnChunk,
    TurnRequest,
)
from shinobu_core.protocol.events import (
    VALID_EVENT_KINDS,
    Event,
    EventKind,
)

__version__ = "1.2.0"

__all__ = [
    "__version__",
    "DISABLE_PLUGINS_ENV",
    "DISABLED_PLUGINS_ENV",
    "EngineAdapter",
    "Event",
    "EventKind",
    "HOME_ENV",
    "HookBus",
    "PLUGIN_GROUP",
    "PluginLoadRecord",
    "ShinobuKernel",
    "ShinobuPlugin",
    "ToolSpec",
    "TurnChunk",
    "TurnRequest",
    "VALID_EVENT_KINDS",
    "default_home",
]
