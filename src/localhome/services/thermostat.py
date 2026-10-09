"""Two-zone (or more) valve thermostat for a shared-plant radiant-panel
setup: one central boiler/chiller sends hot or cold water to per-zone
valves, and all this needs to decide is *when each valve should be open*
- current temperature vs. that zone's own weekly schedule.

The rule, for a radiant ceiling (slow: it keeps giving heat or cool for a
while after its valve closes, and the H&T sensors report in steps of
half a degree): the valve stops as soon as the room reaches the target -
stopping past it overshot - and opens again once the room has moved the
hysteresis away from it, on the season's side. Each target is "comfort"
or "economy" (heating below 20°C, cooling from 26°C up - the palette's two
boxes): an economy target opens again the economy tolerance further on,
on the saving side only - longer pauses, a little less heating or
cooling, never more. Comfort stays as close to its target as the sensor
lets it.

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
from datetime import datetime, timedelta

from localhome.errors import InputError
from localhome.services.log_throttle import FailureLog
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOURS_PER_DAY = 24
MODES = ("heat", "cool")

# Each season has its own weekly grid - heating and cooling setpoints live
# in different ranges, and a single shared grid meant switching to cooling
# applied the winter values as cooling targets (a real deployment was
# cooling toward 19C this way). "zones" is the original key and stays the
# heating grid, so every schedules.json written before the split keeps
# working untouched, and older code reading the same file still finds its
# grid where it expects it. A season with no grid yet reads as all-off.
SEASON_KEYS = {"heat": "zones", "cool": "zones_cool"}

# The schedule's two levels, as the palette shows them (static/
# schedule_grid.js's TIER_OF - kept alike, a test checks): economy is
# heating below 20°C and cooling from 26°C up.
ECONOMY_HEAT_BELOW_C = 20.0
ECONOMY_COOL_FROM_C = 26.0


def target_tier(target_c: float | None, mode: str) -> str | None:
    """"comfort" or "economy" - None without a target."""
    if target_c is None:
        return None
    if mode == "heat":
        return "economy" if target_c < ECONOMY_HEAT_BELOW_C else "comfort"
    return "economy" if target_c >= ECONOMY_COOL_FROM_C else "comfort"


class SilenceLog:
    """How long each zone's sensor has been silent *while LocalHome was
    watching*, kept in a small file so a restart doesn't forget.

    Wall-clock would be wrong in both directions. Counting from when this
    process first noticed lets a board that restarts every day dodge the
    failsafe forever; counting the age of the last reading makes a board
    that was switched off for three days come back convinced every sensor
    had died - but nobody listening is not the same as nothing said. So
    only the time this process spent watching a silent sensor counts, and
    that total survives restarts."""

    def __init__(self, path: str | None = None, save_every_counted_seconds: float = 300):
        self.path = path
        # Written once per this much *counted* silence, not per wall-clock
        # interval: that bounds both what a power cut can lose (five
        # minutes of a countdown measured in hours) and how often a silent
        # sensor writes to the board's eMMC (twelve times an hour at most).
        self._save_every = save_every_counted_seconds
        self._lock = threading.Lock()
        self._unsaved = 0.0
        self._totals: dict[str, float] = {}
        if path:
            try:
                self._totals = {k: float(v) for k, v in load_json(path).items()}
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                self._totals = {}

    def seconds(self, zone_name: str) -> float:
        return self._totals.get(zone_name, 0.0)

    def add(self, zone_name: str, seconds: float) -> float:
        with self._lock:
            seconds = max(0.0, seconds)
            total = self._totals.get(zone_name, 0.0) + seconds
            self._totals[zone_name] = total
            self._unsaved += seconds
            due = self._unsaved >= self._save_every
        if due:
            self._save(force=True)
        return total

    def clear(self, zone_name: str) -> None:
        with self._lock:
            existed = self._totals.pop(zone_name, None) is not None
        if existed:
            self._save()

    def _save(self, force: bool = True) -> None:
        if not self.path:
            return
        self._unsaved = 0.0
        try:
            save_json_atomic(self.path, self._totals)
        except OSError as exc:
            logger.warning("Could not save %s (%s)", self.path, exc)


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
    # False while its Shelly is still to configure in the admin panel
    # (services/climate_map_store.py's to_configure()): there's no device
    # to read or drive yet, and that's not a fault.
    sensor_configured: bool = True
    valve_configured: bool = True


class ScheduleStore:
    """Persists the weekly per-zone setpoint grids (one per season - see
    SEASON_KEYS) and the shared heat/cool mode as one small JSON file
    (schedules.json) - the thermostat equivalent of positions.json. A
    schedule slot is either a number (target °C for that hour) or null
    (zone off for that hour, regardless of temperature)."""

    def __init__(self, path: str, default_mode: str = "heat"):
        self.path = path
        self.default_mode = default_mode
        # Every write here is read-modify-write of the whole file. Without
        # this, two saves landing together (both zones' grids, or a grid
        # and a mode switch - the web server is threaded) could each read
        # the old file and the second write would silently drop the
        # first's change. Same fix PositionStore needed.
        self._lock = threading.Lock()

    def _season_key(self, mode: str | None, data: dict) -> str:
        season = mode or data.get("mode", self.default_mode)
        if season not in MODES:
            raise InputError("mode_invalid", f"mode must be one of {MODES}")
        return SEASON_KEYS[season]

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

    def get_mode_changed(self) -> dict | None:
        """The last time the season was changed: {"at", "from", "by"}, or
        None if it never was since this was kept."""
        changed = self._load().get("mode_changed")
        return changed if isinstance(changed, dict) else None

    def clear_schedules(self) -> None:
        """Empties every zone's grid in both seasons and cancels away and
        diagnostic mode; the current season stays as it is."""
        with self._lock:
            mode = self._load().get("mode", self.default_mode)
            self._save({"mode": mode})

    def set_mode(self, mode: str, by: str | None = None) -> None:
        """`by`: who set it - a device and its address, as the web page
        says it - kept with when and what it was before (get_mode_changed()).
        On the production board the season changed twice with nobody able
        to say who had done it."""
        if mode not in MODES:
            raise InputError("mode_invalid", f"mode must be one of {MODES}")
        with self._lock:
            data = self._load()
            previous = data.get("mode", self.default_mode)
            if previous != mode:
                data["mode_changed"] = {"at": time.time(), "from": previous, "by": by}
            data["mode"] = mode
            # A temporary target belongs to the season it was set in: 22°
            # asked for while heating means nothing once cooling.
            data.pop("overrides", None)
            self._save(data)

    @staticmethod
    def empty_week() -> dict[str, list]:
        return {day: [None] * HOURS_PER_DAY for day in DAYS}

    def get_week(self, zone_name: str, mode: str | None = None) -> dict[str, list]:
        """`mode` picks the season's grid; None means the current season."""
        return self._week_in(self._load(), zone_name, mode)

    def _week_in(self, data: dict, zone_name: str, mode: str | None) -> dict[str, list]:
        week = data.get(self._season_key(mode, data), {}).get(zone_name)
        if not week:
            return self.empty_week()
        # set_week() rejects a non-numeric value going forward, but this
        # sanitizes anything already on disk from before that check
        # existed (or a hand-edited file) - without it, the dashboard's
        # grid (static/schedule_grid.js) loads that week to edit it, so one
        # bad legacy cell would ride along on every future save of that
        # zone and get rejected every time, with no way to fix it short
        # of editing the JSON file by hand.
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

    def set_week(self, zone_name: str, week: dict[str, list], mode: str | None = None) -> None:
        """`mode` picks the season's grid; None means the current season.
        Callers that know which season a grid was edited for should pass
        it - a season switch between editing and saving would otherwise
        file the grid under the wrong one."""
        for day in DAYS:
            hours = week.get(day)
            if not isinstance(hours, list) or len(hours) != HOURS_PER_DAY:
                raise InputError("schedule_invalid", f"week['{day}'] must be a list of exactly {HOURS_PER_DAY} values")
            for hour, value in enumerate(hours):
                # Rejected here, not left for get_target()'s float(value)
                # to discover later - that runs from the thermostat's
                # background tick, several hours after whoever posted a
                # bad value is long gone, and would only be caught by the
                # generic "log and retry next tick" fallback rather than
                # ever telling anyone what was actually wrong.
                if value is not None and not isinstance(value, (int, float)):
                    raise InputError("schedule_invalid", f"week['{day}'][{hour}] must be a number or null, got {value!r}")
        with self._lock:
            data = self._load()
            data.setdefault(self._season_key(mode, data), {})[zone_name] = week
            self._save(data)

    def set_cells(self, zone_name: str, cells: list, mode: str | None = None) -> dict[str, list]:
        """Changes just these hours - [[day, hour, value], ...] - on top of
        whatever the week is *now*, and returns the week that results.

        What the dashboard saves: two phones editing the same schedule each
        used to send their whole week, and the second one's save wiped the
        first one's changes - it had loaded the week before them. Merged
        cell by cell, both survive; only the same cell edited twice keeps
        the later value."""
        if not isinstance(cells, list):
            raise InputError("schedule_invalid", "'cells' must be a list of [day, hour, value]")
        changes = []
        for cell in cells:
            if not isinstance(cell, (list, tuple)) or len(cell) != 3:
                raise InputError("schedule_invalid", f"each cell must be [day, hour, value], got {cell!r}")
            day, hour, value = cell
            if day not in DAYS:
                raise InputError("schedule_invalid", f"unknown day {day!r}")
            if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour < HOURS_PER_DAY:
                raise InputError("schedule_invalid", f"hour must be 0-{HOURS_PER_DAY - 1}, got {hour!r}")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise InputError("schedule_invalid", f"{day} {hour}:00 must be a number or null, got {value!r}")
            changes.append((day, hour, value))
        with self._lock:
            data = self._load()
            week = self._week_in(data, zone_name, mode)
            for day, hour, value in changes:
                week[day][hour] = value
            data.setdefault(self._season_key(mode, data), {})[zone_name] = week
            self._save(data)
        return week

    def get_target(self, zone_name: str, when: datetime, mode: str | None = None) -> float | None:
        week = self.get_week(zone_name, mode)
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

    # ---------------------------------------------- temporary targets --
    # "22° for the next 2 hours", "off until the next change": a zone's
    # schedule set aside for a while, from the dashboard, without touching
    # the weekly grid - and gone by itself once its time is up. Kept here,
    # beside away mode, so it survives a restart. {zone: {"target": number
    # or None (off), "until": iso time, or None - until it's changed back by
    # hand (a season change drops it too)}}.

    def get_overrides(self, now: datetime | None = None) -> dict[str, dict]:
        """Every zone's temporary target still in force: {zone: {"target",
        "until" (datetime, or None: until it's changed back)}}."""
        now = now or datetime.now()
        active = {}
        for zone, entry in (self._load().get("overrides") or {}).items():
            if not isinstance(entry, dict) or "until" not in entry:
                continue
            if entry["until"] is None:
                active[zone] = {"target": entry.get("target"), "until": None}
                continue
            try:
                until = datetime.fromisoformat(entry["until"])
            except (TypeError, ValueError):
                continue
            if now < until:
                active[zone] = {"target": entry.get("target"), "until": until}
        return active

    def set_override(self, zone_name: str, target: float | None, until: datetime | None) -> None:
        """`until` None: until it's changed back by hand."""
        with self._lock:
            data = self._load()
            data.setdefault("overrides", {})[zone_name] = {"target": target,
                                                           "until": until.isoformat() if until else None}
            self._save(data)

    def clear_override(self, zone_name: str) -> None:
        with self._lock:
            data = self._load()
            if zone_name in (data.get("overrides") or {}):
                del data["overrides"][zone_name]
                self._save(data)

    def drop_expired_overrides(self, now: datetime) -> None:
        """Written only when one has actually run out - see the away
        deadline's own self-healing expiry in ThermostatController."""
        with self._lock:
            data = self._load()
            overrides = data.get("overrides") or {}
            keep = {zone: entry for zone, entry in overrides.items()
                    if entry.get("until") is None or datetime.fromisoformat(entry["until"]) > now}
            if len(keep) != len(overrides):
                data["overrides"] = keep
                self._save(data)

    def next_change(self, zone_name: str, now: datetime, mode: str | None = None) -> datetime | None:
        """When this zone's scheduled target next differs from this hour's,
        within a week - "until the next change" of a temporary target."""
        week = self.get_week(zone_name, mode)
        current = week[DAYS[now.weekday()]][now.hour]
        start = now.replace(minute=0, second=0, microsecond=0)
        for step in range(1, 7 * HOURS_PER_DAY):
            at = start + timedelta(hours=step)
            if week[DAYS[at.weekday()]][at.hour] != current:
                return at
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
        """Going away drops every temporary target: away holds all off,
        and coming back finds the schedule - not a 22° "until you change
        it" left from before."""
        with self._lock:
            data = self._load()
            data["away_until"] = until.isoformat() if until else None
            if until is not None:
                data.pop("overrides", None)
            self._save(data)

    def get_diagnostic_mode(self) -> bool:
        """A system-wide override, separate from away mode: while on,
        both ThermostatController and InterlockController skip every
        zone/interlock/mode_switch entirely, on every tick - not even a
        forced close, the same "hands off completely" stance as
        Zone.cool_enabled. Every valve this project knows about (zone
        valves, the bathroom bypass, the dehumidifier) is then only
        ever moved by a direct manual command from /climate-admin's own
        "Diagnostic mode" section, which reuses the generic
        /api/sensors/<name>/on|off routes (web/app.py's
        api_sensor_action() lets a mode-switch-owned switch through
        while this is on) - the flag itself is read/written through
        POST /api/climate/diagnostic-mode and read back from GET
        /api/climate/zones's own "diagnostic_mode" field. Meant for
        on-site verification (does this valve's relay actually work?
        which physical valve is which name?) without the thermostat
        undoing what you just did on its next tick."""
        return bool(self._load().get("diagnostic_mode", False))

    def set_diagnostic_mode(self, enabled: bool) -> None:
        with self._lock:
            data = self._load()
            data["diagnostic_mode"] = enabled
            self._save(data)


class ThermostatController:
    """Background loop: for each zone, compares its current temperature to
    its schedule's target for the current hour and commands its valve
    switch accordingly - see the module's docstring for the rule, and
    thresholds() for its two numbers."""

    def __init__(
        self,
        manager,
        schedule_store: ScheduleStore,
        zones: list[Zone],
        hysteresis_c: float = 0.5,
        poll_interval_seconds: float = 60,
        sensor_failsafe_hours: float = 3.0,
        silence_file: str | None = None,
        economy_tolerance_c: float = 0.5,
    ):
        self.manager = manager
        self.schedule_store = schedule_store
        self.zones = zones
        self.hysteresis_c = hysteresis_c
        self.economy_tolerance_c = economy_tolerance_c
        self.poll_interval_seconds = poll_interval_seconds
        self.sensor_failsafe_hours = sensor_failsafe_hours
        self._valve_open: dict[str, bool] = {}
        self._applied_targets: dict[str, float] = {}
        self._started = False
        # Battery sensors (Shelly H&T Gen3) can go quiet for perfectly
        # normal reasons (weak WiFi, dead battery, someone unscrewing it
        # to swap batteries) and manager.sensor_reading() already leaves
        # the valve frozen in whatever state it was last commanded to -
        # fine for a few missed reports, not fine forever. These two
        # dicts track, per zone name, when a currently-unreachable sensor
        # was FIRST seen unreachable (so a brief blip doesn't reset the
        # clock on every tick) and whether the failsafe has already
        # fired for it, so _apply() is only called once per unreachable
        # spell instead of every tick after the threshold.
        self._silence = SilenceLog(silence_file)
        self._failsafe_active: dict[str, bool] = {}
        # Time between ticks counts toward a sensor's silence, but never
        # more than a couple of cycles at once: the gap across a restart
        # (or three days with the board switched off) is time nobody was
        # listening, not time the sensor stayed quiet.
        self._max_silence_step = max(2 * poll_interval_seconds, 120)
        self._last_tick_ts: float | None = None
        # When the last tick finished without raising - the admin panel's
        # system status flags a control loop that has stopped completing,
        # which nothing else on the dashboard would show. On the monotonic
        # clock: the board has no clock battery, so after being switched
        # off it starts on the last time it saved and jumps hours ahead
        # once synchronized - on the wall clock that read as a loop stalled
        # for hours, and the service watchdog restarted LocalHome for it.
        self.last_tick_ok_ts: float | None = None
        # valve name -> the mode an interlock owns it in (None = every
        # mode), set by create_app() once interlocks are parsed. A zone
        # excluded from cooling leaves such a valve to its interlock (e.g.
        # a water-based dehumidifier fed through it); any other excluded
        # valve is held closed in cool mode - see _tick().
        self.interlock_owned_valves: dict[str, str | None] = {}
        # One tick at a time. The control loop has its own thread, and
        # every mode/season/schedule/away change ticks from the request
        # thread as well - two of them interleaving would double-count a
        # silent sensor's clock and race each other's valve commands.
        self._tick_lock = threading.RLock()
        self._missing_valves: set[str] = set()
        self._failures = FailureLog(logger)

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
        # Rebuilt from scratch every tick and swapped in whole at the end
        # (a single assignment - the history recorder reads it from its
        # own thread). Only a zone whose valve this tick actually decided
        # from a schedule target gets an entry: away, diagnostic mode, a
        # cooling-excluded zone or an unreachable sensor all mean no target
        # was governing the valve, and the chart should show a gap there
        # rather than a line that implies otherwise.
        applied: dict[str, float] = {}
        # What this cycle is worth to a silent sensor's clock (see
        # SilenceLog): the time since the last tick, capped so a gap - a
        # restart, or a board that was off - doesn't count as silence.
        with self._tick_lock:
            tick_ts = time.time()
            # Never negative either: the clock can be set back too.
            elapsed = max(0.0, min(tick_ts - (self._last_tick_ts or tick_ts), self._max_silence_step))
            self._last_tick_ts = tick_ts
            try:
                self._tick(now, applied, elapsed)
                self.last_tick_ok_ts = time.monotonic()
            finally:
                self._applied_targets = applied

    def valve_owned_by_interlock(self, valve: str, mode: str) -> bool:
        if valve not in self.interlock_owned_valves:
            return False
        owned_in = self.interlock_owned_valves[valve]
        return owned_in is None or owned_in == mode

    def applied_target(self, zone_name: str) -> float | None:
        """The target the last tick actually drove this zone's valve
        against, or None if no target was in control - see tick()."""
        return self._applied_targets.get(zone_name)

    def _tick(self, now: datetime | None, applied: dict[str, float], elapsed: float = 0.0) -> None:
        if self.schedule_store.get_diagnostic_mode():
            # Hands off *everything*, not even a forced close - see
            # ScheduleStore.get_diagnostic_mode()'s docstring. Whatever
            # the valves are doing right now is left exactly as-is for
            # the installer's manual toggles in /climate-admin to
            # control directly.
            return
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
        # A temporary target set from the dashboard beats the schedule for
        # its zone - not away mode, which holds everything off until it's
        # ended by hand or runs out (none can be set meanwhile, and going
        # away drops them). Not an excluded zone's, nor while diagnostic
        # mode is on (returned above).
        self.schedule_store.drop_expired_overrides(now)
        overrides = {} if away else self.schedule_store.get_overrides(now)
        for zone in self.zones:
            override = overrides.get(zone.name)
            if away:
                # Same exception as the cooling branch below: a valve an
                # interlock owns (a dehumidifier's water circuit) is that
                # interlock's to open and close. Forcing it closed here
                # would have the two loops fighting over it every cycle
                # while the dehumidifier runs - and an empty house is
                # exactly when it runs unattended.
                if self.valve_owned_by_interlock(zone.valve, mode):
                    continue
                self._apply(zone, False)
                continue
            if mode == "cool" and not zone.cool_enabled:
                # Excluded from cooling (condensation risk): never opened
                # for this zone's own temperature. If an interlock owns the
                # valve in this mode (a dehumidifier sharing its water
                # circuit), that interlock decides - it opens it only while
                # its leader runs, and closes it otherwise. Anything else
                # is held closed, so a valve left open from the heating
                # season can't keep feeding chilled water to these panels.
                if self.valve_owned_by_interlock(zone.valve, mode):
                    continue
                # _apply() checks the valve's real state, so one opened by
                # hand (or by anything else) closes again at the next tick.
                self._apply(zone, False)
                continue
            if not zone.sensor_configured:
                # Its sensor still to configure: nothing to go by, so its
                # valve (if there's one) stays closed. Not a silence -
                # nothing is there to fall silent.
                self._apply(zone, False)
                continue
            reading = self.manager.sensor_reading(zone.sensor)
            if not reading.get("ok") or "temperature_c" not in reading:
                self._on_sensor_unreachable(zone, elapsed)
                continue
            self._on_sensor_recovered(zone)
            # The same `mode` this tick already decided with, not a fresh
            # read - a season switch landing mid-tick must not pair one
            # season's grid with the other season's heat/cool decision.
            target = override["target"] if override else self.schedule_store.get_target(zone.name, now, mode)
            if target is not None:
                applied[zone.name] = target
            desired = self.decide(zone.name, reading["temperature_c"], target, mode)
            self._apply(zone, desired)

    def _on_sensor_unreachable(self, zone: Zone, elapsed: float) -> None:
        hours = self._silence.add(zone.name, elapsed) / 3600
        if hours < self.sensor_failsafe_hours:
            return
        if not self._failsafe_active.get(zone.name):
            logger.warning(
                "Zone %r's sensor %r has been silent for %.1fh of watched time (>= %.1fh threshold) - "
                "forcing valve %r closed as a failsafe. Clear this from /climate-admin once "
                "the sensor is fixed, or use diagnostic mode to control the valve manually.",
                zone.name, zone.sensor, hours, self.sensor_failsafe_hours, zone.valve,
            )
            self._failsafe_active[zone.name] = True
        # Every tick while it lasts, not only the first: _apply() compares
        # the valve's real state, so one opened by hand closes again.
        self._apply(zone, False)

    def _on_sensor_recovered(self, zone: Zone) -> None:
        if self._failsafe_active.get(zone.name):
            logger.info("Zone %r's sensor %r is reporting again - failsafe lifted", zone.name, zone.sensor)
        self._silence.clear(zone.name)
        self._failsafe_active.pop(zone.name, None)

    def is_failsafe_active(self, zone_name: str) -> bool:
        return self._silence.seconds(zone_name) >= self.sensor_failsafe_hours * 3600

    def unreachable_hours(self, zone_name: str) -> float | None:
        seconds = self._silence.seconds(zone_name)
        return seconds / 3600 if seconds else None

    def forget_valve_state(self) -> None:
        """Discards _apply()'s memory of what it last commanded every
        valve to. Needed specifically when leaving diagnostic mode: an
        installer may have manually turned a valve on/off through
        /climate-admin's diagnostic section while the thermostat was
        hands-off, and _apply()'s "skip if this already matches what I
        last commanded" optimization has no way to know that happened -
        without this, the very next tick could wrongly skip a real
        correction just because its own stale memory happens to already
        agree with the newly-computed desired state, leaving the valve
        stuck in whatever the manual test left it at. Call this once,
        right before the first tick() after diagnostic mode turns off -
        see web/app.py's api_climate_set_diagnostic_mode()."""
        self._valve_open.clear()

    def resync_after_diagnostic_mode(self) -> None:
        """The other half of leaving diagnostic mode, alongside
        forget_valve_state() (call both, then tick() - see
        web/app.py's api_climate_set_diagnostic_mode()). forget_valve_state()
        alone isn't enough for a zone whose sensor is *currently*
        unreachable: tick()'s own per-zone loop skips such a zone
        entirely (`if not reading.get("ok")... continue`, a few lines
        up) precisely so a brief outage doesn't flap the valve back and
        forth - it never even reaches decide()/_apply() for it, so
        nothing about the tick that follows this would ever revisit
        whatever an installer left that valve at during diagnostic
        mode. That's fine during ordinary operation (there's no new
        information to act on either way), but it's the wrong default
        right when diagnostic mode ends: a manual test has no relation
        at all to this zone's actual temperature, so trusting it any
        longer than diagnostic mode itself was on is unsafe, the same
        reasoning the sensor failsafe (_on_sensor_unreachable() above)
        already applies after a long outage - just applied immediately
        here instead of after sensor_failsafe_hours, because diagnostic
        mode having been on is itself the signal that whatever state is
        showing right now cannot be trusted. A zone whose sensor *is*
        reachable needs nothing here - the tick() called right after
        this recomputes it for real, from a real reading."""
        for zone in self.zones:
            reading = self.manager.sensor_reading(zone.sensor)
            if not reading.get("ok") or "temperature_c" not in reading:
                self._apply(zone, False)

    def thresholds(self, target_c: float | None, mode: str) -> tuple[float, float] | None:
        """Where the valve opens again and where it stops, for this target:
        (opens_at, stops_at). It stops at the target itself and opens again
        the hysteresis away on the season's side - below it heating, above
        it cooling - plus the economy tolerance for an economy target."""
        if target_c is None:
            return None
        spread = self.hysteresis_c + (self.economy_tolerance_c if target_tier(target_c, mode) == "economy" else 0.0)
        opens_at = target_c - spread if mode == "heat" else target_c + spread
        return round(opens_at, 2), round(target_c, 2)

    def decide(self, zone_name: str, current_c: float, target_c: float | None, mode: str) -> bool:
        """Pure decision logic, kept separate from _apply()/manager access
        so it's trivially unit-testable. A threshold reached counts: the
        sensor reports in half-degree steps, and one that lands exactly on
        it is acted on, not left for the next step."""
        if target_c is None:
            return False
        opens_at, stops_at = self.thresholds(target_c, mode)
        previously_open = self._valve_open.get(zone_name, False)
        current = round(current_c, 2)
        if mode == "heat":
            if current <= opens_at:
                return True
            if current >= stops_at:
                return False
        else:  # cool
            if current >= opens_at:
                return True
            if current <= stops_at:
                return False
        return previously_open  # between the two - keep doing whatever it was doing

    def _apply(self, zone: Zone, desired_open: bool) -> None:
        if not zone.valve_configured:
            return   # its Shelly still to configure: nothing to drive
        # Compared with the valve's real state whenever it's answering, not
        # just with the last command sent: a relay that rebooted into "off",
        # or was switched from its own app, used to stay wrong until the
        # decision itself changed - a heating zone with a closed valve kept
        # cooling down, the thermostat convinced it was open. The last
        # command is only the fallback while the valve isn't answering, so
        # an offline valve isn't sent a command on every tick.
        reading = self.manager.sensor_reading(zone.valve)
        if reading.get("ok") and "is_on" in reading:
            if bool(reading["is_on"]) == desired_open:
                self._valve_open[zone.name] = desired_open
                return
            if self._valve_open.get(zone.name) == desired_open:
                logger.warning(
                    "Zone %r's valve %r is %s but should be %s - sending the command again",
                    zone.name, zone.valve, "open" if reading["is_on"] else "closed",
                    "open" if desired_open else "closed",
                )
        elif self._valve_open.get(zone.name) == desired_open:
            return
        switch = self.manager.switches.get(zone.valve)
        if switch is None:
            if zone.valve not in self._missing_valves:  # once, not every tick
                self._missing_valves.add(zone.valve)
                logger.warning("Zone %r's valve %r is not a known switch, skipping", zone.name, zone.valve)
            return
        try:
            switch.turn_on() if desired_open else switch.turn_off()
        except Exception:
            # Retried every tick until it works; logged in full once, then
            # briefly every 10 minutes - see services/log_throttle.py.
            self._failures.failed(zone.valve, "Failed to %s zone %r's valve %r",
                                  "open" if desired_open else "close", zone.name, zone.valve)
            return
        self._failures.succeeded(zone.valve)
        poller = self.manager.pollers.get(zone.valve)
        if poller is not None:
            poller.cache.merge({"is_on": desired_open})
        self._valve_open[zone.name] = desired_open
