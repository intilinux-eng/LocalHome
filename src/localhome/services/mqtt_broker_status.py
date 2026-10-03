"""A raw TCP reachability probe for the MQTT broker every Shelly device
and LocalHome itself connect to - not a real MQTT client, deliberately,
so it still answers "is anything even listening" the way a real incident
needed once: every device's MQTT settings could look perfectly correct
while mosquitto itself simply wasn't running - see docs/integrations/mqtt.md's
"Running mosquitto automatically" section. Used by the climate admin
panel (web/app.py's /api/climate-admin/broker-status) so a device stuck
on "not reachable yet" doesn't get misdiagnosed as its own fault when
the broker is the actual problem.
"""
from __future__ import annotations

import socket
import time
from typing import Callable

from localhome.util.jsonfile import load_json

# A probe is a connection the broker logs, two lines each: at most one a
# minute, however many admin pages ask.
PROBE_EVERY_SECONDS = 60


def check_host_port(host: str | None, port: int | None, timeout: float = 1.5) -> dict:
    """The probe itself: whether anything listens at host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "host": host, "port": port, "error": None}
    except OSError as exc:
        return {"reachable": False, "host": host, "port": port, "error": str(exc)}


def check_broker(broker_file: str, timeout: float = 1.5) -> dict:
    try:
        broker = load_json(broker_file)
    except (OSError, ValueError) as exc:
        return {"reachable": False, "host": None, "port": None, "error": f"couldn't read {broker_file}: {exc}"}
    return check_host_port(broker.get("host"), broker.get("port", 1883), timeout)


class BrokerStatus:
    """Whether the broker answers, for the admin page that asks every few
    seconds. From LocalHome's own connection to it when that's up - live,
    and no new connection at all; otherwise check_broker()'s probe, at
    most once every PROBE_EVERY_SECONDS (its answer kept in between)."""

    def __init__(self, broker_file: str, connection_up: Callable[[str, int], bool | None] | None = None,
                 probe: Callable[[str], dict] = check_broker, clock: Callable[[], float] = time.monotonic):
        self.broker_file = broker_file
        self._connection_up = connection_up
        self._probe = probe
        self._clock = clock
        self._last: dict | None = None
        self._last_at = 0.0

    def get(self) -> dict:
        try:
            broker = load_json(self.broker_file)
        except (OSError, ValueError):
            broker = {}
        host, port = broker.get("host"), broker.get("port", 1883)
        if host and self._connection_up is not None and self._connection_up(host, port):
            return {"reachable": True, "host": host, "port": port, "error": None}
        now = self._clock()
        if self._last is None or now - self._last_at >= PROBE_EVERY_SECONDS:
            self._last = self._probe(self.broker_file)
            self._last_at = now
        return self._last
