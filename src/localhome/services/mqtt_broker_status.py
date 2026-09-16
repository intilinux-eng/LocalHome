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

from localhome.util.jsonfile import load_json


def check_host_port(host: str | None, port: int | None, timeout: float = 1.5) -> dict:
    """The probe itself, split out from check_broker() below so the panel
    can also test a host/port the operator just typed in - before saving
    it to mqtt_broker.json - the same "verify before you commit to it"
    idea as the rest of this panel (see services/climate_map_store.py)."""
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
