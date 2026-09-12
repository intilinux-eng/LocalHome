"""Driver for Tuya-based motorized covers exposed as a "Curtain switch"
device (Tuya product category `clkg`, or `tdq` on newer firmware -
LoraTap's WiFi roller shutter modules use this). Controlled and polled
entirely over the LAN via tinytuya; no cloud dependency at runtime.

These modules report only the current motor command (open/close/stop),
never an absolute position - see services/position_control.py for how an
approximate 0-100% position is derived from that.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import CoverDriver
from localhome.core.registry import register_driver
from localhome.drivers.tuya.client import load_devices, make_device

DEFAULT_CATEGORIES = ("clkg", "tdq")


class TuyaCoverDriver(CoverDriver):
    def __init__(self, entry: dict, version: float = 3.4):
        self.id = entry["id"]
        self.name = entry["name"]
        self._entry = entry
        self._version = version

    def _device(self):
        return make_device(self._entry, version=self._version)

    def status(self) -> dict[str, Any]:
        raw = self._device().status()
        dps = raw.get("dps", {})
        return {"state": dps.get("1", "unknown"), "raw": raw}

    def send(self, action: str) -> None:
        self._device().set_value(1, action)

    def read_travel_time(self, default: float = 15.0) -> float:
        try:
            raw = self._device().status()
            dps = raw.get("dps", {})
            if "10" in dps:
                return float(dps["10"])
        except Exception:
            pass
        dp10 = self._entry.get("mapping", {}).get("10", {})
        return float(dp10.get("values", {}).get("max", default))


@register_driver("cover", "tuya_curtain_switch")
def _create(options: dict) -> list[TuyaCoverDriver]:
    categories = options.get("categories", DEFAULT_CATEGORIES)
    version = options.get("version", 3.4)
    entries = [e for e in load_devices(options["devices_file"]) if e.get("category") in categories]
    return [TuyaCoverDriver(e, version=version) for e in entries]
