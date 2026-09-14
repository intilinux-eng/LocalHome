"""A fake on/off switch with no real device behind it - for trying out
the dashboard (or a whole thermostat setup) before you own the actual
hardware. Swap `type: simulated` for the real driver's type later;
nothing else needs to change, since it's the same SwitchDriver interface
everything else already talks to. See docs/integrations/simulated.md.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver
from localhome.drivers.simulated import world


class SimulatedSwitchDriver(SwitchDriver):
    def __init__(self, name: str, initial_on: bool = False, poll_interval_seconds: float = 10.0):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.history_fields = ("is_on",)
        world.set_switch(name, initial_on)

    def read(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "is_on": world.get_switch(self.name)}

    def turn_on(self) -> None:
        world.set_switch(self.name, True)

    def turn_off(self) -> None:
        world.set_switch(self.name, False)


@register_driver("switch", "simulated")
def _create(options: dict) -> SimulatedSwitchDriver:
    return SimulatedSwitchDriver(
        name=options.get("name", "Simulated switch"),
        initial_on=bool(options.get("initial_on", False)),
        poll_interval_seconds=options.get("poll_interval_seconds", 10.0),
    )
