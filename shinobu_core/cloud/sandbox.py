# -*- coding: utf-8 -*-
"""Controlled 7x24 cloud sandbox terminal bound to workspace root and ApprovalGate."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence

from shinobu_core.cloud.approval import ApprovalGate, CommandTier, classify_command
from shinobu_core.protocol.engine import ToolSpec

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_SHORT_WIN_FLAG_RE = re.compile(r"^/[A-Za-z0-9?]{1,2}$")
_READONLY_PATH_COMMANDS: frozenset[str] = frozenset({"cat", "head", "tail", "ls", "dir"})
_PROTECTED_FILE_MARKERS: tuple[str, ...] = (
    ".allow_media",
    "agent-blindfold",
    "blindfold-control",
)


class SandboxPathError(ValueError):
    """Raised when a path escapes or violates the cloud sandbox workspace boundary."""


def _normalize_prog_name(raw: str) -> str:
    base = raw.strip().strip('"').strip("'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    marker = "\n...[truncated]"
    if limit <= len(marker):
        return text[:limit], True
    return text[: limit - len(marker)] + marker, True


def _truncate_pair(stdout: str, stderr: str, max_chars: int) -> tuple[str, str, bool]:
    if len(stdout) + len(stderr) <= max_chars:
        return stdout, stderr, False
    if not stderr:
        out, trunc = _truncate_text(stdout, max_chars)
        return out, "", trunc
    if not stdout:
        err, trunc = _truncate_text(stderr, max_chars)
        return "", err, trunc

    # Preserve stderr visibility while prioritizing stdout within the combined budget.
    err_budget = min(len(stderr), max(1, max_chars // 4))
    out_budget = max(0, max_chars - err_budget)
    if len(stdout) < out_budget:
        err_budget = max_chars - len(stdout)
        out_budget = len(stdout)
    out, out_trunc = _truncate_text(stdout, out_budget)
    err, err_trunc = _truncate_text(stderr, err_budget)
    return out, err, (out_trunc or err_trunc)


class CloudSandboxTerminal:
    """7x24 isolated cloud workspace terminal with strict path confinement and L0/L1/L2 gating."""

    def __init__(
        self,
        workspace_dir: Path | str,
        approval_gate: ApprovalGate,
        *,
        timeout_sec: float = 30.0,
        max_output_chars: int = 8000,
        approval_timeout_sec: float = 120.0,
    ) -> None:
        if approval_gate is None:
            raise ValueError("approval_gate must not be None")
        timeout = float(timeout_sec)
        if timeout <= 0:
            raise ValueError("timeout_sec must be greater than 0")
        max_chars = int(max_output_chars)
        if max_chars <= 0:
            raise ValueError("max_output_chars must be greater than 0")
        apr_timeout = float(approval_timeout_sec)
        if apr_timeout < 0:
            raise ValueError("approval_timeout_sec must be >= 0")

        ws_path = Path(workspace_dir)
        if ws_path.exists() and not ws_path.is_dir():
            raise ValueError(f"workspace_dir is not a directory: {ws_path}")
        ws_path.mkdir(parents=True, exist_ok=True)

        self.workspace_dir: Path = ws_path.resolve()
        self.approval_gate: ApprovalGate = approval_gate
        self.timeout_sec: float = timeout
        self.max_output_chars: int = max_chars
        self.approval_timeout_sec: float = apr_timeout

    def resolve_path(
        self,
        target: str | Path | None = None,
        *,
        base_dir: Path | None = None,
    ) -> Path:
        """Resolve a path and enforce that it stays strictly inside `workspace_dir`."""
        anchor = self.workspace_dir if base_dir is None else base_dir.resolve()
        if not anchor.is_relative_to(self.workspace_dir):
            raise SandboxPathError(f"base directory escapes sandbox workspace: {anchor}")

        if target is None:
            return anchor

        raw = str(target).strip()
        if "\x00" in raw:
            raise SandboxPathError("null byte in path is forbidden")
        if not raw or raw == ".":
            return anchor
        if raw == "~" or raw.startswith(("~/", "~\\")):
            raise SandboxPathError(f"home path expansion is forbidden outside sandbox: {raw}")
        if _DRIVE_PREFIX_RE.match(raw) and not Path(raw).is_absolute():
            raise SandboxPathError(f"drive-relative path is forbidden: {raw}")

        candidate = Path(raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (anchor / candidate).resolve()
        if not resolved.is_relative_to(self.workspace_dir):
            raise SandboxPathError(f"path escapes sandbox workspace: {raw}")
        return resolved

    def _validate_argv_paths(self, argv: list[str], cwd: Path) -> None:
        prog = _normalize_prog_name(argv[0])
        is_readonly_file_cmd = prog in _READONLY_PATH_COMMANDS

        for token in argv[1:]:
            if "\x00" in token:
                raise SandboxPathError("null byte in command argument is forbidden")

            # Inspect both the token itself and any `--opt=value` path payload.
            candidates = [token]
            if token.startswith("-") and "=" in token:
                _, rhs = token.split("=", 1)
                if rhs:
                    candidates.append(rhs)

            for raw in candidates:
                cleaned = raw.strip().strip('"').strip("'")
                if not cleaned:
                    continue

                # Always reject home-directory escapes.
                if cleaned == "~" or cleaned.startswith(("~/", "~\\")):
                    raise SandboxPathError(f"argument escapes sandbox via home path: {raw}")

                # Check any parent traversal (`..`) segment.
                parts = cleaned.replace("\\", "/").split("/")
                if ".." in parts:
                    self.resolve_path(cleaned, base_dir=cwd)
                    continue

                # Check explicit drive-qualified or absolute filesystem paths.
                if _DRIVE_PREFIX_RE.match(cleaned) or cleaned.startswith("\\\\"):
                    self.resolve_path(cleaned, base_dir=cwd)
                    continue

                if cleaned.startswith("/") and not _SHORT_WIN_FLAG_RE.match(cleaned):
                    self.resolve_path(cleaned, base_dir=cwd)
                    continue

                # For L0 readonly file commands, also validate relative/symlink targets.
                if is_readonly_file_cmd and raw is token:
                    if cleaned.startswith("-") or _SHORT_WIN_FLAG_RE.match(cleaned):
                        continue
                    self.resolve_path(cleaned, base_dir=cwd)

    def exec_command(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Execute a tokenized command inside the sandbox after path & L0/L1/L2 checks."""
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise TypeError("argv must be a sequence of argument strings, not str/bytes")

        argv_list: list[str] = []
        for item in argv:
            if not isinstance(item, str):
                raise TypeError("every element in argv must be a str")
            if item:
                argv_list.append(item)

        if not argv_list:
            return {
                "ok": False,
                "status": "forbidden",
                "tier": CommandTier.L2_FORBIDDEN.value,
                "reason": "empty command is forbidden",
                "argv": [],
                "cwd": str(self.workspace_dir),
            }

        eff_timeout = self.timeout_sec if timeout_sec is None else float(timeout_sec)
        if eff_timeout <= 0:
            raise ValueError("timeout_sec must be greater than 0")

        # 1. Enforce cwd confinement inside workspace_dir.
        try:
            resolved_cwd = self.resolve_path(cwd)
        except SandboxPathError as exc:
            return {
                "ok": False,
                "status": "path_denied",
                "tier": "",
                "reason": str(exc),
                "argv": argv_list,
                "cwd": str(cwd or ""),
            }
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            return {
                "ok": False,
                "status": "path_denied",
                "tier": "",
                "reason": f"working directory does not exist inside sandbox: {resolved_cwd}",
                "argv": argv_list,
                "cwd": str(resolved_cwd),
            }

        # 2. Classify command into L0 / L1 / L2.
        tier, reason = classify_command(argv_list)
        if tier == CommandTier.L2_FORBIDDEN:
            return {
                "ok": False,
                "status": "forbidden",
                "tier": tier.value,
                "reason": reason,
                "argv": argv_list,
                "cwd": str(resolved_cwd),
            }

        # 3. Block argument-level path traversal / sandbox escape before approval or execution.
        try:
            self._validate_argv_paths(argv_list, resolved_cwd)
        except SandboxPathError as exc:
            return {
                "ok": False,
                "status": "path_denied",
                "tier": tier.value,
                "reason": str(exc),
                "argv": argv_list,
                "cwd": str(resolved_cwd),
            }

        # 4. Require explicit ApprovalGate sign-off for L1 commands.
        approval_id = ""
        actor = ""
        if tier == CommandTier.L1_APPROVAL:
            decision = self.approval_gate.request_approval(
                action="cloud_exec",
                target=" ".join(argv_list),
                detail=f"cwd={resolved_cwd}; reason={reason}",
                timeout_sec=self.approval_timeout_sec,
            )
            approval_id = decision.approval_id
            actor = decision.actor
            if not decision.approved:
                return {
                    "ok": False,
                    "status": decision.status,
                    "tier": tier.value,
                    "reason": decision.reason,
                    "approval_id": approval_id,
                    "actor": actor,
                    "argv": argv_list,
                    "cwd": str(resolved_cwd),
                }

        # 5. Execute strictly with shell=False inside resolved_cwd.
        try:
            completed = subprocess.run(
                argv_list,
                cwd=str(resolved_cwd),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=eff_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raw_out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            raw_err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            stdout_text, stderr_text, truncated = _truncate_pair(raw_out, raw_err, self.max_output_chars)
            return {
                "ok": False,
                "status": "timeout",
                "tier": tier.value,
                "reason": f"command timed out after {eff_timeout:g}s",
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": truncated,
                "approval_id": approval_id,
                "actor": actor,
                "argv": argv_list,
                "cwd": str(resolved_cwd),
            }
        except OSError as exc:
            return {
                "ok": False,
                "status": "exec_error",
                "tier": tier.value,
                "reason": str(exc),
                "approval_id": approval_id,
                "actor": actor,
                "argv": argv_list,
                "cwd": str(resolved_cwd),
            }

        stdout_text, stderr_text, truncated = _truncate_pair(
            completed.stdout or "",
            completed.stderr or "",
            self.max_output_chars,
        )
        result: dict[str, Any] = {
            "ok": completed.returncode == 0,
            "status": "ok" if completed.returncode == 0 else "nonzero_exit",
            "tier": tier.value,
            "returncode": completed.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": truncated,
            "argv": argv_list,
            "cwd": str(resolved_cwd),
        }
        if approval_id:
            result["approval_id"] = approval_id
            result["actor"] = actor
        return result

    def read_file(
        self,
        path: str | Path,
        *,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        """Read a UTF-8 text slice from a file strictly inside `workspace_dir`."""
        try:
            start = max(1, int(start_line))
            limit_lines = max(1, int(max_lines))
        except (TypeError, ValueError) as exc:
            return {
                "ok": False,
                "status": "invalid_args",
                "reason": str(exc),
                "path": str(path),
            }

        try:
            resolved = self.resolve_path(path)
        except SandboxPathError as exc:
            return {
                "ok": False,
                "status": "path_denied",
                "reason": str(exc),
                "path": str(path),
            }

        low_resolved = str(resolved).lower()
        for marker in _PROTECTED_FILE_MARKERS:
            if marker in low_resolved:
                return {
                    "ok": False,
                    "status": "forbidden",
                    "reason": f"reading protected privacy control file ({marker}) is forbidden",
                    "path": str(path),
                }

        if not resolved.exists():
            return {
                "ok": False,
                "status": "not_found",
                "reason": f"file does not exist: {path}",
                "path": str(path),
            }
        if not resolved.is_file():
            return {
                "ok": False,
                "status": "invalid_target",
                "reason": f"target is not a regular file: {path}",
                "path": str(path),
            }

        try:
            raw_text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {
                "ok": False,
                "status": "read_error",
                "reason": str(exc),
                "path": str(path),
            }

        lines = raw_text.splitlines()
        total_lines = len(lines)
        sliced = lines[start - 1 : start - 1 + limit_lines]
        joined = "\n".join(sliced)
        content, char_truncated = _truncate_text(joined, self.max_output_chars)
        line_truncated = (start - 1 + len(sliced)) < total_lines
        rel_path = resolved.relative_to(self.workspace_dir).as_posix()

        return {
            "ok": True,
            "status": "ok",
            "path": rel_path,
            "start_line": start,
            "end_line": start + len(sliced) - 1 if sliced else start - 1,
            "total_lines": total_lines,
            "truncated": bool(char_truncated or line_truncated),
            "content": content,
        }

    def build_tools(self) -> list[ToolSpec]:
        """Expose `cloud_exec` and `cloud_read_file` as EngineAdapter-compatible ToolSpecs."""

        def _handle_cloud_exec(
            argv: Sequence[str] | None = None,
            cwd: str | None = None,
            timeout_sec: float | None = None,
        ) -> str:
            try:
                result = self.exec_command(argv, cwd=cwd, timeout_sec=timeout_sec)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                result = {
                    "ok": False,
                    "status": "invalid_args",
                    "reason": str(exc),
                }
            return json.dumps(result, ensure_ascii=False)

        def _handle_cloud_read_file(
            path: str = "",
            start_line: int = 1,
            max_lines: int = 200,
        ) -> str:
            result = self.read_file(path, start_line=start_line, max_lines=max_lines)
            return json.dumps(result, ensure_ascii=False)

        return [
            ToolSpec(
                name="cloud_exec",
                description=(
                    "Execute a tokenized command (`argv` list, never a shell string) inside "
                    "the isolated cloud workspace. L0 readonly commands run directly, L1 "
                    "commands require operator approval, and L2 commands are forbidden."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tokenized command arguments (e.g. ['git', 'status']).",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory relative to sandbox workspace.",
                        },
                        "timeout_sec": {
                            "type": "number",
                            "description": "Optional execution timeout in seconds.",
                        },
                    },
                    "required": ["argv"],
                },
                handler=_handle_cloud_exec,
            ),
            ToolSpec(
                name="cloud_read_file",
                description=(
                    "Read lines from a text file located inside the isolated cloud workspace. "
                    "Paths escaping the workspace are strictly denied."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the sandbox workspace.",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "1-based starting line number (default 1).",
                        },
                        "max_lines": {
                            "type": "integer",
                            "description": "Maximum number of lines to return (default 200).",
                        },
                    },
                    "required": ["path"],
                },
                handler=_handle_cloud_read_file,
            ),
        ]


__all__ = [
    "CloudSandboxTerminal",
    "SandboxPathError",
]
