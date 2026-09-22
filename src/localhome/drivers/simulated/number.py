"""A fake settable number (a dimmer's brightness, a ventilation unit's
speed) with no real device behind it - for trying out the dashboard, or a
VMC day/night program, before you own the hardware. Swap `type:
simulated` for the real driver's type later; nothing else changes, since
it's the same NumberDriver interface. See docs/integrations/simulated.md.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import NumberDriver
from localhome.core.registry import register_driver


class SimulatedNumberDriver(NumberDriver):
    def __init__(
        self,
        name: str,
        value: float = 0.0,
        min_value: float = 0.0,
        max_value: float = 100.0,
        unit: str = "%",
        poll_interval_seconds: float = 10.0,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.history_fields = ("value",)
        self.min_value = min_value
        self.max_value = max_value
        self.unit = unit
        self._value = value

    def read(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "value": self._value}

    def set_value(self, value: float) -> None:
        self._value = max(self.min_value, min(self.max_value, float(value)))


@register_driver("number", "simulated")
def _create(options: dict) -> SimulatedNumberDriver:
    return SimulatedNumberDriver(
        name=options.get("name", "Simulated number"),
        value=options.get("initial_value", 0.0),
        min_value=options.get("min", 0.0),
        max_value=options.get("max", 100.0),
        unit=options.get("unit", "%"),
        poll_interval_seconds=options.get("poll_interval_seconds", 10.0),
    )
