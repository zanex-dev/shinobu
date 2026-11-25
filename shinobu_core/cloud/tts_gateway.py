# -*- coding: utf-8 -*-
"""Dual-mode local OpenAI-compatible TTS gateway (default http://127.0.0.1:18791/v1).

Routing strategy:
  1. If `upstream.json` is enabled and has a valid `base_url` (e.g. a remote GPU
     IndexTTS-2.5 tunnel), forward `POST /v1/audio/speech` to the GPU upstream.
  2. If the GPU upstream is disabled, offline, or fails/times out, seamlessly
     fall back to optional local `edge-tts` (`pip install -e '.[tts-edge]'`).

Hot-switch without restarting voice clients or this server via:
  - Editing `~/.shinobu/tts/upstream.json`
  - Running `shinobu tts-gateway --set-upstream <URL> [--upstream-key <KEY>]`
  - Or calling `POST http://127.0.0.1:18791/api/upstream`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from shinobu_core.cloud.edge_node import validate_bind_host
from shinobu_core.core.kernel import default_home

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18791
DEFAULT_VOICE = "zh-CN-XiaoyiNeural"

EDGE_TTS_MISSING_MSG = "edge-tts is not installed; install with: pip install -e '.[tts-edge]'"

VOICE_ALIASES: dict[str, str] = {
    "shinobu": "zh-CN-XiaoyiNeural",
    "default": "zh-CN-XiaoyiNeural",
    "alloy": "zh-CN-XiaoyiNeural",
    "nova": "zh-CN-XiaoyiNeural",
    "shimmer": "zh-CN-XiaoxiaoNeural",
    "fable": "ja-JP-NanamiNeural",
    "echo": "zh-CN-XiaoxiaoNeural",
    "onyx": "zh-CN-YunxiNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "nanami": "ja-JP-NanamiNeural",
}

PRESET_VOICES: list[dict[str, str]] = [
    {"id": "shinobu", "name": "Shinobu (GPU clone if online, else Xiaoyi)", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoyiNeural", "name": "Xiaoyi (ZH lively)", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoxiaoNeural", "name": "Xiaoxiao (ZH warm)", "lang": "zh-CN"},
    {"id": "ja-JP-NanamiNeural", "name": "Nanami (JA female)", "lang": "ja-JP"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "Xiaobei (ZH playful)", "lang": "zh-CN"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "HsiaoChen (ZH-TW)", "lang": "zh-TW"},
]

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_DECOR_RE = re.compile(r"[*_~#>]+")
_WHITESPACE_RE = re.compile(r"\s+")


def default_upstream_path(home: Path | None = None) -> Path:
    """Return the default upstream config path (`<shinobu_home>/tts/upstream.json`)."""
    base = Path(home) if home is not None else default_home()
    return base / "tts" / "upstream.json"


def clean_speech_text(raw: str) -> str:
    text = str(raw or "")
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_DECOR_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def resolve_voice(requested: str | None, fallback: str = DEFAULT_VOICE) -> str:
    name = str(requested or "").strip()
    if not name:
        return fallback
    lowered = name.lower()
    if lowered in VOICE_ALIASES:
        return VOICE_ALIASES[lowered]
    if "-" in name and name.endswith("Neural"):
        return name
    return fallback


def speed_to_rate(speed: Any) -> str:
    try:
        val = float(speed)
    except (TypeError, ValueError):
        val = 1.0
    val = max(0.5, min(2.0, val))
    pct = int(round((val - 1.0) * 100))
    return f"{pct:+d}%"


def load_upstream_config(path: Path) -> dict[str, Any]:
    default_cfg: dict[str, Any] = {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "timeout_sec": 30.0,
    }
    if not path.is_file():
        return default_cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_cfg
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        enabled = bool(raw.get("enabled", False)) and bool(base_url)
        try:
            timeout_sec = float(raw.get("timeout_sec", 30.0))
        except (TypeError, ValueError):
            timeout_sec = 30.0
        timeout_sec = max(3.0, min(120.0, timeout_sec))
        return {
            "enabled": enabled,
            "base_url": base_url,
            "api_key": api_key,
            "timeout_sec": timeout_sec,
        }
    except Exception:
        return default_cfg


def save_upstream_config(path: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    api_key = str(cfg.get("api_key") or "").strip()
    enabled = bool(cfg.get("enabled", bool(base_url))) and bool(base_url)
    try:
        timeout_sec = float(cfg.get("timeout_sec", 30.0))
    except (TypeError, ValueError):
        timeout_sec = 30.0
    timeout_sec = max(3.0, min(120.0, timeout_sec))
    normalized = {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "timeout_sec": timeout_sec,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return normalized


def build_upstream_speech_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/audio/speech"):
        return clean
    if clean.endswith("/v1"):
        return f"{clean}/audio/speech"
    return f"{clean}/v1/audio/speech"


def forward_to_upstream(
    cfg: dict[str, Any],
    payload: dict[str, Any],
    cleaned_text: str,
) -> tuple[bytes, str]:
    target = build_upstream_speech_url(cfg["base_url"])
    forward_body = dict(payload)
    forward_body["input"] = cleaned_text
    data = json.dumps(forward_body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "shinobu-tts-gateway/2.0",
    }
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(target, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=cfg["timeout_sec"]) as resp:
        audio = resp.read()
        content_type = resp.headers.get("Content-Type") or "audio/wav"
        if not audio:
            raise RuntimeError("Upstream GPU TTS returned empty audio")
        return audio, content_type


async def _synthesize_async(text: str, voice: str, rate: str, pitch: str) -> bytes:
    try:
        import edge_tts  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(EDGE_TTS_MISSING_MSG) from exc

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            chunks.append(chunk["data"])
    audio = b"".join(chunks)
    if not audio:
        raise RuntimeError("edge-tts returned empty audio stream")
    return audio


def synthesize_mp3(
    text: str,
    voice: str,
    rate: str = "+0%",
    pitch: str = "+0Hz",
    retries: int = 1,
) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return asyncio.run(_synthesize_async(text, voice, rate, pitch))
        except RuntimeError as exc:
            if EDGE_TTS_MISSING_MSG in str(exc):
                raise
            last_err = exc
            if attempt < retries:
                time.sleep(0.4)
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(0.4)
    assert last_err is not None
    raise last_err


class EdgeTTSServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        default_voice: str = DEFAULT_VOICE,
        upstream_path: Path | None = None,
    ) -> None:
        raw_host, port = server_address
        safe_host = validate_bind_host(raw_host)
        self.default_voice = default_voice
        self.upstream_path = (
            Path(upstream_path) if upstream_path is not None else default_upstream_path()
        )
        super().__init__((safe_host, int(port)), EdgeTTSHandler)


class EdgeTTSHandler(BaseHTTPRequestHandler):
    server: EdgeTTSServer

    def log_message(self, format: str, *args: Any) -> None:
        sys.stdout.write(f"[tts-gateway] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Requested-With",
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw_body = self.rfile.read(max(0, length))
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            return None, f"Invalid JSON body: {exc}"
        if not isinstance(payload, dict):
            return None, "Request body must be a JSON object"
        return payload, None

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        cfg = load_upstream_config(self.server.upstream_path)

        if path in ("/", "/health"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "engine": "gpu-upstream+edge-fallback" if cfg["enabled"] else "edge-tts",
                    "default_voice": self.server.default_voice,
                    "port": self.server.server_address[1],
                    "upstream_enabled": cfg["enabled"],
                    "upstream_base_url": cfg["base_url"] if cfg["enabled"] else "",
                },
            )
            return
        if path == "/api/upstream":
            self._send_json(
                HTTPStatus.OK,
                {
                    "enabled": cfg["enabled"],
                    "base_url": cfg["base_url"],
                    "has_api_key": bool(cfg["api_key"]),
                    "timeout_sec": cfg["timeout_sec"],
                    "config_path": str(self.server.upstream_path),
                },
            )
            return
        if path in ("/v1/models", "/models"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {"id": "tts-1", "object": "model", "owned_by": "shinobu-tts-gateway"},
                        {"id": "tts-1-hd", "object": "model", "owned_by": "shinobu-tts-gateway"},
                        {"id": "indextts-2.5", "object": "model", "owned_by": "shinobu-tts-gateway"},
                        {"id": "edge-tts", "object": "model", "owned_by": "shinobu-tts-gateway"},
                    ],
                },
            )
            return
        if path in ("/v1/audio/voices", "/v1/voices", "/voices"):
            self._send_json(
                HTTPStatus.OK,
                {"object": "list", "voices": PRESET_VOICES, "data": PRESET_VOICES},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": f"Not found: {path}"}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/api/upstream":
            payload, err = self._read_json_body()
            if err or payload is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": err}})
                return
            saved = save_upstream_config(self.server.upstream_path, payload)
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "updated",
                    "enabled": saved["enabled"],
                    "base_url": saved["base_url"],
                    "has_api_key": bool(saved["api_key"]),
                    "timeout_sec": saved["timeout_sec"],
                },
            )
            return

        if path not in ("/v1/audio/speech", "/audio/speech"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": f"Not found: {path}"}})
            return

        payload, err = self._read_json_body()
        if err or payload is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": err}})
            return

        text = clean_speech_text(payload.get("input", ""))
        if not text:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": "Empty input text after cleaning"}},
            )
            return

        cfg = load_upstream_config(self.server.upstream_path)
        backend_tag = "edge-tts"
        fallback_reason = ""

        if cfg["enabled"]:
            try:
                audio, content_type = forward_to_upstream(cfg, payload, text)
                self.send_response(HTTPStatus.OK)
                self._send_cors_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("X-Shinobu-TTS-Backend", "gpu-upstream")
                self.end_headers()
                self.wfile.write(audio)
                return
            except Exception as exc:
                fallback_reason = str(exc)[:180]
                backend_tag = "edge-tts-fallback"
                sys.stdout.write(
                    f"[tts-gateway] GPU upstream unavailable ({fallback_reason}); "
                    "falling back to local edge-tts.\n"
                )
                sys.stdout.flush()

        voice = resolve_voice(payload.get("voice"), fallback=self.server.default_voice)
        rate = speed_to_rate(payload.get("speed", 1.0))
        pitch = str(payload.get("pitch", "+0Hz") or "+0Hz")
        if not re.match(r"^[+-]\d+Hz$", pitch):
            pitch = "+0Hz"

        try:
            audio = synthesize_mp3(text=text, voice=voice, rate=rate, pitch=pitch)
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": {"message": f"edge-tts synthesis failed: {exc}"}},
            )
            return

        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Shinobu-TTS-Voice", voice)
        self.send_header("X-Shinobu-TTS-Backend", backend_tag)
        if fallback_reason:
            safe_reason = (
                fallback_reason.encode("ascii", "ignore")
                .decode("ascii")
                .replace("\r", " ")
                .replace("\n", " ")
                .strip()[:120]
            )
            if safe_reason:
                self.send_header("X-Shinobu-TTS-Fallback-Reason", safe_reason)
        self.end_headers()
        self.wfile.write(audio)


def _upstream_summary(cfg: dict[str, Any], path: Path, *, status: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled", False)),
        "base_url": str(cfg.get("base_url") or ""),
        "has_api_key": bool(cfg.get("api_key")),
        "timeout_sec": float(cfg.get("timeout_sec", 30.0)),
        "config_path": str(path),
    }
    if status is not None:
        out["status"] = status
    return out


def _emit_summary(data: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
        return
    status = data.get("status")
    prefix = f"[tts-gateway] {status}: " if status else "[tts-gateway] upstream: "
    mode = f"enabled ({data['base_url']})" if data["enabled"] else "disabled (edge-tts local)"
    print(
        f"{prefix}{mode} | has_api_key={data['has_api_key']} "
        f"| timeout_sec={data['timeout_sec']} | config={data['config_path']}",
        flush=True,
    )


def register_tts_gateway_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach `tts-gateway` CLI options to an ArgumentParser."""
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Loopback or private bind address (default: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Listen port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Default Edge-TTS voice when falling back locally (default: {DEFAULT_VOICE}).",
    )
    parser.add_argument(
        "--upstream-config",
        default=None,
        help="Path to upstream.json (default: <shinobu_home>/tts/upstream.json).",
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--set-upstream",
        metavar="URL",
        default=None,
        help="Persist and enable a remote GPU OpenAI-compatible TTS upstream URL, then exit.",
    )
    action_group.add_argument(
        "--clear-upstream",
        action="store_true",
        help="Disable and clear the configured GPU upstream in upstream.json, then exit.",
    )
    action_group.add_argument(
        "--show-upstream",
        action="store_true",
        help="Print current upstream.json configuration (with API key redacted), then exit.",
    )
    parser.add_argument(
        "--upstream-key",
        "--api-key",
        dest="upstream_key",
        default="",
        help="Optional Bearer API key used with --set-upstream.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output for upstream config actions.",
    )
    return parser


def run_tts_gateway_cli(args: argparse.Namespace, *, home: Path | None = None) -> int:
    """Execute the `shinobu tts-gateway` subcommand."""
    upstream_path = (
        Path(args.upstream_config).expanduser()
        if getattr(args, "upstream_config", None)
        else default_upstream_path(home)
    )
    as_json = bool(getattr(args, "json", False))

    if getattr(args, "set_upstream", None) is not None:
        existing = load_upstream_config(upstream_path)
        saved = save_upstream_config(
            upstream_path,
            {
                "enabled": True,
                "base_url": str(args.set_upstream),
                "api_key": str(getattr(args, "upstream_key", "") or ""),
                "timeout_sec": existing["timeout_sec"],
            },
        )
        _emit_summary(_upstream_summary(saved, upstream_path, status="updated"), as_json=as_json)
        return 0

    if getattr(args, "clear_upstream", False):
        existing = load_upstream_config(upstream_path)
        saved = save_upstream_config(
            upstream_path,
            {
                "enabled": False,
                "base_url": "",
                "api_key": "",
                "timeout_sec": existing["timeout_sec"],
            },
        )
        _emit_summary(_upstream_summary(saved, upstream_path, status="cleared"), as_json=as_json)
        return 0

    if getattr(args, "show_upstream", False):
        cfg = load_upstream_config(upstream_path)
        _emit_summary(_upstream_summary(cfg, upstream_path), as_json=as_json)
        return 0

    try:
        safe_host = validate_bind_host(str(getattr(args, "host", DEFAULT_HOST)))
    except ValueError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            print(f"[tts-gateway] Invalid --host: {exc}", file=sys.stderr, flush=True)
        return 2

    port = int(getattr(args, "port", DEFAULT_PORT))
    voice = str(getattr(args, "voice", DEFAULT_VOICE))
    server = EdgeTTSServer(
        (safe_host, port),
        default_voice=voice,
        upstream_path=upstream_path,
    )
    cfg = load_upstream_config(server.upstream_path)
    mode = (
        f"GPU upstream ({cfg['base_url']}) + edge-tts fallback"
        if cfg["enabled"]
        else "edge-tts local"
    )
    print(
        f"[tts-gateway] Listening on http://{safe_host}:{server.server_address[1]}/v1/audio/speech "
        f"| mode={mode} | default_voice={voice}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dual-mode local OpenAI-compatible TTS gateway")
    register_tts_gateway_arguments(parser)
    args = parser.parse_args(argv)
    return run_tts_gateway_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
