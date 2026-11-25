# -*- coding: utf-8 -*-
"""L0/L1/L2 command safety classification and thread-safe approval gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
import threading
import time
from typing import Any, Callable, Sequence
import uuid

from shinobu_core.protocol.events import Event, EventKind


class CommandTier(str, Enum):
    """Three-tier terminal execution security classification."""

    L0_READONLY = "L0"
    L1_APPROVAL = "L1"
    L2_FORBIDDEN = "L2"


_L0_DIRECT_COMMANDS: frozenset[str] = frozenset(
    {
        "ls",
        "dir",
        "pwd",
        "whoami",
        "uptime",
        "df",
        "free",
        "ps",
        "cat",
        "head",
        "tail",
    }
)

_L0_GIT_SUBCOMMANDS: frozenset[str] = frozenset({"status", "log", "diff"})

_L2_FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "runas",
        "pkexec",
        "gsudo",
        "mkfs",
        "dd",
        "fdisk",
        "parted",
        "wipefs",
        "diskpart",
        "format",
        "shutdown",
        "reboot",
        "poweroff",
        "halt",
        "reg",
        "regedit",
        "visudo",
        "passwd",
        "useradd",
        "userdel",
        "usermod",
        "icacls",
        "takeown",
    }
)

_SHELL_WRAPPERS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh", "cmd", "powershell", "pwsh"}
)

_PRIVACY_AND_AUTH_MARKERS: tuple[str, ...] = (
    ".allow_media",
    "agent-blindfold",
    "blindfold-control",
    "/etc/shadow",
    "/etc/sudoers",
    "sudoers.d",
)

_SHELL_WRITE_OR_CHAIN_RE = re.compile(r"(?:>>?|\|\||&&|\||;|`|\$\(|\r|\n)")

_CRITICAL_DELETE_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/*",
        "\\*",
        "\\",
        "~",
        "~/",
        "$home",
        "${home}",
        "%userprofile%",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/boot",
        "/lib",
        "/lib64",
        "/var",
        "/root",
        "/home",
        "/sys",
        "/proc",
        "/dev",
    }
)

_DRIVE_ROOT_RE = re.compile(r"^[a-z]:[\\/]*\*?$")
_SYSTEM_WIN_PATH_RE = re.compile(r"^[a-z]:[\\/]windows(?:[\\/]system32)?[\\/]*$")

_L2_INLINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[\s;|&`'(])sudo(?:\s|$)"), "privilege escalation via sudo is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])mkfs(?:\.\w+)?(?:\s|$)"), "filesystem formatting via mkfs is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])dd\s+"), "raw block device write via dd is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])(?:shutdown|reboot|poweroff|halt)(?:\s|$)"), "power control command is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])reg(?:\.exe)?\s+"), "registry modification via reg is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])net1?(?:\.exe)?\s+(?:user|localgroup|accounts)(?:\s|$)"), "account management via net is forbidden"),
    (re.compile(r"(?:^|[\s;|&`'(])rm\s+[^\n]*-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)\b[^\n]*\s+/(?:\*|\s|$)"), "destructive root deletion is forbidden"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"), "fork bomb pattern is forbidden"),
    (re.compile(r">\s*/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z])"), "raw block device overwrite is forbidden"),
)


def _normalize_program_name(raw: str) -> str:
    cleaned = raw.strip().strip('"').strip("'").replace("\\", "/")
    base = cleaned.rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base


def _is_critical_delete_target(arg: str) -> bool:
    norm = arg.strip().strip('"').strip("'").lower()
    if not norm:
        return False
    if norm in _CRITICAL_DELETE_PATHS:
        return True
    if _DRIVE_ROOT_RE.match(norm):
        return True
    if _SYSTEM_WIN_PATH_RE.match(norm):
        return True
    # Normalize trailing slashes for Unix system dirs like /etc/
    trimmed = norm.rstrip("/")
    if trimmed and trimmed in _CRITICAL_DELETE_PATHS:
        return True
    return False


def _is_rm_destructive(args: Sequence[str]) -> bool:
    recursive = False
    targets: list[str] = []
    after_double_dash = False
    for tok in args:
        low = tok.lower()
        if not after_double_dash and low == "--":
            after_double_dash = True
            continue
        if not after_double_dash and low == "--no-preserve-root":
            return True
        if not after_double_dash and low.startswith("--"):
            if low == "--recursive":
                recursive = True
            continue
        if not after_double_dash and low.startswith("-") and len(low) > 1:
            if "r" in low[1:]:
                recursive = True
            continue
        targets.append(tok)
    if not recursive:
        return False
    return any(_is_critical_delete_target(target) for target in targets)


def _is_windows_destructive_delete(prog: str, args: Sequence[str]) -> bool:
    low_args = [a.lower() for a in args]
    recursive = False
    if prog in {"rd", "rmdir", "del", "erase"}:
        recursive = any(a in {"/s", "-s"} for a in low_args)
    elif prog in {"remove-item", "ri", "rm"}:
        recursive = any(a in {"-recurse", "-r"} for a in low_args)
    if not recursive:
        return False
    return any(_is_critical_delete_target(a) for a in args if not a.startswith(("/", "-")))


def _extract_git_subcommand(args: Sequence[str]) -> tuple[str | None, bool]:
    """Return (subcommand, has_unsafe_global_option) for a git argv tail."""
    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "--":
            return None, True
        if tok in {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree"}:
            return None, True
        if tok.startswith(("-c", "--exec-path=", "--git-dir=", "--work-tree=", "--config-env=")):
            return None, True
        if tok in {"-C"}:
            idx += 2
            continue
        if tok in {"--no-pager", "-P", "--paginate", "-p"}:
            idx += 1
            continue
        if tok.startswith("-"):
            return None, True
        return tok.lower(), False
    return None, False


def classify_command(argv: Sequence[str]) -> tuple[CommandTier, str]:
    """Classify a tokenized command into L0 (readonly), L1 (approval), or L2 (forbidden)."""
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of argument strings, not str/bytes")
    tokens = [str(part).strip() for part in argv if str(part).strip()]
    if not tokens:
        return CommandTier.L2_FORBIDDEN, "empty command is forbidden"

    prog = _normalize_program_name(tokens[0])
    args = tokens[1:]
    joined_lower = " ".join(tokens).lower()

    # 1. Privacy shield / privilege configuration tampering -> L2
    for marker in _PRIVACY_AND_AUTH_MARKERS:
        if marker in joined_lower:
            return (
                CommandTier.L2_FORBIDDEN,
                f"accessing or modifying protected privacy/auth path ({marker}) is forbidden",
            )

    # 2. Directly forbidden binaries -> L2
    if prog in _L2_FORBIDDEN_COMMANDS or prog.startswith("mkfs."):
        return (
            CommandTier.L2_FORBIDDEN,
            f"command '{prog}' is unconditionally forbidden (L2)",
        )

    # 3. Account / privilege subcommands -> L2
    if prog in {"net", "net1"} and args and args[0].lower() in {"user", "localgroup", "accounts"}:
        return (
            CommandTier.L2_FORBIDDEN,
            f"account management command '{prog} {args[0].lower()}' is forbidden (L2)",
        )

    # 4. Critical chmod/chown on root/system paths -> L2
    if prog in {"chmod", "chown"} and any(_is_critical_delete_target(a) for a in args):
        return (
            CommandTier.L2_FORBIDDEN,
            f"permission mutation on critical system path via '{prog}' is forbidden (L2)",
        )

    # 5. Catastrophic recursive deletion -> L2
    if prog == "rm" and _is_rm_destructive(args):
        return (
            CommandTier.L2_FORBIDDEN,
            "destructive recursive deletion targeting root or system path is forbidden (L2)",
        )
    if prog in {"rd", "rmdir", "del", "erase", "remove-item", "ri"} and _is_windows_destructive_delete(prog, args):
        return (
            CommandTier.L2_FORBIDDEN,
            "destructive recursive deletion targeting drive root or system path is forbidden (L2)",
        )

    # 6. Inline L2 pattern inspection (especially shell wrappers or embedded payloads)
    for pattern, reason in _L2_INLINE_PATTERNS:
        if pattern.search(joined_lower):
            return CommandTier.L2_FORBIDDEN, reason

    if prog in _SHELL_WRAPPERS:
        return (
            CommandTier.L1_APPROVAL,
            f"shell wrapper '{prog}' requires explicit approval (L1)",
        )

    # 7. Reject any write redirection / chaining / subshell metacharacters from L0
    if any(_SHELL_WRITE_OR_CHAIN_RE.search(tok) for tok in tokens):
        return (
            CommandTier.L1_APPROVAL,
            "command contains redirection or shell chaining operators and requires approval (L1)",
        )

    # 8. L0 strict readonly allowlist
    if prog in _L0_DIRECT_COMMANDS:
        return CommandTier.L0_READONLY, f"readonly allowlisted command '{prog}'"

    if prog == "git":
        subcmd, unsafe = _extract_git_subcommand(args)
        if not unsafe and subcmd in _L0_GIT_SUBCOMMANDS:
            return CommandTier.L0_READONLY, f"readonly allowlisted command 'git {subcmd}'"
        return (
            CommandTier.L1_APPROVAL,
            f"git command '{subcmd or 'unknown'}' requires explicit approval (L1)",
        )

    # 9. Everything else defaults to L1 approval
    return (
        CommandTier.L1_APPROVAL,
        f"command '{prog}' is not in readonly allowlist and requires approval (L1)",
    )


@dataclass(frozen=True)
class ApprovalDecision:
    """Immutable outcome of an approval request."""

    approval_id: str
    approved: bool
    status: str
    actor: str = ""
    reason: str = ""
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize decision into a JSON-compatible dictionary."""
        return {
            "approval_id": self.approval_id,
            "approved": self.approved,
            "status": self.status,
            "actor": self.actor,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


@dataclass
class _PendingApproval:
    approval_id: str
    action: str
    target: str
    detail: str
    created_at: float
    timeout_sec: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        expires_at = self.created_at + self.timeout_sec
        return {
            "approval_id": self.approval_id,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "created_at": self.created_at,
            "timeout_sec": self.timeout_sec,
            "expires_at": expires_at,
            "remaining_sec": max(0.0, expires_at - current),
            "status": "pending",
        }


class ApprovalGate:
    """Thread-safe blocking approval gate with timeout auto-denial."""

    def __init__(
        self,
        on_event: Callable[[Event], None] | None = None,
        *,
        max_history: int = 256,
    ) -> None:
        self._lock = threading.RLock()
        self._on_event = on_event
        self._max_history = max(1, int(max_history))
        self._pending: dict[str, _PendingApproval] = {}
        self._history: dict[str, ApprovalDecision] = {}

    def _emit(self, phase: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        event = Event(
            kind=EventKind.TOOL_CALL,
            payload={"source": "approval_gate", "phase": phase, **payload},
        )
        try:
            self._on_event(event)
        except Exception:
            # Observer failures must never stall or crash approval resolution.
            pass

    def _record_history(self, decision: ApprovalDecision) -> None:
        self._history[decision.approval_id] = decision
        if len(self._history) > self._max_history:
            oldest_key = next(iter(self._history))
            self._history.pop(oldest_key, None)

    def request_approval(
        self,
        action: str,
        target: str,
        detail: str = "",
        timeout_sec: float = 120.0,
    ) -> ApprovalDecision:
        """Register an approval request and block until decided or timed out."""
        timeout = float(timeout_sec)
        if timeout < 0:
            raise ValueError("timeout_sec must be >= 0")

        now = time.time()
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        pending = _PendingApproval(
            approval_id=approval_id,
            action=str(action),
            target=str(target),
            detail=str(detail),
            created_at=now,
            timeout_sec=timeout,
        )
        with self._lock:
            self._pending[approval_id] = pending
            request_snapshot = pending.to_dict(now)

        # Emit outside lock so callbacks can safely call decide() or list_pending().
        self._emit("approval_requested", request_snapshot)

        pending.event.wait(timeout=timeout)

        timed_out_decision: ApprovalDecision | None = None
        with self._lock:
            if pending.decision is not None:
                self._pending.pop(approval_id, None)
                return pending.decision

            self._pending.pop(approval_id, None)
            timed_out_decision = ApprovalDecision(
                approval_id=approval_id,
                approved=False,
                status="timeout",
                actor="system",
                reason=f"approval timed out after {timeout:g}s",
            )
            pending.decision = timed_out_decision
            pending.event.set()
            self._record_history(timed_out_decision)

        self._emit("approval_resolved", timed_out_decision.to_dict())
        return timed_out_decision

    def decide(
        self,
        approval_id: str,
        approved: bool,
        actor: str = "",
        reason: str = "",
    ) -> bool:
        """Resolve a pending approval request. Returns False if missing or already resolved."""
        with self._lock:
            pending = self._pending.pop(approval_id, None)
            if pending is None or pending.decision is not None:
                return False
            is_approved = bool(approved)
            decision = ApprovalDecision(
                approval_id=approval_id,
                approved=is_approved,
                status="approved" if is_approved else "denied",
                actor=str(actor),
                reason=str(reason) or ("approved by operator" if is_approved else "denied by operator"),
            )
            pending.decision = decision
            self._record_history(decision)
            pending.event.set()

        self._emit("approval_resolved", decision.to_dict())
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        """List all currently pending approval requests ordered by creation time."""
        now = time.time()
        with self._lock:
            pending_items = list(self._pending.values())
        pending_items.sort(key=lambda item: (item.created_at, item.approval_id))
        return [item.to_dict(now) for item in pending_items]

    def get_decision(self, approval_id: str) -> ApprovalDecision | None:
        """Return a previously recorded decision if available."""
        with self._lock:
            return self._history.get(approval_id)

    def cancel_all(self, reason: str = "gate shutdown") -> int:
        """Cancel and deny all pending requests (useful during graceful shutdown)."""
        decisions: list[ApprovalDecision] = []
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
            for pending in items:
                if pending.decision is not None:
                    continue
                decision = ApprovalDecision(
                    approval_id=pending.approval_id,
                    approved=False,
                    status="cancelled",
                    actor="system",
                    reason=str(reason),
                )
                pending.decision = decision
                self._record_history(decision)
                pending.event.set()
                decisions.append(decision)
        for decision in decisions:
            self._emit("approval_resolved", decision.to_dict())
        return len(decisions)


__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "CommandTier",
    "classify_command",
]
