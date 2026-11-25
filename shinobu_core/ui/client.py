# -*- coding: utf-8 -*-
"""
Pure-stdlib loopback HTTP + SSE thin client for the local Shinobu daemon.

Design constraints:
  - Zero third-party dependencies (standard library only).
  - Strictly loopback-only (`127.0.0.1` / `localhost` / `::1`) and bypasses
    system HTTP proxies so local Bearer tokens never leave the machine.
  - Zero business logic: forwards REST/SSE frames between the daemon and
    lightweight desktop or CLI UI shells.
"""

from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import select
import socket
import ssl
import threading
from typing import Any, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse

from shinobu_core.core.kernel import default_home
from shinobu_core.protocol.events import Event

ENDPOINT_RELATIVE_PATH = Path("runtime") / "endpoint.json"
_ALLOWED_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class DaemonClientError(RuntimeError):
    """Raised when a local daemon HTTP/SSE request fails."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class DaemonAuthError(DaemonClientError):
    """Raised when the daemon rejects the Bearer token (HTTP 401/403)."""


def validate_loopback_url(url: str) -> str:
    """Validate that `url` targets an HTTP loopback endpoint and normalize it."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Daemon URL must be a non-empty string")
    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "http":
        raise ValueError(f"Daemon URL must use http:// scheme, got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_LOOPBACK_HOSTS:
        raise ValueError(
            f"DaemonClient refuses non-loopback host {host!r}; "
            "only loopback addresses are allowed"
        )
    if parsed.port is None or parsed.port <= 0 or parsed.port > 65535:
        raise ValueError(f"Daemon URL must include a valid port: {url!r}")
    return cleaned


def validate_daemon_url(url: str, *, allow_remote: bool = False) -> str:
    """Validate and normalize a daemon URL.

    When `allow_remote` is False, strictly delegates to `validate_loopback_url`.
    When `allow_remote` is True, permits loopback `http://` (with explicit port)
    or `https://` targeting any valid host (defaulting to port 443), while still
    forbidding plaintext `http://` to non-loopback hosts.
    """
    if not allow_remote:
        return validate_loopback_url(url)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Daemon URL must be a non-empty string")
    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        return validate_loopback_url(cleaned)
    if scheme != "https":
        raise ValueError(
            f"Remote daemon URL must use https:// (or loopback http://), got {parsed.scheme!r}"
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"Daemon URL must include a valid hostname: {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Daemon URL has an invalid port: {url!r}") from exc
    if port is not None and (port <= 0 or port > 65535):
        raise ValueError(f"Daemon URL port out of range: {url!r}")
    return cleaned


def _coerce_timestamp(raw_ts: Any) -> float | None:
    """Coerce numeric or ISO-8601 event timestamps into UNIX epoch float seconds."""
    if raw_ts is None or isinstance(raw_ts, bool):
        return None
    if isinstance(raw_ts, (int, float)):
        return float(raw_ts)
    if isinstance(raw_ts, str):
        cleaned = raw_ts.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            pass
        try:
            iso = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _parse_sse_frame(event_name: str, data_lines: list[str]) -> Event | None:
    """Convert accumulated SSE `event:` / `data:` lines into a canonical `Event`."""
    if not data_lines:
        return None
    raw_data = "\n".join(data_lines)
    try:
        parsed = json.loads(raw_data)
    except json.JSONDecodeError:
        kind = event_name.strip() or "message"
        return Event(kind=kind, payload={"raw": raw_data})

    if isinstance(parsed, dict):
        normalized = dict(parsed)
        if not normalized.get("kind") and event_name.strip():
            normalized["kind"] = event_name.strip()
        if "ts" in normalized:
            coerced_ts = _coerce_timestamp(normalized["ts"])
            if coerced_ts is None:
                normalized.pop("ts", None)
            else:
                normalized["ts"] = coerced_ts
        if isinstance(normalized.get("kind"), str) and normalized["kind"].strip():
            return Event.from_dict(normalized)
        return Event(kind=event_name.strip() or "message", payload=normalized)

    return Event(kind=event_name.strip() or "message", payload={"value": parsed})


def iter_sse_events(stream: Any) -> Iterator[Event]:
    """Parse UTF-8 SSE frames from a binary or text line-readable stream."""
    cur_event = ""
    cur_data: list[str] = []

    while True:
        raw_line = stream.readline()
        if not raw_line:
            break
        if isinstance(raw_line, (bytes, bytearray)):
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        else:
            line = str(raw_line).rstrip("\r\n")

        if line.startswith(":"):
            continue
        if line == "":
            ev = _parse_sse_frame(cur_event, cur_data)
            cur_event = ""
            cur_data = []
            if ev is not None:
                yield ev
            continue
        if line.startswith("event:"):
            cur_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            cur_data.append(line[len("data:") :].lstrip())

    # Flush trailing frame if the server closed without a final blank line.
    if cur_data:
        ev = _parse_sse_frame(cur_event, cur_data)
        if ev is not None:
            yield ev


def _decode_chunked_bytes(buf: bytearray) -> tuple[bytes, bool]:
    """Decode available HTTP/1.1 chunked-transfer bytes in place.

    Returns `(decoded_bytes, eof_reached)`.
    """
    out = bytearray()
    while True:
        line_end = buf.find(b"\r\n")
        if line_end == -1:
            break
        size_token = bytes(buf[:line_end]).split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_token, 16)
        except ValueError as exc:
            raise DaemonClientError(f"Malformed chunked SSE frame: {size_token!r}") from exc
        if chunk_size == 0:
            # Wait for trailing CRLF after terminal 0-size chunk if present.
            end_pos = line_end + 4
            if len(buf) >= end_pos:
                del buf[:end_pos]
            else:
                buf.clear()
            return bytes(out), True
        frame_end = line_end + 2 + chunk_size + 2
        if len(buf) < frame_end:
            break
        out.extend(buf[line_end + 2 : line_end + 2 + chunk_size])
        del buf[:frame_end]
    return bytes(out), False


class DaemonClient:
    """Thin HTTP(S)/SSE connector for a running `DaemonServer`."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 10.0,
        stream_timeout: float = 60.0,
        allow_remote: bool = False,
    ) -> None:
        self.allow_remote: bool = bool(allow_remote)
        self.url: str = validate_daemon_url(url, allow_remote=self.allow_remote)
        cleaned_token = str(token or "").strip()
        if not cleaned_token:
            raise ValueError("Bearer token must be a non-empty string")
        self.token: str = cleaned_token
        self.timeout: float = max(0.5, float(timeout))
        self.stream_timeout: float = max(1.0, float(stream_timeout))

        parsed = urlparse(self.url)
        self._scheme: str = parsed.scheme.lower()
        self._is_https: bool = self._scheme == "https"
        self._host: str = parsed.hostname or "127.0.0.1"
        self._port: int = int(
            parsed.port if parsed.port is not None else (443 if self._is_https else 80)
        )
        self._base_path: str = parsed.path.rstrip("/")
        self._ssl_ctx: ssl.SSLContext | None = (
            ssl.create_default_context() if self._is_https else None
        )
        handlers: list[urllib_request.BaseHandler] = []
        if self._host.lower() in _ALLOWED_LOOPBACK_HOSTS:
            # Explicitly disable proxies so local loopback calls never route through system proxies.
            handlers.append(urllib_request.ProxyHandler({}))
        if self._is_https and self._ssl_ctx is not None:
            handlers.append(urllib_request.HTTPSHandler(context=self._ssl_ctx))
        self._opener = urllib_request.build_opener(*handlers)

    def _full_path(self, path: str) -> str:
        norm = path if path.startswith("/") else f"/{path}"
        return f"{self._base_path}{norm}"

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        full_url = f"{self.url}{self._full_path(path)}"
        headers = self._auth_headers({"Accept": "application/json"})
        data_bytes: bytes | None = None
        if body is not None:
            data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib_request.Request(
            full_url,
            data=data_bytes,
            headers=headers,
            method=method.upper(),
        )
        req_timeout = self.timeout if timeout is None else max(0.2, float(timeout))
        try:
            with self._opener.open(req, timeout=req_timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", errors="replace")
            detail: Any = raw_err
            try:
                detail = json.loads(raw_err)
            except json.JSONDecodeError:
                pass
            if exc.code in (401, 403):
                raise DaemonAuthError(
                    f"Daemon authentication failed ({exc.code}) on {method} {path}",
                    status=exc.code,
                    detail=detail,
                ) from exc
            raise DaemonClientError(
                f"Daemon returned HTTP {exc.code} on {method} {path}: {detail}",
                status=exc.code,
                detail=detail,
            ) from exc
        except (urllib_error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            raise DaemonClientError(
                f"Failed to reach daemon at {full_url}: {exc}"
            ) from exc

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DaemonClientError(
                f"Invalid JSON returned from {method} {path}: {raw[:200]}"
            ) from exc
        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Probe `GET /health`."""
        return self._request_json("GET", "/health", timeout=timeout)

    def get_state(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Fetch current state snapshot via `GET /state`."""
        return self._request_json("GET", "/state", timeout=timeout)

    def set_state(
        self,
        state: str | None = None,
        mood: str | None = None,
        *,
        reason: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Update pet state and/or mood via `POST /api/state`."""
        if state is None and mood is None:
            raise ValueError("set_state requires at least one of `state` or `mood`")
        payload: dict[str, Any] = {}
        if state is not None:
            payload["state"] = state
        if mood is not None:
            payload["mood"] = mood
        if reason is not None:
            payload["reason"] = reason
        return self._request_json("POST", "/api/state", body=payload, timeout=timeout)

    def sleep(
        self,
        *,
        date_str: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Trigger sleep-time episode consolidation via `POST /sleep`."""
        payload: dict[str, Any] = {}
        if date_str is not None:
            payload["date_str"] = date_str
        return self._request_json("POST", "/sleep", body=payload, timeout=timeout)

    def get_turns(
        self,
        episode_id: str | None = None,
        limit: int = 50,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Fetch conversation turns via `GET /api/turns`."""
        params: dict[str, Any] = {"limit": int(limit)}
        if episode_id is not None and str(episode_id).strip():
            params["episode_id"] = str(episode_id).strip()
        return self._request_json(
            "GET",
            f"/api/turns?{urlencode(params)}",
            timeout=timeout,
        )

    def list_episodes(
        self,
        limit: int = 8,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """List active and recent closed episodes via `GET /api/episodes`."""
        return self._request_json(
            "GET",
            f"/api/episodes?{urlencode({'limit': int(limit)})}",
            timeout=timeout,
        )

    def new_episode(
        self,
        summary: str = "",
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Archive current episode and start a fresh one via `POST /api/episodes/new`."""
        return self._request_json(
            "POST",
            "/api/episodes/new",
            body={"summary": str(summary or "")},
            timeout=timeout,
        )

    def get_engine(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Fetch current engine configuration via `GET /api/engine`."""
        return self._request_json("GET", "/api/engine", timeout=timeout)

    def set_route(
        self,
        mode: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Switch hybrid engine routing mode via `POST /api/engine/route`."""
        return self._request_json(
            "POST",
            "/api/engine/route",
            body={"mode": str(mode)},
            timeout=timeout,
        )

    def set_reasoning(
        self,
        effort: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Switch engine reasoning effort via `POST /api/engine/reasoning`."""
        return self._request_json(
            "POST",
            "/api/engine/reasoning",
            body={"reasoning_effort": str(effort)},
            timeout=timeout,
        )

    def list_workers(self, *, timeout: float | None = None) -> dict[str, Any]:
        """List registered remote workers via `GET /api/workers`."""
        return self._request_json("GET", "/api/workers", timeout=timeout)

    def list_facts(
        self,
        limit: int = 100,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """List stored long-term memory facts via `GET /api/memory/facts`."""
        return self._request_json(
            "GET",
            f"/api/memory/facts?{urlencode({'limit': int(limit)})}",
            timeout=timeout,
        )

    def upsert_fact(
        self,
        key: str,
        value: str,
        confidence: float = 0.8,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Create or update a long-term memory fact via `POST /api/memory/facts`."""
        return self._request_json(
            "POST",
            "/api/memory/facts",
            body={
                "key": str(key),
                "value": str(value),
                "confidence": float(confidence),
            },
            timeout=timeout,
        )

    def delete_fact(
        self,
        fact_id: int,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Delete a long-term memory fact via `DELETE /api/memory/facts/<id>`."""
        return self._request_json(
            "DELETE",
            f"/api/memory/facts/{int(fact_id)}",
            timeout=timeout,
        )

    def synthesize_tts(
        self,
        text: str,
        *,
        mood: str = "calm",
        fmt: str = "wav",
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """Synthesize speech via `POST /api/tts`, returning `(audio_bytes, content_type)`."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text must be a non-empty string")
        full_url = f"{self.url}{self._full_path('/api/tts')}"
        payload_bytes = json.dumps(
            {
                "text": text.strip(),
                "mood": str(mood or "calm"),
                "fmt": str(fmt or "wav"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = self._auth_headers(
            {
                "Accept": "*/*",
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        req = urllib_request.Request(
            full_url,
            data=payload_bytes,
            headers=headers,
            method="POST",
        )
        req_timeout = self.timeout if timeout is None else max(0.2, float(timeout))
        try:
            with self._opener.open(req, timeout=req_timeout) as resp:
                audio_bytes = resp.read()
                content_type = str(
                    resp.headers.get("Content-Type") or "application/octet-stream"
                ).strip()
                return audio_bytes, content_type
        except urllib_error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", errors="replace")
            detail: Any = raw_err
            try:
                detail = json.loads(raw_err)
            except json.JSONDecodeError:
                pass
            if exc.code in (401, 403):
                raise DaemonAuthError(
                    f"Daemon authentication failed ({exc.code}) on POST /api/tts",
                    status=exc.code,
                    detail=detail,
                ) from exc
            raise DaemonClientError(
                f"Daemon returned HTTP {exc.code} on POST /api/tts: {detail}",
                status=exc.code,
                detail=detail,
            ) from exc
        except (urllib_error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            raise DaemonClientError(
                f"Failed to reach daemon at {full_url}: {exc}"
            ) from exc

    def chat_stream(
        self,
        text: str,
        *,
        timeout: float | None = None,
    ) -> Iterator[Event]:
        """Send `POST /chat` (SSE streaming mode) and yield canonical `Event` objects."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Chat text must be a non-empty string")
        payload_bytes = json.dumps(
            {"text": text, "stream": True},
            ensure_ascii=False,
        ).encode("utf-8")
        req_timeout = self.stream_timeout if timeout is None else max(0.5, float(timeout))

        if self._is_https:
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=req_timeout,
                context=self._ssl_ctx,
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=req_timeout)
        try:
            conn.request(
                "POST",
                self._full_path("/chat"),
                body=payload_bytes,
                headers=self._auth_headers(
                    {
                        "Content-Type": "application/json; charset=utf-8",
                        "Accept": "text/event-stream",
                        "Content-Length": str(len(payload_bytes)),
                        "Connection": "close",
                    }
                ),
            )
            resp = conn.getresponse()
            if resp.status in (401, 403):
                raw_err = resp.read().decode("utf-8", errors="replace")
                raise DaemonAuthError(
                    f"Daemon authentication failed ({resp.status}) on POST /chat",
                    status=resp.status,
                    detail=raw_err,
                )
            if resp.status != 200:
                raw_err = resp.read().decode("utf-8", errors="replace")
                raise DaemonClientError(
                    f"Daemon returned HTTP {resp.status} on POST /chat: {raw_err}",
                    status=resp.status,
                    detail=raw_err,
                )
            yield from iter_sse_events(resp)
        except DaemonClientError:
            raise
        except (http.client.HTTPException, ssl.SSLError, TimeoutError, OSError) as exc:
            raise DaemonClientError(f"Streaming POST /chat failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _http_host_header(self) -> str:
        host_fmt = f"[{self._host}]" if ":" in self._host else self._host
        default_port = 443 if self._is_https else 80
        if self._port == default_port:
            return host_fmt
        return f"{host_fmt}:{self._port}"

    def _open_sse_socket(
        self,
        *,
        connect_timeout: float,
    ) -> tuple[socket.socket, bytes, bool]:
        """Open a TCP (or TLS) socket to `GET /events` and parse response headers.

        Returns `(sock, initial_body_bytes, is_chunked)`.
        """
        raw_sock = socket.create_connection((self._host, self._port), timeout=connect_timeout)
        sock: socket.socket = raw_sock
        try:
            if self._is_https:
                ctx = self._ssl_ctx or ssl.create_default_context()
                sock = ctx.wrap_socket(raw_sock, server_hostname=self._host)

            req_text = (
                f"GET {self._full_path('/events')} HTTP/1.1\r\n"
                f"Host: {self._http_host_header()}\r\n"
                f"Authorization: Bearer {self.token}\r\n"
                "Accept: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(req_text.encode("utf-8"))

            header_buf = bytearray()
            while b"\r\n\r\n" not in header_buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise DaemonClientError("SSE connection closed before headers completed")
                header_buf.extend(chunk)
                if len(header_buf) > 65536:
                    raise DaemonClientError("SSE response headers exceeded 64 KiB limit")

            sep_idx = header_buf.index(b"\r\n\r\n")
            raw_head = bytes(header_buf[:sep_idx]).decode("iso-8859-1", errors="replace")
            leftover = bytes(header_buf[sep_idx + 4 :])

            lines = raw_head.split("\r\n")
            status_line = lines[0] if lines else ""
            parts = status_line.split(" ", 2)
            try:
                status_code = int(parts[1])
            except (IndexError, ValueError) as exc:
                raise DaemonClientError(f"Malformed HTTP status line: {status_line!r}") from exc

            headers: dict[str, str] = {}
            for hdr_line in lines[1:]:
                if ":" in hdr_line:
                    k, v = hdr_line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if status_code in (401, 403):
                raise DaemonAuthError(
                    f"Daemon authentication failed ({status_code}) on GET /events",
                    status=status_code,
                    detail=leftover.decode("utf-8", errors="replace"),
                )
            if status_code != 200:
                raise DaemonClientError(
                    f"Daemon returned HTTP {status_code} on GET /events",
                    status=status_code,
                    detail=leftover.decode("utf-8", errors="replace"),
                )

            is_chunked = "chunked" in headers.get("transfer-encoding", "").lower()
            sock.settimeout(None)
            return sock, leftover, is_chunked
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def iter_events(
        self,
        stop_event: threading.Event,
        *,
        initial_backoff: float = 0.25,
        max_backoff: float = 5.0,
        backoff_factor: float = 2.0,
        connect_timeout: float = 3.0,
        poll_interval: float = 0.2,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[Exception | None], None] | None = None,
    ) -> Iterator[Event]:
        """Subscribe to `GET /events` with exponential backoff reconnection until `stop_event` is set."""
        backoff = max(0.05, float(initial_backoff))
        ceiling = max(backoff, float(max_backoff))
        factor = max(1.1, float(backoff_factor))
        poll = max(0.05, min(1.0, float(poll_interval)))

        while not stop_event.is_set():
            sock: socket.socket | None = None
            disconnect_err: Exception | None = None
            try:
                sock, leftover, is_chunked = self._open_sse_socket(
                    connect_timeout=max(0.2, float(connect_timeout))
                )
                backoff = max(0.05, float(initial_backoff))
                if on_connect is not None:
                    on_connect()

                raw_buf = bytearray(leftover)
                text_buf = bytearray()
                cur_event = ""
                cur_data: list[str] = []

                while not stop_event.is_set():
                    if is_chunked:
                        decoded, chunk_eof = _decode_chunked_bytes(raw_buf)
                        if decoded:
                            text_buf.extend(decoded)
                    else:
                        chunk_eof = False
                        if raw_buf:
                            text_buf.extend(raw_buf)
                            raw_buf.clear()

                    while True:
                        nl_idx = text_buf.find(b"\n")
                        if nl_idx == -1:
                            break
                        raw_line = bytes(text_buf[: nl_idx + 1])
                        del text_buf[: nl_idx + 1]
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line.startswith(":"):
                            continue
                        if line == "":
                            ev = _parse_sse_frame(cur_event, cur_data)
                            cur_event = ""
                            cur_data = []
                            if ev is not None:
                                yield ev
                            continue
                        if line.startswith("event:"):
                            cur_event = line[len("event:") :].strip()
                        elif line.startswith("data:"):
                            cur_data.append(line[len("data:") :].lstrip())

                    if chunk_eof or stop_event.is_set():
                        break

                    has_pending = isinstance(sock, ssl.SSLSocket) and sock.pending() > 0
                    if not has_pending:
                        readable, _, _ = select.select([sock], [], [], poll)
                        if not readable:
                            continue
                    chunk = sock.recv(4096)
                    if not chunk:
                        # Server closed the stream; reconnect with backoff.
                        break
                    raw_buf.extend(chunk)
            except DaemonAuthError:
                raise
            except (DaemonClientError, ssl.SSLError, OSError) as exc:
                disconnect_err = exc
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if stop_event.is_set():
                break
            if on_disconnect is not None:
                on_disconnect(disconnect_err)
            if stop_event.wait(backoff):
                break
            backoff = min(ceiling, backoff * factor)


class EventStreamWorker:
    """Background daemon thread running `DaemonClient.iter_events`."""

    def __init__(
        self,
        client: DaemonClient,
        on_event: Callable[[Event], None],
        *,
        on_status: Callable[[bool, str], None] | None = None,
        initial_backoff: float = 0.25,
        max_backoff: float = 5.0,
        poll_interval: float = 0.2,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._on_status = on_status
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._poll_interval = poll_interval

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="shinobu-sse-client",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run(self) -> None:
        def _handle_connect() -> None:
            if self._on_status is not None:
                self._on_status(True, "connected")

        def _handle_disconnect(err: Exception | None) -> None:
            if self._on_status is not None:
                detail = str(err) if err is not None else "stream closed"
                self._on_status(False, detail)

        try:
            for ev in self._client.iter_events(
                self._stop_event,
                initial_backoff=self._initial_backoff,
                max_backoff=self._max_backoff,
                poll_interval=self._poll_interval,
                on_connect=_handle_connect,
                on_disconnect=_handle_disconnect,
            ):
                if self._stop_event.is_set():
                    break
                self._on_event(ev)
        except DaemonAuthError as exc:
            if self._on_status is not None:
                self._on_status(False, f"auth_error: {exc}")


def discover_endpoint(
    home: Path | str | None = None,
    *,
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    """Read `<home>/runtime/endpoint.json` and verify liveness via `GET /health`."""
    base_home = Path(home) if home is not None else default_home()
    endpoint_file = base_home / ENDPOINT_RELATIVE_PATH
    if not endpoint_file.is_file():
        return None
    try:
        raw = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    url = raw.get("url")
    token = raw.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        return None
    try:
        client = DaemonClient(url=url, token=token, timeout=timeout)
        health_info = client.health(timeout=timeout)
    except (ValueError, DaemonClientError):
        return None

    if not isinstance(health_info, dict) or health_info.get("status") != "ok":
        return None

    result = dict(raw)
    result["url"] = client.url
    result["health"] = health_info
    return result


__all__ = [
    "DaemonAuthError",
    "DaemonClient",
    "DaemonClientError",
    "ENDPOINT_RELATIVE_PATH",
    "EventStreamWorker",
    "discover_endpoint",
    "iter_sse_events",
    "validate_loopback_url",
]
