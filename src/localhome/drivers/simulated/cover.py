"""A fake roller shutter with no real device behind it - for trying out the
dashboard's shutters before owning them, and for the browser tests. It
behaves like the LoraTap curtain switches: its state is the last command
(open/close/stop), a run stops by itself once its travel time is over, and
it says each change by itself to whoever listens. `unreachable: true`
makes it fail every call, as a shutter off the network does. See
docs/integrations/simulated.md.
"""
from __future__ import annotations

import threading
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
        self._listeners: list = []
        self._run: threading.Timer | None = None
        self._lock = threading.Lock()

    def _answer(self) -> None:
        if self._unreachable:
            raise RuntimeError(f"{self.name}: not answering")

    def _set(self, state: str) -> None:
        with self._lock:
            self._state = state
            listeners = list(self._listeners)
        for on_state in listeners:
            on_state(state)

    def status(self) -> dict[str, Any]:
        self._answer()
        return {"state": self._state}

    def send(self, action: str) -> None:
        self._answer()
        if self._run is not None:
            self._run.cancel()
            self._run = None
        self._set(action)
        if action in ("open", "close"):
            # The motor runs for its travel time, then the module stops it.
            self._run = threading.Timer(self._travel_time, self._set, args=("stop",))
            self._run.daemon = True
            self._run.start()

    def read_travel_time(self, default: float = 15.0) -> float:
        return self._travel_time

    def listen(self, on_state, on_link, stop) -> bool:
        if self._unreachable:
            on_link(False, f"{self.name}: not answering")
            stop.wait()
            return True
        on_link(True, None)
        with self._lock:
            self._listeners.append(on_state)
        on_state(self._state)
        stop.wait()
        with self._lock:
            self._listeners.remove(on_state)
        return True


@register_driver("cover", "simulated")
def _create(options: dict) -> SimulatedCoverDriver:
    return SimulatedCoverDriver(
        name=options.get("name", "Simulated shutter"),
        travel_time_seconds=float(options.get("travel_time_seconds", 15.0)),
        unreachable=bool(options.get("unreachable", False)),
    )
