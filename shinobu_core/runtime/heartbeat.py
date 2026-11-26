# -*- coding: utf-8 -*-
"""
Zero-token local heartbeat loop for Shinobu.

Periodically checks:
  1. Due Timemap schedules in SQLite (`MemoryStore.pop_due_schedules`),
  2. Read-only local sensors (fault-isolated per sensor),
  3. Idle timeout state transitions (`idle` -> `donut` -> `sleepy`),
and publishes resulting `Event` objects without making any LLM API calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any, Callable, Iterable

from shinobu_core.memory.store import MemoryStore
from shinobu_core.protocol.events import Event, EventKind
from shinobu_core.runtime.state_machine import PetStateMachine

logger = logging.getLogger("shinobu_core.runtime.heartbeat")

SensorCallable = Callable[[], Any]
PublishCallable = Callable[[Event], Any]


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalize_sensor_events(raw: Any) -> list[Event]:
    if raw is None:
        return []
    if isinstance(raw, Event):
        return [raw]
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, dict)):
        out: list[Event] = []
        for item in raw:
            if isinstance(item, Event):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("kind"), str):
                out.append(
                    Event(
                        kind=item["kind"],
                        payload=dict(item.get("payload") or {}),
                        episode_id=str(item.get("episode_id") or ""),
                    )
                )
        return out
    return []


class HeartbeatLoop:
    """
    Background zero-token heartbeat loop driving Timemap reminders,
    read-only sensors, and idle desktop-pet state transitions.
    """

    def __init__(
        self,
        store: MemoryStore | None,
        state_machine: PetStateMachine,
        sensors: Iterable[SensorCallable] | None = None,
        publish: PublishCallable | None = None,
        interval_sec: float = 30.0,
        *,
        donut_after_sec: float = 180.0,
        sleepy_after_sec: float = 900.0,
        auto_sleep_after_sec: float = 0.0,
        on_auto_sleep: Callable[[], Any] | None = None,
        auto_archive_after_sec: float = 0.0,
        on_auto_archive: Callable[[], Any] | None = None,
        daily_briefing_hour: int | None = None,
        tz_offset_hours: float = 8.0,
        briefing_builder: Callable[[], str | None] | None = None,
    ) -> None:
        self.store: MemoryStore | None = store
        self.state_machine: PetStateMachine = state_machine
        self._sensors: list[SensorCallable] = list(sensors) if sensors else []
        self._publish: PublishCallable | None = publish
        self.interval_sec: float = max(0.01, float(interval_sec))
        self.donut_after_sec: float = float(donut_after_sec)
        self.sleepy_after_sec: float = float(sleepy_after_sec)
        self.auto_sleep_after_sec: float = max(0.0, float(auto_sleep_after_sec))
        self._on_auto_sleep: Callable[[], Any] | None = on_auto_sleep
        self._auto_sleep_done: bool = False
        self.auto_archive_after_sec: float = max(0.0, float(auto_archive_after_sec))
        self._on_auto_archive: Callable[[], Any] | None = on_auto_archive
        self._auto_archive_done: bool = False

        if daily_briefing_hour is not None:
            hour = int(daily_briefing_hour)
            if not (0 <= hour <= 23):
                raise ValueError("daily_briefing_hour must be between 0 and 23")
            self.daily_briefing_hour: int | None = hour
        else:
            self.daily_briefing_hour = None
        self.tz_offset_hours: float = float(tz_offset_hours)
        self._tz = timezone(timedelta(hours=self.tz_offset_hours))
        self._briefing_builder: Callable[[], str | None] | None = briefing_builder
        self._last_briefing_date: str | None = None

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_count: int = 0

    @property
    def publish(self) -> PublishCallable | None:
        with self._lock:
            return self._publish

    @publish.setter
    def publish(self, fn: PublishCallable | None) -> None:
        with self._lock:
            self._publish = fn

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def tick_count(self) -> int:
        with self._lock:
            return self._tick_count

    def add_sensor(self, sensor: SensorCallable) -> None:
        """Register an additional read-only sensor callable."""
        if not callable(sensor):
            raise TypeError("sensor must be callable")
        with self._lock:
            self._sensors.append(sensor)

    def _emit(self, event: Event) -> None:
        pub = self.publish
        if pub is None:
            return
        try:
            pub(event)
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.warning("Heartbeat publish callback failed: %s", exc)

    def tick_once(self, now_iso: str | None = None) -> list[Event]:
        """
        Execute one pure-local 0-token heartbeat cycle.

        Order:
          1. Pop due Timemap schedules from `store` and emit `bubble` events.
          2. Run each read-only sensor (errors isolated per sensor).
          3. Apply idle transitions (`idle` -> `donut` -> `sleepy`).
          4. Publish every produced event via `self.publish`.
        """
        events: list[Event] = []
        now_str = (now_iso or "").strip() or _utc_now_iso()

        # 1. Timemap due schedule checks
        if self.store is not None:
            try:
                due_items = self.store.pop_due_schedules(now_str)
            except Exception as exc:
                logger.warning("Timemap poll failed: %s", exc)
                due_items = []
            for item in due_items:
                item_payload = item.get("payload")
                if not isinstance(item_payload, dict):
                    item_payload = {}
                text = str(item_payload.get("text") or item.get("label") or "")
                events.append(
                    Event(
                        kind=EventKind.BUBBLE,
                        payload={
                            "source": "timemap",
                            "schedule_id": item.get("id"),
                            "label": item.get("label", ""),
                            "trigger_at": item.get("trigger_at", ""),
                            "text": text,
                            "data": item_payload,
                        },
                    )
                )

        # 1b. Optional daily briefing bubble (disabled by default: 0-token)
        if self.daily_briefing_hour is not None:
            local_dt = self._parse_local_datetime(now_str)
            date_key = local_dt.strftime("%Y-%m-%d")
            should_brief = False
            if local_dt.hour == self.daily_briefing_hour:
                with self._lock:
                    if self._last_briefing_date != date_key:
                        self._last_briefing_date = date_key
                        should_brief = True
            if should_brief:
                briefing_text = self._build_daily_briefing(date_key)
                if briefing_text:
                    events.append(
                        Event(
                            kind=EventKind.BUBBLE,
                            payload={
                                "source": "briefing",
                                "label": "daily_briefing",
                                "date": date_key,
                                "text": briefing_text,
                            },
                        )
                    )

        # 2. Read-only sensors (fault-isolated)
        with self._lock:
            sensors_snapshot = list(self._sensors)
        for sensor in sensors_snapshot:
            try:
                raw = sensor()
                events.extend(_normalize_sensor_events(raw))
            except Exception as exc:
                logger.warning("Heartbeat sensor %r failed: %s", sensor, exc)

        # 3. Idle state progression (0-token)
        snap = self.state_machine.snapshot()
        cur_state = snap["state"]
        idle_sec = float(snap["idle_seconds"])

        if (
            cur_state in ("idle", "donut")
            and self.sleepy_after_sec > 0
            and idle_sec >= self.sleepy_after_sec
        ):
            events.append(
                self.state_machine.transition(
                    "sleepy",
                    mood="sleepy",
                    reason="idle_sleepy",
                    touch_activity=False,
                )
            )
        elif (
            cur_state == "idle"
            and self.donut_after_sec > 0
            and idle_sec >= self.donut_after_sec
        ):
            events.append(
                self.state_machine.transition(
                    "donut",
                    reason="idle_donut",
                    touch_activity=False,
                )
            )

        # 3b. Optional idle auto-sleep consolidation (disabled by default: 0-token)
        if self.auto_sleep_after_sec > 0 and self._on_auto_sleep is not None:
            should_auto_sleep = False
            with self._lock:
                if idle_sec < self.auto_sleep_after_sec:
                    self._auto_sleep_done = False
                elif not self._auto_sleep_done:
                    self._auto_sleep_done = True
                    should_auto_sleep = True
            if should_auto_sleep:
                try:
                    self._on_auto_sleep()
                except Exception as exc:
                    logger.warning("Heartbeat auto-sleep callback failed: %s", exc)

        # 3c. Optional zero-token idle auto-archive hook (independent of auto_sleep_after_sec)
        if self.auto_archive_after_sec > 0 and self._on_auto_archive is not None:
            should_auto_archive = False
            with self._lock:
                if idle_sec < self.auto_archive_after_sec:
                    self._auto_archive_done = False
                elif not self._auto_archive_done:
                    self._auto_archive_done = True
                    should_auto_archive = True
            if should_auto_archive:
                try:
                    self._on_auto_archive()
                except Exception as exc:
                    logger.warning("Heartbeat auto-archive callback failed: %s", exc)

        with self._lock:
            self._tick_count += 1

        # 4. Publish all events
        for ev in events:
            self._emit(ev)

        return events

    def _parse_local_datetime(self, now_str: str) -> datetime:
        iso_text = now_str.strip()
        if iso_text.endswith("Z"):
            iso_text = iso_text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso_text)
        except ValueError:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._tz)

    def _build_daily_briefing(self, date_key: str) -> str | None:
        if self._briefing_builder is not None:
            try:
                built = self._briefing_builder()
            except Exception as exc:
                logger.warning("Heartbeat briefing_builder failed: %s", exc)
                return None
            text = str(built or "").strip()
            return text or None

        summary = ""
        if self.store is not None:
            try:
                summary = str(self.store.latest_closed_summary() or "").strip()
            except Exception as exc:
                logger.warning("Heartbeat briefing summary lookup failed: %s", exc)
        if summary:
            return f"早安（{date_key}）！上一段回顾：{summary}"
        return f"早安（{date_key}），新的一天开始了。"

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            try:
                self.tick_once()
            except Exception as exc:  # pragma: no cover - defensive loop guard
                logger.warning("Heartbeat loop tick failed: %s", exc)

    def start(self) -> None:
        """Start the background heartbeat daemon thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="shinobu-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the heartbeat thread to stop and wait up to `timeout` seconds."""
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread:
                self._thread = None


__all__ = ["HeartbeatLoop", "PublishCallable", "SensorCallable"]
