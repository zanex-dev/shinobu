# -*- coding: utf-8 -*-
"""Unit tests for Shinobu Orchestrator and sleep-time Consolidator."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from shinobu_core.core.kernel import ShinobuKernel
from shinobu_core.engines.mock_engine import MockEngine
from shinobu_core.memory.consolidator import ConsolidationResult, consolidate_episode
from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.engine import ToolSpec, TurnRequest
from shinobu_core.runtime.orchestrator import Orchestrator
from shinobu_core.soul.prefix_freezer import verify_frozen


class TestOrchestratorAndConsolidator(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "shinobu_home"
        self.store = MemoryStore(self.home / "memory.db")
        self.kernel = ShinobuKernel()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_mid_episode_soul_edit_preserves_frozen_prefix_hash(self) -> None:
        engine = MockEngine(scripted_replies=["First hello!", "Second hello!"])
        orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine,
            kernel=self.kernel,
        )

        events1 = list(orch.send_message("Hi Shinobu"))
        ep1 = orch.active_prefix
        self.assertIsNotNone(ep1)
        assert ep1 is not None
        self.assertTrue(verify_frozen(ep1))

        # Mutate SOUL.md and MEMORY.md on disk while the episode is still active.
        soul_path = self.home / "soul" / "SOUL.md"
        memory_path = self.home / "soul" / "MEMORY.md"
        soul_path.write_text("# Mutated Soul\nMID_EPISODE_MUTATION_MARKER\n", encoding="utf-8")
        memory_path.write_text("# Mutated Memory\nMID_EPISODE_MEMORY_MARKER\n", encoding="utf-8")

        events2 = list(orch.send_message("Still in same episode"))
        ep2 = orch.active_prefix
        assert ep2 is not None

        self.assertEqual(ep1.episode_id, ep2.episode_id)
        self.assertEqual(ep1.sha256, ep2.sha256)
        self.assertEqual(len(engine.requests), 2)
        self.assertEqual(engine.requests[0].system_prefix, engine.requests[1].system_prefix)
        self.assertNotIn("MID_EPISODE_MUTATION_MARKER", engine.requests[1].system_prefix)
        self.assertNotIn("MID_EPISODE_MEMORY_MARKER", engine.requests[1].system_prefix)

        done1 = [e for e in events1 if e.kind == "done"][-1]
        done2 = [e for e in events2 if e.kind == "done"][-1]
        self.assertEqual(done1.payload["prefix_sha256"], done2.payload["prefix_sha256"])

        # A fresh Orchestrator resuming the same active episode must also keep DB prefix.
        orch_resumed = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine,
            kernel=self.kernel,
        )
        resumed_prefix = orch_resumed.ensure_episode(ep1.episode_id)
        self.assertEqual(resumed_prefix.sha256, ep1.sha256)
        self.assertEqual(resumed_prefix.text, ep1.text)

    def test_trailing_tool_execution_mood_and_hook_bus(self) -> None:
        hook_events: list[str] = []
        self.kernel.hooks.on("tool_call", lambda ev: hook_events.append(f"tool:{ev.payload['name']}"))
        self.kernel.hooks.on("mood_changed", lambda ev: hook_events.append(f"mood:{ev.payload['mood']}"))
        self.kernel.hooks.on("done", lambda ev: hook_events.append("done"))

        # Register an extra plugin tool on the kernel to verify merge with memory tools.
        self.kernel.register_tool(
            ToolSpec(
                name="ping_plugin",
                description="Simple ping plugin tool.",
                parameters={"type": "object", "properties": {"tag": {"type": "string"}}},
                handler=lambda tag="ok": json.dumps({"pong": tag}),
            )
        )

        def _trigger(req: TurnRequest):
            if req.history and req.history[-1].get("role") == "tool":
                return None
            return [
                {
                    "id": "call_note_1",
                    "name": "note_memory",
                    "arguments": {"key": "favorite_drink", "value": "oolong tea", "confidence": 0.95},
                },
                {
                    "id": "call_mood_1",
                    "name": "set_mood",
                    "arguments": {"mood": "cheerful", "reason": "shared tea preference"},
                },
                {
                    "id": "call_ping_1",
                    "name": "ping_plugin",
                    "arguments": {"tag": "tea"},
                },
            ]

        engine = MockEngine(
            scripted_replies=["Noted! I will remember you like oolong tea."],
            tool_trigger=_trigger,
        )
        orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine,
            kernel=self.kernel,
        )

        events = list(orch.send_message("I love drinking oolong tea."))
        kinds = [e.kind for e in events]

        self.assertIn("state_changed", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("mood_changed", kinds)
        self.assertIn("token_delta", kinds)
        self.assertEqual(kinds[-1], "done")

        # Engine called twice (initial + trailing tool follow-up) with identical frozen prefix.
        self.assertEqual(len(engine.requests), 2)
        self.assertEqual(engine.requests[0].system_prefix, engine.requests[1].system_prefix)
        self.assertEqual(engine.requests[1].history[-1]["role"], "tool")

        # Same-turn assistant tool_calls ids must match trailing role="tool" tool_call_id entries.
        hist_r2 = engine.requests[1].history
        asst_tc_msgs = [m for m in hist_r2 if m.get("role") == "assistant" and m.get("tool_calls")]
        tool_msgs = [m for m in hist_r2 if m.get("role") == "tool"]
        self.assertEqual(len(asst_tc_msgs), 1)
        declared_ids = [tc.get("id") for tc in asst_tc_msgs[0]["tool_calls"]]
        self.assertTrue(all(isinstance(cid, str) and cid for cid in declared_ids))
        self.assertEqual([m.get("tool_call_id") for m in tool_msgs], declared_ids)

        # Verify SQLite persisted append-only role="tool" turns and fact.
        ep_id = orch.active_episode_id
        assert ep_id is not None
        turns = self.store.get_turns(ep_id, limit=20)
        tool_turns = [t for t in turns if t["role"] == "tool"]
        self.assertEqual(len(tool_turns), 3)

        hits = self.store.search("oolong tea", limit=5)
        self.assertTrue(any(h.get("value") == "oolong tea" for h in hits))
        self.assertEqual(orch.mood, "cheerful")

        # Verify HookBus received events.
        self.assertIn("tool:note_memory", hook_events)
        self.assertIn("tool:set_mood", hook_events)
        self.assertIn("tool:ping_plugin", hook_events)
        self.assertIn("mood:cheerful", hook_events)
        self.assertIn("done", hook_events)

        # Next user turn must NOT inject persisted SQLite role="tool" audit rows into initial history.
        before_count = len(engine.requests)
        next_events = list(orch.send_message("What tea did I mention?"))
        self.assertEqual(next_events[-1].kind, "done")
        self.assertGreater(len(engine.requests), before_count)
        next_initial_history = engine.requests[before_count].history
        self.assertFalse(any(m.get("role") == "tool" for m in next_initial_history))

    def test_tool_loop_respects_four_round_cap(self) -> None:
        call_counter = {"n": 0}

        def _always_call_tool(req: TurnRequest):
            call_counter["n"] += 1
            return {
                "id": f"call_loop_{call_counter['n']}",
                "name": "recall_memory",
                "arguments": {"query": "loop"},
            }

        engine = MockEngine(
            scripted_replies=["fallback reply"],
            tool_trigger=_always_call_tool,
        )
        orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine,
            kernel=self.kernel,
        )

        events = list(orch.send_message("Keep recalling"))
        done = [e for e in events if e.kind == "done"][-1]
        self.assertEqual(done.payload["tool_rounds"], 4)
        # 1 initial turn + 4 trailing tool follow-up turns = 5 requests max.
        self.assertEqual(len(engine.requests), 5)

    def test_sleep_consolidate_persists_journal_versions_and_loads_yesterday_next_episode(self) -> None:
        chat_engine = MockEngine(
            scripted_replies=[
                "Goodnight! Rest well.",
                "Good morning! Ready for our study plan?",
                "Second night wrap-up.",
            ]
        )
        consolidator_engine = MockEngine(
            json_replies=[
                {
                    "summary": "Discussed Rust async study plan and decided to start tomorrow morning.",
                    "new_facts": [
                        {"key": "study_topic", "value": "Rust async runtime", "confidence": 0.92}
                    ],
                    "open_loops": [
                        {
                            "label": "Review Tokio tutorial chapter 1",
                            "trigger_at": "2026-11-22T09:00:00+00:00",
                            "payload": {"priority": "high"},
                        }
                    ],
                    "memory_entries": ["User is learning Rust async runtime."],
                },
                {
                    "summary": "Completed Tokio chapter 1 walkthrough smoothly.",
                    "new_facts": [
                        {"key": "tokio_progress", "value": "chapter 1 done", "confidence": 0.9}
                    ],
                    "open_loops": [],
                    "memory_entries": ["Finished Tokio chapter 1."],
                },
            ]
        )

        orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=chat_engine,
            kernel=self.kernel,
            consolidator_engine=consolidator_engine,
        )

        # Episode 1
        list(orch.send_message("I want to study Rust async tomorrow, goodnight!"))
        ep1 = orch.active_prefix
        assert ep1 is not None
        self.assertNotIn("## YESTERDAY", ep1.text)

        memory_path = self.home / "soul" / "MEMORY.md"
        original_memory_text = memory_path.read_text(encoding="utf-8")

        res1 = orch.sleep_and_consolidate(date_str="2026-11-21")
        self.assertIsInstance(res1, ConsolidationResult)
        self.assertEqual(res1.episode_id, ep1.episode_id)
        self.assertIn("Rust async study plan", res1.summary)
        self.assertEqual(len(res1.new_facts), 1)
        self.assertEqual(len(res1.open_loops), 1)
        self.assertTrue(res1.journal_path.exists())
        self.assertEqual(res1.journal_path, self.home / "journals" / "2026-11-21.md")

        # Verify journal content
        journal_text_1 = res1.journal_path.read_text(encoding="utf-8")
        self.assertIn(ep1.episode_id, journal_text_1)
        self.assertIn("Rust async study plan", journal_text_1)
        self.assertIn("Review Tokio tutorial chapter 1", journal_text_1)

        # Verify MEMORY.md versioned backup (_versions/MEMORY_v1.md) & non-destructive update
        v1_backup = self.home / "_versions" / "MEMORY_v1.md"
        self.assertTrue(v1_backup.exists())
        self.assertEqual(v1_backup.read_text(encoding="utf-8"), original_memory_text)
        updated_memory_1 = memory_path.read_text(encoding="utf-8")
        self.assertIn("User is learning Rust async runtime.", updated_memory_1)

        # Verify SQLite facts, timemap, and closed episode summary
        self.assertEqual(
            self.store.latest_closed_summary(),
            "Discussed Rust async study plan and decided to start tomorrow morning.",
        )
        due = self.store.pop_due_schedules("2026-11-22T10:00:00+00:00")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["label"], "Review Tokio tutorial chapter 1")
        fact_hits = self.store.search("Rust async runtime", limit=5)
        self.assertTrue(any(h.get("key") == "study_topic" for h in fact_hits))

        # Active episode cleared; next turn must open Episode 2 with YESTERDAY summary loaded
        self.assertIsNone(orch.active_episode_id)
        list(orch.send_message("Morning! Let's start."))
        ep2 = orch.active_prefix
        assert ep2 is not None
        self.assertNotEqual(ep2.episode_id, ep1.episode_id)
        self.assertNotEqual(ep2.sha256, ep1.sha256)
        self.assertIn("## YESTERDAY", ep2.text)
        self.assertIn("Discussed Rust async study plan", ep2.text)
        self.assertIn("User is learning Rust async runtime.", ep2.text)
        self.assertEqual(chat_engine.requests[-1].system_prefix, ep2.text)

        # Episode 2 consolidation must APPEND to same-day journal and create MEMORY_v2.md
        res2 = orch.sleep_and_consolidate(date_str="2026-11-21")
        v2_backup = self.home / "_versions" / "MEMORY_v2.md"
        self.assertTrue(v1_backup.exists())
        self.assertTrue(v2_backup.exists())
        self.assertEqual(v2_backup.read_text(encoding="utf-8"), updated_memory_1)

        journal_text_2 = res2.journal_path.read_text(encoding="utf-8")
        self.assertIn(ep1.episode_id, journal_text_2)
        self.assertIn(ep2.episode_id, journal_text_2)
        self.assertIn("Completed Tokio chapter 1 walkthrough smoothly.", journal_text_2)

    def test_consolidate_episode_rejects_unknown_episode(self) -> None:
        engine = MockEngine(json_replies=[{"summary": "unused"}])
        with self.assertRaises(KeyError):
            consolidate_episode(self.home, self.store, engine, "ep-does-not-exist")


class TestHeartbeatAutoSleep(unittest.TestCase):
    def test_auto_sleep_disabled_by_default_zero_token(self) -> None:
        import time
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        calls = {"count": 0}
        hb = HeartbeatLoop(
            store=None,
            state_machine=sm,
            on_auto_sleep=lambda: calls.__setitem__("count", calls["count"] + 1),
        )
        self.assertEqual(hb.auto_sleep_after_sec, 0.0)
        time.sleep(0.05)
        hb.tick_once()
        self.assertEqual(calls["count"], 0)

    def test_auto_sleep_triggers_once_per_idle_cycle_and_resets_after_activity(self) -> None:
        import time
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        calls = {"count": 0}
        hb = HeartbeatLoop(
            store=None,
            state_machine=sm,
            donut_after_sec=10.0,
            sleepy_after_sec=20.0,
            auto_sleep_after_sec=0.06,
            on_auto_sleep=lambda: calls.__setitem__("count", calls["count"] + 1),
        )

        # Below threshold: does not trigger
        hb.tick_once()
        self.assertEqual(calls["count"], 0)

        # Cross threshold: triggers exactly once despite repeated ticks
        time.sleep(0.08)
        hb.tick_once()
        hb.tick_once()
        hb.tick_once()
        self.assertEqual(calls["count"], 1)

        # New activity resets idle timer; next tick resets debounce flag
        sm.transition("idle", reason="user_chat", touch_activity=True)
        hb.tick_once()
        self.assertEqual(calls["count"], 1)

        # Cross threshold again in the new idle cycle: triggers once more
        time.sleep(0.08)
        hb.tick_once()
        hb.tick_once()
        self.assertEqual(calls["count"], 2)

    def test_auto_sleep_fault_isolated(self) -> None:
        import time
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        calls = {"count": 0}

        def _failing_sleep() -> None:
            calls["count"] += 1
            raise RuntimeError("auto sleep failure")

        hb = HeartbeatLoop(
            store=None,
            state_machine=sm,
            auto_sleep_after_sec=0.04,
            on_auto_sleep=_failing_sleep,
        )
        time.sleep(0.06)
        hb.tick_once()
        hb.tick_once()
        self.assertEqual(calls["count"], 1)


class TestEpisodeRecoveryAndFastReset(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "shinobu_home"
        self.store = MemoryStore(self.home / "memory.db")
        self.kernel = ShinobuKernel()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_sleep_and_consolidate_recovers_active_episode_after_restart(self) -> None:
        engine1 = MockEngine(scripted_replies=["Hello before restart"])
        orch1 = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine1,
            kernel=self.kernel,
        )
        list(orch1.send_message("Hi there"))
        assert orch1.active_prefix is not None
        ep1_id = orch1.active_prefix.episode_id

        # Simulate daemon restart with a fresh Orchestrator (in-memory _active_prefix is None).
        consolidator_reply = json.dumps(
            {
                "journal": "Short test journal.",
                "summary": "Recovered episode summary",
                "facts": [],
                "timemap": [],
            }
        )
        engine2 = MockEngine(scripted_replies=[consolidator_reply])
        orch2 = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine2,
            kernel=self.kernel,
        )
        self.assertIsNone(orch2.active_prefix)

        result = orch2.sleep_and_consolidate()
        self.assertEqual(result.episode_id, ep1_id)
        self.assertIsNone(self.store.get_active_episode())

        # Calling again when no active episode exists still raises RuntimeError.
        with self.assertRaisesRegex(RuntimeError, "No active episode to consolidate"):
            orch2.sleep_and_consolidate()

    def test_archive_and_reset_fast_closes_without_llm_and_recovers_after_restart(self) -> None:
        engine1 = MockEngine(scripted_replies=["First reply", "Second reply"])
        orch1 = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine1,
            kernel=self.kernel,
        )

        # Idle when no active episode exists.
        idle_res = orch1.archive_and_reset()
        self.assertEqual(
            idle_res,
            {"status": "idle", "closed_episode_id": None, "turn_count": 0},
        )

        # Send a turn, verify store active episode + turn count.
        list(orch1.send_message("Save this history"))
        assert orch1.active_prefix is not None
        ep1_id = orch1.active_prefix.episode_id
        active_row = self.store.get_active_episode()
        self.assertIsNotNone(active_row)
        assert active_row is not None
        self.assertEqual(active_row["id"], ep1_id)
        self.assertEqual(self.store.count_turns(ep1_id), 2)

        # Simulate restart and fast-archive without calling LLM.
        engine_unused = MockEngine(scripted_replies=[])
        orch2 = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine_unused,
            kernel=self.kernel,
        )
        archived_res = orch2.archive_and_reset(summary="  自定义快速归档  ")
        self.assertEqual(
            archived_res,
            {"status": "archived", "closed_episode_id": ep1_id, "turn_count": 2},
        )
        self.assertEqual(len(engine_unused.requests), 0)
        self.assertIsNone(self.store.get_active_episode())
        self.assertEqual(len(self.store.get_turns(ep1_id)), 2)
        self.assertEqual(self.store.latest_closed_summary(), "自定义快速归档")

        # Next turn opens a fresh episode, and default summary falls back to (手动快速归档).
        orch1._active_prefix = None
        list(orch1.send_message("Fresh start"))
        assert orch1.active_prefix is not None
        ep2_id = orch1.active_prefix.episode_id
        self.assertNotEqual(ep2_id, ep1_id)

        default_res = orch1.archive_and_reset()
        self.assertEqual(default_res["status"], "archived")
        self.assertEqual(default_res["closed_episode_id"], ep2_id)
        self.assertEqual(default_res["turn_count"], 2)
        self.assertEqual(self.store.latest_closed_summary(), "(手动快速归档)")


class TestZeroCacheBreakAutoRecall(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "shinobu_home"
        self.store = MemoryStore(self.home / "memory.db")
        self.kernel = ShinobuKernel()

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_extract_recall_queries_filters_smalltalk_and_extracts_keywords(self) -> None:
        from shinobu_core.runtime.orchestrator import _extract_recall_queries

        for msg in ("", "   ", "嗯", "好的！", "hi", "Hello!", "谢谢", "在吗？"):
            self.assertEqual(_extract_recall_queries(msg), [], f"should skip smalltalk: {msg!r}")

        zh_q = _extract_recall_queries("你还记得我之前说过喜欢喝乌龙茶吗？")
        self.assertIn("乌龙茶", zh_q)
        self.assertLessEqual(len(zh_q), 3)

        mix_q = _extract_recall_queries("上次聊的 Rust async runtime 进度如何？")
        self.assertTrue(any(q.lower() == "rust" for q in mix_q))
        self.assertLessEqual(len(mix_q), 3)

    def test_auto_recall_preserves_prefix_hash_keeps_sqlite_raw_and_skips_active_turns(self) -> None:
        # Seed a closed historical episode with both a fact (via note_memory) and a past turn.
        def _seed_fact(req: TurnRequest):
            if req.history and req.history[-1].get("role") == "tool":
                return None
            return {
                "id": "call_seed_tea",
                "name": "note_memory",
                "arguments": {"key": "favorite_tea", "value": "凤凰单丛乌龙茶", "confidence": 0.95},
            }

        seed_engine = MockEngine(scripted_replies=["记住啦！"], tool_trigger=_seed_fact)
        seed_orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=seed_engine,
            kernel=self.kernel,
        )
        list(seed_orch.send_message("我最喜欢在下午泡一壶凤凰单丛乌龙茶。"))
        seed_orch.archive_and_reset(summary="聊到了下午喜欢喝凤凰单丛乌龙茶")

        engine = MockEngine(scripted_replies=["好的", "当然记得凤凰单丛乌龙茶！", "明白", "收到"])
        orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=engine,
            kernel=self.kernel,
        )

        # Turn 1: short smalltalk -> establishes active episode & frozen prefix, no recall injected.
        events1 = list(orch.send_message("hi"))
        ep_prefix = orch.active_prefix
        assert ep_prefix is not None
        sha_before = ep_prefix.sha256
        prefix_before = ep_prefix.text
        self.assertEqual(engine.requests[0].user_text, "hi")
        done1 = [e for e in events1 if e.kind == "done"][-1]
        self.assertEqual(done1.payload["prefix_sha256"], sha_before)

        # Turn 2: relevant query -> receives recall block while frozen prefix SHA-256 & SQLite stay clean.
        raw_query = "你还记得我之前喜欢喝的乌龙茶吗？"
        events2 = list(orch.send_message(raw_query))
        ep_prefix_after = orch.active_prefix
        assert ep_prefix_after is not None
        self.assertEqual(ep_prefix_after.sha256, sha_before)
        self.assertEqual(engine.requests[1].system_prefix, prefix_before)
        done2 = [e for e in events2 if e.kind == "done"][-1]
        self.assertEqual(done2.payload["prefix_sha256"], sha_before)

        sent_user_text = engine.requests[1].user_text
        self.assertTrue(sent_user_text.startswith(raw_query))
        self.assertIn("[Auto Memory Recall (read-only hints)]", sent_user_text)
        self.assertIn("凤凰单丛乌龙茶", sent_user_text)

        # SQLite must store ONLY raw user text, never recall hints.
        active_turns = self.store.get_turns(ep_prefix.episode_id, limit=20)
        user_turns = [t["content"] for t in active_turns if t["role"] == "user"]
        self.assertIn(raw_query, user_turns)
        self.assertTrue(all("[Auto Memory Recall" not in content for content in user_turns))

        # Turn 3 & 4: active episode's own turns must NOT be echoed back via auto recall.
        list(orch.send_message("本轮专属暗号ALPHA999"))
        list(orch.send_message("再提一下本轮专属暗号ALPHA999"))
        self.assertNotIn("[Auto Memory Recall", engine.requests[3].user_text)
        self.assertEqual(engine.requests[3].user_text, "再提一下本轮专属暗号ALPHA999")

    def test_auto_recall_respects_disable_flag_max_chars_tool_loop_and_error_isolation(self) -> None:
        from shinobu_core.runtime.orchestrator import _build_auto_recall_block

        def _seed_facts(req: TurnRequest):
            if req.history and req.history[-1].get("role") == "tool":
                return None
            return [
                {
                    "id": "call_seed_1",
                    "name": "note_memory",
                    "arguments": {
                        "key": "project_codename",
                        "value": "NebulaHyperion" + ("X" * 260),
                        "confidence": 0.99,
                    },
                },
                {
                    "id": "call_seed_2",
                    "name": "note_memory",
                    "arguments": {
                        "key": "project_owner",
                        "value": "NebulaHyperion lead architect",
                        "confidence": 0.92,
                    },
                },
            ]

        seed_engine = MockEngine(scripted_replies=["Noted!"], tool_trigger=_seed_facts)
        seed_orch = Orchestrator(
            home=self.home,
            store=self.store,
            engine=seed_engine,
            kernel=self.kernel,
        )
        list(seed_orch.send_message("Remember NebulaHyperion project details."))
        seed_orch.archive_and_reset(summary="NebulaHyperion kickoff")

        # 1) auto_recall_limit=0 completely disables recall.
        eng_off = MockEngine(scripted_replies=["ok"])
        orch_off = Orchestrator(
            home=self.home,
            store=self.store,
            engine=eng_off,
            kernel=self.kernel,
            auto_recall_limit=0,
        )
        list(orch_off.send_message("Check NebulaHyperion status"))
        self.assertEqual(eng_off.requests[0].user_text, "Check NebulaHyperion status")

        # 2) auto_recall_max_chars truncates recall block strictly within budget.
        block = _build_auto_recall_block(
            self.store,
            "Check NebulaHyperion status",
            active_episode_id="ep-current",
            limit=4,
            max_chars=120,
        )
        self.assertTrue(block.startswith("\n\n[Auto Memory Recall (read-only hints)]\n- "))
        self.assertLessEqual(len(block), 120)

        # 3) Tool loop keeps working_history user message clean (without recall block).
        def _trigger_once(req: TurnRequest):
            if req.history and req.history[-1].get("role") == "tool":
                return None
            return {
                "id": "call_recall_check",
                "name": "recall_memory",
                "arguments": {"query": "NebulaHyperion"},
            }

        eng_tool = MockEngine(scripted_replies=["Done checking."], tool_trigger=_trigger_once)
        orch_tool = Orchestrator(
            home=self.home,
            store=self.store,
            engine=eng_tool,
            kernel=self.kernel,
            auto_recall_limit=2,
            auto_recall_max_chars=160,
        )
        raw_msg = "Inspect NebulaHyperion details"
        list(orch_tool.send_message(raw_msg))
        self.assertEqual(len(eng_tool.requests), 2)
        self.assertIn("[Auto Memory Recall (read-only hints)]", eng_tool.requests[0].user_text)
        self.assertEqual(eng_tool.requests[1].user_text, "")
        hist_users = [m["content"] for m in eng_tool.requests[1].history if m.get("role") == "user"]
        self.assertIn(raw_msg, hist_users)
        self.assertTrue(all("[Auto Memory Recall" not in c for c in hist_users))

        # 4) Store error fails silently without raising.
        class _BrokenStore:
            def search(self, query: str, limit: int = 6):
                raise RuntimeError("fts5 broken")

        self.assertEqual(
            _build_auto_recall_block(
                _BrokenStore(),  # type: ignore[arg-type]
                "Inspect NebulaHyperion details",
                active_episode_id="ep-current",
                limit=4,
                max_chars=480,
            ),
            "",
        )


class TestHeartbeatDailyBriefing(unittest.TestCase):
    def test_daily_briefing_disabled_by_default_zero_overhead(self) -> None:
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        builder_calls = {"count": 0}
        hb = HeartbeatLoop(
            store=None,
            state_machine=sm,
            briefing_builder=lambda: (
                builder_calls.__setitem__("count", builder_calls["count"] + 1) or "should not run"
            ),
        )
        self.assertIsNone(hb.daily_briefing_hour)
        events = hb.tick_once("2026-11-23T01:05:00Z")
        self.assertFalse(
            any(e.kind == "bubble" and e.payload.get("source") == "briefing" for e in events)
        )
        self.assertEqual(builder_calls["count"], 0)

    def test_daily_briefing_triggers_once_per_day_at_target_local_hour(self) -> None:
        from shinobu_core.protocol.events import EventKind
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        published: list[object] = []
        calls = {"count": 0}

        def _build() -> str:
            calls["count"] += 1
            return f"早安简报 #{calls['count']}"

        hb = HeartbeatLoop(
            store=None,
            state_machine=sm,
            publish=published.append,
            daily_briefing_hour=9,
            tz_offset_hours=8.0,
            briefing_builder=_build,
        )

        # 08:30+08:00 -> not target hour
        ev_early = hb.tick_once("2026-11-23T00:30:00Z")
        self.assertEqual(calls["count"], 0)
        self.assertFalse(any(e.payload.get("source") == "briefing" for e in ev_early))

        # 09:05+08:00 -> triggers exactly once
        ev_hit = hb.tick_once("2026-11-23T01:05:00Z")
        briefings = [
            e for e in ev_hit if e.kind == EventKind.BUBBLE and e.payload.get("source") == "briefing"
        ]
        self.assertEqual(len(briefings), 1)
        self.assertEqual(briefings[0].payload["label"], "daily_briefing")
        self.assertEqual(briefings[0].payload["text"], "早安简报 #1")
        self.assertEqual(calls["count"], 1)
        self.assertIn(briefings[0], published)

        # 09:40+08:00 same day -> deduplicated
        ev_dup = hb.tick_once("2026-11-23T01:40:00Z")
        self.assertFalse(any(e.payload.get("source") == "briefing" for e in ev_dup))
        self.assertEqual(calls["count"], 1)

        # Next day 09:01+08:00 -> triggers once for the new day
        ev_next_day = hb.tick_once("2026-11-24T01:01:00Z")
        briefings_next = [
            e
            for e in ev_next_day
            if e.kind == EventKind.BUBBLE and e.payload.get("source") == "briefing"
        ]
        self.assertEqual(len(briefings_next), 1)
        self.assertEqual(briefings_next[0].payload["text"], "早安简报 #2")
        self.assertEqual(calls["count"], 2)

    def test_daily_briefing_validation_none_and_fault_isolation(self) -> None:
        from shinobu_core.runtime.heartbeat import HeartbeatLoop
        from shinobu_core.runtime.state_machine import PetStateMachine

        sm = PetStateMachine()
        with self.assertRaises(ValueError):
            HeartbeatLoop(store=None, state_machine=sm, daily_briefing_hour=24)
        with self.assertRaises(ValueError):
            HeartbeatLoop(store=None, state_machine=sm, daily_briefing_hour=-1)

        # Builder returning None emits no briefing event
        hb_none = HeartbeatLoop(
            store=None,
            state_machine=sm,
            daily_briefing_hour=8,
            tz_offset_hours=8.0,
            briefing_builder=lambda: None,
        )
        ev_none = hb_none.tick_once("2026-11-23T00:10:00Z")
        self.assertFalse(any(e.payload.get("source") == "briefing" for e in ev_none))

        # Fault-isolated builder failure does not crash tick or repeat on same day
        err_calls = {"count": 0}

        def _boom() -> str:
            err_calls["count"] += 1
            raise RuntimeError("briefing builder exploded")

        hb_err = HeartbeatLoop(
            store=None,
            state_machine=sm,
            daily_briefing_hour=8,
            tz_offset_hours=8.0,
            briefing_builder=_boom,
        )
        hb_err.tick_once("2026-11-23T00:10:00Z")
        hb_err.tick_once("2026-11-23T00:20:00Z")
        self.assertEqual(err_calls["count"], 1)

        # Default zero-token builder (briefing_builder=None) emits a valid briefing
        hb_default = HeartbeatLoop(
            store=None,
            state_machine=sm,
            daily_briefing_hour=8,
            tz_offset_hours=8.0,
        )
        ev_def = hb_default.tick_once("2026-11-23T00:15:00Z")
        def_briefings = [e for e in ev_def if e.payload.get("source") == "briefing"]
        self.assertEqual(len(def_briefings), 1)
        self.assertEqual(def_briefings[0].payload["label"], "daily_briefing")
        self.assertIn("2026-11-23", def_briefings[0].payload["text"])


if __name__ == "__main__":
    unittest.main()
