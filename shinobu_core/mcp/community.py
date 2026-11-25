# -*- coding: utf-8 -*-
"""
Curated zero-API-key community MCP presets and Antigravity (`agy`) MCP bridge.

Pure standard library (Python >= 3.10). Provides:
  - `COMMUNITY_MCP_PRESETS`: 4 official zero-API-key stdio MCP presets.
  - `list_community_presets(home=None)`: inspect available presets & install status.
  - `install_mcp_preset(home, preset_name, *, overwrite=False)`: non-destructive
    merge into `<home>/mcp_servers.json` with versioned backup under
    `<home>/_versions/mcp_servers_vN.json` and atomic `os.replace`.
  - `register_shinobu_onto_agy(...)`: reverse-register or unregister
    `shinobu mcp-serve` in the local `agy` CLI MCP registry without `shell=True`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from shinobu_core.core.kernel import default_home
from shinobu_core.engines.agy_engines import _augmented_agy_path
from .client import MCP_CONFIG_FILENAME

_BACKUP_NAME_RE = re.compile(r"^mcp_servers_v(\d+)\.json$")
_SANDBOX_PLACEHOLDER = "{sandbox_dir}"

COMMUNITY_MCP_PRESETS: dict[str, dict[str, Any]] = {
    "filesystem": {
        "name": "filesystem",
        "package": "@modelcontextprotocol/server-filesystem",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", _SANDBOX_PLACEHOLDER],
        "requires_api_key": False,
        "creates_sandbox": True,
        "description": "Official MCP filesystem server scoped to <home>/sandbox (zero API key).",
    },
    "memory": {
        "name": "memory",
        "package": "@modelcontextprotocol/server-memory",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "requires_api_key": False,
        "creates_sandbox": False,
        "description": "Official MCP knowledge-graph memory server (zero API key).",
    },
    "context7": {
        "name": "context7",
        "package": "@upstash/context7-mcp",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "requires_api_key": False,
        "creates_sandbox": False,
        "description": "Upstash Context7 up-to-date library documentation MCP server (zero API key).",
    },
    "sequential-thinking": {
        "name": "sequential-thinking",
        "package": "@modelcontextprotocol/server-sequential-thinking",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "requires_api_key": False,
        "creates_sandbox": False,
        "description": "Official MCP dynamic reflective sequential thinking server (zero API key).",
    },
}


def _resolve_home(home: Path | str | None) -> Path:
    return Path(home) if home is not None else default_home()


def _resolve_preset_entry(preset_name: str, home_path: Path) -> tuple[dict[str, Any], Path | None]:
    spec = COMMUNITY_MCP_PRESETS[preset_name]
    sandbox_dir = (home_path / "sandbox") if spec.get("creates_sandbox") else None
    resolved_args = [
        str(sandbox_dir) if (arg == _SANDBOX_PLACEHOLDER and sandbox_dir is not None) else str(arg)
        for arg in spec["args"]
    ]
    entry: dict[str, Any] = {
        "command": str(spec["command"]),
        "args": resolved_args,
        "disabled": False,
    }
    return entry, sandbox_dir


def _read_existing_server_names(config_path: Path) -> set[str]:
    if not config_path.is_file():
        return set()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    return {str(k).strip() for k in servers.keys() if str(k).strip()}


def _backup_mcp_config(config_path: Path, home_path: Path) -> Path | None:
    if not config_path.is_file():
        return None
    versions_dir = home_path / "_versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    max_version = 0
    for child in versions_dir.iterdir():
        if not child.is_file():
            continue
        match = _BACKUP_NAME_RE.match(child.name)
        if match:
            try:
                max_version = max(max_version, int(match.group(1)))
            except ValueError:
                pass
    backup_path = versions_dir / f"mcp_servers_v{max_version + 1}.json"
    shutil.copy2(config_path, backup_path)
    return backup_path


def _atomic_write_json(target_path: Path, payload: dict[str, Any]) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".{target_path.name}.{os.getpid()}.tmp"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(serialized, encoding="utf-8")
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def list_community_presets(home: Path | str | None = None) -> list[dict[str, Any]]:
    """Return all curated zero-API-key community MCP presets and their install status."""
    home_path = _resolve_home(home)
    config_path = home_path / MCP_CONFIG_FILENAME
    installed_names = _read_existing_server_names(config_path)

    items: list[dict[str, Any]] = []
    for name, spec in COMMUNITY_MCP_PRESETS.items():
        entry, sandbox_dir = _resolve_preset_entry(name, home_path)
        items.append(
            {
                "name": name,
                "package": str(spec["package"]),
                "description": str(spec["description"]),
                "command": entry["command"],
                "args": list(entry["args"]),
                "requires_api_key": bool(spec.get("requires_api_key", False)),
                "sandbox_dir": str(sandbox_dir) if sandbox_dir is not None else None,
                "installed": name in installed_names,
            }
        )
    return items


def install_mcp_preset(
    home: Path | str | None,
    preset_name: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Non-destructively merge a curated community MCP preset into `<home>/mcp_servers.json`.

    If `<home>/mcp_servers.json` already exists, a versioned backup is saved to
    `<home>/_versions/mcp_servers_vN.json` before atomically replacing the file.
    """
    clean_name = str(preset_name).strip()
    if clean_name not in COMMUNITY_MCP_PRESETS:
        available = ", ".join(COMMUNITY_MCP_PRESETS.keys())
        raise ValueError(
            f"Unknown community MCP preset: {preset_name!r}. Available presets: {available}"
        )

    home_path = _resolve_home(home)
    config_path = home_path / MCP_CONFIG_FILENAME

    data: dict[str, Any] = {}
    servers: dict[str, Any] = {}
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Failed to parse existing MCP config {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Expected JSON object in existing MCP config {config_path}")
        data = dict(raw)
        raw_servers = data.get("mcpServers")
        if raw_servers is None:
            servers = {}
        elif isinstance(raw_servers, dict):
            servers = dict(raw_servers)
        else:
            raise ValueError(f"'mcpServers' in {config_path} must be a JSON object")

    existed = clean_name in servers
    entry, sandbox_dir = _resolve_preset_entry(clean_name, home_path)

    if existed and not overwrite:
        existing_entry = servers[clean_name]
        return {
            "preset": clean_name,
            "status": "already_exists",
            "changed": False,
            "config_path": str(config_path),
            "backup_path": None,
            "sandbox_dir": str(sandbox_dir) if sandbox_dir is not None else None,
            "entry": dict(existing_entry) if isinstance(existing_entry, dict) else existing_entry,
        }

    home_path.mkdir(parents=True, exist_ok=True)
    if sandbox_dir is not None:
        sandbox_dir.mkdir(parents=True, exist_ok=True)

    backup_path = _backup_mcp_config(config_path, home_path)
    servers[clean_name] = entry
    data["mcpServers"] = servers
    _atomic_write_json(config_path, data)

    return {
        "preset": clean_name,
        "status": "overwritten" if existed else "installed",
        "changed": True,
        "config_path": str(config_path),
        "backup_path": str(backup_path) if backup_path is not None else None,
        "sandbox_dir": str(sandbox_dir) if sandbox_dir is not None else None,
        "entry": entry,
    }


def _resolve_agy_binary(agy_bin: str) -> str | None:
    raw = str(agy_bin or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return str(candidate)
    return shutil.which(raw, path=_augmented_agy_path())


def _resolve_shinobu_command(shinobu_bin: str | None) -> list[str]:
    if shinobu_bin is not None and str(shinobu_bin).strip():
        raw = str(shinobu_bin).strip()
        candidate = Path(raw)
        if candidate.is_file():
            return [str(candidate)]
        found = shutil.which(raw)
        return [found] if found else [raw]

    found = shutil.which("shinobu")
    if found:
        return [found]
    return [sys.executable, "-m", "shinobu_core"]


def register_shinobu_onto_agy(
    *,
    name: str = "shinobu",
    shinobu_bin: str | None = None,
    agy_bin: str = "agy",
    home: Path | str | None = None,
    remote_url: str | None = None,
    bearer_token: str | None = None,
    dry_run: bool = False,
    unregister: bool = False,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """
    Register or unregister `shinobu mcp-serve` (or a remote HTTP MCP endpoint)
    in the local Antigravity (`agy`) MCP registry.

    Uses `shutil.which` with `_augmented_agy_path()` to locate `agy` and falls back to
    `[sys.executable, "-m", "shinobu_core"]` when `shinobu` is not on PATH.
    Never invokes a shell (`shell=True` is strictly forbidden).
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("MCP registration name must be a non-empty string")

    action = "unregister" if unregister else "register"
    agy_path = _resolve_agy_binary(agy_bin)
    agy_exe = agy_path or str(agy_bin or "agy").strip()
    clean_remote_url = str(remote_url).strip() if remote_url is not None else ""
    clean_token = str(bearer_token).strip() if bearer_token is not None else ""

    if unregister:
        command = [agy_exe, "mcp", "remove", clean_name]
    elif clean_remote_url:
        command = [
            agy_exe,
            "mcp",
            "add",
            "--type",
            "http",
            *(("--header", f"Authorization: Bearer {clean_token}") if clean_token else ()),
            clean_name,
            clean_remote_url,
        ]
    else:
        cmd_args = _resolve_shinobu_command(shinobu_bin)
        if home is not None:
            cmd_args.extend(["--home", str(Path(home))])
        cmd_args.append("mcp-serve")
        command = [agy_exe, "mcp", "add", clean_name, *cmd_args]

    if agy_path is None:
        return {
            "ok": False,
            "action": action,
            "name": clean_name,
            "agy_path": None,
            "command": command,
            "dry_run": bool(dry_run),
            "executed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"agy CLI executable not found: {agy_bin!r}",
        }

    if dry_run:
        return {
            "ok": True,
            "action": action,
            "name": clean_name,
            "agy_path": agy_path,
            "command": command,
            "dry_run": True,
            "executed": False,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "error": None,
        }

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "action": action,
            "name": clean_name,
            "agy_path": agy_path,
            "command": command,
            "dry_run": False,
            "executed": True,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"agy CLI timed out after {exc.timeout}s",
        }
    except OSError as exc:
        return {
            "ok": False,
            "action": action,
            "name": clean_name,
            "agy_path": agy_path,
            "command": command,
            "dry_run": False,
            "executed": True,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"Failed to execute agy CLI: {exc}",
        }

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    ok = proc.returncode == 0
    error = None if ok else (stderr.strip() or stdout.strip() or f"agy exited with code {proc.returncode}")
    return {
        "ok": ok,
        "action": action,
        "name": clean_name,
        "agy_path": agy_path,
        "command": command,
        "dry_run": False,
        "executed": True,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
    }


__all__ = [
    "COMMUNITY_MCP_PRESETS",
    "install_mcp_preset",
    "list_community_presets",
    "register_shinobu_onto_agy",
]
