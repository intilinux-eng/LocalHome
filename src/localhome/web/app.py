"""Flask application factory.

Routes are generic across device kinds - this module never imports a
specific brand's driver; everything comes from the DeviceManager built
out of config.yaml. Adding a new brand of power meter, climate sensor or
switch never requires touching this file; adding a wholly new *kind* of
device (not cover/power_meter/climate/switch) does - see
docs/adding-a-driver.md.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from localhome.config import LocalHomeConfig, load_config
from localhome.core.manager import DeviceManager
from localhome.core.registry import skipped_driver_modules
from localhome.services.climate_map_store import (
    EXTRA_DIMMER_FIELDS,
    EXTRA_SENSOR_FIELDS,
    EXTRA_SWITCH_FIELDS,
    ClimateMapStore,
    apply_to_config,
    snapshot_from_config,
)
from localhome.services.dewpoint import dew_point_c
from localhome.services.history import HistoryRecorder, HistoryStore, PowerBudget, RANGE_BUCKETS
from localhome.services.manual_override import ManualOverrideStore
from localhome.services.mqtt_broker_config import get_broker, set_broker
from localhome.services.mqtt_broker_status import check_broker, check_host_port
from localhome.services.position_control import PositionStore
from localhome.services.interlock import Interlock, InterlockController, ModeSwitch
from localhome.services.self_restart import is_supported, schedule_restart
from localhome.services import system_status
from localhome.services.system_clock import server_time
from localhome.services.thermostat import MODES, ScheduleStore, ThermostatController, Zone
from localhome.web.auth import register_auth
from localhome.web.i18n import load_translations

logger = logging.getLogger(__name__)


def create_app(config_path: str | None = None) -> Flask:
    app = Flask(__name__)

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc):
        # Flask's own default for an uncaught exception is an HTML error
        # page - fine for a browser navigating directly, but every route
        # here is JSON, and the frontend's fetch().then(r => r.json())
        # calls expect that shape even on failure (an {ok, error} object,
        # same as every route's own handled error paths already return).
        # Getting HTML back instead is a JSON parse error in the browser
        # console that gives no hint what actually went wrong - and, more
        # importantly, this is the backstop for a bug nobody anticipated
        # yet (like a bad persisted value crashing one specific route),
        # not a substitute for handling errors close to where they can
        # happen. Logging here means it's still visible in the console/
        # journalctl output even though the user just sees a clean error.
        if isinstance(exc, HTTPException):
            # A normal 404/405/etc. (unmatched route, wrong method) is
            # not "unexpected" - let Flask handle it exactly as it
            # would with no errorhandler registered at all.
            return exc
        logger.exception("Unhandled error handling %s %s", request.method, request.path)
        return jsonify(ok=False, error="internal error - see server logs"), 500

    config = load_config(config_path)

    # The climate admin panel: lets an installer map the fixed two-zone
    # thermostat onto whichever Shelly devices *this* apartment has,
    # instead of hand-editing config.yaml - see
    # services/climate_map_store.py's apply_to_config(), which does the
    # actual merge (also used by `localhome-cli doctor`, so it
    # cross-checks the same effective setup this app runs, not just
    # config.yaml's own literal thermostat block).
    climate_map = ClimateMapStore(config.resolve("climate_map.json"))
    manual_override_path = config.resolve("manual_override.json")
    manual_override = ManualOverrideStore(manual_override_path)
    panel_zones = climate_map.get_zones()
    extra_switches = climate_map.get_extra_switches()
    extra_sensors = climate_map.get_extra_sensors()
    extra_dimmers = climate_map.get_extra_dimmers()
    thermostat_cfg = apply_to_config(config)

    manager = DeviceManager(config)

    auth_cfg = (config.raw.get("web") or {}).get("auth")
    if auth_cfg:
        register_auth(app, config.resolve(auth_cfg["secrets_file"]))

    positions_path = config.resolve(config.raw.get("positions_file", "positions.json"))
    positions = PositionStore(positions_path)

    history_cfg = config.raw.get("history", {})
    db_path = config.resolve(history_cfg.get("db_file", "data/history.db"))
    store = HistoryStore(db_path)

    energy_cfg = config.raw.get("energy") or {}
    power_budget = (
        PowerBudget(contract_limit_w=energy_cfg["contract_limit_w"])
        if energy_cfg.get("contract_limit_w")
        else None
    )
    power_sensor = energy_cfg.get("sensor")

    history_poll_interval = history_cfg.get("poll_interval_seconds", 30)
    recorder = HistoryRecorder(
        manager,
        store,
        poll_interval_seconds=history_poll_interval,
        power_budget=power_budget,
        power_sensor=power_sensor,
        retention_days=history_cfg.get("retention_days", 30),
    )

    thermostat = None
    interlock = None
    interlock_followers: dict[str, str] = {}
    mode_switch_names: set[str] = set()
    # thermostat_cfg (from apply_to_config() above) already has the
    # panel's saved zones whole-block-replacing config.yaml's own, if
    # any were saved - see that function's docstring for why "whole
    # block", not a merge: two independent zone/valve configs fighting
    # over the same physical valve is a worse failure mode than "one
    # clearly wins". mode/hysteresis_c/interlocks/mode_switches/
    # schedules_file still come from config.yaml either way, and every
    # .get() below already has a default, so a config.yaml with no
    # thermostat: key at all still works once the panel has a saved
    # mapping (the from-scratch-apartment case).
    if thermostat_cfg:
        schedules_path = config.resolve(thermostat_cfg.get("schedules_file", "schedules.json"))
        schedule_store = ScheduleStore(schedules_path, default_mode=thermostat_cfg.get("mode", "heat"))
        zones = [
            Zone(name=z["name"], sensor=z["sensor"], valve=z["valve"], cool_enabled=z.get("cool_enabled", True))
            for z in thermostat_cfg.get("zones", [])
        ]
        thermostat = ThermostatController(
            manager,
            schedule_store,
            zones,
            hysteresis_c=thermostat_cfg.get("hysteresis_c", 0.3),
            poll_interval_seconds=thermostat_cfg.get("poll_interval_seconds", 60),
            sensor_failsafe_hours=thermostat_cfg.get("sensor_failsafe_hours", 24.0),
        )
        # The target each zone's valve was actually driven against, recorded
        # next to that zone's temperature so the dashboard chart can show
        # *why* the valve opened - see ThermostatController.tick().
        recorder.extra_samples = lambda: {
            zone.sensor: {"target_c": target}
            for zone in thermostat.zones
            if (target := thermostat.applied_target(zone.name)) is not None
        }

        interlocks = [
            Interlock(leader=link["leader"], follower=link["follower"], active_in_mode=link.get("active_in_mode"))
            for link in thermostat_cfg.get("interlocks", [])
        ]
        mode_switches = [
            ModeSwitch(switch=item["switch"], active_in_mode=item["active_in_mode"])
            for item in thermostat_cfg.get("mode_switches", [])
        ]
        mode_switch_names = {ms.switch for ms in mode_switches}
        if interlocks or mode_switches:
            interlock = InterlockController(
                manager,
                interlocks,
                mode_switches,
                mode_provider=schedule_store.get_mode,
                diagnostic_mode_provider=schedule_store.get_diagnostic_mode,
                poll_interval_seconds=thermostat_cfg.get("poll_interval_seconds", 60),
            )
            interlock_followers = {link.leader: link.follower for link in interlocks}

    manager.start()
    recorder.start()
    if thermostat:
        thermostat.start()
    if interlock:
        interlock.start()

    app.config["LOCALHOME_CONFIG"] = config
    app.config["TRANSLATIONS"] = load_translations(config.web_language)
    app.config["MANAGER"] = manager
    app.config["POSITIONS"] = positions
    app.config["HISTORY"] = store
    app.config["HISTORY_POLL_INTERVAL"] = history_poll_interval
    app.config["RECORDER"] = recorder
    app.config["POWER_BUDGET"] = power_budget
    app.config["POWER_SENSOR"] = power_sensor
    app.config["THERMOSTAT"] = thermostat
    app.config["INTERLOCK"] = interlock
    app.config["INTERLOCK_FOLLOWERS"] = interlock_followers
    app.config["MODE_SWITCH_NAMES"] = mode_switch_names
    app.config["CLIMATE_MAP"] = climate_map
    app.config["MANUAL_OVERRIDE"] = manual_override
    # A snapshot of exactly what was loaded to build the integrations
    # above, at this process's startup - compared against a fresh read
    # of climate_map.json on every GET /api/climate-admin/map so the
    # panel can tell a technician the truth about whether a save has
    # actually taken effect yet, instead of the frontend trying to
    # guess it from driver names (which can't tell "shelly" apart from
    # "simulated" once a zone is already panel-managed, since both keep
    # the exact same "<zone> Sensor"/"<zone> Valve" names either way).
    app.config["ACTIVE_CLIMATE_ZONES"] = panel_zones
    app.config["ACTIVE_EXTRA_SWITCHES"] = extra_switches
    app.config["ACTIVE_EXTRA_SENSORS"] = extra_sensors
    app.config["ACTIVE_EXTRA_DIMMERS"] = extra_dimmers

    _register_routes(app)
    return app


def _set_roles(roles: dict) -> dict:
    """Only the roles that have a device id - a saved `{"x": null}` and
    nothing saved at all build the same drivers, so a cleared field isn't
    a pending change."""
    return {key: value for key, value in (roles or {}).items() if value}


def _register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        manager: DeviceManager = app.config["MANAGER"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        return render_template(
            "index.html",
            cover_names=list(manager.covers),
            t=app.config["TRANSLATIONS"],
            lang=config.web_language,
        )

    @app.route("/api/server-time")
    def api_server_time():
        # The clock both pages show - the server's, not the browser's, so
        # a wrongly-set board timezone is visible instead of silently
        # shifting every schedule slot. See services/system_clock.py.
        return jsonify(ok=True, **server_time())

    # The history table scan behind these numbers is the one slow part of
    # a system-status request; its growth only matters over hours.
    db_stats_cache = {"ts": 0.0, "value": None}
    DB_STATS_MAX_AGE_SECONDS = 600

    @app.route("/api/system-status")
    def api_system_status():
        # What the admin panel's system-status section shows: anything
        # that failed to load and why, which devices aren't reporting,
        # whether the background loops are still cycling, and the
        # process/disk/database numbers that tell a slow leak or a disk
        # filling up apart from normal - see services/system_status.py.
        manager: DeviceManager = app.config["MANAGER"]
        store: HistoryStore = app.config["HISTORY"]
        recorder: HistoryRecorder = app.config["RECORDER"]
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]

        now = time.time()
        if db_stats_cache["value"] is None or now - db_stats_cache["ts"] > DB_STATS_MAX_AGE_SECONDS:
            try:
                db_stats_cache["value"] = store.stats()
            except Exception:
                logger.exception("Failed to read history database stats")
                db_stats_cache["value"] = {}
            db_stats_cache["ts"] = now
        database = {
            **db_stats_cache["value"],
            "path": str(store.db_path),
            "retention_days": recorder.retention_days,
            "stats_age_seconds": round(now - db_stats_cache["ts"]),
        }

        loops = [system_status.loop_health("history", recorder.last_write_ok_ts, recorder.poll_interval_seconds, now)]
        if thermostat is not None:
            loops.append(
                system_status.loop_health("thermostat", thermostat.last_tick_ok_ts, thermostat.poll_interval_seconds, now)
            )
        devices = system_status.device_health(manager)
        failed = manager.failed_integrations
        skipped = skipped_driver_modules()
        disk = system_status.disk_stats(os.path.dirname(os.path.abspath(store.db_path)))
        return jsonify(
            ok=True,
            problems=system_status.problems(devices, failed, disk, loops),
            failed_integrations=failed,
            skipped_driver_modules=skipped,
            devices=devices,
            loops=loops,
            process=system_status.process_stats(),
            host=system_status.host_stats(),
            disk=disk,
            database=database,
            config_path=str(config.path),
        )

    @app.route("/api/devices")
    def api_devices():
        manager: DeviceManager = app.config["MANAGER"]
        power_budget: PowerBudget | None = app.config["POWER_BUDGET"]
        power_sensor = app.config["POWER_SENSOR"]
        interlock_followers: dict[str, str] = app.config["INTERLOCK_FOLLOWERS"]
        mode_switch_names: set[str] = app.config["MODE_SWITCH_NAMES"]

        sensors = []
        for name in manager.pollers:
            entry = {
                "name": name,
                "kind": manager.sensor_kinds.get(name, "unknown"),
                # A mode_switch has no owner of its own - it's forced by
                # the thermostat's mode alone (see services/interlock.py)
                # - so a manual toggle would just get overridden on the
                # next tick. Show its state, but don't offer a control
                # that wouldn't actually do anything lasting.
                "controllable": (name in manager.switches or name in manager.numbers) and name not in mode_switch_names,
                "tab": manager.dashboard_tab.get(name, "home"),
                "visible_in_mode": manager.visible_in_mode.get(name),
            }
            if name in interlock_followers:
                entry["linked_valve"] = interlock_followers[name]
            entry.update(manager.ui_options.get(name, {}))
            if power_budget is not None and name == power_sensor:
                entry["power_budget"] = {
                    "contract_limit_w": power_budget.contract_limit_w,
                    "available_power_w": power_budget.available_power_w,
                    "trip_risk_w": power_budget.trip_risk_w,
                }
            number = manager.numbers.get(name)
            if number is not None:
                entry["range"] = {"min": number.min_value, "max": number.max_value, "unit": number.unit}
                paired_switch = manager.paired_switch.get(name)
                if paired_switch in manager.switches:
                    entry["paired_switch"] = paired_switch
            sensors.append(entry)

        return jsonify(covers=list(manager.covers), sensors=sensors)

    @app.route("/api/covers/<name>/status")
    def api_cover_status(name):
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]
        cover = manager.covers.get(name)
        if cover is None:
            return jsonify(ok=False, error=f"unknown cover '{name}'"), 404
        try:
            status = cover.status()
            positions.observe_state(cover, status["state"])
            return jsonify(
                ok=True,
                state=status["state"],
                percent=positions.get(cover.id),
                calibrated=positions.is_calibrated(cover.id),
            )
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502

    @app.route("/api/covers/<name>/action/<action>", methods=["POST"])
    def api_cover_action(name, action):
        if action not in ("open", "close", "stop"):
            return jsonify(ok=False, error="invalid action"), 400
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]

        if name.lower() == "all":
            targets = list(manager.covers.values())
        else:
            cover = manager.covers.get(name)
            if cover is None:
                return jsonify(ok=False, error=f"unknown cover '{name}'"), 404
            targets = [cover]

        for cover in targets:
            positions.cancel_pending_move(cover.id)
            positions.mark_own_command(cover.id)

        try:
            for cover in targets:
                cover.send(action)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502

        if action in ("open", "close"):
            for cover in targets:
                positions.set(cover.id, 100 if action == "open" else 0)

        return jsonify(ok=True)

    @app.route("/api/covers/<name>/position", methods=["POST"])
    def api_cover_position(name):
        data = request.get_json(silent=True) or {}
        if "percent" not in data:
            return jsonify(ok=False, error="missing 'percent' parameter"), 400
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]
        cover = manager.covers.get(name)
        if cover is None:
            return jsonify(ok=False, error=f"unknown cover '{name}'"), 404
        try:
            result = positions.move_to_percent(cover, data["percent"])
            return jsonify(ok=True, **result)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502

    @app.route("/api/sensors/<name>")
    def api_sensor(name):
        manager: DeviceManager = app.config["MANAGER"]
        return jsonify(manager.sensor_reading(name))

    @app.route("/api/sensors/<name>/<action>", methods=["POST"])
    def api_sensor_action(name, action):
        if action not in ("on", "off"):
            return jsonify(ok=False, error="invalid action"), 400
        manager: DeviceManager = app.config["MANAGER"]
        switch = manager.switches.get(name)
        if switch is None:
            return jsonify(ok=False, error=f"'{name}' is not a controllable switch"), 404
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        diagnostic_mode = bool(thermostat and thermostat.schedule_store.get_diagnostic_mode())
        if name in app.config["MODE_SWITCH_NAMES"] and not diagnostic_mode:
            return jsonify(ok=False, error=f"'{name}' is controlled automatically by the thermostat mode"), 409
        try:
            switch.turn_on() if action == "on" else switch.turn_off()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        manager.pollers[name].cache.merge({"is_on": action == "on"})
        return jsonify(ok=True)

    @app.route("/api/sensors/<name>/set-value", methods=["POST"])
    def api_sensor_set_value(name):
        data = request.get_json(silent=True) or {}
        if "value" not in data:
            return jsonify(ok=False, error="missing 'value' parameter"), 400
        manager: DeviceManager = app.config["MANAGER"]
        number = manager.numbers.get(name)
        if number is None:
            return jsonify(ok=False, error=f"'{name}' is not a settable number"), 404
        try:
            number.set_value(data["value"])
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        manager.pollers[name].cache.merge({"value": data["value"]})
        return jsonify(ok=True)

    @app.route("/api/sensors/<name>/history")
    def api_sensor_history(name):
        store: HistoryStore = app.config["HISTORY"]
        fields = request.args.getlist("field")
        range_key = request.args.get("range", "24h")
        if not fields:
            return jsonify(ok=False, error="missing 'field' parameter"), 400
        if range_key not in RANGE_BUCKETS:
            return jsonify(ok=False, error="invalid range"), 400
        return jsonify(ok=True, series={field: store.query(name, field, range_key) for field in fields})

    @app.route("/api/sensors/<name>/peak-today")
    def api_sensor_peak(name):
        store: HistoryStore = app.config["HISTORY"]
        field = request.args.get("field")
        if not field:
            return jsonify(ok=False, error="missing 'field' parameter"), 400
        return jsonify(ok=True, **store.query_peak_today(name, field))

    @app.route("/api/climate/zones")
    def api_climate_zones():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=True, mode=None, zones=[])
        manager: DeviceManager = app.config["MANAGER"]
        now = datetime.now()
        zones = []
        for zone in thermostat.zones:
            sensor_reading = manager.sensor_reading(zone.sensor)
            valve_reading = manager.sensor_reading(zone.valve)
            temperature_c = sensor_reading.get("temperature_c")
            humidity_pct = sensor_reading.get("humidity_pct")
            # humidity_pct == 0 is a broken/uncalibrated reading, not
            # "bone dry air" - math.log(0) would raise anyway, so this
            # also protects the endpoint from a single bad sensor value
            # taking the whole response down.
            dew_point = dew_point_c(temperature_c, humidity_pct) if temperature_c is not None and humidity_pct else None
            zones.append({
                "name": zone.name,
                "sensor": zone.sensor,
                "valve": zone.valve,
                "cool_enabled": zone.cool_enabled,
                "temperature_c": temperature_c,
                "humidity_pct": humidity_pct,
                "dew_point_c": dew_point,
                "battery_pct": sensor_reading.get("battery_pct"),
                "wifi_rssi_dbm": sensor_reading.get("wifi_rssi_dbm"),
                "sensor_ok": sensor_reading.get("ok", False),
                "sensor_seconds_since_update": sensor_reading.get("seconds_since_update"),
                "sensor_error": sensor_reading.get("error"),
                # Set only for the "every MQTT topic went stale, but at
                # least one of them had a last-known value" fallback
                # (drivers/mqtt/sensor.py) - lets the dashboard show
                # "last known: X, stale for Yh" instead of blanking a
                # reading it still actually has.
                "sensor_last_value_age_seconds": sensor_reading.get("last_value_age_seconds"),
                "valve_on": valve_reading.get("is_on", False),
                "valve_ok": valve_reading.get("ok", False),
                "valve_seconds_since_update": valve_reading.get("seconds_since_update"),
                "valve_error": valve_reading.get("error"),
                "target_c": thermostat.schedule_store.get_target(zone.name, now),
                "sensor_failsafe_active": thermostat.is_failsafe_active(zone.name),
                "sensor_unreachable_hours": thermostat.unreachable_hours(zone.name),
                "sensor_failsafe_hours": thermostat.sensor_failsafe_hours,
            })
        away_until = thermostat.schedule_store.get_away_until()
        return jsonify(
            ok=True,
            mode=thermostat.schedule_store.get_mode(),
            away_until=away_until.isoformat() if away_until else None,
            diagnostic_mode=thermostat.schedule_store.get_diagnostic_mode(),
            # For the chart's band around the target: the valve only
            # changes state once the temperature leaves target ± this.
            hysteresis_c=thermostat.hysteresis_c,
            zones=zones,
        )

    @app.route("/api/climate/mode", methods=["POST"])
    def api_climate_set_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        if mode not in ("heat", "cool"):
            return jsonify(ok=False, error="mode must be 'heat' or 'cool'"), 400
        thermostat.schedule_store.set_mode(mode)
        # Re-evaluate right away instead of leaving valves showing stale
        # state until the background loop's next scheduled tick - a mode
        # switch changes every zone's heat/cool logic at once.
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/away", methods=["POST"])
    def api_climate_set_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        data = request.get_json(silent=True) or {}
        hours = data.get("hours")
        if not isinstance(hours, (int, float)) or hours <= 0:
            return jsonify(ok=False, error="hours must be a positive number"), 400
        until = datetime.now() + timedelta(hours=hours)
        thermostat.schedule_store.set_away_until(until)
        thermostat.tick()
        return jsonify(ok=True, away_until=until.isoformat())

    @app.route("/api/climate/away/cancel", methods=["POST"])
    def api_climate_cancel_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        thermostat.schedule_store.set_away_until(None)
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/diagnostic-mode", methods=["POST"])
    def api_climate_set_diagnostic_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify(ok=False, error="'enabled' must be true or false"), 400
        thermostat.schedule_store.set_diagnostic_mode(enabled)
        if not enabled:
            # Resume real control immediately, same "don't wait for the
            # next scheduled tick" idiom as every other setter here -
            # otherwise the panel would show every valve frozen in
            # whatever the installer last left it at for up to a full
            # poll_interval_seconds after switching back to normal.
            # forget_valve_state() + resync_after_diagnostic_mode() first:
            # a manual toggle made while diagnostic mode was on wouldn't
            # otherwise be noticed by _apply()'s "already matches"
            # shortcut, and a zone whose sensor is currently unreachable
            # wouldn't be revisited by tick() at all (it skips such a
            # zone outright) - see both methods' docstrings.
            thermostat.forget_valve_state()
            thermostat.resync_after_diagnostic_mode()
            thermostat.tick()
            interlock: InterlockController | None = app.config["INTERLOCK"]
            if interlock:
                interlock.tick()
        return jsonify(ok=True, enabled=enabled)

    # Each season has its own grid (services/thermostat.py's SEASON_KEYS);
    # `mode` picks which one, defaulting to the current season so a
    # client that predates the split still reads/writes the live grid.
    @app.route("/api/climate/zones/<name>/schedule")
    def api_climate_get_schedule(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        mode = request.args.get("mode") or thermostat.schedule_store.get_mode()
        if mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        return jsonify(ok=True, mode=mode, week=thermostat.schedule_store.get_week(name, mode))

    @app.route("/api/climate/zones/<name>/schedule", methods=["POST"])
    def api_climate_set_schedule(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        data = request.get_json(silent=True) or {}
        week = data.get("week")
        if not isinstance(week, dict):
            return jsonify(ok=False, error="missing 'week'"), 400
        mode = data.get("mode")
        if mode is not None and mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        try:
            thermostat.schedule_store.set_week(name, week, mode)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        # Same reasoning as the mode switch above: don't leave the valve
        # (and the status label derived from it) stale until the next
        # scheduled tick just because the new target was written to disk.
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/zones/<name>/on-hours")
    def api_climate_on_hours(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=False, error="no thermostat configured"), 404
        zone = next((z for z in thermostat.zones if z.name == name), None)
        if zone is None:
            return jsonify(ok=False, error=f"unknown zone '{name}'"), 404
        store: HistoryStore = app.config["HISTORY"]
        interval = app.config["HISTORY_POLL_INTERVAL"]
        return jsonify(
            ok=True,
            today=round(store.sum_on_hours_range(zone.valve, "is_on", "today", interval), 2),
            week=round(store.sum_on_hours_range(zone.valve, "is_on", "week", interval), 2),
            month=round(store.sum_on_hours_range(zone.valve, "is_on", "month", interval), 2),
        )

    # ----------------------------------------------- climate admin panel ----
    # first-home-model: lets an installer map the fixed two-zone thermostat
    # onto this apartment's own Shelly device ids, and debug the control
    # logic with a manually-typed temperature - see
    # services/climate_map_store.py / services/manual_override.py.

    @app.route("/climate-admin")
    def climate_admin_page():
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        return render_template(
            "climate_admin.html",
            t=app.config["TRANSLATIONS"],
            lang=config.web_language,
        )

    @app.route("/api/climate-admin/broker-status")
    def api_climate_admin_broker_status():
        # A raw TCP probe, not a real MQTT client - answers "is anything
        # even listening" so a device stuck on "not reachable yet" isn't
        # misdiagnosed as its own fault when the broker itself is down
        # (a real incident this panel is meant to catch immediately - see
        # services/mqtt_broker_status.py).
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        broker_file = config.resolve("mqtt_broker.json")
        return jsonify(ok=True, **check_broker(broker_file))

    @app.route("/api/climate-admin/broker")
    def api_climate_admin_get_broker():
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        broker_file = config.resolve("mqtt_broker.json")
        return jsonify(ok=True, **get_broker(broker_file))

    @app.route("/api/climate-admin/broker", methods=["POST"])
    def api_climate_admin_set_broker():
        # Doing a full install from a tablet means never needing to open
        # mqtt_broker.json by hand - see services/mqtt_broker_config.py.
        # Same "changes need a restart" contract as the zone mapping
        # below: DeviceManager only ever builds its MQTT client once, at
        # process start.
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        broker_file = config.resolve("mqtt_broker.json")
        data = request.get_json(silent=True) or {}
        try:
            set_broker(
                broker_file,
                data.get("host"),
                data.get("port"),
                data.get("username"),
                data.get("password"),
                bool(data.get("change_password")),
            )
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/broker/test", methods=["POST"])
    def api_climate_admin_test_broker():
        # Tests whatever the operator just typed, before it's saved -
        # same probe as broker-status above (services/mqtt_broker_status.py),
        # just against unsaved values instead of the file on disk.
        data = request.get_json(silent=True) or {}
        host, port = data.get("host"), data.get("port")
        if not isinstance(host, str) or not host.strip() or not isinstance(port, int) or isinstance(port, bool):
            return jsonify(ok=False, error="host and port are required"), 400
        return jsonify(ok=True, **check_host_port(host.strip(), port))

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
            return jsonify(ok=True, configured=True, zones=saved, restart_pending=restart_pending)
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
        data = request.get_json(silent=True) or {}
        zones = data.get("zones")
        if not isinstance(zones, list):
            return jsonify(ok=False, error="missing 'zones'"), 400
        previous = climate_map.get_zones()
        try:
            climate_map.set_zones(zones)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
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
        return jsonify(ok=True, restart_pending=restart_pending, **{key: saved.get(key) for key in EXTRA_SWITCH_FIELDS})

    @app.route("/api/climate-admin/extra-switches", methods=["POST"])
    def api_climate_admin_set_extra_switches():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = request.get_json(silent=True) or {}
        extra_switches = {key: data.get(key) for key in EXTRA_SWITCH_FIELDS}
        try:
            climate_map.set_extra_switches(extra_switches)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/extra-sensors")
    def api_climate_admin_get_extra_sensors():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        saved = climate_map.get_extra_sensors()
        restart_pending = _set_roles(saved) != _set_roles(app.config["ACTIVE_EXTRA_SENSORS"])
        return jsonify(ok=True, restart_pending=restart_pending, **{key: saved.get(key) for key in EXTRA_SENSOR_FIELDS})

    @app.route("/api/climate-admin/extra-sensors", methods=["POST"])
    def api_climate_admin_set_extra_sensors():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = request.get_json(silent=True) or {}
        extra_sensors = {key: data.get(key) for key in EXTRA_SENSOR_FIELDS}
        try:
            climate_map.set_extra_sensors(extra_sensors)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/extra-dimmers")
    def api_climate_admin_get_extra_dimmers():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        saved = climate_map.get_extra_dimmers()
        restart_pending = _set_roles(saved) != _set_roles(app.config["ACTIVE_EXTRA_DIMMERS"])
        # Until the panel manages the VMC, offer the id of a dimmer already
        # hand-wired in config.yaml, so adopting it is one click, not
        # re-typing an id that's already known.
        suggested = None
        if not saved.get("vmc_device_id"):
            for entry in config.raw.get("integrations") or []:
                if entry.get("type") == "shelly_light" and entry.get("kind") == "switch" and entry.get("device_id"):
                    suggested = {"device_id": entry["device_id"], "name": entry.get("name")}
                    break
        return jsonify(
            ok=True, restart_pending=restart_pending, suggested_vmc=suggested,
            **{key: saved.get(key) for key in EXTRA_DIMMER_FIELDS},
        )

    @app.route("/api/climate-admin/extra-dimmers", methods=["POST"])
    def api_climate_admin_set_extra_dimmers():
        climate_map: ClimateMapStore = app.config["CLIMATE_MAP"]
        data = request.get_json(silent=True) or {}
        extra_dimmers = {key: data.get(key) for key in EXTRA_DIMMER_FIELDS}
        try:
            climate_map.set_extra_dimmers(extra_dimmers)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, restart_required=True)

    @app.route("/api/climate-admin/restart", methods=["POST"])
    def api_climate_admin_restart():
        # Fires after this response is sent - see services/self_restart.py.
        # Every "restart_required: true" above meant "go do this by
        # hand, from a terminal" until now - except on Windows, where
        # the underlying os.execv is emulated as spawn-then-exit and
        # reliably leaves two processes fighting over one port instead
        # of actually restarting anything. Refuse there instead of
        # leaving the operator with a page that never comes back.
        if not is_supported():
            return jsonify(
                ok=False,
                platform_unsupported=True,
                error="Restarting from the browser isn't reliable on Windows "
                "(it can leave two processes bound to the same port instead "
                "of actually restarting) - stop the process yourself "
                "(Ctrl+C in its terminal) and start it again.",
            ), 501
        schedule_restart()
        return jsonify(ok=True)

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
            return jsonify(ok=False, error=f"zone '{name}' is not currently configured in manual mode"), 409
        # Diagnostic mode makes the thermostat.tick() below a no-op (see
        # ScheduleStore.get_diagnostic_mode()), so setting a value here
        # would silently do nothing to the valve - confusing during the
        # exact "does the control logic actually react" check this quick-set
        # box exists for. Same "reject, don't silently no-op" stance as the
        # mode_switch block in api_sensor_action() below.
        if thermostat and thermostat.schedule_store.get_diagnostic_mode():
            return jsonify(ok=False, error="diagnostic mode is active - turn it off to let the thermostat react to a manual value"), 409

        data = request.get_json(silent=True) or {}
        if "temperature_c" not in data:
            return jsonify(ok=False, error="missing 'temperature_c'"), 400
        try:
            temperature_c = float(data["temperature_c"])
            humidity_pct = float(data["humidity_pct"]) if data.get("humidity_pct") is not None else None
        except (TypeError, ValueError):
            return jsonify(ok=False, error="temperature_c/humidity_pct must be numbers"), 400

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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    config = app.config["LOCALHOME_CONFIG"]
    app.run(host=config.web_host, port=config.web_port, debug=False)


if __name__ == "__main__":
    main()
