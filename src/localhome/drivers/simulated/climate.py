"""A fake temperature/humidity sensor - drifts slowly toward a resting
base value, or toward `influence_target_c` while a named simulated
switch (e.g. a simulated heating/cooling valve) is on - so a simulated
thermostat setup behaves believably: open the valve in the dashboard,
watch the temperature actually follow over the next few reads instead of
sitting at a static fake number. See docs/integrations/simulated.md.

With `mimic_ht_gen3` it stands in for a Shelly H&T Gen3 on batteries, as
the admin panel's "simulated" zone sensor: the same fields (temperature
and humidity to a tenth, battery, Wi-Fi signal), the same rhythm - a
report when the temperature moves 0.5 degrees or the humidity 1% since
the last one, and a heartbeat every 2 hours on its own clock - and a
room that warms or cools at a radiant floor's pace while its zone's
valve is open, depending on the season, and drifts back when it closes.
Each report is also published in the simulated world on the real one's
topics (status/devicepower:0, status/wifi), so the admin panel's Shelly
health watches it like a real one; and with a `state_file` the room, the
last report and the heartbeat's clock carry over a restart. See
docs/shelly-gen3.md for the real device it copies.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
import zlib
from datetime import datetime
from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.simulated import world
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# The H&T Gen3 it copies (docs/shelly-gen3.md): thresholds, heartbeat.
HT_TEMP_STEP_C = 0.5
HT_HUMIDITY_STEP_PCT = 1.0
HT_HEARTBEAT_SECONDS = 7200
# A radiant floor: about half a degree an hour with the valve open, half
# that back toward the room's resting temperature with it closed.
HT_RUN_C_PER_H = 0.6
HT_DRIFT_C_PER_H = 0.3
# Where a room rests, and where an open valve takes it, by season.
HT_REST_C = {"heat": 19.5, "cool": 27.0}
HT_VALVE_TARGET_C = {"heat": 24.0, "cool": 22.0}
# A lived-in room's humidity wanders about 0.4% per root-hour and is
# pulled back toward 52% over a day: two or three 1% reports in a quiet
# day, as the real ones send (docs/shelly-gen3.md).
HT_HUMIDITY_WANDER = 0.4
HT_HUMIDITY_MEAN = 52.0
HT_HUMIDITY_PULL_H = 24.0

# One state file for every simulated sensor - see _save().
_state_lock = threading.Lock()


class SimulatedClimateDriver(PollingDriver):
    history_fields = ("temperature_c", "humidity_pct")

    def __init__(
        self,
        name: str,
        base_temperature_c: float = 20.0,
        base_humidity_pct: float = 50.0,
        influenced_by: str | None = None,
        influence_target_c: float = 26.0,
        drift_rate_c_per_min: float = 0.15,
        poll_interval_seconds: float = 10.0,
        monthly_base_c: list | None = None,
        daily_swing_c: float = 0.0,
        mimic_ht_gen3: bool = False,
        season_file: str | None = None,
        state_file: str | None = None,
    ):
        self.name = name
        self._mimic = bool(mimic_ht_gen3)
        self._season_file = season_file
        # Outdoors: the resting temperature follows the month's average
        # (12 values, January first) and swings `daily_swing_c` above and
        # below it over the day - warmest at 15:00, coldest at 03:00.
        self._monthly_base = list(monthly_base_c) if monthly_base_c and len(monthly_base_c) == 12 else None
        self._daily_swing = float(daily_swing_c or 0.0)
        if self._monthly_base:
            base_temperature_c = self._resting(datetime.now())
        self.poll_interval_seconds = poll_interval_seconds
        self._temperature = base_temperature_c
        self._humidity = base_humidity_pct
        self._base_temperature = base_temperature_c
        self._influenced_by = influenced_by
        self._influence_target = influence_target_c
        self._drift_rate = drift_rate_c_per_min
        self._last_tick = time.time()
        if self._mimic:
            self.history_fields = ("temperature_c", "humidity_pct", "battery_pct", "wifi_rssi_dbm")
            # Each simulated sensor its own battery and signal, steady
            # from one start to the next (from its name, not at random).
            seed = zlib.crc32(name.encode("utf-8"))
            self._battery = 80.0 + seed % 20
            self._rssi_base = -55.0 - (seed >> 8) % 18
            self._temperature = HT_REST_C[self._season()]
            self._reported: dict[str, Any] | None = None
            self._reported_at: float | None = None
            self._next_heartbeat = 0.0
            self._state_file = state_file
            self._restore()

    def _restore(self) -> None:
        """Where it was before the restart: the room, the last report -
        shown with its real age, as a real sensor's is - and the
        heartbeat's own clock, which kept running meanwhile."""
        if not self._state_file:
            return
        try:
            saved = load_json(self._state_file)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            saved = None
        saved = saved.get(self.name, {}) if isinstance(saved, dict) else None
        if saved == {}:
            return          # a sensor new to the file
        try:
            room, reported = saved["room"], saved["reported"]
            temperature, humidity = float(room["temperature"]), float(room["humidity"])
            battery, at = float(room["battery"]), float(room["at"])
            reported = {key: reported[key] for key in self.history_fields}
            reported_at, next_heartbeat = float(saved["reported_at"]), float(saved["next_heartbeat"])
        except (ValueError, TypeError, KeyError):
            logger.warning("Simulated sensor '%s': its saved state is unreadable, starting afresh", self.name)
            return
        self._temperature, self._humidity, self._battery, self._last_tick = temperature, humidity, battery, at
        self._reported, self._reported_at, self._next_heartbeat = reported, reported_at, next_heartbeat
        now = time.time()
        while self._next_heartbeat <= now:
            self._next_heartbeat += HT_HEARTBEAT_SECONDS

    def _save(self) -> None:
        """At each report - a few times an hour at most, so the board's
        storage barely notices."""
        if not self._state_file:
            return
        entry = {
            "room": {"temperature": self._temperature, "humidity": self._humidity,
                     "battery": self._battery, "at": self._last_tick},
            "reported": self._reported, "reported_at": self._reported_at,
            "next_heartbeat": self._next_heartbeat,
        }
        with _state_lock:
            try:
                try:
                    data = load_json(self._state_file)
                except (OSError, ValueError):
                    data = {}
                data = data if isinstance(data, dict) else {}
                data[self.name] = entry
                save_json_atomic(self._state_file, data)
            except OSError:
                logger.exception("Simulated sensor '%s': couldn't save its state", self.name)

    def _publish(self) -> None:
        """What a real H&T Gen3 sends its broker on each wake."""
        report = self._reported
        world.publish(f"{self.name}/status/devicepower:0", json.dumps(
            {"id": 0, "battery": {"percent": report["battery_pct"]}, "external": {"present": False}}))
        world.publish(f"{self.name}/status/wifi", json.dumps({"rssi": report["wifi_rssi_dbm"]}))

    @property
    def last_update_ts(self) -> float | None:
        """When the simulated H&T last reported - what a real one's age
        reads as on the dashboard. None otherwise: every read is new."""
        return self._reported_at if self._mimic else None

    def _season(self) -> str:
        try:
            with open(self._season_file, encoding="utf-8") as f:
                mode = json.load(f).get("mode")
        except (OSError, ValueError, TypeError, AttributeError):
            mode = None
        return mode if mode in HT_REST_C else "heat"

    def read(self) -> dict[str, Any]:
        if self._mimic:
            return self._read_like_ht()
        now = time.time()
        elapsed_min = max(0.0, (now - self._last_tick) / 60.0)
        self._last_tick = now

        valve_on = bool(self._influenced_by) and world.get_switch(self._influenced_by)
        target = self._influence_target if valve_on else self._resting(datetime.now())

        step = self._drift_rate * elapsed_min
        if self._temperature < target:
            self._temperature = min(target, self._temperature + step)
        elif self._temperature > target:
            self._temperature = max(target, self._temperature - step)
        self._temperature += random.uniform(-0.03, 0.03)
        self._humidity = max(20.0, min(80.0, self._humidity + random.uniform(-0.4, 0.4)))

        return {
            "ok": True,
            "name": self.name,
            "temperature_c": round(self._temperature, 2),
            "humidity_pct": round(self._humidity, 1),
        }

    def _read_like_ht(self) -> dict[str, Any]:
        now = time.time()
        hours = max(0.0, (now - self._last_tick) / 3600.0)
        self._last_tick = now
        season = self._season()
        valve_on = bool(self._influenced_by) and world.get_switch(self._influenced_by)
        if valve_on:
            # Heating only ever warms, cooling only ever cools: a room
            # already past the valve's water stays where it is.
            target = HT_VALVE_TARGET_C[season]
            goal = max(self._temperature, target) if season == "heat" else min(self._temperature, target)
            rate = HT_RUN_C_PER_H
        else:
            goal, rate = HT_REST_C[season], HT_DRIFT_C_PER_H
        step = rate * hours
        if self._temperature < goal:
            self._temperature = min(goal, self._temperature + step)
        else:
            self._temperature = max(goal, self._temperature - step)
        pull = (HT_HUMIDITY_MEAN - self._humidity) * (1 - math.exp(-hours / HT_HUMIDITY_PULL_H))
        wander = random.gauss(0.0, HT_HUMIDITY_WANDER * math.sqrt(hours)) if hours else 0.0
        self._humidity = max(35.0, min(70.0, self._humidity + pull + wander))
        self._battery = max(5.0, self._battery - hours / 720.0)      # about 1% a month

        temperature, humidity = round(self._temperature, 1), round(self._humidity, 1)
        last = self._reported
        heartbeat = now >= self._next_heartbeat
        changed = last is not None and (abs(temperature - last["temperature_c"]) >= HT_TEMP_STEP_C
                                        or abs(humidity - last["humidity_pct"]) >= HT_HUMIDITY_STEP_PCT)
        if last is None or heartbeat or changed:
            # A change report leaves the heartbeat's own clock alone.
            if last is None or heartbeat:
                self._next_heartbeat = (self._next_heartbeat or now) + HT_HEARTBEAT_SECONDS
                while self._next_heartbeat <= now:
                    self._next_heartbeat += HT_HEARTBEAT_SECONDS
            self._reported = {
                "temperature_c": temperature,
                "humidity_pct": humidity,
                "battery_pct": round(self._battery),
                "wifi_rssi_dbm": round(self._rssi_base + random.uniform(-2, 2)),
            }
            self._reported_at = now
            self._save()
            self._publish()
        return {"ok": True, "name": self.name, **self._reported}

    def _resting(self, when: datetime) -> float:
        if not self._monthly_base:
            return self._base_temperature
        hour = when.hour + when.minute / 60
        return self._monthly_base[when.month - 1] + self._daily_swing * math.cos(2 * math.pi * (hour - 15) / 24)


@register_driver("climate", "simulated")
def _create(options: dict) -> SimulatedClimateDriver:
    return SimulatedClimateDriver(
        name=options.get("name", "Simulated sensor"),
        base_temperature_c=options.get("base_temperature_c", 20.0),
        base_humidity_pct=options.get("base_humidity_pct", 50.0),
        influenced_by=options.get("influenced_by"),
        influence_target_c=options.get("influence_target_c", 26.0),
        drift_rate_c_per_min=options.get("drift_rate_c_per_min", 0.15),
        poll_interval_seconds=options.get("poll_interval_seconds", 10.0),
        monthly_base_c=options.get("monthly_base_c"),
        daily_swing_c=options.get("daily_swing_c", 0.0),
        mimic_ht_gen3=bool(options.get("mimic_ht_gen3", False)),
        season_file=options.get("season_file"),
        state_file=options.get("state_file"),
    )
