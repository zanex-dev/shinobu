# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core.soul (loader & L0 prefix freezer)."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from shinobu_core.soul import (
    DEFAULT_SOUL_TEMPLATES,
    SOUL_FILENAMES,
    FrozenPrefix,
    PrefixFreezer,
    SoulBundle,
    ensure_soul_files,
    freeze_prefix,
    load_soul,
    verify_frozen,
)


class TestSoulLoader(unittest.TestCase):
    def test_ensure_soul_files_creates_whitehat_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            soul_dir = ensure_soul_files(home)
            self.assertEqual(soul_dir, home / "soul")
            self.assertTrue(soul_dir.is_dir())

            for filename in SOUL_FILENAMES:
                path = soul_dir / filename
                self.assertTrue(path.is_file(), f"missing {filename}")
                content = path.read_text(encoding="utf-8")
                self.assertTrue(content.strip())
                # Default templates must remain neutral white-hat placeholders.
                self.assertNotIn("C:\\Users", content)
                self.assertNotIn("AppData", content)
                self.assertEqual(content, DEFAULT_SOUL_TEMPLATES[filename])

    def test_ensure_soul_files_does_not_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            soul_dir = home / "soul"
            soul_dir.mkdir(parents=True, exist_ok=True)
            custom_soul = "# Custom Soul\n\nStay curious.\n"
            (soul_dir / "SOUL.md").write_text(custom_soul, encoding="utf-8")

            ensure_soul_files(home)

            self.assertEqual((soul_dir / "SOUL.md").read_text(encoding="utf-8"), custom_soul)
            for filename in ("STYLE.md", "USER.md", "MEMORY.md"):
                self.assertTrue((soul_dir / filename).is_file())

    def test_load_soul_normalizes_crlf_bom_and_trailing_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            soul_dir = ensure_soul_files(home)

            # Write UTF-8 with BOM, CRLF endings, Chinese text, and extra trailing blanks.
            (soul_dir / "SOUL.md").write_bytes(
                "\ufeff# 灵魂\r\n第一行\r\n第二行\r\n   \r\n\r\n".encode("utf-8")
            )
            bundle_dirty = load_soul(home, yesterday_summary="昨日摘要\r\n\r\n  \n")

            (soul_dir / "SOUL.md").write_bytes("# 灵魂\n第一行\n第二行".encode("utf-8"))
            bundle_clean = load_soul(home, yesterday_summary="昨日摘要")

            self.assertEqual(bundle_dirty.soul_md, "# 灵魂\n第一行\n第二行")
            self.assertEqual(bundle_dirty, bundle_clean)

            fp_dirty = freeze_prefix("ep-norm", bundle_dirty)
            fp_clean = freeze_prefix("ep-norm", bundle_clean)
            self.assertEqual(fp_dirty.text, fp_clean.text)
            self.assertEqual(fp_dirty.sha256, fp_clean.sha256)
            self.assertNotIn("\r", fp_dirty.text)

    def test_load_soul_respects_shinobu_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prev = os.environ.get("SHINOBU_HOME")
            os.environ["SHINOBU_HOME"] = tmp
            try:
                bundle = load_soul()
                self.assertTrue(bundle.soul_md)
                self.assertTrue((Path(tmp) / "soul" / "SOUL.md").is_file())
            finally:
                if prev is None:
                    os.environ.pop("SHINOBU_HOME", None)
                else:
                    os.environ["SHINOBU_HOME"] = prev


class TestPrefixFreezer(unittest.TestCase):
    def test_freeze_prefix_deterministic_and_verify(self) -> None:
        bundle = SoulBundle(
            soul_md="Soul A\r\n\r\n",
            style_md="Style B\n",
            user_md="User C",
            memory_md="Memory D\n\n",
            yesterday_summary="   \r\n",
        )
        bundle_equiv = SoulBundle(
            soul_md="Soul A",
            style_md="Style B",
            user_md="User C",
            memory_md="Memory D",
            yesterday_summary="",
        )

        fp1 = freeze_prefix("ep-001", bundle)
        fp2 = freeze_prefix("ep-001", bundle_equiv)

        self.assertEqual(fp1.episode_id, "ep-001")
        self.assertEqual(fp1.text, fp2.text)
        self.assertEqual(fp1.sha256, fp2.sha256)
        self.assertNotIn("## YESTERDAY", fp1.text)
        self.assertTrue(fp1.created_at.endswith("Z"))
        self.assertTrue(verify_frozen(fp1))

        # Tampered text must fail verification.
        tampered = FrozenPrefix(
            episode_id=fp1.episode_id,
            text=fp1.text + "drift",
            sha256=fp1.sha256,
            created_at=fp1.created_at,
        )
        self.assertFalse(verify_frozen(tampered))

        # Empty episode_id rejected.
        with self.assertRaises(ValueError):
            freeze_prefix("   ", bundle)

    def test_mid_episode_file_edit_does_not_drift_frozen_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            freezer = PrefixFreezer()

            # Start episode 1: auto-creates defaults and freezes L0.
            ep1_initial = freezer.freeze_from_home("ep-001", home, yesterday_summary="Day 0 recap")
            self.assertTrue(freezer.is_frozen("ep-001"))
            self.assertTrue(freezer.verify("ep-001"))
            self.assertIn("Day 0 recap", ep1_initial.text)

            # Mutate all soul files on disk mid-episode.
            soul_dir = home / "soul"
            (soul_dir / "SOUL.md").write_text("# Soul v2\nChanged mid-episode!\n", encoding="utf-8")
            (soul_dir / "STYLE.md").write_text("# Style v2\nDifferent tone.\n", encoding="utf-8")
            (soul_dir / "USER.md").write_text("# User v2\nNew user note.\n", encoding="utf-8")
            (soul_dir / "MEMORY.md").write_text("# Memory v2\nNew memory entry.\n", encoding="utf-8")

            # Re-fetching within the same episode must NOT reflect disk mutations.
            ep1_again_from_home = freezer.freeze_from_home("ep-001", home, yesterday_summary="Changed")
            ep1_again_from_bundle = freezer.freeze("ep-001", load_soul(home, yesterday_summary="Changed"))
            self.assertIs(ep1_again_from_home, ep1_initial)
            self.assertIs(ep1_again_from_bundle, ep1_initial)
            self.assertEqual(ep1_again_from_home.sha256, ep1_initial.sha256)
            self.assertNotIn("Changed mid-episode!", ep1_again_from_home.text)
            self.assertTrue(verify_frozen(ep1_again_from_home))

            # Next episode picks up the new files without affecting ep-001.
            ep2 = freezer.freeze_from_home("ep-002", home)
            self.assertNotEqual(ep2.sha256, ep1_initial.sha256)
            self.assertIn("Changed mid-episode!", ep2.text)
            self.assertIn("New memory entry.", ep2.text)
            self.assertEqual(freezer.get("ep-001"), ep1_initial)
            self.assertEqual(freezer.episodes(), ("ep-001", "ep-002"))

            # Releasing ep-001 clears only ep-001.
            released = freezer.release("ep-001")
            self.assertEqual(released, ep1_initial)
            self.assertFalse(freezer.is_frozen("ep-001"))
            self.assertFalse(freezer.verify("ep-001"))
            self.assertTrue(freezer.is_frozen("ep-002"))


if __name__ == "__main__":
    unittest.main()
