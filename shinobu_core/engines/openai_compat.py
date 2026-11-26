# -*- coding: utf-8 -*-
"""Zero-dependency OpenAI-compatible SSE engine adapter using stdlib urllib."""

from __future__ import annotations

import copy
import json
import logging
import math
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator, Sequence

from shinobu_core.protocol.engine import ToolSpec, TurnChunk, TurnRequest

logger = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 503})

GCP_METADATA_API_KEY = "gcp_metadata"
GCP_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)

VERTEX_SAFETY_CATEGORIES: tuple[str, ...] = (
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
)

_VERTEX_SAFETY_THRESHOLD_ALIASES: dict[str, str] = {
    "off": "OFF",
    "none": "OFF",
    "block_none": "OFF",
    "lowest": "OFF",
    "high": "BLOCK_ONLY_HIGH",
    "block_only_high": "BLOCK_ONLY_HIGH",
    "medium": "BLOCK_MEDIUM_AND_ABOVE",
    "block_medium_and_above": "BLOCK_MEDIUM_AND_ABOVE",
    "low": "BLOCK_LOW_AND_ABOVE",
    "block_low_and_above": "BLOCK_LOW_AND_ABOVE",
}

_PROTECTED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"model", "messages", "stream", "tools", "response_format"}
)

VERTEX_REASONING_EFFORTS: tuple[str, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "max",
)

_REASONING_EFFORT_CLEAR_ALIASES: frozenset[str] = frozenset(
    {"", "default", "auto", "none", "off"}
)

_REASONING_EFFORT_ALIASES: dict[str, str] = {
    "min": "minimal",
    "minimal": "minimal",
    "low": "low",
    "med": "medium",
    "medium": "medium",
    "high": "high",
    "max": "max",
}


def normalize_reasoning_effort(level: str | None) -> str:
    """Normalize reasoning effort level; empty string clears explicit override."""
    if level is None:
        return ""
    if not isinstance(level, str):
        raise ValueError(
            f"reasoning_effort must be a string or None, got {type(level).__name__}"
        )
    key = level.strip().lower()
    if key in _REASONING_EFFORT_CLEAR_ALIASES:
        return ""
    norm = _REASONING_EFFORT_ALIASES.get(key)
    if norm is None:
        allowed = ", ".join(VERTEX_REASONING_EFFORTS)
        raise ValueError(
            f"Unsupported reasoning_effort: {level!r}. "
            f"Expected one of: {allowed}, default."
        )
    return norm


def build_vertex_safety_extra_body(threshold: str) -> dict[str, Any]:
    """Build Vertex AI OpenAI-compatible ``google.safety_settings`` extra_body."""
    if not isinstance(threshold, str) or not threshold.strip():
        raise ValueError("threshold must be a non-empty string")
    key = threshold.strip().lower().replace("-", "_").replace(" ", "_")
    norm = _VERTEX_SAFETY_THRESHOLD_ALIASES.get(key)
    if norm is None:
        raise ValueError(
            f"Unsupported Vertex safety threshold: {threshold!r}. "
            "Expected one of: off, high, medium, low, "
            "BLOCK_NONE, BLOCK_ONLY_HIGH, BLOCK_MEDIUM_AND_ABOVE, BLOCK_LOW_AND_ABOVE."
        )
    return {
        "google": {
            "safety_settings": [
                {"category": category, "threshold": norm}
                for category in VERTEX_SAFETY_CATEGORIES
            ]
        }
    }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse tool call arguments JSON string into a dictionary."""
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from model output, tolerating code fences."""
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
            raise ValueError(f"Model did not return a JSON object: {cleaned[:200]!r}") from None
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed


def _finalize_tool_buffers(buffers: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Assemble accumulated SSE tool_call deltas ordered by index."""
    result: list[dict[str, Any]] = []
    for idx in sorted(buffers):
        entry = buffers[idx]
        raw_args = "".join(entry["arguments_parts"])
        item: dict[str, Any] = {
            "id": entry["id"] or f"call_{idx}",
            "name": entry["name"],
            "arguments": _parse_arguments(raw_args),
        }
        extra_content = entry.get("extra_content")
        if isinstance(extra_content, dict) and extra_content:
            item["extra_content"] = copy.deepcopy(extra_content)
        result.append(item)
    return result


class OpenAICompatEngine:
    """OpenAI-compatible chat completions adapter (Ollama, Gemini compat, vLLM, etc.)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        *,
        temperature: float | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        name: str = "openai_compat",
        fallback_models: Sequence[str] | None = None,
        retry_max_attempts: int = 3,
        retry_base_delay_sec: float = 1.0,
        retry_max_delay_sec: float = 8.0,
        _sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("extra_body must be a dict or None")
        if (
            isinstance(retry_max_attempts, bool)
            or not isinstance(retry_max_attempts, int)
            or retry_max_attempts < 1
        ):
            raise ValueError("retry_max_attempts must be an integer >= 1")
        if isinstance(retry_base_delay_sec, bool) or not isinstance(retry_base_delay_sec, (int, float)):
            raise ValueError("retry_base_delay_sec must be a non-negative number")
        retry_base = float(retry_base_delay_sec)
        if not math.isfinite(retry_base) or retry_base < 0.0:
            raise ValueError("retry_base_delay_sec must be >= 0.0")
        if isinstance(retry_max_delay_sec, bool) or not isinstance(retry_max_delay_sec, (int, float)):
            raise ValueError("retry_max_delay_sec must be a non-negative number")
        retry_max = float(retry_max_delay_sec)
        if not math.isfinite(retry_max) or retry_max < 0.0:
            raise ValueError("retry_max_delay_sec must be >= 0.0")
        if _sleep is not None and not callable(_sleep):
            raise ValueError("_sleep must be callable or None")

        self.name: str = name
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.api_key: str = api_key
        self.timeout: float = float(timeout)
        self.temperature: float | None = temperature
        self._extra_headers: dict[str, str] = dict(extra_headers or {})
        self._extra_body: dict[str, Any] = copy.deepcopy(extra_body) if extra_body else {}
        self._extra_body_lock = threading.RLock()
        self._token_lock = threading.Lock()
        self._cached_gcp_token: str = ""
        self._cached_gcp_token_expiry: float = 0.0

        normalized_fallbacks: list[str] = []
        seen_models: set[str] = {self.model, self.model.strip()}
        if fallback_models is not None:
            raw_fallbacks: Sequence[str] = (
                (fallback_models,) if isinstance(fallback_models, str) else fallback_models
            )
            for item in raw_fallbacks:
                if not isinstance(item, str):
                    raise ValueError("fallback_models items must be strings")
                candidate = item.strip()
                if candidate and candidate not in seen_models:
                    seen_models.add(candidate)
                    normalized_fallbacks.append(candidate)
        self.fallback_models: tuple[str, ...] = tuple(normalized_fallbacks)
        self.retry_max_attempts: int = retry_max_attempts
        self.retry_base_delay_sec: float = retry_base
        self.retry_max_delay_sec: float = retry_max
        self._sleep: Callable[[float], None] = _sleep if _sleep is not None else time.sleep
        self.last_model_used: str = self.model

    @property
    def reasoning_effort(self) -> str:
        """Return active reasoning_effort level (empty string when unset/default)."""
        with self._extra_body_lock:
            raw = self._extra_body.get("reasoning_effort")
            return str(raw).strip() if raw is not None else ""

    @reasoning_effort.setter
    def reasoning_effort(self, value: str | None) -> None:
        """Thread-safely hot-switch or clear reasoning_effort in extra_body."""
        normalized = normalize_reasoning_effort(value)
        with self._extra_body_lock:
            if normalized:
                self._extra_body["reasoning_effort"] = normalized
            else:
                self._extra_body.pop("reasoning_effort", None)

    @property
    def endpoint_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _uses_gcp_metadata(self) -> bool:
        return self.api_key.strip().lower() == GCP_METADATA_API_KEY

    def _resolve_bearer_token(self) -> str:
        if not self._uses_gcp_metadata():
            return self.api_key
        now = time.monotonic()
        with self._token_lock:
            if self._cached_gcp_token and now < self._cached_gcp_token_expiry:
                return self._cached_gcp_token
            req = urllib.request.Request(
                GCP_METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception as exc:
                raise RuntimeError(
                    f"OpenAICompatEngine failed to fetch GCP metadata OAuth token: {exc}"
                ) from exc
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise RuntimeError(
                    "OpenAICompatEngine received empty access_token from GCP metadata server"
                )
            expires_in = float(payload.get("expires_in") or 3600.0)
            ttl = max(60.0, expires_in - 300.0)
            self._cached_gcp_token = token
            self._cached_gcp_token_expiry = time.monotonic() + ttl
            return token

    def _build_headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        token = self._resolve_bearer_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(self._extra_headers)
        return headers

    @staticmethod
    def _normalize_outgoing_tool_call(tc: dict[str, Any], idx: int) -> dict[str, Any]:
        call_id = str(tc.get("id") or f"call_{idx}").strip() or f"call_{idx}"
        fn = tc.get("function")
        if isinstance(fn, dict):
            fn_name = str(fn.get("name") or tc.get("name") or "")
            raw_args = fn.get("arguments", tc.get("arguments", {}))
        else:
            fn_name = str(tc.get("name") or "")
            raw_args = tc.get("arguments", {})
        if isinstance(raw_args, str):
            args_str = raw_args if raw_args.strip() else "{}"
        else:
            args_str = json.dumps(raw_args if raw_args is not None else {}, ensure_ascii=False)
        norm: dict[str, Any] = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": fn_name,
                "arguments": args_str,
            },
        }
        extra_content = tc.get("extra_content")
        if isinstance(extra_content, dict) and extra_content:
            norm["extra_content"] = copy.deepcopy(extra_content)
        return norm

    @staticmethod
    def _build_messages(req: TurnRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if req.system_prefix:
            messages.append({"role": "system", "content": req.system_prefix})
        pending_tool_ids: set[str] = set()
        for msg in req.history:
            role = msg.get("role")
            if role == "tool":
                tc_id = msg.get("tool_call_id")
                tc_id_str = str(tc_id).strip() if tc_id is not None else ""
                if not tc_id_str or tc_id_str not in pending_tool_ids:
                    continue
                pending_tool_ids.remove(tc_id_str)
                out_tool = dict(msg)
                out_tool["tool_call_id"] = tc_id_str
                raw_content = out_tool.get("content")
                if raw_content is None:
                    out_tool["content"] = ""
                elif not isinstance(raw_content, str):
                    out_tool["content"] = json.dumps(raw_content, ensure_ascii=False)
                messages.append(out_tool)
                continue

            pending_tool_ids.clear()
            out_msg = dict(msg)
            if role == "assistant" and "tool_calls" in out_msg:
                raw_calls = out_msg.get("tool_calls")
                if isinstance(raw_calls, (list, tuple)) and raw_calls:
                    norm_calls = [
                        OpenAICompatEngine._normalize_outgoing_tool_call(tc, idx)
                        for idx, tc in enumerate(raw_calls)
                        if isinstance(tc, dict)
                    ]
                    if norm_calls:
                        out_msg["tool_calls"] = norm_calls
                        pending_tool_ids = {c["id"] for c in norm_calls}
                    else:
                        out_msg.pop("tool_calls", None)
                else:
                    out_msg.pop("tool_calls", None)
            messages.append(out_msg)
        if req.user_text:
            messages.append({"role": "user", "content": req.user_text})
        return messages

    @staticmethod
    def _build_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": tool.to_schema()} for tool in tools]

    def _compute_retry_delay(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        headers = getattr(exc, "headers", None)
        raw_retry_after = headers.get("Retry-After") if headers is not None else None
        if raw_retry_after is not None:
            try:
                parsed = float(str(raw_retry_after).strip())
            except (TypeError, ValueError):
                parsed = -1.0
            if math.isfinite(parsed) and parsed >= 0.0:
                return min(parsed, self.retry_max_delay_sec)

        delay = min(self.retry_base_delay_sec * (2 ** attempt), self.retry_max_delay_sec)
        if delay <= 0.0:
            return 0.0
        return delay + random.uniform(0.0, 0.25 * delay)

    def _open_request(self, payload: dict[str, Any], *, stream: bool) -> Any:
        candidates = (self.model, *self.fallback_models)
        last_error: RuntimeError | None = None
        last_exc: Exception | None = None

        for model_idx, candidate in enumerate(candidates):
            attempt_payload = dict(payload)
            attempt_payload["model"] = candidate
            body = json.dumps(attempt_payload, ensure_ascii=False).encode("utf-8")

            for attempt in range(self.retry_max_attempts):
                request = urllib.request.Request(
                    self.endpoint_url,
                    data=body,
                    headers=self._build_headers(stream=stream),
                    method="POST",
                )
                try:
                    response = urllib.request.urlopen(request, timeout=self.timeout)
                except urllib.error.HTTPError as exc:
                    if exc.code == 401 and self._uses_gcp_metadata():
                        with self._token_lock:
                            self._cached_gcp_token = ""
                            self._cached_gcp_token_expiry = 0.0
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:500]
                    except Exception:
                        detail = str(exc.reason)
                    last_error = RuntimeError(
                        f"OpenAICompatEngine HTTP {exc.code} from {self.endpoint_url}: {detail}"
                    )
                    last_exc = exc

                    if exc.code not in RETRYABLE_HTTP_STATUSES:
                        raise last_error from exc

                    if attempt + 1 < self.retry_max_attempts:
                        delay = self._compute_retry_delay(exc, attempt)
                        logger.warning(
                            "OpenAICompatEngine HTTP %s for model=%s (attempt %d/%d); retrying in %.2fs",
                            exc.code,
                            candidate,
                            attempt + 1,
                            self.retry_max_attempts,
                            delay,
                        )
                        self._sleep(delay)
                        continue

                    if model_idx + 1 < len(candidates):
                        logger.warning(
                            "OpenAICompatEngine HTTP %s exhausted %d attempts for model=%s; falling back to model=%s",
                            exc.code,
                            self.retry_max_attempts,
                            candidate,
                            candidates[model_idx + 1],
                        )
                        break

                    raise last_error from exc
                except urllib.error.URLError as exc:
                    raise RuntimeError(
                        f"OpenAICompatEngine connection failed for {self.endpoint_url}: {exc.reason}"
                    ) from exc

                self.last_model_used = candidate
                return response

        assert last_error is not None  # pragma: no cover
        raise last_error from last_exc

    def _parse_non_stream_payload(self, data: dict[str, Any]) -> Iterator[TurnChunk]:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        choices = data.get("choices") or []
        content = ""
        tool_calls: list[dict[str, Any]] = []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            raw_calls = message.get("tool_calls") or []
            for idx, item in enumerate(raw_calls):
                fn = item.get("function") or {}
                call_dict: dict[str, Any] = {
                    "id": str(item.get("id") or f"call_{idx}"),
                    "name": str(fn.get("name") or item.get("name") or ""),
                    "arguments": _parse_arguments(fn.get("arguments", item.get("arguments"))),
                }
                extra_content = item.get("extra_content")
                if isinstance(extra_content, dict) and extra_content:
                    call_dict["extra_content"] = copy.deepcopy(extra_content)
                tool_calls.append(call_dict)
        if content:
            yield TurnChunk(delta=str(content))
        yield TurnChunk(delta="", tool_calls=tool_calls, usage=usage, done=True)

    def _apply_extra_body(self, payload: dict[str, Any]) -> None:
        with self._extra_body_lock:
            if not self._extra_body:
                return
            for key, value in self._extra_body.items():
                if key in _PROTECTED_PAYLOAD_KEYS or key in payload:
                    continue
                payload[key] = copy.deepcopy(value)

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(req),
            "stream": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if req.tools:
            payload["tools"] = self._build_tools(req.tools)
        clear_turn_effort = False
        if req.reasoning_effort:
            turn_effort = normalize_reasoning_effort(req.reasoning_effort)
            if turn_effort:
                payload["reasoning_effort"] = turn_effort
            else:
                clear_turn_effort = True
        self._apply_extra_body(payload)
        if clear_turn_effort:
            payload.pop("reasoning_effort", None)

        with self._open_request(payload, stream=True) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" in content_type and "text/event-stream" not in content_type:
                raw_body = resp.read().decode("utf-8", errors="replace")
                yield from self._parse_non_stream_payload(json.loads(raw_body))
                return

            tool_buffers: dict[int, dict[str, Any]] = {}
            final_usage: dict[str, Any] | None = None

            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break

                event = json.loads(data_str)
                if isinstance(event.get("usage"), dict):
                    final_usage = event["usage"]

                choices = event.get("choices") or []
                if not choices:
                    continue

                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield TurnChunk(delta=str(content))

                for tc in delta.get("tool_calls") or ():
                    idx = tc.get("index")
                    if not isinstance(idx, int):
                        idx = len(tool_buffers)
                    buf = tool_buffers.setdefault(
                        idx,
                        {"id": "", "name": "", "arguments_parts": [], "extra_content": None},
                    )
                    if tc.get("id"):
                        buf["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        buf["name"] += str(fn["name"])
                    elif tc.get("name") and not buf["name"]:
                        buf["name"] = str(tc["name"])
                    if "arguments" in fn and fn["arguments"] is not None:
                        buf["arguments_parts"].append(str(fn["arguments"]))
                    elif "arguments" in tc and tc["arguments"] is not None:
                        buf["arguments_parts"].append(str(tc["arguments"]))
                    extra_content = tc.get("extra_content")
                    if isinstance(extra_content, dict) and extra_content:
                        existing = buf.get("extra_content")
                        if isinstance(existing, dict):
                            merged = copy.deepcopy(existing)
                            for k, v in extra_content.items():
                                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                                    merged[k].update(copy.deepcopy(v))
                                else:
                                    merged[k] = copy.deepcopy(v)
                            buf["extra_content"] = merged
                        else:
                            buf["extra_content"] = copy.deepcopy(extra_content)

            final_tool_calls = _finalize_tool_buffers(tool_buffers)
            yield TurnChunk(
                delta="",
                tool_calls=final_tool_calls,
                usage=final_usage,
                done=True,
            )

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        self._apply_extra_body(payload)

        try:
            with self._open_request(payload, stream=False) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
        except RuntimeError as exc:
            # Some OpenAI-compatible servers reject response_format with HTTP 400/422.
            if "HTTP 400" in str(exc) or "HTTP 422" in str(exc):
                payload.pop("response_format", None)
                with self._open_request(payload, stream=False) as resp:
                    raw_body = resp.read().decode("utf-8", errors="replace")
            else:
                raise

        data = json.loads(raw_body)
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("OpenAICompatEngine received empty choices in complete_json")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        return _extract_json_object(str(content))


__all__ = [
    "OpenAICompatEngine",
    "RETRYABLE_HTTP_STATUSES",
    "VERTEX_REASONING_EFFORTS",
    "VERTEX_SAFETY_CATEGORIES",
    "build_vertex_safety_extra_body",
    "normalize_reasoning_effort",
]
