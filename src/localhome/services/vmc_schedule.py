"""A day/night speed program for a ventilation unit (VMC).

Mechanical ventilation runs continuously - what changes is how fast. The
sensible household pattern is one speed during the day and a quieter one
at night, which is all this does: two periods, each a time of day and a
speed, applied to a `number` driver (the VMC's speed).

Deliberately *not* a thermostat: the speed is written once, when a period
begins, and then left alone. Picking "Boost" by hand at 15:00 therefore
stays until the night period starts, instead of being corrected away a
minute later - a program that fought every manual choice would make the
dashboard's own speed buttons useless. The trade-off is that a speed
changed by hand is not restored until the next period begins, which is
exactly what "simple but effective" means here.

The schedule lives in its own small JSON file so the admin panel can
change it while LocalHome runs: the control loop re-reads it every tick,
so a save takes effect without a restart.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta

from localhome.errors import InputError
from localhome.services.log_throttle import FailureLog
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

PERIODS = ("day", "night")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEFAULT_SCHEDULE = {
    "enabled": False,
    "target": None,
    "day": {"at": "07:00", "value": 50},
    "night": {"at": "22:00", "value": 20},
}


def validate(schedule: dict) -> dict:
    """Returns the schedule cleaned up, or raises ValueError describing
    the first problem - the same shape every other store here uses."""
    if not isinstance(schedule, dict):
        raise InputError("request_invalid", "the schedule must be an object")
    target = schedule.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise InputError("vmc_target_invalid", "target must be a device name or null")
    clean = {
        "enabled": bool(schedule.get("enabled", False)),
        "target": target.strip() if isinstance(target, str) and target.strip() else None,
    }
    for period in PERIODS:
        data = schedule.get(period) or {}
        if not isinstance(data, dict):
            raise InputError("request_invalid", f"{period} must be an object with 'at' and 'value'")
        at = data.get("at", DEFAULT_SCHEDULE[period]["at"])
        if not isinstance(at, str) or not TIME_PATTERN.match(at):
            raise InputError("vmc_time_invalid", f"{period}: 'at' must be a time like 07:00, got {at!r}", period=period)
        value = data.get("value", DEFAULT_SCHEDULE[period]["value"])
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
            raise InputError("vmc_speed_invalid", f"{period}: 'value' must be a speed between 0 and 100, got {value!r}",
                             period=period)
        clean[period] = {"at": at, "value": round(float(value))}
    if clean["day"]["at"] == clean["night"]["at"]:
        raise InputError("vmc_same_times", "day and night must start at different times")
    if clean["enabled"] and not clean["target"]:
        raise InputError("vmc_no_target", "choose which device the program controls before turning it on")
    return clean


class VmcScheduleStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict:
        try:
            return validate(load_json(self.path))
        except FileNotFoundError:
            return dict(DEFAULT_SCHEDULE)
        except (ValueError, OSError) as exc:
            logger.warning("Ignoring an unreadable VMC schedule in %s (%s) - using the defaults", self.path, exc)
            return dict(DEFAULT_SCHEDULE)

    def set(self, schedule: dict) -> dict:
        clean = validate(schedule)
        with self._lock:
            save_json_atomic(self.path, clean)
        return clean


def _minutes(at: str) -> int:
    hours, minutes = at.split(":")
    return int(hours) * 60 + int(minutes)


def current_period(schedule: dict, now: datetime) -> str:
    """Which period `now` falls in. The two start times split the day in
    two, wrapping over midnight - with day at 07:00 and night at 22:00,
    01:00 is night and 08:00 is day."""
    minute_of_day = now.hour * 60 + now.minute
    day, night = _minutes(schedule["day"]["at"]), _minutes(schedule["night"]["at"])
    if day < night:
        return "day" if day <= minute_of_day < night else "night"
    return "night" if night <= minute_of_day < day else "day"


def next_change(schedule: dict, now: datetime) -> datetime:
    """When the current period ends."""
    other = "night" if current_period(schedule, now) == "day" else "day"
    at = _minutes(schedule[other]["at"])
    start = now.replace(hour=at // 60, minute=at % 60, second=0, microsecond=0)
    return start if start > now else start + timedelta(days=1)


class VmcScheduleController:
    """Applies the program's speed at each period change, and once at
    startup. Reads the schedule every tick, so the admin panel's save
    takes effect without a restart."""

    def __init__(self, manager, store: VmcScheduleStore, poll_interval_seconds: float = 60):
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self._applied: tuple[str, str, int] | None = None  # target, period, value
        self._failures = FailureLog(logger)
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
                logger.exception("VMC schedule loop failed, will retry next interval")
            time.sleep(self.poll_interval_seconds)

    def tick(self, now: datetime | None = None) -> None:
        schedule = self.store.get()
        if not schedule["enabled"] or not schedule["target"]:
            self._applied = None  # switched off: the next period change applies again
            return
        now = now or datetime.now()
        period = current_period(schedule, now)
        value = schedule[period]["value"]
        wanted = (schedule["target"], period, value)
        if self._applied == wanted:
            return  # mid-period: whatever speed was picked by hand stays
        number = self.manager.numbers.get(schedule["target"])
        if number is None:
            self._failures.failed("missing", "VMC schedule: no settable device named %r - is it configured?", schedule["target"])
            return
        try:
            number.set_value(value)
        except Exception as exc:
            self._failures.failed(schedule["target"], "VMC schedule: could not set %s to %s%% (%s)", schedule["target"], value, exc)
            return
        self._failures.succeeded(schedule["target"])
        self._failures.succeeded("missing")
        poller = self.manager.pollers.get(schedule["target"])
        if poller is not None:
            poller.cache.merge({"value": value})
            if hasattr(poller, "wake"):
                poller.wake()
        self._applied = wanted
        logger.info("VMC schedule: %s period - %s set to %s%%", period, schedule["target"], value)

    def status(self, now: datetime | None = None) -> dict:
        """What the dashboard shows next to the speed buttons."""
        schedule = self.store.get()
        if not schedule["enabled"] or not schedule["target"]:
            return {"enabled": False}
        now = now or datetime.now()
        period = current_period(schedule, now)
        return {
            "enabled": True,
            "target": schedule["target"],
            "period": period,
            "value": schedule[period]["value"],
            "until": next_change(schedule, now).strftime("%H:%M"),
        }
