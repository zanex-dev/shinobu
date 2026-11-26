# -*- coding: utf-8 -*-
"""Lazy-loaded Google Antigravity SDK and CLI engine adapters."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import inspect
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from shinobu_core.protocol.engine import ToolSpec, TurnChunk, TurnRequest

_VALID_SDK_MODES = ("gemini", "openai_local", "litert")

_DEFAULT_BUILTIN_TOOL_NAMES = (
    "list_directory",
    "search_directory",
    "find_file",
    "view_file",
    "create_file",
    "edit_file",
    "run_command",
    "ask_question",
    "start_subagent",
    "generate_image",
    "search_web",
    "read_url_content",
)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from raw model output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Failed to extract JSON object from output: {cleaned[:200]!r}") from None
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed


def _normalize_tool_call(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Normalize tool call payloads into {id, name, arguments}."""
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    name = str(fn.get("name") or raw.get("name") or "")
    call_id = str(raw.get("id") or f"call_agy_{index + 1}")
    raw_args = fn.get("arguments", raw.get("arguments", {}))
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            args: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(text)
                args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
            except json.JSONDecodeError:
                args = {"_raw": text}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        args = {"_raw": raw_args}
    return {"id": call_id, "name": name, "arguments": args}


def _filter_kwargs_for_callable(target: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Filter keyword arguments to match target signature unless **kwargs is accepted."""
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return dict(kwargs)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


_SDK_USAGE_FIELDS = (
    "prompt_token_count",
    "cached_content_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "total_token_count",
)


def _load_session_map(session_map_path: str | None) -> dict[str, str]:
    """Load persisted {episode_id: conversation_id} JSON mapping if available."""
    if not session_map_path:
        return {}
    path = Path(session_map_path)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and k and v
    }


def _save_session_map(
    session_map_path: str | None,
    mapping: dict[str, str],
    lock: threading.Lock,
) -> None:
    """Atomically merge and persist {episode_id: conversation_id} JSON mapping."""
    if not session_map_path:
        return
    path = Path(session_map_path)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = _load_session_map(session_map_path)
        merged.update(mapping)
        payload = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


_SYNC_STATE_FILENAME = "agy_cli_sync_state.json"
_MAX_SYNCED_TAIL_LEN = 64
_SYNCABLE_ROLES = ("user", "assistant")


def _derive_sync_state_path(session_map_path: str | None) -> str | None:
    """Derive sibling agy_cli_sync_state.json path from session_map_path."""
    if not session_map_path:
        return None
    return str(Path(session_map_path).with_name(_SYNC_STATE_FILENAME))


def _turn_fingerprint(role: str, content: str) -> list[str]:
    """Return stable [role, sha256(stripped_content)[:16]] fingerprint."""
    digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]
    return [role, digest]


def _extract_syncable_history(
    history: Sequence[dict[str, Any]] | None,
) -> list[tuple[str, str]]:
    """Extract (role, stripped_content) tuples for non-empty user/assistant turns."""
    entries: list[tuple[str, str]] = []
    if not history:
        return entries
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in _SYNCABLE_ROLES:
            continue
        raw_content = msg.get("content")
        if not isinstance(raw_content, str):
            continue
        content = raw_content.strip()
        if not content:
            continue
        entries.append((role, content))
    return entries


def _sanitize_synced_tail(raw_tail: Any) -> list[list[str]]:
    """Validate and cap a persisted fingerprint tail to <= _MAX_SYNCED_TAIL_LEN."""
    if not isinstance(raw_tail, list):
        return []
    cleaned: list[list[str]] = []
    for item in raw_tail:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
            and item[0] in _SYNCABLE_ROLES
            and item[1]
        ):
            cleaned.append([item[0], item[1]])
    if len(cleaned) > _MAX_SYNCED_TAIL_LEN:
        cleaned = cleaned[-_MAX_SYNCED_TAIL_LEN:]
    return cleaned


def _load_sync_state(sync_state_path: str | None) -> dict[str, list[list[str]]]:
    """Load persisted {episode_id: [[role, digest], ...]} JSON sync state."""
    if not sync_state_path:
        return {}
    path = Path(sync_state_path)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[list[str]]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        tail = _sanitize_synced_tail(value)
        if tail:
            result[key] = tail
    return result


def _save_sync_state(
    sync_state_path: str | None,
    mapping: dict[str, list[list[str]]],
    lock: threading.Lock,
) -> None:
    """Atomically merge and persist {episode_id: [[role, digest], ...]} sync state."""
    if not sync_state_path:
        return
    path = Path(sync_state_path)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = _load_sync_state(sync_state_path)
        for key, tail in mapping.items():
            if not key:
                continue
            cleaned = _sanitize_synced_tail(tail)
            if cleaned:
                merged[key] = cleaned
        payload = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _compute_history_delta(
    history: Sequence[dict[str, Any]] | None,
    synced_tail: Sequence[Sequence[str]] | None,
) -> list[tuple[str, str]]:
    """Return unsynced (role, content) turns after the most recent synced anchor."""
    entries = _extract_syncable_history(history)
    if not entries or not synced_tail:
        return []

    norm_tail: list[tuple[str, str]] = [
        (str(fp[0]), str(fp[1]))
        for fp in synced_tail
        if isinstance(fp, (list, tuple)) and len(fp) == 2 and fp[0] and fp[1]
    ]
    if not norm_tail:
        return []

    hist_fps = [tuple(_turn_fingerprint(role, content)) for role, content in entries]
    n = len(hist_fps)
    anchor_idx: int | None = None

    # 1) Prefer matching the last 2 consecutive fingerprints of synced_tail.
    if len(norm_tail) >= 2 and n >= 2:
        last_two = (norm_tail[-2], norm_tail[-1])
        for i in range(n - 1, 0, -1):
            if (hist_fps[i - 1], hist_fps[i]) == last_two:
                anchor_idx = i
                break

    # 2) Fall back to any 2 consecutive fingerprints in synced_tail.
    if anchor_idx is None and len(norm_tail) >= 2 and n >= 2:
        tail_pairs = {
            (norm_tail[j], norm_tail[j + 1]) for j in range(len(norm_tail) - 1)
        }
        for i in range(n - 1, 0, -1):
            if (hist_fps[i - 1], hist_fps[i]) in tail_pairs:
                anchor_idx = i
                break

    # 3) Fall back to matching the last 1 fingerprint of synced_tail.
    if anchor_idx is None:
        last_one = norm_tail[-1]
        for i in range(n - 1, -1, -1):
            if hist_fps[i] == last_one:
                anchor_idx = i
                break

    # 4) Fall back to any single fingerprint in synced_tail.
    if anchor_idx is None:
        tail_set = set(norm_tail)
        for i in range(n - 1, -1, -1):
            if hist_fps[i] in tail_set:
                anchor_idx = i
                break

    if anchor_idx is None:
        return entries
    return entries[anchor_idx + 1 :]


def _extract_chunk_text(piece: Any) -> str:
    """Extract incremental text delta from a streamed SDK token or chunk object."""
    if piece is None:
        return ""
    if isinstance(piece, str):
        return piece
    for attr in ("text", "delta", "content"):
        val = getattr(piece, attr, None)
        if isinstance(val, str):
            return val
    return str(piece)


def _extract_sdk_usage(agent: Any, response: Any) -> dict[str, Any] | None:
    """Extract token usage from agent.conversation.total_usage with response.usage fallback."""
    conv = getattr(agent, "conversation", None)
    total_usage = getattr(conv, "total_usage", None) if conv is not None else None
    if total_usage is not None:
        extracted: dict[str, Any] = {}
        if isinstance(total_usage, dict):
            for field in _SDK_USAGE_FIELDS:
                val = total_usage.get(field)
                if val is not None:
                    extracted[field] = val
            if not extracted:
                extracted = dict(total_usage)
        else:
            for field in _SDK_USAGE_FIELDS:
                val = getattr(total_usage, field, None)
                if val is not None:
                    extracted[field] = val
        if extracted:
            return extracted

    raw_usage = getattr(response, "usage", None)
    if isinstance(raw_usage, dict):
        return dict(raw_usage)
    if raw_usage is not None:
        extracted = {}
        for field in _SDK_USAGE_FIELDS:
            val = getattr(raw_usage, field, None)
            if val is not None:
                extracted[field] = val
        if extracted:
            return extracted
    return None


def _extract_cli_conversation_id(event: dict[str, Any]) -> str | None:
    """Capture a real agy conversation_id/session_id from a stream-json event."""
    for key in ("conversation_id", "session_id"):
        val = event.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for container_key in ("metadata", "session", "conversation", "init", "step_update", "result"):
        sub = event.get(container_key)
        if isinstance(sub, dict):
            for key in ("conversation_id", "session_id", "id"):
                val = sub.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _augmented_agy_path() -> str:
    """Return PATH augmented with standard user-level agy binary locations."""
    current = os.environ.get("PATH", "")
    entries = [part for part in current.split(os.pathsep) if part]
    seen = {os.path.normcase(os.path.normpath(p)) for p in entries}

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin")
    try:
        candidates.append(Path.home() / ".local" / "bin")
    except RuntimeError:
        pass

    for candidate in candidates:
        candidate_str = str(candidate)
        norm = os.path.normcase(os.path.normpath(candidate_str))
        if norm not in seen:
            entries.append(candidate_str)
            seen.add(norm)
    return os.pathsep.join(entries)


def _wrap_tool_spec(spec: ToolSpec) -> Any:
    """Wrap a ToolSpec preserving spec.handler's explicit signature for SDK reflection."""
    handler = spec.handler

    @functools.wraps(handler)
    def _tool_callable(*args: Any, **kwargs: Any) -> str:
        return handler(*args, **kwargs)

    try:
        _tool_callable.__signature__ = inspect.signature(handler)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        pass
    if getattr(handler, "__annotations__", None):
        _tool_callable.__annotations__ = dict(handler.__annotations__)
    _tool_callable.__name__ = spec.name
    _tool_callable.__qualname__ = spec.name
    _tool_callable.__doc__ = spec.description or getattr(handler, "__doc__", "")
    setattr(_tool_callable, "tool_spec", spec)
    setattr(_tool_callable, "parameters_schema", dict(spec.parameters))
    return _tool_callable


class AgySdkEngine:
    """EngineAdapter backed by the optional `google-antigravity` Python SDK (lazy imported)."""

    def __init__(
        self,
        mode: str = "gemini",
        model: str | None = None,
        base_url: str | None = None,
        model_path: str | None = None,
        save_dir: str | None = None,
        *,
        api_key: str | None = None,
        disable_builtin_tools: bool = True,
        app_data_dir: str | None = None,
        session_map_path: str | None = None,
        name: str = "agy_sdk",
    ) -> None:
        if mode not in _VALID_SDK_MODES:
            raise ValueError(f"Unsupported mode {mode!r}; expected one of {_VALID_SDK_MODES}")
        self.name: str = name
        self.mode: str = mode
        self.model: str | None = model
        self.base_url: str | None = base_url
        self.model_path: str | None = model_path
        self.save_dir: str | None = save_dir
        self.api_key: str | None = api_key
        self.disable_builtin_tools: bool = disable_builtin_tools
        self.app_data_dir: str | None = (
            os.path.abspath(os.path.expanduser(str(app_data_dir)))
            if app_data_dir is not None
            else None
        )
        self.session_map_path: str | None = (
            str(session_map_path) if session_map_path is not None else None
        )
        self._session_lock = threading.Lock()
        self._episode_conversations: dict[str, str] = _load_session_map(self.session_map_path)
        self._last_sdk_tool_calls_count: int = 0

    @staticmethod
    def _load_sdk() -> tuple[Any, Any]:
        """Lazily import `google.antigravity` and its `types` submodule."""
        try:
            sdk = importlib.import_module("google.antigravity")
        except ImportError as exc:
            raise RuntimeError(
                "The optional 'google-antigravity' SDK is not installed. "
                "Install it with: pip install 'shinobu-core[agy-sdk]' (or pip install google-antigravity)."
            ) from exc
        try:
            types_mod = importlib.import_module("google.antigravity.types")
        except ImportError:
            types_mod = sdk
        return sdk, types_mod

    @staticmethod
    def _load_policy_module() -> Any | None:
        """Lazily import optional `google.antigravity.hooks.policy` if installed."""
        try:
            return importlib.import_module("google.antigravity.hooks.policy")
        except ImportError:
            return None

    @staticmethod
    def _collect_disabled_builtin_tools(sdk: Any, types_mod: Any) -> list[Any]:
        builtin_enum = getattr(types_mod, "BuiltinTools", None) or getattr(sdk, "BuiltinTools", None)
        if builtin_enum is not None:
            try:
                return list(builtin_enum)
            except TypeError:
                pass
        return list(_DEFAULT_BUILTIN_TOOL_NAMES)

    def _can_persist_conversations(self, use_persistence: bool) -> bool:
        return bool(
            use_persistence
            and (
                self.save_dir is not None
                or self.app_data_dir is not None
                or self.session_map_path is not None
            )
        )

    def _record_conversation_id(self, episode_id: str, conv_id: Any) -> None:
        if not episode_id or not conv_id:
            return
        conv_str = str(conv_id).strip()
        if not conv_str:
            return
        if self._episode_conversations.get(episode_id) != conv_str:
            self._episode_conversations[episode_id] = conv_str
            _save_session_map(
                self.session_map_path,
                self._episode_conversations,
                self._session_lock,
            )

    @staticmethod
    def _extract_agent_conversation_id(agent: Any) -> str | None:
        conv_id = getattr(agent, "conversation_id", None)
        if conv_id:
            return str(conv_id)
        conv = getattr(agent, "conversation", None)
        if conv is not None:
            for attr in ("id", "conversation_id"):
                val = getattr(conv, attr, None)
                if val:
                    return str(val)
        return None

    def _resolve_config_class(self, sdk: Any) -> Any:
        if self.mode == "openai_local":
            return getattr(sdk, "LocalOpenAIAgentConfig", sdk.LocalAgentConfig)
        if self.mode == "litert":
            return getattr(sdk, "LiteRTAgentConfig", sdk.LocalAgentConfig)
        return sdk.LocalAgentConfig

    def _build_config(
        self,
        sdk: Any,
        types_mod: Any,
        req: TurnRequest,
        *,
        use_persistence: bool = True,
    ) -> Any:
        config_cls = self._resolve_config_class(sdk)
        kwargs: dict[str, Any] = {}

        if self.model is not None:
            kwargs["model"] = self.model
        if self.api_key is not None:
            kwargs["api_key"] = self.api_key
        if self.mode == "openai_local" and self.base_url is not None:
            kwargs["base_url"] = self.base_url
        if self.mode == "litert" and self.model_path is not None:
            kwargs["model_path"] = self.model_path
        if self.app_data_dir is not None:
            kwargs["app_data_dir"] = os.path.abspath(os.path.expanduser(self.app_data_dir))

        if req.system_prefix:
            custom_si_cls = getattr(types_mod, "CustomSystemInstructions", None) or getattr(
                sdk, "CustomSystemInstructions", None
            )
            if custom_si_cls is not None:
                kwargs["system_instructions"] = custom_si_cls(text=req.system_prefix)
            else:
                kwargs["system_instructions"] = req.system_prefix

        if use_persistence and self.save_dir is not None:
            kwargs["save_dir"] = str(self.save_dir)

        if self._can_persist_conversations(use_persistence) and req.episode_id:
            saved_conv_id = self._episode_conversations.get(req.episode_id)
            if saved_conv_id:
                kwargs["conversation_id"] = saved_conv_id

        if self.disable_builtin_tools:
            kwargs["disabled_tools"] = self._collect_disabled_builtin_tools(sdk, types_mod)
            policy_mod = self._load_policy_module()
            if policy_mod is not None:
                deny_all_fn = getattr(policy_mod, "deny_all", None)
                allow_fn = getattr(policy_mod, "allow", None)
                if callable(deny_all_fn):
                    policies: list[Any] = [deny_all_fn()]
                    if callable(allow_fn) and req.tools:
                        for spec in req.tools:
                            policies.append(allow_fn(spec.name))
                    kwargs["policies"] = policies

        if req.tools:
            kwargs["tools"] = [_wrap_tool_spec(spec) for spec in req.tools]
        elif self.disable_builtin_tools:
            kwargs["tools"] = []

        filtered = _filter_kwargs_for_callable(config_cls, kwargs)
        return config_cls(**filtered)

    @staticmethod
    def _compose_prompt(req: TurnRequest, *, has_persisted_conversation: bool) -> str:
        if has_persisted_conversation or not req.history:
            return req.user_text
        lines: list[str] = []
        for msg in req.history:
            role = str(msg.get("role") or "user")
            content = str(msg.get("content") or "")
            if content:
                lines.append(f"[{role}] {content}")
        if req.user_text:
            lines.append(f"[user] {req.user_text}")
        return "\n".join(lines)

    async def _run_turn_async(
        self,
        sdk: Any,
        config: Any,
        req: TurnRequest,
        emit_queue: queue.Queue[TurnChunk | Exception | None],
        *,
        use_persistence: bool,
    ) -> None:
        can_persist = self._can_persist_conversations(use_persistence)
        has_persisted = bool(can_persist and req.episode_id and req.episode_id in self._episode_conversations)
        prompt = self._compose_prompt(req, has_persisted_conversation=has_persisted)
        agent_cls = sdk.Agent

        async with agent_cls(config) as agent:
            response = await agent.chat(prompt)

            if can_persist and req.episode_id:
                conv_id = self._extract_agent_conversation_id(agent)
                if conv_id:
                    self._record_conversation_id(req.episode_id, conv_id)

            emitted_any = False
            stream_fn = getattr(response, "stream", None)
            if callable(stream_fn):
                stream_obj = stream_fn()
                if inspect.isawaitable(stream_obj):
                    stream_obj = await stream_obj
                if hasattr(stream_obj, "__aiter__"):
                    async for piece in stream_obj:
                        delta = _extract_chunk_text(piece)
                        if delta:
                            emitted_any = True
                            emit_queue.put(TurnChunk(delta=delta))
                else:
                    for piece in stream_obj:
                        delta = _extract_chunk_text(piece)
                        if delta:
                            emitted_any = True
                            emit_queue.put(TurnChunk(delta=delta))
            elif hasattr(response, "__aiter__"):
                async for piece in response:
                    delta = _extract_chunk_text(piece)
                    if delta:
                        emitted_any = True
                        emit_queue.put(TurnChunk(delta=delta))

            if not emitted_any:
                text_attr = getattr(response, "text", response)
                if callable(text_attr):
                    text_val = text_attr()
                    if inspect.isawaitable(text_val):
                        text_val = await text_val
                else:
                    text_val = text_attr
                text_str = str(text_val or "")
                if text_str:
                    emit_queue.put(TurnChunk(delta=text_str))

            # Drain response.tool_calls for telemetry only; SDK already executes tools internally,
            # so never emit TurnChunk.tool_calls back to Orchestrator for duplicate execution.
            sdk_tool_calls_count = 0
            raw_calls = getattr(response, "tool_calls", None)
            if callable(raw_calls):
                raw_calls = raw_calls()
                if inspect.isawaitable(raw_calls):
                    raw_calls = await raw_calls
            if raw_calls is not None and not isinstance(raw_calls, (str, bytes)):
                if hasattr(raw_calls, "__aiter__"):
                    async for _call in raw_calls:
                        sdk_tool_calls_count += 1
                elif hasattr(raw_calls, "__iter__"):
                    for _call in raw_calls:
                        sdk_tool_calls_count += 1
            self._last_sdk_tool_calls_count = sdk_tool_calls_count

            if can_persist and req.episode_id:
                conv_id = self._extract_agent_conversation_id(agent)
                if conv_id:
                    self._record_conversation_id(req.episode_id, conv_id)

            usage = _extract_sdk_usage(agent, response)
            emit_queue.put(TurnChunk(delta="", tool_calls=[], usage=usage, done=True))

    def _stream_with_config(self, req: TurnRequest, *, use_persistence: bool) -> Iterator[TurnChunk]:
        sdk, types_mod = self._load_sdk()
        config = self._build_config(sdk, types_mod, req, use_persistence=use_persistence)
        emit_queue: queue.Queue[TurnChunk | Exception | None] = queue.Queue()

        def _worker() -> None:
            try:
                asyncio.run(
                    self._run_turn_async(
                        sdk,
                        config,
                        req,
                        emit_queue,
                        use_persistence=use_persistence,
                    )
                )
            except Exception as exc:
                emit_queue.put(exc)
            finally:
                emit_queue.put(None)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        try:
            while True:
                item = emit_queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            thread.join(timeout=5.0)

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        yield from self._stream_with_config(req, use_persistence=True)

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        req = TurnRequest(
            episode_id=f"json_{uuid.uuid4().hex[:8]}",
            system_prefix=system,
            user_text=prompt,
            tools=[],
        )
        parts = [chunk.delta for chunk in self._stream_with_config(req, use_persistence=False) if chunk.delta]
        return _extract_json_object("".join(parts))


_NON_RETRYABLE_CLI_ERROR_TOKENS = (
    "429",
    "rate limit",
    "quota",
    "resource_exhausted",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "invalid_api_key",
    "authentication",
)

_TRANSIENT_CLI_ERROR_TOKENS = (
    "eof",
    "connection reset",
    "connection refused",
    "broken pipe",
    "tls handshake timeout",
    "i/o timeout",
    "timed out",
    "timeout",
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "temporarily unavailable",
    "forcibly closed",
    "connection aborted",
)

_STALE_CONVERSATION_SUBJECT_TOKENS = ("conversation", "session")
_STALE_CONVERSATION_REASON_TOKENS = (
    "not found",
    "expired",
    "invalid",
    "corrupt",
    "does not exist",
)


def _is_transient_cli_error(msg: str) -> bool:
    lowered = (msg or "").lower()
    if any(token in lowered for token in _NON_RETRYABLE_CLI_ERROR_TOKENS):
        return False
    return any(token in lowered for token in _TRANSIENT_CLI_ERROR_TOKENS)


def _is_stale_conversation_error(msg: str) -> bool:
    lowered = (msg or "").lower()
    return any(token in lowered for token in _STALE_CONVERSATION_SUBJECT_TOKENS) and any(
        token in lowered for token in _STALE_CONVERSATION_REASON_TOKENS
    )


class AgyCliEngine:
    """EngineAdapter invoking the external `agy` CLI with `--output-format stream-json`."""

    def __init__(
        self,
        binary: str = "agy",
        cwd: str | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
        agent_mode: str | None = None,
        persistent: bool = False,
        session_map_path: str | None = None,
        timeout: float = 120.0,
        extra_args: Sequence[str] = (),
        add_dirs: Sequence[str | Path] = (),
        skip_permissions: bool = False,
        settings: str | Mapping[str, Any] | None = None,
        name: str = "agy_cli",
        max_retries: int = 2,
        retry_backoff_sec: float = 0.8,
    ) -> None:
        self.name: str = name
        self.binary: str = binary
        self.cwd: str | None = cwd
        self.model: str | None = model
        self.effort: str | None = str(effort).strip() or None if effort is not None else None
        self.agent_mode: str | None = (
            str(agent_mode).strip() or None if agent_mode is not None else None
        )
        self.persistent: bool = bool(persistent)
        self.session_map_path: str | None = (
            str(session_map_path) if session_map_path is not None else None
        )
        self.timeout: float = float(timeout)
        self.extra_args: tuple[str, ...] = tuple(extra_args)
        normalized_add_dirs: list[str] = []
        seen_add_dirs: set[str] = set()
        for raw_dir in add_dirs or ():
            item = str(raw_dir).strip()
            if item and item not in seen_add_dirs:
                seen_add_dirs.add(item)
                normalized_add_dirs.append(item)
        self.add_dirs: tuple[str, ...] = tuple(normalized_add_dirs)
        self.skip_permissions: bool = bool(skip_permissions)
        if settings is None:
            self.settings: str | None = None
        elif isinstance(settings, Mapping):
            self.settings = json.dumps(
                dict(settings),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            self.settings = str(settings).strip() or None
        self.max_retries: int = max(0, int(max_retries))
        self.retry_backoff_sec: float = max(0.0, float(retry_backoff_sec))
        self._session_lock = threading.Lock()
        self._episode_conversations: dict[str, str] = _load_session_map(self.session_map_path)
        self._sync_state_path: str | None = _derive_sync_state_path(self.session_map_path)
        self._sync_state_lock = threading.Lock()
        self._episode_synced_tails: dict[str, list[list[str]]] = _load_sync_state(
            self._sync_state_path
        )
        self._turn_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._persistent_proc: subprocess.Popen[str] | None = None
        self._persistent_episode_id: str | None = None
        self._persistent_seeded: bool = False
        self._persistent_stderr_buf: list[str] = []
        self._persistent_stderr_thread: threading.Thread | None = None

    def _resolve_binary(self) -> str:
        resolved = shutil.which(self.binary, path=_augmented_agy_path())
        if not resolved:
            raise RuntimeError(
                f"AGY CLI executable {self.binary!r} was not found on PATH. "
                "Please install the Antigravity CLI ('agy') or pass an explicit binary path."
            )
        return resolved

    def _record_conversation_id(self, episode_id: str, conv_id: str) -> None:
        if not episode_id or not conv_id:
            return
        if self._episode_conversations.get(episode_id) != conv_id:
            self._episode_conversations[episode_id] = conv_id
            _save_session_map(
                self.session_map_path,
                self._episode_conversations,
                self._session_lock,
            )

    def _get_synced_tail(self, episode_id: str) -> list[list[str]]:
        if not episode_id:
            return []
        with self._sync_state_lock:
            tail = self._episode_synced_tails.get(episode_id)
            return [list(fp) for fp in tail] if tail else []

    def _record_synced_tail(
        self,
        episode_id: str,
        history: Sequence[dict[str, Any]] | None,
        user_text: str,
        reply_text: str,
    ) -> None:
        if not episode_id or not reply_text or not reply_text.strip():
            return
        entries = _extract_syncable_history(history)
        if entries:
            new_tail = [_turn_fingerprint(role, content) for role, content in entries]
        else:
            with self._sync_state_lock:
                new_tail = [
                    list(fp) for fp in self._episode_synced_tails.get(episode_id, [])
                ]
        if user_text and user_text.strip():
            new_tail.append(_turn_fingerprint("user", user_text))
        new_tail.append(_turn_fingerprint("assistant", reply_text))
        if len(new_tail) > _MAX_SYNCED_TAIL_LEN:
            new_tail = new_tail[-_MAX_SYNCED_TAIL_LEN:]
        with self._sync_state_lock:
            self._episode_synced_tails[episode_id] = new_tail
        _save_sync_state(
            self._sync_state_path,
            {episode_id: new_tail},
            self._sync_state_lock,
        )

    def _clear_synced_tail(self, episode_id: str) -> None:
        if not episode_id:
            return
        with self._sync_state_lock:
            self._episode_synced_tails.pop(episode_id, None)
            if not self._sync_state_path:
                return
            path = Path(self._sync_state_path)
            persisted = _load_sync_state(self._sync_state_path)
            if episode_id not in persisted:
                return
            persisted.pop(episode_id, None)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_text(
                    json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                os.replace(tmp_path, path)
            except OSError:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _clear_conversation_id(self, episode_id: str) -> None:
        if not episode_id:
            return
        try:
            with self._session_lock:
                removed = self._episode_conversations.pop(episode_id, None)
                if removed is None or not self.session_map_path:
                    return
                path = Path(self.session_map_path)
                persisted = _load_session_map(self.session_map_path)
                if episode_id not in persisted:
                    return
                persisted.pop(episode_id, None)
                tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path.write_text(
                        json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                    os.replace(tmp_path, path)
                except OSError:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        finally:
            self._clear_synced_tail(episode_id)

    @staticmethod
    def _compose_prompt(
        req: TurnRequest,
        *,
        has_persisted_conversation: bool = False,
        synced_tail: Sequence[Sequence[str]] | None = None,
    ) -> str:
        if has_persisted_conversation:
            delta_entries = _compute_history_delta(req.history, synced_tail)
            if not delta_entries:
                return req.user_text
            delta_lines = [f"{role}: {content}" for role, content in delta_entries]
            return (
                "[Synced Context from Cloud Brain]\n"
                + "\n".join(delta_lines)
                + f"\n\n[Current User Message]\n{req.user_text}"
            )
        sections: list[str] = []
        if req.system_prefix:
            sections.append(f"[System Instructions]\n{req.system_prefix}")
        if req.history:
            hist_lines = [
                f"{msg.get('role', 'user')}: {msg.get('content', '')}"
                for msg in req.history
                if msg.get("content")
            ]
            if hist_lines:
                sections.append("[History]\n" + "\n".join(hist_lines))
        if req.user_text:
            sections.append(req.user_text)
        return "\n\n".join(sections)

    def _build_command(self, resolved_binary: str, req: TurnRequest) -> list[str]:
        saved_conv_id = self._episode_conversations.get(req.episode_id) if req.episode_id else None
        has_persisted = bool(saved_conv_id)
        synced_tail = self._get_synced_tail(req.episode_id) if has_persisted else None
        prompt_text = self._compose_prompt(
            req,
            has_persisted_conversation=has_persisted,
            synced_tail=synced_tail,
        )
        cmd = [
            resolved_binary,
            "-p",
            prompt_text,
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.agent_mode:
            cmd.extend(["--mode", self.agent_mode])
        for extra_dir in self.add_dirs:
            cmd.extend(["--add-dir", extra_dir])
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.settings:
            cmd.extend(["--settings", self.settings])
        if saved_conv_id:
            cmd.extend(["--conversation", saved_conv_id])
        if self.extra_args:
            cmd.extend(self.extra_args)
        return cmd

    def _build_persistent_command(
        self,
        resolved_binary: str,
        saved_conv_id: str | None = None,
    ) -> list[str]:
        cmd = [
            resolved_binary,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.agent_mode:
            cmd.extend(["--mode", self.agent_mode])
        for extra_dir in self.add_dirs:
            cmd.extend(["--add-dir", extra_dir])
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.settings:
            cmd.extend(["--settings", self.settings])
        if saved_conv_id:
            cmd.extend(["--conversation", saved_conv_id])
        if self.extra_args:
            cmd.extend(self.extra_args)
        return cmd

    @staticmethod
    def _popen_platform_kwargs() -> dict[str, Any]:
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            if creationflags:
                return {"creationflags": creationflags}
        return {}

    @staticmethod
    def _start_stderr_drainer(
        proc: subprocess.Popen[str],
    ) -> tuple[list[str], threading.Thread | None]:
        stderr_stream = getattr(proc, "stderr", None)
        buf: list[str] = []
        if stderr_stream is None:
            return buf, None

        def _drain() -> None:
            try:
                while True:
                    line = stderr_stream.readline()
                    if not line:
                        break
                    buf.append(line)
                    if len(buf) > 64:
                        del buf[:-64]
            except Exception:
                pass

        thread = threading.Thread(target=_drain, name="agy-cli-stderr-drain", daemon=True)
        thread.start()
        return buf, thread

    @staticmethod
    def _terminate_process(
        proc: subprocess.Popen[str] | None,
        *,
        stderr_thread: threading.Thread | None = None,
        wait_timeout: float = 2.0,
    ) -> None:
        if proc is None:
            return
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=wait_timeout)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass
        if (
            stderr_thread is not None
            and stderr_thread.is_alive()
            and stderr_thread is not threading.current_thread()
        ):
            try:
                stderr_thread.join(timeout=0.15)
            except Exception:
                pass
        stderr = getattr(proc, "stderr", None)
        if stderr is not None:
            try:
                stderr.close()
            except Exception:
                pass
        if (
            stderr_thread is not None
            and stderr_thread.is_alive()
            and stderr_thread is not threading.current_thread()
        ):
            try:
                stderr_thread.join(timeout=0.1)
            except Exception:
                pass

    def _reset_persistent_process(
        self,
        expected_proc: subprocess.Popen[str] | None = None,
        *,
        wait_timeout: float = 2.0,
    ) -> str:
        with self._proc_lock:
            proc = self._persistent_proc
            if expected_proc is not None and proc is not expected_proc:
                proc_to_close = expected_proc
                stderr_thread = None
                stderr_buf: list[str] = []
            else:
                proc_to_close = proc
                stderr_thread = self._persistent_stderr_thread
                stderr_buf = self._persistent_stderr_buf
                self._persistent_proc = None
                self._persistent_episode_id = None
                self._persistent_seeded = False
                self._persistent_stderr_thread = None
                self._persistent_stderr_buf = []
        self._terminate_process(
            proc_to_close,
            stderr_thread=stderr_thread,
            wait_timeout=wait_timeout,
        )
        return "".join(stderr_buf).strip()

    def close(self) -> None:
        """Thread-safely terminate and clean up any active persistent CLI subprocess."""
        self._reset_persistent_process()

    def _ensure_persistent_process(
        self,
        resolved_binary: str,
        episode_id: str,
        saved_conv_id: str | None,
    ) -> tuple[subprocess.Popen[str], bool, bool, list[str]]:
        stale_proc: subprocess.Popen[str] | None = None
        stale_stderr_thread: threading.Thread | None = None

        with self._proc_lock:
            proc = self._persistent_proc
            if proc is not None:
                is_alive = False
                try:
                    is_alive = proc.poll() is None
                except Exception:
                    is_alive = False
                if is_alive and self._persistent_episode_id == episode_id:
                    has_persisted = bool(self._persistent_seeded or saved_conv_id)
                    return proc, True, has_persisted, self._persistent_stderr_buf
                stale_proc = proc
                stale_stderr_thread = self._persistent_stderr_thread
                self._persistent_proc = None
                self._persistent_episode_id = None
                self._persistent_seeded = False
                self._persistent_stderr_thread = None
                self._persistent_stderr_buf = []

        if stale_proc is not None:
            self._terminate_process(stale_proc, stderr_thread=stale_stderr_thread)

        cmd = self._build_persistent_command(resolved_binary, saved_conv_id)
        new_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.cwd,
            **self._popen_platform_kwargs(),
        )
        stderr_buf, stderr_thread = self._start_stderr_drainer(new_proc)
        has_persisted = bool(saved_conv_id)
        with self._proc_lock:
            self._persistent_proc = new_proc
            self._persistent_episode_id = episode_id
            self._persistent_seeded = has_persisted
            self._persistent_stderr_buf = stderr_buf
            self._persistent_stderr_thread = stderr_thread
        return new_proc, False, has_persisted, stderr_buf

    def _process_stream_line(
        self,
        raw_line: str,
        req: TurnRequest,
        attempt_state: dict[str, bool],
        collected_tool_calls: list[dict[str, Any]],
        usage_holder: dict[str, Any],
    ) -> tuple[str | None, bool]:
        line = raw_line.strip()
        if not line:
            return None, False
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            attempt_state["emitted_text"] = True
            return line, False

        if not isinstance(event, dict):
            return None, False

        if req.episode_id:
            captured_conv_id = _extract_cli_conversation_id(event)
            if captured_conv_id:
                self._record_conversation_id(req.episode_id, captured_conv_id)

        live_event = str(event.get("event") or "").lower()
        if live_event in ("init", "user"):
            return None, False
        if live_event == "step_update" and isinstance(event.get("step_update"), dict):
            payload = event["step_update"]
            if isinstance(payload.get("usage"), dict):
                usage_holder["usage"] = payload["usage"]
            step_status = str(payload.get("status") or "").upper()
            if step_status == "ERROR":
                message = payload.get("error") or payload.get("message") or line
                raise RuntimeError(f"agy CLI reported error: {message}")
            # Intentionally ignore payload.get("tool_info"): CLI handles internal tools itself.
            step_delta = payload.get("text_delta")
            if not isinstance(step_delta, str):
                step_delta = payload.get("delta")
            if isinstance(step_delta, str) and step_delta:
                attempt_state["emitted_text"] = True
                return step_delta, False
            return None, False
        if live_event == "result" and isinstance(event.get("result"), dict):
            payload = event["result"]
            if isinstance(payload.get("usage"), dict):
                usage_holder["usage"] = payload["usage"]
            res_status = str(payload.get("status") or "").upper()
            if res_status == "ERROR" or (not res_status and payload.get("error")):
                message = payload.get("error") or payload.get("message") or line
                raise RuntimeError(f"agy CLI reported error: {message}")
            if not attempt_state["emitted_text"]:
                fallback = payload.get("response")
                if not isinstance(fallback, str) or not fallback:
                    fallback = payload.get("text")
                if isinstance(fallback, str) and fallback:
                    attempt_state["emitted_text"] = True
                    return fallback, True
            return None, True

        event_type = str(event.get("type") or live_event).lower()
        if event_type == "error":
            err_obj = event.get("error")
            if isinstance(err_obj, dict):
                message = err_obj.get("message") or err_obj.get("error") or line
            else:
                message = event.get("message") or err_obj or line
            raise RuntimeError(f"agy CLI reported error: {message}")

        if isinstance(event.get("usage"), dict):
            usage_holder["usage"] = event["usage"]

        raw_calls = event.get("tool_calls")
        if not raw_calls and isinstance(event.get("tool_call"), dict):
            raw_calls = [event["tool_call"]]
        if raw_calls:
            for item in raw_calls:
                collected_tool_calls.append(
                    _normalize_tool_call(dict(item), len(collected_tool_calls))
                )

        is_terminal = event_type in ("done", "result", "completed")
        delta_text = ""
        if isinstance(event.get("delta"), str):
            delta_text = event["delta"]
        elif isinstance(event.get("text"), str) and not is_terminal:
            delta_text = event["text"]
        elif isinstance(event.get("content"), str):
            delta_text = event["content"]
        elif isinstance(event.get("message"), dict) and isinstance(event["message"].get("content"), str):
            delta_text = event["message"]["content"]
        elif not attempt_state["emitted_text"] and is_terminal:
            fallback = event.get("result") or event.get("text")
            if isinstance(fallback, str):
                delta_text = fallback

        if delta_text:
            attempt_state["emitted_text"] = True
            return delta_text, is_terminal
        return None, is_terminal

    def _stream_single_attempt(
        self,
        resolved_binary: str,
        req: TurnRequest,
        attempt_state: dict[str, bool],
    ) -> Iterator[TurnChunk]:
        cmd = self._build_command(resolved_binary, req)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.cwd,
            **self._popen_platform_kwargs(),
        )

        usage_holder: dict[str, Any] = {"usage": None}
        collected_tool_calls: list[dict[str, Any]] = []
        reply_parts: list[str] = []
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        timer: threading.Timer | None = None
        if self.timeout > 0:
            timer = threading.Timer(self.timeout, _on_timeout)
            timer.daemon = True
            timer.start()

        try:
            assert proc.stdout is not None
            try:
                for raw_line in proc.stdout:
                    if timed_out.is_set():
                        raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")
                    delta_text, _ = self._process_stream_line(
                        raw_line,
                        req,
                        attempt_state,
                        collected_tool_calls,
                        usage_holder,
                    )
                    if delta_text:
                        reply_parts.append(delta_text)
                        yield TurnChunk(delta=delta_text)
            except (ValueError, OSError):
                if timed_out.is_set():
                    raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s") from None
                raise

            if timed_out.is_set():
                raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")

            try:
                returncode = proc.wait(timeout=self.timeout if self.timeout > 0 else None)
            except subprocess.TimeoutExpired:
                timed_out.set()
                raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s") from None

            if timed_out.is_set():
                raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")

            try:
                stderr_text = proc.stderr.read().strip() if proc.stderr is not None else ""
            except (ValueError, OSError):
                if timed_out.is_set():
                    raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s") from None
                raise

            if timed_out.is_set():
                raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")

            if returncode != 0:
                raise RuntimeError(
                    f"agy CLI exited with code {returncode}: {stderr_text or 'no stderr output'}"
                )

            reply_text = "".join(reply_parts).strip()
            if req.episode_id and reply_text:
                self._record_synced_tail(req.episode_id, req.history, req.user_text, reply_text)

            yield TurnChunk(
                delta="",
                tool_calls=collected_tool_calls,
                usage=usage_holder["usage"],
                done=True,
            )
        finally:
            if timer is not None:
                timer.cancel()
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def _stream_persistent_attempt(
        self,
        resolved_binary: str,
        req: TurnRequest,
        attempt_state: dict[str, bool],
    ) -> Iterator[TurnChunk]:
        with self._turn_lock:
            saved_conv_id = (
                self._episode_conversations.get(req.episode_id) if req.episode_id else None
            )
            proc, reused_existing, has_persisted, stderr_buf = self._ensure_persistent_process(
                resolved_binary,
                req.episode_id,
                saved_conv_id,
            )
            attempt_state["reused_persistent"] = reused_existing

            synced_tail = self._get_synced_tail(req.episode_id) if has_persisted else None
            prompt_text = self._compose_prompt(
                req,
                has_persisted_conversation=has_persisted,
                synced_tail=synced_tail,
            )
            payload_line = (
                json.dumps(
                    {"event": "user", "message": {"content": prompt_text}},
                    ensure_ascii=False,
                )
                + "\n"
            )

            usage_holder: dict[str, Any] = {"usage": None}
            collected_tool_calls: list[dict[str, Any]] = []
            reply_parts: list[str] = []
            timed_out = threading.Event()
            turn_completed = False

            def _on_timeout() -> None:
                timed_out.set()
                self._reset_persistent_process(proc, wait_timeout=0.5)

            timer: threading.Timer | None = None
            if self.timeout > 0:
                timer = threading.Timer(self.timeout, _on_timeout)
                timer.daemon = True
                timer.start()

            try:
                if proc.stdin is None or proc.stdout is None:
                    attempt_state["broken_pipe"] = True
                    raise RuntimeError("agy CLI persistent process pipes unavailable (broken pipe)")

                try:
                    proc.stdin.write(payload_line)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    stderr_text = self._reset_persistent_process(proc)
                    if timed_out.is_set():
                        raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s") from None
                    detail = stderr_text or str(exc)
                    attempt_state["broken_pipe"] = True
                    raise RuntimeError(f"agy CLI persistent broken pipe: {detail}") from exc

                try:
                    while True:
                        raw_line = proc.stdout.readline()
                        if timed_out.is_set():
                            raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")
                        if raw_line == "":
                            if proc.poll() is None:
                                try:
                                    proc.wait(timeout=0.5)
                                except Exception:
                                    pass
                            returncode = proc.poll()
                            stderr_text = self._reset_persistent_process(proc)
                            if timed_out.is_set():
                                raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")
                            detail = stderr_text or "".join(stderr_buf).strip()
                            if returncode is not None and returncode != 0:
                                if not detail or _is_transient_cli_error(detail):
                                    attempt_state["broken_pipe"] = True
                                raise RuntimeError(
                                    f"agy CLI exited with code {returncode}: {detail or 'no stderr output'}"
                                )
                            attempt_state["broken_pipe"] = True
                            raise RuntimeError(
                                f"agy CLI persistent stream closed unexpectedly (EOF): "
                                f"{detail or 'no stderr output'}"
                            )

                        delta_text, is_terminal = self._process_stream_line(
                            raw_line,
                            req,
                            attempt_state,
                            collected_tool_calls,
                            usage_holder,
                        )
                        if delta_text:
                            reply_parts.append(delta_text)
                            yield TurnChunk(delta=delta_text)
                        if is_terminal:
                            break
                except (ValueError, OSError) as exc:
                    if timed_out.is_set():
                        raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s") from None
                    stderr_text = self._reset_persistent_process(proc)
                    detail = stderr_text or str(exc)
                    attempt_state["broken_pipe"] = True
                    raise RuntimeError(f"agy CLI persistent broken pipe: {detail}") from exc

                if timed_out.is_set():
                    raise RuntimeError(f"agy CLI timed out after {self.timeout:.1f}s")

                turn_completed = True
                with self._proc_lock:
                    if self._persistent_proc is proc:
                        self._persistent_seeded = True

                reply_text = "".join(reply_parts).strip()
                if req.episode_id and reply_text:
                    self._record_synced_tail(req.episode_id, req.history, req.user_text, reply_text)

                yield TurnChunk(
                    delta="",
                    tool_calls=collected_tool_calls,
                    usage=usage_holder["usage"],
                    done=True,
                )
            finally:
                if timer is not None:
                    timer.cancel()
                if not turn_completed:
                    self._reset_persistent_process(
                        proc,
                        wait_timeout=0.5 if timed_out.is_set() else 2.0,
                    )

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        resolved_binary = self._resolve_binary()
        use_persistent = self.persistent and bool(req.episode_id)
        transient_attempt = 0
        recovered_stale_session = False
        recovered_broken_pipe = False

        while True:
            saved_conv_id = (
                self._episode_conversations.get(req.episode_id) if req.episode_id else None
            )
            attempt_state: dict[str, bool] = {
                "emitted_text": False,
                "reused_persistent": False,
                "broken_pipe": False,
            }
            try:
                if use_persistent:
                    yield from self._stream_persistent_attempt(resolved_binary, req, attempt_state)
                else:
                    yield from self._stream_single_attempt(resolved_binary, req, attempt_state)
                return
            except RuntimeError as exc:
                if attempt_state["emitted_text"]:
                    raise
                err_msg = str(exc)
                if (
                    saved_conv_id
                    and not recovered_stale_session
                    and _is_stale_conversation_error(err_msg)
                ):
                    self._clear_conversation_id(req.episode_id)
                    if use_persistent:
                        self._reset_persistent_process()
                    recovered_stale_session = True
                    continue
                if (
                    use_persistent
                    and attempt_state["reused_persistent"]
                    and attempt_state["broken_pipe"]
                    and not recovered_broken_pipe
                ):
                    recovered_broken_pipe = True
                    continue
                if transient_attempt < self.max_retries and _is_transient_cli_error(err_msg):
                    delay = self.retry_backoff_sec * (2 ** transient_attempt)
                    transient_attempt += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        req = TurnRequest(
            episode_id="",
            system_prefix=system,
            user_text=prompt,
            tools=[],
        )
        parts = [chunk.delta for chunk in self.stream_turn(req) if chunk.delta]
        return _extract_json_object("".join(parts))


__all__ = ["AgyCliEngine", "AgySdkEngine"]
