"""Per-device dashboard-layout overrides - Home-tab show/hide, drag order,
number/switch pairing, mode-based visibility - editable from the admin
panel without ever touching config.yaml.

This is the one mechanism that lets the admin panel affect a device that
was hand-written in config.yaml: `api_devices()` in web/app.py layers
whatever's here over `DeviceManager`'s own `dashboard_tab`/
`visible_in_mode`/`paired_switch` defaults, keyed by device name,
regardless of whether that device came from config.yaml or from the
panel's own discovery flow. Only fields that differ from the computed
default need to be present for a given device.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)


class DeviceOverrideStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"devices": {}}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s - starting from defaults", self.path)
            return {"devices": {}}

    def _save(self, data: dict) -> None:
        save_json_atomic(self.path, data)

    def get(self, name: str) -> dict[str, Any]:
        return self._load().get("devices", {}).get(name, {})

    def get_all(self) -> dict[str, dict[str, Any]]:
        return self._load().get("devices", {})

    def set_override(self, name: str, **fields: Any) -> None:
        """Merges `fields` into `name`'s existing overrides. A field set
        to None clears it (falls back to the computed default) rather
        than storing a literal null - and once a device has no overrides
        left, its entry is dropped entirely rather than kept as `{}`."""
        with self._lock:
            data = self._load()
            devices = data.setdefault("devices", {})
            existing = devices.setdefault(name, {})
            for key, value in fields.items():
                if value is None:
                    existing.pop(key, None)
                else:
                    existing[key] = value
            if not existing:
                devices.pop(name, None)
            self._save(data)

    def set_order(self, order: list[str]) -> None:
        """Replaces every listed device's `order` in one pass. A
        drag-and-drop save always sends the full, dense ordering of
        every currently-visible device, never a single moved item -
        patching just one would leave everyone else on their
        index-based fallback order, which collides/interleaves the
        moment even one device has an explicit order."""
        with self._lock:
            data = self._load()
            devices = data.setdefault("devices", {})
            for index, name in enumerate(order):
                devices.setdefault(name, {})["order"] = index
            self._save(data)

    def reset(self, name: str) -> None:
        with self._lock:
            data = self._load()
            data.get("devices", {}).pop(name, None)
            self._save(data)
