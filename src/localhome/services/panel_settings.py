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

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# key -> where it lives in config.yaml, its type, range and built-in default
SETTINGS = {
    "language": {"section": "web", "option": "language", "type": "choice", "choices": ["it", "en"], "default": "en"},
    "hysteresis_c": {"section": "thermostat", "option": "hysteresis_c", "type": "float",
                     "min": 0.1, "max": 2.0, "step": 0.1, "default": 0.3},
    "thermostat_poll_seconds": {"section": "thermostat", "option": "poll_interval_seconds", "type": "int",
                                "min": 10, "max": 600, "step": 10, "default": 60},
    "sensor_failsafe_hours": {"section": "thermostat", "option": "sensor_failsafe_hours", "type": "float",
                              "min": 1, "max": 168, "step": 1, "default": 24},
    "history_retention_days": {"section": "history", "option": "retention_days", "type": "int",
                               "min": 1, "max": 365, "step": 1, "default": 30},
    "history_max_db_mb": {"section": "history", "option": "max_db_mb", "type": "int",
                          "min": 50, "max": 5000, "step": 50, "default": 300},
}


def validate(values: dict) -> dict:
    """The accepted overrides, converted to their type; raises ValueError
    naming the first bad one. None (or a missing key) means "not set
    here" - fall back to config.yaml."""
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    clean = {}
    for key, value in values.items():
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError(f"unknown setting '{key}'")
        if value is None or value == "":
            continue
        if spec["type"] == "choice":
            if value not in spec["choices"]:
                raise ValueError(f"{key}: must be one of {spec['choices']}")
            clean[key] = value
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: must be a number") from None
        if not spec["min"] <= number <= spec["max"]:
            raise ValueError(f"{key}: must be between {spec['min']} and {spec['max']}")
        clean[key] = int(round(number)) if spec["type"] == "int" else round(number, 2)
    return clean


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
            return validate(data if isinstance(data, dict) else {})
        except ValueError:
            logger.exception("Ignoring invalid %s - using config.yaml's settings", self.path)
            return {}

    def set(self, values: dict) -> dict:
        clean = validate(values)
        with self._lock:
            if clean:
                save_json_atomic(self.path, clean)
            elif os.path.exists(self.path):
                os.remove(self.path)
        return clean


def effective(config, overrides: dict) -> list[dict]:
    """Every setting with the value in force and where it comes from:
    "panel" (set here), "config" (config.yaml) or "default"."""
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
        for extra in ("min", "max", "step", "choices"):
            if extra in spec:
                row[extra] = spec[extra]
        rows.append(row)
    return rows


def apply(config, overrides: dict, thermostat_cfg: dict | None) -> dict | None:
    """Puts the overrides into the loaded config, before anything is built
    from it. Thermostat keys go into `thermostat_cfg` (the effective one
    from climate_map_store.apply_to_config()) and only when there is a
    thermostat at all - a setting alone doesn't create one. Returns
    `thermostat_cfg`."""
    for key, value in overrides.items():
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
