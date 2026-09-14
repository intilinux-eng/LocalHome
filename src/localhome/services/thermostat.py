"""Two-zone (or more) valve thermostat for a shared-plant radiant-panel
setup: one central boiler/chiller sends hot or cold water to per-zone
valves, and all this needs to decide is *when each valve should be open*
- current temperature vs. that zone's own weekly schedule, with
hysteresis so the valve doesn't chatter open/close right at the setpoint.

Each zone is just an existing `climate` sensor + `switch` valve, already
known to the DeviceManager under those names - no new device kind. What's
new here is the schedule (a target temperature per hour of the week, per
zone) and the control loop that reads one and drives the other.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOURS_PER_DAY = 24
MODES = ("heat", "cool")


@dataclass
class Zone:
    name: str
    sensor: str
    valve: str
    # Some rooms physically can't be cooled by a radiant-panel system at
    # all - a bathroom's ambient humidity is high enough that a cold
    # ceiling panel condenses (and can drip/frost) regardless of the
    # room's own temperature. Setting this false means "never let the
    # thermostat open this zone's valve while mode == cool" - not just a
    # different setpoint, the zone is excluded from cooling control
    # entirely. Has no effect in heat mode.
    cool_enabled: bool = True


class ScheduleStore:
    """Persists the weekly per-zone setpoint grid and the shared heat/cool
    mode as one small JSON file (schedules.json) - the thermostat
    equivalent of positions.json. A schedule slot is either a number
    (target °C for that hour) or null (zone off for that hour,
    regardless of temperature)."""

    def __init__(self, path: str, default_mode: str = "heat"):
        self.path = path
        self.default_mode = default_mode

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"mode": self.default_mode, "zones": {}}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            # A schedule/mode that failed to load is exactly the "must
            # survive a power cut" scenario this is meant to guard
            # against - _save() below writes atomically so this really
            # shouldn't happen going forward, but falling back to
            # (re-editable) defaults beats refusing to start at all.
            logger.exception("Failed to read %s - starting from defaults; you may need to re-enter your schedule", self.path)
            return {"mode": self.default_mode, "zones": {}}

    def _save(self, data: dict) -> None:
        save_json_atomic(self.path, data)

    def get_mode(self) -> str:
        return self._load().get("mode", self.default_mode)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        data = self._load()
        data["mode"] = mode
        self._save(data)

    @staticmethod
    def empty_week() -> dict[str, list]:
        return {day: [None] * HOURS_PER_DAY for day in DAYS}

    def get_week(self, zone_name: str) -> dict[str, list]:
        data = self._load()
        week = data.get("zones", {}).get(zone_name)
        if not week:
            return self.empty_week()
        # set_week() rejects a non-numeric value going forward, but this
        # sanitizes anything already on disk from before that check
        # existed (or a hand-edited file) - without it, the dashboard
        # round-trips the *whole* week on every save (see saveSchedule()
        # in app.js), so one bad legacy cell would silently ride along on
        # every future save of that zone and get rejected every time,
        # with no way to fix it short of editing the JSON file by hand.
        # Reading it back as null instead means the very next save
        # overwrites it with a clean value for good.
        clean: dict[str, list] = {}
        for day, hours in week.items():
            clean_hours = []
            for hour, value in enumerate(hours):
                if value is not None and not isinstance(value, (int, float)):
                    logger.warning(
                        "Zone %r has a non-numeric schedule value %r for %s %d:00 - clearing it",
                        zone_name, value, day, hour,
                    )
                    value = None
                clean_hours.append(value)
            clean[day] = clean_hours
        return clean

    def set_week(self, zone_name: str, week: dict[str, list]) -> None:
        for day in DAYS:
            hours = week.get(day)
            if not isinstance(hours, list) or len(hours) != HOURS_PER_DAY:
                raise ValueError(f"week['{day}'] must be a list of exactly {HOURS_PER_DAY} values")
            for hour, value in enumerate(hours):
                # Rejected here, not left for get_target()'s float(value)
                # to discover later - that runs from the thermostat's
                # background tick, several hours after whoever posted a
                # bad value is long gone, and would only be caught by the
                # generic "log and retry next tick" fallback rather than
                # ever telling anyone what was actually wrong.
                if value is not None and not isinstance(value, (int, float)):
                    raise ValueError(f"week['{day}'][{hour}] must be a number or null, got {value!r}")
        data = self._load()
        data.setdefault("zones", {})[zone_name] = week
        self._save(data)

    def get_target(self, zone_name: str, when: datetime) -> float | None:
        week = self.get_week(zone_name)
        value = week[DAYS[when.weekday()]][when.hour]
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            # set_week() rejects a non-numeric value up front, but this
            # is the second line of defense for anything already on disk
            # from before that check existed, or hand-edited directly -
            # this is read on every /api/climate/zones request and every
            # tick, so raising here would take the whole zones API (not
            # just this one zone) down for as long as the bad value sits
            # in the file, not just skip a tick.
            logger.warning(
                "Zone %r has a non-numeric schedule value %r for %s %d:00 - treating as no target",
                zone_name, value, DAYS[when.weekday()], when.hour,
            )
            return None

    def get_away_until(self) -> datetime | None:
        """Away mode is a temporary override, separate from the weekly
        schedule (painting every hour off for a day would work but is
        tedious and easy to forget to undo, and overwrites the schedule
        the person actually wants back when they return). This just
        remembers a deadline; ThermostatController checks it every tick
        and holds every valve closed until then, then resumes the
        schedule on its own - the same shape as ecobee's vacation hold,
        without needing a whole date-range/temperature form for a
        one-person-at-home setup."""
        value = self._load().get("away_until")
        return datetime.fromisoformat(value) if value else None

    def set_away_until(self, until: datetime | None) -> None:
        data = self._load()
        data["away_until"] = until.isoformat() if until else None
        self._save(data)


class ThermostatController:
    """Background loop: for each zone, compares its current temperature to
    its schedule's target for the current hour and commands its valve
    switch accordingly. Hysteresis keeps it from flipping the valve on
    every small fluctuation right at the setpoint - once open, it stays
    open until the temperature has moved `hysteresis_c` past the target
    in the "satisfied" direction, and vice versa."""

    def __init__(
        self,
        manager,
        schedule_store: ScheduleStore,
        zones: list[Zone],
        hysteresis_c: float = 0.3,
        poll_interval_seconds: float = 60,
    ):
        self.manager = manager
        self.schedule_store = schedule_store
        self.zones = zones
        self.hysteresis_c = hysteresis_c
        self.poll_interval_seconds = poll_interval_seconds
        self._valve_open: dict[str, bool] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Thermostat control loop failed, will retry next interval")
            time.sleep(self.poll_interval_seconds)

    def tick(self, now: datetime | None = None) -> None:
        mode = self.schedule_store.get_mode()
        now = now or datetime.now()
        away_until = self.schedule_store.get_away_until()
        if away_until is not None and now >= away_until:
            # Self-healing expiry: the next tick after the deadline clears
            # it, so nothing needs to remember to turn "away" back off,
            # and anyone reading away_until externally never sees a stale
            # past timestamp - it's None again within one poll interval.
            self.schedule_store.set_away_until(None)
            away_until = None
        away = away_until is not None
        for zone in self.zones:
            if away:
                self._apply(zone, False)
                continue
            if mode == "cool" and not zone.cool_enabled:
                # Hands off entirely - not even a forced "closed" command.
                # Something else (e.g. services/interlock.py, for a
                # dehumidifier sharing this valve's water circuit) may
                # legitimately need to open this valve for a reason that
                # has nothing to do with this zone's own temperature;
                # the thermostat has no business fighting it over that.
                continue
            reading = self.manager.sensor_reading(zone.sensor)
            if not reading.get("ok") or "temperature_c" not in reading:
                continue
            target = self.schedule_store.get_target(zone.name, now)
            desired = self.decide(zone.name, reading["temperature_c"], target, mode)
            self._apply(zone, desired)

    def decide(self, zone_name: str, current_c: float, target_c: float | None, mode: str) -> bool:
        """Pure decision logic, kept separate from _apply()/manager access
        so it's trivially unit-testable."""
        if target_c is None:
            return False
        previously_open = self._valve_open.get(zone_name, False)
        if mode == "heat":
            if current_c < target_c - self.hysteresis_c:
                return True
            if current_c > target_c + self.hysteresis_c:
                return False
        else:  # cool
            if current_c > target_c + self.hysteresis_c:
                return True
            if current_c < target_c - self.hysteresis_c:
                return False
        return previously_open  # inside the dead band - keep doing whatever it was doing

    def _apply(self, zone: Zone, desired_open: bool) -> None:
        if self._valve_open.get(zone.name) == desired_open:
            return
        switch = self.manager.switches.get(zone.valve)
        if switch is None:
            logger.warning("Zone %r's valve %r is not a known switch, skipping", zone.name, zone.valve)
            return
        try:
            switch.turn_on() if desired_open else switch.turn_off()
        except Exception:
            logger.exception("Failed to %s zone %r's valve %r", "open" if desired_open else "close", zone.name, zone.valve)
            return
        poller = self.manager.pollers.get(zone.valve)
        if poller is not None:
            poller.cache.merge({"is_on": desired_open})
        self._valve_open[zone.name] = desired_open
