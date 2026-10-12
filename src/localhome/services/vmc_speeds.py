"""The VMC's five speeds - minimum, night, day, high, boost - as set in the
admin panel.

A unit's speeds come with its device: config.yaml's `presets`, or the
admin panel's defaults for one it manages. Set here, they take their
place everywhere the speeds are used - the card's buttons and its fine
slider (which starts at the minimum), the ready-made programs, the timed
boost, the automatics, away - with no restart: they're laid over the
device's presets each time the pages ask for them (web/app.py's
/api/devices). Kept in config/vmc_speeds.json; nothing saved means the
device's own.

The unit had three speeds first (night, day, boost); minimum and high
came later. A file or a device that names only the three gets the other
two worked out from them (complete()): the minimum at 10% under night -
the least that still changes the air of an empty house - and high at
75% between day and boost, or halfway where those don't fit.
"""
from __future__ import annotations

import logging
import threading

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

SPEED_KEYS = ("min", "night", "day", "high", "boost")
DEFAULT_SPEEDS = {"min": 10, "night": 25, "day": 50, "high": 75, "boost": 100}


def complete(speeds: dict) -> dict:
    """`speeds` with the minimum and high worked out when they're missing
    (an older file, a device's three presets) - see the module's
    docstring. One that can't fit (night at 1%) is left out."""
    out = dict(speeds)
    night = out.get("night")
    if "min" not in out and isinstance(night, (int, float)):
        low = DEFAULT_SPEEDS["min"] if DEFAULT_SPEEDS["min"] < night else int(night) // 2
        if 1 <= low < night:
            out["min"] = low
    day, boost = out.get("day"), out.get("boost")
    if "high" not in out and isinstance(day, (int, float)) and isinstance(boost, (int, float)):
        high = DEFAULT_SPEEDS["high"] if day < DEFAULT_SPEEDS["high"] < boost else int(day + boost) // 2
        if day < high < boost:
            out["high"] = high
    return out


def validate(speeds) -> dict[str, int]:
    """Whole percentages, 1 to 100 (0 is "off", the card's switch), each
    above the one before - so a speed is never two buttons at once."""
    if not isinstance(speeds, dict):
        raise InputError("request_invalid", "the speeds must be an object")
    clean = {}
    for key in SPEED_KEYS:
        value = speeds.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 100:
            raise InputError("vmc_speeds_invalid", f"{key}: a speed must be between 1 and 100, got {value!r}")
        clean[key] = round(float(value))
    if not all(clean[a] < clean[b] for a, b in zip(SPEED_KEYS, SPEED_KEYS[1:])):
        raise InputError("vmc_speeds_order", "each speed must be faster than the one before: min < night < day < high < boost")
    return clean


class VmcSpeedStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict[str, int] | None:
        try:
            return validate(complete(load_json(self.path)))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            logger.warning("Ignoring unreadable VMC speeds in %s (%s) - using the device's own", self.path, exc)
            return None

    def set(self, speeds: dict) -> dict[str, int]:
        clean = validate(speeds)
        with self._lock:
            save_json_atomic(self.path, clean)
        return clean


def apply(presets: list[dict], speeds: dict[str, int] | None) -> list[dict]:
    """The presets with the speeds set here in place of their own, and the
    five complete - a device with the classic three gains the other two -
    slowest first. Presets of other keys are left as they are."""
    standard = {p["key"]: p for p in presets if p.get("key") in SPEED_KEYS}
    if not standard:
        return presets
    values = complete({**{k: p["value"] for k, p in standard.items()}, **(speeds or {})})
    levels = [{**standard.get(k, {"key": k}), "value": v} for k, v in values.items() if k in SPEED_KEYS]
    others = [p for p in presets if p.get("key") not in SPEED_KEYS]
    return sorted(levels, key=lambda p: p["value"]) + others


def of_presets(presets: list[dict]) -> dict[str, int]:
    return {p["key"]: p["value"] for p in presets if p.get("key") in SPEED_KEYS}


def remap_week(week: dict[str, list[int]], old: dict[str, int], new: dict[str, int]) -> dict[str, list[int]]:
    """A program's week in the new speeds: each hour at an old speed moves
    to the new one of the same level - "night" stays night."""
    moves = {old[k]: new[k] for k in SPEED_KEYS if k in old and k in new}
    return {day: [moves.get(v, v) for v in hours] for day, hours in week.items()}
