"""Driver for Tuya-based motorized covers exposed as a "Curtain switch"
device (Tuya product category `clkg`, or `tdq` on newer firmware -
LoraTap's WiFi roller shutter modules use this). Controlled and polled
entirely over the LAN via tinytuya; no cloud dependency at runtime.

These modules report only the current motor command (open/close/stop),
never an absolute position - see services/position_control.py for how an
approximate 0-100% position is derived from that.

They also say each change by themselves on a connection kept open, which
is how LocalHome follows them (listen()). Measured on a LoraTap module: a
command over the LAN is reported 0.3 s after, a press of the wall button
about 1.2 s after, and "stop" comes when the module's own run timer (DP10,
set by hand with some margin) runs out - it can't tell the motor's own end
stop, so a run that lasts the whole timer has reached the end.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from localhome.core.interfaces import CoverDriver
from localhome.core.registry import register_driver
from localhome.drivers.tuya.client import TuyaConnection, address_book_for, load_devices, require_dps

logger = logging.getLogger(__name__)

DEFAULT_CATEGORIES = ("clkg", "tdq")
# The module closes a connection it hears nothing on for about 30 s.
HEARTBEAT_SECONDS = 10
# A module that can't be reached is tried again after this long, doubling
# up to the ceiling: one that's off shouldn't cost a socket every second.
RECONNECT_FIRST_SECONDS = 5
RECONNECT_MAX_SECONDS = 300


class TuyaCoverDriver(CoverDriver):
    def __init__(self, entry: dict, version: float = 3.4, addresses=None):
        self.id = entry["id"]
        self.name = entry["name"]
        self._entry = entry
        self._connection = TuyaConnection(entry, version=version, addresses=addresses)
        # The run timer set in the module (DP10), as it last said it.
        self._travel: float | None = None

    def _note(self, dps: dict) -> None:
        if "10" in dps:
            self._travel = float(dps["10"])

    def status(self) -> dict[str, Any]:
        dps = self._connection.status()
        self._note(dps)
        return {"state": dps.get("1", "unknown"), "raw": {"dps": dps}}

    def send(self, action: str) -> None:
        self._connection.set_value(1, action)

    def read_travel_time(self, default: float = 15.0) -> float:
        if self._travel is None:
            try:
                self.status()
            except Exception:  # not answering now: the defaults below
                pass
        if self._travel is not None:
            return self._travel
        dp10 = self._entry.get("mapping", {}).get("10", {})
        return float(dp10.get("values", {}).get("max", default))

    def listen(self, on_state, on_link, stop) -> bool:
        wait = RECONNECT_FIRST_SECONDS
        while not stop.is_set():
            device = None
            try:
                device, dps = self._connection.open_persistent()
                on_link(True, None)
                wait = RECONNECT_FIRST_SECONDS
                self._note(dps)
                if "1" in dps:
                    on_state(dps["1"])
                beat = time.monotonic() + HEARTBEAT_SECONDS
                while not stop.is_set():
                    data = device.receive()  # None when nothing came within the listen timeout
                    if isinstance(data, dict) and data.get("Err"):
                        require_dps(data, self.name)  # raises, saying what went wrong
                    if isinstance(data, dict) and data.get("dps"):
                        self._note(data["dps"])  # its timer changed in the app, say
                        if "1" in data["dps"]:
                            on_state(data["dps"]["1"])
                    if time.monotonic() >= beat:
                        device.heartbeat(nowait=True)
                        on_link(True, None)  # still there: the system status says "just now"
                        beat = time.monotonic() + HEARTBEAT_SECONDS
            except Exception as exc:  # not reached, or the connection dropped: said, and tried again later
                logger.debug("%s: listening stopped (%s), again in %ss", self.name, exc, wait)
                on_link(False, str(exc))
                stop.wait(wait)
                wait = min(wait * 2, RECONNECT_MAX_SECONDS)
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception:  # already gone
                        pass
        return True


@register_driver("cover", "tuya_curtain_switch")
def _create(options: dict) -> list[TuyaCoverDriver]:
    categories = options.get("categories", DEFAULT_CATEGORIES)
    version = options.get("version", 3.4)
    entries = [e for e in load_devices(options["devices_file"]) if e.get("category") in categories]
    addresses = address_book_for(options["devices_file"])
    return [TuyaCoverDriver(e, version=version, addresses=addresses) for e in entries]
