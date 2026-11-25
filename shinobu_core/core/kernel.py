# -*- coding: utf-8 -*-
"""
Microkernel plugin registry & fault-isolated hook bus for Shinobu.

Pure standard library (Python >= 3.10). Discovers optional extensions via
`importlib.metadata.entry_points` under the `shinobu.plugins` group unless
disabled with `SHINOBU_DISABLE_PLUGINS=1|true` or selectively excluded via
`SHINOBU_DISABLED_PLUGINS`.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import inspect
import itertools
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Iterable, Mapping, Protocol, Tuple, runtime_checkable

from shinobu_core.protocol.engine import EngineAdapter, ToolSpec

logger = logging.getLogger("shinobu_core.core.kernel")

PLUGIN_GROUP = "shinobu.plugins"
PLUGIN_ENTRYPOINT_GROUP = PLUGIN_GROUP
DISABLE_PLUGINS_ENV = "SHINOBU_DISABLE_PLUGINS"
DISABLED_PLUGINS_ENV = "SHINOBU_DISABLED_PLUGINS"
HOME_ENV = "SHINOBU_HOME"

_PLUGIN_HOOK_METHODS: Tuple[str, ...] = (
    "register_engines",
    "register_tools",
    "register_sensors",
    "register_cli",
    "register_hooks",
)


def default_home() -> Path:
    """Return the Shinobu runtime home directory (`$SHINOBU_HOME` or `~/.shinobu`)."""
    raw = os.environ.get(HOME_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".shinobu"


def plugins_disabled() -> bool:
    """Return True when external plugin discovery is disabled via environment variable."""
    raw = os.environ.get(DISABLE_PLUGINS_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def disabled_plugin_names() -> frozenset[str]:
    """Return lower-cased plugin names disabled via `SHINOBU_DISABLED_PLUGINS`."""
    raw = os.environ.get(DISABLED_PLUGINS_ENV, "")
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_plugin_disabled(name: str) -> bool:
    """Return True when `name` is disabled globally or listed in `SHINOBU_DISABLED_PLUGINS`."""
    if plugins_disabled():
        return True
    norm = str(name or "").strip().lower()
    if not norm:
        return False
    return norm in disabled_plugin_names()


@runtime_checkable
class ShinobuPlugin(Protocol):
    """
    Neutral extension contract for Shinobu plugins.

    Plugins may implement any subset of the optional registration hooks below;
    the kernel inspects each hook dynamically and isolates failures per plugin.
    """

    def register_engines(self, kernel: "ShinobuKernel") -> Any: ...

    def register_tools(self, kernel: "ShinobuKernel") -> Any: ...

    def register_sensors(self, kernel: "ShinobuKernel") -> Any: ...

    def register_cli(self, subparsers: Any) -> Any: ...

    def register_hooks(self, bus: "HookBus") -> Any: ...


@dataclass(frozen=True, slots=True)
class PluginLoadRecord:
    """Immutable audit record describing a plugin discovery/load attempt."""

    name: str
    status: str  # "loaded" | "disabled" | "failed"
    error: str | None = None
    source: str = "entry_point"


class HookBus:
    """
    Thread-safe, fault-isolated publish/subscribe event bus.

    A failing listener is logged and skipped without blocking subsequent
    listeners or propagating exceptions into the core host flow.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._handlers: dict[str, list[tuple[int, int, Callable[..., Any]]]] = {}

    def on(
        self,
        event: str,
        handler: Callable[..., Any],
        *,
        priority: int = 100,
    ) -> Callable[[], None]:
        """Subscribe `handler` to `event` and return an idempotent unsubscribe callback."""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("HookBus event name must be a non-empty string")
        if not callable(handler):
            raise TypeError("HookBus handler must be callable")
        seq = next(self._seq)
        entry = (int(priority), seq, handler)
        with self._lock:
            bucket = self._handlers.setdefault(event, [])
            bucket.append(entry)
            bucket.sort(key=lambda item: (item[0], item[1]))

        def _unsubscribe() -> None:
            with self._lock:
                cur = self._handlers.get(event)
                if not cur:
                    return
                self._handlers[event] = [item for item in cur if item[1] != seq]
                if not self._handlers[event]:
                    self._handlers.pop(event, None)

        return _unsubscribe

    def off(self, event: str, handler: Callable[..., Any]) -> bool:
        """Remove all registrations of `handler` from `event`. Returns True if removed."""
        with self._lock:
            cur = self._handlers.get(event)
            if not cur:
                return False
            kept = [item for item in cur if item[2] is not handler]
            removed = len(kept) != len(cur)
            if kept:
                self._handlers[event] = kept
            else:
                self._handlers.pop(event, None)
            return removed

    def emit(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Invoke all listeners for `event` in priority order with fault isolation."""
        with self._lock:
            snapshot = list(self._handlers.get(event, ()))
        results: list[Any] = []
        for _priority, _seq, handler in snapshot:
            try:
                results.append(handler(*args, **kwargs))
            except Exception:
                logger.exception(
                    "HookBus listener %r failed while handling event %r",
                    handler,
                    event,
                )
        return results

    def handler_count(self, event: str | None = None) -> int:
        """Return the number of registered listeners for `event` or across all events."""
        with self._lock:
            if event is not None:
                return len(self._handlers.get(event, ()))
            return sum(len(items) for items in self._handlers.values())

    def clear(self, event: str | None = None) -> None:
        """Remove listeners for `event` or clear the entire bus."""
        with self._lock:
            if event is not None:
                self._handlers.pop(event, None)
            else:
                self._handlers.clear()


def _has_plugin_hooks(obj: Any) -> bool:
    return any(callable(getattr(obj, name, None)) for name in _PLUGIN_HOOK_METHODS)


def _instantiate_plugin(loaded: Any) -> Any:
    """Instantiate a class or zero-arg factory when it does not already expose plugin hooks."""
    if inspect.ismodule(loaded):
        return loaded
    if inspect.isclass(loaded):
        return loaded()
    if callable(loaded) and not _has_plugin_hooks(loaded):
        return loaded()
    return loaded


class ShinobuKernel:
    """
    Fault-isolated microkernel coordinating engines, tools, sensors, CLI hooks, and plugins.
    """

    def __init__(self, *, home: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self.home: Path = Path(home).expanduser() if home is not None else default_home()
        self.hooks: HookBus = HookBus()
        self._engines: dict[str, EngineAdapter] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._sensors: dict[str, Any] = {}
        self._cli_registrars: list[tuple[str, Callable[[Any], Any]]] = []
        self._plugins: dict[str, Any] = {}
        self._plugin_records: list[PluginLoadRecord] = []

    @property
    def bus(self) -> HookBus:
        """Alias for `self.hooks`."""
        return self.hooks

    # -- Direct registration APIs -------------------------------------------------

    def register_engine(
        self,
        adapter: EngineAdapter,
        *,
        name: str | None = None,
        replace: bool = True,
    ) -> None:
        """Register a single `EngineAdapter` under `name` or `adapter.name`."""
        key = str(name or getattr(adapter, "name", "")).strip()
        if not key:
            raise ValueError("EngineAdapter must provide a non-empty name")
        with self._lock:
            if not replace and key in self._engines:
                raise ValueError(f"Engine already registered: {key}")
            self._engines[key] = adapter

    def register_engines(
        self,
        engines: Mapping[str, EngineAdapter] | Iterable[EngineAdapter],
        *,
        replace: bool = True,
    ) -> None:
        """Register multiple engines from a mapping or iterable."""
        if isinstance(engines, Mapping):
            for key, adapter in engines.items():
                self.register_engine(adapter, name=str(key), replace=replace)
        else:
            for adapter in engines:
                self.register_engine(adapter, replace=replace)

    def register_tool(self, tool: ToolSpec, *, replace: bool = True) -> None:
        """Register a single `ToolSpec`."""
        if not isinstance(tool, ToolSpec):
            raise TypeError(f"Expected ToolSpec, got {type(tool).__name__}")
        with self._lock:
            if not replace and tool.name in self._tools:
                raise ValueError(f"Tool already registered: {tool.name}")
            self._tools[tool.name] = tool

    def register_tools(
        self,
        tools: Mapping[str, ToolSpec] | Iterable[ToolSpec],
        *,
        replace: bool = True,
    ) -> None:
        """Register multiple `ToolSpec` definitions."""
        if isinstance(tools, Mapping):
            for tool in tools.values():
                self.register_tool(tool, replace=replace)
        else:
            for tool in tools:
                self.register_tool(tool, replace=replace)

    def register_sensor(
        self,
        name: str,
        sensor: Any,
        *,
        replace: bool = True,
    ) -> None:
        """Register a sensor provider under `name`."""
        key = str(name or "").strip()
        if not key:
            raise ValueError("Sensor name must be a non-empty string")
        with self._lock:
            if not replace and key in self._sensors:
                raise ValueError(f"Sensor already registered: {key}")
            self._sensors[key] = sensor

    def register_sensors(
        self,
        sensors: Mapping[str, Any] | Iterable[Any],
        *,
        replace: bool = True,
    ) -> None:
        """Register multiple sensors from a `{name: sensor}` mapping or iterable with `.name`."""
        if isinstance(sensors, Mapping):
            for key, sensor in sensors.items():
                self.register_sensor(str(key), sensor, replace=replace)
        else:
            for sensor in sensors:
                key = str(getattr(sensor, "name", "")).strip()
                if not key:
                    raise ValueError(
                        "Sensors passed as an iterable must expose a non-empty `.name` attribute"
                    )
                self.register_sensor(key, sensor, replace=replace)

    def register_cli(
        self,
        registrar: Callable[[Any], Any],
        *,
        name: str = "",
    ) -> None:
        """Register a CLI subparser registrar callback `registrar(subparsers)`."""
        if not callable(registrar):
            raise TypeError("CLI registrar must be callable")
        label = name or getattr(registrar, "__qualname__", repr(registrar))
        with self._lock:
            self._cli_registrars.append((label, registrar))

    def apply_cli(self, subparsers: Any) -> list[str]:
        """Invoke all registered CLI registrars with fault isolation; return failed labels."""
        with self._lock:
            registrars = list(self._cli_registrars)
        failed: list[str] = []
        for label, registrar in registrars:
            try:
                registrar(subparsers)
            except Exception:
                logger.exception("CLI registrar %r failed", label)
                failed.append(label)
        return failed

    def register_hooks(
        self,
        hooks: Mapping[str, Callable[..., Any]] | Iterable[tuple[str, Callable[..., Any]]],
        *,
        priority: int = 100,
    ) -> list[Callable[[], None]]:
        """Subscribe multiple `(event, handler)` pairs on `self.hooks`."""
        pairs = hooks.items() if isinstance(hooks, Mapping) else hooks
        unsubscribers: list[Callable[[], None]] = []
        for event, handler in pairs:
            unsubscribers.append(self.hooks.on(event, handler, priority=priority))
        return unsubscribers

    # -- Read-only registry inspection -------------------------------------------

    def get_engine(self, name: str) -> EngineAdapter | None:
        with self._lock:
            return self._engines.get(name)

    def get_tool(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def get_sensor(self, name: str) -> Any | None:
        with self._lock:
            return self._sensors.get(name)

    @property
    def engines(self) -> dict[str, EngineAdapter]:
        with self._lock:
            return dict(self._engines)

    @property
    def tools(self) -> dict[str, ToolSpec]:
        with self._lock:
            return dict(self._tools)

    @property
    def sensors(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._sensors)

    @property
    def plugins(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._plugins)

    @property
    def plugin_records(self) -> list[PluginLoadRecord]:
        with self._lock:
            return list(self._plugin_records)

    # -- Plugin attachment & fault-isolated discovery -----------------------------

    def attach_plugin(
        self,
        name: str,
        plugin: Any,
        *,
        source: str = "manual",
        respect_disabled: bool = False,
    ) -> PluginLoadRecord:
        """
        Attach a plugin object/module to the kernel, invoking each supported hook
        inside an isolated fault boundary.
        """
        plugin_name = str(name or getattr(plugin, "name", "")).strip() or "unnamed"
        if respect_disabled and is_plugin_disabled(plugin_name):
            record = PluginLoadRecord(
                name=plugin_name,
                status="disabled",
                error=None,
                source=source,
            )
            with self._lock:
                self._plugin_records.append(record)
            return record

        errors: list[str] = []

        # 1. register_engines
        hook = getattr(plugin, "register_engines", None)
        if callable(hook):
            try:
                returned = hook(self)
                if returned is not None:
                    self.register_engines(returned)
            except Exception as exc:
                logger.exception("Plugin %r failed in register_engines", plugin_name)
                errors.append(f"register_engines: {exc}")

        # 2. register_tools
        hook = getattr(plugin, "register_tools", None)
        if callable(hook):
            try:
                returned = hook(self)
                if returned is not None:
                    self.register_tools(returned)
            except Exception as exc:
                logger.exception("Plugin %r failed in register_tools", plugin_name)
                errors.append(f"register_tools: {exc}")

        # 3. register_sensors
        hook = getattr(plugin, "register_sensors", None)
        if callable(hook):
            try:
                returned = hook(self)
                if returned is not None:
                    self.register_sensors(returned)
            except Exception as exc:
                logger.exception("Plugin %r failed in register_sensors", plugin_name)
                errors.append(f"register_sensors: {exc}")

        # 4. register_cli (deferred until apply_cli(subparsers))
        hook = getattr(plugin, "register_cli", None)
        if callable(hook):
            try:
                self.register_cli(hook, name=f"{plugin_name}.register_cli")
            except Exception as exc:
                logger.exception("Plugin %r failed in register_cli registration", plugin_name)
                errors.append(f"register_cli: {exc}")

        # 5. register_hooks
        hook = getattr(plugin, "register_hooks", None)
        if callable(hook):
            try:
                hook(self.hooks)
            except Exception as exc:
                logger.exception("Plugin %r failed in register_hooks", plugin_name)
                errors.append(f"register_hooks: {exc}")

        status = "failed" if errors else "loaded"
        record = PluginLoadRecord(
            name=plugin_name,
            status=status,
            error="; ".join(errors) if errors else None,
            source=source,
        )
        with self._lock:
            self._plugins[plugin_name] = plugin
            self._plugin_records.append(record)
        return record

    def discover_plugins(
        self,
        *,
        group: str = PLUGIN_GROUP,
        entry_points: Iterable[Any] | None = None,
    ) -> list[PluginLoadRecord]:
        """
        Discover and attach plugins from `importlib.metadata.entry_points(group=group)`.

        Respects `SHINOBU_DISABLE_PLUGINS` and `SHINOBU_DISABLED_PLUGINS`, and isolates
        import, instantiation, and hook errors per plugin so a broken extension never
        crashes the kernel or blocks healthy plugins.
        """
        if plugins_disabled():
            logger.info("Plugin discovery skipped (%s is active)", DISABLE_PLUGINS_ENV)
            return []

        if entry_points is None:
            try:
                discovered = list(importlib.metadata.entry_points(group=group))
            except Exception as exc:
                logger.exception("Failed querying entry_points for group %r", group)
                record = PluginLoadRecord(
                    name=group,
                    status="failed",
                    error=str(exc),
                    source="entry_point",
                )
                with self._lock:
                    self._plugin_records.append(record)
                return [record]
        else:
            discovered = list(entry_points)

        batch_records: list[PluginLoadRecord] = []
        for ep in discovered:
            ep_name = str(getattr(ep, "name", "") or "unnamed").strip()
            if is_plugin_disabled(ep_name):
                record = PluginLoadRecord(
                    name=ep_name,
                    status="disabled",
                    error=None,
                    source="entry_point",
                )
                with self._lock:
                    self._plugin_records.append(record)
                batch_records.append(record)
                continue

            with self._lock:
                if ep_name in self._plugins:
                    continue

            try:
                loaded = ep.load()
                plugin_obj = _instantiate_plugin(loaded)
            except Exception as exc:
                logger.exception("Failed loading plugin entry point %r", ep_name)
                record = PluginLoadRecord(
                    name=ep_name,
                    status="failed",
                    error=str(exc),
                    source="entry_point",
                )
                with self._lock:
                    self._plugin_records.append(record)
                batch_records.append(record)
                continue

            record = self.attach_plugin(
                ep_name,
                plugin_obj,
                source="entry_point",
                respect_disabled=False,
            )
            batch_records.append(record)

        return batch_records


__all__ = [
    "DISABLE_PLUGINS_ENV",
    "DISABLED_PLUGINS_ENV",
    "HOME_ENV",
    "HookBus",
    "PLUGIN_ENTRYPOINT_GROUP",
    "PLUGIN_GROUP",
    "PluginLoadRecord",
    "ShinobuKernel",
    "ShinobuPlugin",
    "default_home",
    "disabled_plugin_names",
    "is_plugin_disabled",
    "plugins_disabled",
]
