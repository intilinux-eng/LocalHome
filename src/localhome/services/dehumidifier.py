"""The dehumidifier: switched on from the home page, or - only if the
admin panel says so - by itself. It draws a lot of power, so by default
nothing here turns it on alone: it's switched on for one of the offered
durations, or until the room is dry enough ("until the target"), and
switched off when that's up - or before, when the reference room's
humidity drops below the stop threshold. Below the start threshold it
isn't started at all: the air is dry enough, and the home page says it's
off for that reason. The two thresholds apart, so it doesn't start and
stop at one value.

The thresholds and the ways to switch it on are the admin panel's
(DehumidifierSettingsStore, config/dehumidifier_settings.json), read at
every check - a change takes effect at once, no restart:
  - stop_below / start_from: off under the first, not started under the
    second. Standard defaults: 50% - the middle of the 40-60% comfort band
    and a dehumidifier's usual factory setting - and 55%;
  - durations: the hours offered on the home page;
  - until_target: also "until the stop threshold", for at most
    target_max_hours;
  - auto: off by default. On, it switches itself on above start_from,
    only between auto_from_hour and auto_to_hour, for at most
    auto_max_hours_day a day; switched off by hand, it waits
    AUTO_PAUSE_AFTER_HAND_SECONDS before doing it again.

Only while the season leaves it to be switched by hand (the dehumidifier
is a summer device - see the season rules in web/app.py) and outside the
diagnostic mode, where whoever installs it switches what they like. The
time it's on until is kept on disk: a restart of LocalHome doesn't make
it run for ever. If the board stops, its Shelly switches it off by itself
within 15 minutes (drivers/mqtt/switch.py's hold).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

DURATION_CHOICES = (0.5, 1, 2, 4, 8)
DEFAULTS = {
    "stop_below": 50,
    "start_from": 55,
    "durations": [1, 2, 4],
    "until_target": True,
    "target_max_hours": 8,
    "auto": False,
    "auto_from_hour": 8,
    "auto_to_hour": 22,
    "auto_max_hours_day": 6,
}
# Each setting's range - checked before anything is saved.
RANGES = {
    "stop_below": (30, 70),
    "start_from": (32, 85),
    "target_max_hours": (1, 12),
    "auto_from_hour": (0, 23),
    "auto_to_hour": (1, 24),
    "auto_max_hours_day": (1, 24),
}
START_ABOVE_STOP_BY = 2         # at least this many points between the two thresholds
AUTO_PAUSE_AFTER_HAND_SECONDS = 2 * 3600

# Kept as names other modules and tests read: the defaults.
STOP_BELOW_PCT = DEFAULTS["stop_below"]
START_FROM_PCT = DEFAULTS["start_from"]


def validate(values: dict, base: dict | None = None) -> dict:
    """The settings `values` change, on top of `base` (the defaults if
    none), checked together: raises InputError naming the first problem."""
    if not isinstance(values, dict):
        raise InputError("request_invalid", "settings must be an object")
    clean = dict(base or DEFAULTS)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise InputError("setting_unknown", f"unknown setting '{key}'", key=key)
        if key in ("until_target", "auto"):
            if not isinstance(value, bool):
                raise InputError("request_invalid", f"{key}: must be true or false")
            clean[key] = value
        elif key == "durations":
            if not isinstance(value, list) or any(isinstance(v, bool) or v not in DURATION_CHOICES for v in value):
                raise InputError("dehumidifier_durations_invalid", f"durations: pick among {DURATION_CHOICES}",
                                 choices=", ".join(_hours(v) for v in DURATION_CHOICES))
            clean[key] = sorted(set(value))
        else:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise InputError("setting_range", f"{key}: must be between {low} and {high}", key=key, min=low, max=high)
            clean[key] = int(round(value))
    if clean["start_from"] < clean["stop_below"] + START_ABOVE_STOP_BY:
        raise InputError("dehumidifier_start_above_stop",
                         f"start_from must be at least {START_ABOVE_STOP_BY} points above stop_below",
                         value=clean["stop_below"], min=clean["stop_below"] + START_ABOVE_STOP_BY)
    if not clean["durations"] and not clean["until_target"]:
        raise InputError("dehumidifier_no_way_on", "offer at least one duration or 'until the target'")
    if clean["auto_from_hour"] == clean["auto_to_hour"]:            # 0-24 is the whole day
        raise InputError("dehumidifier_auto_window", "the automatic window can't start and end at the same hour")
    return clean


def _hours(value: float) -> str:
    return f"{value:g}"


class DehumidifierSettingsStore:
    """config/dehumidifier_settings.json: only what was changed from the
    defaults is kept - a default changed in a later version applies."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _saved(self) -> dict:
        try:
            data = load_json(self.path)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self) -> dict:
        saved = self._saved()
        try:
            return validate(saved)
        except InputError:
            logger.warning("Ignoring invalid %s - using the dehumidifier's defaults", self.path)
            return dict(DEFAULTS)

    def set(self, values: dict) -> dict:
        with self._lock:
            clean = validate(values, self.get())
            changed = {k: v for k, v in clean.items() if v != DEFAULTS[k]}
            if changed:
                save_json_atomic(self.path, changed)
            elif os.path.exists(self.path):
                os.remove(self.path)
        return clean


def in_window(hour: int, from_hour: int, to_hour: int) -> bool:
    """`hour` inside [from, to) - across midnight when from > to (22-6)."""
    to_hour %= 24
    return from_hour <= hour < to_hour if from_hour < to_hour else hour >= from_hour or hour < to_hour


def window_end(now: float, from_hour: int, to_hour: int) -> float:
    """When the window `now` is in ends - never, for the whole day (0-24)."""
    if from_hour == 0 and to_hour == 24:
        return float("inf")
    moment = datetime.fromtimestamp(now)
    end = moment.replace(hour=to_hour % 24, minute=0, second=0, microsecond=0)
    if end <= moment:
        end += timedelta(days=1)
    return end.timestamp()


class DehumidifierTimer:
    def __init__(self, manager, switch_name: str, humidity_sensor: str | None, store_path: str,
                 command: Callable[[str, bool], None], allowed: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.time, poll_interval_seconds: float = 30,
                 humidity_zone: str | None = None, settings: Callable[[], dict] | None = None):
        self.manager = manager
        self.switch = switch_name
        self.sensor = humidity_sensor
        self.zone = humidity_zone         # the room whose humidity it is, as the pages name it
        self.store_path = store_path
        self._command = command          # (name, on) - as the home page's own switch does
        self._allowed = allowed
        self._settings = settings or (lambda: dict(DEFAULTS))
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

    def settings(self) -> dict:
        try:
            return self._settings()
        except Exception:
            logger.exception("Dehumidifier: couldn't read its settings - using the defaults")
            return dict(DEFAULTS)

    def humidity(self) -> float | None:
        if not self.sensor:
            return None
        reading = self.manager.sensor_reading(self.sensor) or {}
        value = reading.get("humidity_pct")
        return float(value) if reading.get("ok") and isinstance(value, (int, float)) else None

    def is_on(self) -> bool | None:
        reading = self.manager.sensor_reading(self.switch) or {}
        return bool(reading["is_on"]) if reading.get("ok") and "is_on" in reading else None

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")

    def _auto_used(self, data: dict) -> float:
        """Seconds it ran by itself today, the run going on included."""
        used = data.get("auto_seconds", 0) if data.get("auto_day") == self._today() else 0
        if data.get("mode") == "auto" and data.get("started_at"):
            used += max(0.0, self.clock() - data["started_at"])
        return used

    def _ended(self, data: dict) -> dict:
        """What's kept once a run is over: today's automatic time, and when
        it was last switched off by hand."""
        kept = {k: data[k] for k in ("auto_day", "auto_seconds", "hand_off_at") if k in data}
        if data.get("mode") == "auto" and data.get("started_at"):
            if kept.get("auto_day") != self._today():
                kept["auto_day"], kept["auto_seconds"] = self._today(), 0
            kept["auto_seconds"] = kept.get("auto_seconds", 0) + max(0.0, self.clock() - data["started_at"])
        return kept

    # ---------------------------------------------------------- actions --

    def switch_on(self, hours=None, until: str | None = None) -> dict:
        """For one of the offered durations, or `until="target"`: until the
        stop threshold, for at most target_max_hours."""
        s = self.settings()
        if until == "target":
            if not s["until_target"]:
                raise InputError("dehumidifier_hours_invalid", "'until the target' isn't offered",
                                 choices=", ".join(_hours(h) for h in s["durations"]))
            seconds, mode = s["target_max_hours"] * 3600, "target"
        else:
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours not in s["durations"]:
                raise InputError("dehumidifier_hours_invalid", f"hours must be one of {s['durations']}",
                                 choices=", ".join(_hours(h) for h in s["durations"]))
            seconds, mode = hours * 3600, "hours"
        if not self._allowed():
            raise InputError("dehumidifier_not_now", "not switched by hand in this season")
        humidity = self.humidity()
        if humidity is not None and humidity < s["start_from"]:
            raise InputError("dehumidifier_humidity_low", f"humidity {humidity:.0f}% - dry enough",
                             value=round(humidity), min=round(s["start_from"]))
        with self._lock:
            data = self._ended(self._load())
            self._command(self.switch, True)
            now = self.clock()
            data.update(until=now + seconds, mode=mode, started_at=now, stopped=None)
            self._save(data)
        logger.info("Dehumidifier on %s", "until the target" if mode == "target" else f"for {_hours(hours)} h")
        return self.status()

    def switch_off(self, why: str = "hand") -> dict:
        with self._lock:
            data = self._ended(self._load())
            self._command(self.switch, False)
            data["until"], data["mode"], data["stopped"] = None, None, {"at": self.clock(), "why": why}
            data.pop("started_at", None)
            if why == "hand":
                data["hand_off_at"] = self.clock()
            self._save(data)
        if why != "hand":
            logger.info("Dehumidifier off: %s", why)
        return self.status()

    def tick(self) -> None:
        """Off when its time is up, when the room is dry enough, when a run
        of its own leaves its hours - and at once when it was switched on
        some other way; on by itself only if the panel says so. Out of its
        season, away or in the diagnostic mode the season's rule (or the
        installer) has it: services/interlock.py."""
        with self._lock:
            s = self.settings()
            data = self._load()
            if not self._allowed():
                if data.get("until") is not None:
                    self._save(self._ended(data))           # the season has it now
                return
            on = self.is_on()
            if on is None:
                return
            humidity = self.humidity()
            now = self.clock()
            if on is False:
                if data.get("until") is not None:
                    self._save(self._ended(data))           # switched off some other way
                elif self._auto_should_start(s, data, humidity, now):
                    self._auto_start(s, data, now)
                return
            until = data.get("until")
            if until is None:
                # On, and no run of its own: switched on from outside - the
                # Shelly app, a test, the diagnostic mode left behind. It
                # draws a lot of power: off, unless asked for here (a time,
                # "until dry", or by itself as the panel allows).
                logger.warning("Dehumidifier on without being asked from LocalHome - switching it off")
                self.switch_off("unasked")
            elif data.get("mode") == "auto" and not in_window(datetime.fromtimestamp(now).hour,
                                                              s["auto_from_hour"], s["auto_to_hour"]):
                self.switch_off("window")
            elif until is not None and now >= until:
                self.switch_off("time")
            elif humidity is not None and humidity < s["stop_below"]:
                self.switch_off("humidity")

    def _auto_should_start(self, s: dict, data: dict, humidity: float | None, now: float) -> bool:
        if not s["auto"] or humidity is None or humidity < s["start_from"]:
            return False
        if not in_window(datetime.fromtimestamp(now).hour, s["auto_from_hour"], s["auto_to_hour"]):
            return False
        hand_off = data.get("hand_off_at")
        if hand_off is not None and now - hand_off < AUTO_PAUSE_AFTER_HAND_SECONDS:
            return False
        return self._auto_used(data) < s["auto_max_hours_day"] * 3600 - 60

    def _auto_start(self, s: dict, data: dict, now: float) -> None:
        # No later than what's left of its day, nor than the end of its
        # hours - the page shows this "at most until".
        left = s["auto_max_hours_day"] * 3600 - self._auto_used(data)
        until = min(now + left, window_end(now, s["auto_from_hour"], s["auto_to_hour"]))
        data = self._ended(data)
        self._command(self.switch, True)
        data.update(until=until, mode="auto", started_at=now, stopped=None)
        self._save(data)
        logger.info("Dehumidifier on by itself: humidity at or above %s%%", s["start_from"])

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
        s = self.settings()
        data = self._load()
        humidity = self.humidity()
        until = data.get("until")
        hand_off = data.get("hand_off_at")
        paused_until = hand_off + AUTO_PAUSE_AFTER_HAND_SECONDS if hand_off else None
        return {
            "switch": self.switch,
            "on": self.is_on(),
            "until": until,
            # The board's local time, as every other "until" the pages get.
            "until_at": datetime.fromtimestamp(until).isoformat(timespec="seconds") if until else None,
            "mode": data.get("mode") if until else None,     # "hours", "target" or "auto"
            "stopped": data.get("stopped"),
            "humidity": None if humidity is None else round(humidity),
            "humidity_sensor": self.sensor,
            "humidity_zone": self.zone,
            "stop_below": s["stop_below"],
            "start_from": s["start_from"],
            "durations": s["durations"],
            "until_target": s["until_target"],
            "target_max_hours": s["target_max_hours"],
            "auto": {
                "enabled": s["auto"], "from_hour": s["auto_from_hour"], "to_hour": s["auto_to_hour"],
                "max_hours_day": s["auto_max_hours_day"],
                "used_today_seconds": round(self._auto_used(data)),
                "paused_until_at": datetime.fromtimestamp(paused_until).isoformat(timespec="seconds")
                if paused_until and paused_until > self.clock() else None,
            },
            "allowed": self._allowed(),
        }
