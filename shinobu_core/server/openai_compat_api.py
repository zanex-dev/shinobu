# -*- coding: utf-8 -*-
"""
Pure-stdlib OpenAI `/v1` compatibility helpers and local-origin CORS policy.

Designed for loopback desktop shells & community VTuber/pet frontends
(e.g. Project AIRI, Open-LLM-VTuber, VPetLLM) connecting to DaemonServer.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL_ID = "shinobu"
MODEL_OWNER = "shinobu-core"
OPENAI_SSE_DONE: bytes = b"data: [DONE]\n\n"

_ALLOWED_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
_ALLOWED_CORS_METHODS = "GET, POST, OPTIONS"
_ALLOWED_CORS_HEADERS = "Authorization, Content-Type"


def is_allowed_local_origin(origin: str | None) -> bool:
    """Return True only for trusted loopback/desktop shell origins."""
    if not isinstance(origin, str):
        return False
    cleaned = origin.strip()
    if not cleaned or any(ch in cleaned for ch in ("\r", "\n", "\t", " ")):
        return False
    if cleaned == "null":
        return True
    if cleaned.startswith(("app://", "tauri://")):
        return len(cleaned) > len("app://") and "://" in cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme != "http":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_LOCAL_HOSTS:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def build_cors_headers(origin: str | None, *, preflight: bool = False) -> dict[str, str]:
    """Build CORS response headers for an allowed local origin, or {} otherwise."""
    if not is_allowed_local_origin(origin):
        return {}
    assert isinstance(origin, str)
    headers: dict[str, str] = {
        "Access-Control-Allow-Origin": origin.strip(),
        "Vary": "Origin",
    }
    if preflight:
        headers["Access-Control-Allow-Methods"] = _ALLOWED_CORS_METHODS
        headers["Access-Control-Allow-Headers"] = _ALLOWED_CORS_HEADERS
        headers["Access-Control-Max-Age"] = "600"
    return headers


def _extract_content_text(content: Any) -> str:
    """Extract text from OpenAI message content (str or multimodal list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
            elif isinstance(item, dict):
                part_type = item.get("type")
                if part_type in ("text", "input_text"):
                    raw_text = item.get("text")
                    if isinstance(raw_text, str) and raw_text.strip():
                        parts.append(raw_text.strip())
        return "\n".join(parts).strip()
    return ""


def extract_user_text(messages: Any) -> str:
    """
    Extract text from the last `role == "user"` entry in OpenAI `messages`.

    Intentionally ignores `system`/`developer` messages so external shells
    cannot overwrite Shinobu's frozen L0 system prompt prefix.
    """
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if isinstance(item, dict) and item.get("role") == "user":
            return _extract_content_text(item.get("content"))
    return ""


def resolve_model_id(requested_model: Any) -> str:
    """Return a clean model id string, defaulting to `shinobu`."""
    if isinstance(requested_model, str) and requested_model.strip():
        return requested_model.strip()
    return DEFAULT_MODEL_ID


def make_completion_id() -> str:
    """Generate an OpenAI-style chat completion identifier."""
    return f"chatcmpl-{secrets.token_hex(12)}"


def build_models_payload(created: int | None = None) -> dict[str, Any]:
    """Build standard `GET /v1/models` response payload."""
    ts = int(created if created is not None else time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL_ID,
                "object": "model",
                "created": ts,
                "owned_by": MODEL_OWNER,
            }
        ],
    }


def build_chat_completion(
    completion_id: str,
    model: str,
    content: str,
    *,
    created: int | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Build standard non-streaming `chat.completion` JSON payload."""
    ts = int(created if created is not None else time.time())
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": ts,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_chat_chunk(
    completion_id: str,
    model: str,
    *,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """Build standard streaming `chat.completion.chunk` JSON payload."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(created),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def format_openai_sse_chunk(chunk: dict[str, Any]) -> bytes:
    """Serialize a chat.completion.chunk dict into an OpenAI SSE frame."""
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def build_openai_error(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible error envelope."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }
