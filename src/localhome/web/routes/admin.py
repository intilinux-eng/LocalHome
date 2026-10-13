"""The climate admin panel's own endpoints: lets an installer map the
fixed two-zone thermostat onto this apartment's own Shelly device ids, and
debug the control logic with a manually-typed temperature - see
services/climate_map_store.py / services/manual_override.py. Also the MQTT
broker's status, the season's rules, the extra devices' roles, the run
hours against the heat meter."""
from __future__ import annotations

import math
from datetime import date

from flask import Flask, jsonify, request

from localhome.config import LocalHomeConfig
from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import (
    BYPASS_NORMALLY_OPEN_KEY,
    DEHUMIDIFIER_ZONE_KEY,
    EXTRA_DIMMER_FIELDS,
    EXTRA_SENSOR_FIELDS,
    EXTRA_SWITCH_FIELDS,
    OUTDOOR_MODE_KEY,
    OUTDOOR_SENSOR_NAME,
    VMC_MODE_KEY,
    SEASON_RULE_DEFAULTS,
    ClimateMapStore,
    configured_rule_switches,
    dehumidifier_water_zone,
    extra_roles,
    snapshot_from_config,
)
from localhome.services.manual_override import ManualOverrideStore
from localhome.services.mqtt_broker_config import get_broker
from localhome.services.mqtt_broker_status import BrokerStatus
from localhome.drivers.mqtt import client as mqtt_client
from localhome.services.interlock import SEASON_ACTIONS
from localhome.services.shelly_check import ShellyCheck
from localhome.services.thermostat import ThermostatController
from localhome.errors import InputError
from localhome.services import run_hours
from localhome.web.routes.common import _fail, _json_body, _refusal
from localhome.web.routes.climate import _valve_zones


def _usage_answer(app: Flask, year=None) -> dict:
    """The admin panel's hours and consumption: `year` (this one by
    default) month by month beside the heat meter's values and the
    outdoor temperature, this month's kWh estimated for the season the
    plant is in, the years there's something for, and this month's
    hours."""
    zones = _valve_zones(app.config["THERMOSTAT"])
    history = app.config["HISTORY"]
    interval = app.config["HISTORY_POLL_INTERVAL"]
    meter = app.config["HEAT_METER"].get()
    today = date.today()
    years = run_hours.years_of(history, meter, today)
    year = year if year in years else today.year
    thermostat = app.config["THERMOSTAT"]
    mode = thermostat.schedule_store.get_mode() if thermostat is not None else None
    report = run_hours.year_report(history, zones, interval, meter, year, mode=mode, outdoor=OUTDOOR_SENSOR_NAME)
    now = report if year == today.year else run_hours.year_report(history, zones, interval, meter, today.year)
    return {"zones": [z["name"] for z in zones], "years": years, **report,
            "this_month_hours": now["months"][-1]["plant_hours"]}


def _set_roles(roles: dict) -> dict:
    """Only the roles that have a device id - a saved `{"x": null}` and
    nothing saved at all build the same drivers, so a cleared field isn't
    a pending change."""
    return {key: value for key, value in (roles or {}).items() if value}


def _built_role(role: dict) -> tuple | None:
    """What an outdoor sensor's or VMC's source builds: nothing for none,
    nor for a Shelly still to configure - one to the other is no restart."""
    if role["mode"] == "none" or (role["mode"] == "shelly" and not role["device_id"]):
        return None
    return role["mode"], role["device_id"]


def register(app: Flask) -> None:
    """The admin panel's endpoints."""
    broker_status = BrokerStatus(app.config["LOCALHOME_CONFIG"].resolve("mqtt_broker.json"),
                                 connection_up=mqtt_client.connection_up)
    app.config.setdefault("SHELLY_CHECK", ShellyCheck(app.config["LOCALHOME_CONFIG"].resolve("mqtt_broker.json")))

    @app.route("/api/climate-admin/shelly-check")
    def api_climate_admin_shelly_check():
        """The Shelly whose id was just typed in the panel: there or not,
        and the settings its role needs that it has otherwise - asked at
        once, before any save or restart. See services/shelly_check.py."""
        check: ShellyCheck = app.config["SHELLY_CHECK"]
        return jsonify(**check.check(request.args.get("id", ""), request.args.get("role", "")))

    @app.route("/api/climate-admin/broker-status")
    def api_climate_admin_broker_status():
        # Whether the broker answers, kept apart from each zone's own
        # status so a device stuck on "not reachable yet" isn't
        # misdiagnosed as its own fault when the broker itself is down.
        # From LocalHome's own connection when it's up, else a TCP probe
        # at most once a minute - see services/mqtt_broker_status.py.
        return jsonify(ok=True, **broker_status.get())

    @app.route("/api/climate-admin/broker")
    def api_climate_admin_get_broker():
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        broker_file = config.resolve("mqtt_broker.json")
        return jsonify(ok=True, **get_broker(broker_file))

    @app.route("/api/climate-admin/map")
    def api_climate_admin_get_map():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        saved = climate_map.get_zones()
        # True the instant a save no longer matches what this process
        # actually loaded at startup - see create_app()'s
        # ACTIVE_CLIMATE_ZONES comment for why this can't be inferred
        # from driver names alone.
        restart_pending = saved != app.config["ACTIVE_CLIMATE_ZONES"]
        if saved is not None:
            # Not saved yet: the factory zones, every Shelly to configure.
            return jsonify(ok=True, configured=climate_map.zones_saved(), zones=saved, restart_pending=restart_pending)
        # Nothing saved yet - read config.yaml's *current* hand-written
        # setup as the form's starting point (real device ids included)
        # instead of a blank form, so trying "manual"/"simulated" mode
        # for a quick test doesn't mean re-typing a real device id -
        # this only ever reads config.yaml, never changes the real
        # device, and is never consulted again once something's saved.
        snapshot = snapshot_from_config(thermostat.zones, config.integrations) if thermostat else None
        return jsonify(ok=True, configured=False, zones=snapshot, restart_pending=restart_pending)

    @app.route("/api/climate-admin/map", methods=["POST"])
    def api_climate_admin_set_map():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = _json_body()
        zones = data.get("zones")
        if not isinstance(zones, list):
            return jsonify(ok=False, error="missing 'zones'"), 400
        previous = climate_map.get_zones()
        try:
            climate_map.set_zones(zones)
        except ValueError as exc:
            return _refusal(exc, 400)
        warnings = []
        if previous is not None:
            for old_zone, new_zone in zip(previous, zones):
                if old_zone.get("name") != new_zone.get("name"):
                    warnings.append(
                        f"Zone renamed from '{old_zone.get('name')}' to '{new_zone.get('name')}' - its "
                        "existing weekly schedule (if any) stays saved under the old name and won't "
                        "carry over automatically."
                    )
        return jsonify(ok=True, restart_required=True, warnings=warnings)

    @app.route("/api/climate-admin/extra-switches")
    def api_climate_admin_get_extra_switches():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        saved = climate_map.get_extra_switches()
        restart_pending = _set_roles(saved) != _set_roles(app.config["ACTIVE_EXTRA_SWITCHES"])
        zones = climate_map.get_zones() or []
        return jsonify(ok=True, restart_pending=restart_pending, **{key: saved.get(key) for key in EXTRA_SWITCH_FIELDS},
                       **{BYPASS_NORMALLY_OPEN_KEY: saved.get(BYPASS_NORMALLY_OPEN_KEY, True),
                          DEHUMIDIFIER_ZONE_KEY: dehumidifier_water_zone(saved, zones)},
                       zones=[z["name"] for z in zones])

    @app.route("/api/climate-admin/extra-switches", methods=["POST"])
    def api_climate_admin_set_extra_switches():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = _json_body()
        extra_switches = {key: data.get(key) for key in EXTRA_SWITCH_FIELDS}
        for key in (BYPASS_NORMALLY_OPEN_KEY, DEHUMIDIFIER_ZONE_KEY):
            if key in data:
                extra_switches[key] = data[key]
        try:
            climate_map.set_extra_switches(extra_switches)
        except ValueError as exc:
            return _refusal(exc, 400)
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/season-rules")
    def api_climate_admin_get_season_rules():
        """What each of the panel's extra switches does in each season. A
        role config.yaml already rules is shown but not editable here."""
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        taken = configured_rule_switches(config.raw.get("thermostat"))
        saved = climate_map.get_season_rules()
        wired = climate_map.get_extra_switches()
        roles = [{"role": field, "switch": EXTRA_SWITCH_FIELDS[field], **saved[field],
                  "wired": bool(wired.get(field)), "in_config": EXTRA_SWITCH_FIELDS[field] in taken,
                  # Its "on" closes it: the admin says "closed"/"open".
                  "normally_open": field == "bypass_valve_device_id" and wired.get(BYPASS_NORMALLY_OPEN_KEY, True) is not False}
                 for field in SEASON_RULE_DEFAULTS]
        # config.yaml's own rules: an installation that writes its switches
        # there has no use for the panel's empty places beside them.
        config_rules = sum(1 for rule in app.config["SEASON_RULES"]() if rule["source"] == "config")
        return jsonify(ok=True, thermostat=app.config["THERMOSTAT"] is not None, roles=roles,
                       actions=list(SEASON_ACTIONS), config_rules=config_rules)

    @app.route("/api/climate-admin/season-rules", methods=["POST"])
    def api_climate_admin_set_season_rules():
        """Takes effect at once: the interlock reads the rules on every tick,
        and ticks now instead of within the minute."""
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        try:
            climate_map.set_season_rules(_json_body().get("rules"))
        except ValueError as exc:
            return _refusal(exc, 400)
        interlock = app.config["INTERLOCK"]
        if interlock is not None:
            interlock.tick()
        return jsonify(ok=True)

    @app.route("/api/climate-admin/extra-sensors")
    def api_climate_admin_get_extra_sensors():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        saved = climate_map.get_extra_sensors()
        role = extra_roles(config, climate_map)["outdoor"]
        restart_pending = _built_role(role) != _built_role(app.config["ACTIVE_EXTRA_ROLES"]["outdoor"])
        return jsonify(ok=True, restart_pending=restart_pending, outdoor_sensor_mode=role["mode"],
                       **{key: saved.get(key) for key in EXTRA_SENSOR_FIELDS})

    @app.route("/api/climate-admin/extra-sensors", methods=["POST"])
    def api_climate_admin_set_extra_sensors():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = _json_body()
        extra_sensors = {key: data.get(key) for key in EXTRA_SENSOR_FIELDS}
        if data.get(OUTDOOR_MODE_KEY) is not None:
            extra_sensors[OUTDOOR_MODE_KEY] = data[OUTDOOR_MODE_KEY]
        try:
            climate_map.set_extra_sensors(extra_sensors)
        except ValueError as exc:
            return _refusal(exc, 400)
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/extra-dimmers")
    def api_climate_admin_get_extra_dimmers():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        saved = climate_map.get_extra_dimmers()
        role = extra_roles(config, climate_map)["vmc"]
        restart_pending = _built_role(role) != _built_role(app.config["ACTIVE_EXTRA_ROLES"]["vmc"])
        # Until the panel manages the VMC, offer the id of a dimmer already
        # hand-wired in config.yaml, so adopting it is one click, not
        # re-typing an id that's already known.
        suggested = None
        if not saved.get("vmc_device_id"):
            for entry in config.raw.get("integrations") or []:
                if (entry.get("type") == "shelly_light" and entry.get("kind") == "switch" and entry.get("device_id")
                        and entry.get("enabled", True) is not False):
                    suggested = {"device_id": entry["device_id"], "name": entry.get("name")}
                    break
        return jsonify(
            ok=True, restart_pending=restart_pending, suggested_vmc=suggested, vmc_mode=role["mode"],
            **{key: saved.get(key) for key in EXTRA_DIMMER_FIELDS},
        )

    @app.route("/api/climate-admin/extra-dimmers", methods=["POST"])
    def api_climate_admin_set_extra_dimmers():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = _json_body()
        extra_dimmers = {key: data.get(key) for key in EXTRA_DIMMER_FIELDS}
        if data.get(VMC_MODE_KEY) is not None:
            extra_dimmers[VMC_MODE_KEY] = data[VMC_MODE_KEY]
        try:
            climate_map.set_extra_dimmers(extra_dimmers)
        except ValueError as exc:
            return _refusal(exc, 400)
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/usage")
    def api_usage():
        """?year=2025: that year; this one without."""
        return jsonify(ok=True, **_usage_answer(app, request.args.get("year", type=int)))

    @app.route("/api/climate-admin/heat-meter/year", methods=["POST"])
    def api_heat_meter_year():
        """{"year": 2026, "months": {"01": {"heat_kwh", "cool_kwh"} or null,
        ...}}: the heat meter's month-end values for that year, all at once."""
        body = _json_body()
        try:
            app.config["HEAT_METER"].set_year(body.get("year"), body.get("months"))
        except InputError as exc:
            return _refusal(exc, 400)
        return jsonify(ok=True, **_usage_answer(app, body.get("year")))

    @app.route("/api/climate-admin/zones/<name>/manual-value", methods=["POST"])
    def api_climate_admin_set_manual_value(name):
        manual_override: ManualOverrideStore = app.config["MANUAL_OVERRIDE"]
        manager: DeviceManager = app.config["MANAGER"]
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]

        # Against ACTIVE_CLIMATE_ZONES (what this process actually
        # loaded at startup), not climate_map.get_zones() (whatever's
        # currently saved) - a zone can be saved as "manual" before a
        # restart while a *different*, still-running driver (e.g. a
        # real Shelly sensor) owns that name in this process. Matches
        # every sibling route's own active-vs-saved distinction - see
        # create_app()'s ACTIVE_CLIMATE_ZONES comment.
        active_zones = app.config["ACTIVE_CLIMATE_ZONES"] or []
        zone = next((z for z in active_zones if z.get("name") == name), None)
        if zone is None or zone.get("sensor_mode") != "manual":
            return _fail("zone_not_manual", f"zone '{name}' is not currently configured in manual mode", 409, zone=name)
        # Diagnostic mode makes the thermostat.tick() below a no-op (see
        # ScheduleStore.get_diagnostic_mode()), so setting a value here
        # would silently do nothing to the valve - confusing during the
        # exact "does the control logic actually react" check this quick-set
        # box exists for. Same "reject, don't silently no-op" stance as the
        # mode_switch block in api_sensor_action() below.
        if thermostat and thermostat.schedule_store.get_diagnostic_mode():
            return _fail("diagnostic_active", "diagnostic mode is active - turn it off to let the thermostat react to a manual value", 409)

        data = _json_body()
        if "temperature_c" not in data:
            return jsonify(ok=False, error="missing 'temperature_c'"), 400
        try:
            temperature_c = float(data["temperature_c"])
            humidity_pct = float(data["humidity_pct"]) if data.get("humidity_pct") is not None else None
        except (TypeError, ValueError):
            return _fail("manual_not_numbers", "temperature_c/humidity_pct must be numbers")
        # A typo ("215" for 21.5) would drive the valves on nonsense, and
        # float() also takes "nan"/"inf" - which aren't valid JSON for the
        # pages that read the value back. Real indoor readings only.
        if not (math.isfinite(temperature_c) and -30 <= temperature_c <= 60):
            return _fail("manual_temp_range", "temperature_c must be between -30 and 60")
        if humidity_pct is not None and not (math.isfinite(humidity_pct) and 0 <= humidity_pct <= 100):
            return _fail("manual_humidity_range", "humidity_pct must be between 0 and 100")

        sensor_name = f"{name} Sensor"
        manual_override.set(sensor_name, temperature_c, humidity_pct)
        # Apply immediately, no restart needed - unlike the map above,
        # this only mutates the already-running ManualOverrideDriver's
        # backing value, not which drivers exist.
        poller = manager.pollers.get(sensor_name)
        if poller is not None:
            reading = {"ok": True, "name": sensor_name, "temperature_c": temperature_c, "manual_override": True}
            if humidity_pct is not None:
                reading["humidity_pct"] = humidity_pct
            poller.cache.set(reading)
        if thermostat:
            thermostat.tick()
        return jsonify(ok=True)
