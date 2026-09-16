"""A debug sensor for the climate admin panel's "manual" zone mode:
reports whatever temperature/humidity value was last set through the
panel, instead of reading any real or simulated device - see
services/manual_override.py for the tiny persisted value store this
reads from.

Exists so an installer/operator can verify services/thermostat.py's
on/off decision at a chosen temperature without a real device event or
a simulated drift. The value stays fixed (unlike drivers/simulated/
climate.py's drift) until explicitly changed or the zone is switched
back to "shelly"/"simulated" mode - the same "a persisted value the
control loop checks every tick" shape as ScheduleStore.away_until.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.services.manual_override import ManualOverrideStore


class ManualOverrideDriver(PollingDriver):
    def __init__(self, name: str, store: ManualOverrideStore, poll_interval_seconds: float = 5.0):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.history_fields = ("temperature_c", "humidity_pct")
        self._store = store

    def read(self) -> dict[str, Any]:
        value = self._store.get(self.name)
        if value is None:
            return {
                "ok": False,
                "name": self.name,
                "error": "no manual value set yet - set one from the climate admin panel",
            }
        reading: dict[str, Any] = {
            "ok": True,
            "name": self.name,
            "temperature_c": value["temperature_c"],
            "manual_override": True,
        }
        if value.get("humidity_pct") is not None:
            reading["humidity_pct"] = value["humidity_pct"]
        return reading


def _create(options: dict) -> ManualOverrideDriver:
    return ManualOverrideDriver(
        name=options["name"],
        store=ManualOverrideStore(options["store_path"]),
        poll_interval_seconds=options.get("poll_interval_seconds", 5.0),
    )


register_driver("climate", "manual_override")(_create)
