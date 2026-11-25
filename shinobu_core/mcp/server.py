# -*- coding: utf-8 -*-
"""
Pure-stdlib outbound MCP (Model Context Protocol) stdio server.

Exposes a locally running Shinobu loopback daemon as 4 MCP tools over
newline-delimited JSON-RPC 2.0 (`2024-11-05`):
  - `shinobu_bubble`
  - `shinobu_state`
  - `shinobu_recall`
  - `shinobu_chat`
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Protocol, runtime_checkable
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote

from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.ui.client import discover_endpoint, validate_loopback_url

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "shinobu"
SERVER_VERSION = "0.1.0"

# Standard JSON-RPC 2.0 error codes
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class DaemonBackendError(RuntimeError):
    """Raised when the local Shinobu loopback daemon cannot be reached or returns an error."""

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


@runtime_checkable
class ShinobuBackendProtocol(Protocol):
    """Contract shared by `LoopbackDaemonBackend` and unit-test mock clients."""

    def bubble(
        self,
        text: str,
        *,
        mood: str | None = None,
        duration_sec: float | None = None,
    ) -> dict[str, Any]: ...

    def get_state(self) -> dict[str, Any]: ...

    def set_state(
        self,
        state: str | None = None,
        mood: str | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]: ...

    def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]: ...

    def remember(
        self,
        key: str,
        value: str,
        *,
        confidence: float = 0.8,
    ) -> dict[str, Any]: ...

    def chat(self, text: str) -> dict[str, Any]: ...


class LoopbackDaemonBackend:
    """Lightweight loopback-only HTTP backend talking to the local Shinobu daemon."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.url: str = validate_loopback_url(url)
        cleaned_token = str(token or "").strip()
        if not cleaned_token:
            raise ValueError("Bearer token must be a non-empty string")
        self.token: str = cleaned_token
        self.timeout: float = max(0.5, float(timeout))
        # Explicitly disable proxies so local Bearer tokens never leave loopback.
        self._opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))

    @classmethod
    def from_home(
        cls,
        home: Path | str | None = None,
        *,
        timeout: float = 15.0,
    ) -> LoopbackDaemonBackend:
        endpoint = discover_endpoint(home, timeout=min(2.0, max(0.5, float(timeout))))
        if endpoint is None:
            raise DaemonBackendError(
                "Shinobu daemon is not running or runtime/endpoint.json is unavailable"
            )
        return cls(
            url=str(endpoint["url"]),
            token=str(endpoint["token"]),
            timeout=timeout,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        norm_path = path if path.startswith("/") else f"/{path}"
        full_url = f"{self.url}{norm_path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
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
            raise DaemonBackendError(
                f"Daemon returned HTTP {exc.code} on {method} {norm_path}: {detail}",
                status=exc.code,
                detail=detail,
            ) from exc
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise DaemonBackendError(
                f"Failed to reach Shinobu daemon at {full_url}: {exc}"
            ) from exc

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DaemonBackendError(
                f"Invalid JSON returned from {method} {norm_path}: {raw[:200]}"
            ) from exc
        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed

    def bubble(
        self,
        text: str,
        *,
        mood: str | None = None,
        duration_sec: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("bubble text must be a non-empty string")
        payload: dict[str, Any] = {"text": text}
        if mood is not None:
            payload["mood"] = mood
        if duration_sec is not None:
            payload["duration_sec"] = float(duration_sec)
        return self._request_json("POST", "/api/bubble", body=payload)

    def get_state(self) -> dict[str, Any]:
        return self._request_json("GET", "/state")

    def set_state(
        self,
        state: str | None = None,
        mood: str | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if state is None and mood is None:
            raise ValueError("set_state requires at least one of 'state' or 'mood'")
        payload: dict[str, Any] = {}
        if state is not None:
            payload["state"] = state
        if mood is not None:
            payload["mood"] = mood
        if reason is not None:
            payload["reason"] = reason
        return self._request_json("POST", "/api/state", body=payload)

    def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("recall query must be a non-empty string")
        safe_limit = max(1, int(limit))
        encoded_q = quote(query, safe="")
        return self._request_json(
            "GET", f"/api/memory/search?q={encoded_q}&limit={safe_limit}"
        )

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
        return self._request_json(
            "POST",
            "/api/memory/facts",
            body={
                "key": key.strip(),
                "value": value.strip(),
                "confidence": safe_conf,
            },
        )

    def chat(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("chat text must be a non-empty string")
        return self._request_json(
            "POST", "/chat", body={"text": text, "stream": False}
        )


OUTBOUND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "shinobu_bubble",
        "description": "Display a speech bubble on the local Shinobu desktop companion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Bubble text to display.",
                },
                "mood": {
                    "type": "string",
                    "description": "Optional mood hint for the bubble.",
                },
                "duration_sec": {
                    "type": "number",
                    "description": "Optional display duration in seconds.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "shinobu_state",
        "description": (
            "Query or update the local Shinobu companion's state and mood. "
            "Omit both 'state' and 'mood' to read the current snapshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Optional target pet state (e.g. idle, thinking, speaking).",
                },
                "mood": {
                    "type": "string",
                    "description": "Optional target mood (e.g. neutral, happy, sleepy).",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for the state/mood transition.",
                },
            },
        },
    },
    {
        "name": "shinobu_recall",
        "description": "Search Shinobu's local memory store for past episodes matching a query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to search in local memory.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 10).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "shinobu_chat",
        "description": "Send a chat message to the local Shinobu companion and return its response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "User message text to send to Shinobu.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "shinobu_remember",
        "description": "Store or update a persistent fact in Shinobu's memory store (upserted by key).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Unique fact key (e.g. 'user.favorite_snack').",
                },
                "value": {
                    "type": "string",
                    "description": "Fact value to remember.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Optional confidence score between 0 and 1 (default 0.8).",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.8,
                },
            },
            "required": ["key", "value"],
        },
    },
]

_TOOL_NAMES: frozenset[str] = frozenset(tool["name"] for tool in OUTBOUND_TOOLS)


def _extract_chat_text(raw: Any) -> dict[str, Any]:
    """Normalize chat output from either JSON dict or Event list into a consistent dict."""
    if isinstance(raw, list):
        events = raw
        result: dict[str, Any] = {"episode_id": "", "text": "", "events": []}
        deltas: list[str] = []
        done_text = ""
        for ev in events:
            if isinstance(ev, Event):
                ev_dict = ev.to_dict()
                kind = ev.kind
                payload = ev.payload
                if ev.episode_id and not result["episode_id"]:
                    result["episode_id"] = ev.episode_id
            elif isinstance(ev, dict):
                ev_dict = ev
                kind = str(ev.get("kind") or "")
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                if ev.get("episode_id") and not result["episode_id"]:
                    result["episode_id"] = str(ev["episode_id"])
            else:
                continue
            result["events"].append(ev_dict)
            if kind == EventKind.TOKEN_DELTA and isinstance(payload.get("delta"), str):
                deltas.append(payload["delta"])
            elif kind == EventKind.DONE and isinstance(payload.get("text"), str):
                done_text = payload["text"]
        result["text"] = done_text or "".join(deltas)
        return result

    if isinstance(raw, dict):
        normalized = dict(raw)
        if not isinstance(normalized.get("text"), str) or not normalized["text"]:
            events = normalized.get("events")
            if isinstance(events, list):
                extracted = _extract_chat_text(events)
                if extracted["text"]:
                    normalized["text"] = extracted["text"]
                if not normalized.get("episode_id") and extracted["episode_id"]:
                    normalized["episode_id"] = extracted["episode_id"]
        return normalized

    return {"text": str(raw)}


class McpStdioServer:
    """Line-delimited JSON-RPC 2.0 MCP server exposing Shinobu tools over stdio."""

    def __init__(
        self,
        home: Path | str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.home = Path(home) if home is not None else None
        self._injected_client = client
        self._resolved_client: Any | None = None

    def _get_client(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        if self._resolved_client is None:
            self._resolved_client = LoopbackDaemonBackend.from_home(self.home)
        return self._resolved_client

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._get_client()

        if name == "shinobu_bubble":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("shinobu_bubble requires a non-empty string 'text'")
            mood = arguments.get("mood")
            if mood is not None and not isinstance(mood, str):
                raise ValueError("shinobu_bubble 'mood' must be a string")
            duration_raw = arguments.get("duration_sec")
            duration_sec = float(duration_raw) if duration_raw is not None else None
            return client.bubble(text, mood=mood, duration_sec=duration_sec)

        if name == "shinobu_state":
            state = arguments.get("state")
            mood = arguments.get("mood")
            reason = arguments.get("reason")
            if state is not None and not isinstance(state, str):
                raise ValueError("shinobu_state 'state' must be a string")
            if mood is not None and not isinstance(mood, str):
                raise ValueError("shinobu_state 'mood' must be a string")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("shinobu_state 'reason' must be a string")
            if state is None and mood is None:
                return client.get_state()
            return client.set_state(state=state, mood=mood, reason=reason)

        if name == "shinobu_recall":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("shinobu_recall requires a non-empty string 'query'")
            limit_raw = arguments.get("limit", 10)
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("shinobu_recall 'limit' must be an integer") from exc
            if limit < 1:
                raise ValueError("shinobu_recall 'limit' must be >= 1")
            return client.recall(query, limit=limit)

        if name == "shinobu_chat":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("shinobu_chat requires a non-empty string 'text'")
            return _extract_chat_text(client.chat(text))

        if name == "shinobu_remember":
            key = arguments.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError("shinobu_remember requires a non-empty string 'key'")
            value = arguments.get("value")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("shinobu_remember requires a non-empty string 'value'")
            conf_raw = arguments.get("confidence", 0.8)
            if conf_raw is None:
                confidence = 0.8
            else:
                if isinstance(conf_raw, bool):
                    raise ValueError("shinobu_remember 'confidence' must be a number between 0 and 1")
                try:
                    confidence = float(conf_raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError("shinobu_remember 'confidence' must be a number between 0 and 1") from exc
                if not (0.0 <= confidence <= 1.0):
                    raise ValueError("shinobu_remember 'confidence' must be between 0 and 1")
            return client.remember(key.strip(), value.strip(), confidence=confidence)

        raise KeyError(f"Unknown tool: {name}")

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Process one decoded JSON-RPC 2.0 message and return a response dict (or None for notifications)."""
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": JSONRPC_INVALID_REQUEST,
                    "message": "Invalid Request: expected JSON object",
                },
            }

        has_id = "id" in message
        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if not has_id:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": JSONRPC_INVALID_REQUEST,
                    "message": "Invalid Request: missing string 'method'",
                },
            }

        params = message.get("params")
        if params is None:
            params = {}

        # Notifications do not receive responses per JSON-RPC 2.0.
        if method == "notifications/initialized" or (not has_id and method.startswith("notifications/")):
            return None
        if not has_id:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": list(OUTBOUND_TOOLS)},
            }

        if method == "tools/call":
            if not isinstance(params, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": JSONRPC_INVALID_PARAMS,
                        "message": "Invalid params: expected object",
                    },
                }
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or tool_name not in _TOOL_NAMES:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": JSONRPC_INVALID_PARAMS,
                        "message": f"Unknown tool: {tool_name!r}",
                    },
                }
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": JSONRPC_INVALID_PARAMS,
                        "message": "Invalid params: 'arguments' must be an object",
                    },
                }

            try:
                tool_result = self._dispatch_tool(tool_name, arguments)
            except ValueError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": JSONRPC_INVALID_PARAMS,
                        "message": str(exc),
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }

            text_payload = (
                json.dumps(tool_result, ensure_ascii=False)
                if not isinstance(tool_result, str)
                else tool_result
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text_payload}],
                    "isError": False,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": JSONRPC_METHOD_NOT_FOUND,
                "message": f"Method not found: {method}",
            },
        }


def _write_json_line(stream: Any, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        stream.write(line)
    except TypeError:
        stream.write(line.encode("utf-8"))
    stream.flush()


def run_mcp_stdio_server(
    home: Path | str | None = None,
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    client: Any | None = None,
) -> int:
    """Run the outbound Shinobu MCP server loop over stdio until EOF."""
    in_stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    out_stream = stdout if stdout is not None else getattr(sys.stdout, "buffer", sys.stdout)

    server = McpStdioServer(home=home, client=client)

    while True:
        raw_line = in_stream.readline()
        if not raw_line:
            break
        if isinstance(raw_line, (bytes, bytearray)):
            line = raw_line.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw_line).strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write_json_line(
                out_stream,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": JSONRPC_PARSE_ERROR,
                        "message": f"Parse error: {exc}",
                    },
                },
            )
            continue

        response = server.handle_message(message)
        if response is not None:
            _write_json_line(out_stream, response)

    return 0


__all__ = [
    "DaemonBackendError",
    "LoopbackDaemonBackend",
    "MCP_PROTOCOL_VERSION",
    "McpStdioServer",
    "OUTBOUND_TOOLS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "ShinobuBackendProtocol",
    "run_mcp_stdio_server",
]
