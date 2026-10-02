"""A weekly speed program for a ventilation unit (VMC), and its timed
boost.

Mechanical ventilation runs continuously - what changes is how fast. The
program is a week of hours, each with a speed (0 is off), like the
programmers sold for these units: a quieter speed at night, a higher one
by day, the maximum around a shower or cooking, off while away. The
dashboard paints it on a grid, or starts from a ready-made one ("day and
night", "away for work"...). The older two-period form - a day and a
night speed with their start times, what the classic page edits - is
still accepted and becomes that week.

Deliberately *not* a thermostat: a speed is written when the program
moves to a new one, and then left alone. Picking "Boost" by hand at
15:00 therefore stays until the program next changes speed, instead of
being corrected away a minute later - a program that fought every manual
choice would make the dashboard's own speed buttons useless.

A timed boost (`override`) is the one exception: a speed for so many
minutes, then back to what the program says - or, with the program off,
to the speed and on/off state from before it.

The schedule lives in its own small JSON file so it can change while
LocalHome runs: the control loop re-reads it every tick, so a save takes
effect without a restart.
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

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PERIODS = ("day", "night")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEK_HOURS = 7 * 24
MAX_OVERRIDE_MINUTES = 24 * 60
DEFAULT_DAY_NIGHT = {"day": {"at": "07:00", "value": 50}, "night": {"at": "22:00", "value": 25}}


def day_night_week(day: dict, night: dict) -> dict[str, list[int]]:
    """The week the two-period form stands for, to the hour: every day
    the same, the day speed from its start to the night's."""
    day_hour, night_hour = int(day["at"][:2]), int(night["at"][:2])
    hours = [night["value"]] * 24
    hour = day_hour
    while hour != night_hour:
        hours[hour] = day["value"]
        hour = (hour + 1) % 24
    return {d: list(hours) for d in DAYS}


def day_night_of(week: dict) -> dict | None:
    """The two-period form of a week, if it is one: every day alike, two
    speeds, each in one stretch (wrapping over midnight) - else None. The
    higher speed is the day's."""
    first = week["mon"]
    if any(week[d] != first for d in DAYS) or len(set(first)) != 2:
        return None
    starts = [h for h in range(24) if first[h] != first[h - 1]]
    if len(starts) != 2:
        return None
    by_value = {first[h]: h for h in starts}
    high, low = max(by_value), min(by_value)
    return {"day": {"at": f"{by_value[high]:02d}:00", "value": high},
            "night": {"at": f"{by_value[low]:02d}:00", "value": low}}


DEFAULT_SCHEDULE = {
    "enabled": False,
    "target": None,
    "week": day_night_week(DEFAULT_DAY_NIGHT["day"], DEFAULT_DAY_NIGHT["night"]),
    "override": None,
}


def _speed(value, what: str, code: str = "vmc_speed_invalid", **params) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
        raise InputError(code, f"{what}: a speed must be between 0 and 100, got {value!r}", **params)
    return round(float(value))


def _validate_day_night(schedule: dict) -> dict:
    periods = {}
    for period in PERIODS:
        data = schedule.get(period) or {}
        if not isinstance(data, dict):
            raise InputError("request_invalid", f"{period} must be an object with 'at' and 'value'")
        at = data.get("at", DEFAULT_DAY_NIGHT[period]["at"])
        if not isinstance(at, str) or not TIME_PATTERN.match(at):
            raise InputError("vmc_time_invalid", f"{period}: 'at' must be a time like 07:00, got {at!r}", period=period)
        periods[period] = {"at": at, "value": _speed(data.get("value", DEFAULT_DAY_NIGHT[period]["value"]), period,
                                                     period=period)}
    if periods["day"]["at"][:2] == periods["night"]["at"][:2]:
        raise InputError("vmc_same_times", "day and night must start at different hours")
    return day_night_week(periods["day"], periods["night"])


def _validate_week(week) -> dict[str, list[int]]:
    if not isinstance(week, dict) or set(week) != set(DAYS):
        raise InputError("vmc_week_invalid", "the week must have mon..sun, each with 24 hourly speeds")
    clean = {}
    for day in DAYS:
        hours = week[day]
        if not isinstance(hours, list) or len(hours) != 24:
            raise InputError("vmc_week_invalid", f"{day}: 24 hourly speeds expected")
        clean[day] = [_speed(v, f"{day} {h:02d}:00", "vmc_week_invalid") for h, v in enumerate(hours)]
    return clean


def _validate_override(override) -> dict | None:
    if override is None:
        return None
    try:
        until = datetime.fromisoformat(override["until"])
        restore = override.get("restore") or {}
        return {
            "device": str(override["device"]),
            "value": _speed(override["value"], "boost", "vmc_override_invalid"),
            "until": until.isoformat(timespec="seconds"),
            "restore": {"value": restore.get("value") if isinstance(restore.get("value"), (int, float)) else None,
                        "on": restore.get("on") if isinstance(restore.get("on"), bool) else None},
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InputError("vmc_override_invalid", "a timed speed needs a device, a speed and an end time")


def validate(schedule: dict) -> dict:
    """Returns the schedule cleaned up, or raises InputError describing
    the first problem. Without a `week`, `day` and `night` (the older
    two-period form) say what it is."""
    if not isinstance(schedule, dict):
        raise InputError("request_invalid", "the schedule must be an object")
    target = schedule.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise InputError("vmc_target_invalid", "target must be a device name or null")
    clean = {
        "enabled": bool(schedule.get("enabled", False)),
        "target": target.strip() if isinstance(target, str) and target.strip() else None,
        "week": _validate_week(schedule["week"]) if "week" in schedule else _validate_day_night(schedule),
        "override": _validate_override(schedule.get("override")),
    }
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
            return _default()
        except (ValueError, OSError) as exc:
            logger.warning("Ignoring an unreadable VMC schedule in %s (%s) - using the defaults", self.path, exc)
            return _default()

    def set(self, schedule: dict) -> dict:
        clean = validate(schedule)
        with self._lock:
            save_json_atomic(self.path, clean)
        return clean

    def update(self, changes: dict) -> dict:
        """Only what `changes` names changes - a page turning the program
        on doesn't send the week. `day`/`night` without a `week` replace
        the week with their two periods."""
        with self._lock:
            merged = {**self.get(), **changes}
            if "week" not in changes and (changes.get("day") or changes.get("night")):
                merged.pop("week")
            clean = validate(merged)
            save_json_atomic(self.path, clean)
        return clean


def _default() -> dict:
    return {**DEFAULT_SCHEDULE, "week": {d: list(h) for d, h in DEFAULT_SCHEDULE["week"].items()}}


def speed_at(week: dict, when: datetime) -> int:
    return week[DAYS[when.weekday()]][when.hour]


def _hour_start(when: datetime) -> datetime:
    return when.replace(minute=0, second=0, microsecond=0)


def stretch_start(week: dict, now: datetime) -> datetime | None:
    """When the speed running now began - None in a week with one speed
    throughout, which has no beginning."""
    value, hour = speed_at(week, now), _hour_start(now)
    for _ in range(WEEK_HOURS):
        before = hour - timedelta(hours=1)
        if speed_at(week, before) != value:
            return hour
        hour = before
    return None


def next_change(week: dict, now: datetime) -> tuple[datetime, int] | None:
    """When the program next changes speed, and to what - None if never."""
    value, hour = speed_at(week, now), _hour_start(now)
    for _ in range(WEEK_HOURS):
        hour += timedelta(hours=1)
        if speed_at(week, hour) != value:
            return hour, speed_at(week, hour)
    return None


def override_active(override: dict | None, now: datetime) -> bool:
    return bool(override) and now < datetime.fromisoformat(override["until"])


class VmcScheduleController:
    """Applies the program's speed whenever it changes, and once at
    startup; a timed boost while it lasts, and what comes after it. Reads
    the schedule every tick, so a save takes effect without a restart."""

    def __init__(self, manager, store: VmcScheduleStore, poll_interval_seconds: float = 60):
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self._applied: tuple | None = None
        self._failures = FailureLog(logger)
        self._started = False
        self._lock = threading.RLock()

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
        with self._lock:
            now = now or datetime.now()
            schedule = self.store.get()
            override = schedule["override"]
            if override_active(override, now):
                wanted = ("override", override["device"], override["value"], override["until"])
                if self._applied != wanted and self._apply(override["device"], override["value"], "timed speed"):
                    self._applied = wanted
                return
            if override:
                self._end_override(schedule)
                schedule = self.store.get()
            if not schedule["enabled"] or not schedule["target"]:
                self._applied = None  # switched off: the next change applies again
                return
            value = speed_at(schedule["week"], now)
            start = stretch_start(schedule["week"], now)
            wanted = (schedule["target"], start, value)
            if self._applied == wanted:
                return  # mid-stretch: whatever speed was picked by hand stays
            if self._apply(schedule["target"], value, "program"):
                self._applied = wanted

    def speeds_changed(self, moves: dict[int, int]) -> None:
        """The unit's speeds were given new values (the admin panel), and
        the week moved with them: the stretch running now counts as applied
        at its new value - so a speed picked by hand isn't replaced."""
        with self._lock:
            if self._applied and self._applied[0] != "override":
                target, start, value = self._applied
                self._applied = (target, start, moves.get(value, value))

    def boost(self, device: str, value: int, minutes: int, now: datetime | None = None) -> dict:
        """`value` on `device` for `minutes`, then back to the program -
        or, with it off, to how the device was before."""
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_OVERRIDE_MINUTES:
            raise InputError("vmc_override_invalid", f"minutes must be between 1 and {MAX_OVERRIDE_MINUTES}")
        if device not in self.manager.numbers:
            raise InputError("vmc_override_invalid", f"no settable device named {device!r}")
        now = now or datetime.now()
        with self._lock:
            current = self.store.get()["override"]
            # A boost on top of a boost keeps what was there before the first.
            restore = current["restore"] if override_active(current, now) else self._reading(device)
            self.store.update({"override": {"device": device, "value": value, "restore": restore,
                                            "until": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")}})
            self.tick(now)
        return self.status(now)

    def cancel_boost(self, now: datetime | None = None) -> dict:
        with self._lock:
            schedule = self.store.get()
            if schedule["override"]:
                self._end_override(schedule)
            self.tick(now)
        return self.status(now)

    def _end_override(self, schedule: dict) -> None:
        override = schedule["override"]
        self.store.update({"override": None})
        self._applied = None
        if schedule["enabled"] and schedule["target"]:
            return  # the program's own speed is applied right after
        restore = override.get("restore") or {}
        if restore.get("on") is False:
            self._apply(override["device"], 0, "after the timed speed")
        elif restore.get("value") is not None:
            self._apply(override["device"], round(restore["value"]), "after the timed speed")

    def _reading(self, device: str) -> dict:
        reading = self._poller_reading(device)
        switch = getattr(self.manager, "paired_switch", {}).get(device)
        return {"value": reading.get("value"), "on": self._poller_reading(switch).get("is_on") if switch else None}

    def _poller_reading(self, name: str) -> dict:
        poller = self.manager.pollers.get(name)
        try:
            return (poller.get() if poller is not None and hasattr(poller, "get") else {}) or {}
        except Exception:
            return {}

    def _apply(self, device: str, value: int, why: str) -> bool:
        """Sets the speed - 0 switches the unit off, any other speed
        switches it on if it was off. False if it couldn't."""
        number = self.manager.numbers.get(device)
        if number is None:
            self._failures.failed("missing", "VMC schedule: no settable device named %r - is it configured?", device)
            return False
        switch_name = getattr(self.manager, "paired_switch", {}).get(device)
        switch = getattr(self.manager, "switches", {}).get(switch_name) if switch_name else None
        try:
            if value == 0 and switch is not None:
                switch.turn_off()
                self._merge(switch_name, {"is_on": False})
            else:
                number.set_value(value)
                self._merge(device, {"value": value})
                if switch is not None and self._poller_reading(switch_name).get("is_on") is not True:
                    switch.turn_on()
                    self._merge(switch_name, {"is_on": True})
        except Exception as exc:
            self._failures.failed(device, "VMC schedule: could not set %s to %s%% (%s)", device, value, exc)
            return False
        self._failures.succeeded(device)
        self._failures.succeeded("missing")
        logger.info("VMC schedule: %s - %s set to %s%%", why, device, value)
        return True

    def _merge(self, name: str, data: dict) -> None:
        poller = self.manager.pollers.get(name)
        if poller is not None:
            poller.cache.merge(data)
            if hasattr(poller, "wake"):
                poller.wake()

    def status(self, now: datetime | None = None) -> dict:
        """What the dashboard shows on the VMC's card."""
        now = now or datetime.now()
        schedule = self.store.get()
        status: dict = {"enabled": bool(schedule["enabled"] and schedule["target"])}
        override = schedule["override"]
        if override_active(override, now):
            status["override"] = {"device": override["device"], "value": override["value"],
                                  "until": override["until"], "until_hhmm": override["until"][11:16]}
        if not status["enabled"]:
            return status
        week = schedule["week"]
        value = speed_at(week, now)
        change = next_change(week, now)
        status.update({"target": schedule["target"], "value": value,
                       "until": change[0].strftime("%H:%M") if change else None,
                       "until_at": change[0].isoformat(timespec="minutes") if change else None,
                       "next_value": change[1] if change else None})
        # The classic page's words for a two-period week.
        two = day_night_of(week)
        if two:
            status["period"] = "day" if value == two["day"]["value"] else "night"
        return status
