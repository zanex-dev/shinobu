# -*- coding: utf-8 -*-
"""
Pure-stdlib loopback HTTP + SSE daemon gateway (`127.0.0.1` only).

Exposes a zero-dependency `ThreadingHTTPServer` secured with a per-launch
Bearer token verified via `hmac.compare_digest`:
  - GET  /health  -> daemon health & active episode status
  - GET  /state   -> current PetStateMachine snapshot
  - GET  /events  -> Server-Sent Events (`text/event-stream`) realtime stream
  - POST /chat    -> streaming SSE (default) or JSON reply via Orchestrator
  - POST /sleep   -> trigger sleep-time episode consolidation
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from shinobu_core.cloud.approval import ApprovalGate
from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry
from shinobu_core.cloud.tts_router import TtsRouter, TtsUnavailableError
from shinobu_core.engines.openai_compat import normalize_reasoning_effort
from shinobu_core.mcp.client import McpClientManager, McpStdioClient
from shinobu_core.mcp.server import LoopbackDaemonBackend, McpStdioServer
from shinobu_core.memory.consolidator import compact_memory
from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.runtime.heartbeat import HeartbeatLoop
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.runtime.state_machine import PetStateMachine
from shinobu_core.server.openai_compat_api import (
    OPENAI_SSE_DONE,
    build_chat_chunk,
    build_chat_completion,
    build_cors_headers,
    build_models_payload,
    build_openai_error,
    extract_user_text,
    format_openai_sse_chunk,
    is_allowed_local_origin,
    make_completion_id,
    resolve_model_id,
)
from shinobu_core.soul.loader import SOUL_DIR_NAME, SOUL_FILENAMES
from shinobu_core.ui.avatar import AvatarResolver, render_builtin_svg
from shinobu_core.ui.web_console import render_console_html

logger = logging.getLogger("shinobu_core.server.daemon")

LOOPBACK_HOST = "127.0.0.1"
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB safety cap
_AVATAR_MIME_TYPES: dict[str, str] = {
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _format_sse_event(event: Event) -> bytes:
    """Format a canonical `Event` into a UTF-8 Server-Sent Events frame."""
    safe_kind = str(event.kind).replace("\r", "").replace("\n", "")
    frame = f"event: {safe_kind}\ndata: {event.to_json()}\n\n"
    return frame.encode("utf-8")


def _serialize_result(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        raw = dataclasses.asdict(obj)
        return json.loads(json.dumps(raw, ensure_ascii=False, default=str))
    if isinstance(obj, dict):
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    return str(obj)


class EventBroker:
    """Thread-safe fan-out broker backing `GET /events` SSE subscribers."""

    def __init__(self, max_queue_size: int = 256) -> None:
        self._lock = threading.RLock()
        self._max_queue_size = max(1, int(max_queue_size))
        self._subscribers: set[queue.Queue[Event | None]] = set()
        self._closed = False

    def subscribe(self) -> queue.Queue[Event | None]:
        q: queue.Queue[Event | None] = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            if self._closed:
                q.put_nowait(None)
                return q
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event | None]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: Event) -> None:
        with self._lock:
            if self._closed:
                return
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            targets = list(self._subscribers)
            self._subscribers.clear()
        for q in targets:
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def reset(self) -> None:
        with self._lock:
            self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class _LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        daemon_ctx: "DaemonServer",
    ) -> None:
        self.daemon_ctx = daemon_ctx
        super().__init__(server_address, handler_cls)


class _DaemonRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("HTTP %s - " + format, self.address_string(), *args)

    @property
    def _ctx(self) -> "DaemonServer":
        return self.server.daemon_ctx  # type: ignore[attr-defined]

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _cors_headers_for_v1(self) -> dict[str, str]:
        return build_cors_headers(self.headers.get("Origin"), preflight=False)

    def _check_auth(
        self,
        *,
        allow_query_token: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            candidate = header[len("Bearer ") :].strip()
            if candidate and self._ctx.verify_token(candidate):
                return True
        if allow_query_token:
            query = parse_qs(urlparse(self.path).query)
            tokens = query.get("token")
            if tokens:
                candidate = tokens[0].strip()
                if candidate and self._ctx.verify_token(candidate):
                    return True
        headers = {"WWW-Authenticate": "Bearer"}
        if extra_headers:
            headers.update(extra_headers)
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "unauthorized"},
            extra_headers=headers,
        )
        return False

    def _parse_int_query(
        self,
        query: dict[str, list[str]],
        key: str,
        default: int,
        *,
        min_val: int = 1,
        max_val: int = 500,
    ) -> int | None:
        raw_vals = query.get(key)
        if not raw_vals or not raw_vals[0].strip():
            return default
        try:
            val = int(raw_vals[0].strip())
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid_{key}"})
            return None
        return max(min_val, min(max_val, val))

    def _read_json_body(
        self,
        *,
        allow_empty: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not raw_len.strip():
            if allow_empty:
                return {}
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "empty_body"},
                extra_headers=extra_headers,
            )
            return None
        try:
            length = int(raw_len.strip())
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_content_length"},
                extra_headers=extra_headers,
            )
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "body_too_large"},
                extra_headers=extra_headers,
            )
            return None
        if length == 0:
            if allow_empty:
                return {}
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "empty_body"},
                extra_headers=extra_headers,
            )
            return None
        raw_bytes = self.rfile.read(length)
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_json"},
                extra_headers=extra_headers,
            )
            return None
        if not isinstance(data, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "json_object_required"},
                extra_headers=extra_headers,
            )
            return None
        return data

    def _start_sse_headers(self, *, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1" or path.startswith("/v1/"):
            origin = self.headers.get("Origin")
            if not is_allowed_local_origin(origin):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "origin_not_allowed"})
                return
            cors_headers = build_cors_headers(origin, preflight=True)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            for k, v in cors_headers.items():
                self.send_header(k, v)
            self.end_headers()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/", "/ui"):
            html_bytes = render_console_html().encode("utf-8")
            self._send_bytes(
                HTTPStatus.OK,
                html_bytes,
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
            return

        cors_v1 = self._cors_headers_for_v1() if path.startswith("/v1/") else None
        if not self._check_auth(allow_query_token=(path == "/events"), extra_headers=cors_v1):
            return
        query = parse_qs(parsed.query)

        if path == "/health":
            hb = self._ctx.heartbeat
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "pid": os.getpid(),
                    "episode_id": self._ctx.orchestrator.active_episode_id,
                    "heartbeat_running": bool(hb is not None and hb.is_running),
                },
            )
            return

        if path == "/state":
            snap = self._ctx.state_machine.snapshot()
            snap["episode_id"] = self._ctx.orchestrator.active_episode_id
            self._send_json(HTTPStatus.OK, snap)
            return

        if path == "/events":
            self._handle_sse_stream()
            return

        if path == "/api/turns":
            self._handle_api_turns(query)
            return

        if path == "/api/soul":
            self._handle_api_soul_list()
            return

        if path == "/api/journals":
            self._handle_api_journals()
            return

        if path == "/api/memory/facts":
            self._handle_api_memory_facts(query)
            return

        if path == "/api/memory/search":
            self._handle_api_memory_search(query)
            return

        if path == "/api/timemap":
            self._handle_api_timemap_list(query)
            return

        if path == "/api/avatar":
            self._handle_api_avatar(query)
            return

        if path == "/api/compat":
            self._handle_api_compat()
            return

        if path == "/api/mcp":
            self._handle_api_mcp()
            return

        if path == "/api/engine":
            self._handle_api_engine()
            return

        if path == "/api/workers":
            self._handle_api_workers_list(query)
            return

        if path == "/api/approvals":
            self._handle_api_approvals_list()
            return

        if path == "/api/episodes":
            self._handle_api_episodes_list(query)
            return

        if path == "/v1/models":
            self._handle_v1_models()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        cors_v1 = self._cors_headers_for_v1() if path.startswith("/v1/") else None
        if not self._check_auth(extra_headers=cors_v1):
            return

        if path == "/chat":
            self._handle_chat()
            return

        if path == "/v1/chat/completions":
            self._handle_v1_chat_completions()
            return

        if path == "/sleep":
            self._handle_sleep()
            return

        if path == "/api/episodes/new":
            self._handle_api_episodes_new()
            return

        if path == "/api/memory/facts":
            self._handle_api_memory_facts_upsert()
            return

        if path == "/api/memory/compact":
            self._handle_api_memory_compact()
            return

        if path == "/api/timemap":
            self._handle_api_timemap_create()
            return

        if path == "/api/state":
            self._handle_api_state_update()
            return

        if path == "/api/bubble":
            self._handle_api_bubble()
            return

        if path == "/api/mcp/probe":
            self._handle_api_mcp_probe()
            return

        if path == "/api/engine/model":
            self._handle_api_engine_model()
            return

        if path == "/api/engine/route":
            self._handle_api_engine_route()
            return

        if path == "/api/engine/reasoning":
            self._handle_api_engine_reasoning()
            return

        if path == "/api/workers/register":
            self._handle_api_workers_register()
            return

        if path == "/api/workers/heartbeat":
            self._handle_api_workers_heartbeat()
            return

        if path.startswith("/api/approvals/") and path.endswith("/decide"):
            raw_id = unquote(path[len("/api/approvals/") : -len("/decide")])
            self._handle_api_approval_decide(raw_id)
            return

        if path == "/api/tts":
            self._handle_api_tts()
            return

        if path == "/mcp":
            self._handle_mcp_post()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path.startswith("/api/soul/"):
            raw_name = unquote(path[len("/api/soul/") :])
            self._handle_api_soul_update(raw_name)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path.startswith("/api/timemap/"):
            raw_id = unquote(path[len("/api/timemap/") :])
            self._handle_api_timemap_delete(raw_id)
            return

        if path.startswith("/api/workers/"):
            raw_id = unquote(path[len("/api/workers/") :])
            self._handle_api_workers_delete(raw_id)
            return

        if path.startswith("/api/memory/facts/"):
            raw_id = unquote(path[len("/api/memory/facts/") :])
            self._handle_api_memory_facts_delete(raw_id)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    # ------------------------------------------------------------------
    # Cloud Control-Plane & Remote Streamable HTTP MCP Handlers
    # ------------------------------------------------------------------

    def _handle_api_workers_list(self, query: dict[str, list[str]]) -> None:
        raw_inc = (query.get("include_expired") or [""])[0].strip().lower()
        include_expired = raw_inc in ("1", "true", "yes")
        workers = self._ctx.worker_registry.list_workers(
            include_expired=include_expired
        )
        self._send_json(
            HTTPStatus.OK,
            {"workers": workers, "count": len(workers)},
        )

    def _handle_api_workers_register(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        worker_id = body.get("worker_id")
        role = body.get("role")
        base_url = body.get("base_url")
        if not isinstance(worker_id, str) or not worker_id.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "worker_id_required"})
            return
        if not isinstance(role, str) or not role.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "role_required"})
            return
        if not isinstance(base_url, str) or not base_url.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "base_url_required"})
            return

        raw_caps = body.get("capabilities", [])
        if raw_caps is None:
            raw_caps = []
        if not isinstance(raw_caps, (list, tuple)) or any(
            not isinstance(c, str) for c in raw_caps
        ):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capabilities"})
            return
        capabilities = tuple(c.strip() for c in raw_caps if c.strip())

        token = body.get("token", "")
        if token is None:
            token = ""
        if not isinstance(token, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_token"})
            return

        raw_ttl = body.get("ttl_sec", 45.0)
        if isinstance(raw_ttl, bool):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_ttl_sec"})
            return
        try:
            ttl_sec = float(raw_ttl)
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_ttl_sec"})
            return
        if not math.isfinite(ttl_sec) or ttl_sec <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_ttl_sec"})
            return

        meta = body.get("meta")
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_meta"})
            return

        try:
            record = WorkerRecord(
                worker_id=worker_id.strip(),
                role=role.strip(),
                base_url=base_url.strip(),
                capabilities=capabilities,
                token=token,
                ttl_sec=ttl_sec,
                meta=dict(meta),
            )
            self._ctx.worker_registry.register(record)
        except (ValueError, TypeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_worker", "detail": str(exc)},
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {"status": "registered", "worker": record.to_dict()},
        )

    def _handle_api_workers_heartbeat(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        worker_id = body.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "worker_id_required"})
            return
        meta = body.get("meta")
        if meta is not None and not isinstance(meta, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_meta"})
            return

        cleaned_id = worker_id.strip()
        ok = self._ctx.worker_registry.heartbeat(cleaned_id, meta=meta)
        if not ok:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "worker_not_found", "worker_id": cleaned_id},
            )
            return
        rec = self._ctx.worker_registry.get(cleaned_id)
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "worker_id": cleaned_id,
                "worker": rec.to_dict() if rec is not None else None,
            },
        )

    def _handle_api_workers_delete(self, raw_id: str) -> None:
        worker_id = raw_id.strip()
        if not worker_id or "/" in worker_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_worker_id"})
            return
        removed = self._ctx.worker_registry.unregister(worker_id)
        if not removed:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "worker_not_found", "worker_id": worker_id},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"status": "unregistered", "worker_id": worker_id},
        )

    def _handle_api_approvals_list(self) -> None:
        pending = self._ctx.approval_gate.list_pending()
        self._send_json(
            HTTPStatus.OK,
            {"pending": pending, "count": len(pending)},
        )

    def _handle_api_approval_decide(self, raw_id: str) -> None:
        approval_id = raw_id.strip()
        if not approval_id or "/" in approval_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_approval_id"})
            return
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        approved = body.get("approved")
        if not isinstance(approved, bool):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "approved_bool_required"},
            )
            return
        actor = body.get("actor", "")
        if actor is None:
            actor = ""
        if not isinstance(actor, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_actor"})
            return

        ok = self._ctx.approval_gate.decide(
            approval_id,
            approved=approved,
            actor=actor,
        )
        if not ok:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "approval_not_found", "approval_id": approval_id},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "decided",
                "approval_id": approval_id,
                "approved": approved,
                "actor": actor,
            },
        )

    def _handle_api_tts(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text_required"})
            return
        mood = body.get("mood", "calm")
        if mood is None:
            mood = "calm"
        if not isinstance(mood, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_mood"})
            return
        fmt = body.get("fmt", "ogg_opus")
        if fmt is None:
            fmt = "ogg_opus"
        if not isinstance(fmt, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_fmt"})
            return

        router = self._ctx.tts_router
        if router is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "tts_unavailable"},
            )
            return

        try:
            result = router.synthesize(text, mood=mood, fmt=fmt)
        except TtsUnavailableError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "tts_unavailable", "detail": str(exc)},
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_tts_request", "detail": str(exc)},
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "tts_failed", "detail": str(exc)},
            )
            return

        self._send_bytes(
            HTTPStatus.OK,
            result.audio,
            result.content_type,
            cache_control="no-store",
            extra_headers={"X-Shinobu-TTS-Provider": result.provider},
        )

    def _handle_mcp_post(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        if body.get("jsonrpc") != "2.0":
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {
                        "code": -32600,
                        "message": "Invalid Request: 'jsonrpc' must be '2.0'",
                    },
                },
            )
            return

        response = self._ctx.mcp_server.handle_message(body)
        if response is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self._send_json(HTTPStatus.OK, response)

    def _handle_sse_stream(self) -> None:
        sub_queue = self._ctx.broker.subscribe()
        try:
            self._start_sse_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            keepalive_sec = self._ctx.sse_keepalive_sec
            while self._ctx.is_running:
                try:
                    ev = sub_queue.get(timeout=keepalive_sec)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    break
                self.wfile.write(_format_sse_event(ev))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self._ctx.broker.unsubscribe(sub_queue)

    def _handle_chat(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return
        raw_text = body.get("text")
        if raw_text is None:
            raw_text = body.get("message")
        if not isinstance(raw_text, str) or not raw_text.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text_required"})
            return

        stream = body.get("stream", True)
        if not isinstance(stream, bool):
            stream = True

        self._ctx.state_machine.touch()

        if not stream:
            events_out: list[dict[str, Any]] = []
            final_text = ""
            episode_id = self._ctx.orchestrator.active_episode_id or ""
            try:
                with self._ctx.chat_lock:
                    for ev in self._ctx.orchestrator.send_message(raw_text):
                        self._ctx.publish_event(ev)
                        events_out.append(ev.to_dict())
                        if ev.episode_id:
                            episode_id = ev.episode_id
                        if ev.kind == "done" and isinstance(ev.payload, dict):
                            final_text = str(ev.payload.get("text", ""))
            except Exception as exc:
                logging.getLogger(__name__).exception("Chat request failed: %s", exc)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "chat_failed", "detail": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "episode_id": episode_id,
                    "text": final_text,
                    "events": events_out,
                },
            )
            return

        # Default: streaming SSE response
        self._start_sse_headers()
        try:
            with self._ctx.chat_lock:
                for ev in self._ctx.orchestrator.send_message(raw_text):
                    self._ctx.publish_event(ev)
                    self.wfile.write(_format_sse_event(ev))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            logging.getLogger(__name__).exception("Chat request failed: %s", exc)
            err_ev = Event(kind="error", payload={"error": str(exc)})
            try:
                self.wfile.write(_format_sse_event(err_ev))
                self.wfile.flush()
            except OSError:
                pass

    def _handle_v1_models(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            build_models_payload(),
            extra_headers=self._cors_headers_for_v1() or None,
        )

    def _handle_v1_chat_completions(self) -> None:
        cors_headers = self._cors_headers_for_v1() or None
        body = self._read_json_body(allow_empty=False, extra_headers=cors_headers)
        if body is None:
            return

        user_text = extract_user_text(body.get("messages"))
        if not user_text:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                build_openai_error(
                    "messages must include a non-empty user message",
                    code="messages_required",
                ),
                extra_headers=cors_headers,
            )
            return

        model = resolve_model_id(body.get("model"))
        stream = body.get("stream", False)
        if not isinstance(stream, bool):
            stream = False

        completion_id = make_completion_id()
        created = int(time.time())
        self._ctx.state_machine.touch()

        if not stream:
            delta_parts: list[str] = []
            final_text = ""
            try:
                with self._ctx.chat_lock:
                    for ev in self._ctx.orchestrator.send_message(user_text):
                        self._ctx.publish_event(ev)
                        if ev.kind == "token_delta" and isinstance(ev.payload, dict):
                            piece = ev.payload.get("delta")
                            if piece is None:
                                piece = ev.payload.get("text")
                            if isinstance(piece, str) and piece:
                                delta_parts.append(piece)
                        elif ev.kind == "done" and isinstance(ev.payload, dict):
                            final_text = str(ev.payload.get("text", ""))
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    build_openai_error(
                        str(exc),
                        error_type="server_error",
                        code="chat_failed",
                    ),
                    extra_headers=cors_headers,
                )
                return

            if not final_text and delta_parts:
                final_text = "".join(delta_parts)

            self._send_json(
                HTTPStatus.OK,
                build_chat_completion(
                    completion_id,
                    model,
                    final_text,
                    created=created,
                ),
                extra_headers=cors_headers,
            )
            return

        self._start_sse_headers(extra_headers=cors_headers)
        try:
            role_chunk = build_chat_chunk(
                completion_id,
                model,
                created=created,
                delta={"role": "assistant", "content": ""},
                finish_reason=None,
            )
            self.wfile.write(format_openai_sse_chunk(role_chunk))
            self.wfile.flush()

            sent_content = False
            with self._ctx.chat_lock:
                for ev in self._ctx.orchestrator.send_message(user_text):
                    self._ctx.publish_event(ev)
                    if ev.kind == "token_delta" and isinstance(ev.payload, dict):
                        piece = ev.payload.get("delta")
                        if piece is None:
                            piece = ev.payload.get("text")
                        if isinstance(piece, str) and piece:
                            sent_content = True
                            chunk = build_chat_chunk(
                                completion_id,
                                model,
                                created=created,
                                delta={"content": piece},
                                finish_reason=None,
                            )
                            self.wfile.write(format_openai_sse_chunk(chunk))
                            self.wfile.flush()
                    elif ev.kind == "done" and isinstance(ev.payload, dict) and not sent_content:
                        done_text = str(ev.payload.get("text", ""))
                        if done_text:
                            sent_content = True
                            chunk = build_chat_chunk(
                                completion_id,
                                model,
                                created=created,
                                delta={"content": done_text},
                                finish_reason=None,
                            )
                            self.wfile.write(format_openai_sse_chunk(chunk))
                            self.wfile.flush()

            stop_chunk = build_chat_chunk(
                completion_id,
                model,
                created=created,
                delta={},
                finish_reason="stop",
            )
            self.wfile.write(format_openai_sse_chunk(stop_chunk))
            self.wfile.write(OPENAI_SSE_DONE)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            err_payload = build_openai_error(
                str(exc),
                error_type="server_error",
                code="chat_failed",
            )
            try:
                self.wfile.write(format_openai_sse_chunk(err_payload))
                self.wfile.write(OPENAI_SSE_DONE)
                self.wfile.flush()
            except OSError:
                pass

    def _handle_sleep(self) -> None:
        body = self._read_json_body(allow_empty=True)
        if body is None:
            return
        date_str = body.get("date_str")
        if date_str is not None and not isinstance(date_str, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_date_str"})
            return

        try:
            with self._ctx.chat_lock:
                result = self._ctx.orchestrator.sleep_and_consolidate(date_str=date_str)
        except RuntimeError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "sleep_failed", "detail": str(exc)},
            )
            return

        sleep_ev = self._ctx.state_machine.transition(
            "sleepy",
            mood="sleepy",
            reason="sleep_consolidate",
            touch_activity=False,
        )
        self._ctx.broker.publish(sleep_ev)

        self._send_json(
            HTTPStatus.OK,
            {
                "status": "consolidated",
                "result": _serialize_result(result),
                "state": self._ctx.state_machine.snapshot(),
            },
        )

    # ------------------------------------------------------------------
    # Studio / Journal / Memory / Timemap / State REST APIs
    # ------------------------------------------------------------------

    def _resolve_home(self) -> Path:
        raw_home = getattr(self._ctx, "home", None) or getattr(
            self._ctx.orchestrator, "home", None
        )
        if raw_home is None:
            raise RuntimeError("daemon home directory is not configured")
        return Path(raw_home)

    def _handle_api_turns(self, query: dict[str, list[str]]) -> None:
        limit = self._parse_int_query(query, "limit", default=50, min_val=1, max_val=500)
        if limit is None:
            return
        ep_vals = query.get("episode_id")
        episode_id = (
            ep_vals[0].strip()
            if ep_vals and ep_vals[0].strip()
            else self._ctx.orchestrator.active_episode_id
        )
        if not episode_id:
            self._send_json(HTTPStatus.OK, {"episode_id": None, "turns": []})
            return
        turns = self._ctx.orchestrator.store.get_turns(episode_id, limit=limit)
        self._send_json(HTTPStatus.OK, {"episode_id": episode_id, "turns": turns})

    def _handle_api_episodes_list(self, query: dict[str, list[str]]) -> None:
        limit = self._parse_int_query(query, "limit", default=8, min_val=1, max_val=50)
        if limit is None:
            return
        store = self._ctx.orchestrator.store
        active_ep = store.get_active_episode()
        active_payload: dict[str, Any] | None = None
        if active_ep is not None:
            active_id = str(active_ep["id"])
            active_payload = {
                "id": active_id,
                "turn_count": store.count_turns(active_id),
                "created_at": active_ep.get("created_at"),
            }

        recent_closed: list[dict[str, Any]] = []
        for ep in store.list_episodes(limit=limit + 5):
            if ep.get("status") != "closed":
                continue
            ep_id = str(ep["id"])
            recent_closed.append(
                {
                    "id": ep_id,
                    "summary": str(ep.get("summary") or ""),
                    "turn_count": store.count_turns(ep_id),
                    "created_at": ep.get("created_at"),
                    "closed_at": ep.get("closed_at"),
                }
            )
            if len(recent_closed) >= limit:
                break

        self._send_json(
            HTTPStatus.OK,
            {
                "active_episode": active_payload,
                "recent_closed": recent_closed,
            },
        )

    def _handle_api_episodes_new(self) -> None:
        body = self._read_json_body(allow_empty=True)
        if body is None:
            return
        summary = body.get("summary", "")
        if summary is None:
            summary = ""
        if not isinstance(summary, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_summary"})
            return

        try:
            with self._ctx.chat_lock:
                result = self._ctx.orchestrator.archive_and_reset(summary=summary)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "episode_reset_failed", "detail": str(exc)},
            )
            return

        payload: dict[str, Any] = {"ok": True}
        payload.update(result)
        snap = self._ctx.state_machine.snapshot()
        snap["episode_id"] = self._ctx.orchestrator.active_episode_id
        snap["reason"] = "episode_new"
        snap["archived_episode_id"] = result.get("episode_id")
        self._ctx.publish_event(Event(kind=EventKind.STATE_CHANGED, payload=snap))
        self._send_json(HTTPStatus.OK, payload)

    def _handle_api_soul_list(self) -> None:
        soul_dir = self._resolve_home() / SOUL_DIR_NAME
        files: dict[str, str] = {}
        for name in SOUL_FILENAMES:
            fp = soul_dir / name
            if fp.is_file():
                files[name] = fp.read_text(encoding="utf-8")
            else:
                files[name] = ""
        self._send_json(HTTPStatus.OK, {"files": files})

    def _handle_api_soul_update(self, name: str) -> None:
        if name not in SOUL_FILENAMES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_soul_file", "allowed": list(SOUL_FILENAMES)},
            )
            return
        body = self._read_json_body()
        if body is None:
            return
        content = body.get("content")
        if not isinstance(content, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content"})
            return

        soul_dir = self._resolve_home() / SOUL_DIR_NAME
        target_path = soul_dir / name
        backup_name: str | None = None

        with self._ctx.chat_lock:
            soul_dir.mkdir(parents=True, exist_ok=True)
            if target_path.is_file():
                versions_dir = soul_dir / "_versions"
                versions_dir.mkdir(parents=True, exist_ok=True)
                stem = target_path.stem
                suffix = target_path.suffix
                pattern = re.compile(rf"^{re.escape(stem)}_v(\d+){re.escape(suffix)}$")
                max_ver = 0
                for existing in versions_dir.iterdir():
                    m = pattern.match(existing.name)
                    if m:
                        max_ver = max(max_ver, int(m.group(1)))
                backup_name = f"{stem}_v{max_ver + 1}{suffix}"
                backup_path = versions_dir / backup_name
                backup_path.write_bytes(target_path.read_bytes())

            tmp_path = soul_dir / f".{name}.{secrets.token_hex(6)}.tmp"
            try:
                tmp_path.write_text(content, encoding="utf-8")
                os.replace(tmp_path, target_path)
            except Exception:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                raise

        self._send_json(
            HTTPStatus.OK,
            {
                "status": "saved",
                "name": name,
                "backup": backup_name,
                "bytes": len(content.encode("utf-8")),
            },
        )

    def _handle_api_journals(self) -> None:
        journals_dir = self._resolve_home() / "journals"
        items: list[dict[str, Any]] = []
        if journals_dir.is_dir():
            md_files = [
                p
                for p in journals_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".md"
            ]
            md_files.sort(key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
            for p in md_files:
                st = p.stat()
                mtime_iso = (
                    datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                items.append(
                    {
                        "name": p.name,
                        "mtime": mtime_iso,
                        "content": p.read_text(encoding="utf-8"),
                    }
                )
        self._send_json(HTTPStatus.OK, {"journals": items})

    def _handle_api_memory_facts(self, query: dict[str, list[str]]) -> None:
        limit = self._parse_int_query(query, "limit", default=100, min_val=1, max_val=500)
        if limit is None:
            return
        facts = self._ctx.orchestrator.store.list_facts(limit=limit)
        self._send_json(HTTPStatus.OK, {"facts": facts})

    def _handle_api_memory_facts_upsert(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        key = body.get("key")
        value = body.get("value")
        if not isinstance(key, str) or not key.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_key"})
            return
        if not isinstance(value, str) or not value.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_value"})
            return

        raw_confidence = body.get("confidence", 0.8)
        if raw_confidence is None:
            confidence = 0.8
        else:
            if isinstance(raw_confidence, bool):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_confidence"})
                return
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_confidence"})
                return
            if confidence != confidence or confidence in (float("inf"), float("-inf")):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_confidence"})
                return
            confidence = max(0.0, min(1.0, confidence))

        raw_episode = body.get("source_episode")
        if raw_episode is None:
            source_episode = self._ctx.orchestrator.active_episode_id or ""
        elif isinstance(raw_episode, str):
            source_episode = raw_episode.strip()
        else:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_source_episode"})
            return

        try:
            fact = self._ctx.orchestrator.store.upsert_fact(
                key=key.strip(),
                value=value.strip(),
                confidence=confidence,
                source_episode=source_episode,
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"status": "saved", "fact": fact})

    def _handle_api_memory_facts_delete(self, raw_id: str) -> None:
        cleaned = (raw_id or "").strip()
        if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_fact_id"})
            return
        fact_id = int(cleaned)
        if fact_id <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_fact_id"})
            return
        deleted = self._ctx.orchestrator.store.delete_fact(fact_id)
        if not deleted:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "fact_not_found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "deleted", "id": fact_id})

    def _handle_api_memory_compact(self) -> None:
        body = self._read_json_body(allow_empty=True)
        if body is None:
            return
        raw_dry_run = body.get("dry_run", True)
        if not isinstance(raw_dry_run, bool):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_dry_run"})
            return
        dry_run = raw_dry_run

        orch = self._ctx.orchestrator
        engine = (
            getattr(orch, "_consolidator_engine", None)
            or getattr(orch, "consolidator_engine", None)
            or orch.engine
        )
        if engine is None or not hasattr(engine, "complete_json"):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "compaction_engine_unavailable"},
            )
            return

        try:
            with self._ctx.chat_lock:
                result = compact_memory(
                    self._resolve_home(),
                    orch.store,
                    engine,
                    dry_run=dry_run,
                )
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "memory_compact_failed", "detail": str(exc)},
            )
            return

        result_payload: dict[str, Any] = {
            "applied": result.applied,
            "dry_run": result.dry_run,
            "summary": result.summary,
            "before_chars": result.before_chars,
            "after_chars": result.after_chars,
            "backup_path": str(result.backup_path) if result.backup_path is not None else None,
            "updated_facts": result.updated_facts,
            "removed_fact_ids": result.removed_fact_ids,
            "preview_memory_md": result.preview_memory_md,
        }

        if not dry_run and result.applied:
            snap = self._ctx.state_machine.snapshot()
            snap["episode_id"] = orch.active_episode_id
            snap["reason"] = "memory_compacted"
            snap["before_chars"] = result.before_chars
            snap["after_chars"] = result.after_chars
            snap["updated_facts_count"] = len(result.updated_facts)
            snap["removed_facts_count"] = len(result.removed_fact_ids)
            self._ctx.publish_event(Event(kind=EventKind.STATE_CHANGED, payload=snap))

        status_label = "preview" if dry_run else ("applied" if result.applied else "noop")
        self._send_json(
            HTTPStatus.OK,
            {
                "status": status_label,
                "result": result_payload,
            },
        )

    def _handle_api_memory_search(self, query: dict[str, list[str]]) -> None:
        limit = self._parse_int_query(query, "limit", default=20, min_val=1, max_val=100)
        if limit is None:
            return
        raw_q = query.get("q")
        q = raw_q[0].strip() if raw_q else ""
        if not q:
            self._send_json(HTTPStatus.OK, {"query": "", "results": []})
            return
        results = self._ctx.orchestrator.store.search(q, limit=limit)
        self._send_json(HTTPStatus.OK, {"query": q, "results": results})

    def _handle_api_timemap_list(self, query: dict[str, list[str]]) -> None:
        limit = self._parse_int_query(query, "limit", default=100, min_val=1, max_val=500)
        if limit is None:
            return
        raw_inc = query.get("include_done")
        include_done = bool(
            raw_inc and raw_inc[0].strip().lower() in {"1", "true", "yes"}
        )
        schedules = self._ctx.orchestrator.store.list_schedules(
            include_done=include_done,
            limit=limit,
        )
        self._send_json(HTTPStatus.OK, {"schedules": schedules})

    def _handle_api_timemap_create(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        trigger_at = body.get("trigger_at")
        label = body.get("label")
        payload = body.get("payload")
        if not isinstance(trigger_at, str) or not trigger_at.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trigger_at"})
            return
        if not isinstance(label, str) or not label.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_label"})
            return
        if payload is not None and not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
            return
        try:
            schedule_id = self._ctx.orchestrator.store.add_schedule(
                trigger_at,
                label,
                payload,
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(
            HTTPStatus.CREATED,
            {
                "id": schedule_id,
                "trigger_at": trigger_at.strip(),
                "label": label.strip(),
                "payload": payload if payload is not None else {},
                "done": False,
            },
        )

    def _handle_api_timemap_delete(self, raw_id: str) -> None:
        cleaned = raw_id.strip()
        if not cleaned.isdigit() or int(cleaned) <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_schedule_id"})
            return
        schedule_id = int(cleaned)
        deleted = self._ctx.orchestrator.store.delete_schedule(schedule_id)
        if not deleted:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "schedule_not_found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "deleted", "id": schedule_id})

    def _handle_api_avatar(self, query: dict[str, list[str]]) -> None:
        snap = self._ctx.state_machine.snapshot()
        raw_state = query.get("state")
        raw_mood = query.get("mood")
        state = (
            raw_state[0].strip()
            if raw_state and raw_state[0].strip()
            else str(snap.get("state", "idle"))
        )
        mood = (
            raw_mood[0].strip()
            if raw_mood and raw_mood[0].strip()
            else str(snap.get("mood", "calm"))
        )
        home = self._resolve_home()
        resolver = AvatarResolver(home=home)
        asset = resolver.resolve(state=state, mood=mood)

        if asset.kind == "file" and asset.file_path is not None:
            try:
                body = asset.file_path.read_bytes()
                content_type = _AVATAR_MIME_TYPES.get(
                    asset.file_path.suffix.lower(),
                    "application/octet-stream",
                )
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    content_type,
                    cache_control="no-store",
                    extra_headers={
                        "X-Shinobu-Avatar-Pack": asset.pack_name,
                        "X-Shinobu-Avatar-State": asset.state,
                        "X-Shinobu-Avatar-Mood": asset.mood,
                    },
                )
                return
            except OSError:
                pass

        svg_text = asset.svg_text or render_builtin_svg(asset.state, asset.mood)
        self._send_bytes(
            HTTPStatus.OK,
            svg_text.encode("utf-8"),
            "image/svg+xml; charset=utf-8",
            cache_control="no-store",
            extra_headers={
                "X-Shinobu-Avatar-Pack": asset.pack_name,
                "X-Shinobu-Avatar-State": asset.state,
                "X-Shinobu-Avatar-Mood": asset.mood,
            },
        )

    def _handle_api_state_update(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        state = body.get("state")
        mood = body.get("mood")
        reason = body.get("reason")
        if state is None and mood is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "state_or_mood_required"})
            return
        if state is not None and (not isinstance(state, str) or not state.strip()):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_state"})
            return
        if mood is not None and (not isinstance(mood, str) or not mood.strip()):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_mood"})
            return
        if reason is not None and not isinstance(reason, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_reason"})
            return

        reason_str = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else "api_update"
        )
        ep_id = self._ctx.orchestrator.active_episode_id or ""
        try:
            if state is not None:
                ev = self._ctx.state_machine.transition(
                    state.strip(),
                    mood=mood.strip() if isinstance(mood, str) else None,
                    reason=reason_str,
                    episode_id=ep_id,
                )
            else:
                assert isinstance(mood, str)
                ev = self._ctx.state_machine.set_mood(
                    mood.strip(),
                    reason=reason_str,
                    episode_id=ep_id,
                )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._ctx.broker.publish(ev)
        snap = self._ctx.state_machine.snapshot()
        snap["episode_id"] = self._ctx.orchestrator.active_episode_id
        self._send_json(HTTPStatus.OK, snap)

    def _handle_api_bubble(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return

        raw_text = body.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "text_required"})
            return
        text_clean = raw_text.strip()

        raw_mood = body.get("mood")
        mood_clean: str | None = None
        if raw_mood is not None:
            if not isinstance(raw_mood, str) or not raw_mood.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_mood"})
                return
            mood_clean = raw_mood.strip()

        raw_duration = body.get("duration_sec")
        duration_val: float | None = None
        if raw_duration is not None:
            if (
                isinstance(raw_duration, bool)
                or not isinstance(raw_duration, (int, float))
                or not math.isfinite(float(raw_duration))
                or float(raw_duration) <= 0
            ):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_duration_sec"})
                return
            duration_val = float(raw_duration)

        ep_id = self._ctx.orchestrator.active_episode_id or ""
        if mood_clean is not None:
            try:
                state_ev = self._ctx.state_machine.set_mood(
                    mood_clean,
                    reason="api_bubble",
                    episode_id=ep_id,
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._ctx.broker.publish(state_ev)

        payload: dict[str, Any] = {
            "text": text_clean,
            "source": "external",
        }
        if mood_clean is not None:
            payload["mood"] = mood_clean
        if duration_val is not None:
            payload["duration_sec"] = duration_val

        bubble_ev = Event(
            kind="bubble",
            payload=payload,
            episode_id=ep_id,
        )
        self._ctx.publish_event(bubble_ev)
        snap = self._ctx.state_machine.snapshot()
        snap["episode_id"] = self._ctx.orchestrator.active_episode_id
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "event": bubble_ev.to_dict(),
                "state": snap,
            },
        )

    def _handle_api_compat(self) -> None:
        url = self._ctx.url
        manager = McpClientManager(home=self._ctx.home)
        self._send_json(
            HTTPStatus.OK,
            {
                "openai_base_url": f"{url}/v1",
                "model": "shinobu",
                "api_key": self._ctx.token,
                "events_url": f"{url}/events",
                "bubble_url": f"{url}/api/bubble",
                "mcp_config_exists": manager.config_path.is_file(),
                "mcp_config_path": str(manager.config_path),
                "frontends": [
                    {
                        "id": "airi",
                        "name": "Project AIRI",
                        "provider": "OpenAI Compatible",
                        "url": "https://github.com/moeru-ai/airi/releases",
                    },
                    {
                        "id": "open_llm_vtuber",
                        "name": "Open-LLM-VTuber",
                        "provider": "OpenAI Compatible",
                        "url": "https://github.com/Open-LLM-VTuber/Open-LLM-VTuber",
                    },
                ],
            },
        )

    def _handle_api_mcp(self) -> None:
        manager = McpClientManager(home=self._ctx.home)
        configs = manager.load_config()
        kernel = getattr(self._ctx.orchestrator, "kernel", None)
        raw_tools = getattr(kernel, "tools", None) if kernel is not None else None
        if isinstance(raw_tools, dict):
            mounted_tools = sorted(
                str(k) for k in raw_tools.keys() if isinstance(k, str) and k.startswith("mcp_")
            )
        else:
            mounted_tools = []

        servers = [
            {
                "name": cfg.name,
                "command": cfg.command,
                "args": list(cfg.args),
                "disabled": bool(cfg.disabled),
                "timeout_sec": float(cfg.timeout_sec),
            }
            for cfg in configs
        ]
        self._send_json(
            HTTPStatus.OK,
            {
                "config_path": str(manager.config_path),
                "config_exists": manager.config_path.is_file(),
                "config_error": manager.last_config_error,
                "mounted_tools": mounted_tools,
                "servers": servers,
            },
        )

    def _handle_api_mcp_probe(self) -> None:
        body = self._read_json_body(allow_empty=True)
        if body is None:
            return

        total_start = time.monotonic()
        manager = McpClientManager(home=self._ctx.home)
        configs = manager.load_config()
        config_error = manager.last_config_error

        results: list[dict[str, Any]] = []
        all_ok = config_error is None
        for cfg in configs:
            if cfg.disabled:
                results.append(
                    {
                        "name": cfg.name,
                        "connected": False,
                        "disabled": True,
                        "tools": [],
                        "tool_count": 0,
                        "error": None,
                        "elapsed_ms": 0.0,
                    }
                )
                continue

            server_start = time.monotonic()
            client = McpStdioClient(cfg)
            connected = False
            tool_names: list[str] = []
            error_msg: str | None = None
            try:
                client.connect()
                connected = True
                discovered = client.list_tools()
                tool_names = [t.name for t in discovered]
            except Exception as exc:  # noqa: BLE001
                connected = False
                error_msg = str(exc) or type(exc).__name__
                all_ok = False
            finally:
                client.close()

            results.append(
                {
                    "name": cfg.name,
                    "connected": connected,
                    "disabled": False,
                    "tools": tool_names,
                    "tool_count": len(tool_names),
                    "error": error_msg,
                    "elapsed_ms": round((time.monotonic() - server_start) * 1000.0, 2),
                }
            )

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": all_ok,
                "elapsed_ms": round((time.monotonic() - total_start) * 1000.0, 2),
                "config_error": config_error,
                "servers": results,
            },
        )

    def _online_brain_workers(self, eng: Any) -> list[str]:
        reg = getattr(eng, "registry", None) or self._ctx.worker_registry
        if reg is None:
            return []
        cap = getattr(eng, "primary_capability", "brain") or "brain"
        return [
            str(w["worker_id"])
            for w in reg.list_workers(include_expired=False)
            if cap in (w.get("capabilities") or [])
        ]

    def _handle_api_engine(self) -> None:
        eng = self._ctx.orchestrator.engine
        raw_model = getattr(eng, "model", None)
        supports_route_switch = hasattr(eng, "route_mode")
        raw_route_mode = getattr(eng, "route_mode", None) if supports_route_switch else None
        supports_reasoning_switch = self._ctx._supports_reasoning_switch(eng)
        raw_reasoning_effort = (
            getattr(eng, "reasoning_effort", None) if supports_reasoning_switch else None
        )
        fallback = getattr(eng, "cloud_fallback", None)
        cloud_fallback_engine = (
            str(getattr(fallback, "name", type(fallback).__name__))
            if fallback is not None
            else ""
        )
        payload: dict[str, Any] = {
            "engine": str(getattr(eng, "name", type(eng).__name__)),
            "model": str(raw_model) if raw_model else "",
            "supports_model_switch": hasattr(eng, "model"),
            "route_mode": str(raw_route_mode) if raw_route_mode else "",
            "supports_route_switch": supports_route_switch,
            "reasoning_effort": str(raw_reasoning_effort) if raw_reasoning_effort else "",
            "supports_reasoning_switch": supports_reasoning_switch,
            "last_route": getattr(eng, "last_route", None),
            "cloud_fallback_engine": cloud_fallback_engine,
            "online_brain_workers": self._online_brain_workers(eng),
        }
        classifier = getattr(self._ctx.orchestrator, "classifier", None)
        if classifier is not None:
            snap_fn = getattr(self._ctx.orchestrator, "reasoning_snapshot", None)
            snap = snap_fn() if callable(snap_fn) else {}
            if not isinstance(snap, dict):
                snap = {}
            payload["reasoning_mode"] = str(
                snap.get("mode") or ("manual" if payload["reasoning_effort"] else "auto")
            )
            payload["effective_reasoning_effort"] = str(
                snap.get("effective_effort") or payload["reasoning_effort"] or ""
            )
            payload["episode_reasoning_effort"] = str(snap.get("episode_effort") or "")
            payload["classifier"] = {
                "enabled": True,
                "backend": str(getattr(classifier, "backend", "auto")),
                "min_effort": str(getattr(classifier, "min_effort", "low")),
                "max_effort": str(getattr(classifier, "max_effort", "high")),
                "has_api_backend": bool(getattr(classifier, "has_api_backend", False)),
                "has_agy_backend": bool(getattr(classifier, "has_agy_backend", False)),
                "api_model": str(getattr(classifier, "api_model", "") or ""),
                "ema_ms": dict(getattr(classifier, "ema_ms", {}) or {}),
                "last_classification": snap.get("last_classification"),
            }
        self._send_json(HTTPStatus.OK, payload)

    def _handle_api_engine_route(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return

        eng = self._ctx.orchestrator.engine
        if not hasattr(eng, "route_mode"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "route_switch_unsupported"})
            return

        raw_mode = body.get("mode")
        if not isinstance(raw_mode, str) or not raw_mode.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_route_mode"})
            return

        with self._ctx.chat_lock:
            prev_raw = getattr(eng, "route_mode", None)
            previous_mode = str(prev_raw) if prev_raw else ""
            try:
                setattr(eng, "route_mode", raw_mode)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_route_mode", "detail": str(exc)},
                )
                return
            curr_raw = getattr(eng, "route_mode", None)
            current_mode = str(curr_raw) if curr_raw else ""

        snap = self._ctx.state_machine.snapshot()
        snap["episode_id"] = self._ctx.orchestrator.active_episode_id
        snap["reason"] = "engine_route_changed"
        snap["engine_route"] = current_mode
        snap["previous_route"] = previous_mode
        self._ctx.publish_event(Event(kind=EventKind.STATE_CHANGED, payload=snap))

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "engine": str(getattr(eng, "name", type(eng).__name__)),
                "mode": current_mode,
                "previous_mode": previous_mode,
                "online_brain_workers": self._online_brain_workers(eng),
            },
        )

    def _handle_api_engine_model(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return

        eng = self._ctx.orchestrator.engine
        if not hasattr(eng, "model"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "model_switch_unsupported"})
            return

        raw_model = body.get("model")
        if not isinstance(raw_model, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model"})
            return

        cleaned = raw_model.strip()
        new_val: str | None = cleaned if cleaned else None
        with self._ctx.chat_lock:
            prev_raw = getattr(eng, "model", None)
            previous_model = str(prev_raw) if prev_raw else ""
            setattr(eng, "model", new_val)
            curr_raw = getattr(eng, "model", None)
            current_model = str(curr_raw) if curr_raw else ""

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "engine": str(getattr(eng, "name", type(eng).__name__)),
                "model": current_model,
                "previous_model": previous_model,
            },
        )

    def _handle_api_engine_reasoning(self) -> None:
        body = self._read_json_body(allow_empty=False)
        if body is None:
            return

        eng = self._ctx.orchestrator.engine
        if not self._ctx._supports_reasoning_switch(eng):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "reasoning_switch_unsupported"},
            )
            return

        if "reasoning_effort" in body:
            raw_effort = body.get("reasoning_effort")
        elif "effort" in body:
            raw_effort = body.get("effort")
        else:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_reasoning_effort"},
            )
            return

        if raw_effort is not None and not isinstance(raw_effort, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_reasoning_effort"},
            )
            return

        try:
            normalized = normalize_reasoning_effort(raw_effort)
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_reasoning_effort", "detail": str(exc)},
            )
            return

        with self._ctx.chat_lock:
            prev_raw = getattr(eng, "reasoning_effort", None)
            previous_effort = str(prev_raw) if prev_raw else ""
            try:
                setattr(eng, "reasoning_effort", normalized)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_reasoning_effort", "detail": str(exc)},
                )
                return
            curr_raw = getattr(eng, "reasoning_effort", None)
            current_effort = str(curr_raw) if curr_raw else ""
            set_manual = getattr(self._ctx.orchestrator, "set_manual_reasoning_effort", None)
            if callable(set_manual):
                set_manual(current_effort)
            self._ctx._save_reasoning_effort(current_effort)

        snap = self._ctx.state_machine.snapshot()
        snap["episode_id"] = self._ctx.orchestrator.active_episode_id
        snap["reason"] = "engine_reasoning_changed"
        snap["reasoning_effort"] = current_effort
        snap["previous_reasoning_effort"] = previous_effort
        self._ctx.publish_event(Event(kind=EventKind.STATE_CHANGED, payload=snap))

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "engine": str(getattr(eng, "name", type(eng).__name__)),
                "reasoning_effort": current_effort,
                "previous_reasoning_effort": previous_effort,
            },
        )


class _InProcessDaemonBackend:
    """In-process `ShinobuBackendProtocol` adapter for remote `POST /mcp`."""

    def __init__(self, daemon: "DaemonServer", *, timeout: float = 120.0) -> None:
        self._daemon = daemon
        self._timeout = max(1.0, float(timeout))
        self._loopback: LoopbackDaemonBackend | None = None
        self._loopback_url: str = ""

    def _get_loopback(self) -> LoopbackDaemonBackend:
        current_url = self._daemon.url
        if self._loopback is None or self._loopback_url != current_url:
            self._loopback = LoopbackDaemonBackend(
                url=current_url,
                token=self._daemon.token,
                timeout=self._timeout,
            )
            self._loopback_url = current_url
        return self._loopback

    def bubble(
        self,
        text: str,
        *,
        mood: str | None = None,
        duration_sec: float | None = None,
    ) -> dict[str, Any]:
        return self._get_loopback().bubble(
            text, mood=mood, duration_sec=duration_sec
        )

    def get_state(self) -> dict[str, Any]:
        snap = self._daemon.state_machine.snapshot()
        snap["episode_id"] = self._daemon.orchestrator.active_episode_id
        return snap

    def set_state(
        self,
        state: str | None = None,
        mood: str | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._get_loopback().set_state(state=state, mood=mood, reason=reason)

    def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return self._get_loopback().recall(query, limit=limit)

    def remember(
        self,
        key: str,
        value: str,
        *,
        confidence: float = 0.8,
    ) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("remember key must be a non-empty string")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("remember value must be a non-empty string")
        if isinstance(confidence, bool):
            raise ValueError("remember confidence must be a number")
        safe_conf = float(confidence)
        if safe_conf != safe_conf or safe_conf in (float("inf"), float("-inf")):
            raise ValueError("remember confidence must be a finite number")
        safe_conf = max(0.0, min(1.0, safe_conf))
        episode_id = self._daemon.orchestrator.active_episode_id or ""
        fact = self._daemon.orchestrator.store.upsert_fact(
            key.strip(),
            value.strip(),
            confidence=safe_conf,
            source_episode=episode_id,
        )
        return {"status": "saved", "fact": fact}

    def chat(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("chat text must be a non-empty string")
        self._daemon.state_machine.touch()
        events_out: list[dict[str, Any]] = []
        final_text = ""
        episode_id = self._daemon.orchestrator.active_episode_id or ""
        with self._daemon.chat_lock:
            for ev in self._daemon.orchestrator.send_message(text):
                self._daemon.publish_event(ev)
                events_out.append(ev.to_dict())
                if ev.episode_id:
                    episode_id = ev.episode_id
                if ev.kind == "done" and isinstance(ev.payload, dict):
                    final_text = str(ev.payload.get("text", ""))
        return {
            "episode_id": episode_id,
            "text": final_text,
            "events": events_out,
        }


class DaemonServer:
    """
    Loopback-only (`127.0.0.1`) HTTP + SSE gateway wrapping `Orchestrator`,
    `PetStateMachine`, and optional `HeartbeatLoop`.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        state_machine: PetStateMachine,
        heartbeat: HeartbeatLoop | None = None,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        token: str | None = None,
        *,
        home: Path | str | None = None,
        sse_keepalive_sec: float = 15.0,
        worker_registry: WorkerRegistry | None = None,
        approval_gate: ApprovalGate | None = None,
        tts_router: TtsRouter | None = None,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError(
                f"DaemonServer refuses to bind non-loopback host {host!r}; "
                f"only {LOOPBACK_HOST!r} is allowed"
            )
        port_int = int(port)
        if port_int < 0 or port_int > 65535:
            raise ValueError(f"Invalid TCP port: {port!r}")

        if token is None:
            resolved_token = secrets.token_urlsafe(32)
        else:
            resolved_token = str(token).strip()
            if not resolved_token:
                raise ValueError("Bearer token must be a non-empty string")

        self.orchestrator: Orchestrator = orchestrator
        self.state_machine: PetStateMachine = state_machine
        self.heartbeat: HeartbeatLoop | None = heartbeat
        self.host: str = LOOPBACK_HOST
        self._requested_port: int = port_int
        self._bound_port: int | None = None
        self._token: str = resolved_token
        self.home: Path = Path(home) if home is not None else Path(orchestrator.home)
        self.endpoint_path: Path = self.home / "runtime" / "endpoint.json"
        self.reasoning_effort_path: Path = self.home / "runtime" / "reasoning_effort.json"
        self.sse_keepalive_sec: float = max(0.05, float(sse_keepalive_sec))

        self.broker: EventBroker = EventBroker()
        self.chat_lock: threading.Lock = threading.Lock()
        self._lifecycle_lock: threading.RLock = threading.RLock()
        self._httpd: _LoopbackThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running: bool = False

        self.worker_registry: WorkerRegistry = (
            worker_registry if worker_registry is not None else WorkerRegistry()
        )
        if approval_gate is not None:
            self.approval_gate: ApprovalGate = approval_gate
        else:
            def _on_approval_event(ev: Any) -> None:
                if isinstance(ev, Event):
                    self.publish_event(ev)

            self.approval_gate = ApprovalGate(on_event=_on_approval_event)
        self.tts_router: TtsRouter = (
            tts_router if tts_router is not None else TtsRouter(self.worker_registry)
        )
        self._mcp_backend: _InProcessDaemonBackend = _InProcessDaemonBackend(self)
        self.mcp_server: McpStdioServer = McpStdioServer(
            home=self.home,
            client=self._mcp_backend,
        )

        if self.heartbeat is not None:
            self._attach_heartbeat(self.heartbeat)

        self._restore_reasoning_effort()

    @staticmethod
    def _supports_reasoning_switch(eng: Any) -> bool:
        target = getattr(eng, "cloud_fallback", None)
        if target is not None:
            return hasattr(eng, "reasoning_effort") and hasattr(target, "reasoning_effort")
        return hasattr(eng, "reasoning_effort")

    def _save_reasoning_effort(self, effort: str) -> None:
        path = self.reasoning_effort_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "reasoning_effort": effort,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp_path = path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except OSError:
            pass

    def _restore_reasoning_effort(self) -> None:
        eng = self.orchestrator.engine
        if not self._supports_reasoning_switch(eng):
            return
        path = self.reasoning_effort_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or "reasoning_effort" not in data:
            return
        raw_effort = data.get("reasoning_effort")
        if raw_effort is not None and not isinstance(raw_effort, str):
            return
        try:
            normalized = normalize_reasoning_effort(raw_effort)
            setattr(eng, "reasoning_effort", normalized)
            set_manual = getattr(self.orchestrator, "set_manual_reasoning_effort", None)
            if callable(set_manual):
                set_manual(normalized)
        except (ValueError, AttributeError):
            return

    def handle_episode_reset(self, info: dict[str, Any] | None = None) -> None:
        """Clear engine manual reasoning override and persisted file when an episode resets."""
        eng = self.orchestrator.engine
        if not self._supports_reasoning_switch(eng):
            return
        prev_raw = getattr(eng, "reasoning_effort", None)
        previous_effort = str(prev_raw) if prev_raw else ""
        if previous_effort:
            try:
                setattr(eng, "reasoning_effort", "")
            except (ValueError, AttributeError):
                pass
        if previous_effort or self.reasoning_effort_path.is_file():
            self._save_reasoning_effort("")

    def notify_episode_idle_archived(
        self,
        archived: Any = None,
        *,
        previous_effort: str = "",
    ) -> None:
        """Publish a STATE_CHANGED event after an inactivity-triggered episode archive."""
        snap = self.state_machine.snapshot()
        snap["episode_id"] = self.orchestrator.active_episode_id
        snap["reason"] = "episode_idle_archived"
        snap["reasoning_effort"] = ""
        snap["previous_reasoning_effort"] = previous_effort
        if isinstance(archived, dict) and archived.get("closed_episode_id"):
            snap["closed_episode_id"] = archived["closed_episode_id"]
        elif isinstance(archived, str) and archived:
            snap["closed_episode_id"] = archived
        self.publish_event(Event(kind=EventKind.STATE_CHANGED, payload=snap))

    def _attach_heartbeat(self, heartbeat: HeartbeatLoop) -> None:
        prev_publish = heartbeat.publish
        if prev_publish is None:
            heartbeat.publish = self.broker.publish
            return
        if prev_publish is self.broker.publish:
            return

        def _fanout(ev: Event) -> None:
            try:
                prev_publish(ev)
            finally:
                self.broker.publish(ev)

        heartbeat.publish = _fanout

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        if self._bound_port is not None:
            return self._bound_port
        return self._requested_port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._running

    def verify_token(self, candidate: str) -> bool:
        """Constant-time Bearer token comparison using `hmac.compare_digest`."""
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            self._token.encode("utf-8"),
        )

    def publish_event(self, event: Event) -> None:
        """Sync state machine (if applicable) and fan-out event to SSE subscribers."""
        self.state_machine.apply_event(event)
        self.broker.publish(event)

    def _write_endpoint_file(self) -> dict[str, Any]:
        info = {
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
            "token": self._token,
            "started_at": _utc_now_iso(),
        }
        self.endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.endpoint_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, self.endpoint_path)
        try:
            os.chmod(self.endpoint_path, 0o600)
        except OSError:
            pass
        return info

    def _cleanup_endpoint_file(self) -> None:
        try:
            self.endpoint_path.unlink(missing_ok=True)
        except OSError:
            pass

    def start(self, *, start_heartbeat: bool = True) -> dict[str, Any]:
        """Bind `127.0.0.1`, write `runtime/endpoint.json`, and start serving."""
        with self._lifecycle_lock:
            if self._running:
                return {
                    "url": self.url,
                    "host": self.host,
                    "port": self.port,
                    "pid": os.getpid(),
                    "token": self._token,
                }

            self.broker.reset()
            httpd = _LoopbackThreadingHTTPServer(
                (self.host, self._requested_port),
                _DaemonRequestHandler,
                self,
            )
            self._httpd = httpd
            self._bound_port = int(httpd.server_address[1])
            self._running = True

            info = self._write_endpoint_file()

            self._thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="shinobu-daemon-http",
                daemon=True,
            )
            self._thread.start()

            if start_heartbeat and self.heartbeat is not None:
                self.heartbeat.start()

            return info

    def stop(self) -> None:
        """Stop heartbeat, close SSE streams, shut down HTTP server, and remove endpoint.json."""
        with self._lifecycle_lock:
            if not self._running and self._httpd is None:
                self._cleanup_endpoint_file()
                return
            self._running = False
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None

        if self.heartbeat is not None:
            self.heartbeat.stop()

        self.broker.close_all()

        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass

        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        self._cleanup_endpoint_file()

    def __enter__(self) -> "DaemonServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


__all__ = ["DaemonServer", "EventBroker", "LOOPBACK_HOST"]
