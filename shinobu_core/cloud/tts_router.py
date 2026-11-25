# -*- coding: utf-8 -*-
"""Hot-pluggable TTS router with automatic multi-tier fallback.

Routing priority:
1. Online GPU/edge TTS worker registered in ``WorkerRegistry`` (preferring
   ``role="colab_gpu"`` and falling back to any online ``"tts"`` worker) via
   OpenAI-compatible ``POST /v1/audio/speech``.
2. Google Cloud Text-to-Speech v1 ``text:synthesize`` (defaulting to
   ``cmn-CN-Chirp3-HD-Aoede`` with ``OGG_OPUS`` output for direct voice note
   delivery), supporting IAM bearer token provider or API key auth.
3. OpenAI-compatible / Edge-TTS HTTP fallback gateway (``fallback_url``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request

from shinobu_core.cloud.registry import WorkerRecord, WorkerRegistry

HttpPostFn = Callable[
    [str, Mapping[str, str], bytes, float],
    tuple[int, Mapping[str, str], bytes],
]

DEFAULT_GCP_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
DEFAULT_GCP_VOICE = "cmn-CN-Chirp3-HD-Aoede"

_MOOD_SPEED: dict[str, float] = {
    "calm": 1.0,
    "neutral": 1.0,
    "gentle": 0.94,
    "sad": 0.92,
    "sleepy": 0.9,
    "happy": 1.06,
    "excited": 1.08,
    "urgent": 1.12,
}


class TtsUnavailableError(RuntimeError):
    """Raised when all configured TTS backends fail or are unavailable."""

    def __init__(self, message: str, *, errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


@dataclass(frozen=True)
class TtsResult:
    """Synthesized audio payload with content type and winning provider tag."""

    audio: bytes
    content_type: str
    provider: str


def _normalize_format(fmt: str) -> tuple[str, str, str]:
    """Return ``(openai_format, gcp_encoding, default_content_type)``."""
    key = str(fmt or "ogg_opus").strip().lower()
    if key in ("ogg_opus", "opus", "ogg"):
        return ("opus", "OGG_OPUS", "audio/ogg")
    if key in ("mp3", "mpeg"):
        return ("mp3", "MP3", "audio/mpeg")
    if key in ("wav", "wave", "linear16", "pcm"):
        return ("wav", "LINEAR16", "audio/wav")
    return ("opus", "OGG_OPUS", "audio/ogg")


def _extract_language_code(voice_name: str) -> str:
    """Extract BCP-47 language code prefix (e.g. ``cmn-CN``) from a GCP voice name."""
    parts = [part for part in str(voice_name or "").strip().split("-") if part]
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return "cmn-CN"


def _build_speech_endpoint(base_url: str) -> str:
    """Resolve an OpenAI-compatible speech synthesis endpoint from a base URL."""
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.endswith("/v1/audio/speech") or cleaned.endswith("/audio/speech"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/audio/speech"
    return f"{cleaned}/v1/audio/speech"


def _normalize_content_type(raw_header: str | None, default_ct: str) -> str:
    """Strip charset/params from a response Content-Type header if usable."""
    if not raw_header:
        return default_ct
    ct = str(raw_header).split(";", 1)[0].strip().lower()
    if not ct or ct in ("application/octet-stream", "binary/octet-stream"):
        return default_ct
    return ct


def _header_get_ci(headers: Mapping[str, str], key: str) -> str:
    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return ""


def _default_http_post(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_sec: float,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", 200))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read()
            return (status, resp_headers, data)
    except urllib.error.HTTPError as exc:
        err_body = b""
        try:
            err_body = exc.read(512)
        except Exception:
            pass
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return (int(exc.code), resp_headers, err_body)


class TtsRouter:
    """Hot-pluggable TTS router with online GPU worker priority and cloud fallback."""

    def __init__(
        self,
        registry: WorkerRegistry,
        *,
        fallback_url: str = "",
        fallback_api_key: str = "",
        gcp_voice: str = DEFAULT_GCP_VOICE,
        gcp_token_provider: Callable[[], str] | None = None,
        gcp_api_key: str = "",
        gcp_endpoint: str = DEFAULT_GCP_TTS_ENDPOINT,
        fallback_voice: str = "alloy",
        fallback_model: str = "tts-1",
        timeout_sec: float = 30.0,
        http_post: HttpPostFn | None = None,
    ) -> None:
        if not isinstance(registry, WorkerRegistry):
            raise TypeError("registry must be a WorkerRegistry instance")
        timeout = float(timeout_sec)
        if timeout <= 0:
            raise ValueError("timeout_sec must be greater than 0")

        self._registry = registry
        self._fallback_url = str(fallback_url or "").strip()
        self._fallback_api_key = str(fallback_api_key or "").strip()
        self._gcp_voice = str(gcp_voice or "").strip()
        self._gcp_token_provider = gcp_token_provider
        self._gcp_api_key = str(gcp_api_key or "").strip()
        self._gcp_endpoint = str(gcp_endpoint or DEFAULT_GCP_TTS_ENDPOINT).strip()
        self._fallback_voice = str(fallback_voice or "alloy").strip()
        self._fallback_model = str(fallback_model or "tts-1").strip()
        self._timeout_sec = timeout
        self._http_post: HttpPostFn = http_post or _default_http_post

    def _select_worker(self) -> WorkerRecord | None:
        """Prefer an online ``colab_gpu`` TTS worker, else any online ``tts`` worker."""
        preferred = self._registry.get_online("tts", role="colab_gpu")
        if preferred is not None:
            return preferred
        return self._registry.get_online("tts")

    def _try_worker(
        self,
        worker: WorkerRecord,
        text: str,
        *,
        mood: str,
        openai_fmt: str,
        default_ct: str,
    ) -> TtsResult:
        endpoint = _build_speech_endpoint(worker.base_url)
        if not endpoint:
            raise RuntimeError(f"worker {worker.worker_id} has empty base_url")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if worker.token:
            headers["Authorization"] = f"Bearer {worker.token}"

        speed = _MOOD_SPEED.get(mood.lower(), 1.0)
        payload: dict[str, Any] = {
            "model": str(worker.meta.get("model") or "indextts-2.5"),
            "input": text,
            "voice": str(worker.meta.get("voice") or "default"),
            "response_format": openai_fmt,
            "speed": speed,
            "mood": mood,
        }
        raw_req = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, resp_headers, data = self._http_post(
            endpoint,
            headers,
            raw_req,
            self._timeout_sec,
        )
        if not (200 <= status < 300):
            snippet = data[:200].decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"HTTP {status}: {snippet}")
        if not data:
            raise RuntimeError("empty audio response")

        content_type = _normalize_content_type(
            _header_get_ci(resp_headers, "content-type"),
            default_ct,
        )
        return TtsResult(
            audio=data,
            content_type=content_type,
            provider=f"worker:{worker.worker_id}",
        )

    def _has_gcp_backend(self) -> bool:
        return bool(self._gcp_voice) and (
            self._gcp_token_provider is not None or bool(self._gcp_api_key)
        )

    def _try_gcp(
        self,
        text: str,
        *,
        gcp_encoding: str,
        default_ct: str,
    ) -> TtsResult:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = ""
        if self._gcp_token_provider is not None:
            token = str(self._gcp_token_provider() or "").strip()

        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self._gcp_api_key:
            headers["X-Goog-Api-Key"] = self._gcp_api_key
        else:
            raise RuntimeError("no GCP bearer token or API key available")

        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": _extract_language_code(self._gcp_voice),
                "name": self._gcp_voice,
            },
            "audioConfig": {
                "audioEncoding": gcp_encoding,
            },
        }
        raw_req = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, data = self._http_post(
            self._gcp_endpoint,
            headers,
            raw_req,
            self._timeout_sec,
        )
        if not (200 <= status < 300):
            snippet = data[:200].decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"HTTP {status}: {snippet}")

        parsed = json.loads(data.decode("utf-8"))
        audio_b64 = parsed.get("audioContent") if isinstance(parsed, dict) else None
        if not isinstance(audio_b64, str) or not audio_b64.strip():
            raise RuntimeError("missing audioContent in GCP TTS response")

        audio_bytes = base64.b64decode(audio_b64)
        if not audio_bytes:
            raise RuntimeError("decoded GCP audioContent is empty")

        return TtsResult(
            audio=audio_bytes,
            content_type=default_ct,
            provider=f"gcp:{self._gcp_voice}",
        )

    def _try_fallback(
        self,
        text: str,
        *,
        mood: str,
        openai_fmt: str,
        default_ct: str,
    ) -> TtsResult:
        endpoint = _build_speech_endpoint(self._fallback_url)
        if not endpoint:
            raise RuntimeError("fallback_url is empty")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._fallback_api_key:
            headers["Authorization"] = f"Bearer {self._fallback_api_key}"

        speed = _MOOD_SPEED.get(mood.lower(), 1.0)
        payload: dict[str, Any] = {
            "model": self._fallback_model,
            "input": text,
            "voice": self._fallback_voice,
            "response_format": openai_fmt,
            "speed": speed,
            "mood": mood,
        }
        raw_req = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, resp_headers, data = self._http_post(
            endpoint,
            headers,
            raw_req,
            self._timeout_sec,
        )
        if not (200 <= status < 300):
            snippet = data[:200].decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"HTTP {status}: {snippet}")
        if not data:
            raise RuntimeError("empty audio response")

        content_type = _normalize_content_type(
            _header_get_ci(resp_headers, "content-type"),
            default_ct,
        )
        return TtsResult(
            audio=data,
            content_type=content_type,
            provider="fallback",
        )

    def synthesize(
        self,
        text: str,
        *,
        mood: str = "calm",
        fmt: str = "ogg_opus",
    ) -> TtsResult:
        """Synthesize speech using online worker priority and tiered fallback."""
        cleaned = str(text or "").strip()
        if not cleaned:
            raise ValueError("text must be a non-empty string")

        mood_label = str(mood or "calm").strip() or "calm"
        openai_fmt, gcp_encoding, default_ct = _normalize_format(fmt)
        errors: list[str] = []

        # Tier 1: Online hot-pluggable GPU/edge TTS worker.
        worker = self._select_worker()
        if worker is not None:
            try:
                return self._try_worker(
                    worker,
                    cleaned,
                    mood=mood_label,
                    openai_fmt=openai_fmt,
                    default_ct=default_ct,
                )
            except Exception as exc:
                errors.append(f"worker({worker.worker_id}): {exc}")

        # Tier 2: Google Cloud Text-to-Speech v1 (Chirp 3: HD).
        if self._has_gcp_backend():
            try:
                return self._try_gcp(
                    cleaned,
                    gcp_encoding=gcp_encoding,
                    default_ct=default_ct,
                )
            except Exception as exc:
                errors.append(f"gcp({self._gcp_voice}): {exc}")

        # Tier 3: OpenAI-compatible / Edge-TTS fallback HTTP gateway.
        if self._fallback_url:
            try:
                return self._try_fallback(
                    cleaned,
                    mood=mood_label,
                    openai_fmt=openai_fmt,
                    default_ct=default_ct,
                )
            except Exception as exc:
                errors.append(f"fallback({self._fallback_url}): {exc}")

        detail = "; ".join(errors) if errors else "no TTS backend configured or online"
        raise TtsUnavailableError(
            f"All TTS backends failed: {detail}",
            errors=tuple(errors),
        )


__all__ = [
    "TtsResult",
    "TtsRouter",
    "TtsUnavailableError",
]
