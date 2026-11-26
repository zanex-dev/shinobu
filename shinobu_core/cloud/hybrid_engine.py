# -*- coding: utf-8 -*-
"""Hybrid cloud/edge router engine with seamless fallback to cloud flagship API."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Iterator, Mapping
import urllib.error
import urllib.parse
import urllib.request

from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.engines.openai_compat import normalize_reasoning_effort
from shinobu_core.protocol.engine import EngineAdapter, ToolSpec, TurnChunk, TurnRequest

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ROUTE_MODE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "hybrid": "auto",
    "cloud": "cloud",
    "api": "cloud",
    "local": "local",
    "agy": "local",
    "edge": "local",
}


def normalize_route_mode(mode: str) -> str:
    """Normalize a hybrid route mode or alias into 'auto', 'cloud', or 'local'."""
    if not isinstance(mode, str):
        raise ValueError(f"unsupported route_mode: {mode!r}")
    key = mode.strip().lower()
    normalized = _ROUTE_MODE_ALIASES.get(key)
    if normalized is None:
        raise ValueError(
            f"unsupported route_mode: {mode!r} (expected auto/hybrid, cloud/api, or local/agy/edge)"
        )
    return normalized


def _elapsed_ms(started_monotonic: float) -> int:
    return int(round(max(0.0, (time.monotonic() - started_monotonic) * 1000.0)))


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    except Exception:
        return False
    return host in _LOOPBACK_HOSTS


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _extract_json_dict(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("remote complete_json response did not decode to a JSON object")
    return parsed


def _normalize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_calls, list):
        return []
    result: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_calls):
        if not isinstance(item, Mapping):
            continue
        fn = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        name = str(fn.get("name") or item.get("name") or "")
        arguments = _parse_arguments(fn.get("arguments", item.get("arguments")))
        result.append(
            {
                "id": str(item.get("id") or f"call_{idx}"),
                "name": name,
                "arguments": arguments,
            }
        )
    return result


class HybridRouterEngine:
    """Routes turns to an online edge brain worker first, falling back seamlessly to cloud."""

    def __init__(
        self,
        cloud_fallback: EngineAdapter,
        registry: WorkerRegistry,
        *,
        primary_capability: str = "brain",
        remote_timeout_sec: float = 90.0,
        connect_timeout_sec: float = 3.0,
        primary_role: str | None = None,
        on_route: Callable[[dict[str, Any]], None] | None = None,
        route_mode: str = "auto",
        name: str = "hybrid",
    ) -> None:
        if cloud_fallback is None:
            raise ValueError("cloud_fallback must not be None")
        if registry is None:
            raise ValueError("registry must not be None")
        timeout = float(remote_timeout_sec)
        if timeout <= 0:
            raise ValueError("remote_timeout_sec must be greater than 0")
        connect_timeout = float(connect_timeout_sec)
        if connect_timeout <= 0:
            raise ValueError("connect_timeout_sec must be greater than 0")
        normalized_route_mode = normalize_route_mode(route_mode)

        self.name: str = str(name)
        self.cloud_fallback: EngineAdapter = cloud_fallback
        self.registry: WorkerRegistry = registry
        self.primary_capability: str = str(primary_capability).strip()
        self.remote_timeout_sec: float = timeout
        self.connect_timeout_sec: float = connect_timeout
        self.primary_role: str | None = primary_role.strip() if isinstance(primary_role, str) and primary_role.strip() else None
        self._on_route = on_route
        self._route_lock = threading.Lock()
        self._route_mode: str = normalized_route_mode
        self._last_route: dict[str, Any] | None = None

    @property
    def model(self) -> str:
        """Proxy active model name from the cloud fallback engine."""
        return str(getattr(self.cloud_fallback, "model", ""))

    @model.setter
    def model(self, value: str) -> None:
        """Proxy runtime model hot-switching to the cloud fallback engine."""
        setattr(self.cloud_fallback, "model", value)

    @property
    def reasoning_effort(self) -> str:
        """Proxy active reasoning_effort from the cloud fallback engine."""
        raw = getattr(self.cloud_fallback, "reasoning_effort", "")
        return normalize_reasoning_effort(str(raw)) if raw else ""

    @reasoning_effort.setter
    def reasoning_effort(self, value: str | None) -> None:
        """Proxy runtime reasoning_effort hot-switching to the cloud fallback engine."""
        if not hasattr(self.cloud_fallback, "reasoning_effort"):
            raise AttributeError("cloud_fallback engine does not support reasoning_effort")
        normalized = normalize_reasoning_effort(value)
        setattr(self.cloud_fallback, "reasoning_effort", normalized)

    @property
    def route_mode(self) -> str:
        """Return active routing policy ('auto', 'cloud', or 'local')."""
        with self._route_lock:
            return self._route_mode

    @route_mode.setter
    def route_mode(self, value: str) -> None:
        """Thread-safely hot-switch routing policy at runtime."""
        normalized = normalize_route_mode(value)
        with self._route_lock:
            self._route_mode = normalized

    @property
    def last_route(self) -> dict[str, Any] | None:
        """Return a snapshot of the most recent routing decision."""
        with self._route_lock:
            return dict(self._last_route) if self._last_route is not None else None

    def _record_route(
        self,
        *,
        mode: str,
        target: str,
        worker_id: str | None = None,
        fallback_reason: str = "",
        route_mode: str | None = None,
        elapsed_ms: int | float | None = None,
    ) -> None:
        with self._route_lock:
            effective_route_mode = route_mode if route_mode is not None else self._route_mode
            elapsed = int(round(max(0.0, float(elapsed_ms)))) if elapsed_ms is not None else 0
            info: dict[str, Any] = {
                "mode": mode,
                "route_mode": effective_route_mode,
                "target": target,
                "worker_id": worker_id,
                "fallback_reason": fallback_reason,
                "elapsed_ms": elapsed,
                "ts": round(time.time(), 3),
            }
            self._last_route = info

        if target == "remote":
            logger.info(
                "hybrid route mode=%s route_mode=%s target=remote worker=%s elapsed_ms=%d",
                mode,
                effective_route_mode,
                worker_id or "-",
                elapsed,
            )
        elif target == "cloud":
            logger.info(
                "hybrid route mode=%s route_mode=%s target=cloud elapsed_ms=%d",
                mode,
                effective_route_mode,
                elapsed,
            )
        else:
            logger.warning(
                "hybrid route mode=%s route_mode=%s target=%s worker=%s elapsed_ms=%d reason=%s",
                mode,
                effective_route_mode,
                target,
                worker_id or "-",
                elapsed,
                fallback_reason or "-",
            )

        if self._on_route is not None:
            try:
                self._on_route(dict(info))
            except Exception:
                pass

    def _select_worker(self) -> WorkerRecord | None:
        worker = self.registry.get_online(self.primary_capability, role=self.primary_role)
        if worker is None or not worker.base_url:
            return None
        return worker

    @staticmethod
    def _worker_turn_url(worker: WorkerRecord) -> str:
        base = worker.base_url.rstrip("/")
        if base.endswith("/node/turn"):
            return base
        return f"{base}/node/turn"

    @staticmethod
    def _build_headers(worker: WorkerRecord, *, accept_sse: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json" if accept_sse else "application/json",
        }
        if worker.token:
            headers["Authorization"] = f"Bearer {worker.token}"
        return headers

    def _open_worker_request(
        self,
        worker: WorkerRecord,
        payload: dict[str, Any],
        *,
        accept_sse: bool,
    ) -> Any:
        url = self._worker_turn_url(worker)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._build_headers(worker, accept_sse=accept_sse),
            method="POST",
        )
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        probe_timeout = min(self.connect_timeout_sec, self.remote_timeout_sec)
        try:
            if host:
                with socket.create_connection((host, port), timeout=probe_timeout):
                    pass
            if _is_loopback_url(url):
                return _NO_PROXY_OPENER.open(request, timeout=self.remote_timeout_sec)
            return urllib.request.urlopen(request, timeout=self.remote_timeout_sec)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = str(exc.reason)
            raise RuntimeError(f"remote worker HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise RuntimeError(f"remote worker connection error: {exc}") from exc

    @staticmethod
    def _serialize_turn_request(
        req: TurnRequest,
        model: str,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        effective_effort = (
            normalize_reasoning_effort(req.reasoning_effort)
            if req.reasoning_effort
            else normalize_reasoning_effort(reasoning_effort)
        )
        payload: dict[str, Any] = {
            "mode": "stream_turn",
            "episode_id": req.episode_id,
            "system_prefix": req.system_prefix,
            "history": [dict(msg) for msg in req.history],
            "user_text": req.user_text,
            "tools": [tool.to_schema() for tool in req.tools if isinstance(tool, ToolSpec)],
            "model": model,
        }
        if effective_effort:
            payload["reasoning_effort"] = effective_effort
        return payload

    @staticmethod
    def _parse_json_turn_chunks(data: Any) -> list[TurnChunk]:
        if not isinstance(data, Mapping):
            raise ValueError("remote turn payload must be a JSON object")
        if data.get("error"):
            raise RuntimeError(f"remote worker returned error: {data.get('error')}")

        if isinstance(data.get("chunks"), list):
            chunks: list[TurnChunk] = []
            saw_done = False
            for item in data["chunks"]:
                if not isinstance(item, Mapping):
                    continue
                done_flag = bool(item.get("done", False))
                saw_done = saw_done or done_flag
                usage = item.get("usage") if isinstance(item.get("usage"), dict) else None
                chunks.append(
                    TurnChunk(
                        delta=str(item.get("delta") or item.get("content") or item.get("text") or ""),
                        tool_calls=_normalize_tool_calls(item.get("tool_calls")),
                        usage=usage,
                        done=done_flag,
                    )
                )
            if chunks and not saw_done:
                chunks.append(TurnChunk(delta="", done=True))
            return chunks

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        tool_calls = _normalize_tool_calls(data.get("tool_calls"))
        text_val = data.get("delta")
        if text_val is None:
            text_val = data.get("content")
        if text_val is None:
            text_val = data.get("text")
        if text_val is None:
            text_val = data.get("reply")

        # Also support OpenAI-style choices payload if a worker proxies it directly.
        if text_val is None and isinstance(data.get("choices"), list) and data["choices"]:
            first = data["choices"][0]
            if isinstance(first, Mapping):
                msg = first.get("message") if isinstance(first.get("message"), Mapping) else {}
                text_val = msg.get("content")
                if not tool_calls:
                    tool_calls = _normalize_tool_calls(msg.get("tool_calls"))

        chunks = []
        if text_val:
            chunks.append(TurnChunk(delta=str(text_val)))
        if chunks or tool_calls or data.get("done") is True:
            chunks.append(TurnChunk(delta="", tool_calls=tool_calls, usage=usage, done=True))
        return chunks

    @staticmethod
    def _parse_sse_event_chunk(event_data: Mapping[str, Any]) -> TurnChunk | None:
        if event_data.get("error"):
            raise RuntimeError(f"remote worker stream error: {event_data.get('error')}")

        usage = event_data.get("usage") if isinstance(event_data.get("usage"), dict) else None
        tool_calls = _normalize_tool_calls(event_data.get("tool_calls"))
        done_flag = bool(event_data.get("done", False))

        delta_val = event_data.get("delta")
        if delta_val is None:
            delta_val = event_data.get("content")
        if delta_val is None:
            delta_val = event_data.get("text")

        if delta_val is None and isinstance(event_data.get("choices"), list) and event_data["choices"]:
            first = event_data["choices"][0]
            if isinstance(first, Mapping):
                delta_obj = first.get("delta") if isinstance(first.get("delta"), Mapping) else {}
                delta_val = delta_obj.get("content")
                if not tool_calls:
                    tool_calls = _normalize_tool_calls(delta_obj.get("tool_calls"))
                if first.get("finish_reason") is not None:
                    done_flag = True

        delta_str = str(delta_val) if delta_val is not None else ""
        if not delta_str and not tool_calls and not done_flag:
            return None
        return TurnChunk(delta=delta_str, tool_calls=tool_calls, usage=usage, done=done_flag)

    def _stream_from_worker(self, worker: WorkerRecord, req: TurnRequest) -> Iterator[TurnChunk]:
        payload = self._serialize_turn_request(
            req,
            self.model,
            reasoning_effort=self.reasoning_effort,
        )
        with self._open_worker_request(worker, payload, accept_sse=True) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                raw_body = resp.read().decode("utf-8", errors="replace").strip()
                if not raw_body:
                    raise RuntimeError("remote worker returned empty body")
                data = json.loads(raw_body)
                chunks = self._parse_json_turn_chunks(data)
                if not chunks:
                    raise RuntimeError("remote worker returned no usable turn chunks")
                yield from chunks
                return

            saw_done = False
            saw_meaningful = False
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    if not saw_done and saw_meaningful:
                        yield TurnChunk(delta="", done=True)
                        saw_done = True
                    break
                parsed = json.loads(data_str)
                if not isinstance(parsed, Mapping):
                    raise ValueError("remote SSE event payload must be a JSON object")
                chunk = self._parse_sse_event_chunk(parsed)
                if chunk is None:
                    continue
                saw_meaningful = True
                if chunk.done:
                    saw_done = True
                yield chunk

            if not saw_meaningful:
                raise RuntimeError("remote worker SSE stream ended before producing output")
            if not saw_done:
                yield TurnChunk(delta="", done=True)

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        """Stream a turn according to route_mode ('auto', 'cloud', or 'local')."""
        started = time.monotonic()
        policy = self.route_mode
        if policy == "cloud":
            self._record_route(
                mode="stream_turn",
                target="cloud",
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            yield from self.cloud_fallback.stream_turn(req)
            return

        worker = self._select_worker()
        if worker is None:
            if policy == "local":
                self._record_route(
                    mode="stream_turn",
                    target="local_error",
                    fallback_reason="no online worker",
                    route_mode=policy,
                    elapsed_ms=_elapsed_ms(started),
                )
                raise RuntimeError("local brain route enabled but no online brain worker is available")
            self._record_route(
                mode="stream_turn",
                target="cloud_fallback",
                fallback_reason="no online worker",
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            yield from self.cloud_fallback.stream_turn(req)
            return

        emitted_first_chunk = False
        try:
            for chunk in self._stream_from_worker(worker, req):
                if not emitted_first_chunk:
                    emitted_first_chunk = True
                    self._record_route(
                        mode="stream_turn",
                        target="remote",
                        worker_id=worker.worker_id,
                        route_mode=policy,
                        elapsed_ms=_elapsed_ms(started),
                    )
                yield chunk
            if emitted_first_chunk:
                return
            raise RuntimeError("remote worker yielded zero chunks")
        except Exception as exc:
            if emitted_first_chunk:
                raise
            if policy == "local":
                self._record_route(
                    mode="stream_turn",
                    target="local_error",
                    worker_id=worker.worker_id,
                    fallback_reason=str(exc),
                    route_mode=policy,
                    elapsed_ms=_elapsed_ms(started),
                )
                raise RuntimeError(f"local brain worker '{worker.worker_id}' failed: {exc}") from exc
            self._record_route(
                mode="stream_turn",
                target="cloud_fallback",
                worker_id=worker.worker_id,
                fallback_reason=str(exc),
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            yield from self.cloud_fallback.stream_turn(req)

    def _complete_json_from_worker(
        self,
        worker: WorkerRecord,
        system: str,
        prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "mode": "complete_json",
            "system": system,
            "prompt": prompt,
            "model": self.model,
        }
        with self._open_worker_request(worker, payload, accept_sse=False) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace").strip()
            if not raw_body:
                raise RuntimeError("remote worker returned empty body for complete_json")
            data = json.loads(raw_body)

        if not isinstance(data, dict):
            raise ValueError("remote complete_json payload must be a JSON object")
        if data.get("error"):
            raise RuntimeError(f"remote worker returned error: {data.get('error')}")
        if isinstance(data.get("result"), dict):
            return data["result"]
        if isinstance(data.get("json"), dict):
            return data["json"]

        for text_key in ("content", "delta", "text", "reply"):
            val = data.get(text_key)
            if isinstance(val, str) and val.strip():
                return _extract_json_dict(val)

        return data

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        """Request structured JSON according to route_mode ('auto', 'cloud', or 'local')."""
        started = time.monotonic()
        policy = self.route_mode
        if policy == "cloud":
            result = self.cloud_fallback.complete_json(system, prompt)
            self._record_route(
                mode="complete_json",
                target="cloud",
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            return result

        worker = self._select_worker()
        if worker is None:
            if policy == "local":
                self._record_route(
                    mode="complete_json",
                    target="local_error",
                    fallback_reason="no online worker",
                    route_mode=policy,
                    elapsed_ms=_elapsed_ms(started),
                )
                raise RuntimeError("local brain route enabled but no online brain worker is available")
            self._record_route(
                mode="complete_json",
                target="cloud_fallback",
                fallback_reason="no online worker",
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            return self.cloud_fallback.complete_json(system, prompt)

        try:
            result = self._complete_json_from_worker(worker, system, prompt)
            self._record_route(
                mode="complete_json",
                target="remote",
                worker_id=worker.worker_id,
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            return result
        except Exception as exc:
            if policy == "local":
                self._record_route(
                    mode="complete_json",
                    target="local_error",
                    worker_id=worker.worker_id,
                    fallback_reason=str(exc),
                    route_mode=policy,
                    elapsed_ms=_elapsed_ms(started),
                )
                raise RuntimeError(f"local brain worker '{worker.worker_id}' failed: {exc}") from exc
            self._record_route(
                mode="complete_json",
                target="cloud_fallback",
                worker_id=worker.worker_id,
                fallback_reason=str(exc),
                route_mode=policy,
                elapsed_ms=_elapsed_ms(started),
            )
            return self.cloud_fallback.complete_json(system, prompt)


__all__ = [
    "HybridRouterEngine",
    "normalize_route_mode",
]
