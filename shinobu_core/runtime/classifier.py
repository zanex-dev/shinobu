# -*- coding: utf-8 -*-
"""Zero-dependency adaptive low-latency model complexity classifier.

Provides 0ms deterministic short-circuits (ceiling + smalltalk), dual-backend
low-latency classification (`api` direct HTTP + `agy` local SDK/callable) with
EMA latency routing, monotonic effort ratcheting, and fail-open timeout safety.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from shinobu_core.engines.agy_engines import AgySdkEngine
from shinobu_core.engines.openai_compat import (
    GCP_METADATA_API_KEY,
    GCP_METADATA_TOKEN_URL,
    normalize_reasoning_effort,
)
from shinobu_core.protocol.engine import TurnRequest

logger = logging.getLogger(__name__)

EFFORT_RANK: dict[str, int] = {
    "minimal": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "max": 4,
}

_RANK_TO_EFFORT: dict[int, str] = {rank: name for name, rank in EFFORT_RANK.items()}

SMALLTALK_PHRASES: frozenset[str] = frozenset(
    {
        "嗯",
        "嗯嗯",
        "哦",
        "哦哦",
        "啊",
        "好",
        "好的",
        "好呀",
        "好吧",
        "好滴",
        "好的呢",
        "行",
        "行吧",
        "可以",
        "没问题",
        "收到",
        "明白",
        "知道",
        "知道了",
        "谢",
        "谢谢",
        "谢谢你",
        "感谢",
        "辛苦了",
        "哈哈",
        "哈哈哈",
        "呵呵",
        "嘻嘻",
        "晚安",
        "早安",
        "早上好",
        "中午好",
        "晚上好",
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "在吗",
        "在不在",
        "没事",
        "拜拜",
        "再见",
        "hi",
        "hey",
        "hello",
        "ok",
        "okay",
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "lol",
        "haha",
        "thanks",
        "thx",
        "ty",
        "bye",
        "goodnight",
        "morning",
    }
)

_SMALLTALK_STRIP_CHARS = " \t\r\n!！?？.。,，~～…、;；:：\"'“”‘’()（）[]【】"

CLASSIFIER_SYSTEM_PROMPT = (
    "Classify the reasoning effort needed to answer the user message into exactly "
    "one word: low, medium, or high.\n"
    "- low: casual chat, greetings, simple facts, short translation or rewording.\n"
    "- medium: multi-step explanation, standard coding/debugging, structured comparison.\n"
    "- high: complex architecture, deep math/logic proof, tricky concurrency/root-cause analysis.\n"
    "Reply with ONLY one word: low, medium, or high."
)

_WORD_EFFORT_RE = re.compile(r"\b(minimal|min|low|medium|med|high|max)\b", re.IGNORECASE)

_DISABLED_ENV_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off", "disabled"})
_VALID_BACKENDS: frozenset[str] = frozenset({"auto", "api", "agy"})

VERTEX_DEFAULT_CLASSIFIER_MODEL = "google/gemini-2.5-flash-lite"
GEMINI_DEFAULT_CLASSIFIER_MODEL = "gemini-2.5-flash-lite"


@dataclasses.dataclass(frozen=True)
class ClassificationResult:
    """Immutable outcome of a single turn complexity classification."""

    effort: str
    backend: str
    elapsed_ms: int
    upgraded: bool
    reason: str = ""


def _resolve_effort_or_default(value: str | None, default: str) -> str:
    norm = normalize_reasoning_effort(value)
    return norm if norm else default


def ratchet_effort(
    current: str,
    candidate: str,
    *,
    min_effort: str = "low",
    max_effort: str = "high",
) -> str:
    """Clamp ``candidate`` to ``[min_effort, max_effort]`` and take ``max(current, clamped)``."""
    min_norm = normalize_reasoning_effort(min_effort)
    max_norm = normalize_reasoning_effort(max_effort)
    if not min_norm or min_norm not in EFFORT_RANK:
        raise ValueError(f"Invalid min_effort: {min_effort!r}")
    if not max_norm or max_norm not in EFFORT_RANK:
        raise ValueError(f"Invalid max_effort: {max_effort!r}")

    min_rank = EFFORT_RANK[min_norm]
    max_rank = EFFORT_RANK[max_norm]
    if min_rank > max_rank:
        raise ValueError(
            f"min_effort ({min_norm!r}) must not exceed max_effort ({max_norm!r})"
        )

    curr_norm = _resolve_effort_or_default(current, min_norm)
    cand_norm = _resolve_effort_or_default(candidate, min_norm)

    curr_rank = EFFORT_RANK[curr_norm]
    cand_rank = EFFORT_RANK[cand_norm]
    clamped_rank = max(min_rank, min(max_rank, cand_rank))
    final_rank = max(curr_rank, clamped_rank)
    return _RANK_TO_EFFORT[final_rank]


def is_smalltalk_text(user_text: str) -> bool:
    """Return True when ``user_text`` is empty or matches deterministic smalltalk."""
    if not isinstance(user_text, str):
        return True
    cleaned = user_text.strip(_SMALLTALK_STRIP_CHARS).lower()
    if not cleaned:
        return True
    return cleaned in SMALLTALK_PHRASES


def parse_classified_effort(raw_text: str) -> tuple[str, str]:
    """Parse ``low|medium|high`` (or JSON object) from classifier output."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("Empty classifier response")

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            cleaned = "\n".join(lines[1:-1]).strip()
        elif len(lines) >= 2:
            cleaned = "\n".join(lines[1:]).strip()

    # 1. Try JSON object if braces are present.
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        snippet = cleaned[brace_start : brace_end + 1]
        try:
            obj = json.loads(snippet)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for key in ("effort", "reasoning_effort", "level", "complexity", "tier"):
                raw_val = obj.get(key)
                if isinstance(raw_val, str) and raw_val.strip():
                    norm = normalize_reasoning_effort(raw_val)
                    if norm in EFFORT_RANK:
                        reason = str(obj.get("reason") or "").strip()
                        return norm, reason

    # 2. Try direct single-token normalization.
    try:
        direct = normalize_reasoning_effort(cleaned.strip(" \t\r\n.\"'`"))
    except ValueError:
        direct = ""
    if direct in EFFORT_RANK:
        return direct, ""

    # 3. Scan for standalone effort keyword.
    match = _WORD_EFFORT_RE.search(cleaned)
    if match:
        norm = normalize_reasoning_effort(match.group(1))
        if norm in EFFORT_RANK:
            return norm, ""

    raise ValueError(f"Unable to parse reasoning effort from: {raw_text[:120]!r}")


def _extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Chat completion payload missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("Invalid choice item in completion payload")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
    text_fallback = first.get("text")
    if isinstance(text_fallback, str):
        return text_fallback
    raise ValueError("Chat completion choice contains no text content")


class ModelComplexityClassifier:
    """0ms short-circuit + API/AGY dual-backend EMA complexity classifier."""

    DEFAULT_API_TIMEOUT_SEC: float = 0.6
    DEFAULT_AGY_TIMEOUT_SEC: float = 0.9
    INITIAL_API_EMA_MS: float = 180.0
    INITIAL_AGY_EMA_MS: float = 420.0
    EMA_ALPHA: float = 0.3

    def __init__(
        self,
        *,
        backend: str = "auto",
        min_effort: str = "low",
        max_effort: str = "high",
        api_base_url: str = "",
        api_key: str = "",
        api_model: str = "",
        api_timeout_sec: float = DEFAULT_API_TIMEOUT_SEC,
        api_extra_headers: dict[str, str] | None = None,
        api_caller: Callable[[str, str, float], str] | None = None,
        agy_engine: Any = None,
        agy_callable: Callable[..., str] | None = None,
        agy_timeout_sec: float = DEFAULT_AGY_TIMEOUT_SEC,
        ema_alpha: float = EMA_ALPHA,
        initial_api_ema_ms: float = INITIAL_API_EMA_MS,
        initial_agy_ema_ms: float = INITIAL_AGY_EMA_MS,
    ) -> None:
        norm_backend = (backend or "auto").strip().lower()
        if norm_backend not in _VALID_BACKENDS:
            raise ValueError(
                f"Unsupported classifier backend {backend!r}; expected one of {sorted(_VALID_BACKENDS)}"
            )
        min_norm = normalize_reasoning_effort(min_effort)
        max_norm = normalize_reasoning_effort(max_effort)
        if not min_norm or min_norm not in EFFORT_RANK:
            raise ValueError(f"Invalid min_effort: {min_effort!r}")
        if not max_norm or max_norm not in EFFORT_RANK:
            raise ValueError(f"Invalid max_effort: {max_effort!r}")
        if EFFORT_RANK[min_norm] > EFFORT_RANK[max_norm]:
            raise ValueError(
                f"min_effort ({min_norm!r}) must not exceed max_effort ({max_norm!r})"
            )
        if api_timeout_sec <= 0:
            raise ValueError("api_timeout_sec must be > 0")
        if agy_timeout_sec <= 0:
            raise ValueError("agy_timeout_sec must be > 0")
        if not (0.0 < ema_alpha <= 1.0):
            raise ValueError("ema_alpha must be in (0.0, 1.0]")

        self.backend: str = norm_backend
        self.min_effort: str = min_norm
        self.max_effort: str = max_norm

        self.api_base_url: str = (api_base_url or "").strip().rstrip("/")
        self.api_key: str = (api_key or "").strip()
        self.api_model: str = (api_model or "").strip()
        self.api_timeout_sec: float = float(api_timeout_sec)
        self._api_extra_headers: dict[str, str] = dict(api_extra_headers or {})
        self._api_caller: Callable[[str, str, float], str] | None = api_caller

        self._agy_engine: Any = agy_engine
        self._agy_callable: Callable[..., str] | None = agy_callable
        self.agy_timeout_sec: float = float(agy_timeout_sec)

        self.ema_alpha: float = float(ema_alpha)
        self._ema_lock = threading.Lock()
        self._ema_ms: dict[str, float] = {
            "api": float(initial_api_ema_ms),
            "agy": float(initial_agy_ema_ms),
        }

        self._token_lock = threading.Lock()
        self._cached_gcp_token: str = ""
        self._cached_gcp_token_expiry: float = 0.0

    @property
    def ema_ms(self) -> dict[str, float]:
        """Return a thread-safe snapshot of per-backend EMA latency (ms)."""
        with self._ema_lock:
            return dict(self._ema_ms)

    @property
    def has_api_backend(self) -> bool:
        return self._api_caller is not None or bool(self.api_base_url and self.api_model)

    @property
    def has_agy_backend(self) -> bool:
        return self._agy_callable is not None or self._agy_engine is not None

    @property
    def api_endpoint_url(self) -> str:
        if self.api_base_url.endswith("/chat/completions"):
            return self.api_base_url
        return f"{self.api_base_url}/chat/completions"

    def _resolve_bearer_token(self, timeout_sec: float) -> str:
        if self.api_key.lower() != GCP_METADATA_API_KEY:
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
            with urllib.request.urlopen(req, timeout=max(0.1, timeout_sec)) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("Empty access_token from GCP metadata server")
            expires_in = float(payload.get("expires_in") or 3600.0)
            ttl = max(60.0, expires_in - 300.0)
            self._cached_gcp_token = token
            self._cached_gcp_token_expiry = time.monotonic() + ttl
            return token

    @staticmethod
    def _build_lean_prompt(user_text: str, recent_context: str) -> str:
        ctx_slice = (recent_context or "")[:200]
        user_slice = (user_text or "")[:500]
        if ctx_slice:
            return f"Recent context:\n{ctx_slice}\n\nUser message:\n{user_slice}"
        return user_slice

    def _update_ema_success(self, channel: str, elapsed_ms: float) -> None:
        with self._ema_lock:
            prev = self._ema_ms.get(channel, elapsed_ms)
            self._ema_ms[channel] = (1.0 - self.ema_alpha) * prev + self.ema_alpha * max(
                0.0, elapsed_ms
            )

    def _update_ema_failure(self, channel: str, timeout_sec: float) -> None:
        penalty_ms = timeout_sec * 1000.0 * 2.5
        with self._ema_lock:
            prev = self._ema_ms.get(channel, penalty_ms)
            penalized = max(penalty_ms, prev * 1.5)
            self._ema_ms[channel] = (1.0 - self.ema_alpha) * prev + self.ema_alpha * penalized

    def _select_candidate_channels(self) -> list[str]:
        if self.backend == "api":
            return ["api"] if self.has_api_backend else []
        if self.backend == "agy":
            return ["agy"] if self.has_agy_backend else []

        available: list[str] = []
        if self.has_api_backend:
            available.append("api")
        if self.has_agy_backend:
            available.append("agy")
        if len(available) <= 1:
            return available

        with self._ema_lock:
            return sorted(available, key=lambda ch: (self._ema_ms.get(ch, 9999.0), ch != "api"))

    def _call_api_once(self, prompt_text: str) -> str:
        if self._api_caller is not None:
            return self._api_caller(CLASSIFIER_SYSTEM_PROMPT, prompt_text, self.api_timeout_sec)

        deadline = time.monotonic() + self.api_timeout_sec
        token = self._resolve_bearer_token(self.api_timeout_sec)
        remaining = max(0.05, deadline - time.monotonic())

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(self._api_extra_headers)

        body = {
            "model": self.api_model,
            "messages": [
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ],
            "max_tokens": 16,
            "temperature": 0,
            "reasoning_effort": "minimal",
            "stream": False,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.api_endpoint_url,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=remaining) as resp:
            raw_bytes = resp.read()
        parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise ValueError("Non-object JSON returned from classifier API")
        return _extract_chat_completion_text(parsed)

    def _invoke_agy_sync(self, prompt_text: str) -> str:
        if self._agy_callable is not None:
            sig = inspect.signature(self._agy_callable)
            positional = [
                p
                for p in sig.parameters.values()
                if p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
            )
            if has_varargs or len(positional) >= 2:
                return str(self._agy_callable(CLASSIFIER_SYSTEM_PROMPT, prompt_text))
            return str(self._agy_callable(prompt_text))

        if self._agy_engine is None:
            raise RuntimeError("AGY backend is not configured")

        req = TurnRequest(
            episode_id="",
            system_prefix=CLASSIFIER_SYSTEM_PROMPT,
            user_text=prompt_text,
            tools=[],
        )
        stream_no_persist = getattr(self._agy_engine, "_stream_with_config", None)
        if callable(stream_no_persist):
            chunks = stream_no_persist(req, use_persistence=False)
        else:
            chunks = self._agy_engine.stream_turn(req)
        parts = [chunk.delta for chunk in chunks if getattr(chunk, "delta", "")]
        return "".join(parts)

    def _call_agy_once(self, prompt_text: str) -> str:
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def _runner() -> None:
            try:
                outcome["text"] = self._invoke_agy_sync(prompt_text)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=_runner,
            name="shinobu-classifier-agy",
            daemon=True,
        )
        worker.start()
        if not done.wait(timeout=self.agy_timeout_sec):
            raise TimeoutError(
                f"AGY classifier timed out after {self.agy_timeout_sec:.2f}s"
            )
        if "error" in outcome:
            raise outcome["error"]
        return str(outcome.get("text") or "")

    def classify(
        self,
        user_text: str,
        *,
        current_effort: str = "low",
        recent_context: str = "",
    ) -> ClassificationResult:
        """Classify turn complexity with 0ms short-circuits and fail-open safety."""
        curr_norm = _resolve_effort_or_default(current_effort, self.min_effort)
        curr_rank = EFFORT_RANK[curr_norm]
        max_rank = EFFORT_RANK[self.max_effort]

        # 1. 0ms ceiling short-circuit.
        if curr_rank >= max_rank:
            return ClassificationResult(
                effort=curr_norm,
                backend="short_circuit_ceiling",
                elapsed_ms=0,
                upgraded=False,
                reason="already_at_or_above_max_effort",
            )

        # 2. 0ms deterministic smalltalk short-circuit.
        if is_smalltalk_text(user_text):
            ratcheted = ratchet_effort(
                curr_norm,
                self.min_effort,
                min_effort=self.min_effort,
                max_effort=self.max_effort,
            )
            return ClassificationResult(
                effort=ratcheted,
                backend="short_circuit_smalltalk",
                elapsed_ms=0,
                upgraded=EFFORT_RANK[ratcheted] > curr_rank,
                reason="smalltalk",
            )

        # 3. Dual-backend low-latency classification.
        prompt_text = self._build_lean_prompt(user_text, recent_context)
        candidates = self._select_candidate_channels()
        total_start = time.perf_counter()
        errors: list[str] = []

        for channel in candidates:
            ch_start = time.perf_counter()
            timeout_sec = (
                self.api_timeout_sec if channel == "api" else self.agy_timeout_sec
            )
            try:
                raw_output = (
                    self._call_api_once(prompt_text)
                    if channel == "api"
                    else self._call_agy_once(prompt_text)
                )
                classified_effort, parsed_reason = parse_classified_effort(raw_output)
            except Exception as exc:  # noqa: BLE001
                self._update_ema_failure(channel, timeout_sec)
                errors.append(f"{channel}:{type(exc).__name__}")
                logger.debug("Classifier channel %s failed: %s", channel, exc)
                continue

            ch_elapsed_ms = (time.perf_counter() - ch_start) * 1000.0
            self._update_ema_success(channel, ch_elapsed_ms)

            final_effort = ratchet_effort(
                curr_norm,
                classified_effort,
                min_effort=self.min_effort,
                max_effort=self.max_effort,
            )
            total_ms = max(0, int(round((time.perf_counter() - total_start) * 1000.0)))
            return ClassificationResult(
                effort=final_effort,
                backend=channel,
                elapsed_ms=total_ms,
                upgraded=EFFORT_RANK[final_effort] > curr_rank,
                reason=parsed_reason or f"classified_{classified_effort}",
            )

        total_ms = max(0, int(round((time.perf_counter() - total_start) * 1000.0)))
        fallback_reason = ",".join(errors) if errors else "no_available_backend"
        return ClassificationResult(
            effort=curr_norm,
            backend="fallback",
            elapsed_ms=total_ms,
            upgraded=False,
            reason=fallback_reason,
        )


def _is_agy_sdk_available() -> bool:
    """Return True when the optional local Antigravity SDK is importable."""
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except (ImportError, ValueError):
        return False


def _read_dotenv_classifier_vars(home: Path | None) -> dict[str, str]:
    """Read ``SHINOBU_CLASSIFIER_*`` defaults from ``<home>/.env`` if present."""
    if home is None:
        return {}
    try:
        dotenv_path = Path(home) / ".env"
        if not dotenv_path.is_file():
            return {}
        lines = dotenv_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    extracted: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if not key.startswith("SHINOBU_CLASSIFIER_"):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        extracted[key] = val
    return extracted


def _infer_default_api_model(
    base_url: str,
    api_key: str,
    main_engine: Any = None,
) -> str:
    lower_url = base_url.lower()
    main_model = str(getattr(main_engine, "model", "") or "").strip()

    if (
        "aiplatform.googleapis.com" in lower_url
        or "/endpoints/openapi" in lower_url
        or api_key.strip().lower() == GCP_METADATA_API_KEY
        or main_model.lower().startswith("google/")
    ):
        return VERTEX_DEFAULT_CLASSIFIER_MODEL

    if "generativelanguage.googleapis.com" in lower_url or main_model.lower().startswith(
        "gemini-"
    ):
        return GEMINI_DEFAULT_CLASSIFIER_MODEL

    if main_model:
        return main_model
    return GEMINI_DEFAULT_CLASSIFIER_MODEL


def build_classifier_from_env(
    home: Path,
    main_engine: Any = None,
) -> ModelComplexityClassifier | None:
    """Build ``ModelComplexityClassifier`` from environment and ``main_engine``."""
    dotenv_vars = _read_dotenv_classifier_vars(home)

    def _get_cfg(name: str, default: str = "") -> str:
        if name in os.environ:
            return os.environ[name].strip()
        return dotenv_vars.get(name, default).strip()

    enabled_raw = _get_cfg("SHINOBU_CLASSIFIER_ENABLED", "1").lower()
    if enabled_raw in _DISABLED_ENV_VALUES:
        return None

    backend_raw = _get_cfg("SHINOBU_CLASSIFIER_BACKEND", "auto").lower() or "auto"
    if backend_raw in _DISABLED_ENV_VALUES or backend_raw == "none":
        return None
    if backend_raw not in _VALID_BACKENDS:
        logger.warning(
            "Ignoring invalid SHINOBU_CLASSIFIER_BACKEND=%r; falling back to 'auto'",
            backend_raw,
        )
        backend_raw = "auto"

    min_effort = _get_cfg("SHINOBU_CLASSIFIER_MIN_EFFORT", "low") or "low"
    max_effort = _get_cfg("SHINOBU_CLASSIFIER_MAX_EFFORT", "high") or "high"

    # Resolve API channel config (explicit env overrides main_engine inheritance).
    env_base_url = _get_cfg("SHINOBU_CLASSIFIER_BASE_URL")
    env_api_key = _get_cfg("SHINOBU_CLASSIFIER_API_KEY")
    env_model = _get_cfg("SHINOBU_CLASSIFIER_MODEL")
    env_timeout = _get_cfg("SHINOBU_CLASSIFIER_TIMEOUT_SEC")

    inherited_base_url = ""
    inherited_api_key = ""
    inherited_headers: dict[str, str] | None = None
    if main_engine is not None and not isinstance(main_engine, AgySdkEngine):
        raw_main_url = getattr(main_engine, "base_url", None)
        if isinstance(raw_main_url, str) and raw_main_url.strip():
            inherited_base_url = raw_main_url.strip()
        raw_main_key = getattr(main_engine, "api_key", None)
        if isinstance(raw_main_key, str) and raw_main_key.strip():
            inherited_api_key = raw_main_key.strip()
        raw_headers = getattr(main_engine, "_extra_headers", None)
        if isinstance(raw_headers, dict) and raw_headers:
            inherited_headers = dict(raw_headers)

    api_base_url = env_base_url or inherited_base_url
    api_key = env_api_key or inherited_api_key
    api_model = env_model or (
        _infer_default_api_model(api_base_url, api_key, main_engine)
        if api_base_url
        else ""
    )

    api_timeout_sec = ModelComplexityClassifier.DEFAULT_API_TIMEOUT_SEC
    agy_timeout_sec = ModelComplexityClassifier.DEFAULT_AGY_TIMEOUT_SEC
    if env_timeout:
        try:
            parsed_timeout = float(env_timeout)
            if parsed_timeout > 0:
                api_timeout_sec = parsed_timeout
                agy_timeout_sec = parsed_timeout
        except ValueError:
            logger.warning("Ignoring invalid SHINOBU_CLASSIFIER_TIMEOUT_SEC=%r", env_timeout)

    # Resolve AGY channel config (tool-free, persistence-free AgySdkEngine).
    agy_engine: AgySdkEngine | None = None
    if backend_raw in ("auto", "agy"):
        if isinstance(main_engine, AgySdkEngine):
            agy_model = env_model if (env_model and backend_raw == "agy") else None
            agy_engine = AgySdkEngine(
                mode=main_engine.mode,
                model=agy_model,
                base_url=main_engine.base_url,
                model_path=main_engine.model_path,
                save_dir=None,
                api_key=main_engine.api_key,
                disable_builtin_tools=True,
                app_data_dir=main_engine.app_data_dir,
                session_map_path=None,
                name="classifier_agy_sdk",
            )
        elif _is_agy_sdk_available():
            agy_model = env_model if (env_model and backend_raw == "agy") else None
            agy_engine = AgySdkEngine(
                mode="gemini",
                model=agy_model,
                save_dir=None,
                disable_builtin_tools=True,
                app_data_dir=None,
                session_map_path=None,
                name="classifier_agy_sdk",
            )

    has_usable_api = backend_raw in ("auto", "api") and bool(api_base_url and api_model)
    has_usable_agy = backend_raw in ("auto", "agy") and (agy_engine is not None)
    if not has_usable_api and not has_usable_agy:
        return None

    return ModelComplexityClassifier(
        backend=backend_raw,
        min_effort=min_effort,
        max_effort=max_effort,
        api_base_url=api_base_url if has_usable_api else "",
        api_key=api_key if has_usable_api else "",
        api_model=api_model if has_usable_api else "",
        api_timeout_sec=api_timeout_sec,
        api_extra_headers=inherited_headers if (has_usable_api and not env_base_url) else None,
        agy_engine=agy_engine if has_usable_agy else None,
        agy_timeout_sec=agy_timeout_sec,
    )
