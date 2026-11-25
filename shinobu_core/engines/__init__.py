# -*- coding: utf-8 -*-
"""Built-in engine adapters and factory for Shinobu."""

from __future__ import annotations

from typing import Any

from shinobu_core.cloud.hybrid_engine import HybridRouterEngine
from shinobu_core.engines.agy_engines import AgyCliEngine, AgySdkEngine
from shinobu_core.engines.mock_engine import MockEngine
from shinobu_core.engines.openai_compat import OpenAICompatEngine
from shinobu_core.protocol.engine import EngineAdapter

ENGINE_REGISTRY: dict[str, type[Any]] = {
    "mock": MockEngine,
    "openai_compat": OpenAICompatEngine,
    "openai": OpenAICompatEngine,
    "ollama": OpenAICompatEngine,
    "agy_sdk": AgySdkEngine,
    "agy-sdk": AgySdkEngine,
    "agy_cli": AgyCliEngine,
    "agy-cli": AgyCliEngine,
    "hybrid": HybridRouterEngine,
}


def create_engine(name: str, **kwargs: Any) -> EngineAdapter:
    """Instantiate a built-in EngineAdapter by registry name."""
    key = name.strip().lower() if isinstance(name, str) else ""
    cls = ENGINE_REGISTRY.get(key)
    if cls is None:
        canonical = ("mock", "openai_compat", "agy_sdk", "agy_cli", "hybrid")
        raise ValueError(f"Unknown engine {name!r}; expected one of {canonical}")
    return cls(**kwargs)


__all__ = [
    "AgyCliEngine",
    "AgySdkEngine",
    "ENGINE_REGISTRY",
    "HybridRouterEngine",
    "MockEngine",
    "OpenAICompatEngine",
    "create_engine",
]
