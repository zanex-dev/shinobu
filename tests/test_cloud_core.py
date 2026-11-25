# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core.cloud registry and L0/L1/L2 approval gate."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shinobu_core.cloud import (  # noqa: E402
    ApprovalDecision,
    ApprovalGate,
    CommandTier,
    WorkerRecord,
    WorkerRegistry,
    classify_command,
)
from shinobu_core.protocol.events import Event  # noqa: E402


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class TestWorkerRegistry(unittest.TestCase):
    def test_register_heartbeat_ttl_and_routing(self) -> None:
        clock = _FakeClock(start=10_000.0)
        events: list[Event] = []
        reg = WorkerRegistry(clock=clock, on_event=events.append)

        rec = WorkerRecord(
            worker_id="gpu-colab-1",
            role="gpu_worker",
            base_url="https://colab.example.invalid/",
            capabilities=("tts", "llm", "tts"),
            token="secret-worker-token",
            ttl_sec=30.0,
            last_seen=clock(),
            meta={"gpu": "T4"},
        )
        stored = reg.register(rec)
        self.assertEqual(stored.base_url, "https://colab.example.invalid")
        self.assertEqual(stored.capabilities, ("tts", "llm"))
        self.assertEqual(len(events), 1)

        # Token must not leak in default list_workers() output.
        listed = reg.list_workers()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertTrue(listed[0]["has_token"])
        self.assertTrue(listed[0]["online"])

        # Heartbeat refreshes last_seen and merges metadata.
        clock.advance(20.0)
        self.assertTrue(reg.heartbeat("gpu-colab-1", meta={"load": 0.25}))
        self.assertFalse(reg.heartbeat("missing-worker"))

        clock.advance(20.0)  # 40s since register, but only 20s since heartbeat (< 30s TTL)
        online = reg.get_online("tts", role="gpu_worker")
        self.assertIsNotNone(online)
        assert online is not None
        self.assertEqual(online.worker_id, "gpu-colab-1")
        self.assertEqual(online.meta["gpu"], "T4")
        self.assertEqual(online.meta["load"], 0.25)

        # Advance past TTL -> excluded from online queries and default list_workers.
        clock.advance(10.0)  # 30s since last heartbeat -> expired
        self.assertIsNone(reg.get_online("tts"))
        self.assertEqual(reg.list_workers(include_expired=False), [])

        all_workers = reg.list_workers(include_expired=True)
        self.assertEqual(len(all_workers), 1)
        self.assertFalse(all_workers[0]["online"])
        self.assertTrue(all_workers[0]["expired"])

        # prune_expired physically evicts expired entries.
        evicted = reg.prune_expired()
        self.assertEqual(evicted, ["gpu-colab-1"])
        self.assertEqual(reg.list_workers(include_expired=True), [])

    def test_selects_freshest_worker_by_capability_and_role(self) -> None:
        clock = _FakeClock(start=20_000.0)
        reg = WorkerRegistry(clock=clock)

        reg.register(
            WorkerRecord(
                worker_id="edge-pc-a",
                role="edge_pc",
                base_url="http://127.0.0.1:9101",
                capabilities=("terminal", "fs"),
                ttl_sec=45.0,
                last_seen=clock(),
            )
        )
        clock.advance(5.0)
        reg.register(
            WorkerRecord(
                worker_id="edge-pc-b",
                role="edge_pc",
                base_url="http://127.0.0.1:9102",
                capabilities=("terminal",),
                ttl_sec=45.0,
                last_seen=clock(),
            )
        )
        reg.register(
            WorkerRecord(
                worker_id="gpu-1",
                role="gpu_worker",
                base_url="https://gpu.example.invalid",
                capabilities=("tts",),
                ttl_sec=45.0,
                last_seen=clock(),
            )
        )

        chosen = reg.get_online("terminal", role="edge_pc")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.worker_id, "edge-pc-b")

        # Heartbeat on edge-pc-a makes it fresher than edge-pc-b.
        clock.advance(2.0)
        self.assertTrue(reg.heartbeat("edge-pc-a"))
        chosen2 = reg.get_online("terminal", role="edge_pc")
        assert chosen2 is not None
        self.assertEqual(chosen2.worker_id, "edge-pc-a")

        # Role mismatch returns None.
        self.assertIsNone(reg.get_online("terminal", role="gpu_worker"))

        # Unregister works once and then returns False.
        self.assertTrue(reg.unregister("edge-pc-a"))
        self.assertFalse(reg.unregister("edge-pc-a"))

    def test_validation_and_thread_safety(self) -> None:
        with self.assertRaises(ValueError):
            WorkerRecord(worker_id="", role="edge", base_url="http://localhost")
        with self.assertRaises(ValueError):
            WorkerRecord(worker_id="w1", role="edge", base_url="http://localhost", ttl_sec=0)

        reg = WorkerRegistry()
        errors: list[Exception] = []

        def worker_task(idx: int) -> None:
            try:
                wid = f"worker-{idx}"
                reg.register(
                    WorkerRecord(
                        worker_id=wid,
                        role="edge_pc",
                        base_url=f"http://127.0.0.1:{9200 + idx}",
                        capabilities=("terminal",),
                        ttl_sec=60.0,
                    )
                )
                for step in range(5):
                    reg.heartbeat(wid, meta={"step": step})
                    reg.list_workers()
                    reg.get_online("terminal")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(reg.list_workers()), 8)


class TestCommandClassifier(unittest.TestCase):
    def test_l0_readonly_allowlist(self) -> None:
        readonly_cmds = [
            ["ls", "-la"],
            ["dir"],
            ["pwd"],
            ["whoami"],
            ["uptime"],
            ["df", "-h"],
            ["free", "-m"],
            ["ps", "aux"],
            ["git", "status"],
            ["git", "log", "-n", "5"],
            ["git", "diff", "HEAD~1"],
            ["git", "-C", "repo", "status"],
            ["cat", "README.md"],
            ["head", "-n", "20", "notes.txt"],
            ["tail", "-n", "20", "notes.txt"],
        ]
        for argv in readonly_cmds:
            with self.subTest(argv=argv):
                tier, reason = classify_command(argv)
                self.assertEqual(tier, CommandTier.L0_READONLY, reason)

    def test_l2_forbidden_hard_blocks(self) -> None:
        forbidden_cmds = [
            [],
            ["rm", "-rf", "/"],
            ["rm", "-fr", "/*"],
            ["rm", "--recursive", "--force", "/etc"],
            ["rm", "--no-preserve-root", "-rf", "/tmp/demo"],
            ["sudo", "ls"],
            ["/usr/bin/sudo", "cat", "README.md"],
            ["mkfs.ext4", "/dev/sda1"],
            ["dd", "if=/dev/zero", "of=/dev/sda"],
            ["shutdown", "-h", "now"],
            ["reg", "delete", "HKLM\\Software\\Example"],
            ["reg.exe", "add", "HKCU\\Software\\Test"],
            ["net", "user", "operator", "Pass123!"],
            ["net", "localgroup", "administrators", "operator", "/add"],
            ["cat", ".allow_media"],
            ["cat", "agent-blindfold.json"],
            ["bash", "-c", "sudo rm -rf /"],
            ["rd", "/s", "/q", "X:\\"],
            ["chmod", "777", "/"],
        ]
        for argv in forbidden_cmds:
            with self.subTest(argv=argv):
                tier, reason = classify_command(argv)
                self.assertEqual(tier, CommandTier.L2_FORBIDDEN, reason)

    def test_l1_approval_required_and_redirection_downgrade(self) -> None:
        l1_cmds = [
            ["python", "-m", "unittest"],
            ["git", "commit", "-m", "feat: cloud core"],
            ["git", "push", "origin", "main"],
            ["git", "-c", "core.pager=sh", "log"],
            ["mkdir", "build"],
            ["rm", "temp_artifact.txt"],
            ["pip", "install", "example"],
            ["cat", "a.txt", ">", "b.txt"],
            ["ls", "&&", "rm", "temp.txt"],
            ["git", "status;whoami"],
            ["bash", "-c", "ls -la"],
        ]
        for argv in l1_cmds:
            with self.subTest(argv=argv):
                tier, reason = classify_command(argv)
                self.assertEqual(tier, CommandTier.L1_APPROVAL, reason)

    def test_rejects_raw_string_argument(self) -> None:
        with self.assertRaises(TypeError):
            classify_command("ls -la")  # type: ignore[arg-type]


class TestApprovalGate(unittest.TestCase):
    def test_async_approve_flow_and_events(self) -> None:
        events: list[Event] = []
        gate = ApprovalGate(on_event=events.append)
        result_holder: list[ApprovalDecision] = []

        def requester() -> None:
            decision = gate.request_approval(
                action="exec_command",
                target="edge-pc-1",
                detail="git commit -m 'test'",
                timeout_sec=5.0,
            )
            result_holder.append(decision)

        thread = threading.Thread(target=requester)
        thread.start()

        pending = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pending = gate.list_pending()
            if pending:
                break
            time.sleep(0.01)

        self.assertEqual(len(pending), 1)
        approval_id = pending[0]["approval_id"]
        self.assertEqual(pending[0]["action"], "exec_command")
        self.assertEqual(pending[0]["target"], "edge-pc-1")

        self.assertTrue(gate.decide(approval_id, True, actor="owner"))
        # Second decision on same request must be rejected.
        self.assertFalse(gate.decide(approval_id, False, actor="owner"))

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result_holder), 1)

        decision = result_holder[0]
        self.assertTrue(decision.approved)
        self.assertEqual(decision.status, "approved")
        self.assertEqual(decision.actor, "owner")
        self.assertEqual(gate.list_pending(), [])
        self.assertEqual(gate.get_decision(approval_id), decision)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].payload["phase"], "approval_requested")
        self.assertEqual(events[1].payload["phase"], "approval_resolved")

    def test_async_deny_flow(self) -> None:
        gate = ApprovalGate()
        result_holder: list[ApprovalDecision] = []

        thread = threading.Thread(
            target=lambda: result_holder.append(
                gate.request_approval("exec_command", "edge-pc-1", "rm build.log", timeout_sec=5.0)
            )
        )
        thread.start()

        deadline = time.monotonic() + 2.0
        pending = []
        while time.monotonic() < deadline:
            pending = gate.list_pending()
            if pending:
                break
            time.sleep(0.01)

        self.assertEqual(len(pending), 1)
        self.assertTrue(gate.decide(pending[0]["approval_id"], False, actor="reviewer", reason="unsafe"))
        thread.join(timeout=2.0)

        self.assertEqual(len(result_holder), 1)
        self.assertFalse(result_holder[0].approved)
        self.assertEqual(result_holder[0].status, "denied")
        self.assertEqual(result_holder[0].actor, "reviewer")
        self.assertEqual(result_holder[0].reason, "unsafe")

    def test_timeout_auto_denies_and_clears_pending(self) -> None:
        gate = ApprovalGate()
        decision = gate.request_approval(
            action="exec_command",
            target="edge-pc-1",
            detail="python deploy.py",
            timeout_sec=0.05,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.status, "timeout")
        self.assertEqual(decision.actor, "system")
        self.assertEqual(gate.list_pending(), [])
        # Late decide after timeout returns False.
        self.assertFalse(gate.decide(decision.approval_id, True, actor="owner"))

    def test_reentrant_callback_and_observer_error_isolation(self) -> None:
        # 1. Re-entrant decide inside on_event must not deadlock.
        gate_holder: list[ApprovalGate] = []

        def auto_approve(event: Event) -> None:
            if event.payload.get("phase") == "approval_requested":
                gate_holder[0].decide(event.payload["approval_id"], True, actor="auto")

        gate = ApprovalGate(on_event=auto_approve)
        gate_holder.append(gate)
        decision = gate.request_approval("exec", "pc", "pytest", timeout_sec=2.0)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.actor, "auto")

        # 2. Broken observer must not crash request_approval.
        def broken_observer(_event: Event) -> None:
            raise RuntimeError("observer failure")

        fault_gate = ApprovalGate(on_event=broken_observer)
        timeout_decision = fault_gate.request_approval("exec", "pc", "pytest", timeout_sec=0.02)
        self.assertFalse(timeout_decision.approved)
        self.assertEqual(timeout_decision.status, "timeout")

    def test_cancel_all_unblocks_waiting_threads(self) -> None:
        gate = ApprovalGate()
        results: list[ApprovalDecision] = []

        def wait_one(idx: int) -> None:
            results.append(
                gate.request_approval("exec", f"pc-{idx}", f"cmd-{idx}", timeout_sec=10.0)
            )

        threads = [threading.Thread(target=wait_one, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(gate.list_pending()) < 3:
            time.sleep(0.01)

        self.assertEqual(len(gate.list_pending()), 3)
        cancelled = gate.cancel_all(reason="daemon stopping")
        self.assertEqual(cancelled, 3)

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(results), 3)
        for dec in results:
            self.assertFalse(dec.approved)
            self.assertEqual(dec.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
