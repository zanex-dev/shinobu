# -*- coding: utf-8 -*-
"""CLI end-to-end tests and microkernel dual-gate verification."""

from __future__ import annotations

import contextlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from shinobu_core.core.kernel import PLUGIN_GROUP, ShinobuKernel
from shinobu_core.protocol.events import Event
from shinobu_core.server.cli import (
    _build_engine,
    build_daemon,
    build_edge_node,
    build_parser,
    build_telegram_bridge,
    main,
)


def _clean_env(home: Path, *, disable_plugins: bool = True, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["SHINOBU_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    if disable_plugins:
        env["SHINOBU_DISABLE_PLUGINS"] = "1"
    else:
        env.pop("SHINOBU_DISABLE_PLUGINS", None)
        env.pop("SHINOBU_DISABLED_PLUGINS", None)
    if extra:
        env.update(extra)
    return env


def _run_cli_subprocess(
    args: list[str],
    *,
    env: dict[str, str],
    use_script: bool = False,
) -> subprocess.CompletedProcess[str]:
    if use_script:
        exe = shutil.which("shinobu")
        cmd = [exe, *args] if exe else [sys.executable, "-m", "shinobu_core", *args]
    else:
        cmd = [sys.executable, "-m", "shinobu_core", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
        check=False,
    )


class _DummyPlugin:
    name = "dummy_ext"

    def __init__(self) -> None:
        self.events: list[str] = []

    def register_sensors(self, kernel: ShinobuKernel) -> None:
        kernel.register_sensor(
            "dummy_sensor",
            lambda: Event(kind="sensor_tick", payload={"sensor": "dummy_sensor"}),
        )

    def register_cli(self, subparsers) -> None:
        p = subparsers.add_parser("dummy-ping", help="Dummy plugin ping")
        p.set_defaults(func=lambda _args: (print("dummy-pong"), 0)[1])

    def register_hooks(self, bus) -> None:
        bus.on("done", lambda *_a, **_kw: self.events.append("done"))


class TestCliAndDualGate(unittest.TestCase):
    def test_gate1_bare_foundation_help_and_chat_isolated(self) -> None:
        """Gate 1: SHINOBU_DISABLE_PLUGINS=1 keeps CLI fully operational with zero plugin residue."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            env = _clean_env(home, disable_plugins=True)

            # 1) shinobu --help (console script if installed, else python -m)
            res_help = _run_cli_subprocess(["--help"], env=env, use_script=True)
            self.assertEqual(res_help.returncode, 0, res_help.stderr)
            for sub in (
                "init",
                "chat",
                "sleep",
                "serve",
                "ui",
                "pet",
                "compat",
                "mcp",
                "mcp-serve",
                "telegram",
                "node",
                "status",
                "doctor",
            ):
                self.assertIn(sub, res_help.stdout)
            private_subcmd = "".join(["pri", "vate-info"])
            self.assertNotIn(private_subcmd, res_help.stdout)

            res_serve_help = _run_cli_subprocess(["serve", "--help"], env=env, use_script=True)
            self.assertEqual(res_serve_help.returncode, 0, res_serve_help.stderr)
            for flag in ("--host", "--port", "--engine", "--token", "--cloud-sandbox"):
                self.assertIn(flag, res_serve_help.stdout)

            res_tg_help = _run_cli_subprocess(["telegram", "--help"], env=env, use_script=True)
            self.assertEqual(res_tg_help.returncode, 0, res_tg_help.stderr)
            for flag in ("--bot-token", "--chat-id", "--daemon-url", "--token", "--approval-poll-sec"):
                self.assertIn(flag, res_tg_help.stdout)

            res_node_help = _run_cli_subprocess(["node", "--help"], env=env, use_script=True)
            self.assertEqual(res_node_help.returncode, 0, res_node_help.stderr)
            for flag in ("--cloud-url", "--cloud-token", "--bind-host", "--port", "--engine", "--allowed-root"):
                self.assertIn(flag, res_node_help.stdout)

            res_ui_help = _run_cli_subprocess(["ui", "--help"], env=env, use_script=True)
            self.assertEqual(res_ui_help.returncode, 0, res_ui_help.stderr)
            for flag in ("--port", "--engine", "--start", "--no-open", "--json"):
                self.assertIn(flag, res_ui_help.stdout)

            res_pet_help = _run_cli_subprocess(["pet", "--help"], env=env, use_script=True)
            self.assertEqual(res_pet_help.returncode, 0, res_pet_help.stderr)
            for flag in ("--port", "--engine", "--interval", "--avatar-dir"):
                self.assertIn(flag, res_pet_help.stdout)

            res_compat_help = _run_cli_subprocess(["compat", "--help"], env=env, use_script=True)
            self.assertEqual(res_compat_help.returncode, 0, res_compat_help.stderr)
            self.assertIn("--json", res_compat_help.stdout)

            res_mcp_help = _run_cli_subprocess(["mcp", "--help"], env=env, use_script=True)
            self.assertEqual(res_mcp_help.returncode, 0, res_mcp_help.stderr)
            for flag in ("--probe", "--json"):
                self.assertIn(flag, res_mcp_help.stdout)

            res_mcp_serve_help = _run_cli_subprocess(["mcp-serve", "--help"], env=env, use_script=True)
            self.assertEqual(res_mcp_serve_help.returncode, 0, res_mcp_serve_help.stderr)

            # 2) shinobu init
            res_init = _run_cli_subprocess(["init", "--json"], env=env, use_script=True)
            self.assertEqual(res_init.returncode, 0, res_init.stderr)
            init_data = json.loads(res_init.stdout)
            self.assertEqual(init_data["status"], "initialized")
            self.assertTrue((home / "soul" / "SOUL.md").exists())
            self.assertTrue((home / "memory" / "shinobu.db").exists())

            # 3) shinobu chat --engine mock --once "hi"
            res_chat = _run_cli_subprocess(
                ["chat", "--engine", "mock", "--once", "hi"],
                env=env,
                use_script=True,
            )
            self.assertEqual(res_chat.returncode, 0, res_chat.stderr)
            self.assertTrue(res_chat.stdout.strip(), "Expected non-empty reply from mock engine")

            # 4) status reports active episode and zero plugins
            res_status = _run_cli_subprocess(["status", "--json"], env=env, use_script=True)
            self.assertEqual(res_status.returncode, 0, res_status.stderr)
            status_data = json.loads(res_status.stdout)
            self.assertTrue(status_data["plugins_disabled"])
            self.assertEqual(status_data["plugins"], [])
            self.assertEqual(status_data["registered_sensors"], [])
            self.assertTrue(status_data["active_episode_id"])

            # 5) shinobu sleep consolidates and closes the active episode
            res_sleep = _run_cli_subprocess(
                ["sleep", "--engine", "mock", "--json"],
                env=env,
                use_script=True,
            )
            self.assertEqual(res_sleep.returncode, 0, res_sleep.stderr)
            sleep_data = json.loads(res_sleep.stdout)
            self.assertEqual(sleep_data["episode_id"], status_data["active_episode_id"])
            self.assertTrue(Path(sleep_data["journal_path"]).exists())

            # 6) Second sleep is a clean no-op
            res_sleep2 = _run_cli_subprocess(
                ["sleep", "--engine", "mock", "--json"],
                env=env,
                use_script=True,
            )
            self.assertEqual(res_sleep2.returncode, 0, res_sleep2.stderr)
            self.assertEqual(json.loads(res_sleep2.stdout)["status"], "idle")

    def test_env_engine_override_and_python_m_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env = _clean_env(home, disable_plugins=True, extra={"SHINOBU_ENGINE": "mock"})
            res = _run_cli_subprocess(["chat", "--once", "hello via env"], env=env, use_script=False)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(res.stdout.strip())

    def test_plugin_cli_and_sensor_wiring_in_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)
            plugin = _DummyPlugin()
            rec = kernel.attach_plugin("dummy_ext", plugin)
            self.assertEqual(rec.status, "loaded")

            # CLI registrar attached via build_parser
            parser = build_parser(kernel)
            args = parser.parse_args(["dummy-ping"])
            self.assertEqual(args.command, "dummy-ping")

            # Daemon attaches kernel sensors to HeartbeatLoop & writes endpoint.json
            daemon, store = build_daemon(kernel, engine_name="mock", port=0, interval_sec=60.0)
            try:
                info = daemon.start(start_heartbeat=False)
                self.assertEqual(info["host"], "127.0.0.1")
                self.assertGreater(info["port"], 0)
                self.assertTrue(daemon.endpoint_path.exists())

                assert daemon.heartbeat is not None
                emitted = daemon.heartbeat.tick_once()
                sensor_events = [ev for ev in emitted if ev.kind == "sensor_tick"]
                self.assertTrue(
                    any(ev.payload.get("sensor") == "dummy_sensor" for ev in sensor_events),
                    f"Expected dummy_sensor tick in {emitted!r}",
                )

                # status must redact Bearer token while showing daemon URL
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main(["status", "--json"], kernel=kernel)
                self.assertEqual(code, 0)
                status_text = buf.getvalue()
                self.assertNotIn(daemon.token, status_text)
                status_obj = json.loads(status_text)
                self.assertEqual(status_obj["daemon"]["url"], info["url"])
                self.assertTrue(status_obj["daemon"]["token_present"])
            finally:
                daemon.stop()
                store.close()
            self.assertFalse(daemon.endpoint_path.exists())

    def test_installed_entry_point_plugin_toggle(self) -> None:
        """When external plugins are installed in the environment, toggle works cleanly."""
        eps = list(importlib.metadata.entry_points(group=PLUGIN_GROUP))
        if not eps:
            self.skipTest("No external shinobu.plugins entry points installed")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            # Enabled: plugin loads and registers sensors/CLI
            env_on = _clean_env(home, disable_plugins=False)
            res_on = _run_cli_subprocess(["status", "--json"], env=env_on, use_script=True)
            self.assertEqual(res_on.returncode, 0, res_on.stderr)
            data_on = json.loads(res_on.stdout)
            self.assertFalse(data_on["plugins_disabled"])
            loaded_names = {p["name"] for p in data_on["plugins"] if p["status"] == "loaded"}
            expected_names = {ep.name for ep in eps}
            self.assertTrue(expected_names.issubset(loaded_names))

            # Disabled: bare foundation loads zero plugins
            env_off = _clean_env(home, disable_plugins=True)
            res_off = _run_cli_subprocess(["status", "--json"], env=env_off, use_script=True)
            self.assertEqual(res_off.returncode, 0, res_off.stderr)
            data_off = json.loads(res_off.stdout)
            self.assertTrue(data_off["plugins_disabled"])
            self.assertEqual(data_off["plugins"], [])
            self.assertEqual(data_off["registered_sensors"], [])

    def test_build_engine_agy_sdk_wiring_and_isolation(self) -> None:
        """_build_engine and build_daemon wire agy_sdk env vars and isolated runtime dirs."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)
            expected_runtime = (home.resolve() / "runtime")
            expected_save = (expected_runtime / "agy_sdk_save").resolve()
            expected_app_data = (expected_runtime / "agy_app_data").resolve()
            expected_sessions = (expected_runtime / "agy_sdk_sessions.json").resolve()

            env_patch = {
                "SHINOBU_AGY_MODE": "openai_local",
                "SHINOBU_MODEL": "gemini-2.5-pro",
                "SHINOBU_BASE_URL": "https://example.invalid/v1",
                "SHINOBU_MODEL_PATH": "/models/local.gguf",
                "SHINOBU_API_KEY": "sk-test-shinobu-key",
                "GEMINI_API_KEY": "gm-fallback-key",
            }
            captured: dict[str, object] = {}

            def _spy_create(name: str, **kwargs):
                captured["name"] = name
                captured["kwargs"] = kwargs
                from shinobu_core.engines import create_engine as real_create

                return real_create(name, **kwargs)

            with mock.patch.dict(os.environ, env_patch, clear=False):
                with mock.patch("shinobu_core.server.cli.create_engine", side_effect=_spy_create):
                    engine = _build_engine(kernel, "agy-sdk")

            self.assertEqual(engine.name, "agy_sdk")
            kw = captured["kwargs"]
            assert isinstance(kw, dict)
            self.assertEqual(kw["mode"], "openai_local")
            self.assertEqual(kw["model"], "gemini-2.5-pro")
            self.assertEqual(kw["base_url"], "https://example.invalid/v1")
            self.assertEqual(kw["model_path"], "/models/local.gguf")
            self.assertEqual(kw["api_key"], "sk-test-shinobu-key")
            self.assertEqual(Path(kw["save_dir"]), expected_save)
            self.assertEqual(Path(kw["app_data_dir"]), expected_app_data)
            self.assertEqual(Path(kw["session_map_path"]), expected_sessions)
            self.assertTrue(expected_save.is_dir())
            self.assertTrue(expected_app_data.is_dir())

            # Fallback to GEMINI_API_KEY and default mode="gemini" when SHINOBU_* unset
            env_fallback = {
                "SHINOBU_AGY_MODE": "",
                "SHINOBU_API_KEY": "",
                "GEMINI_API_KEY": "gm-only-key",
            }
            with mock.patch.dict(os.environ, env_fallback, clear=False):
                with mock.patch("shinobu_core.server.cli.create_engine", side_effect=_spy_create):
                    daemon, store = build_daemon(kernel, engine_name="agy_sdk", port=0)
                    try:
                        self.assertEqual(daemon.orchestrator.engine.name, "agy_sdk")
                    finally:
                        store.close()
            kw2 = captured["kwargs"]
            assert isinstance(kw2, dict)
            self.assertEqual(kw2["mode"], "gemini")
            self.assertEqual(kw2["api_key"], "gm-only-key")

    def test_build_engine_agy_cli_wiring_and_isolation(self) -> None:
        """_build_engine wires agy_cli binary/model/cwd/session_map_path with isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)
            expected_runtime = (home.resolve() / "runtime")
            expected_cwd = (expected_runtime / "agy_workspace").resolve()
            expected_sessions = (expected_runtime / "agy_cli_sessions.json").resolve()

            captured: dict[str, object] = {}

            def _spy_create(name: str, **kwargs):
                captured["name"] = name
                captured["kwargs"] = kwargs
                from shinobu_core.engines import create_engine as real_create

                return real_create(name, **kwargs)

            with mock.patch.dict(
                os.environ,
                {"SHINOBU_AGY_BIN": "agy-custom", "SHINOBU_MODEL": "gemini-2.5-flash"},
                clear=False,
            ):
                with mock.patch("shinobu_core.server.cli.create_engine", side_effect=_spy_create):
                    engine = _build_engine(kernel, "agy-cli")

            self.assertEqual(engine.name, "agy_cli")
            kw = captured["kwargs"]
            assert isinstance(kw, dict)
            self.assertEqual(kw["binary"], "agy-custom")
            self.assertEqual(kw["model"], "gemini-2.5-flash")
            self.assertEqual(Path(kw["cwd"]), expected_cwd)
            self.assertEqual(Path(kw["session_map_path"]), expected_sessions)
            self.assertTrue(expected_cwd.is_dir())

            # Default binary falls back to "agy"
            with mock.patch.dict(os.environ, {"SHINOBU_AGY_BIN": ""}, clear=False):
                with mock.patch("shinobu_core.server.cli.create_engine", side_effect=_spy_create):
                    _build_engine(kernel, "agy_cli")
            kw_default = captured["kwargs"]
            assert isinstance(kw_default, dict)
            self.assertEqual(kw_default["binary"], "agy")

    def test_doctor_json_text_and_secret_redaction(self) -> None:
        """shinobu doctor diagnoses drivers/isolation/plugins and never leaks raw secrets."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)
            secret_val = "sk-top-secret-never-print-987654"
            gemini_secret = "AIza-secret-never-print-123456"

            # Case 1: SDK available + API key set -> recommends agy_sdk, zero secret leakage
            with mock.patch.dict(
                os.environ,
                {
                    "SHINOBU_API_KEY": secret_val,
                    "GEMINI_API_KEY": gemini_secret,
                    "SHINOBU_AGY_MODE": "gemini",
                    "SHINOBU_MODEL": "gemini-2.5-pro",
                    "SHINOBU_BASE_URL": "",
                    "SHINOBU_MODEL_PATH": "",
                },
                clear=False,
            ):
                with (
                    mock.patch("shinobu_core.server.cli.importlib.util.find_spec", return_value=object()),
                    mock.patch("shinobu_core.server.cli.shutil.which", return_value=None),
                ):
                    buf_json = io.StringIO()
                    with contextlib.redirect_stdout(buf_json):
                        code_json = main(["doctor", "--json"], kernel=kernel)
                    self.assertEqual(code_json, 0)
                    raw_json = buf_json.getvalue()
                    self.assertNotIn(secret_val, raw_json)
                    self.assertNotIn(gemini_secret, raw_json)

                    data = json.loads(raw_json)
                    self.assertEqual(data["recommended_engine"], "agy_sdk")
                    self.assertTrue(data["drivers"]["agy_sdk"]["available"])
                    self.assertTrue(data["drivers"]["agy_sdk"]["ready"])
                    self.assertFalse(data["drivers"]["agy_cli"]["available"])
                    self.assertTrue(data["credentials"]["shinobu_api_key"]["present"])
                    self.assertTrue(data["credentials"]["gemini_api_key"]["present"])
                    self.assertTrue(data["plugins"]["healthy"])
                    self.assertIn("agy_sdk", data["official_install_hints"])
                    self.assertIn("agy_cli", data["official_install_hints"])
                    self.assertTrue(data["isolation"]["agy_app_data_dir"].endswith("agy_app_data"))

                    buf_txt = io.StringIO()
                    with contextlib.redirect_stdout(buf_txt):
                        code_txt = main(["doctor"], kernel=kernel)
                    self.assertEqual(code_txt, 0)
                    raw_txt = buf_txt.getvalue()
                    self.assertNotIn(secret_val, raw_txt)
                    self.assertNotIn(gemini_secret, raw_txt)
                    self.assertIn("Recommended engine : agy_sdk", raw_txt)

            # Case 2: SDK missing (ModuleNotFoundError on parent pkg), CLI available -> recommends agy_cli
            with mock.patch.dict(
                os.environ,
                {
                    "SHINOBU_API_KEY": "",
                    "GEMINI_API_KEY": "",
                    "SHINOBU_BASE_URL": "",
                    "SHINOBU_MODEL_PATH": "",
                },
                clear=False,
            ):
                with (
                    mock.patch(
                        "shinobu_core.server.cli.importlib.util.find_spec",
                        side_effect=ModuleNotFoundError("google"),
                    ),
                    mock.patch("shinobu_core.server.cli.shutil.which", return_value="/usr/bin/agy"),
                ):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        code = main(["doctor", "--json"], kernel=kernel)
                    self.assertEqual(code, 0)
                    data2 = json.loads(buf.getvalue())
                    self.assertEqual(data2["recommended_engine"], "agy_cli")
                    self.assertFalse(data2["drivers"]["agy_sdk"]["available"])
                    self.assertTrue(data2["drivers"]["agy_cli"]["available"])
                    self.assertFalse(data2["credentials"]["any_api_key_present"])

            # Case 3: Neither SDK nor CLI available + failed plugin -> recommends mock & reports unhealthy
            class _BrokenPlugin:
                def register_sensors(self, _k: ShinobuKernel) -> None:
                    raise RuntimeError("boom")

            kernel.attach_plugin("broken_ext", _BrokenPlugin())
            with mock.patch.dict(
                os.environ,
                {
                    "SHINOBU_API_KEY": "",
                    "GEMINI_API_KEY": "",
                    "SHINOBU_BASE_URL": "",
                    "SHINOBU_MODEL_PATH": "",
                },
                clear=False,
            ):
                with (
                    mock.patch("shinobu_core.server.cli.importlib.util.find_spec", return_value=None),
                    mock.patch("shinobu_core.server.cli.shutil.which", return_value=None),
                ):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        code = main(["doctor", "--json"], kernel=kernel)
                    self.assertEqual(code, 0)
                    data3 = json.loads(buf.getvalue())
                    self.assertEqual(data3["recommended_engine"], "mock")
                    self.assertFalse(data3["plugins"]["healthy"])
                    self.assertEqual(data3["plugins"]["failed"], 1)

            # Case 4: Subprocess end-to-end shinobu doctor --json under isolated env
            env_sub = _clean_env(home, disable_plugins=True)
            res_doc = _run_cli_subprocess(["doctor", "--json"], env=env_sub, use_script=True)
            self.assertEqual(res_doc.returncode, 0, res_doc.stderr)
            sub_data = json.loads(res_doc.stdout)
            self.assertIn("recommended_engine", sub_data)
            self.assertIn("official_install_hints", sub_data)
            self.assertTrue(sub_data["plugins_disabled"])

    def test_ui_command_no_open_json_probe_and_reuse(self) -> None:
        """`shinobu ui --no-open --json` reports not_running cleanly and reuses a live daemon with #token=."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            env = _clean_env(home, disable_plugins=True)

            # 1) Without a running daemon, probe reports not_running without leaking local paths.
            res_down = _run_cli_subprocess(["ui", "--no-open", "--json"], env=env, use_script=True)
            self.assertEqual(res_down.returncode, 1, res_down.stderr)
            down_data = json.loads(res_down.stdout)
            self.assertEqual(down_data["status"], "not_running")
            self.assertFalse(down_data["reused"])
            self.assertFalse(down_data["opened"])
            self.assertNotIn(str(home).lower(), res_down.stdout.lower())

            # 2) With a live loopback daemon, `shinobu ui --no-open --json` reuses it and builds #token= URL.
            kernel = ShinobuKernel(home=home)
            daemon, store = build_daemon(kernel, engine_name="mock", port=0)
            try:
                info = daemon.start(start_heartbeat=False)
                res_up = _run_cli_subprocess(["ui", "--no-open", "--json"], env=env, use_script=True)
                self.assertEqual(res_up.returncode, 0, res_up.stderr)
                up_data = json.loads(res_up.stdout)
                self.assertEqual(up_data["status"], "ready")
                self.assertTrue(up_data["reused"])
                self.assertFalse(up_data["started"])
                self.assertFalse(up_data["opened"])
                self.assertEqual(up_data["url"], info["url"])
                self.assertEqual(up_data["console_url"], f"{info['url']}/#token={info['token']}")
                self.assertNotIn(str(home).lower(), res_up.stdout.lower())
            finally:
                daemon.stop()
                store.close()

    def test_pet_command_reuses_or_autostarts_daemon_and_handles_missing_pyside6(self) -> None:
        """`shinobu pet` reuses live daemons, stops self-started ones, and handles missing PySide6 cleanly."""
        from shinobu_core.ui.client import DaemonClient, discover_endpoint
        from shinobu_core.ui.pet_window import PySide6NotInstalledError

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            avatar_dir = Path(tmp) / "custom_avatars"
            avatar_dir.mkdir(parents=True, exist_ok=True)
            captured: list[dict[str, object]] = []

            def _fake_pet_shell(
                home: Path | str | None = None,
                url: str | None = None,
                token: str | None = None,
                open_web_cb: object = None,
                *,
                avatar_dir: Path | str | None = None,
            ) -> int:
                assert url is not None and token is not None
                health = DaemonClient(url=url, token=token).health()
                self.assertEqual(health.get("status"), "ok")
                captured.append(
                    {
                        "home": str(home),
                        "url": url,
                        "token": token,
                        "avatar_dir": str(avatar_dir) if avatar_dir is not None else None,
                        "callable_cb": callable(open_web_cb),
                    }
                )
                return 0

            # A) Auto-starts daemon when none is running, and shuts it down on exit.
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                mock.patch("shinobu_core.ui.pet_window.run_pet_shell", side_effect=_fake_pet_shell),
            ):
                rc = main(
                    [
                        "--home",
                        str(home),
                        "pet",
                        "--port",
                        "0",
                        "--engine",
                        "mock",
                        "--avatar-dir",
                        str(avatar_dir),
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0]["callable_cb"])
            self.assertEqual(captured[0]["avatar_dir"], str(avatar_dir))
            self.assertIsNone(discover_endpoint(home))

            # B) Reuses an already-running daemon without stopping it when pet exits.
            kernel = ShinobuKernel(home=home)
            daemon, store = build_daemon(kernel, engine_name="mock", port=0)
            try:
                info = daemon.start(start_heartbeat=False)
                with (
                    mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                    mock.patch("shinobu_core.ui.pet_window.run_pet_shell", side_effect=_fake_pet_shell),
                ):
                    rc_reuse = main(["--home", str(home), "pet", "--engine", "mock"])
                self.assertEqual(rc_reuse, 0)
                self.assertEqual(captured[-1]["url"], info["url"])
                self.assertEqual(captured[-1]["token"], info["token"])
                self.assertTrue(daemon.is_running)
            finally:
                daemon.stop()
                store.close()

            # C) Missing PySide6 yields non-zero exit with install guidance and cleans up daemon.
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                mock.patch(
                    "shinobu_core.ui.pet_window.run_pet_shell",
                    side_effect=PySide6NotInstalledError("PySide6 is not installed"),
                ),
                contextlib.redirect_stderr(err_buf),
            ):
                rc_missing = main(["--home", str(home), "pet", "--port", "0", "--engine", "mock"])
            self.assertEqual(rc_missing, 1)
            self.assertIn("PySide6", err_buf.getvalue())
            self.assertIsNone(discover_endpoint(home))

    def test_compat_subcommand_reports_endpoints_and_handles_missing_daemon(self) -> None:
        """`shinobu compat` emits `/v1` connection info for healthy daemons and returns 1 when absent."""
        from shinobu_core.core.kernel import ShinobuKernel
        from shinobu_core.server.cli import build_daemon

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "compat_home"
            home.mkdir(parents=True, exist_ok=True)

            # 1) No daemon running -> exit 1 with guidance
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                contextlib.redirect_stderr(err_buf),
            ):
                rc_absent = main(["--home", str(home), "compat"])
            self.assertEqual(rc_absent, 1)
            self.assertIn("shinobu serve", err_buf.getvalue())

            out_absent_json = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                contextlib.redirect_stdout(out_absent_json),
            ):
                rc_absent_json = main(["--home", str(home), "compat", "--json"])
            self.assertEqual(rc_absent_json, 1)
            absent_payload = json.loads(out_absent_json.getvalue())
            self.assertFalse(absent_payload["ok"])
            self.assertFalse(absent_payload["mcp_config_exists"])

            # 2) Healthy running daemon -> exit 0 with OpenAI /v1 + SSE + MCP info
            kernel = ShinobuKernel(home=home)
            with mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False):
                daemon, store = build_daemon(kernel, engine_name="mock", port=0)
            try:
                info = daemon.start(start_heartbeat=False)
                (home / "mcp_servers.json").write_text('{"mcpServers": {}}', encoding="utf-8")

                out_json = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                    contextlib.redirect_stdout(out_json),
                ):
                    rc_ok = main(["--home", str(home), "compat", "--json"])
                self.assertEqual(rc_ok, 0)
                data = json.loads(out_json.getvalue())
                self.assertEqual(data["openai_base_url"], f"{info['url'].rstrip('/')}/v1")
                self.assertEqual(data["model"], "shinobu")
                self.assertEqual(data["api_key"], info["token"])
                self.assertEqual(data["events_url"], f"{info['url'].rstrip('/')}/events")
                self.assertTrue(data["mcp_config_exists"])

                out_text = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                    contextlib.redirect_stdout(out_text),
                ):
                    rc_text = main(["--home", str(home), "compat"])
                self.assertEqual(rc_text, 0)
                text_rendered = out_text.getvalue()
                self.assertIn(data["openai_base_url"], text_rendered)
                self.assertIn(data["api_key"], text_rendered)
            finally:
                daemon.stop()
                store.close()

    def test_mcp_subcommand_list_probe_and_lifecycle_wireup(self) -> None:
        """`shinobu mcp [--probe]` lists/probes MCP servers and `build_daemon`/`chat` wire & close them."""
        from shinobu_core.core.kernel import ShinobuKernel
        from shinobu_core.server.cli import build_daemon

        stub_code = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n"
            "        continue\n"
            "    msg = json.loads(line)\n"
            "    req_id = msg.get('id')\n"
            "    if req_id is None:\n"
            "        continue\n"
            "    method = msg.get('method')\n"
            "    if method == 'initialize':\n"
            "        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'demo-mcp', 'version': '1.0'}}\n"
            "    elif method == 'tools/list':\n"
            "        result = {'tools': [{'name': 'ping_tool', 'description': 'Echo ping', 'inputSchema': {'type': 'object', 'properties': {}}}]}\n"
            "    elif method == 'tools/call':\n"
            "        result = {'content': [{'type': 'text', 'text': 'pong'}]}\n"
            "    else:\n"
            "        result = {}\n"
            "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': result}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mcp_home"
            home.mkdir(parents=True, exist_ok=True)
            stub_path = Path(tmp) / "demo_mcp_server.py"
            stub_path.write_text(stub_code, encoding="utf-8")

            # 1) Missing mcp_servers.json -> empty config, exit 0
            out_empty = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                contextlib.redirect_stdout(out_empty),
            ):
                rc_empty = main(["--home", str(home), "mcp", "--json"])
            self.assertEqual(rc_empty, 0)
            empty_data = json.loads(out_empty.getvalue())
            self.assertFalse(empty_data["config_exists"])
            self.assertEqual(empty_data["servers"], [])

            # 2) Write mcp_servers.json with 1 enabled + 1 disabled server
            cfg_payload = {
                "mcpServers": {
                    "demo": {
                        "command": sys.executable,
                        "args": ["-u", str(stub_path)],
                    },
                    "off": {
                        "command": sys.executable,
                        "args": ["-u", str(stub_path)],
                        "disabled": True,
                    },
                }
            }
            (home / "mcp_servers.json").write_text(
                json.dumps(cfg_payload, indent=2),
                encoding="utf-8",
            )

            # 2a) List only (no probe)
            out_list = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                contextlib.redirect_stdout(out_list),
            ):
                rc_list = main(["--home", str(home), "mcp", "--json"])
            self.assertEqual(rc_list, 0)
            list_data = json.loads(out_list.getvalue())
            self.assertTrue(list_data["config_exists"])
            self.assertFalse(list_data["probed"])
            self.assertEqual(len(list_data["servers"]), 2)

            # 2b) Live probe handshake + tools discovery + elapsed_ms
            out_probe = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                contextlib.redirect_stdout(out_probe),
            ):
                rc_probe = main(["--home", str(home), "mcp", "--probe", "--json"])
            self.assertEqual(rc_probe, 0)
            probe_data = json.loads(out_probe.getvalue())
            self.assertTrue(probe_data["probed"])
            self.assertGreaterEqual(probe_data["elapsed_ms"], 0.0)
            by_name = {item["name"]: item for item in probe_data["servers"]}
            self.assertTrue(by_name["demo"]["connected"])
            self.assertEqual(by_name["demo"]["tools"], ["ping_tool"])
            self.assertGreaterEqual(by_name["demo"]["elapsed_ms"], 0.0)
            self.assertTrue(by_name["off"]["disabled"])
            self.assertFalse(by_name["off"]["connected"])

            # 3) build_daemon skips MCP when SHINOBU_DISABLE_PLUGINS=1
            kernel_disabled = ShinobuKernel(home=home)
            with mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False):
                daemon_off, store_off = build_daemon(kernel_disabled, engine_name="mock", port=0)
            try:
                self.assertIsNone(getattr(daemon_off, "mcp_manager", None))
                self.assertNotIn("mcp_demo_ping_tool", kernel_disabled.tools)
            finally:
                daemon_off.stop()
                store_off.close()

            # 4) build_daemon mounts MCP when enabled and closes cleanly on daemon.stop()
            kernel_enabled = ShinobuKernel(home=home)
            with mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": ""}, clear=False):
                daemon_on, store_on = build_daemon(kernel_enabled, engine_name="mock", port=0)
            mcp_mgr = getattr(daemon_on, "mcp_manager", None)
            self.assertIsNotNone(mcp_mgr)
            self.assertIn("mcp_demo_ping_tool", kernel_enabled.tools)
            self.assertIsNotNone(mcp_mgr.get_client("demo"))
            daemon_on.stop()
            store_on.close()
            self.assertIsNone(mcp_mgr.get_client("demo"))

    def test_mcp_serve_subcommand_delegates_to_stdio_server(self) -> None:
        """`shinobu mcp-serve` delegates to `run_mcp_stdio_server(home=kernel.home)` and forwards exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mcp_serve_home"
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                mock.patch("shinobu_core.server.cli.run_mcp_stdio_server", return_value=0) as mock_serve,
            ):
                rc = main(["--home", str(home), "mcp-serve"])
            self.assertEqual(rc, 0)
            mock_serve.assert_called_once_with(home=home)

    def test_community_mcp_presets_install_merge_backup_and_cli(self) -> None:
        """Curated zero-key presets merge non-destructively, version-backup old config, and work via CLI."""
        from contextlib import redirect_stderr, redirect_stdout
        import io
        import json

        from shinobu_core.mcp import (
            COMMUNITY_MCP_PRESETS,
            McpClientManager,
            install_mcp_preset,
            list_community_presets,
        )

        self.assertEqual(
            set(COMMUNITY_MCP_PRESETS.keys()),
            {"filesystem", "memory", "context7", "sequential-thinking"},
        )
        for spec in COMMUNITY_MCP_PRESETS.values():
            self.assertFalse(spec["requires_api_key"])

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "community_mcp_home"
            home.mkdir(parents=True, exist_ok=True)
            cfg_path = home / "mcp_servers.json"

            # Seed an existing custom MCP server and extra top-level metadata
            original_payload = {
                "meta": {"owner": "local-test"},
                "mcpServers": {
                    "custom_demo": {
                        "command": "python",
                        "args": ["-V"],
                        "disabled": True,
                    }
                },
            }
            cfg_path.write_text(json.dumps(original_payload, indent=2), encoding="utf-8")

            # Initially none of the community presets are installed
            initial_presets = list_community_presets(home=home)
            self.assertEqual(len(initial_presets), 4)
            self.assertTrue(all(not p["installed"] for p in initial_presets))

            # 1) Install `filesystem`: creates <home>/sandbox, backs up v1, preserves custom_demo & meta
            res_fs = install_mcp_preset(home, "filesystem")
            self.assertEqual(res_fs["status"], "installed")
            self.assertTrue(res_fs["changed"])
            sandbox_dir = home / "sandbox"
            self.assertTrue(sandbox_dir.is_dir())
            self.assertEqual(res_fs["sandbox_dir"], str(sandbox_dir))
            self.assertEqual(res_fs["entry"]["command"], "npx")
            self.assertEqual(
                res_fs["entry"]["args"],
                ["-y", "@modelcontextprotocol/server-filesystem", str(sandbox_dir)],
            )

            v1_backup = home / "_versions" / "mcp_servers_v1.json"
            self.assertTrue(v1_backup.is_file())
            self.assertEqual(res_fs["backup_path"], str(v1_backup))
            self.assertEqual(json.loads(v1_backup.read_text(encoding="utf-8")), original_payload)

            merged = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["meta"], {"owner": "local-test"})
            self.assertIn("custom_demo", merged["mcpServers"])
            self.assertIn("filesystem", merged["mcpServers"])

            # Ensure McpClientManager can parse the merged config cleanly
            loaded = McpClientManager(home=home).load_config()
            self.assertEqual({c.name for c in loaded}, {"custom_demo", "filesystem"})

            # 2) Re-installing without overwrite is a no-op and creates no extra backup
            res_dup = install_mcp_preset(home, "filesystem", overwrite=False)
            self.assertEqual(res_dup["status"], "already_exists")
            self.assertFalse(res_dup["changed"])
            self.assertIsNone(res_dup["backup_path"])
            self.assertFalse((home / "_versions" / "mcp_servers_v2.json").exists())

            # 3) Re-installing with overwrite=True updates config and creates v2 backup
            res_ow = install_mcp_preset(home, "filesystem", overwrite=True)
            self.assertEqual(res_ow["status"], "overwritten")
            self.assertTrue(res_ow["changed"])
            v2_backup = home / "_versions" / "mcp_servers_v2.json"
            self.assertTrue(v2_backup.is_file())
            self.assertEqual(res_ow["backup_path"], str(v2_backup))

            # 4) Unknown preset raises ValueError
            with self.assertRaises(ValueError):
                install_mcp_preset(home, "nonexistent-preset")

            # 5) CLI `--add-preset` (repeatable) and `--presets --json`
            out_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                redirect_stdout(out_buf),
            ):
                rc_add = main(
                    [
                        "--home",
                        str(home),
                        "mcp",
                        "--add-preset",
                        "memory",
                        "--add-preset",
                        "context7",
                        "--json",
                    ]
                )
            self.assertEqual(rc_add, 0)
            add_data = json.loads(out_buf.getvalue())
            self.assertTrue(add_data["ok"])
            self.assertEqual([r["preset"] for r in add_data["results"]], ["memory", "context7"])
            self.assertTrue((home / "_versions" / "mcp_servers_v3.json").is_file())
            self.assertTrue((home / "_versions" / "mcp_servers_v4.json").is_file())

            presets_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                redirect_stdout(presets_buf),
            ):
                rc_list = main(["--home", str(home), "mcp", "--presets", "--json"])
            self.assertEqual(rc_list, 0)
            presets_data = json.loads(presets_buf.getvalue())
            installed_map = {item["name"]: item["installed"] for item in presets_data["presets"]}
            self.assertTrue(installed_map["filesystem"])
            self.assertTrue(installed_map["memory"])
            self.assertTrue(installed_map["context7"])
            self.assertFalse(installed_map["sequential-thinking"])

            # 6) CLI unknown preset returns non-zero
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                redirect_stderr(err_buf),
            ):
                rc_bad = main(["--home", str(home), "mcp", "--add-preset", "does-not-exist"])
            self.assertEqual(rc_bad, 1)

    def test_register_shinobu_onto_agy_bridge_and_cli(self) -> None:
        """`register_shinobu_onto_agy` and `shinobu mcp --bridge-agy/--unbridge-agy` invoke agy safely without shell=True."""
        from contextlib import redirect_stdout
        import io
        import json
        import subprocess
        import sys

        from shinobu_core.mcp import register_shinobu_onto_agy

        fake_agy = "/fake/bin/agy"
        fake_shinobu = "/fake/bin/shinobu"

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "agy_bridge_home"

            # 1) dry_run with resolved shinobu + custom home does NOT call subprocess.run
            def fake_which_both(cmd: str, path: str | None = None) -> str | None:
                if cmd == "agy":
                    return fake_agy
                if cmd == "shinobu":
                    return fake_shinobu
                return None

            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_both),
                mock.patch("shinobu_core.mcp.community.subprocess.run") as mock_run,
            ):
                res_dry = register_shinobu_onto_agy(home=home, dry_run=True)
            self.assertTrue(res_dry["ok"])
            self.assertTrue(res_dry["dry_run"])
            self.assertFalse(res_dry["executed"])
            self.assertEqual(
                res_dry["command"],
                [fake_agy, "mcp", "add", "shinobu", fake_shinobu, "--home", str(home), "mcp-serve"],
            )
            mock_run.assert_not_called()

            # 2) Fallback to `[sys.executable, "-m", "shinobu_core"]` when `shinobu` is not on PATH
            def fake_which_agy_only(cmd: str, path: str | None = None) -> str | None:
                return fake_agy if cmd == "agy" else None

            completed_ok = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Added MCP server shinobu\n", stderr=""
            )
            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_agy_only),
                mock.patch("shinobu_core.mcp.community.subprocess.run", return_value=completed_ok) as mock_run,
            ):
                res_reg = register_shinobu_onto_agy(dry_run=False)
            self.assertTrue(res_reg["ok"])
            self.assertTrue(res_reg["executed"])
            expected_cmd = [
                fake_agy,
                "mcp",
                "add",
                "shinobu",
                sys.executable,
                "-m",
                "shinobu_core",
                "mcp-serve",
            ]
            self.assertEqual(res_reg["command"], expected_cmd)
            mock_run.assert_called_once()
            call_args, call_kwargs = mock_run.call_args
            self.assertEqual(call_args[0], expected_cmd)
            self.assertFalse(call_kwargs.get("shell", False))

            # 3) unregister executes `[agy, "mcp", "remove", "shinobu"]`
            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_agy_only),
                mock.patch("shinobu_core.mcp.community.subprocess.run", return_value=completed_ok) as mock_run,
            ):
                res_unreg = register_shinobu_onto_agy(unregister=True)
            self.assertTrue(res_unreg["ok"])
            self.assertEqual(res_unreg["action"], "unregister")
            self.assertEqual(res_unreg["command"], [fake_agy, "mcp", "remove", "shinobu"])
            call_args, call_kwargs = mock_run.call_args
            self.assertEqual(call_args[0], [fake_agy, "mcp", "remove", "shinobu"])
            self.assertFalse(call_kwargs.get("shell", False))

            # 4) Missing agy binary reports ok=False and never calls subprocess.run
            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", return_value=None),
                mock.patch("shinobu_core.mcp.community.subprocess.run") as mock_run,
            ):
                res_missing = register_shinobu_onto_agy()
            self.assertFalse(res_missing["ok"])
            self.assertIsNone(res_missing["agy_path"])
            self.assertIn("not found", res_missing["error"].lower())
            mock_run.assert_not_called()

            # 4b) Remote HTTP MCP registration places flags before <name> and avoids shell=True
            remote_url = "https://cloud.shinobu.example/mcp"
            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_agy_only),
                mock.patch("shinobu_core.mcp.community.subprocess.run", return_value=completed_ok) as mock_run,
            ):
                res_remote_auth = register_shinobu_onto_agy(
                    remote_url=remote_url,
                    bearer_token="tok-secret-123",
                    dry_run=False,
                )
            expected_remote_auth_cmd = [
                fake_agy,
                "mcp",
                "add",
                "--type",
                "http",
                "--header",
                "Authorization: Bearer tok-secret-123",
                "shinobu",
                remote_url,
            ]
            self.assertTrue(res_remote_auth["ok"])
            self.assertTrue(res_remote_auth["executed"])
            self.assertEqual(res_remote_auth["command"], expected_remote_auth_cmd)
            call_args, call_kwargs = mock_run.call_args
            self.assertEqual(call_args[0], expected_remote_auth_cmd)
            self.assertFalse(call_kwargs.get("shell", False))

            with (
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_agy_only),
                mock.patch("shinobu_core.mcp.community.subprocess.run") as mock_run,
            ):
                res_remote_no_auth = register_shinobu_onto_agy(
                    name="shinobu-cloud",
                    remote_url=remote_url,
                    dry_run=True,
                )
            self.assertTrue(res_remote_no_auth["ok"])
            self.assertFalse(res_remote_no_auth["executed"])
            self.assertEqual(
                res_remote_no_auth["command"],
                [fake_agy, "mcp", "add", "--type", "http", "shinobu-cloud", remote_url],
            )
            mock_run.assert_not_called()

            # 5) CLI `--bridge-agy --dry-run --json`, `--unbridge-agy --json`, and flag conflict
            buf_bridge = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_both),
                mock.patch("shinobu_core.mcp.community.subprocess.run") as mock_run,
                redirect_stdout(buf_bridge),
            ):
                rc_bridge = main(
                    ["--home", str(home), "mcp", "--bridge-agy", "--dry-run", "--json"]
                )
            self.assertEqual(rc_bridge, 0)
            mock_run.assert_not_called()
            bridge_json = json.loads(buf_bridge.getvalue())
            self.assertTrue(bridge_json["ok"])
            self.assertTrue(bridge_json["dry_run"])
            self.assertEqual(
                bridge_json["command"],
                [fake_agy, "mcp", "add", "shinobu", fake_shinobu, "--home", str(home), "mcp-serve"],
            )

            buf_unbridge = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                mock.patch("shinobu_core.mcp.community.shutil.which", side_effect=fake_which_both),
                mock.patch("shinobu_core.mcp.community.subprocess.run", return_value=completed_ok) as mock_run,
                redirect_stdout(buf_unbridge),
            ):
                rc_unbridge = main(["--home", str(home), "mcp", "--unbridge-agy", "--json"])
            self.assertEqual(rc_unbridge, 0)
            mock_run.assert_called_once()
            unbridge_json = json.loads(buf_unbridge.getvalue())
            self.assertEqual(unbridge_json["command"], [fake_agy, "mcp", "remove", "shinobu"])

            buf_conflict = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False),
                redirect_stdout(buf_conflict),
            ):
                rc_conflict = main(
                    ["--home", str(home), "mcp", "--bridge-agy", "--unbridge-agy", "--json"]
                )
            self.assertEqual(rc_conflict, 2)
            self.assertFalse(json.loads(buf_conflict.getvalue())["ok"])

    def test_cloud_cli_build_daemon_token_hybrid_sandbox_and_approval_events(self) -> None:
        """build_daemon persists SHINOBU_TOKEN, wires hybrid + CloudSandboxTerminal + ApprovalGate events."""
        import threading
        from shinobu_core.server.cli import _daemon_http_call
        from shinobu_core.ui.client import DaemonClient

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            sandbox_dir = Path(tmp) / "cloud_sandbox"
            kernel = ShinobuKernel(home=home)
            env_patch = {
                "SHINOBU_DISABLE_PLUGINS": "1",
                "SHINOBU_TOKEN": "persistent-cloud-secret-token",
                "SHINOBU_CLOUD_ENGINE": "mock",
                "SHINOBU_MODEL": "flagship-test-model",
                "SHINOBU_SANDBOX_DIR": str(sandbox_dir),
            }
            with mock.patch.dict(os.environ, env_patch, clear=False):
                daemon, store = build_daemon(
                    kernel,
                    engine_name="hybrid",
                    port=0,
                )
            info = daemon.start(start_heartbeat=False)
            try:
                self.assertEqual(daemon.token, "persistent-cloud-secret-token")
                self.assertTrue(sandbox_dir.is_dir())
                self.assertIn("cloud_exec", kernel.tools)
                self.assertIn("cloud_read_file", kernel.tools)

                # Verify SSE event stream receives approval_requested when cloud_exec hits L1
                received_events: list[Event] = []
                sse_ready = threading.Event()
                sse_stop = threading.Event()
                client = DaemonClient(info["url"], daemon.token, timeout=5.0, stream_timeout=5.0)

                def _listen_sse() -> None:
                    for ev in client.iter_events(
                        sse_stop,
                        poll_interval=0.05,
                        on_connect=sse_ready.set,
                    ):
                        received_events.append(ev)
                        if (
                            ev.kind == "tool_call"
                            and ev.payload.get("source") == "approval_gate"
                            and ev.payload.get("phase") == "approval_requested"
                        ):
                            sse_stop.set()
                            break

                sse_thread = threading.Thread(target=_listen_sse, daemon=True)
                sse_thread.start()
                self.assertTrue(sse_ready.wait(timeout=3.0), "SSE client failed to connect")

                tool_result: list[str] = []

                def _run_l1_tool() -> None:
                    spec = kernel.tools["cloud_exec"]
                    out = spec.handler(argv=["git", "commit", "-m", "test"])
                    tool_result.append(str(out))

                t = threading.Thread(target=_run_l1_tool, daemon=True)
                t.start()
                try:
                    pending_aid = ""
                    for _ in range(60):
                        resp = _daemon_http_call(
                            info["url"], daemon.token, "GET", "/api/approvals", timeout=5.0
                        )
                        items = (
                            (resp.get("pending") if resp.get("pending") is not None else resp.get("approvals"))
                            if isinstance(resp, dict)
                            else []
                        ) or []
                        if items:
                            pending_aid = str(items[0].get("approval_id") or items[0].get("id") or "")
                            if pending_aid:
                                break
                        threading.Event().wait(0.05)
                    self.assertTrue(pending_aid, "Expected L1 cloud_exec to create a pending approval")
                    _daemon_http_call(
                        info["url"],
                        daemon.token,
                        "POST",
                        f"/api/approvals/{pending_aid}/decide",
                        {"approved": False, "actor": "test"},
                        timeout=5.0,
                    )
                finally:
                    t.join(timeout=5.0)
                    sse_stop.set()
                    sse_thread.join(timeout=3.0)

                self.assertEqual(len(tool_result), 1)
                self.assertTrue(
                    any(
                        ev.kind == "tool_call"
                        and ev.payload.get("source") == "approval_gate"
                        and ev.payload.get("phase") == "approval_requested"
                        for ev in received_events
                    ),
                    f"Expected approval_requested on SSE stream, got {[(e.kind, e.payload) for e in received_events]}",
                )
            finally:
                daemon.stop()
                store.close()

    def test_cloud_cli_telegram_wiring_and_missing_args(self) -> None:
        """shinobu telegram validates required creds and wires bridge callbacks to DaemonServer."""
        import threading
        from shinobu_core.server.cli import _daemon_http_call

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            sandbox_dir = Path(tmp) / "tg_sandbox"
            err_buf = io.StringIO()
            clean_env = {
                "SHINOBU_DISABLE_PLUGINS": "1",
                "SHINOBU_TELEGRAM_BOT_TOKEN": "",
                "SHINOBU_TELEGRAM_CHAT_ID": "",
            }
            with (
                mock.patch.dict(os.environ, clean_env, clear=False),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = main(["--home", str(home), "telegram"])
            self.assertEqual(rc, 2)
            self.assertIn("SHINOBU_TELEGRAM_BOT_TOKEN", err_buf.getvalue())

            kernel = ShinobuKernel(home=home)
            with mock.patch.dict(os.environ, {"SHINOBU_DISABLE_PLUGINS": "1"}, clear=False):
                daemon, store = build_daemon(
                    kernel,
                    engine_name="mock",
                    port=0,
                    token="tg-wire-token",
                    cloud_sandbox_dir=sandbox_dir,
                )
            info = daemon.start(start_heartbeat=False)
            from http.server import BaseHTTPRequestHandler, HTTPServer

            tg_calls: list[tuple[str, dict[str, Any]]] = []

            class _FakeTgHandler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    body = self.rfile.read(length) if length > 0 else b"{}"
                    try:
                        parsed = json.loads(body.decode("utf-8"))
                    except Exception:
                        parsed = {}
                    tg_calls.append((self.path, parsed if isinstance(parsed, dict) else {}))
                    out = json.dumps({"ok": True, "result": {"message_id": 42}}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

                def log_message(self, format: str, *args: Any) -> None:
                    pass

            tg_srv = HTTPServer(("127.0.0.1", 0), _FakeTgHandler)
            tg_port = tg_srv.server_address[1]
            tg_thread = threading.Thread(target=tg_srv.serve_forever, daemon=True)
            tg_thread.start()
            try:
                bridge = build_telegram_bridge(
                    bot_token="123456:TEST_BOT_TOKEN",
                    allowed_chat_id="99887766",
                    daemon_url=info["url"],
                    daemon_token="tg-wire-token",
                    api_base=f"http://127.0.0.1:{tg_port}",
                )
                with mock.patch.object(bridge, "push_text", return_value={"ok": True}) as mock_push:
                    res_status = bridge.handle_update(
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/status",
                            },
                        }
                    )
                    self.assertTrue(res_status.get("handled", True))
                    status_msg = mock_push.call_args[0][0]
                    self.assertIn("Shinobu Cloud Online", status_msg)
                    self.assertNotIn("State: unknown", status_msg)
                    self.assertNotIn("Engine: unknown", status_msg)

                    res_brain = bridge.handle_update(
                        {
                            "update_id": 101,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/brain",
                            },
                        }
                    )
                    self.assertEqual(res_brain.get("action"), "command_brain")
                    self.assertTrue(res_brain.get("handled"))

                    res_nodes = bridge.handle_update(
                        {
                            "update_id": 2,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/nodes",
                            },
                        }
                    )
                    self.assertTrue(res_nodes.get("handled", True))
                    self.assertIn("pure cloud mode", mock_push.call_args[0][0])

                    res_chat = bridge.handle_update(
                        {
                            "update_id": 3,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "hello cloud",
                            },
                        }
                    )
                    self.assertTrue(res_chat.get("handled", True))
                    self.assertTrue(len(mock_push.call_args[0][0]) > 0)

                # Verify hybrid daemon wiring for _on_status (/state + /api/engine) and _on_route (/api/engine/route)
                hybrid_state: dict[str, Any] = {
                    "route_mode": "auto",
                    "route_posts": [],
                    "reasoning_effort": "minimal",
                    "reasoning_posts": [],
                    "tg_calls": [],
                }

                class _FakeHybridDaemonHandler(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        if self.path == "/state":
                            body = json.dumps({"state": "idle", "mood": "happy"}).encode("utf-8")
                        elif self.path == "/api/engine":
                            body = json.dumps(
                                {
                                    "engine": "hybrid",
                                    "route_mode": hybrid_state["route_mode"],
                                    "supports_route_switch": True,
                                    "reasoning_effort": hybrid_state["reasoning_effort"],
                                    "supports_reasoning_switch": True,
                                    "online_brain_workers": ["home_pc"],
                                    "last_route": {
                                        "target": "remote",
                                        "worker_id": "home_pc",
                                        "elapsed_ms": 310,
                                    },
                                }
                            ).encode("utf-8")
                        elif self.path == "/api/approvals":
                            body = json.dumps({"pending": []}).encode("utf-8")
                        else:
                            body = json.dumps({"ok": True}).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def do_POST(self) -> None:
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        payload = json.loads(raw.decode("utf-8"))
                        if self.path == "/api/engine/route":
                            mode = str(payload.get("mode") or "")
                            hybrid_state["route_posts"].append(mode)
                            if mode not in ("auto", "cloud", "local"):
                                body = json.dumps({"ok": False, "error": "invalid mode"}).encode("utf-8")
                                self.send_response(400)
                            else:
                                prev = hybrid_state["route_mode"]
                                hybrid_state["route_mode"] = mode
                                body = json.dumps(
                                    {
                                        "ok": True,
                                        "engine": "hybrid",
                                        "mode": mode,
                                        "previous_mode": prev,
                                        "online_brain_workers": ["home_pc"],
                                    }
                                ).encode("utf-8")
                                self.send_response(200)
                        elif self.path == "/api/engine/reasoning":
                            effort = str(payload.get("reasoning_effort") or "")
                            hybrid_state["reasoning_posts"].append(effort)
                            prev_e = hybrid_state["reasoning_effort"]
                            new_e = "" if effort == "default" else effort
                            hybrid_state["reasoning_effort"] = new_e
                            body = json.dumps(
                                {
                                    "ok": True,
                                    "engine": "hybrid",
                                    "reasoning_effort": new_e,
                                    "previous_reasoning_effort": prev_e,
                                }
                            ).encode("utf-8")
                            self.send_response(200)
                        elif self.path == "/sleep":
                            sleep_calls = hybrid_state.setdefault("sleep_calls", 0) + 1
                            hybrid_state["sleep_calls"] = sleep_calls
                            if sleep_calls == 1:
                                body = json.dumps(
                                    {
                                        "ok": True,
                                        "status": "consolidated",
                                        "episode_id": "ep-test-001",
                                        "summary": "归档测试摘要",
                                        "facts_added": 2,
                                    }
                                ).encode("utf-8")
                                self.send_response(200)
                            else:
                                body = json.dumps({"ok": False, "error": "no active episode"}).encode("utf-8")
                                self.send_response(409)
                        else:
                            body = json.dumps({"ok": True}).encode("utf-8")
                            self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, format: str, *args: Any) -> None:
                        pass

                hybrid_srv = HTTPServer(("127.0.0.1", 0), _FakeHybridDaemonHandler)
                hybrid_port = hybrid_srv.server_address[1]
                hybrid_thread = threading.Thread(target=hybrid_srv.serve_forever, daemon=True)
                hybrid_thread.start()
                try:
                    hybrid_bridge = build_telegram_bridge(
                        bot_token="123456:TEST_BOT_TOKEN",
                        allowed_chat_id="99887766",
                        daemon_url=f"http://127.0.0.1:{hybrid_port}",
                        daemon_token="tg-wire-token",
                        api_base=f"http://127.0.0.1:{tg_port}",
                    )
                    st_res = hybrid_bridge.handle_update(
                        {
                            "update_id": 201,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/status",
                            },
                        }
                    )
                    self.assertIn("State: idle (happy)", st_res["reply"])
                    self.assertIn("Engine: hybrid", st_res["reply"])
                    self.assertIn("Route mode: auto", st_res["reply"])
                    self.assertIn("Reasoning: minimal", st_res["reply"])
                    self.assertIn("Brain workers: home_pc", st_res["reply"])
                    self.assertIn("Last route: remote (home_pc)", st_res["reply"])
                    self.assertTrue(hybrid_bridge._typing_indicator)

                    sleep_ok = hybrid_bridge.handle_update(
                        {
                            "update_id": 202,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/sleep",
                            },
                        }
                    )
                    self.assertEqual(hybrid_state.get("sleep_calls"), 1)
                    self.assertIn("归档测试摘要", sleep_ok["reply"])

                    sleep_idle = hybrid_bridge.handle_update(
                        {
                            "update_id": 203,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/sleep",
                            },
                        }
                    )
                    self.assertEqual(hybrid_state.get("sleep_calls"), 2)
                    self.assertIn("当前没有进行中的对话可归档", sleep_idle["reply"])

                    # Verify `shinobu mcp --bridge-agy --remote-url` + default $SHINOBU_TOKEN / explicit --bearer-token
                    import contextlib as _ctxlib
                    import io as _io
                    from unittest import mock as _mock
                    from shinobu_core.server.cli import build_daemon as _build_daemon, main as _cli_main

                    with _mock.patch.dict(os.environ, {"SHINOBU_TOKEN": "env-remote-tok"}, clear=False):
                        buf_env = _io.StringIO()
                        with _ctxlib.redirect_stdout(buf_env):
                            rc_env = _cli_main(
                                [
                                    "mcp",
                                    "--bridge-agy",
                                    "--remote-url",
                                    "https://shinobu.ts.net/mcp",
                                    "--dry-run",
                                    "--json",
                                ]
                            )
                        self.assertEqual(rc_env, 0)
                        res_env = json.loads(buf_env.getvalue())
                        self.assertTrue(res_env["ok"])
                        self.assertIn("https://shinobu.ts.net/mcp", res_env["command"])
                        self.assertTrue(
                            any("env-remote-tok" in str(arg) for arg in res_env["command"]),
                            f"expected env token in {res_env['command']!r}",
                        )

                        buf_explicit = _io.StringIO()
                        with _ctxlib.redirect_stdout(buf_explicit):
                            rc_explicit = _cli_main(
                                [
                                    "mcp",
                                    "--bridge-agy",
                                    "--remote-url",
                                    "https://shinobu.ts.net/mcp",
                                    "--bearer-token",
                                    "cli-override-tok",
                                    "--dry-run",
                                    "--json",
                                ]
                            )
                        self.assertEqual(rc_explicit, 0)
                        res_explicit = json.loads(buf_explicit.getvalue())
                        self.assertTrue(
                            any("cli-override-tok" in str(arg) for arg in res_explicit["command"]),
                            f"expected explicit token in {res_explicit['command']!r}",
                        )

                    # Verify `build_daemon` wires `consolidator_engine=cloud_fallback` and `SHINOBU_AUTO_SLEEP_IDLE_SEC`
                    from shinobu_core.core.kernel import ShinobuKernel as _ShinobuKernel

                    with tempfile.TemporaryDirectory() as _auto_tmp:
                        _auto_home = Path(_auto_tmp)
                        _auto_kernel = _ShinobuKernel(home=_auto_home)
                        _fallback_eng = object()

                        class _FakeHybridWithFallback:
                            cloud_fallback = _fallback_eng

                        with _mock.patch.dict(
                            os.environ,
                            {"SHINOBU_AUTO_SLEEP_IDLE_SEC": "120"},
                            clear=False,
                        ), _mock.patch(
                            "shinobu_core.server.cli._build_engine",
                            return_value=_FakeHybridWithFallback(),
                        ):
                            _d, _d_store = _build_daemon(_auto_kernel, engine_name="echo", port=0)
                            try:
                                self.assertIs(_d.orchestrator.consolidator_engine, _fallback_eng)
                                self.assertEqual(_d.heartbeat.auto_sleep_after_sec, 120.0)
                                self.assertIsNotNone(_d.heartbeat._on_auto_sleep)
                                with _mock.patch.object(
                                    _d.orchestrator,
                                    "sleep_and_consolidate",
                                    return_value="consolidated",
                                ) as _mock_sleep:
                                    # No active episode yet -> skip
                                    self.assertIsNone(_d.heartbeat._on_auto_sleep())
                                    _mock_sleep.assert_not_called()
                                    # Active episode with 0 turns -> skip
                                    _ep = _d.orchestrator.ensure_episode()
                                    self.assertIsNone(_d.heartbeat._on_auto_sleep())
                                    _mock_sleep.assert_not_called()
                                    # Active episode with >=1 turn -> triggers sleep_and_consolidate
                                    _d.orchestrator.store.append_turn(_ep.episode_id, "user", "hi")
                                    self.assertEqual(_d.heartbeat._on_auto_sleep(), "consolidated")
                                    _mock_sleep.assert_called_once()
                            finally:
                                _d_store.close()

                    think_cb = hybrid_bridge.handle_update(
                        {
                            "update_id": 204,
                            "callback_query": {
                                "id": "cbq_hybrid_think_high",
                                "from": {"id": 99887766},
                                "message": {
                                    "message_id": 34,
                                    "chat": {"id": 99887766, "type": "private"},
                                    "text": "think panel",
                                },
                                "data": "think:high",
                            },
                        }
                    )
                    self.assertTrue(think_cb["handled"])
                    self.assertTrue(think_cb["ok"])
                    self.assertEqual(think_cb["effort"], "high")
                    self.assertEqual(hybrid_state["reasoning_posts"], ["high"])

                    # Verify SHINOBU_TELEGRAM_TYPING=0 disables typing_indicator and openai_compat env wiring
                    prev_typing = os.environ.get("SHINOBU_TELEGRAM_TYPING")
                    prev_effort = os.environ.get("SHINOBU_REASONING_EFFORT")
                    prev_base = os.environ.get("SHINOBU_BASE_URL")
                    prev_model = os.environ.get("SHINOBU_MODEL")
                    prev_fallbacks = os.environ.get("SHINOBU_FALLBACK_MODELS")
                    prev_retry_max = os.environ.get("SHINOBU_RETRY_MAX_ATTEMPTS")
                    prev_retry_delay = os.environ.get("SHINOBU_RETRY_BASE_DELAY")
                    try:
                        os.environ["SHINOBU_TELEGRAM_TYPING"] = "0"
                        quiet_bridge = build_telegram_bridge(
                            bot_token="123456:TEST_BOT_TOKEN",
                            allowed_chat_id="99887766",
                            daemon_url=f"http://127.0.0.1:{hybrid_port}",
                            daemon_token="tg-wire-token",
                            api_base=f"http://127.0.0.1:{tg_port}",
                        )
                        self.assertFalse(quiet_bridge._typing_indicator)

                        from shinobu_core.server.cli import _build_engine
                        os.environ["SHINOBU_BASE_URL"] = "http://127.0.0.1:11434/v1"
                        os.environ["SHINOBU_MODEL"] = "gemini-3-pro"
                        os.environ["SHINOBU_REASONING_EFFORT"] = "minimal"
                        os.environ["SHINOBU_FALLBACK_MODELS"] = " google/gemini-2.5-flash , , google/gemini-2.5-pro "
                        os.environ["SHINOBU_RETRY_MAX_ATTEMPTS"] = "4"
                        os.environ["SHINOBU_RETRY_BASE_DELAY"] = "0.25"
                        eng_built = _build_engine(ShinobuKernel(home=home), "openai_compat")
                        self.assertEqual(getattr(eng_built, "reasoning_effort", ""), "minimal")
                        self.assertEqual(
                            list(eng_built.fallback_models),
                            ["google/gemini-2.5-flash", "google/gemini-2.5-pro"],
                        )
                        self.assertEqual(
                            getattr(eng_built, "retry_max_attempts", getattr(eng_built, "_retry_max_attempts", None)),
                            4,
                        )
                        self.assertAlmostEqual(
                            getattr(eng_built, "retry_base_delay_sec", getattr(eng_built, "_retry_base_delay_sec", None)),
                            0.25,
                        )
                    finally:
                        for k, v in (
                            ("SHINOBU_TELEGRAM_TYPING", prev_typing),
                            ("SHINOBU_REASONING_EFFORT", prev_effort),
                            ("SHINOBU_BASE_URL", prev_base),
                            ("SHINOBU_MODEL", prev_model),
                            ("SHINOBU_FALLBACK_MODELS", prev_fallbacks),
                            ("SHINOBU_RETRY_MAX_ATTEMPTS", prev_retry_max),
                            ("SHINOBU_RETRY_BASE_DELAY", prev_retry_delay),
                        ):
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v

                    cb_res = hybrid_bridge.handle_update(
                        {
                            "update_id": 202,
                            "callback_query": {
                                "id": "cbq_hybrid_cloud",
                                "from": {"id": 99887766},
                                "message": {
                                    "message_id": 33,
                                    "chat": {"id": 99887766, "type": "private"},
                                    "text": "brain panel",
                                },
                                "data": "brain:cloud",
                            },
                        }
                    )
                    self.assertTrue(cb_res["handled"])
                    self.assertTrue(cb_res["ok"])
                    self.assertEqual(cb_res["mode"], "cloud")
                    self.assertEqual(hybrid_state["route_posts"], ["cloud"])

                    st_after = hybrid_bridge.handle_update(
                        {
                            "update_id": 203,
                            "message": {
                                "chat": {"id": 99887766, "type": "private"},
                                "text": "/status",
                            },
                        }
                    )
                    self.assertIn("Route mode: cloud", st_after["reply"])
                finally:
                    hybrid_srv.shutdown()
                    hybrid_srv.server_close()

                # Test approval callback through daemon HTTP endpoint using real cloud_exec L1 request
                exec_out: list[str] = []

                def _req_approval() -> None:
                    out = kernel.tools["cloud_exec"].handler(argv=["git", "commit", "-m", "tg"])
                    exec_out.append(str(out))

                t = threading.Thread(target=_req_approval, daemon=True)
                t.start()
                try:
                    captured_aid = ""
                    for _ in range(60):
                        resp = _daemon_http_call(
                            info["url"], "tg-wire-token", "GET", "/api/approvals", timeout=5.0
                        )
                        pending = (
                            (resp.get("pending") if resp.get("pending") is not None else resp.get("approvals"))
                            if isinstance(resp, dict)
                            else []
                        ) or []
                        if pending:
                            captured_aid = str(pending[0].get("approval_id") or pending[0].get("id") or "")
                            if captured_aid:
                                break
                        threading.Event().wait(0.05)
                    self.assertTrue(captured_aid)
                    bridge.handle_update(
                        {
                            "update_id": 4,
                            "callback_query": {
                                "id": "cbq_1",
                                "message": {
                                    "message_id": 10,
                                    "chat": {"id": 99887766, "type": "private"},
                                },
                                "data": f"reject:{captured_aid}",
                            },
                        }
                    )
                finally:
                    t.join(timeout=5.0)
                self.assertEqual(len(exec_out), 1)
                self.assertIn("denied", exec_out[0].lower())
                self.assertTrue(any("answerCallbackQuery" in p for p, _ in tg_calls))
            finally:
                tg_srv.shutdown()
                tg_srv.server_close()
                daemon.stop()
                store.close()

    def test_cloud_cli_node_wiring_and_missing_args(self) -> None:
        """shinobu node validates --cloud-url and registers/unregisters with cloud daemon."""
        from shinobu_core.server.cli import _daemon_http_call

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shinobu_home"
            err_buf = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_DISABLE_PLUGINS": "1", "SHINOBU_CLOUD_URL": ""},
                    clear=False,
                ),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = main(["--home", str(home), "node"])
            self.assertEqual(rc, 2)
            self.assertIn("--cloud-url", err_buf.getvalue())

            kernel = ShinobuKernel(home=home)
            with mock.patch.dict(
                os.environ,
                {"SHINOBU_DISABLE_PLUGINS": "1", "SHINOBU_CLOUD_ENGINE": "mock"},
                clear=False,
            ):
                daemon, store = build_daemon(
                    kernel,
                    engine_name="hybrid",
                    port=0,
                    token="node-wire-token",
                )
            info = daemon.start(start_heartbeat=False)
            try:
                node_server, node_client = build_edge_node(
                    kernel,
                    cloud_url=info["url"],
                    cloud_token="node-wire-token",
                    node_token="edge-secret-token",
                    worker_id="home_pc_test",
                    bind_host="127.0.0.1",
                    port=0,
                    engine_name="mock",
                    allowed_roots=[Path(tmp)],
                    heartbeat_sec=5.0,
                )
                srv_info = node_server.start()
                try:
                    actual_url = (
                        srv_info.get("url")
                        if isinstance(srv_info, dict)
                        else getattr(node_server, "url", "")
                    )
                    if actual_url:
                        node_client.advertise_url = actual_url
                    node_client.start()
                    resp = _daemon_http_call(
                        info["url"], "node-wire-token", "GET", "/api/workers", timeout=5.0
                    )
                    workers = resp.get("workers", []) if isinstance(resp, dict) else []
                    ids = [w.get("worker_id") for w in workers]
                    self.assertIn("home_pc_test", ids)
                finally:
                    node_client.stop()
                    node_server.stop()
                resp_after = _daemon_http_call(
                    info["url"], "node-wire-token", "GET", "/api/workers", timeout=5.0
                )
                workers_after = resp_after.get("workers", []) if isinstance(resp_after, dict) else []
                self.assertNotIn("home_pc_test", [w.get("worker_id") for w in workers_after])
            finally:
                daemon.stop()
                store.close()

    def test_cloud_deploy_suite_whitehat_complete(self) -> None:
        """deploy/ contains systemd unit, GCS backup script, and GCP setup guide with required sections."""
        public_root = Path(__file__).resolve().parents[1]
        service_path = public_root / "deploy" / "shinobu-cloud.service"
        backup_path = public_root / "deploy" / "backup_sqlite_gcs.sh"
        guide_path = public_root / "deploy" / "gcp_setup.md"

        self.assertTrue(service_path.is_file())
        self.assertTrue(backup_path.is_file())
        self.assertTrue(guide_path.is_file())

        service_text = service_path.read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1", service_text)
        self.assertIn("--engine hybrid", service_text)
        self.assertIn("--cloud-sandbox", service_text)

        backup_text = backup_path.read_text(encoding="utf-8")
        self.assertIn("sqlite3", backup_text)
        self.assertIn("gcloud storage cp", backup_text)

        guide_text = guide_path.read_text(encoding="utf-8")
        for keyword in ("Standard Tier", "200 GiB", "Tailscale", "BotFather", "$35"):
            self.assertIn(keyword, guide_text)

    def test_build_telegram_bridge_wires_session_new_and_menu_env(self) -> None:
        from unittest import mock
        from shinobu_core.server.cli import build_telegram_bridge

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHINOBU_TELEGRAM_MENU", None)
            bridge = build_telegram_bridge(
                bot_token="123456:TESTTOKEN",
                allowed_chat_id=987654321,
                daemon_url="http://127.0.0.1:8787",
                daemon_token="secret-token",
            )
            self.assertTrue(bridge._register_commands)
            self.assertIsNotNone(bridge._on_session)
            self.assertIsNotNone(bridge._on_new_session)

            with mock.patch(
                "shinobu_core.server.cli._daemon_http_call",
                return_value={"active_episode": None, "recent_closed": []},
            ) as m_call:
                res = bridge._on_session()  # type: ignore[misc]
                self.assertEqual(res, {"active_episode": None, "recent_closed": []})
                m_call.assert_called_once_with(
                    "http://127.0.0.1:8787",
                    "secret-token",
                    "GET",
                    "/api/episodes?limit=5",
                    timeout=10.0,
                )

            with mock.patch(
                "shinobu_core.server.cli._daemon_http_call",
                return_value={"ok": True, "status": "archived", "closed_episode_id": "ep_1", "turn_count": 2},
            ) as m_new:
                res_new = bridge._on_new_session()  # type: ignore[misc]
                self.assertEqual(res_new["status"], "archived")
                m_new.assert_called_once_with(
                    "http://127.0.0.1:8787",
                    "secret-token",
                    "POST",
                    "/api/episodes/new",
                    {},
                    timeout=15.0,
                )

        with mock.patch.dict(os.environ, {"SHINOBU_TELEGRAM_MENU": "0"}, clear=False):
            bridge_no_menu = build_telegram_bridge(
                bot_token="123456:TESTTOKEN",
                allowed_chat_id=987654321,
                daemon_url="http://127.0.0.1:8787",
                daemon_token="secret-token",
            )
            self.assertFalse(bridge_no_menu._register_commands)

    def test_ui_remote_mode_and_local_fallback(self) -> None:
        """shinobu ui supports --remote-url/--token & env vars without starting local daemon."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)

            # 1) Explicit CLI flags: HTTPS remote URL + token, --no-open --json
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch("shinobu_core.ui.client.discover_endpoint") as m_disc,
                mock.patch("shinobu_core.server.cli.build_daemon") as m_build,
                mock.patch("shinobu_core.server.cli.webbrowser.open", return_value=True) as m_open,
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main(
                        [
                            "ui",
                            "--remote-url",
                            "https://example.invalid/",
                            "--token",
                            "tok remote+1",
                            "--no-open",
                            "--json",
                        ],
                        kernel=kernel,
                    )
                self.assertEqual(code, 0)
                data = json.loads(buf.getvalue())
                self.assertEqual(data["status"], "ready")
                self.assertTrue(data["remote"])
                self.assertFalse(data["started"])
                self.assertFalse(data["opened"])
                self.assertEqual(data["url"], "https://example.invalid")
                self.assertEqual(data["console_url"], "https://example.invalid/#token=tok%20remote%2B1")
                m_disc.assert_not_called()
                m_build.assert_not_called()
                m_open.assert_not_called()

            # 2) Env vars fallback (SHINOBU_REMOTE_URL + SHINOBU_CLOUD_TOKEN) opens browser
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SHINOBU_REMOTE_URL": "https://example.invalid",
                        "SHINOBU_CLOUD_TOKEN": "tok-cloud-env",
                        "SHINOBU_TOKEN": "tok-fallback-ignored",
                    },
                    clear=False,
                ),
                mock.patch("shinobu_core.server.cli.webbrowser.open", return_value=True) as m_open,
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main(["ui", "--json"], kernel=kernel)
                self.assertEqual(code, 0)
                data = json.loads(buf.getvalue())
                self.assertTrue(data["opened"])
                self.assertEqual(data["console_url"], "https://example.invalid/#token=tok-cloud-env")
                m_open.assert_called_once_with("https://example.invalid/#token=tok-cloud-env")

            # 3) Plaintext non-loopback HTTP URL rejected
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch("shinobu_core.server.cli.webbrowser.open") as m_open,
            ):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = main(
                        ["ui", "--remote-url", "http://example.invalid:18790", "--token", "tok"],
                        kernel=kernel,
                    )
                self.assertEqual(code, 1)
                self.assertIn("Error:", err.getvalue())
                m_open.assert_not_called()

            # 4) Missing token rejected
            with mock.patch.dict(
                os.environ,
                {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                clear=False,
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main(["ui", "--remote-url", "https://example.invalid", "--json"], kernel=kernel)
                self.assertEqual(code, 1)
                err_data = json.loads(buf.getvalue())
                self.assertEqual(err_data["status"], "error")
                self.assertIn("token", err_data["error"].lower())

            # 5) Local fallback preserved when --remote-url and SHINOBU_REMOTE_URL unset
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch("shinobu_core.ui.client.discover_endpoint", return_value=None) as m_disc,
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main(["ui", "--json"], kernel=kernel)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(buf.getvalue())["status"], "not_running")
                m_disc.assert_called_once_with(kernel.home)

    def test_pet_remote_mode_and_local_fallback(self) -> None:
        """shinobu pet wires --remote-url/--token with allow_remote=True and keeps local mode intact."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)

            # 1) Explicit remote flags pass allow_remote=True and never touch local daemon
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch("shinobu_core.ui.client.discover_endpoint") as m_disc,
                mock.patch("shinobu_core.server.cli.build_daemon") as m_build,
                mock.patch("shinobu_core.ui.pet_window.run_pet_shell", return_value=0) as m_pet,
                mock.patch("shinobu_core.server.cli.webbrowser.open", return_value=True) as m_open,
            ):
                code = main(
                    ["pet", "--remote-url", "https://example.invalid/", "--token", "tok-pet-1"],
                    kernel=kernel,
                )
                self.assertEqual(code, 0)
                m_disc.assert_not_called()
                m_build.assert_not_called()
                m_pet.assert_called_once()
                _, kwargs = m_pet.call_args
                self.assertEqual(kwargs["url"], "https://example.invalid")
                self.assertEqual(kwargs["token"], "tok-pet-1")
                self.assertTrue(kwargs["allow_remote"])
                kwargs["open_web_cb"]()
                m_open.assert_called_once_with("https://example.invalid/#token=tok-pet-1")

            # 2) Env vars fallback (SHINOBU_REMOTE_URL + SHINOBU_TOKEN)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SHINOBU_REMOTE_URL": "https://example.invalid",
                        "SHINOBU_CLOUD_TOKEN": "",
                        "SHINOBU_TOKEN": "tok-env-pet",
                    },
                    clear=False,
                ),
                mock.patch("shinobu_core.ui.pet_window.run_pet_shell", return_value=0) as m_pet,
            ):
                code = main(["pet"], kernel=kernel)
                self.assertEqual(code, 0)
                _, kwargs = m_pet.call_args
                self.assertEqual(kwargs["url"], "https://example.invalid")
                self.assertEqual(kwargs["token"], "tok-env-pet")
                self.assertTrue(kwargs["allow_remote"])

            # 3) Invalid remote URL or missing token fails fast before launching Qt shell
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch("shinobu_core.ui.pet_window.run_pet_shell") as m_pet,
            ):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code_bad_url = main(
                        ["pet", "--remote-url", "http://example.invalid:18790", "--token", "tok"],
                        kernel=kernel,
                    )
                    code_no_tok = main(
                        ["pet", "--remote-url", "https://example.invalid"],
                        kernel=kernel,
                    )
                self.assertEqual(code_bad_url, 1)
                self.assertEqual(code_no_tok, 1)
                m_pet.assert_not_called()

            # 4) Local mode without --remote-url uses discover_endpoint and does not force allow_remote
            with (
                mock.patch.dict(
                    os.environ,
                    {"SHINOBU_REMOTE_URL": "", "SHINOBU_CLOUD_TOKEN": "", "SHINOBU_TOKEN": ""},
                    clear=False,
                ),
                mock.patch(
                    "shinobu_core.ui.client.discover_endpoint",
                    return_value={"url": "http://127.0.0.1:18790", "token": "local-tok"},
                ) as m_disc,
                mock.patch("shinobu_core.ui.pet_window.run_pet_shell", return_value=0) as m_pet,
            ):
                code = main(["pet"], kernel=kernel)
                self.assertEqual(code, 0)
                m_disc.assert_called_once_with(kernel.home)
                _, kwargs = m_pet.call_args
                self.assertEqual(kwargs["url"], "http://127.0.0.1:18790")
                self.assertEqual(kwargs["token"], "local-tok")
                self.assertNotIn("allow_remote", kwargs)

    def test_t05_daemon_wiring_auto_recall_presence_and_briefing(self) -> None:
        """build_daemon wires auto-recall limit, PresenceEvaluator sensor, and daily briefing."""
        from shinobu_core.cloud.presence import PresenceEvaluator

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)

            # 1) Defaults: auto_recall_limit=4, presence enabled, briefing disabled
            with mock.patch.dict(
                os.environ,
                {
                    "SHINOBU_DISABLE_PLUGINS": "1",
                    "SHINOBU_AUTO_RECALL_LIMIT": "",
                    "SHINOBU_PRESENCE_ENABLED": "",
                    "SHINOBU_DAILY_BRIEFING_HOUR": "",
                },
                clear=False,
            ):
                daemon_def, store_def = build_daemon(kernel, engine_name="mock", port=0)
                try:
                    self.assertEqual(daemon_def.orchestrator.auto_recall_limit, 4)
                    assert daemon_def.heartbeat is not None
                    self.assertTrue(
                        any(isinstance(s, PresenceEvaluator) for s in daemon_def.heartbeat._sensors)
                    )
                    self.assertIsNone(daemon_def.heartbeat.daily_briefing_hour)
                    self.assertIsNone(daemon_def.heartbeat._briefing_builder)
                finally:
                    daemon_def.stop()
                    store_def.close()

            # 2) Overrides: auto_recall=0, presence=0, daily briefing=9 with journal + pending schedule
            journals_dir = home / "memory" / "journals"
            journals_dir.mkdir(parents=True, exist_ok=True)
            (journals_dir / "2026-11-22.md").write_text(
                "# 2026-11-22\n今天完成了云端主动触达联调。\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "SHINOBU_DISABLE_PLUGINS": "1",
                    "SHINOBU_AUTO_RECALL_LIMIT": "0",
                    "SHINOBU_PRESENCE_ENABLED": "0",
                    "SHINOBU_DAILY_BRIEFING_HOUR": "9",
                    "SHINOBU_TZ_OFFSET_HOURS": "9",
                },
                clear=False,
            ):
                daemon_ov, store_ov = build_daemon(kernel, engine_name="mock", port=0)
                try:
                    self.assertEqual(daemon_ov.orchestrator.auto_recall_limit, 0)
                    assert daemon_ov.heartbeat is not None
                    self.assertFalse(
                        any(isinstance(s, PresenceEvaluator) for s in daemon_ov.heartbeat._sensors)
                    )
                    self.assertEqual(daemon_ov.heartbeat.daily_briefing_hour, 9)
                    self.assertEqual(daemon_ov.heartbeat.tz_offset_hours, 9.0)
                    store_ov.add_schedule(
                        "2099-01-01T09:00:00Z",
                        "喝水提醒",
                    )
                    builder = daemon_ov.heartbeat._briefing_builder
                    self.assertIsNotNone(builder)
                    assert builder is not None
                    briefing = builder()
                    self.assertIn("今天完成了云端主动触达联调", briefing)
                    self.assertIn("1", briefing)
                finally:
                    daemon_ov.stop()
                    store_ov.close()

    def test_t05_telegram_proactive_sse_and_quiet_hours_wiring(self) -> None:
        """_cmd_telegram wires quiet hours, DaemonClient(allow_remote=True), and EventStreamWorker."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)

            # 1) build_telegram_bridge respects SHINOBU_QUIET_HOURS & SHINOBU_TZ_OFFSET_HOURS
            with mock.patch.dict(
                os.environ,
                {"SHINOBU_QUIET_HOURS": "23:30-07:00", "SHINOBU_TZ_OFFSET_HOURS": "9"},
                clear=False,
            ):
                bridge = build_telegram_bridge(
                    bot_token="123:tok",
                    allowed_chat_id="999",
                    daemon_url="http://127.0.0.1:18790",
                    daemon_token="secret",
                )
            self.assertEqual(bridge._quiet_hours_spec, "23:30-07:00")
            self.assertEqual(bridge._tz_offset_hours, 9.0)

            # 2) _cmd_telegram subscribes to SSE and forwards only bubble events, stopping cleanly
            captured: dict[str, object] = {}

            class _FakeBridge:
                def __init__(self) -> None:
                    self.pushed: list[object] = []
                    self.started = False
                    self.stopped = False

                def start_polling(self) -> None:
                    self.started = True

                def stop(self) -> None:
                    self.stopped = True

                def push_proactive_event(self, ev: object) -> dict[str, object]:
                    self.pushed.append(ev)
                    return {"sent": True}

                def push_approval_prompt(self, _item: object) -> None:
                    return None

            fake_bridge = _FakeBridge()

            class _FakeWorker:
                def __init__(self, client: object, on_event: object) -> None:
                    captured["client"] = client
                    captured["on_event"] = on_event
                    self.started = False
                    self.stopped = False

                def start(self) -> None:
                    self.started = True
                    cb = captured["on_event"]
                    assert callable(cb)
                    cb(Event(kind="state_changed", payload={"state": "idle"}))
                    cb(Event(kind="bubble", payload={"text": "时间到了", "source": "timemap"}))

                def stop(self, timeout: float = 2.0) -> None:
                    self.stopped = True

            worker_holder: list[_FakeWorker] = []

            def _make_worker(client: object, on_event: object) -> _FakeWorker:
                w = _FakeWorker(client, on_event)
                worker_holder.append(w)
                return w

            def _spy_build_bridge(**kwargs: object) -> _FakeBridge:
                captured["bridge_kwargs"] = kwargs
                return fake_bridge

            import threading as _real_threading

            class _QuickStopEvent(_real_threading.Event):
                def wait(self, timeout: float | None = None) -> bool:
                    if timeout == 0.5:
                        self.set()
                        return True
                    return super().wait(timeout=min(timeout or 0.01, 0.05))

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SHINOBU_DISABLE_PLUGINS": "1",
                        "SHINOBU_QUIET_HOURS": "01:00-08:00",
                        "SHINOBU_TZ_OFFSET_HOURS": "8",
                    },
                    clear=False,
                ),
                mock.patch("shinobu_core.server.cli.build_telegram_bridge", side_effect=_spy_build_bridge),
                mock.patch("shinobu_core.server.cli.EventStreamWorker", side_effect=_make_worker),
                mock.patch("shinobu_core.server.cli._daemon_http_call", return_value={"pending": []}),
                mock.patch("shinobu_core.server.cli.threading.Event", side_effect=_QuickStopEvent),
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = main(
                        [
                            "telegram",
                            "--bot-token",
                            "123:tok",
                            "--chat-id",
                            "999",
                            "--daemon-url",
                            "https://example.invalid",
                            "--token",
                            "tok-cloud",
                        ],
                        kernel=kernel,
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(len(worker_holder), 1)
            self.assertTrue(worker_holder[0].started)
            self.assertTrue(worker_holder[0].stopped)
            self.assertTrue(fake_bridge.stopped)
            self.assertEqual(len(fake_bridge.pushed), 1)
            pushed_ev = fake_bridge.pushed[0]
            assert isinstance(pushed_ev, Event)
            self.assertEqual(pushed_ev.kind, "bubble")
            bkw = captured["bridge_kwargs"]
            assert isinstance(bkw, dict)
            self.assertEqual(bkw["quiet_hours"], "01:00-08:00")
            self.assertEqual(bkw["tz_offset_hours"], 8.0)

    def test_t05_edge_node_presence_flag_and_env_wiring(self) -> None:
        """build_edge_node and `shinobu node --no-presence` wire SHINOBU_EDGE_PRESENCE cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kernel = ShinobuKernel(home=home)

            # 1) Default enabled; SHINOBU_EDGE_PRESENCE=0 disables
            with mock.patch.dict(os.environ, {"SHINOBU_EDGE_PRESENCE": ""}, clear=False):
                _srv1, cli1 = build_edge_node(
                    kernel,
                    cloud_url="https://example.invalid",
                    cloud_token="tok",
                    engine_name="none",
                    port=0,
                )
            self.assertTrue(cli1.presence_enabled)

            with mock.patch.dict(os.environ, {"SHINOBU_EDGE_PRESENCE": "0"}, clear=False):
                _srv2, cli2 = build_edge_node(
                    kernel,
                    cloud_url="https://example.invalid",
                    cloud_token="tok",
                    engine_name="none",
                    port=0,
                )
            self.assertFalse(cli2.presence_enabled)

            # 2) Parser accepts --no-presence and overrides SHINOBU_EDGE_PRESENCE=1
            parser = build_parser(kernel)
            ns_default = parser.parse_args(["node", "--cloud-url", "https://example.invalid"])
            self.assertFalse(ns_default.no_presence)
            ns_disabled = parser.parse_args(
                ["node", "--cloud-url", "https://example.invalid", "--no-presence"]
            )
            self.assertTrue(ns_disabled.no_presence)

            captured_presence: list[object] = []

            class _FakeSrv:
                def start(self) -> dict[str, object]:
                    return {"url": "http://127.0.0.1:18792"}

                def stop(self) -> None:
                    return None

            class _FakeCli:
                worker_id = "home_pc"
                advertise_url = "http://127.0.0.1:18792"

                def start(self) -> None:
                    return None

                def stop(self) -> None:
                    return None

            def _spy_build_node(*_args: object, **kwargs: object) -> tuple[_FakeSrv, _FakeCli]:
                captured_presence.append(kwargs.get("presence_enabled"))
                return _FakeSrv(), _FakeCli()

            with (
                mock.patch.dict(os.environ, {"SHINOBU_EDGE_PRESENCE": "1"}, clear=False),
                mock.patch("shinobu_core.server.cli.build_edge_node", side_effect=_spy_build_node),
                mock.patch("shinobu_core.server.cli.threading.Event.wait", side_effect=KeyboardInterrupt),
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = main(
                        [
                            "node",
                            "--cloud-url",
                            "https://example.invalid",
                            "--cloud-token",
                            "tok",
                            "--no-presence",
                        ],
                        kernel=kernel,
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(captured_presence, [False])

    def test_gate2_whitehat_purity_scan(self) -> None:
        """Gate 2: shinobu-public/ contains zero private references or local user-home paths."""
        public_root = Path(__file__).resolve().parents[1]
        forbidden_tokens = (
            "".join(["shino", "bu_private"]),
            "".join(["shino", "bu-private"]),
            "".join(["acero", "la_orion"]),
            "".join(["acero", "la-orion"]),
            "".join(["c:\\", "users\\"]),
            "".join(["c:/", "users/"]),
        )
        skip_dirs = {"__pycache__", ".git", ".pytest_cache", "_versions", "build", "dist"}
        scan_exts = {".py", ".toml", ".md", ".txt", ".json", ".yml", ".yaml", ".html", ".js", ".css"}

        violations: list[str] = []
        for path in public_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs or part.endswith(".egg-info") for part in path.parts):
                continue
            if path.suffix.lower() not in scan_exts:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore").lower()
            for token in forbidden_tokens:
                if token in content:
                    rel = path.relative_to(public_root)
                    violations.append(f"{rel}: contains forbidden token {token!r}")

        self.assertEqual(violations, [], "White-hat purity scan failed:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
