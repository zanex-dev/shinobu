# -*- coding: utf-8 -*-
"""Unit tests for shinobu_core protocol contracts and microkernel."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator
import unittest
from unittest import mock

from shinobu_core.core.kernel import (
    DISABLE_PLUGINS_ENV,
    DISABLED_PLUGINS_ENV,
    HOME_ENV,
    PLUGIN_GROUP,
    HookBus,
    ShinobuKernel,
    default_home,
    disabled_plugin_names,
    is_plugin_disabled,
    plugins_disabled,
)
from shinobu_core.protocol.engine import (
    EngineAdapter,
    ToolSpec,
    TurnChunk,
    TurnRequest,
)
from shinobu_core.protocol.events import (
    VALID_EVENT_KINDS,
    Event,
    EventKind,
)


class _DummyEngine:
    def __init__(self, name: str = "dummy") -> None:
        self.name = name

    def stream_turn(self, req: TurnRequest) -> Iterator[TurnChunk]:
        yield TurnChunk(delta=f"echo:{req.user_text}")
        yield TurnChunk(usage={"input_tokens": 1, "output_tokens": 2}, done=True)

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        return {"system": system, "prompt": prompt}


class _FakeEntryPoint:
    def __init__(self, name: str, loader: Any) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> Any:
        if isinstance(self._loader, BaseException):
            raise self._loader
        if callable(self._loader) and getattr(self._loader, "_is_loader", False):
            return self._loader()
        return self._loader


class TestProtocolContracts(unittest.TestCase):
    def test_event_kinds_and_roundtrip(self) -> None:
        expected = {
            "state_changed",
            "token_delta",
            "mood_changed",
            "bubble",
            "tool_call",
            "error",
            "done",
        }
        self.assertEqual(VALID_EVENT_KINDS, expected)

        ev = Event(EventKind.BUBBLE, {"text": "hello"}, 1700000000.5, "ep-42")
        self.assertTrue(ev.is_standard_kind)
        self.assertEqual(ev.episode_id, "ep-42")
        rebuilt = Event.from_dict(ev.to_dict())
        self.assertEqual(rebuilt, ev)
        self.assertIn('"kind":"bubble"', ev.to_json())

        ev_default = Event("done", {}, 1.0)
        self.assertEqual(ev_default.episode_id, "")

        with self.assertRaises(ValueError):
            Event("", {}, 0.0)

    def test_engine_protocol_and_dataclasses(self) -> None:
        tool = ToolSpec(
            name="ping",
            description="Return pong",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "pong",
        )
        self.assertEqual(tool.handler(), "pong")
        self.assertEqual(tool.to_schema()["name"], "ping")
        self.assertNotIn("handler", tool.to_schema())

        req = TurnRequest("ep-1", "sys", [{"role": "user", "content": "hi"}], "next", [tool])
        self.assertEqual(req.episode_id, "ep-1")
        self.assertEqual(len(req.tools), 1)

        empty_chunk = TurnChunk()
        self.assertEqual(empty_chunk.delta, "")
        self.assertEqual(empty_chunk.tool_calls, ())
        self.assertIsNone(empty_chunk.usage)
        self.assertFalse(empty_chunk.done)

        engine = _DummyEngine("local-test")
        self.assertIsInstance(engine, EngineAdapter)
        chunks = list(engine.stream_turn(req))
        self.assertEqual(chunks[0].delta, "echo:next")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(engine.complete_json("s", "p"), {"system": "s", "prompt": "p"})


class TestHomeAndEnvFlags(unittest.TestCase):
    def test_default_home_respects_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(HOME_ENV, None)
            self.assertEqual(default_home(), Path.home() / ".shinobu")

        with mock.patch.dict(os.environ, {HOME_ENV: "/tmp/custom-shinobu"}, clear=False):
            self.assertEqual(default_home(), Path("/tmp/custom-shinobu"))

    def test_plugin_disable_env_flags(self) -> None:
        with mock.patch.dict(
            os.environ,
            {DISABLE_PLUGINS_ENV: "true", DISABLED_PLUGINS_ENV: ""},
            clear=False,
        ):
            self.assertTrue(plugins_disabled())
            self.assertTrue(is_plugin_disabled("any_plugin"))

        with mock.patch.dict(
            os.environ,
            {DISABLE_PLUGINS_ENV: "0", DISABLED_PLUGINS_ENV: "Alpha, beta , "},
            clear=False,
        ):
            self.assertFalse(plugins_disabled())
            self.assertEqual(disabled_plugin_names(), frozenset({"alpha", "beta"}))
            self.assertTrue(is_plugin_disabled("alpha"))
            self.assertTrue(is_plugin_disabled("BETA"))
            self.assertFalse(is_plugin_disabled("gamma"))


class TestHookBus(unittest.TestCase):
    def test_priority_order_unsubscribe_and_fault_isolation(self) -> None:
        bus = HookBus()
        order: list[str] = []

        def low() -> str:
            order.append("low")
            return "low"

        def high() -> str:
            order.append("high")
            return "high"

        def broken() -> str:
            order.append("broken")
            raise RuntimeError("boom")

        unsub_low = bus.on("tick", low, priority=200)
        bus.on("tick", broken, priority=50)
        bus.on("tick", high, priority=10)

        with self.assertLogs("shinobu_core.core.kernel", level=logging.ERROR):
            results = bus.emit("tick")

        self.assertEqual(order, ["high", "broken", "low"])
        self.assertEqual(results, ["high", "low"])

        # Unsubscribe is idempotent.
        unsub_low()
        unsub_low()
        self.assertEqual(bus.handler_count("tick"), 2)
        self.assertTrue(bus.off("tick", broken))
        self.assertFalse(bus.off("tick", broken))

        order.clear()
        self.assertEqual(bus.emit("tick"), ["high"])
        self.assertEqual(order, ["high"])


class TestShinobuKernel(unittest.TestCase):
    def test_direct_registrations_and_cli_fault_isolation(self) -> None:
        kernel = ShinobuKernel(home=Path("/tmp/shinobu-unit"))
        self.assertEqual(kernel.home, Path("/tmp/shinobu-unit"))

        eng = _DummyEngine("e1")
        tool = ToolSpec("t1", "desc", {}, lambda: "ok")
        seen_cli: list[str] = []
        seen_hooks: list[int] = []

        kernel.register_engines([eng])
        kernel.register_tools([tool])
        kernel.register_sensors({"idle": lambda: 42})
        kernel.register_cli(lambda sp: seen_cli.append(sp), name="good_cli")
        kernel.register_cli(lambda sp: (_ for _ in ()).throw(RuntimeError("cli err")), name="bad_cli")
        kernel.register_hooks({"ping": lambda x: seen_hooks.append(x)})

        self.assertIs(kernel.get_engine("e1"), eng)
        self.assertIs(kernel.get_tool("t1"), tool)
        self.assertEqual(kernel.get_sensor("idle")(), 42)

        with self.assertLogs("shinobu_core.core.kernel", level=logging.ERROR):
            failed_cli = kernel.apply_cli("subparsers-obj")
        self.assertEqual(seen_cli, ["subparsers-obj"])
        self.assertEqual(failed_cli, ["bad_cli"])

        kernel.hooks.emit("ping", 7)
        self.assertEqual(seen_hooks, [7])

    def test_discover_plugins_full_hooks_and_fault_isolation(self) -> None:
        class GoodPlugin:
            def register_engines(self, k: ShinobuKernel) -> None:
                k.register_engine(_DummyEngine("good-eng"))

            def register_tools(self, k: ShinobuKernel) -> list[ToolSpec]:
                return [ToolSpec("good-tool", "ok", {}, lambda: "good")]

            def register_sensors(self, k: ShinobuKernel) -> dict[str, Any]:
                return {"window": "active"}

            def register_cli(self, subparsers: Any) -> None:
                subparsers.append("good-cli")

            def register_hooks(self, bus: HookBus) -> None:
                bus.on("greet", lambda who: f"hi {who}")

        class PartialBrokenPlugin:
            def register_engines(self, k: ShinobuKernel) -> None:
                raise RuntimeError("engine init exploded")

            def register_tools(self, k: ShinobuKernel) -> None:
                k.register_tool(ToolSpec("surviving-tool", "still works", {}, lambda: "alive"))

        eps = [
            _FakeEntryPoint("broken_import", ImportError("missing optional dep")),
            _FakeEntryPoint("partial_bad", PartialBrokenPlugin),
            _FakeEntryPoint("disabled_one", GoodPlugin),
            _FakeEntryPoint("healthy", GoodPlugin),
        ]

        kernel = ShinobuKernel()
        with mock.patch.dict(
            os.environ,
            {DISABLE_PLUGINS_ENV: "", DISABLED_PLUGINS_ENV: "disabled_one"},
            clear=False,
        ), mock.patch("importlib.metadata.entry_points", return_value=eps) as ep_mock:
            with self.assertLogs("shinobu_core.core.kernel", level=logging.ERROR):
                records = kernel.discover_plugins()

        ep_mock.assert_called_once_with(group=PLUGIN_GROUP)
        by_name = {rec.name: rec for rec in records}
        self.assertEqual(by_name["broken_import"].status, "failed")
        self.assertIn("missing optional dep", by_name["broken_import"].error or "")
        self.assertEqual(by_name["partial_bad"].status, "failed")
        self.assertIn("register_engines", by_name["partial_bad"].error or "")
        self.assertEqual(by_name["disabled_one"].status, "disabled")
        self.assertEqual(by_name["healthy"].status, "loaded")

        # Partial plugin's unaffected hook and healthy plugin both registered cleanly.
        self.assertIsNotNone(kernel.get_tool("surviving-tool"))
        self.assertIsNotNone(kernel.get_engine("good-eng"))
        self.assertIsNotNone(kernel.get_tool("good-tool"))
        self.assertEqual(kernel.get_sensor("window"), "active")

        cli_targets: list[str] = []
        self.assertEqual(kernel.apply_cli(cli_targets), [])
        self.assertEqual(cli_targets, ["good-cli"])
        self.assertEqual(kernel.hooks.emit("greet", "shinobu"), ["hi shinobu"])

    def test_discover_plugins_respects_global_disable(self) -> None:
        kernel = ShinobuKernel()
        with mock.patch.dict(
            os.environ,
            {DISABLE_PLUGINS_ENV: "1"},
            clear=False,
        ), mock.patch("importlib.metadata.entry_points") as ep_mock:
            records = kernel.discover_plugins()

        self.assertEqual(records, [])
        ep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
