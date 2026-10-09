"""The VMC's automatic adjustments, on top of its program and of a speed
picked by hand - each switched on in the admin panel (all off by default:
the VMC is usually the one device already in daily use, and an update
mustn't change what it does unasked; whoever knows where the sensors
are switches them on), with thresholds after the way ventilation units
with their own humidity and temperature sensors do it:

shower   the bathroom's humidity rising fast - `shower_rise` points within
         `shower_window_minutes` (a shower takes it from the fifties to the
         eighties in a few minutes; washing hands or a humid day doesn't) -
         runs the unit at its maximum until the humidity is back within
         `shower_back_within` points of where it started: at least
         `shower_min_minutes`, at most `shower_max_minutes`. A rate of rise
         rather than a fixed threshold, as the better hygrostats do: a
         bathroom's ordinary humidity moves with the season, and a fixed
         70% would fire all summer. In the quiet hours the day speed
         instead: the maximum is loud.
humid    summer only: the outdoor air holding more water than the indoor -
         its dew point `humid_margin_c` above the reference room's - the
         night speed, the least air that still ventilates; never off.
         Back when the outdoor dew point is down to the indoor one.
cooling  summer only, night cooling: the house warm (the reference room at
         `cooling_from_c` or more), the outdoor air `cooling_delta_c`
         cooler but not under `cooling_outdoor_min_c`, and no more humid
         than the indoor - the day speed. The floor is the unit's own
         summer bypass: it lets the cool air past the heat exchanger only
         above it (a Maico AERA 320's opens with indoor > 22 °C, indoor >
         outdoor and outdoor > 16 °C), and air through the exchanger
         doesn't cool. Not the maximum: it's a night thing, and loud.

The adjustments never switch the unit on (off is someone's choice); a
speed picked by hand - on the home page, in the Shelly app, on the
unit's regulator - wins over the adjustments under way until their
reasons pass; a timed speed from the home page wins over everything. The
thresholds to stop are set apart from those to start, so an adjustment
doesn't come and go with each report. Settings in config/vmc_auto.json
(only what differs from the defaults), read at every check: a change
applies at once, no restart. The state - a shower under way - is in
memory: after a restart the program's speed is put back, and a shower
still going is seen again only if the humidity rises again.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from localhome.errors import InputError
from localhome.services.dehumidifier import in_window
from localhome.services.dewpoint import dew_point_c
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

DEFAULTS = {
    "shower": False,
    "shower_rise": 8,
    "shower_window_minutes": 10,
    "shower_back_within": 5,
    "shower_min_minutes": 15,
    "shower_max_minutes": 60,
    "quiet_from_hour": 22,
    "quiet_to_hour": 7,
    "humid": False,
    "humid_margin_c": 1.0,
    "cooling": False,
    "cooling_from_c": 24.0,
    "cooling_delta_c": 3.0,
    "cooling_outdoor_min_c": 16.0,
    # None: the zone out of cooling (the bathrooms) and the first one
    # cooled - see zones_for().
    "bathroom_zone": None,
    "reference_zone": None,
}
RANGES = {
    "shower_rise": (3, 30),
    "shower_window_minutes": (3, 30),
    "shower_back_within": (1, 20),
    "shower_min_minutes": (5, 60),
    "shower_max_minutes": (10, 180),
    "quiet_from_hour": (0, 23),
    "quiet_to_hour": (0, 23),
    "humid_margin_c": (0.5, 5),
    "cooling_from_c": (20, 30),
    "cooling_delta_c": (1, 10),
    "cooling_outdoor_min_c": (5, 25),
}
SWITCHES = ("shower", "humid", "cooling")
ZONE_KEYS = ("bathroom_zone", "reference_zone")
HALF_DEGREE_KEYS = ("humid_margin_c", "cooling_from_c", "cooling_delta_c", "cooling_outdoor_min_c")
# Stopping, a little past where it started - see the module's docstring.
COOLING_STOP_BELOW_BY = 0.5     # the house this much under cooling_from_c
COOLING_STOP_DELTA_BY = 1.0     # the outdoor air this much less cool than the delta
COOLING_STOP_OUTDOOR_BY = 0.5   # the outdoor air this much under its floor
COOLING_STOP_HUMID_BY = 0.5     # the outdoor dew point this much over the indoor one
REASONS = ("shower", "humid", "cooling")   # in this order, the first under way wins


def validate(values: dict, base: dict | None = None) -> dict:
    """The settings `values` change, on top of `base` (the defaults if
    none): raises InputError naming the first problem."""
    if not isinstance(values, dict):
        raise InputError("request_invalid", "settings must be an object")
    clean = dict(base or DEFAULTS)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise InputError("setting_unknown", f"unknown setting '{key}'", key=key)
        if key in SWITCHES:
            if not isinstance(value, bool):
                raise InputError("request_invalid", f"{key}: must be true or false")
            clean[key] = value
        elif key in ZONE_KEYS:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InputError("request_invalid", f"{key}: a zone's name, or null for the automatic choice")
            clean[key] = value.strip() if isinstance(value, str) else None
        else:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise InputError("setting_range", f"{key}: must be between {low} and {high}", key=key, min=low, max=high)
            clean[key] = round(value * 2) / 2 if key in HALF_DEGREE_KEYS else int(round(value))
    if clean["shower_max_minutes"] < clean["shower_min_minutes"]:
        raise InputError("vmc_auto_shower_times", "the longest a shower boost lasts can't be under the shortest")
    if clean["quiet_from_hour"] == clean["quiet_to_hour"]:
        raise InputError("vmc_auto_quiet_hours", "the quiet hours can't start and end at the same hour")
    return clean


class VmcAutoSettingsStore:
    """config/vmc_auto.json: only what was changed from the defaults is
    kept - a default changed in a later version applies."""

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
        try:
            return validate(self._saved())
        except InputError:
            logger.warning("Ignoring invalid %s - using the VMC adjustments' defaults", self.path)
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


def zones_for(settings: dict, zones: list[dict]) -> tuple[dict | None, dict | None]:
    """The bathroom and the reference room among `zones` ({"name",
    "sensor", "cool_enabled"}): the ones the settings name, else the zone
    kept out of cooling (a bathroom's ceiling would condense) and the
    first one cooled."""
    by_name = {z["name"]: z for z in zones}
    bathroom = by_name.get(settings["bathroom_zone"]) if settings["bathroom_zone"] else \
        next((z for z in zones if not z.get("cool_enabled", True)), None)
    reference = by_name.get(settings["reference_zone"]) if settings["reference_zone"] else \
        next((z for z in zones if z.get("cool_enabled", True)), None)
    return bathroom, reference


@dataclass
class Air:
    """A sensor's reading the adjustments go by - one answering, with both
    values."""
    temperature_c: float
    humidity_pct: float

    @property
    def dew_point_c(self) -> float:
        return dew_point_c(self.temperature_c, self.humidity_pct)


def air_of(reading: dict | None) -> Air | None:
    if not reading or not reading.get("ok"):
        return None
    t, rh = reading.get("temperature_c"), reading.get("humidity_pct")
    if not isinstance(t, (int, float)) or not isinstance(rh, (int, float)) or rh <= 0:
        return None
    return Air(float(t), float(rh))


@dataclass
class Adjustment:
    reason: str
    value: int


class VmcAutomation:
    """What the adjustments want now, from the readings - the state of
    each (a shower under way, the outdoor air humid, night cooling), and,
    given the speed it would otherwise run at, the one to set."""

    def __init__(self):
        self.samples: deque[tuple[float, float]] = deque()   # the bathroom's humidity, (time, %)
        self.shower: dict | None = None                       # {"since", "baseline"}
        self.humid = False
        self.cooling = False
        self.dismissed: set[str] = set()                      # under way, but someone picked a speed
        self.last: dict = {}                                  # what each one went by, for the pages

    def observe(self, now: float, settings: dict, season: str | None,
                bathroom: Air | None, indoor: Air | None, outdoor: Air | None) -> None:
        self._shower(now, settings, bathroom)
        self._humid(settings, season, indoor, outdoor)
        self._cooling(settings, season, indoor, outdoor)
        self.dismissed &= set(self.active())

    def _shower(self, now: float, settings: dict, bathroom: Air | None) -> None:
        window = settings["shower_window_minutes"] * 60
        if bathroom is not None:
            self.samples.append((now, bathroom.humidity_pct))
        while self.samples and self.samples[0][0] < now - window:
            self.samples.popleft()
        if self.shower is not None:
            elapsed = now - self.shower["since"]
            back = bathroom is not None and \
                bathroom.humidity_pct <= self.shower["baseline"] + settings["shower_back_within"]
            if not settings["shower"] or elapsed >= settings["shower_max_minutes"] * 60 or \
                    (back and elapsed >= settings["shower_min_minutes"] * 60):
                self.shower = None
        elif settings["shower"] and bathroom is not None and self.samples:
            lowest = min(rh for _, rh in self.samples)
            if bathroom.humidity_pct - lowest >= settings["shower_rise"]:
                self.shower = {"since": now, "baseline": lowest}
        if bathroom is not None:
            self.last["shower"] = {"humidity": bathroom.humidity_pct,
                                   "baseline": self.shower["baseline"] if self.shower else None}

    def _humid(self, settings: dict, season: str | None, indoor: Air | None, outdoor: Air | None) -> None:
        if season != "cool" or not settings["humid"] or indoor is None or outdoor is None:
            self.humid = False
            return
        inside, outside = indoor.dew_point_c, outdoor.dew_point_c
        if self.humid:
            self.humid = outside > inside
        else:
            self.humid = outside >= inside + settings["humid_margin_c"]
        self.last["humid"] = {"outdoor_dew": round(outside, 1), "indoor_dew": round(inside, 1)}

    def _cooling(self, settings: dict, season: str | None, indoor: Air | None, outdoor: Air | None) -> None:
        if season != "cool" or not settings["cooling"] or indoor is None or outdoor is None:
            self.cooling = False
            return
        t_in, t_out = indoor.temperature_c, outdoor.temperature_c
        dew_in, dew_out = indoor.dew_point_c, outdoor.dew_point_c
        if self.cooling:
            self.cooling = not (t_in < settings["cooling_from_c"] - COOLING_STOP_BELOW_BY
                                or t_out > t_in - (settings["cooling_delta_c"] - COOLING_STOP_DELTA_BY)
                                or t_out < settings["cooling_outdoor_min_c"] - COOLING_STOP_OUTDOOR_BY
                                or dew_out > dew_in + COOLING_STOP_HUMID_BY)
        else:
            self.cooling = (t_in >= settings["cooling_from_c"] and t_out <= t_in - settings["cooling_delta_c"]
                            and t_out >= settings["cooling_outdoor_min_c"] and dew_out <= dew_in)
        self.last["cooling"] = {"outdoor": t_out, "indoor": t_in}

    def active(self) -> list[str]:
        return [r for r, on in (("shower", self.shower is not None), ("humid", self.humid), ("cooling", self.cooling))
                if on]

    def dismiss(self) -> None:
        """A speed picked by hand: what's under way now steps aside until
        its reason has passed."""
        self.dismissed |= set(self.active())

    def adjust(self, base: int | None, speeds: dict, quiet: bool) -> Adjustment | None:
        """The speed to run at instead of `base` - None to leave `base`.
        Never from off: an adjustment doesn't switch the unit on."""
        if not base or not speeds:
            return None
        for reason in REASONS:
            if reason not in self.active() or reason in self.dismissed:
                continue
            if reason == "shower":
                value = speeds["day"] if quiet else speeds["boost"]
                return Adjustment(reason, value) if base < value else None
            if reason == "humid":
                value = speeds["night"]
                return Adjustment(reason, value) if base > value else None
            value = speeds["day"]
            return Adjustment(reason, value) if base < value else None
        return None


class VmcAuto:
    """The adjustments wired to the house: the settings, the season, the
    sensors - for VmcScheduleController, which owns the unit's speed."""

    def __init__(self, manager, store: VmcAutoSettingsStore, season: Callable[[], str | None],
                 zones: Callable[[], list[dict]], outdoor_sensor: str | None,
                 device: Callable[[], str | None], speeds: Callable[[str], dict]):
        self.manager = manager
        self.store = store
        self.season = season
        self.zones = zones
        self.outdoor_sensor = outdoor_sensor
        self.device = device
        self.speeds = speeds
        self.automation = VmcAutomation()
        self.places: dict = {}

    def _air(self, name: str | None) -> Air | None:
        return air_of(self.manager.sensor_reading(name)) if name else None

    def observe(self, now: float) -> dict:
        settings = self.store.get()
        bathroom, reference = zones_for(settings, self.zones())
        self.places = {"bathroom": bathroom["name"] if bathroom else None,
                       "reference": reference["name"] if reference else None}
        self.automation.observe(now, settings, self.season(),
                                self._air(bathroom["sensor"]) if bathroom else None,
                                self._air(reference["sensor"]) if reference else None,
                                self._air(self.outdoor_sensor))
        return settings

    def adjust(self, base: int | None, device: str, settings: dict, hour: int) -> Adjustment | None:
        quiet = in_window(hour, settings["quiet_from_hour"], settings["quiet_to_hour"])
        speeds = self.speeds(device)
        if not all(k in speeds for k in ("night", "day", "boost")):
            return None
        return self.automation.adjust(base, speeds, quiet)

    def describe(self, reason: str) -> dict:
        """What the home page says of an adjustment under way."""
        detail = dict(self.automation.last.get(reason) or {})
        detail["zone"] = self.places.get("bathroom" if reason == "shower" else "reference")
        return detail
