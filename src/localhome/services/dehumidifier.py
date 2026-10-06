"""The dehumidifier, switched on by hand for a while. It draws a lot of
power, so nothing here ever turns it on by itself: from the home page it's
switched on for one of DURATIONS_HOURS, and switched off when that's up -
or before, when the reference room's humidity drops below STOP_BELOW_PCT.
Below START_FROM_PCT it isn't started at all: the air is dry enough. Two
points apart, so it doesn't start and stop at the threshold.

Only while the season leaves it to be switched by hand (the dehumidifier
is a summer device - see the season rules in web/app.py) and outside the
diagnostic mode, where whoever installs it switches what they like. The
time it's on until is kept on disk: a restart of LocalHome doesn't make
it run for ever. If the board stops, its Shelly switches it off by itself
within 15 minutes (drivers/mqtt/switch.py's hold).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

STOP_BELOW_PCT = 45.0
START_FROM_PCT = 47.0
DURATIONS_HOURS = (1, 2, 4)


class DehumidifierTimer:
    def __init__(self, manager, switch_name: str, humidity_sensor: str | None, store_path: str,
                 command: Callable[[str, bool], None], allowed: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.time, poll_interval_seconds: float = 30,
                 humidity_zone: str | None = None):
        self.manager = manager
        self.switch = switch_name
        self.sensor = humidity_sensor
        self.zone = humidity_zone         # the room whose humidity it is, as the pages name it
        self.store_path = store_path
        self._command = command          # (name, on) - as the home page's own switch does
        self._allowed = allowed
        self.clock = clock
        self.poll_interval_seconds = poll_interval_seconds
        self._lock = threading.RLock()
        self._started = False

    # ------------------------------------------------------------ state --

    def _load(self) -> dict:
        try:
            data = load_json(self.store_path)
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            save_json_atomic(self.store_path, data)
        except OSError:
            logger.exception("Dehumidifier: couldn't save %s", self.store_path)

    def humidity(self) -> float | None:
        if not self.sensor:
            return None
        reading = self.manager.sensor_reading(self.sensor) or {}
        value = reading.get("humidity_pct")
        return float(value) if reading.get("ok") and isinstance(value, (int, float)) else None

    def is_on(self) -> bool | None:
        reading = self.manager.sensor_reading(self.switch) or {}
        return bool(reading["is_on"]) if reading.get("ok") and "is_on" in reading else None

    # ---------------------------------------------------------- actions --

    def switch_on(self, hours) -> dict:
        if isinstance(hours, bool) or hours not in DURATIONS_HOURS:
            raise InputError("dehumidifier_hours_invalid", f"hours must be one of {DURATIONS_HOURS}",
                             choices=", ".join(str(h) for h in DURATIONS_HOURS))
        if not self._allowed():
            raise InputError("dehumidifier_not_now", "not switched by hand in this season")
        humidity = self.humidity()
        if humidity is not None and humidity < START_FROM_PCT:
            raise InputError("dehumidifier_humidity_low", f"humidity {humidity:.0f}% - dry enough",
                             value=round(humidity), min=round(START_FROM_PCT))
        with self._lock:
            self._command(self.switch, True)
            self._save({"until": self.clock() + hours * 3600, "stopped": None})
        logger.info("Dehumidifier on for %d h", hours)
        return self.status()

    def switch_off(self, why: str = "hand") -> dict:
        with self._lock:
            self._command(self.switch, False)
            self._save({"until": None, "stopped": {"at": self.clock(), "why": why}})
        if why != "hand":
            logger.info("Dehumidifier off: %s", why)
        return self.status()

    def tick(self) -> None:
        """Off when its time is up, or when the room is dry enough - even
        if it was switched on some other way. Never on."""
        with self._lock:
            if not self._allowed():
                if self._load().get("until") is not None:
                    self._save({"until": None, "stopped": None})   # the season has it now
                return
            on = self.is_on()
            until = self._load().get("until")
            if on is False:
                if until is not None:
                    self._save({"until": None, "stopped": None})   # switched off some other way
                return
            if on is None:
                return
            humidity = self.humidity()
            if until is not None and self.clock() >= until:
                self.switch_off("time")
            elif humidity is not None and humidity < STOP_BELOW_PCT:
                self.switch_off("humidity")

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        def loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    logger.exception("Dehumidifier check failed, will retry")
                time.sleep(self.poll_interval_seconds)
        threading.Thread(target=loop, daemon=True, name="dehumidifier").start()

    def status(self) -> dict:
        data = self._load()
        humidity = self.humidity()
        until = data.get("until")
        return {
            "switch": self.switch,
            "on": self.is_on(),
            "until": until,
            # The board's local time, as every other "until" the pages get.
            "until_at": datetime.fromtimestamp(until).isoformat(timespec="seconds") if until else None,
            "stopped": data.get("stopped"),
            "humidity": None if humidity is None else round(humidity),
            "humidity_sensor": self.sensor,
            "humidity_zone": self.zone,
            "stop_below": STOP_BELOW_PCT,
            "start_from": START_FROM_PCT,
            "durations": list(DURATIONS_HOURS),
            "allowed": self._allowed(),
        }
