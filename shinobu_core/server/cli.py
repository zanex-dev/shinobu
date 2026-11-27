# -*- coding: utf-8 -*-
"""
Command-line interface for Shinobu (`init`, `chat`, `sleep`, `serve`, `ui`, `pet`, `compat`, `mcp`, `mcp-serve`, `telegram`, `node`, `status`, `doctor`).

Pure standard library (Python >= 3.10). Supports environment overrides:
  - `SHINOBU_HOME`            : runtime data directory (default `~/.shinobu`)
  - `SHINOBU_ENGINE`          : default engine adapter name (default `mock`)
  - `SHINOBU_CLOUD_ENGINE`    : fallback cloud engine for `hybrid` (default `openai_compat`)
  - `SHINOBU_TOKEN`           : persistent Bearer token for `serve` / clients
  - `SHINOBU_SANDBOX_DIR`     : workspace root for `CloudSandboxTerminal`
  - `SHINOBU_DISABLE_PLUGINS` : disable all external plugin discovery when truthy
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import inspect
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Sequence
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from shinobu_core.cloud.approval import ApprovalGate
from shinobu_core.cloud.edge_node import EdgeNodeClient, EdgeNodeServer
from shinobu_core.cloud.presence import PresenceEvaluator
from shinobu_core.cloud.registry import WorkerRegistry
from shinobu_core.cloud.sandbox import CloudSandboxTerminal
from shinobu_core.cloud.telegram_bridge import TelegramBotBridge
from shinobu_core.cloud.tts_router import TtsRouter
from shinobu_core.core.kernel import (
    ShinobuKernel,
    default_home,
    plugins_disabled,
)
from shinobu_core.engines import create_engine
from shinobu_core.engines.agy_engines import _augmented_agy_path
from shinobu_core.mcp.client import (
    MCP_CONFIG_FILENAME,
    McpClientManager,
    McpServerStatus,
    McpStdioClient,
)
from shinobu_core.mcp.community import (
    COMMUNITY_MCP_PRESETS,
    install_mcp_preset,
    list_community_presets,
    register_shinobu_onto_agy,
)
from shinobu_core.mcp.server import run_mcp_stdio_server
from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.engine import EngineAdapter
from shinobu_core.runtime.heartbeat import HeartbeatLoop
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.runtime.state_machine import PetStateMachine
from shinobu_core.server.daemon import DaemonServer, LOOPBACK_HOST
from shinobu_core.soul.loader import ensure_soul_files
from shinobu_core.ui.client import DaemonClient, EventStreamWorker, discover_endpoint

ENGINE_ENV = "SHINOBU_ENGINE"
DEFAULT_ENGINE_NAME = "mock"
DEFAULT_DB_RELATIVE = Path("memory") / "shinobu.db"
DEFAULT_SERVE_PORT = 18790


def resolve_home(home: Path | str | None = None) -> Path:
    """Resolve the active Shinobu home directory."""
    if home is not None and str(home).strip():
        return Path(home).expanduser()
    return default_home()


def resolve_engine_name(engine: str | None = None) -> str:
    """Resolve the requested engine name from CLI flag, `SHINOBU_ENGINE`, or default `mock`."""
    if engine is not None and engine.strip():
        return engine.strip()
    env_val = os.environ.get(ENGINE_ENV, "").strip()
    if env_val:
        return env_val
    return DEFAULT_ENGINE_NAME


def db_path_for(home: Path) -> Path:
    """Return the canonical SQLite memory store path under `home`."""
    return home / DEFAULT_DB_RELATIVE


def _find_active_episode_id(db_path: Path) -> str | None:
    """Return the most recent active episode ID in SQLite, or None if absent."""
    if not db_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM episodes WHERE status = 'active' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def _read_endpoint_metadata(home: Path) -> dict[str, Any] | None:
    """Read `runtime/endpoint.json` if present, redacting the Bearer token."""
    ep_path = home / "runtime" / "endpoint.json"
    if not ep_path.exists():
        return None
    try:
        raw = json.loads(ep_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(ep_path), "readable": False}
    if not isinstance(raw, dict):
        return {"path": str(ep_path), "readable": False}
    redacted = {k: v for k, v in raw.items() if k != "token"}
    redacted["token_present"] = bool(raw.get("token"))
    redacted["path"] = str(ep_path)
    return redacted


def _build_engine(
    kernel: ShinobuKernel,
    engine_name: str,
    *,
    worker_registry: WorkerRegistry | None = None,
) -> EngineAdapter:
    """Look up a plugin-registered engine first, then fall back to built-in adapters."""
    existing = kernel.get_engine(engine_name)
    if existing is not None:
        return existing

    norm = engine_name.strip().lower()
    kwargs: dict[str, Any] = {}
    if norm == "hybrid":
        cloud_engine_name = os.environ.get("SHINOBU_CLOUD_ENGINE", "").strip() or "openai_compat"
        if cloud_engine_name.strip().lower() == "hybrid":
            raise ValueError("SHINOBU_CLOUD_ENGINE cannot be 'hybrid'")
        cloud_fallback = _build_engine(kernel, cloud_engine_name)
        registry = worker_registry if worker_registry is not None else WorkerRegistry()
        raw_timeout = os.environ.get("SHINOBU_HYBRID_TIMEOUT", "").strip()
        timeout_sec = float(raw_timeout) if raw_timeout else 180.0
        primary_cap = os.environ.get("SHINOBU_HYBRID_CAPABILITY", "").strip() or "brain"
        return create_engine(
            "hybrid",
            cloud_fallback=cloud_fallback,
            registry=registry,
            primary_capability=primary_cap,
            remote_timeout_sec=timeout_sec,
        )
    if norm in ("openai_compat", "openai", "ollama"):
        base_url = os.environ.get("SHINOBU_BASE_URL", "").strip()
        model = os.environ.get("SHINOBU_MODEL", "").strip()
        api_key = os.environ.get("SHINOBU_API_KEY", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        if model:
            kwargs["model"] = model
        if api_key:
            kwargs["api_key"] = api_key
        extra_body: dict[str, Any] = {}
        extra_body_json = os.environ.get("SHINOBU_EXTRA_BODY_JSON", "").strip()
        if extra_body_json:
            import json as _json

            parsed_extra = _json.loads(extra_body_json)
            if not isinstance(parsed_extra, dict):
                raise ValueError("SHINOBU_EXTRA_BODY_JSON must decode to a JSON object")
            extra_body.update(parsed_extra)
        vertex_safety = os.environ.get("SHINOBU_VERTEX_SAFETY", "").strip()
        if vertex_safety:
            from shinobu_core.engines.openai_compat import build_vertex_safety_extra_body

            safety_body = build_vertex_safety_extra_body(vertex_safety)
            existing_google = extra_body.get("google")
            if isinstance(existing_google, dict):
                merged_google = dict(existing_google)
                merged_google.setdefault("safety_settings", safety_body["google"]["safety_settings"])
                extra_body["google"] = merged_google
            else:
                extra_body["google"] = safety_body["google"]
        reasoning_effort_env = os.environ.get("SHINOBU_REASONING_EFFORT", "").strip()
        if reasoning_effort_env:
            from shinobu_core.engines.openai_compat import normalize_reasoning_effort

            normalized_effort = normalize_reasoning_effort(reasoning_effort_env)
            if normalized_effort:
                extra_body["reasoning_effort"] = normalized_effort
            else:
                extra_body.pop("reasoning_effort", None)
        if extra_body:
            kwargs["extra_body"] = extra_body
        fallback_models_raw = os.environ.get("SHINOBU_FALLBACK_MODELS", "").strip()
        if fallback_models_raw:
            fallback_models = [item.strip() for item in fallback_models_raw.split(",") if item.strip()]
            if fallback_models:
                kwargs["fallback_models"] = fallback_models
        retry_max_attempts_raw = os.environ.get("SHINOBU_RETRY_MAX_ATTEMPTS", "").strip()
        if retry_max_attempts_raw:
            kwargs["retry_max_attempts"] = int(retry_max_attempts_raw)
        retry_base_delay_raw = os.environ.get("SHINOBU_RETRY_BASE_DELAY", "").strip()
        if retry_base_delay_raw:
            kwargs["retry_base_delay_sec"] = float(retry_base_delay_raw)
        api_timeout_raw = os.environ.get("SHINOBU_API_TIMEOUT", "").strip()
        if api_timeout_raw:
            api_timeout = float(api_timeout_raw)
            if api_timeout <= 0:
                raise ValueError("SHINOBU_API_TIMEOUT must be > 0")
            kwargs["timeout"] = api_timeout
    elif norm in ("agy_sdk", "agy-sdk"):
        runtime_dir = (kernel.home.expanduser().resolve() / "runtime")
        save_dir = (runtime_dir / "agy_sdk_save").resolve()
        app_data_dir = (runtime_dir / "agy_app_data").resolve()
        session_map_path = (runtime_dir / "agy_sdk_sessions.json").resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        app_data_dir.mkdir(parents=True, exist_ok=True)
        session_map_path.parent.mkdir(parents=True, exist_ok=True)

        mode = os.environ.get("SHINOBU_AGY_MODE", "").strip() or "gemini"
        model = os.environ.get("SHINOBU_MODEL", "").strip()
        base_url = os.environ.get("SHINOBU_BASE_URL", "").strip()
        model_path = os.environ.get("SHINOBU_MODEL_PATH", "").strip()
        api_key = (
            os.environ.get("SHINOBU_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )

        kwargs["mode"] = mode
        if model:
            kwargs["model"] = model
        if base_url:
            kwargs["base_url"] = base_url
        if model_path:
            kwargs["model_path"] = model_path
        if api_key:
            kwargs["api_key"] = api_key
        kwargs["save_dir"] = save_dir
        kwargs["app_data_dir"] = app_data_dir
        kwargs["session_map_path"] = session_map_path
    elif norm in ("agy_cli", "agy-cli"):
        kwargs.update(_build_agy_cli_kwargs(kernel))
    return create_engine(engine_name, **kwargs)


def _parse_optional_bool(raw: str) -> bool | None:
    val = raw.strip().lower()
    if not val:
        return None
    if val in ("0", "false", "off", "no"):
        return False
    if val in ("1", "true", "on", "yes"):
        return True
    return None


def _build_agy_cli_kwargs(
    kernel: ShinobuKernel,
    *,
    cwd: str | Path | None = None,
    effort: str | None = None,
    agent_mode: str | None = None,
    persistent: bool | None = None,
    add_dirs: Sequence[str | Path] | None = None,
    skip_permissions: bool | None = None,
    settings: str | dict[str, Any] | None = None,
    default_effort: str = "",
    default_persistent: bool | None = None,
) -> dict[str, object]:
    runtime_dir = (kernel.home.expanduser().resolve() / "runtime")
    env_cwd = os.environ.get("SHINOBU_AGY_CWD", "").strip()
    if cwd is not None and str(cwd).strip():
        resolved_cwd = Path(cwd).expanduser().resolve()
    elif env_cwd:
        resolved_cwd = Path(env_cwd).expanduser().resolve()
    else:
        resolved_cwd = (runtime_dir / "agy_workspace").resolve()
    session_map_path = (runtime_dir / "agy_cli_sessions.json").resolve()
    resolved_cwd.mkdir(parents=True, exist_ok=True)
    session_map_path.parent.mkdir(parents=True, exist_ok=True)

    binary = os.environ.get("SHINOBU_AGY_BIN", "").strip() or "agy"
    model = os.environ.get("SHINOBU_MODEL", "").strip()

    eff_effort = (
        effort.strip()
        if effort is not None
        else (os.environ.get("SHINOBU_AGY_EFFORT", "").strip() or default_effort)
    )
    eff_mode = (
        agent_mode.strip()
        if agent_mode is not None
        else os.environ.get("SHINOBU_AGY_MODE", "").strip()
    )
    env_persistent = _parse_optional_bool(os.environ.get("SHINOBU_AGY_PERSISTENT", ""))
    if persistent is False:
        eff_persistent: bool | None = False
    elif persistent is True:
        eff_persistent = env_persistent if env_persistent is not None else True
    else:
        eff_persistent = env_persistent if env_persistent is not None else default_persistent

    raw_add_dirs: list[str | Path] = []
    env_add_dirs = os.environ.get("SHINOBU_AGY_ADD_DIRS", "")
    if env_add_dirs:
        raw_add_dirs.extend(env_add_dirs.split(os.pathsep))
    if add_dirs:
        raw_add_dirs.extend(add_dirs)
    merged_add_dirs: list[str] = []
    seen_add_dirs: set[str] = set()
    for raw_dir in raw_add_dirs:
        text = str(raw_dir).strip()
        if not text:
            continue
        resolved_dir = str(Path(text).expanduser().resolve())
        norm_key = os.path.normcase(resolved_dir)
        if norm_key not in seen_add_dirs:
            seen_add_dirs.add(norm_key)
            merged_add_dirs.append(resolved_dir)

    env_skip_permissions = _parse_optional_bool(
        os.environ.get("SHINOBU_AGY_SKIP_PERMISSIONS", "")
    )
    if skip_permissions is not None:
        eff_skip_permissions = bool(skip_permissions)
    else:
        eff_skip_permissions = bool(env_skip_permissions) if env_skip_permissions is not None else False

    if settings is not None:
        eff_settings: Any = settings.strip() if isinstance(settings, str) else settings
    else:
        eff_settings = os.environ.get("SHINOBU_AGY_SETTINGS", "").strip() or None

    out: dict[str, object] = {
        "binary": binary,
        "cwd": resolved_cwd,
        "session_map_path": session_map_path,
    }
    if model:
        out["model"] = model
    if eff_effort:
        out["effort"] = eff_effort
    if eff_mode:
        out["agent_mode"] = eff_mode
    if eff_persistent is not None:
        out["persistent"] = eff_persistent
    if merged_add_dirs:
        out["add_dirs"] = tuple(merged_add_dirs)
    if eff_skip_permissions:
        out["skip_permissions"] = True
    if isinstance(eff_settings, str):
        if eff_settings:
            out["settings"] = eff_settings
    elif eff_settings is not None:
        out["settings"] = eff_settings
    return out


def _attach_kernel_sensors(kernel: ShinobuKernel, heartbeat: HeartbeatLoop) -> int:
    """Attach callable sensors registered on `kernel` to `heartbeat`."""
    attached = 0
    for sensor in kernel.sensors.values():
        if callable(sensor):
            heartbeat.add_sensor(sensor)
            attached += 1
    return attached


def _attach_mcp_manager(kernel: ShinobuKernel) -> McpClientManager | None:
    """Attach inbound MCP servers from `<home>/mcp_servers.json` onto `kernel` when enabled."""
    if plugins_disabled():
        return None
    config_path = kernel.home / MCP_CONFIG_FILENAME
    if not config_path.is_file():
        return None
    manager = McpClientManager(home=kernel.home, config_path=config_path)
    try:
        manager.connect_all()
        manager.register_onto(kernel)
    except Exception:
        manager.close()
        raise
    return manager


def _bind_mcp_cleanup(
    daemon: DaemonServer,
    store: MemoryStore,
    mcp_manager: McpClientManager,
) -> None:
    """Ensure `mcp_manager.close()` runs deterministically on `daemon.stop()` or `store.close()`."""
    closed = False

    def _close_once() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        mcp_manager.close()

    orig_stop = daemon.stop

    def _wrapped_stop(*args: Any, **kwargs: Any) -> Any:
        try:
            return orig_stop(*args, **kwargs)
        finally:
            _close_once()

    orig_store_close = store.close

    def _wrapped_store_close(*args: Any, **kwargs: Any) -> Any:
        try:
            return orig_store_close(*args, **kwargs)
        finally:
            _close_once()

    try:
        daemon.stop = _wrapped_stop  # type: ignore[method-assign]
        daemon.mcp_manager = mcp_manager  # type: ignore[attr-defined]
    except AttributeError:
        pass
    try:
        store.close = _wrapped_store_close  # type: ignore[method-assign]
    except AttributeError:
        pass


def build_daemon(
    kernel: ShinobuKernel,
    *,
    engine_name: str | None = None,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_SERVE_PORT,
    interval_sec: float = 30.0,
    token: str | None = None,
    cloud_sandbox_dir: Path | str | None = None,
    worker_registry: WorkerRegistry | None = None,
    approval_gate: ApprovalGate | None = None,
    tts_router: TtsRouter | None = None,
) -> tuple[DaemonServer, MemoryStore]:
    """Assemble `MemoryStore`, `Orchestrator`, `PetStateMachine`, `HeartbeatLoop`, and `DaemonServer`."""
    home = kernel.home
    ensure_soul_files(home)
    db_path = db_path_for(home)
    store = MemoryStore(db_path)
    mcp_manager: McpClientManager | None = None
    try:
        mcp_manager = _attach_mcp_manager(kernel)

        effective_token = token
        if not (isinstance(effective_token, str) and effective_token.strip()):
            env_token = os.environ.get("SHINOBU_TOKEN", "").strip()
            effective_token = env_token or None

        registry = worker_registry if worker_registry is not None else WorkerRegistry()
        daemon_ref: list[DaemonServer] = []

        def _forward_approval_event(ev: Any) -> None:
            if not daemon_ref:
                return
            srv = daemon_ref[0]
            publish_fn = getattr(srv, "publish_event", None)
            if callable(publish_fn):
                publish_fn(ev)
                return
            ctx = getattr(srv, "_ctx", None)
            broker = getattr(ctx, "broker", None) if ctx is not None else getattr(srv, "_broker", None)
            if broker is not None and callable(getattr(broker, "publish", None)):
                broker.publish(ev)

        gate = approval_gate if approval_gate is not None else ApprovalGate(on_event=_forward_approval_event)

        if tts_router is not None:
            tts = tts_router
        else:
            tts_fallback_url = os.environ.get("SHINOBU_TTS_FALLBACK_URL", "").strip()
            tts_fallback_key = os.environ.get("SHINOBU_TTS_FALLBACK_API_KEY", "").strip()
            gcp_voice = os.environ.get("SHINOBU_GCP_TTS_VOICE", "").strip() or "cmn-CN-Chirp3-HD-Aoede"
            tts = TtsRouter(
                registry,
                fallback_url=tts_fallback_url,
                fallback_api_key=tts_fallback_key,
                gcp_voice=gcp_voice,
            )

        raw_sandbox = cloud_sandbox_dir
        if raw_sandbox is None or not str(raw_sandbox).strip():
            raw_sandbox = os.environ.get("SHINOBU_SANDBOX_DIR", "").strip() or None
        if raw_sandbox is not None:
            sandbox_path = Path(raw_sandbox).expanduser().resolve()
            sandbox_path.mkdir(parents=True, exist_ok=True)
            raw_approval_timeout = os.environ.get("SHINOBU_APPROVAL_TIMEOUT_SEC", "").strip()
            approval_timeout_sec = 120.0
            if raw_approval_timeout:
                try:
                    parsed_approval_timeout = float(raw_approval_timeout)
                    if parsed_approval_timeout > 0:
                        approval_timeout_sec = parsed_approval_timeout
                except ValueError:
                    pass
            sandbox_terminal = CloudSandboxTerminal(
                sandbox_path,
                gate,
                approval_timeout_sec=approval_timeout_sec,
            )
            for tool in sandbox_terminal.build_tools():
                kernel.register_tool(tool)

        raw_auto_recall = os.environ.get("SHINOBU_AUTO_RECALL_LIMIT", "").strip()
        if raw_auto_recall:
            try:
                auto_recall_limit = max(0, int(raw_auto_recall))
            except ValueError:
                auto_recall_limit = 4
        else:
            auto_recall_limit = 4

        resolved_engine = resolve_engine_name(engine_name)
        engine = _build_engine(kernel, resolved_engine, worker_registry=registry)
        consolidator_engine = getattr(engine, "cloud_fallback", None) or engine
        from shinobu_core.runtime.classifier import build_classifier_from_env

        classifier = build_classifier_from_env(home, consolidator_engine)

        raw_idle_archive = os.environ.get("SHINOBU_EPISODE_IDLE_ARCHIVE_SEC", "").strip()
        if not raw_idle_archive:
            idle_archive_sec = 1800.0
        else:
            try:
                idle_archive_sec = float(raw_idle_archive)
            except ValueError:
                idle_archive_sec = 1800.0
        if idle_archive_sec < 0.0:
            idle_archive_sec = 0.0

        def _on_episode_reset(info: dict[str, Any]) -> None:
            if not daemon_ref:
                return
            daemon_ref[0].handle_episode_reset(info)

        orchestrator = Orchestrator(
            home,
            store,
            engine,
            kernel=kernel,
            consolidator_engine=consolidator_engine,
            auto_recall_limit=auto_recall_limit,
            classifier=classifier,
            inactivity_archive_sec=idle_archive_sec,
            on_episode_reset=_on_episode_reset,
        )
        active_ep = _find_active_episode_id(db_path)
        if active_ep is not None:
            orchestrator.ensure_episode(active_ep)

        raw_auto_sleep = os.environ.get("SHINOBU_AUTO_SLEEP_IDLE_SEC", "").strip()
        try:
            auto_sleep_sec = float(raw_auto_sleep) if raw_auto_sleep else 0.0
        except ValueError:
            auto_sleep_sec = 0.0
        if auto_sleep_sec < 0.0:
            auto_sleep_sec = 0.0

        raw_briefing_hour = os.environ.get("SHINOBU_DAILY_BRIEFING_HOUR", "").strip()
        daily_briefing_hour: int | None = None
        if raw_briefing_hour:
            try:
                parsed_hour = int(raw_briefing_hour)
                if 0 <= parsed_hour <= 23:
                    daily_briefing_hour = parsed_hour
            except ValueError:
                daily_briefing_hour = None

        raw_tz_offset = os.environ.get("SHINOBU_TZ_OFFSET_HOURS", "").strip()
        try:
            tz_offset_hours = float(raw_tz_offset) if raw_tz_offset else 8.0
        except ValueError:
            tz_offset_hours = 8.0

        def _build_daily_briefing() -> str:
            snippet = ""
            journals_dir = home / "memory" / "journals"
            try:
                if journals_dir.is_dir():
                    journal_files = sorted(
                        (p for p in journals_dir.glob("*.md") if p.is_file()),
                        key=lambda p: p.name,
                        reverse=True,
                    )
                    for jf in journal_files[:1]:
                        raw_md = jf.read_text(encoding="utf-8", errors="replace")
                        lines = [
                            ln.lstrip("#- *>").strip()
                            for ln in raw_md.splitlines()
                            if ln.strip() and not ln.strip().startswith("---")
                        ]
                        if lines:
                            snippet = " ".join(lines[:2])[:160].strip()
            except Exception:
                snippet = ""
            if not snippet:
                try:
                    snippet = str(store.latest_closed_summary() or "").strip()[:160]
                except Exception:
                    snippet = ""
            pending_count = 0
            try:
                pending_count = len(store.list_schedules(include_done=False, limit=50))
            except Exception:
                pending_count = 0
            parts: list[str] = ["早安！"]
            if snippet:
                parts.append(f"最近回顾：{snippet}。")
            if pending_count > 0:
                parts.append(f"当前有 {pending_count} 条待办提醒。")
            if len(parts) == 1:
                parts.append("新的一天开始了。")
            return "".join(parts)

        def _on_auto_sleep() -> Any:
            if not daemon_ref:
                return None
            current_daemon = daemon_ref[0]
            with current_daemon.chat_lock:
                ep_id = orchestrator.active_episode_id
                if not ep_id:
                    return None
                turns = store.get_turns(ep_id, limit=1)
                if not turns:
                    return None
                return orchestrator.sleep_and_consolidate()

        def _on_auto_archive() -> Any:
            if not daemon_ref:
                return None
            current_daemon = daemon_ref[0]
            with current_daemon.chat_lock:
                prev_raw = getattr(orchestrator.engine, "reasoning_effort", None)
                previous_effort = str(prev_raw) if prev_raw else ""
                archived = orchestrator.check_and_archive_inactive()
                if not archived:
                    return None
                current_daemon.notify_episode_idle_archived(
                    archived,
                    previous_effort=previous_effort,
                )
                return archived

        state_machine = PetStateMachine()
        heartbeat = HeartbeatLoop(
            store=store,
            state_machine=state_machine,
            sensors=None,
            publish=None,
            interval_sec=interval_sec,
            auto_sleep_after_sec=auto_sleep_sec,
            on_auto_sleep=_on_auto_sleep if auto_sleep_sec > 0.0 else None,
            auto_archive_after_sec=idle_archive_sec,
            on_auto_archive=_on_auto_archive if idle_archive_sec > 0.0 else None,
            daily_briefing_hour=daily_briefing_hour,
            tz_offset_hours=tz_offset_hours,
            briefing_builder=_build_daily_briefing if daily_briefing_hour is not None else None,
        )
        _attach_kernel_sensors(kernel, heartbeat)
        presence_env = os.environ.get("SHINOBU_PRESENCE_ENABLED", "1").strip().lower()
        if presence_env not in ("0", "false", "no", "off"):
            heartbeat.add_sensor(PresenceEvaluator(registry))

        daemon = DaemonServer(
            orchestrator=orchestrator,
            state_machine=state_machine,
            heartbeat=heartbeat,
            host=host,
            port=port,
            token=effective_token,
            home=home,
            worker_registry=registry,
            approval_gate=gate,
            tts_router=tts,
        )
        daemon_ref.append(daemon)
        if mcp_manager is not None:
            _bind_mcp_cleanup(daemon, store, mcp_manager)
    except Exception:
        if mcp_manager is not None:
            mcp_manager.close()
        store.close()
        raise
    return daemon, store


def _cmd_init(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    home = kernel.home
    soul_dir = ensure_soul_files(home)
    db_path = db_path_for(home)
    with MemoryStore(db_path):
        pass

    payload = {
        "status": "initialized",
        "home": str(home),
        "soul_dir": str(soul_dir),
        "db_path": str(db_path),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Initialized Shinobu home: {home}")
        print(f"  soul : {soul_dir}")
        print(f"  db   : {db_path}")
    return 0


def _run_chat_turn(orchestrator: Orchestrator, text: str) -> str:
    streamed = False
    final_text = ""
    for event in orchestrator.send_message(text):
        if event.kind == "token_delta" and isinstance(event.payload, dict):
            delta = str(event.payload.get("delta", ""))
            if delta:
                sys.stdout.write(delta)
                sys.stdout.flush()
                streamed = True
        elif event.kind == "done" and isinstance(event.payload, dict):
            final_text = str(event.payload.get("text", ""))
    if streamed:
        sys.stdout.write("\n")
        sys.stdout.flush()
    elif final_text:
        print(final_text)
    return final_text


def _cmd_chat(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    home = kernel.home
    ensure_soul_files(home)
    db_path = db_path_for(home)
    engine_name = resolve_engine_name(getattr(args, "engine", None))
    engine = _build_engine(kernel, engine_name)

    from shinobu_core.runtime.classifier import build_classifier_from_env

    consolidator_engine = getattr(engine, "cloud_fallback", None) or engine
    classifier = build_classifier_from_env(home, consolidator_engine)
    raw_idle_archive = os.environ.get("SHINOBU_EPISODE_IDLE_ARCHIVE_SEC", "").strip()
    try:
        idle_archive_sec = float(raw_idle_archive) if raw_idle_archive else 1800.0
    except ValueError:
        idle_archive_sec = 1800.0
    if idle_archive_sec < 0.0:
        idle_archive_sec = 0.0

    mcp_manager = _attach_mcp_manager(kernel)
    try:
        with MemoryStore(db_path) as store:
            orchestrator = Orchestrator(
                home,
                store,
                engine,
                kernel=kernel,
                classifier=classifier,
                inactivity_archive_sec=idle_archive_sec,
            )
            active_ep = _find_active_episode_id(db_path)
            if active_ep is not None:
                orchestrator.ensure_episode(active_ep)

            once_text = getattr(args, "once", None)
            if once_text is not None:
                if not str(once_text).strip():
                    print("Error: --once requires non-empty input text.", file=sys.stderr)
                    return 2
                _run_chat_turn(orchestrator, str(once_text))
                return 0

            # Interactive REPL loop
            print(f"Shinobu chat (engine={engine_name}, home={home}). Type /quit to exit, /sleep to consolidate.")
            while True:
                try:
                    sys.stdout.write("> ")
                    sys.stdout.flush()
                    line = sys.stdin.readline()
                except KeyboardInterrupt:
                    print()
                    break
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                if text in ("/quit", "/exit"):
                    break
                if text == "/sleep":
                    if orchestrator.active_episode_id is None:
                        print("No active episode to consolidate.")
                    else:
                        res = orchestrator.sleep_and_consolidate()
                        print(f"Consolidated episode {res.episode_id} -> {res.journal_path}")
                    continue
                _run_chat_turn(orchestrator, text)
    finally:
        if mcp_manager is not None:
            mcp_manager.close()
    return 0


def _cmd_sleep(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    home = kernel.home
    db_path = db_path_for(home)
    active_ep = _find_active_episode_id(db_path)
    if active_ep is None:
        if getattr(args, "json", False):
            print(json.dumps({"status": "idle", "detail": "no_active_episode"}, ensure_ascii=False))
        else:
            print("No active episode to consolidate.")
        return 0

    engine_name = resolve_engine_name(getattr(args, "engine", None))
    engine = _build_engine(kernel, engine_name)
    date_str = getattr(args, "date", None)

    with MemoryStore(db_path) as store:
        orchestrator = Orchestrator(home, store, engine, kernel=kernel)
        orchestrator.ensure_episode(active_ep)
        result = orchestrator.sleep_and_consolidate(date_str=date_str)

    if getattr(args, "json", False):
        data = (
            dataclasses.asdict(result)
            if dataclasses.is_dataclass(result) and not isinstance(result, type)
            else {"result": str(result)}
        )
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Consolidated episode: {result.episode_id}")
        print(f"  summary : {result.summary}")
        print(f"  journal : {result.journal_path}")
    return 0


def _cmd_serve(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    daemon, store = build_daemon(
        kernel,
        engine_name=getattr(args, "engine", None),
        host=getattr(args, "host", LOOPBACK_HOST),
        port=int(getattr(args, "port", DEFAULT_SERVE_PORT)),
        interval_sec=float(getattr(args, "interval", 30.0)),
        token=getattr(args, "token", None),
        cloud_sandbox_dir=getattr(args, "cloud_sandbox", None),
    )

    stop_event = threading.Event()
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            stop_event.set()

        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, OSError, AttributeError):
            prev_sigterm = None

    try:
        info = daemon.start(start_heartbeat=True)
        print(
            f"Shinobu daemon listening on {info['url']} "
            f"(pid={info['pid']}, endpoint={daemon.endpoint_path})"
        )
        sys.stdout.flush()
        while not stop_event.is_set() and daemon.is_running:
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError, AttributeError):
                pass
        daemon.stop()
        store.close()
    return 0


_LOCAL_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _daemon_http_call(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    expect_bytes: bool = False,
) -> Any:
    url = base_url.rstrip("/") + path
    headers: dict[str, str] = {"Accept": "*/*" if expect_bytes else "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    with _LOCAL_NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if expect_bytes:
            return raw
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))


def _resolve_daemon_connection(
    home: Path,
    daemon_url: str | None = None,
    token: str | None = None,
) -> tuple[str, str]:
    resolved_url = (daemon_url or "").strip() or os.environ.get("SHINOBU_DAEMON_URL", "").strip()
    resolved_token = (token or "").strip() or os.environ.get("SHINOBU_TOKEN", "").strip()
    if not resolved_url or not resolved_token:
        ep = discover_endpoint(home)
        if isinstance(ep, dict):
            if not resolved_url:
                resolved_url = str(ep.get("url") or "").strip()
            if not resolved_token:
                resolved_token = str(ep.get("token") or "").strip()
    if not resolved_url:
        resolved_url = f"http://{LOOPBACK_HOST}:{DEFAULT_SERVE_PORT}"
    return resolved_url.rstrip("/"), resolved_token


def build_telegram_bridge(
    *,
    bot_token: str,
    allowed_chat_id: str | int,
    daemon_url: str,
    daemon_token: str,
    api_base: str = "https://api.telegram.org",
    timeout_sec: float = 600.0,
    quiet_hours: str | None = None,
    tz_offset_hours: float | None = None,
) -> TelegramBotBridge:
    """Wire `TelegramBotBridge` (T04 four-lock isolation) to a running Shinobu daemon."""
    base = daemon_url.rstrip("/")

    def _on_chat(text: str) -> str:
        try:
            raw_bytes = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/chat",
                {"text": text, "stream": False},
                timeout=timeout_sec,
                expect_bytes=True,
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                err_body = ""
            if err_body:
                try:
                    parsed = json.loads(err_body)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    detail = str(parsed.get("detail") or parsed.get("error") or "").strip()
                if not detail:
                    detail = err_body[:300]
            if not detail:
                detail = str(exc.reason or f"HTTP {exc.code}").strip()
            raise RuntimeError(f"daemon HTTP {exc.code}: {detail}") from exc
        raw_text = raw_bytes.decode("utf-8", errors="replace").strip() if raw_bytes else ""
        if not raw_text:
            return ""
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            # Parse SSE stream fallback (`event: ...` / `data: {...}`)
            deltas: list[str] = []
            final_text = ""
            for line in raw_text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                payload_str = stripped[5:].strip()
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    deltas.append(delta)
                for key in ("reply", "assistant_text", "text"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.strip():
                        final_text = val
            return final_text or "".join(deltas)

        if isinstance(data, dict):
            for key in ("reply", "assistant_text", "text", "message", "content"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val
        return str(data or "")

    def _on_approval_decision(
        approval_id: str,
        approved: bool,
        actor: str = "telegram",
    ) -> bool:
        safe_id = urllib.parse.quote(str(approval_id), safe="")
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                f"/api/approvals/{safe_id}/decide",
                {"approved": bool(approved), "actor": str(actor or "telegram")},
                timeout=15.0,
            )
        except urllib.error.HTTPError:
            return False
        if isinstance(data, dict):
            if "ok" in data:
                return bool(data["ok"])
            if "decided" in data:
                return bool(data["decided"])
        return True

    def _on_status() -> str:
        state_data: dict[str, Any] = {}
        try:
            raw_state = _daemon_http_call(base, daemon_token, "GET", "/state", timeout=10.0)
            if isinstance(raw_state, dict):
                state_data = raw_state
        except Exception:
            try:
                raw_health = _daemon_http_call(base, daemon_token, "GET", "/health", timeout=10.0)
                if isinstance(raw_health, dict):
                    state_data = raw_health
            except Exception:
                state_data = {}

        engine_data: dict[str, Any] = {}
        try:
            raw_engine = _daemon_http_call(base, daemon_token, "GET", "/api/engine", timeout=10.0)
            if isinstance(raw_engine, dict):
                engine_data = raw_engine
        except Exception:
            engine_data = {}

        pending_count = 0
        try:
            approvals = _daemon_http_call(base, daemon_token, "GET", "/api/approvals", timeout=10.0)
            if isinstance(approvals, dict):
                raw_items = approvals.get("pending")
                if raw_items is None:
                    raw_items = approvals.get("approvals")
                if isinstance(raw_items, list):
                    pending_count = len(raw_items)
            elif isinstance(approvals, list):
                pending_count = len(approvals)
        except Exception:
            pending_count = 0

        state = str(
            state_data.get("state")
            or state_data.get("status")
            or "unknown"
        )
        mood = str(state_data.get("mood") or "calm")
        engine = str(
            engine_data.get("engine")
            or engine_data.get("kind")
            or state_data.get("engine")
            or "unknown"
        )
        route_mode = engine_data.get("route_mode")
        reasoning_effort = str(engine_data.get("reasoning_effort") or "").strip()
        supports_reasoning = engine_data.get("supports_reasoning_switch")
        reasoning_mode = str(engine_data.get("reasoning_mode") or "").strip().lower()
        effective_effort = str(engine_data.get("effective_reasoning_effort") or "").strip()
        episode_effort = str(engine_data.get("episode_reasoning_effort") or "").strip()
        brain_workers = engine_data.get("online_brain_workers")
        last_route = engine_data.get("last_route")

        lines = [
            "Shinobu Cloud Online",
            f"- State: {state} ({mood})",
            f"- Engine: {engine}",
        ]
        if route_mode:
            lines.append(f"- Route mode: {route_mode}")
        if supports_reasoning is True or reasoning_effort or "reasoning_effort" in engine_data:
            if reasoning_mode == "auto" and (effective_effort or episode_effort):
                lines.append(f"- Reasoning: auto (ratchet: {effective_effort or episode_effort})")
            elif reasoning_mode == "manual" and reasoning_effort:
                lines.append(f"- Reasoning: {reasoning_effort} (manual lock)")
            else:
                lines.append(f"- Reasoning: {reasoning_effort or 'default'}")
        if isinstance(brain_workers, list):
            worker_text = ", ".join(str(w) for w in brain_workers) if brain_workers else "none (cloud fallback)"
            lines.append(f"- Brain workers: {worker_text}")
        if isinstance(last_route, dict) and last_route:
            target = str(last_route.get("target") or "unknown")
            wid = str(last_route.get("worker_id") or "").strip()
            elapsed = last_route.get("elapsed_ms")
            route_desc = target
            if wid:
                route_desc += f" ({wid})"
            if elapsed is not None:
                route_desc += f" · {elapsed}ms"
            lines.append(f"- Last route: {route_desc}")
        lines.append(f"- Pending approvals: {pending_count}")
        return "\n".join(lines)

    def _on_route(mode: str | None = None) -> dict[str, Any]:
        if mode is None or not str(mode).strip():
            data = _daemon_http_call(base, daemon_token, "GET", "/api/engine", timeout=10.0)
            return data if isinstance(data, dict) else {"ok": True, "engine": str(data or "")}
        target_mode = str(mode).strip()
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/api/engine/route",
                {"mode": target_mode},
                timeout=10.0,
            )
            return data if isinstance(data, dict) else {"ok": True, "mode": target_mode}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg, "mode": target_mode}

    def _on_reasoning(effort: str | None = None) -> dict[str, Any]:
        if effort is None or not str(effort).strip():
            data = _daemon_http_call(base, daemon_token, "GET", "/api/engine", timeout=10.0)
            return data if isinstance(data, dict) else {"ok": True, "engine": str(data or "")}
        target_effort = str(effort).strip()
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/api/engine/reasoning",
                {"reasoning_effort": target_effort},
                timeout=10.0,
            )
            return data if isinstance(data, dict) else {"ok": True, "reasoning_effort": target_effort}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg, "reasoning_effort": target_effort}

    def _on_nodes() -> str:
        data = _daemon_http_call(base, daemon_token, "GET", "/api/workers", timeout=10.0)
        workers = data.get("workers", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not workers:
            return "No online edge/GPU workers (running in pure cloud mode)."
        lines = ["Online workers:"]
        for item in workers:
            if not isinstance(item, dict):
                continue
            wid = item.get("worker_id", "unknown")
            role = item.get("role", "worker")
            caps = ",".join(str(c) for c in (item.get("capabilities") or []))
            lines.append(f"- {wid} [{role}] ({caps or 'none'})")
        return "\n".join(lines)

    def _on_tts(text: str) -> bytes | None:
        try:
            raw = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/api/tts",
                {"text": text, "mood": "calm", "fmt": "ogg_opus"},
                timeout=35.0,
                expect_bytes=True,
            )
            return raw if isinstance(raw, (bytes, bytearray)) and len(raw) > 0 else None
        except Exception:
            return None

    def _on_sleep() -> dict[str, Any]:
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/sleep",
                {},
                timeout=90.0,
            )
            return data if isinstance(data, dict) else {"ok": True, "result": str(data or "")}
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return {
                    "ok": True,
                    "status": "idle",
                    "message": "当前没有进行中的对话可归档",
                }
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg}

    def _on_session() -> dict[str, Any]:
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "GET",
                "/api/episodes?limit=5",
                timeout=10.0,
            )
            return data if isinstance(data, dict) else {"ok": False, "error": "invalid_response"}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg}

    def _on_new_session() -> dict[str, Any]:
        try:
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/api/episodes/new",
                {},
                timeout=15.0,
            )
            return data if isinstance(data, dict) else {"ok": True, "status": "archived"}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg}

    def _on_remind(
        trigger_at: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        try:
            if trigger_at is None and label is None:
                data = _daemon_http_call(
                    base,
                    daemon_token,
                    "GET",
                    "/api/timemap?limit=20",
                    None,
                    timeout=10.0,
                )
                return data if isinstance(data, dict) else {"schedules": []}
            label_clean = str(label or "").strip()
            body = {
                "trigger_at": str(trigger_at or "").strip(),
                "label": label_clean,
                "payload": {
                    "message": label_clean,
                    "source": "timemap",
                    "created_by": "telegram",
                },
            }
            data = _daemon_http_call(
                base,
                daemon_token,
                "POST",
                "/api/timemap",
                body,
                timeout=10.0,
            )
            return data if isinstance(data, dict) else {"ok": True}
        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}"
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and parsed.get("error"):
                    err_msg = str(parsed["error"])
                elif raw_body.strip():
                    err_msg = raw_body.strip()
            except Exception:
                pass
            return {"ok": False, "error": err_msg}

    typing_env = os.environ.get("SHINOBU_TELEGRAM_TYPING", "").strip().lower()
    typing_enabled = typing_env not in ("0", "false", "no", "off")
    menu_env = os.environ.get("SHINOBU_TELEGRAM_MENU", "").strip().lower()
    register_commands = menu_env not in ("0", "false", "no", "off")

    if quiet_hours is None:
        env_quiet = os.environ.get("SHINOBU_QUIET_HOURS")
        effective_quiet = "01:00-08:00" if env_quiet is None else env_quiet.strip()
    else:
        effective_quiet = str(quiet_hours).strip()

    if tz_offset_hours is None:
        raw_tz = os.environ.get("SHINOBU_TZ_OFFSET_HOURS", "").strip()
        try:
            effective_tz = float(raw_tz) if raw_tz else 8.0
        except ValueError:
            effective_tz = 8.0
    else:
        effective_tz = float(tz_offset_hours)

    return TelegramBotBridge(
        bot_token,
        allowed_chat_id,
        on_chat=_on_chat,
        on_approval_decision=_on_approval_decision,
        on_status=_on_status,
        on_nodes=_on_nodes,
        on_tts=_on_tts,
        on_route=_on_route,
        on_reasoning=_on_reasoning,
        on_sleep=_on_sleep,
        on_session=_on_session,
        on_new_session=_on_new_session,
        on_remind=_on_remind,
        register_commands=register_commands,
        api_base=api_base,
        typing_indicator=typing_enabled,
        quiet_hours=effective_quiet,
        tz_offset_hours=effective_tz,
    )


def _cmd_telegram(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    bot_token = (getattr(args, "bot_token", None) or "").strip() or os.environ.get(
        "SHINOBU_TELEGRAM_BOT_TOKEN", ""
    ).strip()
    chat_id = (getattr(args, "chat_id", None) or "").strip() or os.environ.get(
        "SHINOBU_TELEGRAM_CHAT_ID", ""
    ).strip()
    if not bot_token or not chat_id:
        print(
            "Error: Telegram bridge requires --bot-token ($SHINOBU_TELEGRAM_BOT_TOKEN) "
            "and --chat-id ($SHINOBU_TELEGRAM_CHAT_ID).",
            file=sys.stderr,
        )
        return 2

    daemon_url, daemon_token = _resolve_daemon_connection(
        kernel.home,
        daemon_url=getattr(args, "daemon_url", None),
        token=getattr(args, "token", None),
    )
    api_base = (getattr(args, "api_base", None) or "").strip() or "https://api.telegram.org"
    approval_poll_sec = max(0.5, float(getattr(args, "approval_poll_sec", 2.0)))
    raw_chat_timeout = getattr(args, "chat_timeout_sec", None)
    if raw_chat_timeout is None:
        env_chat_timeout = os.environ.get("SHINOBU_TELEGRAM_CHAT_TIMEOUT", "").strip()
        if env_chat_timeout:
            try:
                parsed_chat_timeout = float(env_chat_timeout)
                if parsed_chat_timeout > 0:
                    raw_chat_timeout = parsed_chat_timeout
            except ValueError:
                raw_chat_timeout = None
    chat_timeout_sec = max(1.0, float(raw_chat_timeout)) if raw_chat_timeout is not None else 600.0

    env_quiet = os.environ.get("SHINOBU_QUIET_HOURS")
    quiet_hours = "01:00-08:00" if env_quiet is None else env_quiet.strip()
    raw_tz = os.environ.get("SHINOBU_TZ_OFFSET_HOURS", "").strip()
    try:
        tz_offset_hours = float(raw_tz) if raw_tz else 8.0
    except ValueError:
        tz_offset_hours = 8.0

    bridge = build_telegram_bridge(
        bot_token=bot_token,
        allowed_chat_id=chat_id,
        daemon_url=daemon_url,
        daemon_token=daemon_token,
        api_base=api_base,
        timeout_sec=chat_timeout_sec,
        quiet_hours=quiet_hours,
        tz_offset_hours=tz_offset_hours,
    )

    def _on_proactive_event(ev: Any) -> None:
        kind = str(
            getattr(ev, "kind", "")
            or (ev.get("kind", "") if isinstance(ev, dict) else "")
        ).strip()
        if kind != "bubble":
            return
        try:
            bridge.push_proactive_event(ev)
        except Exception:
            pass

    event_client = DaemonClient(daemon_url, daemon_token, allow_remote=True)
    event_worker = EventStreamWorker(event_client, _on_proactive_event)

    stop_event = threading.Event()
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            stop_event.set()

        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, OSError, AttributeError):
            prev_sigterm = None

    pushed_ids: set[str] = set()

    def _approval_watcher() -> None:
        while not stop_event.is_set():
            try:
                data = _daemon_http_call(
                    daemon_url, daemon_token, "GET", "/api/approvals", timeout=10.0
                )
                if isinstance(data, dict):
                    pending = data.get("pending")
                    if pending is None:
                        pending = data.get("approvals")
                    if not isinstance(pending, list):
                        pending = []
                elif isinstance(data, list):
                    pending = data
                else:
                    pending = []
                active_ids: set[str] = set()
                for item in pending:
                    if not isinstance(item, dict):
                        continue
                    aid = str(item.get("approval_id") or item.get("id") or "").strip()
                    if not aid:
                        continue
                    active_ids.add(aid)
                    if aid not in pushed_ids:
                        bridge.push_approval_prompt(item)
                        pushed_ids.add(aid)
                for stale_id in list(pushed_ids - active_ids):
                    try:
                        getattr(bridge, "close_approval_prompt", lambda _id: False)(stale_id)
                    except Exception:
                        pass
                pushed_ids.intersection_update(active_ids)
            except Exception:
                pass
            stop_event.wait(timeout=approval_poll_sec)

    watcher_thread = threading.Thread(
        target=_approval_watcher, name="shinobu-tg-approvals", daemon=True
    )

    try:
        bridge.start_polling()
        watcher_thread.start()
        event_worker.start()
        print(
            f"Shinobu Telegram bridge started (daemon={daemon_url}, chat_id={chat_id})"
        )
        sys.stdout.flush()
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError, AttributeError):
                pass
        event_worker.stop(timeout=2.0)
        bridge.stop()
        if watcher_thread.is_alive():
            watcher_thread.join(timeout=2.0)
    return 0


def build_edge_node(
    kernel: ShinobuKernel,
    *,
    cloud_url: str,
    cloud_token: str = "",
    node_token: str | None = None,
    worker_id: str = "home_pc",
    bind_host: str = LOOPBACK_HOST,
    port: int = 18792,
    advertise_url: str | None = None,
    engine_name: str | None = None,
    allowed_roots: Sequence[Path | str] | None = None,
    airi_bubble_url: str = "",
    heartbeat_sec: float = 15.0,
    effort: str | None = None,
    agy_mode: str | None = None,
    persistent: bool = True,
    agy_add_dirs: Sequence[Path | str] | None = None,
    agy_skip_permissions: bool | None = None,
    presence_enabled: bool | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_registered: Callable[[bool], None] | None = None,
) -> tuple[EdgeNodeServer, EdgeNodeClient]:
    """Assemble T06 `EdgeNodeServer` + `EdgeNodeClient` for home-PC edge agent."""
    resolved_cloud_url = cloud_url.strip().rstrip("/")
    if not resolved_cloud_url:
        raise ValueError("cloud_url is required")

    effective_node_token = (node_token or "").strip() or os.environ.get("SHINOBU_NODE_TOKEN", "").strip()
    if not effective_node_token:
        effective_node_token = secrets.token_urlsafe(24)

    resolved_engine_name = (
        (engine_name or "").strip()
        or os.environ.get("SHINOBU_NODE_ENGINE", "").strip()
        or "agy_cli"
    )
    local_engine: EngineAdapter | None
    if resolved_engine_name.lower() == "none":
        local_engine = None
    elif resolved_engine_name.lower() in ("agy_cli", "agy-cli"):
        agy_kwargs = _build_agy_cli_kwargs(
            kernel,
            effort=effort,
            agent_mode=agy_mode,
            persistent=persistent,
            add_dirs=agy_add_dirs,
            skip_permissions=agy_skip_permissions,
            default_effort="low",
            default_persistent=True,
        )
        local_engine = create_engine(resolved_engine_name, **agy_kwargs)
    else:
        local_engine = _build_engine(kernel, resolved_engine_name)

    roots: list[Path] = []
    if allowed_roots:
        for r in allowed_roots:
            if str(r).strip():
                roots.append(Path(r).expanduser().resolve())
    if not roots:
        roots = [Path.cwd().resolve()]

    server = EdgeNodeServer(
        local_engine,
        node_token=effective_node_token,
        allowed_roots=roots,
        bind_host=bind_host,
        port=int(port),
        airi_bubble_url=airi_bubble_url.strip(),
    )

    eff_advertise = (advertise_url or "").strip() or os.environ.get(
        "SHINOBU_NODE_ADVERTISE_URL", ""
    ).strip()
    if not eff_advertise:
        eff_advertise = f"http://{bind_host}:{int(port)}"

    if presence_enabled is None:
        edge_presence_env = os.environ.get("SHINOBU_EDGE_PRESENCE", "1").strip().lower()
        effective_presence = edge_presence_env not in ("0", "false", "no", "off")
    else:
        effective_presence = bool(presence_enabled)

    capabilities = ("brain", "terminal", "bubble") if local_engine is not None else ("terminal", "bubble")
    client = EdgeNodeClient(
        resolved_cloud_url,
        cloud_token,
        worker_id=worker_id.strip() or "home_pc",
        advertise_url=eff_advertise,
        node_token=effective_node_token,
        capabilities=capabilities,
        heartbeat_sec=float(heartbeat_sec),
        on_error=on_error,
        on_registered=on_registered,
        presence_enabled=effective_presence,
    )
    return server, client


def _cmd_node(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    cloud_url = (getattr(args, "cloud_url", None) or "").strip() or os.environ.get(
        "SHINOBU_CLOUD_URL", ""
    ).strip()
    if not cloud_url:
        print(
            "Error: Edge node requires --cloud-url ($SHINOBU_CLOUD_URL).",
            file=sys.stderr,
        )
        return 2

    cloud_token = (
        (getattr(args, "cloud_token", None) or "").strip()
        or os.environ.get("SHINOBU_CLOUD_TOKEN", "").strip()
        or os.environ.get("SHINOBU_TOKEN", "").strip()
    )
    bind_host = (getattr(args, "bind_host", None) or "").strip() or LOOPBACK_HOST
    port = int(getattr(args, "port", 18792))
    advertise_url = getattr(args, "advertise_url", None)

    worker_id_value = (getattr(args, "worker_id", None) or "home_pc").strip() or "home_pc"
    caller_thread = threading.current_thread()
    log_lock = threading.Lock()
    last_error_logged: dict[tuple[str, str, str], float] = {}
    in_initial_start = True

    def _on_node_error(stage: str, exc: Exception) -> None:
        err_key = (stage, type(exc).__name__, str(exc))
        now = time.monotonic()
        with log_lock:
            prev = last_error_logged.get(err_key)
            if prev is not None and (now - prev) < 60.0:
                return
            last_error_logged[err_key] = now
        print(
            f"[shinobu node] {stage} failed ({cloud_url}): {exc}",
            file=sys.stderr,
        )
        sys.stderr.flush()

    def _on_node_registered(is_reregister: bool) -> None:
        with log_lock:
            last_error_logged.clear()
            suppress = in_initial_start and threading.current_thread() is caller_thread
        if suppress:
            return
        status_verb = "re-registered with" if is_reregister else "registered with"
        print(f"Shinobu edge node '{worker_id_value}' {status_verb} {cloud_url}")
        sys.stdout.flush()

    heartbeat_sec = float(getattr(args, "heartbeat_sec", 15.0))
    server, client = build_edge_node(
        kernel,
        cloud_url=cloud_url,
        cloud_token=cloud_token,
        node_token=getattr(args, "node_token", None),
        worker_id=worker_id_value,
        bind_host=bind_host,
        port=port,
        advertise_url=advertise_url,
        engine_name=getattr(args, "engine", None),
        allowed_roots=getattr(args, "allowed_root", None),
        airi_bubble_url=getattr(args, "airi_bubble_url", None) or "",
        heartbeat_sec=heartbeat_sec,
        effort=getattr(args, "effort", None),
        agy_mode=getattr(args, "agy_mode", None),
        persistent=not bool(getattr(args, "no_persistent", False)),
        agy_add_dirs=getattr(args, "agy_add_dir", None),
        agy_skip_permissions=True if bool(getattr(args, "agy_skip_permissions", False)) else None,
        presence_enabled=False if bool(getattr(args, "no_presence", False)) else None,
        on_error=_on_node_error,
        on_registered=_on_node_registered,
    )

    stop_event = threading.Event()
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            stop_event.set()

        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, OSError, AttributeError):
            prev_sigterm = None

    try:
        server_info = server.start()
        if not (advertise_url and str(advertise_url).strip()) and port == 0:
            actual_url = (
                server_info.get("url")
                if isinstance(server_info, dict)
                else getattr(server, "url", None)
            )
            if isinstance(actual_url, str) and actual_url.strip():
                client.advertise_url = actual_url.strip()
        try:
            registered = client.start()
        finally:
            with log_lock:
                in_initial_start = False
        if registered:
            print(
                f"Shinobu edge node '{client.worker_id}' listening on {client.advertise_url} "
                f"(registered with {cloud_url})"
            )
        else:
            retry_sec = float(getattr(client, "heartbeat_sec", heartbeat_sec))
            print(
                f"Shinobu edge node '{client.worker_id}' listening on {client.advertise_url} "
                f"(waiting for cloud {cloud_url}; retrying every {retry_sec:g}s)"
            )
        sys.stdout.flush()
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError, AttributeError):
                pass
        try:
            client.stop()
        finally:
            server.stop()
    return 0


def _is_daemon_healthy(base_url: str, token: str) -> bool:
    """Probe the local Shinobu daemon health endpoint via `DaemonClient`."""
    try:
        health = DaemonClient(url=base_url, token=token).health()
        return isinstance(health, dict) and health.get("status") == "ok"
    except Exception:
        return False


def _cmd_compat(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    """Emit community-frontend connection info (`/v1` OpenAI-compatible API + SSE events)."""
    home = kernel.home
    mcp_config_path = home / MCP_CONFIG_FILENAME
    mcp_config_exists = mcp_config_path.is_file()
    as_json = bool(getattr(args, "json", False))

    info = discover_endpoint(home)
    if isinstance(info, dict):
        base_url = str(info.get("url") or "").rstrip("/")
        token = str(info.get("token") or "")
    elif info is not None:
        base_url = str(getattr(info, "url", "") or "").rstrip("/")
        token = str(getattr(info, "token", "") or "")
    else:
        base_url = ""
        token = ""
    healthy = bool(base_url and token and _is_daemon_healthy(base_url, token))

    if not healthy:
        msg = "No healthy Shinobu daemon is running. Start one first with: shinobu serve"
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": msg,
                        "mcp_config_exists": mcp_config_exists,
                        "mcp_config_path": str(mcp_config_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(msg, file=sys.stderr)
        return 1

    payload = {
        "openai_base_url": f"{base_url}/v1",
        "model": "shinobu",
        "api_key": token,
        "events_url": f"{base_url}/events",
        "bubble_url": f"{base_url}/api/bubble",
        "mcp_config_exists": mcp_config_exists,
        "mcp_config_path": str(mcp_config_path),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Shinobu Community Compatibility Endpoint (Project AIRI / Open-LLM-VTuber / VPetLLM)")
        print(f"  OpenAI Base URL : {payload['openai_base_url']}")
        print(f"  Model           : {payload['model']}")
        print(f"  API Key         : {payload['api_key']}")
        print(f"  SSE Events URL  : {payload['events_url']}")
        print(f"  Bubble Push URL : {payload['bubble_url']}")
        mcp_state = "present" if mcp_config_exists else "not found"
        print(f"  MCP Config      : {mcp_config_path} ({mcp_state})")
    return 0


def _cmd_mcp(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    """List, probe, install community presets, or bridge MCP servers with `agy`."""
    home = kernel.home
    probe = bool(getattr(args, "probe", False))
    as_json = bool(getattr(args, "json", False))
    show_presets = bool(getattr(args, "presets", False))
    add_presets = list(getattr(args, "add_preset", None) or [])
    overwrite = bool(getattr(args, "overwrite", False))
    bridge_agy = bool(getattr(args, "bridge_agy", False))
    unbridge_agy = bool(getattr(args, "unbridge_agy", False))
    dry_run = bool(getattr(args, "dry_run", False))

    if bridge_agy and unbridge_agy:
        err_msg = "Cannot use --bridge-agy and --unbridge-agy together."
        if as_json:
            print(json.dumps({"ok": False, "error": err_msg}, ensure_ascii=False, indent=2))
        else:
            print(f"Error: {err_msg}", file=sys.stderr)
        return 2

    if bridge_agy or unbridge_agy:
        explicit_home = getattr(args, "home", None)
        bridge_home = home if explicit_home else None
        remote_url = (getattr(args, "remote_url", None) or "").strip() or None
        raw_bearer = getattr(args, "bearer_token", None)
        if raw_bearer is not None:
            bearer_token = str(raw_bearer).strip() or None
        elif remote_url:
            bearer_token = os.environ.get("SHINOBU_TOKEN", "").strip() or None
        else:
            bearer_token = None
        result = register_shinobu_onto_agy(
            home=bridge_home,
            remote_url=remote_url,
            bearer_token=bearer_token,
            dry_run=dry_run,
            unregister=unbridge_agy,
        )
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            cmd_preview = " ".join(result.get("command") or [])
            if result.get("ok"):
                if result.get("dry_run"):
                    print(f"[dry-run] {cmd_preview}")
                else:
                    verb = "Unregistered" if unbridge_agy else "Registered"
                    print(f"{verb} MCP server {result['name']!r} via agy: {cmd_preview}")
                    if result.get("stdout", "").strip():
                        print(result["stdout"].strip())
            else:
                print(f"Error: {result.get('error') or 'agy MCP bridge failed'}", file=sys.stderr)
                if cmd_preview:
                    print(f"Command: {cmd_preview}", file=sys.stderr)
        return 0 if result.get("ok") else 1

    if add_presets:
        install_results: list[dict[str, Any]] = []
        has_install_error = False
        for preset_name in add_presets:
            try:
                res = install_mcp_preset(home, preset_name, overwrite=overwrite)
                res["ok"] = True
                install_results.append(res)
            except ValueError as exc:
                has_install_error = True
                install_results.append(
                    {
                        "preset": str(preset_name),
                        "status": "error",
                        "changed": False,
                        "ok": False,
                        "error": str(exc),
                    }
                )
        if not probe and not show_presets:
            payload_install: dict[str, Any] = {
                "ok": not has_install_error,
                "config_path": str(home / MCP_CONFIG_FILENAME),
                "results": install_results,
            }
            if as_json:
                print(json.dumps(payload_install, ensure_ascii=False, indent=2))
            else:
                for item in install_results:
                    if not item.get("ok"):
                        print(f"Error [{item['preset']}]: {item['error']}", file=sys.stderr)
                    elif item["status"] == "already_exists":
                        print(
                            f"  - {item['preset']}: already configured "
                            "(pass --overwrite to replace)"
                        )
                    else:
                        extra = []
                        if item.get("backup_path"):
                            extra.append(f"backup={item['backup_path']}")
                        if item.get("sandbox_dir"):
                            extra.append(f"sandbox={item['sandbox_dir']}")
                        suffix = f" ({', '.join(extra)})" if extra else ""
                        print(f"  - {item['preset']}: {item['status']}{suffix}")
            return 1 if has_install_error else 0
        if has_install_error:
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "config_path": str(home / MCP_CONFIG_FILENAME),
                            "results": install_results,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for item in install_results:
                    if not item.get("ok"):
                        print(f"Error [{item['preset']}]: {item['error']}", file=sys.stderr)
            return 1

    if show_presets:
        presets = list_community_presets(home=home)
        payload_presets: dict[str, Any] = {
            "config_path": str(home / MCP_CONFIG_FILENAME),
            "presets": presets,
        }
        if as_json:
            print(json.dumps(payload_presets, ensure_ascii=False, indent=2))
        else:
            print(f"Curated zero-API-key community MCP presets ({len(presets)}):")
            for item in presets:
                badge = "installed" if item["installed"] else "available"
                cmd_str = " ".join([item["command"], *item["args"]]).strip()
                print(f"  - {item['name']} [{badge}]: {item['description']}")
                print(f"      cmd: {cmd_str}")
        return 0

    manager = McpClientManager(home=home)
    config_path = manager.config_path
    config_exists = config_path.is_file()
    configs = manager.load_config()
    config_error = manager.last_config_error

    if not probe:
        server_entries = [
            {
                "name": cfg.name,
                "command": cfg.command,
                "args": list(cfg.args),
                "cwd": cfg.cwd,
                "disabled": cfg.disabled,
                "timeout_sec": cfg.timeout_sec,
            }
            for cfg in configs
        ]
        payload: dict[str, Any] = {
            "config_path": str(config_path),
            "config_exists": config_exists,
            "config_error": config_error,
            "probed": False,
            "servers": server_entries,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if config_error:
                print(f"Error: {config_error}", file=sys.stderr)
            elif not config_exists:
                print(f"MCP config not found: {config_path} (0 servers configured)")
            elif not server_entries:
                print(f"MCP config {config_path} has 0 servers configured.")
            else:
                print(f"MCP servers configured in {config_path}:")
                for entry in server_entries:
                    state = "disabled" if entry["disabled"] else "enabled"
                    cmd_str = " ".join([entry["command"], *entry["args"]]).strip()
                    print(f"  - {entry['name']} [{state}]: {cmd_str}")
        return 1 if config_error else 0

    total_start = time.perf_counter()
    probed_servers: list[dict[str, Any]] = []
    has_failure = bool(config_error)
    try:
        for cfg in configs:
            if cfg.disabled:
                status = McpServerStatus(
                    name=cfg.name,
                    connected=False,
                    disabled=True,
                )
                entry = status.to_dict()
                entry["elapsed_ms"] = 0.0
                probed_servers.append(entry)
                continue

            t0 = time.perf_counter()
            client = McpStdioClient(cfg)
            try:
                client.connect()
                tools = client.list_tools()
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                status = McpServerStatus(
                    name=cfg.name,
                    connected=True,
                    disabled=False,
                    tools=[t.name for t in tools],
                    error=None,
                    server_info=dict(client.server_info),
                    protocol_version=client.protocol_version,
                )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                has_failure = True
                status = McpServerStatus(
                    name=cfg.name,
                    connected=False,
                    disabled=False,
                    tools=[],
                    error=str(exc),
                )
            finally:
                client.close()

            entry = status.to_dict()
            entry["elapsed_ms"] = elapsed_ms
            probed_servers.append(entry)
    finally:
        manager.close()

    total_elapsed_ms = round((time.perf_counter() - total_start) * 1000.0, 2)
    payload = {
        "config_path": str(config_path),
        "config_exists": config_exists,
        "config_error": config_error,
        "probed": True,
        "elapsed_ms": total_elapsed_ms,
        "servers": probed_servers,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if config_error:
            print(f"Error: {config_error}", file=sys.stderr)
        elif not config_exists:
            print(f"MCP config not found: {config_path} (0 servers probed)")
        elif not probed_servers:
            print(f"MCP config {config_path} has 0 servers to probe.")
        else:
            print(f"MCP probe results ({total_elapsed_ms:.1f} ms total):")
            for entry in probed_servers:
                if entry["disabled"]:
                    print(f"  - {entry['name']}: disabled")
                elif entry["connected"]:
                    tools_preview = ", ".join(entry["tools"]) if entry["tools"] else "(none)"
                    print(
                        f"  - {entry['name']}: OK ({entry['elapsed_ms']:.1f} ms, "
                        f"{entry['tool_count']} tools: {tools_preview})"
                    )
                else:
                    print(
                        f"  - {entry['name']}: FAILED ({entry['elapsed_ms']:.1f} ms) - {entry['error']}"
                    )
    return 1 if has_failure else 0


def _cmd_mcp_serve(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    """Run the outbound stdio MCP server exposing Shinobu capabilities."""
    del args
    return int(run_mcp_stdio_server(home=kernel.home))


def _build_console_url(base_url: str, token: str) -> str:
    """Compose loopback Web console URL passing the Bearer token only in the URL fragment."""
    clean_base = base_url.rstrip("/")
    encoded_token = urllib.parse.quote(token, safe="")
    return f"{clean_base}/#token={encoded_token}"


def _cmd_ui(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    from shinobu_core.ui.client import DaemonClient, EventStreamWorker, discover_endpoint, validate_daemon_url

    as_json = bool(getattr(args, "json", False))
    no_open = bool(getattr(args, "no_open", False))
    auto_start = bool(getattr(args, "start", False))
    remote_url = str(
        getattr(args, "remote_url", None) or os.environ.get("SHINOBU_REMOTE_URL") or ""
    ).strip()
    remote_token = str(
        getattr(args, "token", None)
        or os.environ.get("SHINOBU_CLOUD_TOKEN")
        or os.environ.get("SHINOBU_TOKEN")
        or ""
    ).strip()

    if remote_url:
        try:
            base_url = validate_daemon_url(remote_url, allow_remote=True)
        except ValueError as exc:
            if as_json:
                print(
                    json.dumps(
                        {"status": "error", "remote": True, "error": str(exc)},
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not remote_token:
            msg = "Remote mode requires a non-empty Bearer token (--token, $SHINOBU_CLOUD_TOKEN, or $SHINOBU_TOKEN)."
            if as_json:
                print(
                    json.dumps(
                        {"status": "error", "remote": True, "error": msg},
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1
        console_url = _build_console_url(base_url, remote_token)
        opened = False if no_open else bool(webbrowser.open(console_url))
        if as_json:
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "url": base_url,
                        "console_url": console_url,
                        "remote": True,
                        "reused": True,
                        "started": False,
                        "opened": opened,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Shinobu Web Console: {console_url}")
            if not no_open:
                print("Opened in default browser." if opened else "Open the URL above in your browser.")
        return 0

    existing = discover_endpoint(kernel.home)
    if existing is not None:
        base_url = str(existing["url"])
        token = str(existing["token"])
        console_url = _build_console_url(base_url, token)
        opened = False if no_open else bool(webbrowser.open(console_url))
        if as_json:
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "url": base_url,
                        "console_url": console_url,
                        "reused": True,
                        "started": False,
                        "opened": opened,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Shinobu Web Console: {console_url}")
            if not no_open:
                print("Opened in default browser." if opened else "Open the URL above in your browser.")
        return 0

    if not auto_start:
        hint = "No running Shinobu daemon found. Start one with 'shinobu serve' or run 'shinobu ui --start'."
        if as_json:
            print(
                json.dumps(
                    {
                        "status": "not_running",
                        "reused": False,
                        "started": False,
                        "opened": False,
                        "hint": hint,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(hint, file=sys.stderr)
        return 1

    daemon, store = build_daemon(
        kernel,
        engine_name=getattr(args, "engine", None),
        host=LOOPBACK_HOST,
        port=int(getattr(args, "port", DEFAULT_SERVE_PORT)),
        interval_sec=float(getattr(args, "interval", 30.0)),
    )

    stop_event = threading.Event()
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            stop_event.set()

        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, OSError, AttributeError):
            prev_sigterm = None

    try:
        info = daemon.start(start_heartbeat=True)
        base_url = str(info["url"])
        token = str(info["token"])
        console_url = _build_console_url(base_url, token)
        opened = False if no_open else bool(webbrowser.open(console_url))
        if as_json:
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "url": base_url,
                        "console_url": console_url,
                        "reused": False,
                        "started": True,
                        "opened": opened,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Shinobu daemon started on {base_url}")
            print(f"Shinobu Web Console: {console_url}")
            if not no_open:
                print("Opened in default browser." if opened else "Open the URL above in your browser.")
            print("Press Ctrl+C to stop daemon.")
        sys.stdout.flush()
        while not stop_event.is_set() and daemon.is_running:
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError, AttributeError):
                pass
        daemon.stop()
        store.close()
    return 0


def _cmd_pet(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    from shinobu_core.ui.avatar import AVATAR_ENV_VAR
    from shinobu_core.ui.client import DaemonClient, EventStreamWorker, discover_endpoint, validate_daemon_url
    from shinobu_core.ui.pet_window import PySide6NotInstalledError, run_pet_shell

    avatar_dir = getattr(args, "avatar_dir", None)
    remote_url = str(
        getattr(args, "remote_url", None) or os.environ.get("SHINOBU_REMOTE_URL") or ""
    ).strip()
    remote_token = str(
        getattr(args, "token", None)
        or os.environ.get("SHINOBU_CLOUD_TOKEN")
        or os.environ.get("SHINOBU_TOKEN")
        or ""
    ).strip()
    owned_daemon: DaemonServer | None = None
    owned_store: MemoryStore | None = None
    had_avatar_env = AVATAR_ENV_VAR in os.environ
    prev_avatar_env = os.environ.get(AVATAR_ENV_VAR)

    try:
        if avatar_dir:
            os.environ[AVATAR_ENV_VAR] = str(avatar_dir)

        allow_remote = False
        if remote_url:
            try:
                base_url = validate_daemon_url(remote_url, allow_remote=True)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            if not remote_token:
                print(
                    "Error: Remote mode requires a non-empty Bearer token (--token, $SHINOBU_CLOUD_TOKEN, or $SHINOBU_TOKEN).",
                    file=sys.stderr,
                )
                return 1
            token = remote_token
            allow_remote = True
        else:
            existing = discover_endpoint(kernel.home)
            if existing is not None:
                base_url = str(existing["url"])
                token = str(existing["token"])
            else:
                owned_daemon, owned_store = build_daemon(
                    kernel,
                    engine_name=getattr(args, "engine", None),
                    host=LOOPBACK_HOST,
                    port=int(getattr(args, "port", DEFAULT_SERVE_PORT)),
                    interval_sec=float(getattr(args, "interval", 30.0)),
                )
                info = owned_daemon.start(start_heartbeat=True)
                base_url = str(info["url"])
                token = str(info["token"])

        console_url = _build_console_url(base_url, token)

        def _open_web() -> None:
            webbrowser.open(console_url)

        pet_kwargs: dict[str, Any] = {
            "home": kernel.home,
            "url": base_url,
            "token": token,
            "open_web_cb": _open_web,
            "avatar_dir": avatar_dir,
        }
        if allow_remote:
            pet_kwargs["allow_remote"] = True

        return int(run_pet_shell(**pet_kwargs))
    except PySide6NotInstalledError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(
            'Install optional desktop UI support with: pip install "shinobu-core[desktop]" (or: pip install PySide6)',
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if avatar_dir:
            if had_avatar_env and prev_avatar_env is not None:
                os.environ[AVATAR_ENV_VAR] = prev_avatar_env
            else:
                os.environ.pop(AVATAR_ENV_VAR, None)
        if owned_daemon is not None:
            owned_daemon.stop()
        if owned_store is not None:
            owned_store.close()


def _cmd_status(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    home = kernel.home
    soul_dir = home / "soul"
    db_path = db_path_for(home)
    active_ep = _find_active_episode_id(db_path)
    endpoint = _read_endpoint_metadata(home)

    status_data: dict[str, Any] = {
        "home": str(home),
        "soul_initialized": (soul_dir / "SOUL.md").exists(),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "active_episode_id": active_ep,
        "default_engine": resolve_engine_name(None),
        "plugins_disabled": plugins_disabled(),
        "plugins": [
            {
                "name": rec.name,
                "status": rec.status,
                "source": rec.source,
                "error": rec.error,
            }
            for rec in kernel.plugin_records
        ],
        "registered_engines": sorted(kernel.engines.keys()),
        "registered_sensors": sorted(kernel.sensors.keys()),
        "daemon": endpoint,
    }

    if getattr(args, "json", False):
        print(json.dumps(status_data, ensure_ascii=False, indent=2))
    else:
        print(f"Shinobu home      : {status_data['home']}")
        print(f"Soul initialized  : {status_data['soul_initialized']}")
        print(f"Memory DB         : {status_data['db_path']} (exists={status_data['db_exists']})")
        print(f"Active episode    : {status_data['active_episode_id'] or '(none)'}")
        print(f"Default engine    : {status_data['default_engine']}")
        print(f"Plugins disabled  : {status_data['plugins_disabled']}")
        if status_data["plugins"]:
            summary = ", ".join(
                f"{p['name']}={p['status']}" for p in status_data["plugins"]
            )
            print(f"Plugins           : {summary}")
        else:
            print("Plugins           : (none)")
        if status_data["registered_sensors"]:
            print(f"Sensors           : {', '.join(status_data['registered_sensors'])}")
        if endpoint is not None:
            print(f"Daemon            : {endpoint.get('url', '(unknown)')} (pid={endpoint.get('pid')})")
        else:
            print("Daemon            : (stopped)")
    return 0


def _find_spec_safe(module_name: str) -> Any | None:
    """Safely check whether a dotted module spec exists without raising on missing parents."""
    try:
        return importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        return None


def _inspect_agy_cli_settings() -> dict[str, Any]:
    """Collect a read-only, secret-free summary of `~/.gemini/antigravity-cli/settings.json`."""
    fallback: dict[str, Any] = {
        "settings_present": False,
        "tool_permission": None,
        "allow_non_workspace_access": None,
    }
    try:
        settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        if not settings_path.is_file():
            return fallback
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(raw, dict):
        return {
            "settings_present": True,
            "tool_permission": None,
            "allow_non_workspace_access": None,
        }

    perms_block = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
    raw_tool_perm = raw.get("toolPermission")
    if raw_tool_perm is None:
        raw_tool_perm = raw.get("tool_permission")
    if raw_tool_perm is None:
        raw_tool_perm = perms_block.get("toolPermission")
    if raw_tool_perm is None:
        raw_tool_perm = perms_block.get("tool_permission")
    tool_permission = (
        raw_tool_perm.strip()
        if isinstance(raw_tool_perm, str) and raw_tool_perm.strip()
        else None
    )

    raw_non_ws = raw.get("allowNonWorkspaceAccess")
    if raw_non_ws is None:
        raw_non_ws = raw.get("allow_non_workspace_access")
    if raw_non_ws is None:
        raw_non_ws = perms_block.get("allowNonWorkspaceAccess")
    if raw_non_ws is None:
        raw_non_ws = perms_block.get("allow_non_workspace_access")
    allow_non_workspace_access = raw_non_ws if isinstance(raw_non_ws, bool) else None

    return {
        "settings_present": True,
        "tool_permission": tool_permission,
        "allow_non_workspace_access": allow_non_workspace_access,
    }


def collect_doctor_report(kernel: ShinobuKernel) -> dict[str, Any]:
    """Collect a secret-free diagnostic report for native Antigravity drivers and runtime health."""
    home = kernel.home.expanduser().resolve()
    soul_dir = home / "soul"
    db_path = db_path_for(home)
    runtime_dir = (home / "runtime").resolve()
    endpoint = _read_endpoint_metadata(home)

    sdk_available = _find_spec_safe("google.antigravity") is not None
    agy_mode = os.environ.get("SHINOBU_AGY_MODE", "").strip() or "gemini"
    model = os.environ.get("SHINOBU_MODEL", "").strip() or None
    base_url_configured = bool(os.environ.get("SHINOBU_BASE_URL", "").strip())
    model_path_configured = bool(os.environ.get("SHINOBU_MODEL_PATH", "").strip())

    agy_bin_name = os.environ.get("SHINOBU_AGY_BIN", "").strip() or "agy"
    agy_bin_path = shutil.which(agy_bin_name, path=_augmented_agy_path())
    cli_available = agy_bin_path is not None
    agy_cli_settings = _inspect_agy_cli_settings()

    shinobu_api_key_present = bool(os.environ.get("SHINOBU_API_KEY", "").strip())
    gemini_api_key_present = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    any_api_key_present = shinobu_api_key_present or gemini_api_key_present

    sdk_ready = sdk_available and (
        any_api_key_present
        or agy_mode != "gemini"
        or base_url_configured
        or model_path_configured
    )

    if sdk_ready:
        recommended_engine = "agy_sdk"
    elif cli_available:
        recommended_engine = "agy_cli"
    elif base_url_configured:
        recommended_engine = "openai_compat"
    else:
        recommended_engine = "mock"

    plugin_records = [
        {
            "name": rec.name,
            "status": rec.status,
            "source": rec.source,
            "error": rec.error,
        }
        for rec in kernel.plugin_records
    ]
    loaded_plugins = sum(1 for rec in plugin_records if rec["status"] == "loaded")
    failed_plugins = len(plugin_records) - loaded_plugins

    from shinobu_core.runtime.classifier import (
        _DISABLED_ENV_VALUES as _CLF_DISABLED_VALUES,
        _VALID_BACKENDS as _CLF_VALID_BACKENDS,
        _infer_default_api_model,
        _read_dotenv_classifier_vars,
    )

    clf_dotenv = _read_dotenv_classifier_vars(home)

    def _clf_cfg(name: str, default: str = "") -> str:
        if name in os.environ:
            return os.environ[name].strip()
        return clf_dotenv.get(name, default).strip()

    clf_enabled_raw = _clf_cfg("SHINOBU_CLASSIFIER_ENABLED", "1").lower()
    clf_backend_raw = _clf_cfg("SHINOBU_CLASSIFIER_BACKEND", "auto").lower() or "auto"
    clf_enabled = (
        clf_enabled_raw not in _CLF_DISABLED_VALUES
        and clf_backend_raw not in _CLF_DISABLED_VALUES
        and clf_backend_raw != "none"
    )
    clf_backend = clf_backend_raw if clf_backend_raw in _CLF_VALID_BACKENDS else "auto"
    clf_min_effort = _clf_cfg("SHINOBU_CLASSIFIER_MIN_EFFORT", "low") or "low"
    clf_max_effort = _clf_cfg("SHINOBU_CLASSIFIER_MAX_EFFORT", "high") or "high"
    clf_base_url = _clf_cfg("SHINOBU_CLASSIFIER_BASE_URL") or os.environ.get(
        "SHINOBU_BASE_URL", ""
    ).strip()
    clf_api_key = _clf_cfg("SHINOBU_CLASSIFIER_API_KEY") or os.environ.get(
        "SHINOBU_API_KEY", ""
    ).strip()
    clf_api_configured = bool(clf_base_url)
    clf_api_model = _clf_cfg("SHINOBU_CLASSIFIER_MODEL") or (
        _infer_default_api_model(clf_base_url, clf_api_key) if clf_api_configured else ""
    )
    raw_doc_idle = os.environ.get("SHINOBU_EPISODE_IDLE_ARCHIVE_SEC", "").strip()
    try:
        doc_idle_archive_sec = float(raw_doc_idle) if raw_doc_idle else 1800.0
    except ValueError:
        doc_idle_archive_sec = 1800.0
    if doc_idle_archive_sec < 0.0:
        doc_idle_archive_sec = 0.0
    clf_ready = bool(
        clf_enabled
        and (
            (clf_backend in ("auto", "api") and clf_api_configured and bool(clf_api_model))
            or (clf_backend in ("auto", "agy") and sdk_available)
        )
    )

    official_install_hints: dict[str, str] = {
        "agy_sdk": "pip install google-antigravity",
        "agy_cli": "Install the Antigravity CLI (`agy`) and ensure it is on PATH, or set SHINOBU_AGY_BIN.",
        "credentials": "Export GEMINI_API_KEY or SHINOBU_API_KEY (or set SHINOBU_AGY_MODE for local/custom endpoints).",
        "quick_start": f'shinobu chat --engine {recommended_engine} --once "hello"',
    }

    return {
        "home": str(home),
        "soul_initialized": (soul_dir / "SOUL.md").exists(),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "default_engine": resolve_engine_name(None),
        "drivers": {
            "agy_sdk": {
                "available": sdk_available,
                "ready": sdk_ready,
                "mode": agy_mode,
                "model": model,
                "base_url_configured": base_url_configured,
                "model_path_configured": model_path_configured,
            },
            "agy_cli": {
                "available": cli_available,
                "ready": cli_available,
                "binary": agy_bin_name,
                "resolved_path": agy_bin_path,
                "model": model,
                "settings_present": agy_cli_settings["settings_present"],
                "tool_permission": agy_cli_settings["tool_permission"],
                "allow_non_workspace_access": agy_cli_settings["allow_non_workspace_access"],
            },
        },
        "classifier": {
            "enabled": clf_enabled,
            "ready": clf_ready,
            "backend": clf_backend,
            "min_effort": clf_min_effort,
            "max_effort": clf_max_effort,
            "api_base_url_configured": clf_api_configured,
            "api_model": clf_api_model,
            "agy_sdk_available": sdk_available,
            "episode_idle_archive_sec": doc_idle_archive_sec,
        },
        "credentials": {
            "shinobu_api_key": {"present": shinobu_api_key_present},
            "gemini_api_key": {"present": gemini_api_key_present},
            "any_api_key_present": any_api_key_present,
            "daemon_token_present": bool(endpoint and endpoint.get("token_present")),
        },
        "isolation": {
            "runtime_dir": str(runtime_dir),
            "runtime_dir_exists": runtime_dir.exists(),
            "agy_sdk_save_dir": str((runtime_dir / "agy_sdk_save").resolve()),
            "agy_app_data_dir": str((runtime_dir / "agy_app_data").resolve()),
            "agy_sdk_session_map": str((runtime_dir / "agy_sdk_sessions.json").resolve()),
            "agy_cli_workspace": str((runtime_dir / "agy_workspace").resolve()),
            "agy_cli_session_map": str((runtime_dir / "agy_cli_sessions.json").resolve()),
        },
        "plugins_disabled": plugins_disabled(),
        "plugins": {
            "total": len(plugin_records),
            "loaded": loaded_plugins,
            "failed": failed_plugins,
            "healthy": failed_plugins == 0,
            "records": plugin_records,
        },
        "registered_engines": sorted(kernel.engines.keys()),
        "registered_sensors": sorted(kernel.sensors.keys()),
        "daemon": endpoint,
        "recommended_engine": recommended_engine,
        "official_install_hints": official_install_hints,
    }


def _cmd_doctor(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    report = collect_doctor_report(kernel)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sdk = report["drivers"]["agy_sdk"]
        cli_drv = report["drivers"]["agy_cli"]
        creds = report["credentials"]
        iso = report["isolation"]
        plug = report["plugins"]
        print(f"Shinobu home       : {report['home']}")
        print(f"Soul initialized   : {report['soul_initialized']}")
        print(f"Memory DB          : {report['db_path']} (exists={report['db_exists']})")
        print(
            f"Driver agy_sdk     : available={sdk['available']} ready={sdk['ready']} "
            f"(mode={sdk['mode']}, model={sdk['model'] or '(default)'})"
        )
        print(
            f"Driver agy_cli     : available={cli_drv['available']} "
            f"(bin={cli_drv['binary']}, resolved={cli_drv['resolved_path'] or '(not found)'})"
        )
        print(
            f"Credentials        : SHINOBU_API_KEY={creds['shinobu_api_key']['present']}, "
            f"GEMINI_API_KEY={creds['gemini_api_key']['present']}"
        )
        print(f"Isolation root     : {iso['runtime_dir']} (exists={iso['runtime_dir_exists']})")
        print(
            f"Plugins            : disabled={report['plugins_disabled']} "
            f"loaded={plug['loaded']}/{plug['total']} failed={plug['failed']} healthy={plug['healthy']}"
        )
        print(f"Recommended engine : {report['recommended_engine']}")
        print("Official hints     :")
        for key, hint in report["official_install_hints"].items():
            print(f"  - {key}: {hint}")
    return 0


def build_parser(kernel: ShinobuKernel | None = None) -> argparse.ArgumentParser:
    """Build the top-level `shinobu` ArgumentParser and attach plugin CLI hooks."""
    parser = argparse.ArgumentParser(
        prog="shinobu",
        description="Shinobu microkernel desktop companion CLI.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Override Shinobu data directory (default: $SHINOBU_HOME or ~/.shinobu).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # init
    p_init = subparsers.add_parser("init", help="Initialize soul templates and SQLite memory store.")
    p_init.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    p_init.set_defaults(func=_cmd_init)

    # chat
    p_chat = subparsers.add_parser("chat", help="Chat with Shinobu (single-turn or interactive).")
    p_chat.add_argument(
        "--engine",
        default=None,
        help="Engine adapter name (default: $SHINOBU_ENGINE or mock).",
    )
    p_chat.add_argument(
        "--once",
        default=None,
        metavar="TEXT",
        help="Send a single message non-interactively and exit.",
    )
    p_chat.set_defaults(func=_cmd_chat)

    # sleep
    p_sleep = subparsers.add_parser("sleep", help="Consolidate and close the active episode.")
    p_sleep.add_argument(
        "--engine",
        default=None,
        help="Engine adapter used for consolidation (default: $SHINOBU_ENGINE or mock).",
    )
    p_sleep.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override journal date stamp for consolidation.",
    )
    p_sleep.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    p_sleep.set_defaults(func=_cmd_sleep)

    # serve
    p_serve = subparsers.add_parser("serve", help="Run the 127.0.0.1 loopback HTTP/SSE daemon.")
    p_serve.add_argument(
        "--host",
        default=LOOPBACK_HOST,
        help="Bind host (only 127.0.0.1 is allowed).",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help=f"Loopback TCP port (0 for ephemeral, default: {DEFAULT_SERVE_PORT}).",
    )
    p_serve.add_argument(
        "--engine",
        default=None,
        help="Engine adapter name (default: $SHINOBU_ENGINE or mock).",
    )
    p_serve.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Heartbeat tick interval in seconds (default: 30.0).",
    )
    p_serve.add_argument(
        "--token",
        default=None,
        help="Optional explicit Bearer token (default: $SHINOBU_TOKEN or random per-launch token).",
    )
    p_serve.add_argument(
        "--cloud-sandbox",
        default=None,
        help="Enable 7x24 CloudSandboxTerminal restricted to this workspace directory (default: $SHINOBU_SANDBOX_DIR).",
    )
    p_serve.set_defaults(func=_cmd_serve)

    # ui
    p_ui = subparsers.add_parser(
        "ui",
        help="Open or print the local Web Memory Journal console URL.",
    )
    p_ui.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help=f"Loopback TCP port when starting a daemon (default: {DEFAULT_SERVE_PORT}).",
    )
    p_ui.add_argument(
        "--engine",
        default=None,
        help="Engine adapter name when starting a daemon (default: $SHINOBU_ENGINE or mock).",
    )
    p_ui.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Heartbeat tick interval in seconds when starting a daemon (default: 30.0).",
    )
    p_ui.add_argument(
        "--start",
        action="store_true",
        help="Start a local loopback daemon in the foreground if none is running.",
    )
    p_ui.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open a web browser automatically.",
    )
    p_ui.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    p_ui.add_argument(
        "--remote-url",
        default=None,
        help="Remote HTTPS daemon URL (default: $SHINOBU_REMOTE_URL). Skips local daemon startup.",
    )
    p_ui.add_argument(
        "--token",
        default=None,
        help="Bearer token for --remote-url (default: $SHINOBU_CLOUD_TOKEN or $SHINOBU_TOKEN).",
    )
    p_ui.set_defaults(func=_cmd_ui)

    # pet
    p_pet = subparsers.add_parser(
        "pet",
        help="Launch the transparent PySide6 desktop pet shell.",
    )
    p_pet.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help=f"Loopback TCP port if auto-starting a daemon (default: {DEFAULT_SERVE_PORT}).",
    )
    p_pet.add_argument(
        "--engine",
        default=None,
        help="Engine adapter name if auto-starting a daemon (default: $SHINOBU_ENGINE or mock).",
    )
    p_pet.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Heartbeat tick interval in seconds if auto-starting a daemon (default: 30.0).",
    )
    p_pet.add_argument(
        "--avatar-dir",
        default=None,
        help="Optional local avatar pack directory override.",
    )
    p_pet.add_argument(
        "--remote-url",
        default=None,
        help="Remote HTTPS daemon URL (default: $SHINOBU_REMOTE_URL). Skips local daemon startup.",
    )
    p_pet.add_argument(
        "--token",
        default=None,
        help="Bearer token for --remote-url (default: $SHINOBU_CLOUD_TOKEN or $SHINOBU_TOKEN).",
    )
    p_pet.set_defaults(func=_cmd_pet)

    # compat
    p_compat = subparsers.add_parser(
        "compat",
        help="Print OpenAI-compatible (/v1) and event endpoints for community frontends.",
    )
    p_compat.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    p_compat.set_defaults(func=_cmd_compat)

    # mcp
    p_mcp = subparsers.add_parser(
        "mcp",
        help="Inspect, probe, install community presets, or bridge MCP servers with `agy`.",
    )
    p_mcp.add_argument(
        "--probe",
        action="store_true",
        help="Launch each enabled MCP server, perform handshake/tools discovery, and close immediately.",
    )
    p_mcp.add_argument(
        "--presets",
        action="store_true",
        help="List curated zero-API-key community MCP presets and their install status.",
    )
    p_mcp.add_argument(
        "--add-preset",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Install a curated zero-API-key community MCP preset "
            f"({', '.join(COMMUNITY_MCP_PRESETS.keys())}); repeatable."
        ),
    )
    p_mcp.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing server entry when used with --add-preset.",
    )
    p_mcp.add_argument(
        "--bridge-agy",
        action="store_true",
        help="Register `shinobu mcp-serve` into the local Antigravity (`agy`) MCP registry.",
    )
    p_mcp.add_argument(
        "--unbridge-agy",
        action="store_true",
        help="Remove `shinobu` from the local Antigravity (`agy`) MCP registry.",
    )
    p_mcp.add_argument(
        "--remote-url",
        default=None,
        help="Register a remote Shinobu Streamable HTTP MCP endpoint URL (e.g. https://<host>/mcp) with `agy`.",
    )
    p_mcp.add_argument(
        "--bearer-token",
        default=None,
        help="Optional Bearer token for --remote-url (defaults to $SHINOBU_TOKEN when --remote-url is set).",
    )
    p_mcp.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the `agy mcp` bridge command without executing it.",
    )
    p_mcp.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    p_mcp.set_defaults(func=_cmd_mcp)

    # mcp-serve
    p_mcp_serve = subparsers.add_parser(
        "mcp-serve",
        help="Run Shinobu outbound MCP server over stdio for external AI clients.",
    )
    p_mcp_serve.set_defaults(func=_cmd_mcp_serve)

    # telegram
    p_telegram = subparsers.add_parser(
        "telegram",
        help="Run the four-lock isolated Telegram Bot bridge wired to the local Shinobu daemon.",
    )
    p_telegram.add_argument(
        "--bot-token",
        default=None,
        help="Telegram Bot API token (default: $SHINOBU_TELEGRAM_BOT_TOKEN).",
    )
    p_telegram.add_argument(
        "--chat-id",
        default=None,
        help="Single whitelisted private chat ID (default: $SHINOBU_TELEGRAM_CHAT_ID).",
    )
    p_telegram.add_argument(
        "--daemon-url",
        default=None,
        help=f"Shinobu daemon base URL (default: $SHINOBU_DAEMON_URL or endpoint.json or http://{LOOPBACK_HOST}:{DEFAULT_SERVE_PORT}).",
    )
    p_telegram.add_argument(
        "--token",
        default=None,
        help="Shinobu daemon Bearer token (default: $SHINOBU_TOKEN or endpoint.json).",
    )
    p_telegram.add_argument(
        "--api-base",
        default=None,
        help="Telegram Bot API base URL (default: https://api.telegram.org).",
    )
    p_telegram.add_argument(
        "--approval-poll-sec",
        type=float,
        default=2.0,
        help="Interval in seconds to poll pending L1 approvals and push Inline Keyboard prompts (default: 2.0).",
    )
    p_telegram.add_argument(
        "--chat-timeout-sec",
        type=float,
        default=None,
        help="Timeout in seconds for /chat calls from Telegram bridge to daemon (env: SHINOBU_TELEGRAM_CHAT_TIMEOUT, default: 600.0).",
    )
    p_telegram.set_defaults(func=_cmd_telegram)

    # node
    p_node = subparsers.add_parser(
        "node",
        help="Run the home-PC edge node agent (registers local engine/exec/bubble worker with cloud daemon).",
    )
    p_node.add_argument(
        "--cloud-url",
        default=None,
        help="Cloud Shinobu daemon URL over Tailscale/HTTPS (default: $SHINOBU_CLOUD_URL).",
    )
    p_node.add_argument(
        "--cloud-token",
        default=None,
        help="Cloud Shinobu daemon Bearer token (default: $SHINOBU_CLOUD_TOKEN or $SHINOBU_TOKEN).",
    )
    p_node.add_argument(
        "--node-token",
        default=None,
        help="Bearer token protecting this edge node (default: $SHINOBU_NODE_TOKEN or random).",
    )
    p_node.add_argument(
        "--worker-id",
        default="home_pc",
        help="Worker ID advertised to cloud registry (default: home_pc).",
    )
    p_node.add_argument(
        "--bind-host",
        default=LOOPBACK_HOST,
        help="Loopback or Tailscale/private IP to bind (0.0.0.0 and public IPs are rejected; default: 127.0.0.1).",
    )
    p_node.add_argument(
        "--port",
        type=int,
        default=18792,
        help="Edge node HTTP port (default: 18792).",
    )
    p_node.add_argument(
        "--advertise-url",
        default=None,
        help="Reachable URL advertised to cloud daemon (default: $SHINOBU_NODE_ADVERTISE_URL or http://<bind-host>:<port>).",
    )
    p_node.add_argument(
        "--engine",
        default=None,
        help="Local engine adapter exposed to cloud hybrid router (default: $SHINOBU_NODE_ENGINE or agy_cli; use 'none' to disable).",
    )
    p_node.add_argument(
        "--allowed-root",
        action="append",
        default=None,
        help="Whitelisted workspace root for /node/exec (repeatable; default: current working directory).",
    )
    p_node.add_argument(
        "--airi-bubble-url",
        default="",
        help="Optional local AIRI/bubble webhook URL for /node/bubble forwarding (default: $SHINOBU_AIRI_BUBBLE_URL).",
    )
    p_node.add_argument(
        "--heartbeat-sec",
        type=float,
        default=15.0,
        help="Heartbeat interval to cloud registry in seconds (default: 15.0).",
    )
    p_node.add_argument(
        "--effort",
        default=None,
        help="Reasoning effort passed to local agy_cli engine (default: $SHINOBU_AGY_EFFORT or low).",
    )
    p_node.add_argument(
        "--agy-mode",
        default=None,
        help="Optional agy_cli agent mode such as 'auto' (default: $SHINOBU_AGY_MODE).",
    )
    p_node.add_argument(
        "--no-persistent",
        action="store_true",
        default=False,
        help="Disable persistent agy_cli stream-json worker process (or set SHINOBU_AGY_PERSISTENT=0).",
    )
    p_node.add_argument(
        "--agy-add-dir",
        action="append",
        default=None,
        help="Additional workspace directory passed to local agy_cli via --add-dir (repeatable; merges with $SHINOBU_AGY_ADD_DIRS).",
    )
    p_node.add_argument(
        "--agy-skip-permissions",
        action="store_true",
        default=False,
        help="Pass --dangerously-skip-permissions to local agy_cli (or set SHINOBU_AGY_SKIP_PERMISSIONS=1).",
    )
    p_node.add_argument(
        "--no-presence",
        action="store_true",
        default=False,
        help="Disable local idle/active presence reporting in edge heartbeats (or set SHINOBU_EDGE_PRESENCE=0).",
    )
    p_node.set_defaults(func=_cmd_node)

    # status
    p_status = subparsers.add_parser("status", help="Inspect Shinobu home, plugins, and daemon status.")
    p_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    p_status.set_defaults(func=_cmd_status)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Diagnose native Antigravity drivers, credentials, isolation dirs, and plugins.",
    )
    p_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    p_doctor.set_defaults(func=_cmd_doctor)

    # tts-gateway
    p_tts_gw = subparsers.add_parser(
        "tts-gateway",
        help="Run or configure the optional local OpenAI-compatible dual-mode TTS gateway.",
    )
    from shinobu_core.cloud.tts_gateway import register_tts_gateway_arguments

    register_tts_gateway_arguments(p_tts_gw)
    p_tts_gw.set_defaults(func=_cmd_tts_gateway)

    # avatar
    p_avatar = subparsers.add_parser(
        "avatar",
        help="Scan local differential avatar sprites, generate manifest.json, and activate a pack.",
    )
    p_avatar.add_argument(
        "--source",
        default=None,
        help="Local directory containing state/mood sprite files (.svg/.png/.webp/.gif).",
    )
    p_avatar.add_argument(
        "--name",
        default="custom",
        help="Avatar pack name under <home>/avatars/<name>/ (default: custom).",
    )
    p_avatar.add_argument(
        "--force",
        action="store_true",
        help="Overwrite differing target sprites after backing up originals under <home>/_versions/.",
    )
    p_avatar.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    p_avatar.set_defaults(func=_cmd_avatar)

    if kernel is not None:
        kernel.apply_cli(subparsers)

    return parser


def _cmd_tts_gateway(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    from shinobu_core.cloud.tts_gateway import run_tts_gateway_cli

    return run_tts_gateway_cli(args, home=kernel.home)


def _cmd_avatar(args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    import json
    import sys

    from shinobu_core.ui.avatar_installer import DEFAULT_AVATAR_PACK_NAME, apply_avatar_pack

    pack_name = getattr(args, "name", None) or DEFAULT_AVATAR_PACK_NAME
    try:
        result = apply_avatar_pack(
            source=getattr(args, "source", None),
            name=pack_name,
            home=kernel.home,
            force=bool(getattr(args, "force", False)),
        )
    except (FileNotFoundError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    states = result.get("states", {})
    missing = result.get("missing_states", [])
    copied = result.get("copied", [])
    unchanged = result.get("unchanged", [])
    protected = result.get("protected", [])
    backed_up = result.get("backed_up", [])
    ignored = result.get("ignored_files", [])

    print(f"Avatar pack '{result['pack_name']}' -> {result['pack_dir']}")
    print(f"Manifest: {result['manifest_path']} ({len(states)} state sprites mapped)")
    if result.get("activated"):
        print(f"Activated: {result['active_path']}")
    else:
        print("Activated: False (no valid sprites found; pass --force to activate empty pack)")
    print(
        f"Summary: copied={len(copied)}, unchanged={len(unchanged)}, "
        f"protected={len(protected)}, backed_up={len(backed_up)}, ignored={len(ignored)}"
    )
    if missing:
        print(f"Missing core states ({len(missing)}): {', '.join(missing)}")
    if backed_up:
        print("Backups:")
        for item in backed_up:
            print(f"  - {item}")
    return 0


def _invoke_handler(handler: Any, args: argparse.Namespace, kernel: ShinobuKernel) -> int:
    """Invoke a built-in `(args, kernel)` or plugin `(args)` CLI handler cleanly."""
    sig = inspect.signature(handler)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(params) >= 2:
        result = handler(args, kernel)
    elif len(params) == 1:
        result = handler(args)
    else:
        result = handler()
    return int(result) if isinstance(result, int) else 0


def _preparse_home(argv: Sequence[str] | None) -> str | None:
    """Extract `--home` before constructing the kernel so plugins see the target home."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--home", default=None)
    ns, _unknown = pre.parse_known_args(list(argv) if argv is not None else None)
    return ns.home


def main(
    argv: Sequence[str] | None = None,
    *,
    kernel: ShinobuKernel | None = None,
) -> int:
    """Entry point for the `shinobu` console script and `python -m shinobu_core`."""
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    home_override = _preparse_home(raw_argv)

    active_kernel = kernel
    if active_kernel is None:
        active_kernel = ShinobuKernel(home=resolve_home(home_override))
        active_kernel.discover_plugins()
    elif home_override is not None:
        active_kernel.home = resolve_home(home_override)

    parser = build_parser(active_kernel)
    args = parser.parse_args(raw_argv)

    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 0

    return _invoke_handler(handler, args, active_kernel)


__all__ = [
    "DEFAULT_DB_RELATIVE",
    "DEFAULT_ENGINE_NAME",
    "DEFAULT_SERVE_PORT",
    "ENGINE_ENV",
    "build_daemon",
    "build_parser",
    "db_path_for",
    "main",
    "resolve_engine_name",
    "resolve_home",
]
