# -*- coding: utf-8 -*-
"""
Pure-stdlib inbound MCP (Model Context Protocol) stdio client bridge.

Reads `<home>/mcp_servers.json`, launches configured local MCP servers over
stdio without `shell=True`, performs JSON-RPC 2.0 handshake (`2024-11-05`),
discovers tools, and adapts them into `ToolSpec` instances with per-server
fault isolation and strict subprocess cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from shinobu_core.core.kernel import default_home
from shinobu_core.protocol.engine import ToolSpec

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CONFIG_FILENAME = "mcp_servers.json"
DEFAULT_TIMEOUT_SEC = 15.0
_MAX_STDERR_BYTES = 8192
_MAX_TOOL_LIST_PAGES = 50
_SAFE_IDENT_RE = re.compile(r"[^A-Za-z0-9_-]+")


class McpClientError(RuntimeError):
    """Base error raised by the inbound MCP stdio client."""


class McpTimeoutError(McpClientError):
    """Raised when an MCP JSON-RPC request exceeds its configured timeout."""


class McpProtocolError(McpClientError):
    """Raised when an MCP server returns a JSON-RPC error or malformed response."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(slots=True)
class McpServerConfig:
    """Normalized configuration for a single stdio MCP server entry."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    disabled: bool = False
    timeout_sec: float = DEFAULT_TIMEOUT_SEC


@dataclass(slots=True)
class McpToolInfo:
    """Metadata describing a single tool advertised by an MCP server."""

    server_name: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class McpServerStatus:
    """Connection and discovery status snapshot for one configured MCP server."""

    name: str
    connected: bool
    disabled: bool = False
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    protocol_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "disabled": self.disabled,
            "tools": list(self.tools),
            "tool_count": len(self.tools),
            "error": self.error,
            "server_info": dict(self.server_info),
            "protocol_version": self.protocol_version,
        }


def _sanitize_name_part(raw: str) -> str:
    cleaned = _SAFE_IDENT_RE.sub("_", raw.strip()).strip("_")
    return cleaned or "unnamed"


def _resolve_command(command: str) -> str:
    """Resolve an executable safely via `shutil.which` without ever using a shell."""
    if not isinstance(command, str) or not command.strip():
        raise McpClientError("MCP server command must be a non-empty string")
    raw = command.strip()

    # Check direct path first when explicit path or file exists (e.g. sys.executable).
    candidate = Path(raw)
    if candidate.is_file():
        return str(candidate)

    resolved = shutil.which(raw)
    if resolved:
        return resolved

    raise McpClientError(f"MCP server command not found on PATH: {raw!r}")


def format_mcp_tool_result(result: dict[str, Any]) -> str:
    """Convert an MCP `tools/call` result envelope into a plain string for `ToolSpec`."""
    if not isinstance(result, dict):
        return str(result)

    is_error = bool(result.get("isError", False))
    content = result.get("content")
    parts: list[str] = []

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            elif item is not None:
                parts.append(str(item))
    elif isinstance(content, str):
        parts.append(content)

    rendered = "\n".join(parts).strip()
    if not rendered:
        rendered = json.dumps(result, ensure_ascii=False)

    if is_error and not rendered.lower().startswith("error"):
        return f"MCP tool error: {rendered}"
    return rendered


class _PendingCall:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: Exception | None = None


class McpStdioClient:
    """Low-level JSON-RPC 2.0 over newline-delimited stdio client for one MCP server."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str = ""

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_buf = bytearray()
        self._stderr_lock = threading.Lock()

        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingCall] = {}
        self._next_id = 1
        self._closed = False
        self._eof_error: Exception | None = None

    @property
    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and not self._closed

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    def _recent_stderr(self) -> str:
        with self._stderr_lock:
            if not self._stderr_buf:
                return ""
            return bytes(self._stderr_buf).decode("utf-8", errors="replace").strip()

    def _fail_all_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            self._eof_error = exc
            pending_items = list(self._pending.values())
            self._pending.clear()
        for waiter in pending_items:
            waiter.error = exc
            waiter.event.set()

    def _reader_loop(self) -> None:
        proc = self._proc
        stdout = proc.stdout if proc is not None else None
        if stdout is None:
            self._fail_all_pending(McpClientError("MCP server stdout is unavailable"))
            return

        try:
            while True:
                raw_line = stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "MCP server %r emitted non-JSON stdout line: %s",
                        self.config.name,
                        line[:200],
                    )
                    continue

                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    # Server-to-client notification; ignore cleanly.
                    continue

                with self._pending_lock:
                    waiter = self._pending.pop(msg_id, None)
                if waiter is not None:
                    waiter.response = msg
                    waiter.event.set()
        except Exception as exc:
            self._fail_all_pending(
                McpClientError(f"MCP server {self.config.name!r} stdout read error: {exc}")
            )
            return

        # EOF reached.
        rc = proc.poll() if proc is not None else None
        stderr_hint = self._recent_stderr()
        detail = f"exit_code={rc}" if rc is not None else "stream closed"
        if stderr_hint:
            detail = f"{detail}; stderr={stderr_hint[:300]}"
        self._fail_all_pending(
            McpClientError(f"MCP server {self.config.name!r} disconnected ({detail})")
        )

    def _stderr_loop(self) -> None:
        proc = self._proc
        stderr = proc.stderr if proc is not None else None
        if stderr is None:
            return
        try:
            while True:
                chunk = stderr.read(1024)
                if not chunk:
                    break
                with self._stderr_lock:
                    self._stderr_buf.extend(chunk)
                    if len(self._stderr_buf) > _MAX_STDERR_BYTES:
                        del self._stderr_buf[: len(self._stderr_buf) - _MAX_STDERR_BYTES]
        except OSError:
            pass

    def _send_frame(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed:
            raise McpClientError(f"MCP server {self.config.name!r} is not connected")
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with self._write_lock:
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except OSError as exc:
                raise McpClientError(
                    f"Failed writing to MCP server {self.config.name!r}: {exc}"
                ) from exc

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and wait for its matching response."""
        wait_timeout = (
            max(0.1, float(timeout))
            if timeout is not None
            else max(0.1, float(self.config.timeout_sec))
        )

        waiter = _PendingCall()
        with self._pending_lock:
            if self._closed:
                raise McpClientError(f"MCP server {self.config.name!r} is closed")
            if self._eof_error is not None:
                raise self._eof_error
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = waiter

        frame: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params

        try:
            self._send_frame(frame)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        if not waiter.event.wait(timeout=wait_timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise McpTimeoutError(
                f"MCP server {self.config.name!r} timed out after {wait_timeout:.2f}s on {method!r}"
            )

        if waiter.error is not None:
            raise waiter.error

        response = waiter.response
        if not isinstance(response, dict):
            raise McpProtocolError(
                f"MCP server {self.config.name!r} returned non-object response for {method!r}"
            )

        if "error" in response and response["error"] is not None:
            err = response["error"]
            if isinstance(err, dict):
                raise McpProtocolError(
                    str(err.get("message") or f"MCP error on {method!r}"),
                    code=err.get("code") if isinstance(err.get("code"), int) else None,
                    data=err.get("data"),
                )
            raise McpProtocolError(f"MCP error on {method!r}: {err}")

        result = response.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            return {"value": result}
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no response expected)."""
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._send_frame(frame)

    def connect(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Spawn the configured stdio subprocess and perform MCP `initialize` handshake."""
        if self._proc is not None and not self._closed:
            return {
                "protocolVersion": self.protocol_version,
                "serverInfo": dict(self.server_info),
            }

        resolved = _resolve_command(self.config.command)
        cmd = [resolved, *self.config.args]

        env = os.environ.copy()
        for key, value in self.config.env.items():
            env[str(key)] = str(value)

        cwd = self.config.cwd
        if cwd is not None and not Path(cwd).is_dir():
            raise McpClientError(f"Configured cwd does not exist: {cwd!r}")

        self._closed = False
        self._eof_error = None
        with self._stderr_lock:
            self._stderr_buf.clear()

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                shell=False,
                bufsize=0,
            )
        except OSError as exc:
            raise McpClientError(
                f"Failed to spawn MCP server {self.config.name!r} ({resolved!r}): {exc}"
            ) from exc

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"mcp-stdout-{self.config.name}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"mcp-stderr-{self.config.name}",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

        try:
            init_result = self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "shinobu-core", "version": "0.1.0"},
                },
                timeout=timeout,
            )
            self.protocol_version = str(
                init_result.get("protocolVersion") or MCP_PROTOCOL_VERSION
            )
            raw_info = init_result.get("serverInfo")
            self.server_info = dict(raw_info) if isinstance(raw_info, dict) else {}
            self.notify("notifications/initialized")
            return init_result
        except Exception:
            self.close()
            raise

    def ping(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self.request("ping", {}, timeout=timeout)

    def list_tools(self, *, timeout: float | None = None) -> list[McpToolInfo]:
        """Fetch all tools exposed by this server (following `nextCursor` if present)."""
        tools: list[McpToolInfo] = []
        cursor: str | None = None

        for _ in range(_MAX_TOOL_LIST_PAGES):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = self.request("tools/list", params, timeout=timeout)
            raw_tools = result.get("tools")
            if isinstance(raw_tools, list):
                for entry in raw_tools:
                    if not isinstance(entry, dict):
                        continue
                    tool_name = str(entry.get("name") or "").strip()
                    if not tool_name:
                        continue
                    desc = str(entry.get("description") or "").strip()
                    schema = entry.get("inputSchema")
                    if not isinstance(schema, dict):
                        schema = {"type": "object", "properties": {}}
                    tools.append(
                        McpToolInfo(
                            server_name=self.config.name,
                            name=tool_name,
                            description=desc,
                            input_schema=dict(schema),
                        )
                    )
            next_cursor = result.get("nextCursor")
            if isinstance(next_cursor, str) and next_cursor:
                cursor = next_cursor
            else:
                break

        return tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke a remote MCP tool via `tools/call`."""
        return self.request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": dict(arguments) if isinstance(arguments, dict) else {},
            },
            timeout=timeout,
        )

    def close(self) -> None:
        """Terminate and reap the subprocess and close all pipe handles cleanly."""
        self._closed = True
        self._fail_all_pending(
            McpClientError(f"MCP server {self.config.name!r} closed")
        )

        proc = self._proc
        self._proc = None
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

            if proc.poll() is None:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        try:
                            proc.wait(timeout=1.5)
                        except subprocess.TimeoutExpired:
                            pass

            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._reader_thread = None
        self._stderr_thread = None

    def __enter__(self) -> McpStdioClient:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class McpClientManager:
    """Manages multiple inbound stdio MCP servers configured under `<home>/mcp_servers.json`."""

    def __init__(
        self,
        home: Path | str | None = None,
        *,
        config_path: Path | str | None = None,
        default_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.home: Path = Path(home) if home is not None else default_home()
        self.config_path: Path = (
            Path(config_path)
            if config_path is not None
            else (self.home / MCP_CONFIG_FILENAME)
        )
        self.default_timeout_sec: float = max(0.1, float(default_timeout_sec))

        self._lock = threading.RLock()
        self._clients: dict[str, McpStdioClient] = {}
        self._tools: dict[str, list[McpToolInfo]] = {}
        self._statuses: dict[str, McpServerStatus] = {}
        self.last_config_error: str | None = None

    def load_config(self) -> list[McpServerConfig]:
        """Parse `<home>/mcp_servers.json` into normalized `McpServerConfig` objects."""
        self.last_config_error = None
        if not self.config_path.is_file():
            return []

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_config_error = f"Failed to parse {self.config_path}: {exc}"
            logger.warning("%s", self.last_config_error)
            return []

        if not isinstance(raw, dict):
            self.last_config_error = f"Expected JSON object in {self.config_path}"
            logger.warning("%s", self.last_config_error)
            return []

        servers_map = raw.get("mcpServers")
        if servers_map is None:
            return []
        if not isinstance(servers_map, dict):
            self.last_config_error = "'mcpServers' must be a JSON object"
            logger.warning("%s", self.last_config_error)
            return []

        configs: list[McpServerConfig] = []
        for raw_name, entry in servers_map.items():
            name = str(raw_name).strip()
            if not name:
                continue
            if not isinstance(entry, dict):
                configs.append(
                    McpServerConfig(
                        name=name,
                        command="",
                        disabled=False,
                        timeout_sec=self.default_timeout_sec,
                    )
                )
                continue

            command = entry.get("command")
            command_str = command.strip() if isinstance(command, str) else ""

            raw_args = entry.get("args")
            args: list[str] = (
                [str(item) for item in raw_args] if isinstance(raw_args, list) else []
            )

            raw_env = entry.get("env")
            env: dict[str, str] = (
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            )

            raw_cwd = entry.get("cwd")
            cwd = str(raw_cwd).strip() if isinstance(raw_cwd, str) and raw_cwd.strip() else None

            disabled = bool(entry.get("disabled", False))

            raw_timeout = entry.get("timeout_sec", self.default_timeout_sec)
            try:
                timeout_sec = max(0.1, float(raw_timeout))
            except (TypeError, ValueError):
                timeout_sec = self.default_timeout_sec

            configs.append(
                McpServerConfig(
                    name=name,
                    command=command_str,
                    args=args,
                    env=env,
                    cwd=cwd,
                    disabled=disabled,
                    timeout_sec=timeout_sec,
                )
            )
        return configs

    def connect_all(self) -> list[McpServerStatus]:
        """Connect to all enabled MCP servers with per-server fault isolation."""
        self.close()
        configs = self.load_config()
        results: list[McpServerStatus] = []

        for cfg in configs:
            if cfg.disabled:
                status = McpServerStatus(
                    name=cfg.name,
                    connected=False,
                    disabled=True,
                )
                with self._lock:
                    self._statuses[cfg.name] = status
                results.append(status)
                continue

            client = McpStdioClient(cfg)
            try:
                client.connect()
                tools = client.list_tools()
                status = McpServerStatus(
                    name=cfg.name,
                    connected=True,
                    disabled=False,
                    tools=[t.name for t in tools],
                    error=None,
                    server_info=dict(client.server_info),
                    protocol_version=client.protocol_version,
                )
                with self._lock:
                    self._clients[cfg.name] = client
                    self._tools[cfg.name] = tools
                    self._statuses[cfg.name] = status
                results.append(status)
            except Exception as exc:
                logger.warning("Failed to connect MCP server %r: %s", cfg.name, exc)
                client.close()
                status = McpServerStatus(
                    name=cfg.name,
                    connected=False,
                    disabled=False,
                    tools=[],
                    error=str(exc),
                )
                with self._lock:
                    self._statuses[cfg.name] = status
                results.append(status)

        return results

    @property
    def statuses(self) -> list[McpServerStatus]:
        with self._lock:
            return list(self._statuses.values())

    def get_client(self, server_name: str) -> McpStdioClient | None:
        with self._lock:
            return self._clients.get(server_name)

    def build_tool_specs(self) -> list[ToolSpec]:
        """Convert all discovered remote MCP tools into fault-isolated `ToolSpec` objects."""
        specs: list[ToolSpec] = []
        seen_names: set[str] = set()

        with self._lock:
            snapshot = [
                (server_name, self._clients.get(server_name), list(tool_list))
                for server_name, tool_list in self._tools.items()
            ]

        for server_name, client, tool_list in snapshot:
            if client is None:
                continue
            safe_server = _sanitize_name_part(server_name)
            for tool in tool_list:
                safe_tool = _sanitize_name_part(tool.name)
                full_name = f"mcp_{safe_server}_{safe_tool}"
                if full_name in seen_names:
                    logger.warning(
                        "Duplicate MCP tool name %r skipped (server=%r, tool=%r)",
                        full_name,
                        server_name,
                        tool.name,
                    )
                    continue
                seen_names.add(full_name)

                def _make_handler(
                    _client: McpStdioClient,
                    _remote_tool: str,
                    _full_name: str,
                ):
                    def _handler(*args: Any, **kwargs: Any) -> str:
                        if args and not kwargs and len(args) == 1 and isinstance(args[0], dict):
                            call_args = dict(args[0])
                        else:
                            call_args = dict(kwargs)
                        try:
                            res = _client.call_tool(_remote_tool, call_args)
                            return format_mcp_tool_result(res)
                        except Exception as exc:
                            logger.warning("MCP tool %r failed: %s", _full_name, exc)
                            return f"MCP tool '{_full_name}' error: {exc}"

                    return _handler

                description = (
                    tool.description
                    or f"MCP tool '{tool.name}' provided by server '{server_name}'."
                )
                parameters = (
                    dict(tool.input_schema)
                    if isinstance(tool.input_schema, dict) and tool.input_schema
                    else {"type": "object", "properties": {}}
                )
                specs.append(
                    ToolSpec(
                        name=full_name,
                        description=description,
                        parameters=parameters,
                        handler=_make_handler(client, tool.name, full_name),
                    )
                )

        return specs

    def register_onto(self, kernel: Any, *, replace: bool = True) -> list[ToolSpec]:
        """Build `ToolSpec` wrappers and register them onto a `ShinobuKernel`."""
        specs = self.build_tool_specs()
        if specs:
            kernel.register_tools(specs, replace=replace)
        return specs

    def close(self) -> None:
        """Close and reap every managed MCP server subprocess."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._tools.clear()
            self._statuses.clear()

        for client in clients:
            try:
                client.close()
            except Exception:
                logger.exception("Error while closing MCP server %r", client.config.name)

    def __enter__(self) -> McpClientManager:
        self.connect_all()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "MCP_CONFIG_FILENAME",
    "MCP_PROTOCOL_VERSION",
    "McpClientError",
    "McpClientManager",
    "McpProtocolError",
    "McpServerConfig",
    "McpServerStatus",
    "McpStdioClient",
    "McpTimeoutError",
    "McpToolInfo",
    "format_mcp_tool_result",
]
