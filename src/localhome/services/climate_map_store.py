"""Lets the climate admin panel (web/app.py's /climate-admin routes) map
the fixed two-zone thermostat onto whichever physical devices this
particular apartment has, instead of hand-editing config.yaml - the
whole point being that redeploying this software to a different
apartment with the same Shelly device family should only ever mean
changing device ids, never code or config.yaml's own hand-written
comments.

Persists config/climate_map.json (additive, gitignored by the existing
config/*.json pattern - see .gitignore). `get_zones()` returns None when
nothing has been saved yet (missing file, or a corrupt one - same
fail-safe stance as ScheduleStore._load()), meaning "defer entirely to
config.yaml's own thermostat.zones" - an apartment that hasn't touched
this panel keeps working exactly as before.

Each zone's sensor has three possible sources - see the `sensor_mode`
field and `_sensor_integration()` below:
  "shelly"    - a real Shelly H&T Gen3 over MQTT (needs sensor_device_id)
  "simulated" - drivers/simulated/climate.py's existing driver, for a
                zone with no hardware installed yet
  "manual"    - drivers/manual/climate.py's debug driver: whatever value
                the panel's operator last typed in, held fixed, so a
                zone's on/off logic can be exercised at a chosen
                temperature without a real event or a simulated drift
The valve is always a real Shelly relay (Gen2+ JSON boolean shape) -
this feature doesn't need a simulated/manual valve.

set_zones() validates before writing anything - the file on disk is
always either absent or fully valid, never partial (mirrors the
"reject on the way in" lesson from ScheduleStore.set_week(), see
services/thermostat.py).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from localhome.config import Integration
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

ZONE_COUNT = 2
SENSOR_MODES = ("shelly", "simulated", "manual")


def validate_zones(zones: list[dict]) -> None:
    """Raises ValueError describing the first problem found. Every check
    a hand-typed device id or a rushed API call could get wrong -
    duplicate names, a device id reused across two roles/zones (which
    would otherwise silently make one of them unreachable, see
    core/manager.py's DeviceManager._register()), a missing id for the
    mode that needs one."""
    if not isinstance(zones, list) or len(zones) != ZONE_COUNT:
        raise ValueError(f"exactly {ZONE_COUNT} zones are required")

    names: list[str] = []
    device_ids: list[str] = []
    for i, zone in enumerate(zones):
        if not isinstance(zone, dict):
            raise ValueError(f"zone {i} must be an object")
        name = zone.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"zone {i}: 'name' is required")
        names.append(name)

        mode = zone.get("sensor_mode")
        if mode not in SENSOR_MODES:
            raise ValueError(f"zone {i} ('{name}'): sensor_mode must be one of {SENSOR_MODES}")

        sensor_device_id = zone.get("sensor_device_id")
        if mode == "shelly":
            if not isinstance(sensor_device_id, str) or not sensor_device_id.strip():
                raise ValueError(f"zone {i} ('{name}'): sensor_device_id is required when sensor_mode is 'shelly'")
            device_ids.append(sensor_device_id.strip())

        valve_device_id = zone.get("valve_device_id")
        if not isinstance(valve_device_id, str) or not valve_device_id.strip():
            raise ValueError(f"zone {i} ('{name}'): valve_device_id is required")
        device_ids.append(valve_device_id.strip())

        if "cool_enabled" in zone and not isinstance(zone["cool_enabled"], bool):
            raise ValueError(f"zone {i} ('{name}'): cool_enabled must be true or false")

    if len(set(names)) != len(names):
        raise ValueError("zone names must be unique")
    if len(set(device_ids)) != len(device_ids):
        raise ValueError("each Shelly device id may only be used for one role - a duplicate id was found")


def _shelly_sensor_options(zone: dict, sensor_name: str, broker_file: str) -> dict:
    device_id = zone["sensor_device_id"].strip()
    return {
        "name": sensor_name,
        "broker_file": broker_file,
        "state_topic": [
            f"{device_id}/status/temperature:0",
            f"{device_id}/status/humidity:0",
            f"{device_id}/status/devicepower:0",
        ],
        "fields": {"temperature_c": "tC", "humidity_pct": "rh", "battery_pct": "battery.percent"},
        "stale_after_seconds": 21600,
        "dashboard_tab": "climate",
    }


def _valve_integration_options(zone: dict, valve_name: str, broker_file: str) -> dict:
    device_id = zone["valve_device_id"].strip()
    return {
        "name": valve_name,
        "broker_file": broker_file,
        "state_topic": f"{device_id}/status/switch:0",
        "command_topic": f"{device_id}/command/switch:0",
        "state_field": "output",
        "payload_on": True,
        "payload_off": False,
        "dashboard_tab": "climate",
        # No stale_after_seconds: a relay only publishes on state change,
        # so a timeout here would wrongly flag a valve that's just been
        # closed for a while as unreachable - see docs/integrations/mqtt.md.
    }


def _sensor_integration(zone: dict, sensor_name: str, broker_file: str, manual_store_path: str) -> Integration:
    mode = zone.get("sensor_mode", "shelly")
    if mode == "shelly":
        return Integration("climate", "mqtt_json", _shelly_sensor_options(zone, sensor_name, broker_file))
    if mode == "simulated":
        # Deliberately the driver's own flat defaults, no influenced_by
        # wiring to the zone's valve - a boring, obviously-fake value is
        # exactly right for "hardware isn't installed yet", not a
        # believable drift.
        return Integration("climate", "simulated", {"name": sensor_name, "dashboard_tab": "climate"})
    if mode == "manual":
        return Integration(
            "climate", "manual_override",
            {"name": sensor_name, "dashboard_tab": "climate", "store_path": manual_store_path},
        )
    raise ValueError(f"unknown sensor_mode {mode!r}")


def build_climate_integrations(zones: list[dict], broker_file: str, manual_store_path: str) -> list[Integration]:
    """The Integrations DeviceManager needs to build every zone's sensor
    + valve - concatenated onto config.integrations before DeviceManager
    construction (see web/app.py's create_app()), the same "additive,
    zero core changes" seam this project already uses for panel-added
    devices."""
    integrations: list[Integration] = []
    for zone in zones:
        sensor_name = f"{zone['name']} Sensor"
        valve_name = f"{zone['name']} Valve"
        integrations.append(_sensor_integration(zone, sensor_name, broker_file, manual_store_path))
        integrations.append(Integration("switch", "mqtt_json", _valve_integration_options(zone, valve_name, broker_file)))
    return integrations


def build_zone_configs(zones: list[dict]) -> list[dict]:
    """The shape create_app() already builds Zone(...) objects from -
    see thermostat_cfg["zones"] parsing in web/app.py."""
    return [
        {
            "name": zone["name"],
            "sensor": f"{zone['name']} Sensor",
            "valve": f"{zone['name']} Valve",
            "cool_enabled": zone.get("cool_enabled", True),
        }
        for zone in zones
    ]


class ClimateMapStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s - deferring to config.yaml's own thermostat zones", self.path)
            return {}

    def get_zones(self) -> list[dict] | None:
        zones = self._load().get("zones")
        return zones or None

    def set_zones(self, zones: list[dict]) -> None:
        validate_zones(zones)
        with self._lock:
            save_json_atomic(self.path, {"zones": zones})
