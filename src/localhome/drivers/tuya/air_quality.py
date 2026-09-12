"""Driver for Tuya's "9-in-1"/"12-in-1"/"14-in-1" air-quality sensor
(product category `hjjcy`). Unlike the temperature/humidity-only sensor
in the same product line, this one answers direct LAN polls (tinytuya
protocol v3.5) - no cloud dependency.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.tuya.client import load_devices, make_device

CATEGORY = "hjjcy"

# Tuya "dps" id -> (reading field name, raw-value transform).
FIELDS = {
    "1": ("air_quality_index", lambda v: v),
    "2": ("temperature_c", lambda v: v / 10),
    "3": ("humidity_pct", lambda v: v),
    "4": ("co2_ppm", lambda v: v),
    "5": ("ch2o_mgm3", lambda v: v / 1000),
    "6": ("voc_mgm3", lambda v: v / 1000),
    "7": ("pm25_ugm3", lambda v: v),
    "9": ("pm10_ugm3", lambda v: v),
    "22": ("battery_pct", lambda v: v),
}


class TuyaAirQualityDriver(PollingDriver):
    history_fields = ("temperature_c", "humidity_pct")

    def __init__(self, entry: dict, poll_interval_seconds: float = 30.0):
        self.name = entry["name"]
        self._entry = entry
        self._version = entry.get("version", 3.5)
        self.poll_interval_seconds = poll_interval_seconds

    def read(self) -> dict[str, Any]:
        device = make_device(self._entry, version=self._version)
        status = device.status()
        dps = status.get("dps")
        if not dps:
            raise RuntimeError(status.get("Error") or f"unexpected response: {status}")

        reading: dict[str, Any] = {"ok": True, "name": self.name}
        for dp_id, (key, transform) in FIELDS.items():
            if dp_id in dps:
                reading[key] = transform(dps[dp_id])
        return reading


@register_driver("climate", "tuya_air_quality")
def _create(options: dict) -> list[TuyaAirQualityDriver]:
    poll_interval = options.get("poll_interval_seconds", 30.0)
    entries = load_devices(options["devices_file"], category=CATEGORY)
    return [TuyaAirQualityDriver(e, poll_interval_seconds=poll_interval) for e in entries]
