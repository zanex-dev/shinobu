# -*- coding: utf-8 -*-
"""Local edge node agent (HTTP server + cloud keepalive client) for home PC nodes."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from shinobu_core.cloud.approval import CommandTier, classify_command
from shinobu_core.engines.openai_compat import normalize_reasoning_effort
from shinobu_core.protocol.engine import EngineAdapter, TurnChunk, TurnRequest

_LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Explicit private networks permitted for binding and local forwarding.
_ALLOWED_PRIVATE_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("100.64.0.0/10"),    # Tailscale / RFC 6598 CGNAT
    ipaddress.ip_network("10.0.0.0/8"),       # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),    # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC 1918
)
_ALLOWED_PRIVATE_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("fc00::/7"),         # ULA (includes Tailscale fd7a:115c:a1e0::/48)
)

_UNIX_ABS_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/root",
    "/home",
    "/var",
    "/tmp",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/opt",
)


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    except Exception:
        return False
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str) -> str:
    """Validate that bind_host is loopback or an explicit private/Tailscale IP.

    Strictly rejects 0.0.0.0, ::, wildcard, public IPs, and non-localhost hostnames.
    """
    if not isinstance(host, str):
        raise ValueError("bind_host must be a string")
    cleaned = host.strip()
    if not cleaned or cleaned in {"0.0.0.0", "::", "*", "[::]"}:
        raise ValueError(f"bind_host '{host}' is forbidden: wildcard/unspecified address is not allowed")

    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1].strip()

    lowered = cleaned.lower()
    if lowered == "localhost":
        return "127.0.0.1"

    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"bind_host '{host}' is forbidden: must be localhost or an explicit loopback/private IP"
        ) from exc

    if addr.is_unspecified or addr.is_multicast or addr.is_link_local:
        raise ValueError(f"bind_host '{host}' is forbidden: non-routable or wildcard address")

    if isinstance(addr, ipaddress.IPv4Address):
        if any(addr in net for net in _ALLOWED_PRIVATE_V4):
            return str(addr)
        raise ValueError(f"bind_host '{host}' is forbidden: public IPv4 address is not allowed")

    if addr.is_loopback or any(addr in net for net in _ALLOWED_PRIVATE_V6):
        return str(addr)
    raise ValueError(f"bind_host '{host}' is forbidden: public IPv6 address is not allowed")


def _validate_private_http_url(url: str, *, label: str) -> str:
    cleaned = str(url).strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be a valid http(s) URL")
    validate_bind_host(parsed.hostname)
    return cleaned


def _format_host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


@dataclass(frozen=True)
class _ProxyToolSpec:
    """Duck-typed ToolSpec reconstruction for remote /node/turn tool schemas."""

    name: str
    description: str
    parameters: dict[str, Any]
    raw_schema: dict[str, Any]

    def to_schema(self) -> dict[str, Any]:
        return dict(self.raw_schema)


def _deserialize_tools(raw_tools: Any) -> list[Any]:
    if not isinstance(raw_tools, list):
        return []
    specs: list[Any] = []
    for item in raw_tools:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("function"), Mapping):
            fn = item["function"]
            name = str(fn.get("name") or "")
            description = str(fn.get("description") or "")
            params = dict(fn.get("parameters")) if isinstance(fn.get("parameters"), Mapping) else {}
            raw_schema = dict(item)
        else:
            name = str(item.get("name") or "")
            description = str(item.get("description") or "")
            params = dict(item.get("parameters")) if isinstance(item.get("parameters"), Mapping) else {}
            raw_schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            }
        if name:
            specs.append(
                _ProxyToolSpec(
                    name=name,
                    description=description,
                    parameters=params,
                    raw_schema=raw_schema,
                )
            )
    return specs


def _is_within_roots(candidate: Path, roots: Sequence[Path]) -> bool:
    norm_cand = Path(os.path.normcase(str(candidate)))
    for root in roots:
        norm_root = Path(os.path.normcase(str(root)))
        try:
            norm_cand.relative_to(norm_root)
            return True
        except ValueError:
            continue
    return False


def _has_parent_traversal_segment(token: str) -> bool:
    parts = token.replace("\\", "/").split("/")
    return ".." in parts


def _looks_like_unix_absolute_path(token: str) -> bool:
    if not token.startswith("/") or len(token) < 2:
        return False
    lowered = token.lower()
    if any(lowered == p or lowered.startswith(p + "/") for p in _UNIX_ABS_PREFIXES):
        return True
    # Multi-segment leading slash path such as /some/outside/file
    return token.count("/") >= 2


def _extract_path_operand(token: str) -> str | None:
    cleaned = token.strip().strip('"').strip("'")
    if not cleaned:
        return None
    if "=" in cleaned and cleaned.startswith("-"):
        _, value = cleaned.split("=", 1)
        cleaned = value.strip().strip('"').strip("'")
    return cleaned or None


def _validate_argv_paths(args: Sequence[str], cwd: Path, roots: Sequence[Path]) -> str | None:
    """Return an error reason if any argv operand escapes allowed_roots, else None."""
    for raw in args:
        if "\x00" in raw:
            return "command argument contains NUL byte"
        operand = _extract_path_operand(raw)
        if not operand:
            continue

        if _looks_like_unix_absolute_path(operand):
            resolved = Path(operand).resolve(strict=False)
            if not _is_within_roots(resolved, roots):
                return f"path argument '{raw}' escapes allowed_roots"
            continue

        candidate_path = Path(operand).expanduser()
        if candidate_path.is_absolute():
            resolved = candidate_path.resolve(strict=False)
            if not _is_within_roots(resolved, roots):
                return f"absolute path argument '{raw}' escapes allowed_roots"
            continue

        if _has_parent_traversal_segment(operand):
            resolved = (cwd / candidate_path).resolve(strict=False)
            if not _is_within_roots(resolved, roots):
                return f"relative path traversal '{raw}' escapes allowed_roots"
    return None


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    truncated = raw[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _extract_cloud_approval(payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate explicit cloud approval marker on L1 exec requests."""
    if payload.get("cloud_approved") is True:
        approval_id = str(payload.get("approval_id") or "").strip()
        if approval_id:
            return True, approval_id
    approval = payload.get("approval")
    if isinstance(approval, Mapping) and approval.get("approved") is True:
        approval_id = str(approval.get("approval_id") or payload.get("approval_id") or "").strip()
        if approval_id:
            return True, approval_id
    return False, ""


class _EdgeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        edge_node: EdgeNodeServer,
    ) -> None:
        self.edge_node = edge_node
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, handler_cls)


class _EdgeNodeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress noisy stderr logging during daemon operation and unit tests.
        return

    @property
    def _node(self) -> EdgeNodeServer:
        return self.server.edge_node  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not self._node.verify_bearer_token(auth_header):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._send_json(400, {"ok": False, "error": "missing Content-Length header"})
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid Content-Length header"})
            return None
        if length < 0 or length > self._node.max_body_bytes:
            self._send_json(413, {"ok": False, "error": "request body exceeds maximum allowed size"})
            return None
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return None
        if not isinstance(parsed, dict):
            self._send_json(400, {"ok": False, "error": "request body must be a JSON object"})
            return None
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/node/health":
            self._send_json(200, self._node.handle_health())
            return
        self._send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})

    def _write_sse_event(self, payload: Mapping[str, Any]) -> None:
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(line)
        self.wfile.flush()

    def _send_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _handle_turn_sse(self, body: Mapping[str, Any]) -> None:
        if self._node.local_engine is None:
            self._send_json(503, {"ok": False, "error": "local_engine is not configured"})
            return

        req = self._node._build_turn_request(body)
        headers_sent = False
        saw_done = False
        try:
            for chunk in self._node.local_engine.stream_turn(req):
                if not isinstance(chunk, TurnChunk):
                    continue
                if not headers_sent:
                    self._send_sse_headers()
                    headers_sent = True
                event = self._node._serialize_turn_chunk(chunk)
                if event["done"]:
                    saw_done = True
                self._write_sse_event(event)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            if not headers_sent:
                self._send_json(502, {"ok": False, "error": f"local_engine stream_turn failed: {exc}"})
                return
            try:
                self._write_sse_event(
                    {
                        "delta": "",
                        "tool_calls": [],
                        "usage": None,
                        "done": True,
                        "ok": False,
                        "error": f"local_engine stream_turn failed: {exc}",
                    }
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if not headers_sent:
            self._send_sse_headers()
        if not saw_done:
            try:
                self._write_sse_event(
                    {"delta": "", "tool_calls": [], "usage": None, "done": True}
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_json_body()
        if body is None:
            return

        if path == "/node/turn":
            mode = str(body.get("mode") or "stream_turn").strip().lower()
            accept = str(self.headers.get("Accept") or "").lower()
            if mode == "stream_turn" and "text/event-stream" in accept:
                self._handle_turn_sse(body)
                return
            status, payload = self._node.handle_turn(body)
            self._send_json(status, payload)
            return
        if path == "/node/exec":
            status, payload = self._node.handle_exec(body)
            self._send_json(status, payload)
            return
        if path == "/node/bubble":
            status, payload = self._node.handle_bubble(body)
            self._send_json(status, payload)
            return
        self._send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})


class EdgeNodeServer:
    """Secure local HTTP bridge exposing local engine, controlled exec, and bubble relay."""

    def __init__(
        self,
        local_engine: EngineAdapter | None,
        *,
        node_token: str,
        allowed_roots: Sequence[Path | str],
        bind_host: str = "127.0.0.1",
        port: int = 18792,
        airi_bubble_url: str = "",
        airi_bubble_token: str = "",
        bubble_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        exec_timeout_sec: float = 30.0,
        max_output_bytes: int = 65536,
        max_body_bytes: int = 2 * 1024 * 1024,
        worker_id: str = "home_pc",
    ) -> None:
        if not isinstance(node_token, str) or not node_token.strip():
            raise ValueError("node_token must be a non-empty string")
        validated_host = validate_bind_host(bind_host)

        port_int = int(port)
        if port_int < 0 or port_int > 65535:
            raise ValueError("port must be between 0 and 65535")

        timeout_val = float(exec_timeout_sec)
        if timeout_val <= 0:
            raise ValueError("exec_timeout_sec must be greater than 0")

        validated_bubble_url = ""
        if airi_bubble_url and str(airi_bubble_url).strip():
            validated_bubble_url = _validate_private_http_url(
                str(airi_bubble_url),
                label="airi_bubble_url",
            )

        self.local_engine: EngineAdapter | None = local_engine
        self._node_token: str = node_token.strip()
        self.allowed_roots: tuple[Path, ...] = tuple(
            Path(root).expanduser().resolve(strict=False) for root in allowed_roots
        )
        self.bind_host: str = validated_host
        self._requested_port: int = port_int
        self.airi_bubble_url: str = validated_bubble_url
        self._airi_bubble_token: str = str(airi_bubble_token).strip()
        self._bubble_handler = bubble_handler
        self.exec_timeout_sec: float = timeout_val
        self.max_output_bytes: int = max(256, int(max_output_bytes))
        self.max_body_bytes: int = max(1024, int(max_body_bytes))
        self.worker_id: str = str(worker_id).strip() or "home_pc"

        self._lock = threading.RLock()
        self._httpd: _EdgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._httpd is not None

    @property
    def port(self) -> int:
        with self._lock:
            if self._httpd is not None:
                return int(self._httpd.server_address[1])
            return self._requested_port

    @property
    def base_url(self) -> str:
        return f"http://{_format_host_for_url(self.bind_host)}:{self.port}"

    def verify_bearer_token(self, authorization_header: str) -> bool:
        """Constant-time Bearer token comparison."""
        if not isinstance(authorization_header, str):
            return False
        parts = authorization_header.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        provided = parts[1].strip()
        if not provided:
            return False
        return hmac.compare_digest(
            provided.encode("utf-8"),
            self._node_token.encode("utf-8"),
        )

    def advertised_capabilities(self) -> tuple[str, ...]:
        caps: list[str] = []
        if self.local_engine is not None:
            caps.append("brain")
        if self.allowed_roots:
            caps.append("terminal")
        if self.airi_bubble_url or self._bubble_handler is not None:
            caps.append("bubble")
        return tuple(caps)

    def handle_health(self) -> dict[str, Any]:
        uptime = 0.0
        if self._started_at is not None:
            uptime = max(0.0, time.time() - self._started_at)
        return {
            "ok": True,
            "status": "ok",
            "worker_id": self.worker_id,
            "has_local_engine": self.local_engine is not None,
            "engine_name": str(getattr(self.local_engine, "name", "")) if self.local_engine else "",
            "engine_model": str(getattr(self.local_engine, "model", "")) if self.local_engine else "",
            "capabilities": list(self.advertised_capabilities()),
            "allowed_roots": [str(p) for p in self.allowed_roots],
            "airi_bubble_configured": bool(self.airi_bubble_url or self._bubble_handler is not None),
            "uptime_sec": round(uptime, 3),
        }

    @staticmethod
    def _build_turn_request(payload: Mapping[str, Any]) -> TurnRequest:
        raw_history = payload.get("history")
        history = [dict(item) for item in raw_history if isinstance(item, Mapping)] if isinstance(raw_history, list) else []
        raw_effort = payload.get("reasoning_effort")
        reasoning_effort = (
            normalize_reasoning_effort(raw_effort)
            if raw_effort is not None
            else ""
        )
        return TurnRequest(
            episode_id=str(payload.get("episode_id") or ""),
            system_prefix=str(payload.get("system_prefix") or ""),
            history=history,
            user_text=str(payload.get("user_text") or ""),
            tools=_deserialize_tools(payload.get("tools")),
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    def _serialize_turn_chunk(chunk: TurnChunk) -> dict[str, Any]:
        tool_calls: list[dict[str, Any]] = []
        if chunk.tool_calls:
            for tc in chunk.tool_calls:
                if isinstance(tc, Mapping):
                    tool_calls.append(dict(tc))
        usage = dict(chunk.usage) if isinstance(chunk.usage, Mapping) else None
        return {
            "delta": str(chunk.delta or ""),
            "tool_calls": tool_calls,
            "usage": usage,
            "done": bool(chunk.done),
        }

    def handle_turn(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.local_engine is None:
            return 503, {"ok": False, "error": "local_engine is not configured"}

        mode = str(payload.get("mode") or "stream_turn").strip().lower()
        if mode == "complete_json":
            system = str(payload.get("system") or "")
            prompt = str(payload.get("prompt") or "")
            try:
                result = self.local_engine.complete_json(system, prompt)
            except Exception as exc:
                return 502, {"ok": False, "error": f"local_engine complete_json failed: {exc}"}
            if not isinstance(result, dict):
                return 502, {"ok": False, "error": "local_engine complete_json did not return a dict"}
            return 200, {"ok": True, "result": result}

        if mode != "stream_turn":
            return 400, {"ok": False, "error": f"unsupported turn mode: {mode}"}

        req = self._build_turn_request(payload)

        delta_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, Any] | None = None

        try:
            for chunk in self.local_engine.stream_turn(req):
                if not isinstance(chunk, TurnChunk):
                    continue
                if chunk.delta:
                    delta_parts.append(str(chunk.delta))
                if chunk.tool_calls:
                    for tc in chunk.tool_calls:
                        if isinstance(tc, Mapping):
                            tool_calls.append(dict(tc))
                if isinstance(chunk.usage, Mapping):
                    usage = dict(chunk.usage)
        except Exception as exc:
            return 502, {"ok": False, "error": f"local_engine stream_turn failed: {exc}"}

        return 200, {
            "ok": True,
            "delta": "".join(delta_parts),
            "tool_calls": tool_calls,
            "usage": usage,
            "done": True,
        }

    def handle_exec(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if payload.get("shell") is True:
            return 400, {"ok": False, "error": "shell=True is strictly forbidden"}

        raw_argv = payload.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            return 400, {"ok": False, "error": "argv must be a non-empty list of strings"}

        argv: list[str] = []
        for item in raw_argv:
            if not isinstance(item, str) or not item.strip():
                return 400, {"ok": False, "error": "every argv item must be a non-empty string"}
            if "\x00" in item:
                return 400, {"ok": False, "error": "argv item contains NUL byte"}
            argv.append(item)

        if not self.allowed_roots:
            return 403, {"ok": False, "error": "no allowed_roots configured for local execution"}

        raw_cwd = payload.get("cwd")
        if raw_cwd is None or str(raw_cwd).strip() == "":
            resolved_cwd = self.allowed_roots[0]
        else:
            cwd_str = str(raw_cwd)
            if "\x00" in cwd_str:
                return 400, {"ok": False, "error": "cwd contains NUL byte"}
            cwd_path = Path(cwd_str).expanduser()
            if not cwd_path.is_absolute():
                cwd_path = self.allowed_roots[0] / cwd_path
            resolved_cwd = cwd_path.resolve(strict=False)

        if not _is_within_roots(resolved_cwd, self.allowed_roots):
            return 403, {
                "ok": False,
                "error": f"cwd '{resolved_cwd}' is outside allowed_roots",
            }
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            return 400, {
                "ok": False,
                "error": f"cwd '{resolved_cwd}' does not exist or is not a directory",
            }

        path_err = _validate_argv_paths(argv[1:], resolved_cwd, self.allowed_roots)
        if path_err is not None:
            return 403, {"ok": False, "error": path_err}

        tier, reason = classify_command(argv)
        if tier is CommandTier.L2_FORBIDDEN:
            return 403, {
                "ok": False,
                "error": f"L2 forbidden: {reason}",
                "tier": tier.value,
                "reason": reason,
            }

        approval_id = ""
        if tier is CommandTier.L1_APPROVAL:
            approved, approval_id = _extract_cloud_approval(payload)
            if not approved:
                return 403, {
                    "ok": False,
                    "error": f"L1 command requires cloud approval: {reason}",
                    "tier": tier.value,
                    "requires_approval": True,
                    "reason": reason,
                }

        req_timeout = payload.get("timeout_sec")
        timeout = self.exec_timeout_sec
        if req_timeout is not None:
            try:
                parsed_timeout = float(req_timeout)
            except (TypeError, ValueError):
                return 400, {"ok": False, "error": "timeout_sec must be a positive number"}
            if parsed_timeout <= 0:
                return 400, {"ok": False, "error": "timeout_sec must be greater than 0"}
            timeout = min(parsed_timeout, self.exec_timeout_sec)

        try:
            proc = subprocess.run(
                argv,
                cwd=str(resolved_cwd),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 504, {
                "ok": False,
                "error": f"command timed out after {timeout:.1f}s",
                "tier": tier.value,
                "timed_out": True,
            }
        except FileNotFoundError:
            return 400, {
                "ok": False,
                "error": f"executable not found: {argv[0]}",
                "tier": tier.value,
            }
        except OSError as exc:
            return 500, {
                "ok": False,
                "error": f"process execution error: {exc}",
                "tier": tier.value,
            }

        stdout, out_trunc = _truncate_text_bytes(proc.stdout or "", self.max_output_bytes)
        stderr, err_trunc = _truncate_text_bytes(proc.stderr or "", self.max_output_bytes)
        return 200, {
            "ok": True,
            "tier": tier.value,
            "argv": argv,
            "cwd": str(resolved_cwd),
            "exit_code": int(proc.returncode),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": out_trunc or err_trunc,
            "approval_id": approval_id,
        }

    def handle_bubble(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        text = payload.get("text")
        if text is None:
            text = payload.get("message")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "bubble payload requires non-empty 'text' or 'message'"}

        forward_payload = dict(payload)
        forward_payload.setdefault("text", text)

        if self._bubble_handler is not None:
            try:
                res = self._bubble_handler(forward_payload)
            except Exception as exc:
                return 502, {"ok": False, "forwarded": False, "error": f"bubble handler failed: {exc}"}
            return 200, {"ok": True, "forwarded": True, "response": res if res is not None else {}}

        if not self.airi_bubble_url:
            return 503, {"ok": False, "forwarded": False, "error": "airi_bubble_url is not configured"}

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._airi_bubble_token:
            headers["Authorization"] = f"Bearer {self._airi_bubble_token}"

        data = json.dumps(forward_payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.airi_bubble_url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            opener = _NO_PROXY_OPENER if _is_loopback_url(self.airi_bubble_url) else None
            resp_ctx = opener.open(req, timeout=10.0) if opener else urllib.request.urlopen(req, timeout=10.0)
            with resp_ctx as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace").strip()
                parsed_resp: Any = {}
                if raw_resp:
                    try:
                        parsed_resp = json.loads(raw_resp)
                    except json.JSONDecodeError:
                        parsed_resp = {"raw": raw_resp}
                return 200, {
                    "ok": True,
                    "forwarded": True,
                    "status_code": int(getattr(resp, "status", 200)),
                    "response": parsed_resp,
                }
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = str(exc.reason)
            return 502, {
                "ok": False,
                "forwarded": False,
                "error": f"upstream bubble HTTP {exc.code}: {detail}",
            }
        except Exception as exc:
            return 502, {
                "ok": False,
                "forwarded": False,
                "error": f"upstream bubble relay error: {exc}",
            }

    def start(self) -> str:
        """Start the background HTTP server thread and return its base URL."""
        with self._lock:
            if self._httpd is not None:
                return self.base_url
            httpd = _EdgeHTTPServer(
                (self.bind_host, self._requested_port),
                _EdgeNodeRequestHandler,
                self,
            )
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.2},
                name=f"edge-node-server-{self.worker_id}",
                daemon=True,
            )
            self._httpd = httpd
            self._thread = thread
            self._started_at = time.time()
            thread.start()
            return self.base_url

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Gracefully shut down the HTTP server and release its socket."""
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
            self._started_at = None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        close_fn = getattr(self.local_engine, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def __enter__(self) -> EdgeNodeServer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


class EdgeNodeClient:
    """Cloud registration, periodic heartbeat, and graceful unregister client."""

    def __init__(
        self,
        cloud_url: str,
        cloud_token: str,
        *,
        worker_id: str = "home_pc",
        advertise_url: str,
        node_token: str,
        capabilities: Iterable[str] = ("brain", "terminal", "bubble"),
        heartbeat_sec: float = 15.0,
        ttl_sec: float | None = None,
        role: str = "edge_node",
        meta: Mapping[str, Any] | None = None,
        request_timeout_sec: float = 10.0,
        on_error: Callable[[str, Exception], None] | None = None,
        on_registered: Callable[[bool], None] | None = None,
        presence_enabled: bool = True,
        presence_tracker: Any | None = None,
    ) -> None:
        cloud_cleaned = str(cloud_url).strip().rstrip("/")
        parsed_cloud = urllib.parse.urlparse(cloud_cleaned)
        if parsed_cloud.scheme not in {"http", "https"} or not parsed_cloud.netloc:
            raise ValueError("cloud_url must be a valid http(s) URL")

        adv_cleaned = str(advertise_url).strip().rstrip("/")
        parsed_adv = urllib.parse.urlparse(adv_cleaned)
        if parsed_adv.scheme not in {"http", "https"} or not parsed_adv.netloc:
            raise ValueError("advertise_url must be a valid http(s) URL")

        wid = str(worker_id).strip()
        if not wid:
            raise ValueError("worker_id must be a non-empty string")

        hb_sec = float(heartbeat_sec)
        if hb_sec <= 0:
            raise ValueError("heartbeat_sec must be greater than 0")

        computed_ttl = max(hb_sec * 3.0, 45.0) if ttl_sec is None else float(ttl_sec)
        if computed_ttl <= 0:
            raise ValueError("ttl_sec must be greater than 0")

        req_timeout = float(request_timeout_sec)
        if req_timeout <= 0:
            raise ValueError("request_timeout_sec must be greater than 0")

        self.cloud_url: str = cloud_cleaned
        self._cloud_token: str = str(cloud_token).strip()
        self.worker_id: str = wid
        self.advertise_url: str = adv_cleaned
        self._node_token: str = str(node_token).strip()
        self.capabilities: tuple[str, ...] = tuple(
            str(cap).strip() for cap in capabilities if str(cap).strip()
        )
        self.heartbeat_sec: float = hb_sec
        self.ttl_sec: float = computed_ttl
        self.role: str = str(role).strip() or "edge_node"
        self.meta: dict[str, Any] = dict(meta) if isinstance(meta, Mapping) else {}
        self.request_timeout_sec: float = req_timeout
        self._on_error = on_error
        self._on_registered = on_registered
        self.presence_enabled: bool = bool(presence_enabled)
        if self.presence_enabled:
            if presence_tracker is not None:
                self.presence_tracker: Any | None = presence_tracker
            else:
                from shinobu_core.cloud.presence import LocalPresenceTracker

                self.presence_tracker = LocalPresenceTracker()
        else:
            self.presence_tracker = None

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered: bool = False
        self._ever_registered: bool = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_registered(self) -> bool:
        with self._lock:
            return self._registered

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.cloud_url}{path}"
        headers = {"Accept": "application/json"}
        if self._cloud_token:
            headers["Authorization"] = f"Bearer {self._cloud_token}"
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = _NO_PROXY_OPENER if _is_loopback_url(url) else None
        resp_ctx = (
            opener.open(req, timeout=self.request_timeout_sec)
            if opener
            else urllib.request.urlopen(req, timeout=self.request_timeout_sec)
        )
        with resp_ctx as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return status, {}
            try:
                parsed = json.loads(raw)
                return status, parsed if isinstance(parsed, dict) else {"data": parsed}
            except json.JSONDecodeError:
                return status, {"raw": raw}

    def register(self) -> dict[str, Any]:
        """Register this edge node with the cloud worker registry."""
        payload = {
            "worker_id": self.worker_id,
            "role": self.role,
            "base_url": self.advertise_url,
            "capabilities": list(self.capabilities),
            "token": self._node_token,
            "ttl_sec": self.ttl_sec,
            "meta": dict(self.meta),
        }
        _, data = self._request_json("POST", "/api/workers/register", payload)
        should_notify = False
        was_ever_registered = False
        with self._lock:
            if not self._registered:
                should_notify = True
                was_ever_registered = self._ever_registered
            self._registered = True
            self._ever_registered = True
        if should_notify:
            self._report_registered(was_ever_registered)
        return data

    def _collect_presence_meta(self) -> dict[str, Any] | None:
        if not self.presence_enabled or self.presence_tracker is None:
            return None
        try:
            sample = self.presence_tracker.sample()
            if isinstance(sample, Mapping):
                return dict(sample)
        except Exception as exc:
            self._report_error("presence", exc)
        return None

    def send_heartbeat(self, meta: Mapping[str, Any] | None = None) -> bool:
        """Send a single heartbeat; auto-re-registers if cloud returns 404."""
        payload: dict[str, Any] = {"worker_id": self.worker_id}
        merged_meta = dict(self.meta)
        presence_sample = self._collect_presence_meta()
        if presence_sample is not None:
            merged_meta["presence"] = presence_sample
        if isinstance(meta, Mapping):
            merged_meta.update(meta)
        if merged_meta:
            payload["meta"] = merged_meta

        try:
            _, data = self._request_json("POST", "/api/workers/heartbeat", payload)
            if data.get("ok") is False and data.get("found") is False:
                with self._lock:
                    self._registered = False
                self.register()
                return True
            with self._lock:
                self._registered = True
                self._ever_registered = True
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                with self._lock:
                    self._registered = False
                self.register()
                return True
            with self._lock:
                self._registered = False
            raise
        except Exception:
            with self._lock:
                self._registered = False
            raise

    def unregister(self) -> bool:
        """Gracefully unregister this edge node from the cloud registry."""
        encoded_id = urllib.parse.quote(self.worker_id, safe="")
        try:
            self._request_json("DELETE", f"/api/workers/{encoded_id}", None)
            with self._lock:
                self._registered = False
            return True
        except Exception as exc:
            with self._lock:
                self._registered = False
            self._report_error("unregister", exc)
            return False

    def _report_error(self, stage: str, exc: Exception) -> None:
        if self._on_error is not None:
            try:
                self._on_error(stage, exc)
            except Exception:
                pass

    def _report_registered(self, is_reregister: bool) -> None:
        if self._on_registered is not None:
            try:
                self._on_registered(is_reregister)
            except Exception:
                pass

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_sec):
            try:
                if not self.is_registered:
                    self.register()
                else:
                    self.send_heartbeat()
            except Exception as exc:
                self._report_error("heartbeat", exc)

    def start(self, *, fail_fast: bool = False) -> bool:
        """Register with cloud and launch the background heartbeat thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._registered
            self._stop_event.clear()

        registered_ok = False
        try:
            self.register()
            registered_ok = True
        except Exception as exc:
            self._report_error("register", exc)
            if fail_fast:
                raise

        with self._lock:
            thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"edge-node-heartbeat-{self.worker_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return registered_ok

    def stop(self, *, unregister: bool = True, join_timeout: float = 5.0) -> bool:
        """Stop heartbeats and gracefully unregister from cloud."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        if unregister:
            return self.unregister()
        return True

    def __enter__(self) -> EdgeNodeClient:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


class EdgeNodeAgent:
    """High-level coordinator uniting EdgeNodeServer and EdgeNodeClient lifecycles."""

    def __init__(
        self,
        *,
        local_engine: EngineAdapter | None,
        cloud_url: str,
        cloud_token: str,
        node_token: str,
        allowed_roots: Sequence[Path | str],
        worker_id: str = "home_pc",
        bind_host: str = "127.0.0.1",
        port: int = 18792,
        advertise_url: str = "",
        airi_bubble_url: str = "",
        airi_bubble_token: str = "",
        bubble_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        capabilities: Iterable[str] | None = None,
        heartbeat_sec: float = 15.0,
        ttl_sec: float | None = None,
        meta: Mapping[str, Any] | None = None,
        presence_enabled: bool = True,
        presence_tracker: Any | None = None,
    ) -> None:
        self.server = EdgeNodeServer(
            local_engine,
            node_token=node_token,
            allowed_roots=allowed_roots,
            bind_host=bind_host,
            port=port,
            airi_bubble_url=airi_bubble_url,
            airi_bubble_token=airi_bubble_token,
            bubble_handler=bubble_handler,
            worker_id=worker_id,
        )
        self._cloud_url = cloud_url
        self._cloud_token = cloud_token
        self._node_token = node_token
        self._worker_id = worker_id
        self._advertise_url = str(advertise_url).strip()
        self._capabilities = tuple(capabilities) if capabilities is not None else None
        self._heartbeat_sec = heartbeat_sec
        self._ttl_sec = ttl_sec
        self._meta = dict(meta) if isinstance(meta, Mapping) else {}
        self._presence_enabled = bool(presence_enabled)
        self._presence_tracker = presence_tracker
        self.client: EdgeNodeClient | None = None
        self._stop_wait = threading.Event()

    def start(self, *, fail_fast: bool = False) -> tuple[str, bool]:
        """Start local HTTP server first, then register & start cloud heartbeat."""
        local_url = self.server.start()
        adv_url = self._advertise_url or local_url
        caps = self._capabilities if self._capabilities is not None else self.server.advertised_capabilities()
        self.client = EdgeNodeClient(
            self._cloud_url,
            self._cloud_token,
            worker_id=self._worker_id,
            advertise_url=adv_url,
            node_token=self._node_token,
            capabilities=caps,
            heartbeat_sec=self._heartbeat_sec,
            ttl_sec=self._ttl_sec,
            meta=self._meta,
            presence_enabled=self._presence_enabled,
            presence_tracker=self._presence_tracker,
        )
        self._stop_wait.clear()
        registered = self.client.start(fail_fast=fail_fast)
        return local_url, registered

    def stop(self) -> None:
        """Unregister from cloud first (instant fallback), then stop local HTTP server."""
        self._stop_wait.set()
        if self.client is not None:
            self.client.stop(unregister=True)
        self.server.stop()

    def run_forever(self) -> None:
        """Run until interrupted by SIGINT/SIGTERM, then gracefully unregister and exit."""
        self.start()

        def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
            self._stop_wait.set()

        prev_handlers: dict[int, Any] = {}
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    prev_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, _handle_signal)
                except (ValueError, OSError):
                    pass
        try:
            while not self._stop_wait.wait(0.5):
                pass
        finally:
            self.stop()
            for sig, prev in prev_handlers.items():
                try:
                    signal.signal(sig, prev)
                except (ValueError, OSError):
                    pass

    def __enter__(self) -> EdgeNodeAgent:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


__all__ = [
    "EdgeNodeAgent",
    "EdgeNodeClient",
    "EdgeNodeServer",
    "validate_bind_host",
]
