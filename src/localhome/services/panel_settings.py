"""The settings the admin panel can change without anyone editing
config.yaml or the code: thermostat tuning, history limits, language.

Saved in config/panel_settings.json (gitignored with the rest of
config/), they take precedence over the same keys in config.yaml - the
same "the panel's version wins" rule as the device mapping in
climate_map.json. Only keys set here are overridden; everything else
still comes from config.yaml, or from the built-in default. Applied at
startup (a restart after saving), like the device mapping.

Every setting has a range, checked before anything is saved: a typo
can't turn the thermostat off or fill the disk.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# A name has to fit the dashboard header on a phone and the label under a
# home-screen icon; past this it gets cut off by the screen instead of by
# us, which reads like a bug.
SITE_NAME_MAX = 24

# Settings that take effect at once, without a restart: the name is read
# again on every page render, so it must not be baked into the loaded
# config the way the others are (clearing it has to bring the old name
# back immediately too).
LIVE_KEYS = {"site_name"}

# key -> where it lives in config.yaml, its type, range and built-in default
SETTINGS = {
    "site_name": {"section": "web", "option": "site_name", "type": "text",
                  "max_length": SITE_NAME_MAX, "default": "LocalHome"},
    "language": {"section": "web", "option": "language", "type": "choice", "choices": ["it", "en"], "default": "en"},
    "hysteresis_c": {"section": "thermostat", "option": "hysteresis_c", "type": "float",
                     "min": 0.1, "max": 2.0, "step": 0.1, "default": 0.5},
    "economy_tolerance_c": {"section": "thermostat", "option": "economy_tolerance_c", "type": "float",
                            "min": 0, "max": 1.5, "step": 0.5, "default": 0.5},
    "thermostat_poll_seconds": {"section": "thermostat", "option": "poll_interval_seconds", "type": "int",
                                "min": 10, "max": 600, "step": 10, "default": 60},
    "sensor_failsafe_hours": {"section": "thermostat", "option": "sensor_failsafe_hours", "type": "float",
                              "min": 1, "max": 168, "step": 1, "default": 24},
    "history_retention_days": {"section": "history", "option": "retention_days", "type": "int",
                               "min": 1, "max": 365, "step": 1, "default": 30},
    "history_max_db_mb": {"section": "history", "option": "max_db_mb", "type": "int",
                          "min": 50, "max": 2000, "step": 50, "default": 300},
}

# The history's ceiling also follows the disk it's on, and stays well
# inside it: a tenth of the disk, and a third of the room there is (its
# free space plus what the history already takes) - room for the history,
# its backup copy before a deletion and SQLite's temporary copy while it's
# cleared, with the system's own needs on top. 2000 MB at most whatever
# the disk: a year of a house with fifteen sensors is about 1.5 GB.
HISTORY_DISK_SHARE = 0.10
HISTORY_ROOM_SHARE = 1 / 3


def history_ceiling_mb(disk: dict | None, history_bytes: int = 0) -> int:
    """The largest history this disk allows, in MB, on the setting's step
    - never under its minimum. A disk that can't be read: the setting's
    own maximum."""
    spec = SETTINGS["history_max_db_mb"]
    total = (disk or {}).get("total_bytes")
    free = (disk or {}).get("free_bytes")
    if not total or free is None:
        return spec["max"]
    room = min(total * HISTORY_DISK_SHARE, (free + history_bytes) * HISTORY_ROOM_SHARE)
    return max(spec["min"], min(spec["max"], int(room / 1_000_000) // spec["step"] * spec["step"]))


def validate(values: dict, ceilings: dict | None = None, clamp: bool = False) -> dict:
    """The accepted overrides, converted to their type; raises ValueError
    naming the first bad one. None (or a missing key) means "not set
    here" - fall back to config.yaml. `ceilings` lowers a setting's
    maximum (the history's, to what the disk allows). `clamp` brings a
    number outside its range back inside instead - for a file saved when
    the range was wider."""
    if not isinstance(values, dict):
        raise InputError("request_invalid", "settings must be an object")
    clean = {}
    for key, value in values.items():
        spec = SETTINGS.get(key)
        if spec is None:
            raise InputError("setting_unknown", f"unknown setting '{key}'", key=key)
        if value is None or value == "":
            continue
        if spec["type"] == "text":
            clean_text = _text(key, value, spec)
            if clean_text is not None:
                clean[key] = clean_text
            continue
        if spec["type"] == "choice":
            if value not in spec["choices"]:
                raise InputError("setting_choice", f"{key}: must be one of {spec['choices']}",
                                 key=key, choices=", ".join(spec["choices"]))
            clean[key] = value
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise InputError("setting_number", f"{key}: must be a number", key=key) from None
        high = min(spec["max"], (ceilings or {}).get(key, spec["max"]))
        if not spec["min"] <= number <= high:
            if not clamp:
                raise InputError("setting_range", f"{key}: must be between {spec['min']} and {high}",
                                 key=key, min=spec["min"], max=high)
            logger.warning("Setting %s: %s is outside %s-%s - using the nearest", key, value, spec["min"], high)
            number = min(max(number, spec["min"]), high)
        clean[key] = int(round(number)) if spec["type"] == "int" else round(number, 2)
    return clean


def _text(key: str, value, spec: dict) -> str | None:
    """A name someone typed, cleaned up and checked. None means "they left
    it blank" - not set here, so the default comes back."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise InputError("setting_text", f"{key}: must be text", key=key)
    text = " ".join(str(value).split())  # no line breaks, no runs of spaces
    if not text:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InputError("setting_control_chars", f"{key}: must not contain control characters", key=key)
    if len(text) > spec["max_length"]:
        raise InputError("setting_too_long", f"{key}: at most {spec['max_length']} characters, "
                                             f"this one has {len(text)}",
                         key=key, max=spec["max_length"], length=len(text))
    return text


class PanelSettingsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            data = load_json(self.path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s - using config.yaml's settings", self.path)
            return {}
        try:
            return validate(data if isinstance(data, dict) else {}, clamp=True)
        except ValueError:
            logger.exception("Ignoring invalid %s - using config.yaml's settings", self.path)
            return {}

    def set(self, values: dict, ceilings: dict | None = None) -> dict:
        clean = validate(values, ceilings)
        with self._lock:
            if clean:
                save_json_atomic(self.path, clean)
            elif os.path.exists(self.path):
                os.remove(self.path)
        return clean


def effective(config, overrides: dict, ceilings: dict | None = None) -> list[dict]:
    """Every setting with the value in force and where it comes from:
    "panel" (set here), "config" (config.yaml) or "default" - and, when
    that's over its ceiling, the lower one actually used."""
    rows = []
    for key, spec in SETTINGS.items():
        from_config = (config.raw.get(spec["section"]) or {}).get(spec["option"])
        if key in overrides:
            value, source = overrides[key], "panel"
        elif from_config is not None:
            value, source = from_config, "config"
        else:
            value, source = spec["default"], "default"
        row = {"key": key, "value": value, "source": source, "type": spec["type"],
               "config_value": from_config, "default": spec["default"]}
        for extra in ("min", "max", "step", "choices", "max_length"):
            if extra in spec:
                row[extra] = spec[extra]
        if key in (ceilings or {}):
            row["max"] = min(spec["max"], ceilings[key])
            if isinstance(value, (int, float)) and value > row["max"]:
                row["capped_to"] = row["max"]
        rows.append(row)
    return rows


def apply(config, overrides: dict, thermostat_cfg: dict | None) -> dict | None:
    """Puts the overrides into the loaded config, before anything is built
    from it. Thermostat keys go into `thermostat_cfg` (the effective one
    from climate_map_store.apply_to_config()) and only when there is a
    thermostat at all - a setting alone doesn't create one. Returns
    `thermostat_cfg`."""
    for key, value in overrides.items():
        if key in LIVE_KEYS:
            continue  # read straight from the store, every time it's shown
        spec = SETTINGS[key]
        if spec["section"] == "thermostat":
            if thermostat_cfg:
                thermostat_cfg[spec["option"]] = value
            continue
        config.raw.setdefault(spec["section"], {})
        if config.raw[spec["section"]] is None:
            config.raw[spec["section"]] = {}
        config.raw[spec["section"]][spec["option"]] = value
        if key == "language":
            config.web_language = value
    return thermostat_cfg


def needs_restart(saved: dict, active: dict) -> bool:
    """Whether the saved settings differ from the ones the running process
    started with - ignoring the ones that already took effect."""
    return {k: v for k, v in saved.items() if k not in LIVE_KEYS} != \
           {k: v for k, v in (active or {}).items() if k not in LIVE_KEYS}
