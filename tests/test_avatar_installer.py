# -*- coding: utf-8 -*-
"""Unit tests for the generic non-destructive avatar pack installer, CLI, and CI workflow."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from shinobu_core.server import cli as cli_mod
from shinobu_core.ui.avatar import (
    ACTIVE_POINTER_FILENAME,
    AVATARS_DIR_NAME,
    MANIFEST_FILENAME,
)
from shinobu_core.ui.avatar_installer import (
    DEFAULT_AVATAR_PACK_NAME,
    VALID_AVATAR_KEYS,
    apply_avatar_pack,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class AvatarInstallerTest(unittest.TestCase):
    def test_scan_copy_manifest_and_active_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            src = root / "source_pack"
            assets = src / "assets"
            assets.mkdir(parents=True)

            # Mix of extensions, casing, hyphens, assets/ subdir, and ignored files
            (src / "idle.svg").write_text("<svg>idle-svg</svg>", encoding="utf-8")
            (src / "idle.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
            (src / "Thinking-Happy.webp").write_bytes(b"RIFF....WEBP")
            (assets / "sleepy.gif").write_bytes(b"GIF89a")
            (src / "notes.txt").write_text("ignored text", encoding="utf-8")
            (src / "unknown_pose.png").write_bytes(b"\x89PNG\r\n\x1a\nunknown")
            (src / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

            result = apply_avatar_pack(src, name="my_pack", home=home)

            self.assertTrue(result["activated"])
            self.assertEqual(result["pack_name"], "my_pack")
            # .svg has higher priority than .png for the same key
            self.assertEqual(
                result["states"],
                {
                    "idle": "idle.svg",
                    "sleepy": "sleepy.gif",
                    "thinking_happy": "thinking_happy.webp",
                },
            )
            self.assertEqual(sorted(result["copied"]), ["idle.svg", "sleepy.gif", "thinking_happy.webp"])
            self.assertEqual(result["ignored_files"], ["notes.txt", "unknown_pose.png"])
            self.assertEqual(result["protected"], [])
            self.assertEqual(result["backed_up"], [])

            manifest_path = home / AVATARS_DIR_NAME / "my_pack" / MANIFEST_FILENAME
            active_path = home / AVATARS_DIR_NAME / ACTIVE_POINTER_FILENAME
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(active_path.is_file())

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["name"], "my_pack")
            self.assertEqual(manifest["states"], result["states"])
            for rel_path in manifest["states"].values():
                self.assertFalse(Path(rel_path).is_absolute())
                self.assertNotIn("..", rel_path)

            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(active["active_pack"], "my_pack")
            self.assertEqual(active["manifest"], f"my_pack/{MANIFEST_FILENAME}")

    def test_non_destructive_protection_and_force_version_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            src = root / "src"
            src.mkdir()

            idle_src = src / "idle.svg"
            idle_src.write_text("<svg>v1</svg>", encoding="utf-8")
            first = apply_avatar_pack(src, home=home)
            self.assertEqual(first["pack_name"], DEFAULT_AVATAR_PACK_NAME)
            self.assertEqual(first["copied"], ["idle.svg"])

            # Idempotent re-run produces no backups and marks unchanged
            second = apply_avatar_pack(src, home=home)
            self.assertEqual(second["unchanged"], ["idle.svg"])
            self.assertEqual(second["copied"], [])
            self.assertEqual(second["backed_up"], [])

            # Modified source without force preserves target
            idle_src.write_text("<svg>v2</svg>", encoding="utf-8")
            third = apply_avatar_pack(src, home=home, force=False)
            self.assertEqual(third["protected"], ["idle.svg"])
            self.assertEqual(third["copied"], [])
            target_idle = home / AVATARS_DIR_NAME / DEFAULT_AVATAR_PACK_NAME / "idle.svg"
            self.assertEqual(target_idle.read_text(encoding="utf-8"), "<svg>v1</svg>")

            # With force=True, backs up v1 under <home>/_versions/ before replacing
            fourth = apply_avatar_pack(src, home=home, force=True)
            self.assertEqual(fourth["copied"], ["idle.svg"])
            self.assertEqual(target_idle.read_text(encoding="utf-8"), "<svg>v2</svg>")
            backup_v1 = home / "_versions" / "avatars_custom_idle_v1.svg"
            self.assertTrue(backup_v1.is_file())
            self.assertEqual(backup_v1.read_text(encoding="utf-8"), "<svg>v1</svg>")

            # Updating manifest & active pointer also backs up prior JSON files
            (src / "speaking.svg").write_text("<svg>speak</svg>", encoding="utf-8")
            fifth = apply_avatar_pack(src, home=home)
            self.assertIn("speaking.svg", fifth["copied"])
            self.assertTrue((home / "_versions" / "avatars_custom_manifest_v1.json").is_file())

            other = apply_avatar_pack(src, name="alt", home=home)
            self.assertTrue(other["activated"])
            self.assertTrue((home / "_versions" / "avatars_active_v1.json").is_file())

    def test_in_place_scan_and_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            pack_dir = home / AVATARS_DIR_NAME / "local_pack"
            pack_dir.mkdir(parents=True)
            (pack_dir / "idle.svg").write_text("<svg>local</svg>", encoding="utf-8")

            res = apply_avatar_pack(None, name="local_pack", home=home)
            self.assertTrue(res["activated"])
            self.assertEqual(res["states"], {"idle": "idle.svg"})
            self.assertEqual(res["unchanged"], ["idle.svg"])

            with self.assertRaises(ValueError):
                apply_avatar_pack(None, name="../escape", home=home)

            with self.assertRaises(FileNotFoundError):
                apply_avatar_pack(Path(tmp) / "missing_dir", home=home)

    def test_valid_avatar_keys_contract(self) -> None:
        self.assertIn("idle", VALID_AVATAR_KEYS)
        self.assertIn("speaking", VALID_AVATAR_KEYS)
        self.assertIn("thinking", VALID_AVATAR_KEYS)
        self.assertIn("sleepy", VALID_AVATAR_KEYS)
        self.assertIn("idle_happy", VALID_AVATAR_KEYS)

    def test_cli_avatar_subcommand_and_no_plugin_conflict(self) -> None:
        parser = cli_mod.build_parser(kernel=None)
        subparser_actions = [
            action for action in parser._actions if getattr(action, "choices", None) is not None
        ]
        self.assertTrue(subparser_actions)
        choices = subparser_actions[0].choices
        self.assertIn("avatar", choices)
        # Ensure built-in subcommand does not collide with external plugin command `avatar-apply`
        self.assertNotIn("avatar-apply", choices)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            src = root / "sprites"
            src.mkdir()
            (src / "idle.svg").write_text("<svg>cli</svg>", encoding="utf-8")

            args = parser.parse_args(["avatar", "--source", str(src), "--name", "cli_pack", "--json"])
            buf = io.StringIO()
            fake_kernel = SimpleNamespace(home=home)
            with contextlib.redirect_stdout(buf):
                rc = args.func(args, fake_kernel)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["pack_name"], "cli_pack")
            self.assertTrue(payload["activated"])
            self.assertEqual(payload["states"], {"idle": "idle.svg"})

            # Human-readable output path
            args_text = parser.parse_args(["avatar", "--name", "cli_pack"])
            buf_text = io.StringIO()
            with contextlib.redirect_stdout(buf_text):
                rc_text = args_text.func(args_text, fake_kernel)
            self.assertEqual(rc_text, 0)
            self.assertIn("Avatar pack 'cli_pack'", buf_text.getvalue())

            # Missing source directory returns non-zero exit code cleanly
            args_err = parser.parse_args(["avatar", "--source", str(root / "nope"), "--json"])
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_err):
                rc_err = args_err.func(args_err, fake_kernel)
            self.assertEqual(rc_err, 1)
            err_payload = json.loads(buf_err.getvalue())
            self.assertFalse(err_payload["ok"])

    def test_github_actions_ci_workflow_present_and_valid(self) -> None:
        ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci_path.is_file(), f"Missing CI workflow: {ci_path}")
        text = ci_path.read_text(encoding="utf-8")
        for expected in (
            "push:",
            "pull_request:",
            'branches: ["main"]',
            "ubuntu-latest",
            "windows-latest",
            "macos-latest",
            '"3.10"',
            '"3.11"',
            '"3.12"',
            "pip install -e .",
            "SHINOBU_DISABLE_PLUGINS",
            "python -m unittest discover -s tests -v",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("requirements.txt", text)


if __name__ == "__main__":
    unittest.main()
