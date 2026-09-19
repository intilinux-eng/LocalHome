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
The valve has its own, separate `valve_mode` - "shelly" (a real relay,
Gen2+ JSON boolean shape, needs valve_device_id) or "simulated"
(drivers/simulated/switch.py, controllable through the same generic
on/off route the live status page's manual test buttons already use).
No "manual" mode here - unlike a sensor reading, a switch's state is
already directly settable by just flipping it, real or simulated, so
sensor_mode="manual" plus valve_mode="simulated" together are enough to
exercise the whole control loop - typed temperature in, valve reacts
out - with no hardware wired at all, before anything is trusted to run
on its own.

set_zones() validates before writing anything - the file on disk is
always either absent or fully valid, never partial (mirrors the
"reject on the way in" lesson from ScheduleStore.set_week(), see
services/thermostat.py).

The same file also holds `extra_switches` - two more fixed, standalone
Shelly Plus 1 on/off switches this apartment's plant always has, same
as the two zones are fixed: a bathroom bypass valve and a dehumidifier
(see EXTRA_SWITCH_FIELDS/build_extra_switch_integrations() below).
Each is independently optional (either, both, or neither may be
panel-managed - a fresh apartment might not have the hardware wired
yet), but unlike a zone there's no name field: the panel always builds
these under a fixed English name ("Bathroom Bypass Valve"/
"Dehumidifier"). Adopting this for a device already hand-wired in
config.yaml means updating that config.yaml's own `thermostat.
interlocks`/`mode_switches` entries to reference that same fixed name
instead of whatever they called it before - a one-time edit, not
something this panel does for you (DeviceManager logs a name-collision
warning at startup if the old config.yaml entry is left in place too -
see web/app.py's create_app()).

It also holds `extra_sensors` - the same "fixed role, optional,
device-id-only" shape as extra_switches, but for a standalone *sensor*
that isn't part of either zone: an outdoor temperature/humidity
reading, always a real Shelly H&T Gen3 (see EXTRA_SENSOR_FIELDS/
build_extra_sensor_integrations() below - reuses the exact same
3-topic MQTT shape a zone's own "shelly" sensor mode uses). No
"simulated"/"manual" mode here - unlike a zone's sensor, there's no
thermostat decision riding on this reading, so there's nothing to
exercise before the hardware exists; it's either wired (a device id is
set) or it isn't (blank, not built at all).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from localhome.config import Integration, LocalHomeConfig
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

ZONE_COUNT = 2
SENSOR_MODES = ("shelly", "simulated", "manual")
VALVE_MODES = ("shelly", "simulated")


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

        # Same "no hardware yet" escape hatch as the sensor - a valve
        # has no "manual" mode of its own (there's nothing to type a
        # value for), but a simulated one is still fully controllable
        # through the same on/off route the live status page's test
        # buttons use, so the control logic can be exercised end to end
        # before any Shelly relay is actually wired.
        valve_mode = zone.get("valve_mode", "shelly")
        if valve_mode not in VALVE_MODES:
            raise ValueError(f"zone {i} ('{name}'): valve_mode must be one of {VALVE_MODES}")
        valve_device_id = zone.get("valve_device_id")
        if valve_mode == "shelly":
            if not isinstance(valve_device_id, str) or not valve_device_id.strip():
                raise ValueError(f"zone {i} ('{name}'): valve_device_id is required when valve_mode is 'shelly'")
            device_ids.append(valve_device_id.strip())

        if "cool_enabled" in zone and not isinstance(zone["cool_enabled"], bool):
            raise ValueError(f"zone {i} ('{name}'): cool_enabled must be true or false")

    _check_no_duplicates(names, "duplicate zone name '{value}' - zone names must be unique")
    _check_no_duplicates(
        device_ids,
        "duplicate id '{value}' - each Shelly device id may only be used for one role",
    )


def _check_no_duplicates(values: list[str], message: str) -> None:
    """Names the actual duplicate rather than just saying "something's
    duplicated" - with up to 2 zones and 2 extra switches all sharing
    one device-id namespace, "a duplicate id was found" alone leaves a
    technician checking every field by hand to find which one."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(message.format(value=value))
        seen.add(value)


def _zone_device_ids(zones: list[dict]) -> list[str]:
    ids = []
    for zone in zones or []:
        if zone.get("sensor_mode") == "shelly" and zone.get("sensor_device_id"):
            ids.append(zone["sensor_device_id"].strip())
        if zone.get("valve_mode", "shelly") == "shelly" and zone.get("valve_device_id"):
            ids.append(zone["valve_device_id"].strip())
    return ids


# Fixed roles, not a user-typed name - every "reference model" apartment
# has exactly these two, same as it has exactly two zones (ZONE_COUNT).
# The dict key is this store's on-disk field name; the value is the
# literal Integration `name` build_extra_switch_integrations() below
# gives it - a generic English string, safe to commit, deliberately
# fixed across every install (see the module docstring for what that
# means for config.yaml's own interlocks/mode_switches).
EXTRA_SWITCH_FIELDS = {
    "bypass_valve_device_id": "Bathroom Bypass Valve",
    "dehumidifier_device_id": "Dehumidifier",
}


def _extra_switch_device_ids(extra_switches: dict) -> list[str]:
    return [
        extra_switches[key].strip()
        for key in EXTRA_SWITCH_FIELDS
        if (extra_switches or {}).get(key)
    ]


def _extra_sensor_device_ids(extra_sensors: dict) -> list[str]:
    return [
        extra_sensors[key].strip()
        for key in EXTRA_SENSOR_FIELDS
        if (extra_sensors or {}).get(key)
    ]


def validate_extra_switches(
    extra_switches: dict, zones: list[dict] | None = None, extra_sensors: dict | None = None,
) -> None:
    """Each of the two fixed roles is independently optional - a fresh
    apartment might not have either wired yet - but if given, must be a
    non-empty device id, and no device id may double up with a zone's
    own sensor/valve (validate_zones() enforces the equivalent rule
    within the zones themselves) or the extra sensor below."""
    if not isinstance(extra_switches, dict):
        raise ValueError("extra_switches must be an object")

    device_ids: list[str] = list(_zone_device_ids(zones or [])) + _extra_sensor_device_ids(extra_sensors or {})
    for key in EXTRA_SWITCH_FIELDS:
        device_id = extra_switches.get(key)
        if device_id is not None:
            if not isinstance(device_id, str) or not device_id.strip():
                raise ValueError(f"{key}: device id must be a non-empty string or null")
            device_ids.append(device_id.strip())

    _check_no_duplicates(
        device_ids,
        "duplicate id '{value}' - each Shelly device id may only be used for one role",
    )


# Fixed role, not a user-typed name - an outdoor reading isn't part of
# either zone (no valve, no thermostat decision), so unlike a zone
# there's nothing to exercise with a simulated/manual stand-in before
# the hardware exists: it's either a real Shelly device id, or not
# built at all. Same fixed-English-name convention as
# EXTRA_SWITCH_FIELDS - see that constant and the module docstring for
# what a matching config.yaml entry needs updating to.
EXTRA_SENSOR_FIELDS = {
    "outdoor_sensor_device_id": "Outdoor Sensor",
}


def validate_extra_sensors(
    extra_sensors: dict, zones: list[dict] | None = None, extra_switches: dict | None = None,
) -> None:
    """Same shape as validate_extra_switches() above, mirrored for the
    one fixed sensor role instead of the two fixed switch roles."""
    if not isinstance(extra_sensors, dict):
        raise ValueError("extra_sensors must be an object")

    device_ids: list[str] = list(_zone_device_ids(zones or [])) + _extra_switch_device_ids(extra_switches or {})
    for key in EXTRA_SENSOR_FIELDS:
        device_id = extra_sensors.get(key)
        if device_id is not None:
            if not isinstance(device_id, str) or not device_id.strip():
                raise ValueError(f"{key}: device id must be a non-empty string or null")
            device_ids.append(device_id.strip())

    _check_no_duplicates(
        device_ids,
        "duplicate id '{value}' - each Shelly device id may only be used for one role",
    )


def _shelly_sensor_options(zone: dict, sensor_name: str, broker_file: str) -> dict:
    # Same 4-topic shape (temperature/humidity/devicepower/wifi) this
    # project's own hand-written Shelly H&T entries use - see
    # docs/integrations/mqtt.md - so a panel-built sensor shows the same
    # WiFi signal strength a hand-written one would, not a reduced set
    # just because it came from here.
    device_id = zone["sensor_device_id"].strip()
    return {
        "name": sensor_name,
        "broker_file": broker_file,
        "state_topic": [
            f"{device_id}/status/temperature:0",
            f"{device_id}/status/humidity:0",
            f"{device_id}/status/devicepower:0",
            f"{device_id}/status/wifi",
        ],
        "fields": {
            "temperature_c": "tC",
            "humidity_pct": "rh",
            "battery_pct": "battery.percent",
            "wifi_rssi_dbm": "rssi",
        },
        # 4h - just under 2x the 2h heartbeat these devices promise
        # (confirmed live overnight: both real H&T sensors landed every
        # heartbeat within a minute of exactly 2h, no exceptions once
        # WiFi was stable), so one missed heartbeat alone never trips
        # this, but two in a row does - catching a genuinely dead sensor
        # in half the time the previous 6h value would have.
        "stale_after_seconds": 14400,
        "dashboard_tab": "climate",
        # Deliberately NO availability_topic here, unlike the valve
        # builder below - tried it live against the two real H&T Gen3
        # sensors this session and had to back it out. In battery mode
        # (see docs/configuration.md#sensor-edge-cases-explained-in-panel)
        # this device wakes every ~1 min but only actually connects to
        # publish when a threshold trips or its 2h heartbeat is due -
        # between those moments it isn't connected at all, so its own
        # "<id>/online" LWT-backed topic reads "false" essentially the
        # whole time it's working perfectly normally, not just when
        # something's actually wrong. Wiring that up here made a
        # healthy, correctly-reporting sensor look permanently offline.
        # stale_after_seconds above already gives this the right signal
        # (3x its 2h heartbeat) without that false positive - a mains-
        # powered device (the valve below) has no such legitimate
        # "disconnected on purpose" state, so availability_topic stays
        # meaningful there.
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
        # availability_topic still applies though - see the matching
        # comment in _shelly_sensor_options() above. A mains-powered relay
        # going offline is unambiguous (no battery-sleep case to confuse
        # it with), so this is a clean signal for a valve too.
        "availability_topic": f"{device_id}/online",
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


def _valve_integration(zone: dict, valve_name: str, broker_file: str) -> Integration:
    mode = zone.get("valve_mode", "shelly")
    if mode == "shelly":
        return Integration("switch", "mqtt_json", _valve_integration_options(zone, valve_name, broker_file))
    if mode == "simulated":
        # Fully controllable through the same generic on/off route the
        # live status page's manual test buttons already use - lets the
        # thermostat's control logic (and a technician's manual test) be
        # exercised end to end before any relay is actually wired.
        return Integration("switch", "simulated", {"name": valve_name, "dashboard_tab": "climate"})
    raise ValueError(f"unknown valve_mode {mode!r}")


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
        integrations.append(_valve_integration(zone, valve_name, broker_file))
    return integrations


def build_extra_switch_integrations(extra_switches: dict, broker_file: str) -> list[Integration]:
    """Each of the two fixed roles (EXTRA_SWITCH_FIELDS) becomes a real
    switch only if its device id is set - a fresh apartment that hasn't
    wired one yet simply doesn't get an integration for it, same as an
    unmanaged zone. Reuses the exact same Shelly relay shape a zone's
    valve already uses (_valve_integration_options() above), just keyed
    by the fixed field name instead of "valve_device_id"."""
    integrations: list[Integration] = []
    for key, display_name in EXTRA_SWITCH_FIELDS.items():
        device_id = (extra_switches or {}).get(key)
        if device_id:
            zone_shaped = {"valve_device_id": device_id}
            integrations.append(Integration("switch", "mqtt_json", _valve_integration_options(zone_shaped, display_name, broker_file)))
    return integrations


def build_extra_sensor_integrations(extra_sensors: dict, broker_file: str) -> list[Integration]:
    """The one fixed sensor role (EXTRA_SENSOR_FIELDS) becomes a real
    climate integration only if its device id is set - same "not wired
    yet means not built at all" rule as build_extra_switch_integrations()
    above. Reuses the exact same Shelly H&T Gen3 shape a zone's own
    "shelly" sensor mode already uses (_shelly_sensor_options()), just
    keyed by the fixed field name instead of "sensor_device_id"."""
    integrations: list[Integration] = []
    for key, display_name in EXTRA_SENSOR_FIELDS.items():
        device_id = (extra_sensors or {}).get(key)
        if device_id:
            zone_shaped = {"sensor_device_id": device_id}
            integrations.append(Integration("climate", "mqtt_json", _shelly_sensor_options(zone_shaped, display_name, broker_file)))
    return integrations


def _blank_zone() -> dict:
    return {
        "name": "", "sensor_mode": "shelly", "sensor_device_id": None,
        "valve_mode": "shelly", "valve_device_id": None, "cool_enabled": True,
    }


def _device_id_from_topic(topic) -> str | None:
    """The inverse of _shelly_sensor_options()/_valve_integration_options()
    above: every topic this project's Shelly drivers use starts with
    "<device_id>/..." - state_topic is a list for the multi-topic sensor
    shape, a plain string for the valve, so take whichever comes first."""
    if isinstance(topic, list):
        topic = topic[0] if topic else None
    if not topic or "/" not in topic:
        return None
    return topic.split("/", 1)[0]


def snapshot_from_config(zones, integrations: list[Integration]) -> list[dict]:
    """Best-effort read of config.yaml's *current* hand-written setup -
    used only to pre-fill the panel's form the first time, before
    anything's been saved here, never written back to config.yaml and
    never consulted again once climate_map.json exists (see
    ClimateMapStore.get_zones()). Without this, switching a zone to
    "manual"/"simulated" for a quick test would mean re-typing a real
    device id you're about to type right back in - reading it from the
    already-running setup instead means nothing about the real,
    currently-connected device is ever changed just by looking at it.

    `zones` is services.thermostat.Zone objects (name/sensor/valve/
    cool_enabled) - what create_app() already built from config.yaml's
    thermostat.zones before this ever runs.
    """
    by_name = {i.options.get("name"): i for i in integrations}
    snapshot = []
    for zone in zones:
        sensor = by_name.get(zone.sensor)
        if sensor is not None and sensor.type == "mqtt_json":
            sensor_mode, sensor_device_id = "shelly", _device_id_from_topic(sensor.options.get("state_topic"))
        elif sensor is not None and sensor.type == "simulated":
            sensor_mode, sensor_device_id = "simulated", None
        else:
            # An unrecognized/absent driver type (or none at all) isn't
            # representable by this Shelly-only panel - fall back to an
            # empty "shelly" slot rather than guessing or crashing.
            sensor_mode, sensor_device_id = "shelly", None

        valve = by_name.get(zone.valve)
        if valve is not None and valve.type == "mqtt_json":
            valve_mode, valve_device_id = "shelly", _device_id_from_topic(valve.options.get("state_topic"))
        elif valve is not None and valve.type == "simulated":
            valve_mode, valve_device_id = "simulated", None
        else:
            valve_mode, valve_device_id = "shelly", None

        snapshot.append({
            "name": zone.name,
            "sensor_mode": sensor_mode,
            "sensor_device_id": sensor_device_id,
            "valve_mode": valve_mode,
            "valve_device_id": valve_device_id,
            "cool_enabled": zone.cool_enabled,
        })
    while len(snapshot) < ZONE_COUNT:
        snapshot.append(_blank_zone())
    return snapshot[:ZONE_COUNT]


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


def apply_to_config(config: LocalHomeConfig) -> dict | None:
    """Applies the climate admin panel's saved mapping (if any) onto an
    already-loaded LocalHomeConfig, exactly the way web/app.py's
    create_app() does before building a DeviceManager from it - so any
    other entry point that also builds one (`localhome-cli doctor`,
    most notably) cross-checks the same effective setup a running
    server actually uses, not just config.yaml's own literal - and for
    a from-scratch apartment, possibly entirely absent -
    `thermostat.zones`.

    Mutates `config.integrations` in place (additive). Returns the
    effective `thermostat` dict callers should use in place of
    `config.raw.get("thermostat")` - unchanged (including None) if the
    panel has no saved zones, since only having *some* apartments use
    this panel and others not is exactly the point.
    """
    climate_map = ClimateMapStore(config.resolve("climate_map.json"))
    manual_override_path = config.resolve("manual_override.json")
    broker_file = config.resolve("mqtt_broker.json")
    panel_zones = climate_map.get_zones()
    extra_switches = climate_map.get_extra_switches()
    extra_sensors = climate_map.get_extra_sensors()

    new_integrations: list[Integration] = []
    if panel_zones is not None:
        new_integrations += build_climate_integrations(panel_zones, broker_file, manual_override_path)
    new_integrations += build_extra_switch_integrations(extra_switches, broker_file)
    new_integrations += build_extra_sensor_integrations(extra_sensors, broker_file)
    if new_integrations:
        existing_names = {i.options.get("name") for i in config.integrations}
        for integration in new_integrations:
            name = integration.options.get("name")
            if name in existing_names:
                logger.warning(
                    "Climate admin panel device %r collides with an existing config.yaml integration "
                    "name - the panel's version will win (DeviceManager._register() is last-one-wins).",
                    name,
                )
        config.integrations = config.integrations + new_integrations

    if panel_zones is None:
        return config.raw.get("thermostat")
    thermostat_cfg = dict(config.raw.get("thermostat") or {})
    thermostat_cfg["zones"] = build_zone_configs(panel_zones)
    return thermostat_cfg


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
        # Validated against the *same* snapshot that gets written back,
        # both under one lock acquisition - not two get_extra_*() calls
        # (each its own separate _load()) followed by a second, later
        # _load() for the write. Reading the current extra_switches/
        # extra_sensors, validating, and writing as three separate steps
        # would leave a window where a concurrent save of the other one
        # (two admin-panel tabs saving near-simultaneously, say) could
        # validate against an already-stale snapshot.
        validate_zones(zones)
        with self._lock:
            data = self._load()
            extra_switches, extra_sensors = data.get("extra_switches") or {}, data.get("extra_sensors") or {}
            validate_extra_switches(extra_switches, zones, extra_sensors)
            validate_extra_sensors(extra_sensors, zones, extra_switches)
            data["zones"] = zones
            save_json_atomic(self.path, data)

    def get_extra_switches(self) -> dict:
        # Independent of zones - either can be panel-managed without the
        # other (an apartment might only ever use this for the
        # dehumidifier/bypass valve and keep zones hand-edited in
        # config.yaml, or the reverse). A missing/null field means "not
        # panel-managed", not "broken" - config.yaml's own switch (if
        # any) is simply untouched, same as an unmanaged zone.
        return self._load().get("extra_switches") or {}

    def set_extra_switches(self, extra_switches: dict) -> None:
        with self._lock:
            data = self._load()
            validate_extra_switches(extra_switches, data.get("zones"), data.get("extra_sensors") or {})
            data["extra_switches"] = extra_switches
            save_json_atomic(self.path, data)

    def get_extra_sensors(self) -> dict:
        # Same independence from zones/extra_switches as get_extra_switches()
        # above - a missing/null field means "not panel-managed", not
        # "broken".
        return self._load().get("extra_sensors") or {}

    def set_extra_sensors(self, extra_sensors: dict) -> None:
        with self._lock:
            data = self._load()
            validate_extra_sensors(extra_sensors, data.get("zones"), data.get("extra_switches") or {})
            data["extra_sensors"] = extra_sensors
            save_json_atomic(self.path, data)
