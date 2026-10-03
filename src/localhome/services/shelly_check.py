"""Is the Shelly whose id was just typed in the admin panel there, and set
as its role needs? Asked at once over MQTT RPC - no restart, and nothing
is ever changed on the device. For the devices that run,
services/shelly_health.py checks the same every 5 minutes; this is for
the installer, the moment the id goes in.

A battery H&T sleeps and can't be asked: it shows up in Devices when it
next wakes (its button wakes it).
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

from localhome.config import Integration
from localhome.drivers.mqtt.client import load_broker_config
from localhome.services.shelly_health import SHELLY_ID, expected_settings, settings_wrong

# Roles the admin panel types a Shelly id for, and which of them are
# relays to compare against what the panel builds for them.
RELAY_ROLES = {"valve", "dehumidifier", "bypass"}
BATTERY_ROLES = {"sensor", "outdoor"}
ROLES = RELAY_ROLES | BATTERY_ROLES | {"vmc"}


def role_integration(role: str, device_id: str) -> Integration | None:
    """The relay integration the panel builds for this role on this
    device - what its settings are compared against."""
    from localhome.services import climate_map_store as panel
    if role == "valve":
        return Integration("switch", "mqtt_json",
                           panel.valve_integration_options({"valve_device_id": device_id}, "check", "mqtt_broker.json"))
    if role in ("dehumidifier", "bypass"):
        key = "dehumidifier_device_id" if role == "dehumidifier" else "bypass_valve_device_id"
        return panel.build_extra_switch_integrations({key: device_id}, "mqtt_broker.json")[0]
    return None


class ShellyCheck:
    def __init__(self, broker_file: str, rpc_for: Callable[[dict, str], Any] | None = None):
        self.broker_file = broker_file
        if rpc_for is None:
            from localhome.drivers.mqtt.rpc import ShellyRpcClient
            rpc_for = ShellyRpcClient
        self._rpc_for = rpc_for
        # One client per device: each one subscribes to the replies once.
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    def check(self, device_id: str, role: str) -> dict:
        device_id = (device_id or "").strip()
        if role not in ROLES:
            return {"ok": False, "reason": "role"}
        if not SHELLY_ID.match(device_id):
            return {"ok": False, "reason": "id"}
        if role in BATTERY_ROLES:
            return {"ok": True, "answered": None, "battery": True}
        if not os.path.exists(self.broker_file):
            return {"ok": True, "answered": False, "reason": "no_broker"}
        try:
            client = self._client(device_id)
            info = client.call("Shelly.GetDeviceInfo")
        except Exception as exc:
            return {"ok": True, "answered": False, "reason": "silent", "error": str(exc)}
        result = {"ok": True, "answered": True, "model": info.get("app") or info.get("model"),
                  "firmware": info.get("ver"), "settings_wrong": []}
        integration = role_integration(role, device_id)
        expect = expected_settings(integration) if integration is not None else {}
        if expect:
            try:
                result["settings_wrong"] = settings_wrong(expect, client.call("Shelly.GetConfig"))
            except Exception as exc:
                result["settings_error"] = str(exc)
        return result

    def _client(self, device_id: str):
        with self._lock:
            client = self._clients.get(device_id)
            if client is None:
                broker = load_broker_config({"broker_file": self.broker_file})
                client = self._clients[device_id] = self._rpc_for(broker, device_id)
            return client
