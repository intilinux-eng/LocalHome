"""A fake roller shutter with no real device behind it - for trying out the
dashboard's shutters before owning them, and for the browser tests. It
remembers the last command (open/close/stop) as its state, like the cheap
curtain switches that report only that. `unreachable: true` makes it fail
every call, as a shutter off the network does. See
docs/integrations/simulated.md.
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import CoverDriver
from localhome.core.registry import register_driver


class SimulatedCoverDriver(CoverDriver):
    def __init__(self, name: str, travel_time_seconds: float = 15.0, unreachable: bool = False):
        self.id = f"simulated-{name}"
        self.name = name
        self._travel_time = travel_time_seconds
        self._unreachable = unreachable
        self._state = "stop"

    def _answer(self) -> None:
        if self._unreachable:
            raise RuntimeError(f"{self.name}: not answering")

    def status(self) -> dict[str, Any]:
        self._answer()
        return {"state": self._state}

    def send(self, action: str) -> None:
        self._answer()
        self._state = action

    def read_travel_time(self, default: float = 15.0) -> float:
        return self._travel_time


@register_driver("cover", "simulated")
def _create(options: dict) -> SimulatedCoverDriver:
    return SimulatedCoverDriver(
        name=options.get("name", "Simulated shutter"),
        travel_time_seconds=float(options.get("travel_time_seconds", 15.0)),
        unreachable=bool(options.get("unreachable", False)),
    )
