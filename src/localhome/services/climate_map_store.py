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

And `extra_dimmers` - the VMC (mechanical ventilation), driven by a
Shelly 0/1-10V Dimmer Gen3 whose output sets the fan speed. Same fixed-
role, device-id-only shape again; one device id builds two integrations,
an on/off switch and its speed as a number paired into the switch's card
(see EXTRA_DIMMER_FIELDS/build_extra_dimmer_integrations() below).

A config.yaml integration pointing at a device id the panel now manages
is dropped in favour of the panel's (see apply_to_config()) - otherwise
adopting a hand-wired device here would leave two drivers polling and
commanding the same hardware under two names.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from localhome.config import Integration, LocalHomeConfig
from localhome.errors import InputError
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
        raise InputError("zones_count", f"exactly {ZONE_COUNT} zones are required", count=ZONE_COUNT)

    names: list[str] = []
    device_ids: list[str] = []
    for i, zone in enumerate(zones):
        if not isinstance(zone, dict):
            raise InputError("zone_invalid", f"zone {i} must be an object", zone=i + 1)
        name = zone.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InputError("zone_name_required", f"zone {i}: 'name' is required", zone=i + 1)
        # The name is part of the zone's URLs (/api/climate/zones/<name>/...):
        # a slash splits it however it's encoded. Anything else - accents,
        # apostrophes, quotes - is fine.
        if "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
            raise InputError("zone_name_chars", f"zone {i} ('{name}'): a zone name can't contain '/', '\\' or control characters",
                             zone=name)
        if len(name.strip()) > 40:
            raise InputError("zone_name_long", f"zone {i}: a zone name can be at most 40 characters", zone=i + 1, max=40)
        names.append(name)

        mode = zone.get("sensor_mode")
        if mode not in SENSOR_MODES:
            raise InputError("sensor_mode_invalid", f"zone {i} ('{name}'): sensor_mode must be one of {SENSOR_MODES}", zone=name)

        sensor_device_id = zone.get("sensor_device_id")
        if mode == "shelly":
            if not isinstance(sensor_device_id, str) or not sensor_device_id.strip():
                raise InputError("sensor_id_required",
                                 f"zone {i} ('{name}'): sensor_device_id is required when sensor_mode is 'shelly'", zone=name)
            device_ids.append(sensor_device_id.strip())

        # Same "no hardware yet" escape hatch as the sensor - a valve
        # has no "manual" mode of its own (there's nothing to type a
        # value for), but a simulated one is still fully controllable
        # through the same on/off route the live status page's test
        # buttons use, so the control logic can be exercised end to end
        # before any Shelly relay is actually wired.
        valve_mode = zone.get("valve_mode", "shelly")
        if valve_mode not in VALVE_MODES:
            raise InputError("valve_mode_invalid", f"zone {i} ('{name}'): valve_mode must be one of {VALVE_MODES}", zone=name)
        valve_device_id = zone.get("valve_device_id")
        if valve_mode == "shelly":
            if not isinstance(valve_device_id, str) or not valve_device_id.strip():
                raise InputError("valve_id_required",
                                 f"zone {i} ('{name}'): valve_device_id is required when valve_mode is 'shelly'", zone=name)
            device_ids.append(valve_device_id.strip())

        if "cool_enabled" in zone and not isinstance(zone["cool_enabled"], bool):
            raise InputError("zone_invalid", f"zone {i} ('{name}'): cool_enabled must be true or false", zone=name)

    _check_no_duplicates(names, "duplicate zone name '{value}' - zone names must be unique", "duplicate_zone_name")
    _check_no_duplicates(
        device_ids,
        "duplicate id '{value}' - each Shelly device id may only be used for one role",
        "duplicate_device_id",
    )


def _check_no_duplicates(values: list[str], message: str, code: str = "duplicate_device_id") -> None:
    """Names the actual duplicate rather than just saying "something's
    duplicated" - with up to 2 zones and 2 extra switches all sharing
    one device-id namespace, "a duplicate id was found" alone leaves a
    technician checking every field by hand to find which one."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise InputError(code, message.format(value=value), value=value)
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
# The bypass valve is normally open: its Shelly on means the valve closed.
# The pages say "closed"/"open" by this, not "on"/"off". True unless saved
# otherwise.
BYPASS_NORMALLY_OPEN_KEY = "bypass_valve_normally_open"
# The zone whose valve the dehumidifier draws its chilled water through:
# while it runs (in summer) that valve is held open, even for a zone kept
# out of cooling - see services/interlock.py's Interlock. Never saved: the
# one zone kept out of cooling, if there's exactly one. Saved None: none.
DEHUMIDIFIER_ZONE_KEY = "dehumidifier_water_zone"


def _extra_switch_device_ids(extra_switches: dict) -> list[str]:
    return [
        extra_switches[key].strip()
        for key in EXTRA_SWITCH_FIELDS
        if (extra_switches or {}).get(key)
    ]


def _extra_sensor_device_ids(extra_sensors: dict) -> list[str]:
    # An id kept while the role is simulated or switched off isn't in use.
    if (extra_sensors or {}).get(OUTDOOR_MODE_KEY) in ("simulated", "none"):
        return []
    return [
        extra_sensors[key].strip()
        for key in EXTRA_SENSOR_FIELDS
        if (extra_sensors or {}).get(key)
    ]


def _extra_dimmer_device_ids(extra_dimmers: dict) -> list[str]:
    if (extra_dimmers or {}).get(VMC_MODE_KEY) in ("simulated", "none"):
        return []
    return [
        extra_dimmers[key].strip()
        for key in EXTRA_DIMMER_FIELDS
        if (extra_dimmers or {}).get(key)
    ]


# The outdoor sensor and the VMC each have a source, like a zone's sensor:
# a real Shelly (its id required), simulated, or none at all.
EXTRA_ROLE_MODES = ("shelly", "simulated", "none")
OUTDOOR_MODE_KEY = "outdoor_sensor_mode"
VMC_MODE_KEY = "vmc_mode"
OUTDOOR_SENSOR_NAME = "Outdoor Sensor"
VMC_NAME = "VMC"
# The simulated outdoor sensor: a mild Mediterranean year (monthly means,
# January first) and a 5 degree swing either side over the day - enough
# for the outdoor chart to look like weather, next to the rooms'.
SIMULATED_OUTDOOR_MONTHLY_C = [8, 9, 11, 14, 18, 23, 26, 26, 22, 18, 13, 9]
SIMULATED_OUTDOOR_SWING_C = 5.0


def effective_extra_mode(section: dict | None, id_key: str, mode_key: str, simulated_by_default: bool) -> str:
    """What a fixed role actually runs as. `section` None means the panel
    has never saved that section: a new installation, where the role
    starts simulated or as none - see extra_roles(). A section saved
    before modes existed keeps meaning what it meant: an id is a real
    device, no id is none."""
    if section is None:
        return "simulated" if simulated_by_default else "none"
    mode = section.get(mode_key)
    if mode in EXTRA_ROLE_MODES:
        return mode
    return "shelly" if section.get(id_key) else "none"


def _validate_role_mode(section: dict, id_key: str, mode_key: str) -> None:
    mode = section.get(mode_key)
    if mode is None:
        return
    if mode not in EXTRA_ROLE_MODES:
        raise InputError("role_mode_invalid", f"{mode_key} must be one of {list(EXTRA_ROLE_MODES)}")
    if mode == "shelly" and not (isinstance(section.get(id_key), str) and section[id_key].strip()):
        raise InputError("role_id_required", f"{id_key} is required when {mode_key} is 'shelly'")


def _validate_role_ids(section: dict, fields: dict, other_ids: list[str], section_name: str) -> None:
    """The shared rule for every fixed-role section: each role optional,
    a given id non-empty, and no id reused by any other role anywhere."""
    if not isinstance(section, dict):
        raise InputError("request_invalid", f"{section_name} must be an object")
    device_ids = list(other_ids)
    for key in fields:
        device_id = section.get(key)
        if device_id is not None:
            if not isinstance(device_id, str) or not device_id.strip():
                raise InputError("device_id_invalid", f"{key}: device id must be a non-empty string or null")
            device_ids.append(device_id.strip())
    _check_no_duplicates(
        device_ids,
        "duplicate id '{value}' - each Shelly device id may only be used for one role",
        "duplicate_device_id",
    )


def validate_extra_switches(
    extra_switches: dict, zones: list[dict] | None = None, extra_sensors: dict | None = None,
    extra_dimmers: dict | None = None,
) -> None:
    """Each of the two fixed roles is independently optional - a fresh
    apartment might not have either wired yet - but if given, must be a
    non-empty device id, and no device id may double up with a zone's
    own sensor/valve (validate_zones() enforces the equivalent rule
    within the zones themselves) or the other extra devices below."""
    other_ids = (
        _zone_device_ids(zones or [])
        + _extra_sensor_device_ids(extra_sensors or {})
        + _extra_dimmer_device_ids(extra_dimmers or {})
    )
    _validate_role_ids(extra_switches, EXTRA_SWITCH_FIELDS, other_ids, "extra_switches")
    normally_open = extra_switches.get(BYPASS_NORMALLY_OPEN_KEY, True)
    if not isinstance(normally_open, bool):
        raise InputError("request_invalid", f"{BYPASS_NORMALLY_OPEN_KEY} must be true or false")
    water_zone = extra_switches.get(DEHUMIDIFIER_ZONE_KEY)
    names = [z.get("name") for z in zones or []]
    if water_zone is not None and (not isinstance(water_zone, str) or (zones is not None and water_zone not in names)):
        raise InputError("dehumidifier_zone_invalid", f"{DEHUMIDIFIER_ZONE_KEY}: not one of the zones ({', '.join(names)})",
                         zone=str(water_zone))


def dehumidifier_water_zone(extra_switches: dict, zones: list[dict] | None) -> str | None:
    """The zone whose valve the dehumidifier opens - see DEHUMIDIFIER_ZONE_KEY."""
    extra_switches = extra_switches or {}
    if DEHUMIDIFIER_ZONE_KEY in extra_switches:
        return extra_switches[DEHUMIDIFIER_ZONE_KEY]
    kept_out = [z["name"] for z in zones or [] if z.get("cool_enabled") is False]
    return kept_out[0] if len(kept_out) == 1 else None


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
    extra_dimmers: dict | None = None,
) -> None:
    """Same shape as validate_extra_switches() above, mirrored for the
    one fixed sensor role instead of the two fixed switch roles."""
    other_ids = (
        _zone_device_ids(zones or [])
        + _extra_switch_device_ids(extra_switches or {})
        + _extra_dimmer_device_ids(extra_dimmers or {})
    )
    if isinstance(extra_sensors, dict):
        _validate_role_mode(extra_sensors, "outdoor_sensor_device_id", OUTDOOR_MODE_KEY)
    if isinstance(extra_sensors, dict) and extra_sensors.get(OUTDOOR_MODE_KEY) in ("simulated", "none"):
        return   # the id isn't used, so it can't collide
    _validate_role_ids(extra_sensors, EXTRA_SENSOR_FIELDS, other_ids, "extra_sensors")


# Fixed role again: the VMC, on a Shelly 0/1-10V Dimmer Gen3 (its analog
# output sets the fan speed - see drivers/mqtt/shelly_light.py). The value
# is the on/off switch's name; its speed is DIMMER_SPEED_SUFFIX appended.
EXTRA_DIMMER_FIELDS = {
    "vmc_device_id": "VMC",
}
DIMMER_SPEED_SUFFIX = " Speed"
# The VMC runs continuously - residential mechanical ventilation isn't
# meant to be switched off - so its card has no on/off toggle, only these
# speeds, and picking one also switches it back on if it was off.
# `key` names the dashboard's translated label (vmc_presets.<key>).
VMC_PRESETS = [
    {"key": "night", "value": 25},
    {"key": "day", "value": 50},
    {"key": "boost", "value": 100},
]


def validate_extra_dimmers(
    extra_dimmers: dict, zones: list[dict] | None = None, extra_switches: dict | None = None,
    extra_sensors: dict | None = None,
) -> None:
    other_ids = (
        _zone_device_ids(zones or [])
        + _extra_switch_device_ids(extra_switches or {})
        + _extra_sensor_device_ids(extra_sensors or {})
    )
    if isinstance(extra_dimmers, dict):
        _validate_role_mode(extra_dimmers, "vmc_device_id", VMC_MODE_KEY)
    if isinstance(extra_dimmers, dict) and extra_dimmers.get(VMC_MODE_KEY) in ("simulated", "none"):
        return
    _validate_role_ids(extra_dimmers, EXTRA_DIMMER_FIELDS, other_ids, "extra_dimmers")


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
            # Powered over USB-C: its battery isn't what keeps it going, and
            # isn't shown (web/app.py's zones).
            "external_power": "external.present",
        },
        # The numbers only, as before: whether it's on USB isn't history.
        "history_fields": ["temperature_c", "humidity_pct", "battery_pct", "wifi_rssi_dbm"],
        # What an H&T Gen3 can measure, with room to spare: anything else is
        # a failing element, not the room (-127 °C, 0 %). Without a valid
        # temperature the sensor isn't "ok" - the thermostat's safe path.
        "valid_ranges": {"temperature_c": [-30, 70], "humidity_pct": [1, 100], "battery_pct": [0, 100]},
        "required_fields": ["temperature_c"],
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


VALVE_HOLD_SECONDS = 900


def valve_integration_options(zone: dict, valve_name: str, broker_file: str) -> dict:
    device_id = zone["valve_device_id"].strip()
    return {
        "name": valve_name,
        "broker_file": broker_file,
        "state_topic": f"{device_id}/status/switch:0",
        "command_topic": f"{device_id}/command/switch:0",
        "state_field": "output",
        "payload_on": True,
        "payload_off": False,
        # What the relay takes on its command topic - "True"/"False" (the
        # payloads above as text) it ignores without a word.
        "command_on": "on",
        "command_off": "off",
        # Open only while LocalHome is there to say so: "on,900", renewed
        # every 5 minutes - if the board goes, the valve closes within 15
        # minutes by itself (drivers/mqtt/switch.py).
        "hold_seconds": VALVE_HOLD_SECONDS,
        # Asked for its state at start and when back online - what's kept
        # on disk is from before - and its safety timer, if it ever acts,
        # written where the watchdogs write (drivers/mqtt/switch.py).
        "status_command": "status_update",
        "event_log": os.path.join(os.path.dirname(broker_file), "watchdog.log"),
        # What it is - the settings its Shelly is checked against follow
        # (services/shelly_health.py's expected_settings()).
        "shelly_role": "valve",
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


def _sensor_integration(zone: dict, sensor_name: str, broker_file: str, manual_store_path: str,
                        valve_name: str | None = None, season_file: str | None = None,
                        simulated_state_file: str | None = None) -> Integration:
    mode = zone.get("sensor_mode", "shelly")
    if mode == "shelly":
        return Integration("climate", "mqtt_json", _shelly_sensor_options(zone, sensor_name, broker_file))
    if mode == "simulated":
        # A stand-in for the Shelly H&T Gen3 that will go there: its
        # fields, its reporting rhythm, a room that follows the zone's
        # valve and the season - so a test bench or a demo runs like the
        # real thing (drivers/simulated/climate.py's mimic_ht_gen3).
        options = {"name": sensor_name, "dashboard_tab": "climate", "mimic_ht_gen3": True}
        if valve_name:
            options["influenced_by"] = valve_name
        if season_file:
            options["season_file"] = season_file
        if simulated_state_file:
            options["state_file"] = simulated_state_file
        return Integration("climate", "simulated", options)
    if mode == "manual":
        return Integration(
            "climate", "manual_override",
            {"name": sensor_name, "dashboard_tab": "climate", "store_path": manual_store_path},
        )
    raise ValueError(f"unknown sensor_mode {mode!r}")


def _valve_integration(zone: dict, valve_name: str, broker_file: str) -> Integration:
    mode = zone.get("valve_mode", "shelly")
    if mode == "shelly":
        return Integration("switch", "mqtt_json", valve_integration_options(zone, valve_name, broker_file))
    if mode == "simulated":
        # Fully controllable through the same generic on/off route the
        # live status page's manual test buttons already use - lets the
        # thermostat's control logic (and a technician's manual test) be
        # exercised end to end before any relay is actually wired.
        return Integration("switch", "simulated", {"name": valve_name, "dashboard_tab": "climate"})
    raise ValueError(f"unknown valve_mode {mode!r}")


def build_climate_integrations(zones: list[dict], broker_file: str, manual_store_path: str,
                               season_file: str | None = None,
                               simulated_state_file: str | None = None) -> list[Integration]:
    """The Integrations DeviceManager needs to build every zone's sensor
    + valve - concatenated onto config.integrations before DeviceManager
    construction (see web/app.py's create_app()), the same "additive,
    zero core changes" seam this project already uses for panel-added
    devices."""
    integrations: list[Integration] = []
    for zone in zones:
        sensor_name = f"{zone['name']} Sensor"
        valve_name = f"{zone['name']} Valve"
        integrations.append(_sensor_integration(zone, sensor_name, broker_file, manual_store_path,
                                                valve_name, season_file, simulated_state_file))
        integrations.append(_valve_integration(zone, valve_name, broker_file))
    return integrations


def build_extra_switch_integrations(extra_switches: dict, broker_file: str) -> list[Integration]:
    """Each of the two fixed roles (EXTRA_SWITCH_FIELDS) becomes a real
    switch only if its device id is set - a fresh apartment that hasn't
    wired one yet simply doesn't get an integration for it, same as an
    unmanaged zone. Reuses the exact same Shelly relay shape a zone's
    valve already uses (valve_integration_options() above), just keyed
    by the fixed field name instead of "valve_device_id"."""
    integrations: list[Integration] = []
    for key, display_name in EXTRA_SWITCH_FIELDS.items():
        device_id = (extra_switches or {}).get(key)
        if device_id:
            zone_shaped = {"valve_device_id": device_id}
            options = valve_integration_options(zone_shaped, display_name, broker_file)
            if key == "bypass_valve_device_id":
                if (extra_switches or {}).get(BYPASS_NORMALLY_OPEN_KEY, True):
                    options["normally_open"] = True
                # If the board stops, the bypass stays as the season left
                # it - closed all summer - so no hold: it keeps its state.
                options.pop("hold_seconds")
                options["shelly_role"] = "bypass"
            else:
                options["shelly_role"] = "dehumidifier"
            integrations.append(Integration("switch", "mqtt_json", options))
    return integrations


def build_extra_sensor_integrations(extra_sensors: dict, broker_file: str, mode: str | None = None) -> list[Integration]:
    """The outdoor sensor, by its mode (see effective_extra_mode()):
    "shelly" is the same Shelly H&T Gen3 shape a zone's own sensor uses
    (_shelly_sensor_options()), "simulated" a sensor that follows the
    month and the time of day, "none" nothing at all. Without a mode (the
    older call), an id means a real device and no id means none."""
    if mode == "none":
        return []
    if mode == "simulated":
        return [Integration("climate", "simulated", {
            "name": OUTDOOR_SENSOR_NAME,
            "dashboard_tab": "climate",
            "monthly_base_c": SIMULATED_OUTDOOR_MONTHLY_C,
            "daily_swing_c": SIMULATED_OUTDOOR_SWING_C,
            "base_humidity_pct": 65.0,
            "drift_rate_c_per_min": 0.05,
            "poll_interval_seconds": 60.0,
        })]
    integrations: list[Integration] = []
    for key, display_name in EXTRA_SENSOR_FIELDS.items():
        device_id = (extra_sensors or {}).get(key)
        if device_id:
            zone_shaped = {"sensor_device_id": device_id}
            integrations.append(Integration("climate", "mqtt_json", _shelly_sensor_options(zone_shaped, display_name, broker_file)))
    return integrations


def build_extra_dimmer_integrations(extra_dimmers: dict, broker_file: str, mode: str | None = None) -> list[Integration]:
    """One device id -> two integrations over the same Shelly RPC
    connection: the on/off switch, and its speed as a number paired into
    that switch's dashboard card. "simulated" builds the same pair with
    no hardware behind it - the same card, speeds and day/night program -
    and "none" nothing. Without a mode, a blank id means none."""
    if mode == "none":
        return []
    if mode == "simulated":
        return [
            Integration("switch", "simulated", {"name": VMC_NAME, "initial_on": True, "hide_toggle": True}),
            Integration("number", "simulated", {
                "name": VMC_NAME + DIMMER_SPEED_SUFFIX, "paired_switch": VMC_NAME,
                "presets": VMC_PRESETS, "initial_value": 50, "min": 0, "max": 100, "unit": "%",
                "on_with_value": True,
            }),
        ]
    integrations: list[Integration] = []
    for key, display_name in EXTRA_DIMMER_FIELDS.items():
        device_id = (extra_dimmers or {}).get(key)
        if device_id:
            base = {"broker_file": broker_file, "device_id": device_id.strip()}
            integrations.append(Integration("switch", "shelly_light", {**base, "name": display_name, "hide_toggle": True}))
            integrations.append(Integration("number", "shelly_light", {
                **base, "name": display_name + DIMMER_SPEED_SUFFIX, "paired_switch": display_name,
                "presets": VMC_PRESETS, "on_with_value": True,
            }))
    return integrations


def integration_device_id(integration: Integration) -> str | None:
    """The Shelly device an integration talks to: an explicit `device_id`
    (RPC drivers) or the first segment of its MQTT topics."""
    options = integration.options
    if options.get("device_id"):
        return str(options["device_id"]).strip()
    return _device_id_from_topic(options.get("state_topic") or options.get("command_topic"))


def _blank_zone() -> dict:
    # Simulated, not "shelly": this is a zone that doesn't exist in
    # config.yaml at all, so there's no real device behind it - and a
    # "shelly" zone with no device id can't be saved from the panel.
    return {
        "name": "", "sensor_mode": "simulated", "sensor_device_id": None,
        "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True,
    }


def _device_id_from_topic(topic) -> str | None:
    """The inverse of _shelly_sensor_options()/valve_integration_options()
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
    extra_dimmers = climate_map.get_extra_dimmers()
    roles = extra_roles(config, climate_map)

    new_integrations: list[Integration] = []
    if panel_zones is not None:
        # The season, for a simulated sensor to know which way an open valve takes its room.
        schedules = config.resolve((config.raw.get("thermostat") or {}).get("schedules_file", "schedules.json"))
        # Where a simulated sensor keeps its room across a restart.
        simulated_state = config.resolve("simulated_sensors.json")
        new_integrations += build_climate_integrations(panel_zones, broker_file, manual_override_path,
                                                       str(schedules), str(simulated_state))
    new_integrations += build_extra_switch_integrations(extra_switches, broker_file)
    new_integrations += build_extra_sensor_integrations(extra_sensors, broker_file, roles["outdoor"]["mode"])
    new_integrations += build_extra_dimmer_integrations(extra_dimmers, broker_file, roles["vmc"]["mode"])
    if new_integrations:
        # A hand-written entry for a device the panel now manages would
        # poll and command the same hardware a second time, under its old
        # name - the panel's version replaces it.
        panel_devices = {integration_device_id(i) for i in new_integrations} - {None}
        kept = []
        for integration in config.integrations:
            if integration_device_id(integration) in panel_devices:
                logger.warning(
                    "config.yaml integration %r uses device %s, now managed by the climate admin "
                    "panel - using the panel's version instead. Remove it from config.yaml to "
                    "silence this.",
                    integration.options.get("name"), integration_device_id(integration),
                )
            else:
                kept.append(integration)
        config.integrations = kept
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
    # The panel's dehumidifier draws its water through a zone's valve:
    # while it runs, in summer, that valve is held open - unless
    # config.yaml already links the dehumidifier to something.
    water_zone = dehumidifier_water_zone(extra_switches, panel_zones)
    dehumidifier = EXTRA_SWITCH_FIELDS["dehumidifier_device_id"]
    interlocks = list(thermostat_cfg.get("interlocks") or [])
    if ((extra_switches or {}).get("dehumidifier_device_id") and water_zone
            and any(z.get("name") == water_zone for z in panel_zones)
            and not any(link.get("leader") == dehumidifier for link in interlocks)):
        interlocks.append({"leader": dehumidifier, "follower": f"{water_zone} Valve", "active_in_mode": "cool"})
        thermostat_cfg["interlocks"] = interlocks
    return thermostat_cfg


# What each panel-managed extra switch does in each season until someone
# changes it in the admin panel: "on"/"off" are forced by LocalHome,
# "manual" is left to whoever uses it (see services/interlock.py's
# SeasonRule). The bypass valve keeps chilled water out of the bathroom
# panels in summer; the dehumidifier is only of use in summer.
SEASON_MODES = ("heat", "cool")
SEASON_RULE_DEFAULTS = {
    "bypass_valve_device_id": {"heat": "off", "cool": "on"},
    "dehumidifier_device_id": {"heat": "off", "cool": "manual"},
}


def configured_rule_switches(thermostat_cfg: dict | None) -> set[str]:
    """Switches config.yaml already gives a season rule (or an interlock
    leader): those keep it, and the panel's rule for them is ignored."""
    cfg = thermostat_cfg or {}
    names = {item.get("switch") for key in ("mode_switches", "mode_limited_switches", "season_rules")
             for item in cfg.get(key) or []}
    return names | {link.get("follower") for link in cfg.get("interlocks") or []}


def panel_season_rules(extra_switches: dict, saved_rules: dict, thermostat_cfg: dict | None) -> list[dict]:
    """[{"role", "switch", "heat", "cool"}] for each extra switch the panel
    has a device id for and config.yaml doesn't already rule. Nothing
    without a thermostat: no thermostat, no season."""
    if thermostat_cfg is None:
        return []
    taken = configured_rule_switches(thermostat_cfg)
    rules = []
    for field, name in EXTRA_SWITCH_FIELDS.items():
        if (extra_switches or {}).get(field) and name not in taken:
            rules.append({"role": field, "switch": name, **(saved_rules or {}).get(field, SEASON_RULE_DEFAULTS[field])})
    return rules


def validate_season_rules(rules) -> dict:
    """{role: {"heat": action, "cool": action}} for the panel's two extra
    switch roles; raises InputError on anything else."""
    from localhome.services.interlock import SEASON_ACTIONS
    if not isinstance(rules, dict):
        raise InputError("request_invalid", "'rules' must be an object")
    clean = {}
    for field, rule in rules.items():
        if field not in SEASON_RULE_DEFAULTS:
            raise InputError("season_rule_invalid", f"unknown switch role {field!r}")
        if not isinstance(rule, dict) or set(rule) != set(SEASON_MODES)                 or any(rule[m] not in SEASON_ACTIONS for m in SEASON_MODES):
            raise InputError("season_rule_invalid",
                             f"{field}: give 'heat' and 'cool', each one of {', '.join(SEASON_ACTIONS)}")
        clean[field] = {m: rule[m] for m in SEASON_MODES}
    return clean


def extra_roles(config, climate_map: "ClimateMapStore") -> dict:
    """The outdoor sensor's and the VMC's effective source and id. A role
    the panel has never saved: the outdoor sensor starts as none - the
    home then says it isn't connected yet, instead of showing made-up
    weather next to the real rooms. The VMC starts simulated on a climate
    installation (the panel manages the zones), so its card and program
    can be tried - unless config.yaml already wires a real one by hand
    (its fixed name, or any Shelly dimmer): a simulated stand-in would
    replace or duplicate it. Anywhere else - LocalHome running covers or
    plugs only - nothing appears by itself."""
    hand_wired = [e for e in config.raw.get("integrations") or []
                  if isinstance(e, dict) and e.get("enabled", True) is not False]
    names = {entry.get("name") for entry in hand_wired if isinstance(entry, dict)}
    has_dimmer = any(isinstance(e, dict) and e.get("type") == "shelly_light" for e in hand_wired)
    climate_panel = climate_map.get_zones() is not None
    sensors = climate_map.get_extra_sensors() if climate_map.has_section("extra_sensors") else None
    dimmers = climate_map.get_extra_dimmers() if climate_map.has_section("extra_dimmers") else None
    outdoor = effective_extra_mode(sensors, "outdoor_sensor_device_id", OUTDOOR_MODE_KEY, simulated_by_default=False)
    vmc = effective_extra_mode(dimmers, "vmc_device_id", VMC_MODE_KEY,
                               simulated_by_default=climate_panel and VMC_NAME not in names and not has_dimmer)
    # The id only counts for a real device - one typed in ahead while the
    # role is still simulated changes nothing that runs.
    return {
        "outdoor": {"mode": outdoor,
                    "device_id": (sensors or {}).get("outdoor_sensor_device_id") if outdoor == "shelly" else None},
        "vmc": {"mode": vmc, "device_id": (dimmers or {}).get("vmc_device_id") if vmc == "shelly" else None},
    }


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
            extra_dimmers = data.get("extra_dimmers") or {}
            validate_extra_switches(extra_switches, zones, extra_sensors, extra_dimmers)
            validate_extra_sensors(extra_sensors, zones, extra_switches, extra_dimmers)
            validate_extra_dimmers(extra_dimmers, zones, extra_switches, extra_sensors)
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
            validate_extra_switches(
                extra_switches, data.get("zones"), data.get("extra_sensors") or {}, data.get("extra_dimmers") or {},
            )
            data["extra_switches"] = extra_switches
            save_json_atomic(self.path, data)

    def has_section(self, key: str) -> bool:
        """Whether the panel has ever saved this section - "never saved"
        and "saved empty" mean different things (see extra_roles())."""
        return key in self._load()

    def get_season_rules(self) -> dict:
        """{role: {"heat", "cool"}} - the defaults for a role never saved."""
        saved = self._load().get("season_rules") or {}
        return {field: {**default, **saved.get(field, {})} for field, default in SEASON_RULE_DEFAULTS.items()}

    def set_season_rules(self, rules: dict) -> None:
        clean = validate_season_rules(rules)
        with self._lock:
            data = self._load()
            data["season_rules"] = {**(data.get("season_rules") or {}), **clean}
            save_json_atomic(self.path, data)

    def get_extra_sensors(self) -> dict:
        # Same independence from zones/extra_switches as get_extra_switches()
        # above - a missing/null field means "not panel-managed", not
        # "broken".
        return self._load().get("extra_sensors") or {}

    def set_extra_sensors(self, extra_sensors: dict) -> None:
        with self._lock:
            data = self._load()
            validate_extra_sensors(
                extra_sensors, data.get("zones"), data.get("extra_switches") or {}, data.get("extra_dimmers") or {},
            )
            data["extra_sensors"] = extra_sensors
            save_json_atomic(self.path, data)

    def get_extra_dimmers(self) -> dict:
        # Same independence as get_extra_switches() above.
        return self._load().get("extra_dimmers") or {}

    def set_extra_dimmers(self, extra_dimmers: dict) -> None:
        with self._lock:
            data = self._load()
            validate_extra_dimmers(
                extra_dimmers, data.get("zones"), data.get("extra_switches") or {}, data.get("extra_sensors") or {},
            )
            data["extra_dimmers"] = extra_dimmers
            save_json_atomic(self.path, data)
