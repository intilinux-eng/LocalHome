"""Persists a debug/testing value for a zone whose sensor is in "manual"
mode - see the climate admin panel (web/routes/admin.py)
and drivers/manual/climate.py's ManualOverrideDriver, which is the only
reader. The point: verify services/thermostat.py's on/off decision at a
chosen temperature without a real device event or a simulated drift.
Keyed by the driver's own name (e.g. "Rooms Sensor"), the same "one name
everything else already uses" convention as ScheduleStore (zone name) and
PositionStore (cover id).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)


class ManualOverrideStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s - manual overrides reset to empty", self.path)
            return {}

    def get(self, key: str) -> dict | None:
        return self._load().get(key)

    def set(self, key: str, temperature_c: float, humidity_pct: float | None = None) -> None:
        with self._lock:
            data = self._load()
            data[key] = {
                "temperature_c": float(temperature_c),
                "humidity_pct": float(humidity_pct) if humidity_pct is not None else None,
            }
            save_json_atomic(self.path, data)
