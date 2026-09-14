"""A fake temperature/humidity sensor - drifts slowly toward a resting
base value, or toward `influence_target_c` while a named simulated
switch (e.g. a simulated heating/cooling valve) is on - so a simulated
thermostat setup behaves believably: open the valve in the dashboard,
watch the temperature actually follow over the next few reads instead of
sitting at a static fake number. See docs/integrations/simulated.md.
"""
from __future__ import annotations

import random
import time
from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.simulated import world


class SimulatedClimateDriver(PollingDriver):
    history_fields = ("temperature_c", "humidity_pct")

    def __init__(
        self,
        name: str,
        base_temperature_c: float = 20.0,
        base_humidity_pct: float = 50.0,
        influenced_by: str | None = None,
        influence_target_c: float = 26.0,
        drift_rate_c_per_min: float = 0.15,
        poll_interval_seconds: float = 10.0,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self._temperature = base_temperature_c
        self._humidity = base_humidity_pct
        self._base_temperature = base_temperature_c
        self._influenced_by = influenced_by
        self._influence_target = influence_target_c
        self._drift_rate = drift_rate_c_per_min
        self._last_tick = time.time()

    def read(self) -> dict[str, Any]:
        now = time.time()
        elapsed_min = max(0.0, (now - self._last_tick) / 60.0)
        self._last_tick = now

        valve_on = bool(self._influenced_by) and world.get_switch(self._influenced_by)
        target = self._influence_target if valve_on else self._base_temperature

        step = self._drift_rate * elapsed_min
        if self._temperature < target:
            self._temperature = min(target, self._temperature + step)
        elif self._temperature > target:
            self._temperature = max(target, self._temperature - step)
        self._temperature += random.uniform(-0.03, 0.03)
        self._humidity = max(20.0, min(80.0, self._humidity + random.uniform(-0.4, 0.4)))

        return {
            "ok": True,
            "name": self.name,
            "temperature_c": round(self._temperature, 2),
            "humidity_pct": round(self._humidity, 1),
        }


@register_driver("climate", "simulated")
def _create(options: dict) -> SimulatedClimateDriver:
    return SimulatedClimateDriver(
        name=options.get("name", "Simulated sensor"),
        base_temperature_c=options.get("base_temperature_c", 20.0),
        base_humidity_pct=options.get("base_humidity_pct", 50.0),
        influenced_by=options.get("influenced_by"),
        influence_target_c=options.get("influence_target_c", 26.0),
        drift_rate_c_per_min=options.get("drift_rate_c_per_min", 0.15),
        poll_interval_seconds=options.get("poll_interval_seconds", 10.0),
    )
