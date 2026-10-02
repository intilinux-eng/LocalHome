"""The VMC's three speeds - night, day, boost - as set in the admin panel.

A unit's speeds come with its device: config.yaml's `presets`, or the
admin panel's defaults for one it manages. Set here, they take their
place everywhere the speeds are used - the card's buttons, the ready-made
programs, the timed boost - with no restart: they're laid over the
device's presets each time the pages ask for them (web/app.py's
/api/devices). Kept in config/vmc_speeds.json; nothing saved means the
device's own.
"""
from __future__ import annotations

import logging
import threading

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

SPEED_KEYS = ("night", "day", "boost")


def validate(speeds) -> dict[str, int]:
    """Whole percentages, 1 to 100 (0 is "off", its own button), each
    above the one before - so a speed is never two buttons at once."""
    if not isinstance(speeds, dict):
        raise InputError("request_invalid", "the speeds must be an object")
    clean = {}
    for key in SPEED_KEYS:
        value = speeds.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 100:
            raise InputError("vmc_speeds_invalid", f"{key}: a speed must be between 1 and 100, got {value!r}")
        clean[key] = round(float(value))
    if not clean["night"] < clean["day"] < clean["boost"]:
        raise InputError("vmc_speeds_order", "night must be slower than day, and day slower than boost")
    return clean


class VmcSpeedStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict[str, int] | None:
        try:
            return validate(load_json(self.path))
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
    """The presets with the speeds set here in place of their own."""
    if not speeds:
        return presets
    return [{**p, "value": speeds[p["key"]]} if p.get("key") in speeds else p for p in presets]


def of_presets(presets: list[dict]) -> dict[str, int]:
    return {p["key"]: p["value"] for p in presets if p.get("key") in SPEED_KEYS}


def remap_week(week: dict[str, list[int]], old: dict[str, int], new: dict[str, int]) -> dict[str, list[int]]:
    """A program's week in the new speeds: each hour at the old night,
    day or boost speed moves to the new one - "night" stays night."""
    moves = {old[k]: new[k] for k in SPEED_KEYS if k in old and k in new}
    return {day: [moves.get(v, v) for v in hours] for day, hours in week.items()}
