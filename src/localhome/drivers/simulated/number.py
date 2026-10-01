"""A fake settable number (a dimmer's brightness, a ventilation unit's
speed) with no real device behind it - for trying out the dashboard, or a
VMC day/night program, before you own the hardware. Swap `type:
simulated` for the real driver's type later; nothing else changes, since
it's the same NumberDriver interface. See docs/integrations/simulated.md.

With `on_with_value` and a `paired_switch` (a simulated switch), setting a
value also switches that on - what a real VMC dimmer does with its
`on_with_value` (drivers/mqtt/shelly_light.py): picking a speed starts it.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import NumberDriver
from localhome.core.registry import register_driver
from localhome.drivers.simulated import world


class SimulatedNumberDriver(NumberDriver):
    def __init__(
        self,
        name: str,
        value: float = 0.0,
        min_value: float = 0.0,
        max_value: float = 100.0,
        unit: str = "%",
        poll_interval_seconds: float = 10.0,
        on_with_value: bool = False,
        paired_switch: str | None = None,
    ):
        self.name = name
        self.on_with_value = on_with_value
        self._paired_switch = paired_switch
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
        if self.on_with_value and self._paired_switch:
            world.set_switch(self._paired_switch, True)


@register_driver("number", "simulated")
def _create(options: dict) -> SimulatedNumberDriver:
    return SimulatedNumberDriver(
        name=options.get("name", "Simulated number"),
        value=options.get("initial_value", 0.0),
        min_value=options.get("min", 0.0),
        max_value=options.get("max", 100.0),
        unit=options.get("unit", "%"),
        poll_interval_seconds=options.get("poll_interval_seconds", 10.0),
        on_with_value=bool(options.get("on_with_value", False)),
        paired_switch=options.get("paired_switch"),
    )
