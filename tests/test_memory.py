# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core.memory (SQLite WAL/FTS5 store & trailing tools)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from shinobu_core.memory import MemoryStore, build_memory_tools
from shinobu_core.protocol.engine import ToolSpec


class TestMemoryStoreAndTools(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "shinobu_memory.sqlite3"
        self.store = MemoryStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_wal_mode_enabled_for_file_db(self) -> None:
        self.assertTrue(self.db_path.exists())
        self.assertEqual(self.store.journal_mode, "wal")

    def test_episode_prefix_immutability_and_closing(self) -> None:
        self.assertEqual(self.store.latest_closed_summary(), "")

        prefix_v1 = "L0 SYSTEM PREFIX :: Shinobu immutable persona v1"
        sha_v1 = hashlib.sha256(prefix_v1.encode("utf-8")).hexdigest()
        ep1 = self.store.create_or_get_active_episode("ep-001", prefix_v1, sha_v1)
        self.assertEqual(ep1["id"], "ep-001")
        self.assertEqual(ep1["prefix_text"], prefix_v1)
        self.assertEqual(ep1["prefix_sha256"], sha_v1)
        self.assertEqual(ep1["status"], "active")

        # Re-calling with a different prefix must NOT overwrite the active episode prefix.
        tampered = "TAMPERED L0 PREFIX"
        tampered_sha = hashlib.sha256(tampered.encode("utf-8")).hexdigest()
        ep1_again = self.store.create_or_get_active_episode("ep-001", tampered, tampered_sha)
        self.assertEqual(ep1_again["prefix_text"], prefix_v1)
        self.assertEqual(ep1_again["prefix_sha256"], sha_v1)

        # Direct SQL UPDATE of prefix_text / prefix_sha256 must be blocked by SQLite trigger.
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute(
                "UPDATE episodes SET prefix_text = ? WHERE id = ?",
                ("HACKED", "ep-001"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute(
                "UPDATE episodes SET prefix_sha256 = ? WHERE id = ?",
                ("deadbeef", "ep-001"),
            )

        closed1 = self.store.close_episode("ep-001", "第一章：完成了咖啡偏好记录。")
        self.assertEqual(closed1["status"], "closed")
        self.assertEqual(closed1["prefix_text"], prefix_v1)
        self.assertEqual(closed1["summary"], "第一章：完成了咖啡偏好记录。")
        self.assertIsNotNone(closed1["closed_at"])
        self.assertEqual(self.store.latest_closed_summary(), "第一章：完成了咖啡偏好记录。")

        # Attempting to reopen a closed episode id as active should fail.
        with self.assertRaises(ValueError):
            self.store.create_or_get_active_episode("ep-001", prefix_v1, sha_v1)

        # Closing a second episode updates latest_closed_summary().
        self.store.create_or_get_active_episode("ep-002", "L0 v2", "sha2")
        self.store.close_episode("ep-002", "第二章：安排了周三提醒。")
        self.assertEqual(self.store.latest_closed_summary(), "第二章：安排了周三提醒。")

    def test_turns_append_and_limit_order(self) -> None:
        self.store.create_or_get_active_episode("ep-a", "prefix-a", "sha-a")
        self.store.create_or_get_active_episode("ep-b", "prefix-b", "sha-b")

        for i in range(1, 6):
            self.store.append_turn("ep-a", "user" if i % 2 else "assistant", f"turn-{i}")
        self.store.append_turn("ep-b", "user", "other-episode-turn")

        all_a = self.store.get_turns("ep-a", limit=50)
        self.assertEqual([t["content"] for t in all_a], ["turn-1", "turn-2", "turn-3", "turn-4", "turn-5"])

        recent_a = self.store.get_turns("ep-a", limit=2)
        self.assertEqual([t["content"] for t in recent_a], ["turn-4", "turn-5"])

        turns_b = self.store.get_turns("ep-b")
        self.assertEqual([t["content"] for t in turns_b], ["other-episode-turn"])

    def test_chinese_fts5_trigram_short_like_fallback_and_special_chars(self) -> None:
        self.store.create_or_get_active_episode("ep-search", "L0", "sha-l0")
        self.store.append_turn("ep-search", "user", "我最喜欢在雨天喝手冲耶加雪菲咖啡。")
        self.store.append_turn("ep-search", "user", "下周三要去东京出差，记得带电源适配器。")
        self.store.append_turn("ep-search", "assistant", "进度已经达到100%_完成了，包含特殊符号(a:b)*测试。")

        self.store.upsert_fact("drink_pref", "抹茶拿铁少冰", confidence=0.95, source_episode="ep-search")
        self.store.upsert_fact("pet_info", "家里有一只橘猫叫阿忍", confidence=0.9, source_episode="ep-search")

        # >= 3 Chinese chars -> FTS5 trigram match on turn
        hits_turn = self.store.search("耶加雪菲")
        self.assertTrue(hits_turn)
        self.assertEqual(hits_turn[0]["source"], "turn")
        self.assertEqual(hits_turn[0]["match_mode"], "fts5")
        self.assertIn("耶加雪菲", hits_turn[0]["content"])

        # >= 3 Chinese chars -> FTS5 trigram match on fact
        hits_fact = self.store.search("抹茶拿铁")
        self.assertTrue(hits_fact)
        self.assertEqual(hits_fact[0]["source"], "fact")
        self.assertEqual(hits_fact[0]["match_mode"], "fts5")
        self.assertEqual(hits_fact[0]["key"], "drink_pref")

        # < 3 chars (2 Chinese chars) -> safe LIKE fallback
        hits_two = self.store.search("出差")
        self.assertTrue(hits_two)
        self.assertEqual(hits_two[0]["match_mode"], "like")
        self.assertIn("东京出差", hits_two[0]["content"])

        # < 3 chars (1 Chinese char) -> safe LIKE fallback
        hits_one = self.store.search("猫")
        self.assertTrue(hits_one)
        self.assertEqual(hits_one[0]["match_mode"], "like")
        self.assertEqual(hits_one[0]["source"], "fact")
        self.assertEqual(hits_one[0]["key"], "pet_info")

        # FTS5 special syntax characters must safely degrade to parameterized LIKE without raising
        hits_special = self.store.search("(a:b)*测试")
        self.assertEqual(len(hits_special), 1)
        self.assertEqual(hits_special[0]["match_mode"], "like")
        self.assertIn("(a:b)*测试", hits_special[0]["content"])

        # Malicious/broken FTS syntax strings must never throw sqlite3.OperationalError
        for weird in ['"unclosed', "OR NOT AND (", "foo:bar* -baz^", "'; DROP TABLE turns; --"]:
            res = self.store.search(weird)
            self.assertIsInstance(res, list)

        # LIKE wildcards '%' and '_' must be escaped literally
        hits_percent = self.store.search("100%_")
        self.assertEqual(len(hits_percent), 1)
        self.assertIn("100%_完成", hits_percent[0]["content"])
        self.assertEqual(self.store.search("%%%"), [])

        # Upsert updates FTS5 index and removes stale terms
        self.store.upsert_fact("drink_pref", "桂花乌龙冷萃", confidence=0.98, source_episode="ep-search")
        self.assertTrue(any(h["key"] == "drink_pref" for h in self.store.search("桂花乌龙") if h["source"] == "fact"))
        self.assertEqual(self.store.search("抹茶拿铁"), [])

    def test_timemap_schedules_add_and_pop_due(self) -> None:
        s1 = self.store.add_schedule("2026-11-21T20:00:00+08:00", "提醒喝水", {"cup": 1})
        s2 = self.store.add_schedule("2026-11-21T22:00:00+08:00", "晚安打卡", {"kind": "sleep"})
        self.assertGreater(s1, 0)
        self.assertGreater(s2, s1)

        # Before any trigger_at -> empty
        self.assertEqual(self.store.pop_due_schedules("2026-11-21T19:59:59+08:00"), [])

        # Exactly at s1 trigger_at -> pops s1 only
        due_1 = self.store.pop_due_schedules("2026-11-21T20:00:00+08:00")
        self.assertEqual(len(due_1), 1)
        self.assertEqual(due_1[0]["id"], s1)
        self.assertEqual(due_1[0]["label"], "提醒喝水")
        self.assertEqual(due_1[0]["payload"], {"cup": 1})
        self.assertTrue(due_1[0]["done"])

        # Popping again at 20:05 returns empty (s1 already marked done)
        self.assertEqual(self.store.pop_due_schedules("2026-11-21T20:05:00+08:00"), [])

        # UTC equivalent of 2026-11-21T22:00:00+08:00 is 2026-11-21T14:00:00Z -> pops s2
        due_2 = self.store.pop_due_schedules("2026-11-21T14:00:00Z")
        self.assertEqual(len(due_2), 1)
        self.assertEqual(due_2[0]["id"], s2)
        self.assertEqual(due_2[0]["label"], "晚安打卡")
        self.assertEqual(due_2[0]["payload"], {"kind": "sleep"})

    def test_build_memory_tools_and_l0_prefix_safety(self) -> None:
        prefix = "L0 IMMUTABLE PREFIX FOR TOOLS TEST"
        sha = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        self.store.create_or_get_active_episode("ep-tools", prefix, sha)
        self.store.append_turn("ep-tools", "user", "我计划周末去京都赏枫。")

        mood_events: list[tuple[str, str]] = []
        tools = build_memory_tools(
            self.store,
            episode_id="ep-tools",
            on_mood=lambda mood, reason: mood_events.append((mood, reason)),
        )
        self.assertEqual(len(tools), 5)
        by_name = {t.name: t for t in tools}
        self.assertEqual(
            set(by_name.keys()),
            {"recall_memory", "note_memory", "set_mood", "schedule_reminder", "list_reminders"},
        )
        for spec in tools:
            self.assertIsInstance(spec, ToolSpec)
            schema = spec.to_schema()
            self.assertEqual(schema["name"], spec.name)
            json.dumps(schema)  # must be JSON-serializable

        # 1) note_memory
        note_raw = by_name["note_memory"].handler(
            key="autumn_plan",
            value="周末想去京都岚山赏红叶",
            confidence=0.92,
        )
        note_out = json.loads(note_raw)
        self.assertEqual(note_out["status"], "saved")
        self.assertEqual(note_out["fact"]["source_episode"], "ep-tools")
        self.assertEqual(note_out["fact"]["key"], "autumn_plan")

        # 2) recall_memory (Chinese trigram & short fallback)
        recall_raw = by_name["recall_memory"].handler(query="京都岚山", limit=5)
        recall_out = json.loads(recall_raw)
        self.assertGreaterEqual(recall_out["count"], 1)
        self.assertEqual(recall_out["results"][0]["source"], "fact")
        self.assertEqual(recall_out["results"][0]["key"], "autumn_plan")

        recall_short = json.loads(by_name["recall_memory"].handler(query="京都"))
        self.assertGreaterEqual(recall_short["count"], 2)

        # 3) set_mood
        mood_raw = by_name["set_mood"].handler(mood="playful", reason="user mentioned autumn trip")
        mood_out = json.loads(mood_raw)
        self.assertEqual(mood_out["status"], "ok")
        self.assertEqual(mood_out["mood"], "playful")
        self.assertEqual(mood_events, [("playful", "user mentioned autumn trip")])

        # 4) schedule_reminder (delay_minutes & explicit ISO trigger_at)
        rem_delay = json.loads(
            by_name["schedule_reminder"].handler(label="喝水休息", delay_minutes=15)
        )
        self.assertEqual(rem_delay["status"], "scheduled")
        self.assertIsInstance(rem_delay["id"], int)
        self.assertTrue(rem_delay["trigger_at"].endswith("Z"))
        self.assertEqual(
            rem_delay["payload"],
            {"message": "喝水休息", "source": "timemap", "created_by": "tool"},
        )

        rem_iso = json.loads(
            by_name["schedule_reminder"].handler(
                label="团队周会",
                trigger_at="2026-12-01T09:00:00Z",
                message="记得带上季度复盘材料",
            )
        )
        self.assertEqual(rem_iso["status"], "scheduled")
        self.assertEqual(rem_iso["trigger_at"], "2026-12-01T09:00:00Z")
        self.assertEqual(
            rem_iso["payload"],
            {"message": "记得带上季度复盘材料", "source": "timemap", "created_by": "tool"},
        )

        # Validation errors on schedule_reminder
        with self.assertRaises(ValueError):
            by_name["schedule_reminder"].handler(label="  ", delay_minutes=5)
        with self.assertRaises(ValueError):
            by_name["schedule_reminder"].handler(label="bad delay", delay_minutes=0)
        with self.assertRaises(ValueError):
            by_name["schedule_reminder"].handler(label="bad iso", trigger_at="not-an-iso")
        with self.assertRaises(ValueError):
            by_name["schedule_reminder"].handler(label="missing time")

        # 5) list_reminders (pending vs include_done)
        list_pending = json.loads(by_name["list_reminders"].handler())
        self.assertEqual(list_pending["count"], 2)
        self.assertFalse(list_pending["include_done"])
        self.assertEqual(
            {r["label"] for r in list_pending["reminders"]},
            {"喝水休息", "团队周会"},
        )

        popped = self.store.pop_due_schedules("2026-12-01T10:00:00Z")
        self.assertGreaterEqual(len(popped), 1)
        list_after_pop = json.loads(by_name["list_reminders"].handler(include_done=False))
        self.assertLess(list_after_pop["count"], 2)
        list_with_done = json.loads(by_name["list_reminders"].handler(include_done=True))
        self.assertEqual(list_with_done["count"], 2)

        # Verify L0 prefix remained completely untouched after all 5 tool calls
        ep_after = self.store.get_episode("ep-tools")
        self.assertIsNotNone(ep_after)
        assert ep_after is not None
        self.assertEqual(ep_after["prefix_text"], prefix)
        self.assertEqual(ep_after["prefix_sha256"], sha)
        self.assertEqual(ep_after["status"], "active")

        # set_mood also works cleanly when on_mood is None
        tools_no_cb = build_memory_tools(self.store, episode_id="ep-tools", on_mood=None)
        set_mood_no_cb = {t.name: t for t in tools_no_cb}["set_mood"]
        res_no_cb = json.loads(set_mood_no_cb.handler(mood="calm"))
        self.assertEqual(res_no_cb["mood"], "calm")


class TestMemoryCompaction(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.store = MemoryStore(self.home / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def test_compact_memory_dry_run_and_apply_with_backup_and_journal_safety(self) -> None:
        from shinobu_core.memory.consolidator import CompactionResult, compact_memory

        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        memory_path = soul_dir / "MEMORY.md"
        original_memory = (
            "# Long-Term Memory\n\n"
            "- favorite_drink: 冰美式\n"
            "- favorite_drink: 冰美式少糖（最新）\n"
            "- temp_task: 昨天记得买牛奶\n"
        )
        memory_path.write_text(original_memory, encoding="utf-8", newline="\n")

        journals_dir = self.home / "journals"
        journals_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journals_dir / "2026-11-22.md"
        original_journal = "# Journal 2026-11-22\n\n原始日记绝对不可修改。\n"
        journal_path.write_text(original_journal, encoding="utf-8", newline="\n")

        f_old = self.store.upsert_fact("old_temp_note", "过期的临时备忘标记", confidence=0.5)
        f_drink = self.store.upsert_fact("favorite_drink", "冰美式", confidence=0.7)
        f_keep = self.store.upsert_fact("hobby", "摄影", confidence=0.95)
        old_id = int(f_old["id"])

        compacted_md = "# Long-Term Memory\n\n- favorite_drink: 冰美式少糖\n- hobby: 摄影"

        class _CompactStubEngine:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
                self.calls.append((system_prompt, user_prompt))
                return {
                    "summary": "合并了重复饮品偏好并清理了 1 条过期备忘",
                    "compacted_memory_md": compacted_md,
                    "upsert_facts": [
                        {"key": "favorite_drink", "value": "冰美式少糖", "confidence": 0.96},
                    ],
                    "remove_fact_ids": [old_id, True, "bad", -3, 999999, old_id],
                }

        engine = _CompactStubEngine()

        # 1) dry_run=True -> zero disk / DB writes
        preview = compact_memory(self.home, self.store, engine, dry_run=True)
        self.assertIsInstance(preview, CompactionResult)
        self.assertFalse(preview.applied)
        self.assertTrue(preview.dry_run)
        self.assertIsNone(preview.backup_path)
        self.assertEqual(preview.before_chars, len(original_memory))
        self.assertEqual(preview.after_chars, len(compacted_md + "\n"))
        self.assertEqual(preview.removed_fact_ids, [old_id])
        self.assertEqual(len(preview.updated_facts), 1)
        self.assertEqual(preview.preview_memory_md, compacted_md + "\n")
        self.assertEqual(memory_path.read_text(encoding="utf-8"), original_memory)
        self.assertFalse((self.home / "_versions").exists())
        self.assertEqual(len(self.store.list_facts(limit=50)), 3)
        self.assertEqual(journal_path.read_text(encoding="utf-8"), original_journal)

        # 2) dry_run=False -> backup first, atomic write, apply facts, never touch journals
        applied = compact_memory(self.home, self.store, engine, dry_run=False)
        self.assertTrue(applied.applied)
        self.assertFalse(applied.dry_run)
        self.assertIsNotNone(applied.backup_path)
        assert applied.backup_path is not None
        self.assertEqual(applied.backup_path.parent, self.home / "_versions")
        self.assertEqual(applied.backup_path.name, "MEMORY_v1.md")
        self.assertEqual(applied.backup_path.read_text(encoding="utf-8"), original_memory)
        self.assertEqual(memory_path.read_text(encoding="utf-8"), compacted_md + "\n")
        self.assertEqual(applied.removed_fact_ids, [old_id])
        self.assertEqual(self.store.search("过期的临时备忘标记", limit=5), [])

        facts_after = {f["key"]: f for f in self.store.list_facts(limit=50)}
        self.assertNotIn("old_temp_note", facts_after)
        self.assertEqual(facts_after["favorite_drink"]["value"], "冰美式少糖")
        self.assertEqual(facts_after["hobby"]["id"], f_keep["id"])
        self.assertEqual(f_drink["key"], "favorite_drink")
        self.assertEqual(journal_path.read_text(encoding="utf-8"), original_journal)

    def test_compact_memory_empty_markdown_preserves_original_file(self) -> None:
        from shinobu_core.memory.consolidator import compact_memory

        soul_dir = self.home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        memory_path = soul_dir / "MEMORY.md"
        original_memory = "# Memory\n\n- keep_me: 完整保留\n"
        memory_path.write_text(original_memory, encoding="utf-8", newline="\n")

        class _EmptyStubEngine:
            def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
                return {
                    "summary": "",
                    "compacted_memory_md": "   \n  ",
                    "upsert_facts": [],
                    "remove_fact_ids": ["not-an-int", False],
                }

        res = compact_memory(self.home, self.store, _EmptyStubEngine(), dry_run=False)
        self.assertFalse(res.applied)
        self.assertIsNone(res.backup_path)
        self.assertEqual(res.before_chars, len(original_memory))
        self.assertEqual(res.after_chars, len(original_memory))
        self.assertEqual(res.preview_memory_md, original_memory)
        self.assertEqual(memory_path.read_text(encoding="utf-8"), original_memory)
        self.assertFalse((self.home / "_versions").exists())


if __name__ == "__main__":
    unittest.main()
