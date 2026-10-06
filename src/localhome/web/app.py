"""Flask application factory.

Routes are generic across device kinds - this module never imports a
specific brand's driver; everything comes from the DeviceManager built
out of config.yaml. Adding a new brand of power meter, climate sensor or
switch never requires touching this file; adding a wholly new *kind* of
device (not cover/power_meter/climate/switch) does - see
docs/adding-a-driver.md.
"""
from __future__ import annotations

import io
import logging
import math
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from localhome.config import LocalHomeConfig, load_config
from localhome.core.manager import DeviceManager
from localhome.core.registry import skipped_driver_modules
from localhome.services.climate_map_store import (
    BYPASS_NORMALLY_OPEN_KEY,
    DEHUMIDIFIER_ZONE_KEY,
    EXTRA_DIMMER_FIELDS,
    EXTRA_SENSOR_FIELDS,
    EXTRA_SWITCH_FIELDS,
    OUTDOOR_MODE_KEY,
    VMC_MODE_KEY,
    SEASON_MODES,
    SEASON_RULE_DEFAULTS,
    ClimateMapStore,
    apply_to_config,
    configured_rule_switches,
    dehumidifier_water_zone,
    extra_roles,
    panel_season_rules,
    snapshot_from_config,
)
from localhome.services.dehumidifier import DehumidifierSettingsStore, DehumidifierTimer
from localhome.services.dewpoint import dew_point_c
from localhome.services.history import HistoryRecorder, HistoryStore, PowerBudget, RANGE_BUCKETS
from localhome.services import diagnostics, led_matrix, panel_settings
from localhome.services.maintenance import Maintenance
from localhome.services.manual_override import ManualOverrideStore
from localhome.services.mqtt_broker_config import get_broker
from localhome.services.mqtt_broker_status import BrokerStatus
from localhome.drivers.mqtt import base as mqtt_base, client as mqtt_client
from localhome.drivers.mqtt.rpc import ShellyRpcClient
from localhome.drivers.simulated import world as simulated_world
from localhome.services.position_control import PositionStore
from localhome.services.interlock import (
    SEASON_ACTIONS,
    Interlock,
    InterlockController,
    ModeLimitedSwitch,
    ModeSwitch,
    SeasonRule,
    season_rules_of,
)
from localhome.services.self_restart import is_supported, schedule_restart
from localhome.services.service_watchdog import ServiceWatchdog
from localhome.services import shelly_health
from localhome.services.shelly_check import ShellyCheck
from localhome.services import system_status
from localhome.services.system_clock import server_time
from localhome.services.thermostat import MODES, ScheduleStore, ThermostatController, Zone
from localhome.errors import InputError
from localhome.services import vmc_speeds
from localhome.services.energy import EnergyLedger, summary as energy_summary
from localhome.services.vmc_schedule import VmcScheduleController, VmcScheduleStore
from localhome.web.auth import register_auth
from localhome.web.i18n import load_translations

logger = logging.getLogger(__name__)


def _asset_version() -> str:
    """Changes whenever the dashboard's own files do (an update copied to
    the board): the newest modification time among static files and
    templates. Appended to every static URL, so no browser keeps an old
    copy, and reported by /api/server-time, so an already-open page can
    tell it's out of date."""
    base = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for folder in ("static", "templates", "locales"):
        for root, _, files in os.walk(os.path.join(base, folder)):
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    pass
    return str(int(newest))


def _software_version() -> dict:
    """The installed code's commit and date, when it's a git checkout (the
    usual install) - so it's visible whether the board runs the latest."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h %cI"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "date": None}
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 2:
        return {"commit": None, "date": None}
    return {"commit": parts[0], "date": parts[1]}


def create_app(config_path: str | None = None) -> Flask:
    app = Flask(__name__)
    asset_version = _asset_version()
    app.config["SOFTWARE_VERSION"] = _software_version()
    app.config["ASSET_VERSION"] = asset_version

    @app.url_defaults
    def _version_static_urls(endpoint, values):
        if endpoint == "static" and "filename" in values:
            values.setdefault("v", asset_version)

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
        return jsonify(ok=False, error="internal error - see server logs", code="internal_error", params={}), 500

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
    # What the outdoor sensor and the VMC run as in this process - compared
    # with a fresh read to tell the panel whether a save needs a restart.
    active_extra_roles = extra_roles(config, climate_map)
    thermostat_cfg = apply_to_config(config)
    # Settings changed from the admin panel (thermostat tuning, history
    # limits, language) override config.yaml's - see services/panel_settings.py.
    settings_store = panel_settings.PanelSettingsStore(config.resolve("panel_settings.json"))
    active_settings = settings_store.get()
    thermostat_cfg = panel_settings.apply(config, active_settings, thermostat_cfg)

    manager = DeviceManager(config)

    def _panel_rules() -> list[tuple[SeasonRule, dict]]:
        """The admin panel's rules for its extra switches, read fresh - an
        edit there applies on the next tick, no restart."""
        rows = panel_season_rules(extra_switches, climate_map.get_season_rules(), thermostat_cfg)
        return [(SeasonRule(r["switch"], {m: r[m] for m in SEASON_MODES}), r) for r in rows]

    def season_rules() -> list[dict]:
        """Every switch that follows the season, whatever form its rule was
        written in: [{"switch", "heat", "cool", "source", "normally_open"}]
        - "on" is the Shelly on, which closes a normally open valve."""
        rows = [{"switch": r.switch, **{m: r.action(m) for m in SEASON_MODES}, "source": "config"}
                for r in static_rules]
        rows += [{"switch": r.switch, **{m: r.action(m) for m in SEASON_MODES}, "source": "panel", "role": row["role"]}
                 for r, row in _panel_rules()]
        for row in rows:
            row["normally_open"] = bool(manager.ui_options.get(row["switch"], {}).get("normally_open"))
        return rows

    def season_links() -> list[dict]:
        """What one switch opens with it - a dehumidifier the valve it draws
        its water through: [{"leader", "follower", "zone", "mode"}]."""
        zone_of = {zone.valve: zone.name for zone in (thermostat.zones if thermostat else [])}
        return [{"leader": link.leader, "follower": link.follower, "zone": zone_of.get(link.follower),
                 "mode": link.active_in_mode} for link in interlock_links]

    def forced_by_season(name: str) -> bool:
        """Whether the current season turns this switch on or off by itself -
        a manual command would just be undone on the next tick."""
        if thermostat is None:
            return False
        mode = thermostat.schedule_store.get_mode()
        return any(r["switch"] == name and r[mode] != "manual" for r in season_rules())

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
    max_db_mb = history_cfg.get("max_db_mb", 300)
    history_ceiling = _history_ceiling_mb(store)
    if max_db_mb > history_ceiling:
        logger.warning("History: max_db_mb %s is more than this disk allows - using %d MB", max_db_mb, history_ceiling)
        max_db_mb = history_ceiling
    recorder = HistoryRecorder(
        manager,
        store,
        poll_interval_seconds=history_poll_interval,
        power_budget=power_budget,
        power_sensor=power_sensor,
        retention_days=history_cfg.get("retention_days", 30),
        max_db_bytes=int(max_db_mb * 1_000_000),
    )
    # No rows with a clock that isn't synchronized yet - see
    # HistoryRecorder.clock_ready. Where that can't be known (a PC), it
    # records as always.
    recorder.clock_ready = lambda: system_status.clock_synced_fast() is not False
    recorder.disk_stats = lambda: _history_disk(store)

    thermostat = None
    interlock = None
    interlock_followers: dict[str, str] = {}
    interlock_links: list[Interlock] = []
    # Every season rule, whatever it was written as - see season_rules() below.
    static_rules: list[SeasonRule] = []
    config_rules: list[SeasonRule] = []
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
            silence_file=config.resolve("sensor_silence.json"),
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
        mode_limited = [
            ModeLimitedSwitch(switch=item["switch"], only_in_mode=item["only_in_mode"])
            for item in thermostat_cfg.get("mode_limited_switches", [])
        ]
        # The general form: `season_rules: [{switch, heat, cool}]`, each
        # season "on", "off" or "manual".
        for item in thermostat_cfg.get("season_rules") or []:
            actions = {m: item.get(m, "manual") for m in SEASON_MODES}
            if not item.get("switch") or any(a not in SEASON_ACTIONS for a in actions.values()):
                logger.warning("thermostat.season_rules entry %r skipped: needs 'switch' and heat/cool "
                               "each one of %s", item, ", ".join(SEASON_ACTIONS))
                continue
            config_rules.append(SeasonRule(item["switch"], actions))
        static_rules = season_rules_of(mode_switches, mode_limited, SEASON_MODES) + config_rules
        # Always there with a thermostat: the admin panel's rules can appear
        # at any moment, and a tick with nothing to do costs nothing.
        interlock = InterlockController(
            manager,
            interlocks,
            mode_switches,
            mode_provider=schedule_store.get_mode,
            mode_limited=mode_limited,
            diagnostic_mode_provider=schedule_store.get_diagnostic_mode,
            poll_interval_seconds=thermostat_cfg.get("poll_interval_seconds", 60),
            season_rules_provider=lambda: config_rules + [rule for rule, _ in _panel_rules()],
        )
        interlock_followers = {link.leader: link.follower for link in interlocks}
        thermostat.interlock_owned_valves = {link.follower: link.active_in_mode for link in interlocks}
        interlock_links = interlocks

    # The ventilation unit's own day/night speed program (services/
    # vmc_schedule.py) - independent of the thermostat, and re-read from
    # its file every tick so the admin panel can change it without a
    # restart.
    vmc_schedule = VmcScheduleStore(config.resolve("vmc_schedule.json"))
    vmc = VmcScheduleController(manager, vmc_schedule)

    # What each Shelly says about itself - signal, temperature, restarts,
    # battery reports on time: see services/shelly_health.py. Listening
    # before the devices start, so a simulated sensor's first report
    # (sent on its first read) isn't missed.
    shelly_watch = _start_shelly_health(config, store, manager)

    # Each power meter's day, kept for good: its past days worked out from
    # the history first, then every recording cycle adds to today.
    recorder.energy = EnergyLedger(store)
    recorder.backfill_energy()
    manager.start()
    recorder.start()
    if thermostat:
        thermostat.start()
    if interlock:
        interlock.start()
    vmc.start()

    def command_switch(name: str, on: bool) -> None:
        """A switch on or off as the home page does it: its card says so at
        once, and a valve it opens with it follows now, not a minute later."""
        switch = manager.switches[name]
        switch.turn_on() if on else switch.turn_off()
        manager.pollers[name].cache.merge({"is_on": on})
        _wake_poller(manager, name)
        if interlock is not None and name in interlock_followers:
            interlock.tick()

    def dehumidifier_switch() -> str | None:
        """The admin panel's dehumidifier - or else, written in config.yaml,
        a season switch that opens a zone's valve for its water: what a
        radiant system's dehumidifier is."""
        panel = EXTRA_SWITCH_FIELDS["dehumidifier_device_id"]
        if panel in manager.switches:
            return panel
        leaders = {link.leader for link in interlock_links}
        return next((rule["switch"] for rule in season_rules()
                     if rule["switch"] in leaders and rule["switch"] in manager.switches), None)

    def dehumidifier_allowed(name: str) -> bool:
        """Switched by hand in this season - a summer device - and not in
        the diagnostic mode, where whoever installs it switches what they like."""
        if thermostat is None:
            return True
        if thermostat.schedule_store.get_diagnostic_mode():
            return False
        mode = thermostat.schedule_store.get_mode()
        rule = next((r for r in season_rules() if r["switch"] == name), None)
        return rule is None or rule[mode] == "manual"

    # The dehumidifier: on from the home page for a while or until the air
    # is dry enough - by itself only if the admin panel says so; off when
    # the time is up or the first cooled zone's air is dry enough. Its
    # thresholds and ways to switch it on are the panel's, read at every
    # check (services/dehumidifier.py).
    dehumidifier = None
    dehumidifier_settings = DehumidifierSettingsStore(config.resolve("dehumidifier_settings.json"))
    dehumidifier_name = dehumidifier_switch()
    if dehumidifier_name:
        cooled = [zone for zone in (thermostat.zones if thermostat else []) if zone.cool_enabled]
        dehumidifier = DehumidifierTimer(
            manager, dehumidifier_name, cooled[0].sensor if cooled else None,
            config.resolve("dehumidifier.json"), command=command_switch,
            allowed=lambda: dehumidifier_allowed(dehumidifier_name),
            humidity_zone=cooled[0].name if cooled else None, settings=dehumidifier_settings.get)
        dehumidifier.start()

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
    app.config["VMC_SCHEDULE"] = vmc
    app.config["DEHUMIDIFIER"] = dehumidifier
    app.config["DEHUMIDIFIER_SETTINGS"] = dehumidifier_settings
    # The VMC's night/day/boost speeds as set in the admin panel, laid over
    # its device's presets - see services/vmc_speeds.py.
    app.config["VMC_SPEEDS"] = vmc_speeds.VmcSpeedStore(config.resolve("vmc_speeds.json"))
    # What each cover's last read said - covers are read on demand, not
    # polled, so this is where the system status learns about them.
    app.config["COVER_READS"] = {}
    # Whether the Shellys still reach the board - see system_status.LanWatch.
    app.config["LAN_WATCH"] = system_status.LanWatch()
    # The eMMC's writes over the last hour - see system_status.WriteRate.
    app.config["WRITE_RATE"] = system_status.WriteRate()
    app.config["WRITE_RATE"].start()
    app.config["SHELLY_HEALTH"] = shelly_watch
    # Constant while the process lives, so it's read once, not per request.
    app.config["INTERLOCK_FOLLOWERS"] = interlock_followers
    app.config["INTERLOCK_LINKS"] = interlock_links
    app.config["SEASON_RULES"] = season_rules
    app.config["SEASON_LINKS"] = season_links
    app.config["FORCED_BY_SEASON"] = forced_by_season
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
    app.config["ACTIVE_EXTRA_ROLES"] = active_extra_roles
    app.config["PANEL_SETTINGS"] = settings_store
    app.config["ACTIVE_PANEL_SETTINGS"] = active_settings
    # What a factory reset removes: everything LocalHome itself wrote at
    # runtime - never config.yaml, credentials or the broker settings.
    runtime_files = [
        config.resolve("climate_map.json"),
        manual_override_path,
        config.resolve("mqtt_state_cache.json"),
        config.resolve("panel_settings.json"),
        positions_path,
    ]
    if thermostat is not None:
        runtime_files.append(thermostat.schedule_store.path)
    app.config["MAINTENANCE"] = Maintenance(
        str(config.path.parent),
        store,
        thermostat.schedule_store if thermostat is not None else None,
        [str(p) for p in runtime_files],
    )

    _register_routes(app)
    # The UNO Q's LED matrix: health at a glance for whoever is standing
    # in front of the box - off by itself on anything that isn't a UNO Q
    # (no arduino-router socket). See services/led_matrix.py.
    matrix = led_matrix.LedMatrixService(lambda: _matrix_state(app), ip_provider=_lan_ip,
                                         name_provider=lambda: app.config["SITE_NAME"]())
    app.config["LED_MATRIX"] = matrix
    matrix.start()
    return app


def _wake_poller(manager: DeviceManager, name: str) -> None:
    """The command went through: re-read that device now. For a paired
    switch/number (the VMC's on/off and speed) the partner too - one
    device, so both cards should come back together."""
    names = {name, manager.paired_switch.get(name)}
    names |= {number for number, switch in manager.paired_switch.items() if switch == name}
    for key in names - {None}:
        wake = getattr(manager.pollers.get(key), "wake", None)
        if wake is not None:
            wake()


def _valve_driver(manager: DeviceManager, link) -> dict | None:
    if link is None:
        return None
    reading = manager.sensor_reading(link.leader)
    return {"name": link.leader, "ok": bool(reading.get("ok")), "on": bool(reading.get("ok") and reading.get("is_on"))}


AWAY_MAX_HOURS = 24 * 366
# A zone's temporary target (see api_climate_set_override): what the
# dashboard offers - a few hours, or until the schedule's next change.
OVERRIDE_MAX_HOURS = 24
OVERRIDE_MIN_C, OVERRIDE_MAX_C = 5, 35


def _fail(code: str, message: str, status: int = 400, **params):
    """A refusal with a code the pages translate (errors.<code>) - the
    English message stays for logs and API callers. See localhome/errors.py."""
    return jsonify(ok=False, error=message, code=code, params=params), status


def _vmc_speed_device(manager: DeviceManager) -> str | None:
    """The VMC's speed: the level paired with a switch that has speed
    presets - the same one the home page's VMC card shows."""
    for name, switch in manager.paired_switch.items():
        if name in manager.numbers and (manager.ui_options.get(name) or {}).get("presets") and switch:
            return name
    return None


def _vmc_speeds_in_use(app: Flask, device: str | None) -> dict:
    manager: DeviceManager = app.config["MANAGER"]
    presets = (manager.ui_options.get(device) or {}).get("presets") or [] if device else []
    return vmc_speeds.of_presets(vmc_speeds.apply(presets, app.config["VMC_SPEEDS"].get()))


def _move_vmc_to_new_speed(manager: DeviceManager, device: str | None, moves: dict) -> None:
    """A unit running at an old speed goes to its new value - one that's
    off stays off (setting a speed would switch it on)."""
    switch = manager.paired_switch.get(device) if device else None
    if not device or (switch and manager.sensor_reading(switch).get("is_on") is not True):
        return
    current = manager.sensor_reading(device).get("value")
    target = moves.get(round(current)) if isinstance(current, (int, float)) else None
    if target is None or target == round(current):
        return
    try:
        manager.numbers[device].set_value(target)
    except Exception as exc:
        logger.warning("VMC speeds: couldn't move %s from %s%% to %s%% (%s)", device, current, target, exc)
        return
    manager.pollers[device].cache.merge({"value": target})
    _wake_poller(manager, device)


def _refusal(exc: Exception, status: int = 400):
    """A validation error as an answer: its code and parameters when it's
    an InputError, the plain message otherwise."""
    return jsonify(ok=False, error=str(exc), code=getattr(exc, "code", None),
                   params=getattr(exc, "params", None) or {}), status


def _json_body() -> dict:
    """The request's JSON body when it's an object, else an empty one. A
    body that parses but isn't an object (a bare number, a string) used to
    reach `.get()` in every POST route and come back as a 500."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _set_roles(roles: dict) -> dict:
    """Only the roles that have a device id - a saved `{"x": null}` and
    nothing saved at all build the same drivers, so a cleared field isn't
    a pending change."""
    return {key: value for key, value in (roles or {}).items() if value}


def _loops(app: Flask) -> list[dict]:
    recorder: HistoryRecorder = app.config["RECORDER"]
    thermostat: ThermostatController | None = app.config["THERMOSTAT"]
    now = time.monotonic()
    loops = [system_status.loop_health("history", recorder.last_write_ok_ts, recorder.poll_interval_seconds, now)]
    if thermostat is not None:
        loops.append(
            system_status.loop_health("thermostat", thermostat.last_tick_ok_ts, thermostat.poll_interval_seconds, now)
        )
    return loops


def _lan_signals(app: Flask, devices: list[dict], uptime_seconds: float | None) -> dict:
    """Whether the devices still reach the board - see
    system_status.LanWatch. From /proc and memory only: the LED matrix
    asks for it all day too."""
    manager: DeviceManager = app.config["MANAGER"]
    battery = {name for name, kind in manager.sensor_kinds.items()
               if kind == "climate" and manager.driver_types.get(name) == "mqtt_json"}
    return {
        "lan_silent_seconds": app.config["LAN_WATCH"].update(system_status.lan_mqtt_clients()),
        "battery_silent": system_status.battery_sensors_silent(devices, battery, uptime_seconds),
    }


def _start_shelly_health(config: LocalHomeConfig, store: HistoryStore,
                         manager: DeviceManager) -> shelly_health.ShellyHealth | None:
    """None without a Shelly to watch - real ones through the broker they
    speak to, simulated H&Ts through the simulated world."""
    devices = shelly_health.shelly_devices(config.integrations)
    simulated = shelly_health.simulated_devices(config.integrations)
    broker_file = config.resolve("mqtt_broker.json")
    broker = connection = None
    if devices and os.path.exists(broker_file):
        try:
            broker = mqtt_client.load_broker_config({"broker_file": broker_file})
            connection = mqtt_base.get_connection(broker)
        except Exception:
            logger.exception("Shelly health: no broker - the devices' own status isn't watched")
    if connection is None:
        devices = []
    if not devices and not simulated:
        return None

    def subscribe(topic, handler):
        if topic.startswith(shelly_health.SIMULATED_PREFIX):
            simulated_world.subscribe(topic[len(shelly_health.SIMULATED_PREFIX):], handler)
        else:
            connection.subscribe(topic, handler)

    health = shelly_health.ShellyHealth(devices + simulated, store, subscribe=subscribe,
                                        rpc_for=(lambda device_id: ShellyRpcClient(broker, device_id)) if broker else None,
                                        reading_of=manager.sensor_reading)
    health.start()
    return health


def _shelly_problems(app: Flask) -> list[dict]:
    health = app.config.get("SHELLY_HEALTH")
    return health.problems() if health else []


def _history_disk(store: HistoryStore) -> dict:
    return system_status.disk_stats(os.path.dirname(os.path.abspath(store.db_path)))


def _history_ceiling_mb(store: HistoryStore) -> int:
    """How big the history may grow on the disk it's on - see
    panel_settings.history_ceiling_mb."""
    try:
        size = os.path.getsize(store.db_path)
    except OSError:
        size = 0
    return panel_settings.history_ceiling_mb(_history_disk(store), size)


def _health(app: Flask) -> dict:
    """What the admin panel's system status is built from."""
    manager: DeviceManager = app.config["MANAGER"]
    store: HistoryStore = app.config["HISTORY"]
    loops = _loops(app)
    devices = system_status.device_health(manager) + system_status.cover_health(app.config["COVER_READS"])
    failed = manager.failed_integrations
    disk = _history_disk(store)
    host = system_status.host_stats()
    storage = system_status.storage_health(uptime_seconds=host.get("uptime_seconds"),
                                           recent=app.config["WRITE_RATE"].per_day())
    network = system_status.network_status()
    protections = system_status.board_protections()
    return {
        "devices": devices, "failed": failed, "disk": disk, "loops": loops, "host": host,
        "storage": storage, "network": network, "protections": protections,
        "problems": system_status.problems(devices, failed, disk, loops, storage, host.get("cpu_temp_c"), network, host,
                                           protections, **_lan_signals(app, devices, host.get("uptime_seconds")),
                                           shelly=_shelly_problems(app)),
    }


def _matrix_state(app: Flask) -> led_matrix.MatrixState:
    """What the LED matrix needs to know: the season, away/diagnostic,
    each zone's valve, and the problems as codes.

    From memory only - the devices' cached readings, the loops'
    timestamps, one statvfs, the MQTT client's own connection state -
    never the system status's subprocesses (nmcli, timedatectl) or a
    probe of the broker: this runs all day, every day, for a nicety."""
    thermostat: ThermostatController | None = app.config["THERMOSTAT"]
    zones = thermostat.zones if thermostat else []
    state = led_matrix.MatrixState(
        thermostat=thermostat is not None,
        problems=led_matrix.problem_codes(_quick_problems(app), zones,
                                          mqtt_client.broker_down_for() > led_matrix.BROKER_GRACE_SECONDS),
    )
    if thermostat is not None:
        store = thermostat.schedule_store
        state.mode = store.get_mode()
        state.away = store.get_away_until() is not None
        state.diagnostic = store.get_diagnostic_mode()
        manager: DeviceManager = app.config["MANAGER"]
        for zone in zones:
            reading = manager.sensor_reading(zone.valve)
            state.zones.append(bool(reading["is_on"]) if reading.get("ok") and "is_on" in reading else None)
            state.zone_names.append(zone.name)
    return state


def _quick_problems(app: Flask) -> list[dict]:
    """The system status's problems from memory, /proc and one statvfs -
    no subprocess, no broker probe: what the LED matrix and the
    dashboard's badge ask for all day."""
    manager: DeviceManager = app.config["MANAGER"]
    store: HistoryStore = app.config["HISTORY"]
    devices = system_status.device_health(manager) + system_status.cover_health(app.config["COVER_READS"])
    uptime = system_status._uptime_seconds()
    return system_status.problems(
        devices,
        manager.failed_integrations,
        system_status.disk_stats(os.path.dirname(os.path.abspath(store.db_path))),
        _loops(app),
        # "not heard yet" after a boot isn't a fault; an unsynchronized
        # clock is (CK) - both read without a subprocess.
        host={"uptime_seconds": uptime, "clock_synced": system_status.clock_synced_fast()},
        # RT: the devices can't reach the board - from /proc, no subprocess.
        **_lan_signals(app, devices, uptime),
        # Worked out every 5 minutes by the Shelly health check, kept in memory.
        shelly=_shelly_problems(app),
    )


def _lan_ip() -> str | None:
    """The address other machines reach this board at - the one the
    matrix scrolls at start-up. A UDP "connect" sends nothing; it only
    asks the kernel which interface would be used."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            ip = probe.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _register_routes(app: Flask) -> None:
    def site_name() -> str:
        """What this installation calls itself: the admin panel's setting,
        else config.yaml's, else the translated default. Read per page
        render (pages are rare; the API polls don't touch this), so a new
        name is there on the next reload without a restart."""
        settings: panel_settings.PanelSettingsStore = app.config["PANEL_SETTINGS"]
        chosen = settings.get().get("site_name")
        if chosen:
            return chosen
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        from_config = (config.raw.get("web") or {}).get("site_name")
        return from_config or app.config["TRANSLATIONS"]["app"]["title"]

    app.config["SITE_NAME"] = site_name

    @app.route("/site.webmanifest")
    def site_webmanifest():
        # Built here rather than served from static/, so the name under the
        # icon on a phone's home screen is the one set in the panel.
        name = site_name()
        return jsonify(
            name=name,
            short_name=name,
            start_url="/",
            scope="/",
            display="standalone",
            background_color="#0b0d13",
            theme_color="#0b0d13",
            icons=[
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            ],
        )

    @app.route("/")
    def home():
        # The home page - glance, tweak, plan; see static/home.js.
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        return render_template("home.html", site_name=site_name(), t=app.config["TRANSLATIONS"],
                               lang=config.web_language)

    @app.route("/api/server-time")
    def api_server_time():
        # The clock both pages show - the server's, not the browser's, so
        # a wrongly-set board timezone is visible instead of silently
        # shifting every schedule slot. See services/system_clock.py.
        # The board's start time goes out with the clock both pages already
        # poll: it's what tells a page that a days-old reading was simply
        # never heard (the board was off) rather than a sensor gone quiet.
        return jsonify(ok=True, app_version=app.config["ASSET_VERSION"],
                       clock_synced=system_status.clock_synced_fast(),
                       **system_status.boot_times(), **server_time())

    # The history table scan behind these numbers is the one slow part of
    # a system-status request; its growth only matters over hours.
    db_stats_cache = {"ts": 0.0, "value": None}
    DB_STATS_MAX_AGE_SECONDS = 600

    def _database_stats() -> dict:
        """The history database's size and contents, cached: the scan
        costs a full table read, and both the system status and the
        settings panel ask for it."""
        store: HistoryStore = app.config["HISTORY"]
        recorder: HistoryRecorder = app.config["RECORDER"]
        now = time.time()
        if db_stats_cache["value"] is None or now - db_stats_cache["ts"] > DB_STATS_MAX_AGE_SECONDS:
            try:
                db_stats_cache["value"] = store.stats()
            except Exception:
                logger.exception("Failed to read history database stats")
                db_stats_cache["value"] = {}
            db_stats_cache["ts"] = now
        return {
            **db_stats_cache["value"],
            "path": str(store.db_path),
            "retention_days": recorder.retention_days,
            "max_bytes": recorder.max_db_bytes,
            "paused_for_clock": recorder.paused_for_clock,
            "disk_hold_bytes": recorder.disk_hold_bytes,
            "disk_hold_since": recorder.disk_hold_since,
            "stats_age_seconds": round(now - db_stats_cache["ts"]),
        }

    @app.route("/api/system-status")
    def api_system_status():
        # What the admin panel's system-status section shows: anything
        # that failed to load and why, which devices aren't reporting,
        # whether the background loops are still cycling, and the
        # process/disk/database numbers that tell a slow leak or a disk
        # filling up apart from normal - see services/system_status.py.
        return jsonify(ok=True, **_system_status_payload())

    def _system_status_payload() -> dict:
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        health = _health(app)
        return dict(
            problems=health["problems"],
            boots=system_status.boot_history(),
            # What tune-board.sh's protections are on, and what the
            # watchdogs have done - LocalHome's own and the Wi-Fi one.
            protections=health["protections"],
            watchdog_history=system_status.watchdog_history(config.resolve("watchdog.log")),
            # What's over now but worth knowing: a safety timer that acted.
            recent=system_status.recent_events(config.resolve("watchdog.log")),
            software=app.config["SOFTWARE_VERSION"],
            network=health["network"],
            storage=health["storage"],
            failed_integrations=health["failed"],
            skipped_driver_modules=skipped_driver_modules(),
            devices=health["devices"],
            loops=health["loops"],
            process=system_status.process_stats(),
            host=health["host"],
            disk=health["disk"],
            database=_database_stats(),
            config_path=str(config.path),
        )

    @app.route("/api/problems")
    def api_problems():
        """What's wrong right now, cheaply - the dashboard's badge polls it.
        A device not heard from yet after a boot isn't counted."""
        found = [p for p in _quick_problems(app) if p["code"] != "device_not_heard_yet"]
        return jsonify(ok=True, problems=found)

    @app.route("/api/climate-admin/shelly-health")
    def api_shelly_health():
        """Each Shelly's own status and its last day and week - see
        services/shelly_health.py. An empty list without any Shelly."""
        health = app.config.get("SHELLY_HEALTH")
        return jsonify(ok=True, devices=health.devices() if health else [],
                       hot_c=shelly_health.HOT_C, switch_ons_24h_max=shelly_health.SWITCH_ONS_24H_MAX,
                       heartbeat_seconds=shelly_health.HEARTBEAT_SECONDS)

    @app.route("/api/climate-admin/led-matrix")
    def api_led_matrix():
        # The admin panel's "LED matrix" section: whether it's drawing (or
        # why not), what's on it right now for the live mirror, and every
        # screen it can show for the legend - drawn by the same code that
        # drives the LEDs, see services/led_matrix.py.
        service: led_matrix.LedMatrixService | None = app.config.get("LED_MATRIX")
        data = service.status() if service else {"status": "off", "error": None, "frames_sent": 0, "screen": None}
        if request.args.get("legend"):
            # Once per page load - the mirror itself is polled several
            # times a second and doesn't need it every time.
            data["legend"] = led_matrix.legend()
            data["codes"] = sorted({*led_matrix.PROBLEM_CODES.values(), "S1", "S2", "V1", "V2", "DV", "MQ"})
        return jsonify(ok=True, **data)

    @app.route("/api/devices")
    def api_devices():
        manager: DeviceManager = app.config["MANAGER"]
        power_budget: PowerBudget | None = app.config["POWER_BUDGET"]
        power_sensor = app.config["POWER_SENSOR"]
        interlock_followers: dict[str, str] = app.config["INTERLOCK_FOLLOWERS"]
        forced_by_season = app.config["FORCED_BY_SEASON"]

        sensors = []
        for name in manager.pollers:
            entry = {
                "name": name,
                "kind": manager.sensor_kinds.get(name, "unknown"),
                # A switch the current season turns on or off by itself
                # (see services/interlock.py's SeasonRule) would just be
                # switched back on the next tick. Show its state, but don't
                # offer a control that wouldn't do anything lasting.
                "controllable": (name in manager.switches or name in manager.numbers) and not forced_by_season(name),
                "tab": manager.dashboard_tab.get(name, "home"),
                "visible_in_mode": manager.visible_in_mode.get(name),
                # Simulated readings are marked on the dashboard, so a demo
                # or a test bench never passes for the real apartment.
                "simulated": manager.driver_types.get(name) == "simulated",
            }
            entry.update(manager.ui_options.get(name, {}))
            if entry.get("presets"):
                entry["presets"] = vmc_speeds.apply(entry["presets"], app.config["VMC_SPEEDS"].get())
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

    # A cover that fails is asked again after this long, doubling up to
    # the ceiling: every open page asks every 8s, and a device that isn't
    # answering costs a full socket timeout each time - five of those had
    # the board spending most of its time waiting on blinds that had
    # nothing to say. The failure itself is still returned immediately,
    # so the page keeps showing it.
    COVER_RETRY_SECONDS = 30
    COVER_RETRY_MAX_SECONDS = 300

    def _cover_retry_due(name: str, now: float) -> bool:
        last = app.config["COVER_READS"].get(name)
        if not last or last.get("ok") or not last.get("at"):
            return True
        wait = min(COVER_RETRY_MAX_SECONDS, COVER_RETRY_SECONDS * 2 ** (last.get("failures", 1) - 1))
        return now - last["at"] >= wait

    @app.route("/api/covers/<name>/status")
    def api_cover_status(name):
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]
        cover = manager.covers.get(name)
        if cover is None:
            return jsonify(ok=False, error=f"unknown cover '{name}'"), 404
        now = time.time()
        if not _cover_retry_due(name, now):
            last = app.config["COVER_READS"][name]
            return jsonify(ok=False, error=last["error"], code=last.get("code"), waiting=True), 502
        try:
            status = cover.status()
            positions.observe_state(cover, status["state"])
            app.config["COVER_READS"][name] = {"ok": True, "at": now}
            return jsonify(
                ok=True,
                state=status["state"],
                percent=positions.get(cover.id),
                calibrated=positions.is_calibrated(cover.id),
            )
        except Exception as exc:
            previous = app.config["COVER_READS"].get(name) or {}
            app.config["COVER_READS"][name] = {
                "ok": False, "error": str(exc), "code": getattr(exc, "code", None), "at": now,
                "failures": (previous.get("failures", 0) if not previous.get("ok") else 0) + 1,
            }
            return jsonify(ok=False, error=str(exc), code=getattr(exc, "code", None)), 502

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
            return jsonify(ok=False, error=str(exc), code=getattr(exc, "code", None)), 502
        for cover in targets:
            app.config["COVER_READS"].pop(cover.name, None)  # it answered: poll it normally again

        if action in ("open", "close"):
            for cover in targets:
                positions.set(cover.id, 100 if action == "open" else 0)

        return jsonify(ok=True)

    @app.route("/api/covers/<name>/position", methods=["POST"])
    def api_cover_position(name):
        data = _json_body()
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

    @app.route("/api/readings")
    def api_readings():
        """Every device's last reading at once, by name - what a page that
        shows them all asks every 20 s: one request instead of one a
        device (a dozen on a real install, six at a time in a browser),
        and one moment for all of them."""
        manager: DeviceManager = app.config["MANAGER"]
        return jsonify(ok=True, readings={name: manager.sensor_reading(name) for name in manager.pollers})

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
        if app.config["FORCED_BY_SEASON"](name) and not diagnostic_mode:
            return _fail("switch_mode_controlled", f"'{name}' is controlled automatically by the thermostat mode", 409, name=name)
        try:
            switch.turn_on() if action == "on" else switch.turn_off()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        manager.pollers[name].cache.merge({"is_on": action == "on"})
        _wake_poller(manager, name)
        # A switch that opens another with it (a dehumidifier, the valve it
        # draws its water through): that one follows now, not a minute later.
        interlock: InterlockController | None = app.config["INTERLOCK"]
        if interlock is not None and name in app.config["INTERLOCK_FOLLOWERS"]:
            interlock.tick()
        return jsonify(ok=True)

    @app.route("/api/sensors/<name>/set-value", methods=["POST"])
    def api_sensor_set_value(name):
        data = _json_body()
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
        _wake_poller(manager, name)
        return jsonify(ok=True)

    def vmc_answer(vmc: VmcScheduleController, schedule: dict | None = None):
        return jsonify(ok=True, schedule=schedule or vmc.store.get(), status=vmc.status())

    @app.route("/api/vmc-schedule")
    def api_vmc_schedule_get():
        """The ventilation unit's weekly program, read and written from
        its own card on the dashboard - it's a daily control, not part of
        the installation the admin panel configures."""
        return vmc_answer(app.config["VMC_SCHEDULE"])

    @app.route("/api/vmc-schedule", methods=["POST"])
    def api_vmc_schedule_save():
        """Only what the body names changes: {"enabled", "target"} turns it
        on or off, {"week"} replaces the week."""
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        body = {k: v for k, v in _json_body().items() if k in ("enabled", "target", "week")}
        try:
            schedule = vmc.store.update(body)
        except ValueError as exc:
            return _refusal(exc, 400)
        # Applied straight away, so saving is the whole action - no
        # restart, and no waiting for the next tick to see it work.
        vmc.tick()
        return vmc_answer(vmc, schedule)

    @app.route("/api/vmc-schedule/boost", methods=["POST"])
    def api_vmc_boost():
        """{"device", "value", "minutes"}: that speed for that long, then
        back to the program - or to how the unit was, with it off."""
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        body = _json_body()
        try:
            value = body.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
                raise InputError("vmc_override_invalid", "a speed must be between 0 and 100")
            vmc.boost(str(body.get("device") or ""), round(value), body.get("minutes"))
        except ValueError as exc:
            return _refusal(exc, 400)
        return vmc_answer(vmc)

    @app.route("/api/dehumidifier")
    def api_dehumidifier():
        """The dehumidifier switched on by hand for a while: on until when,
        the reference humidity, the thresholds - None without one."""
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        return jsonify(ok=True, dehumidifier=timer.status() if timer else None)

    @app.route("/api/dehumidifier/on", methods=["POST"])
    def api_dehumidifier_on():
        """{"hours": one of the offered durations} or {"until": "target"}:
        on for that long, or until the room is dry enough. Refused (409)
        with the air dry enough, or out of its season."""
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is None:
            return _fail("no_dehumidifier", "no dehumidifier configured", 404)
        body = _json_body()
        try:
            status = timer.switch_on(body.get("hours"), until=body.get("until"))
        except InputError as exc:
            return _refusal(exc, 400 if exc.code == "dehumidifier_hours_invalid" else 409)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        return jsonify(ok=True, dehumidifier=status)

    @app.route("/api/dehumidifier/settings")
    def api_dehumidifier_settings():
        """Its thresholds and the ways it can be switched on - the admin
        panel's; the defaults, and the durations one can offer."""
        from localhome.services import dehumidifier as dehum
        store: DehumidifierSettingsStore = app.config["DEHUMIDIFIER_SETTINGS"]
        return jsonify(ok=True, settings=store.get(), defaults=dehum.DEFAULTS, ranges=dehum.RANGES,
                       duration_choices=list(dehum.DURATION_CHOICES), configured=app.config["DEHUMIDIFIER"] is not None)

    @app.route("/api/dehumidifier/settings", methods=["POST"])
    def api_dehumidifier_settings_save():
        """Only what the body names changes; `{"reset": true}` puts the
        defaults back. Takes effect at once - no restart."""
        store: DehumidifierSettingsStore = app.config["DEHUMIDIFIER_SETTINGS"]
        body = _json_body()
        try:
            if body.get("reset") is True:
                from localhome.services.dehumidifier import DEFAULTS
                settings = store.set(dict(DEFAULTS))
            else:
                settings = store.set(body)
        except InputError as exc:
            return _refusal(exc, 400)
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is not None:
            timer.tick()        # a lower stop threshold switches it off now, not in 30 s
        return jsonify(ok=True, settings=settings, dehumidifier=timer.status() if timer else None)

    @app.route("/api/dehumidifier/off", methods=["POST"])
    def api_dehumidifier_off():
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is None:
            return _fail("no_dehumidifier", "no dehumidifier configured", 404)
        try:
            status = timer.switch_off()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        return jsonify(ok=True, dehumidifier=status)

    @app.route("/api/vmc-schedule/boost/cancel", methods=["POST"])
    def api_vmc_boost_cancel():
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        vmc.cancel_boost()
        return vmc_answer(vmc)

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

    @app.route("/api/energy/<name>")
    def api_energy(name):
        """Today, yesterday, this week, this month and this year by month -
        kWh and peaks - of anything that measures its power: a meter, a
        smart plug (services/energy.py)."""
        manager: DeviceManager = app.config["MANAGER"]
        poller = manager.pollers.get(name)
        measured = manager.sensor_kinds.get(name) == "power_meter" or (
            poller is not None and "power_w" in getattr(poller.driver, "history_fields", ()))
        if not measured:
            return _fail("not_a_power_meter", f"'{name}' doesn't measure its power", 404, name=name)
        return jsonify(ok=True, name=name, **energy_summary(app.config["HISTORY"], name))

    @app.route("/api/climate/zones")
    def api_climate_zones():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=True, mode=None, zones=[])
        manager: DeviceManager = app.config["MANAGER"]
        now = datetime.now()
        mode = thermostat.schedule_store.get_mode()
        # follower valve -> the interlock driving it in the current mode
        valve_drivers = {
            link.follower: link for link in app.config["INTERLOCK_LINKS"]
            if link.active_in_mode in (None, mode)
        }
        # For a sensor not heard from since the board came up - see sensor_waiting.
        uptime = system_status.board_uptime_seconds()
        zones = []
        overrides = thermostat.schedule_store.get_overrides(now)
        for zone in thermostat.zones:
            sensor_reading = manager.sensor_reading(zone.sensor)
            override = overrides.get(zone.name)
            scheduled = thermostat.schedule_store.get_target(zone.name, now)
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
                # Not shown for an H&T powered over USB-C.
                "battery_pct": None if sensor_reading.get("external_power") is True else sensor_reading.get("battery_pct"),
                "wifi_rssi_dbm": sensor_reading.get("wifi_rssi_dbm"),
                "sensor_ok": sensor_reading.get("ok", False),
                # Tagged on the zone's card ("simulated") - see home.js.
                "sensor_simulated": manager.driver_types.get(zone.sensor) == "simulated",
                # A value typed by hand (the admin panel's "manual" mode):
                # the home page lets it be set from the zone's card.
                "sensor_manual": manager.driver_types.get(zone.sensor) == "manual_override",
                # Not heard from since the board came up, in its first
                # hours: "waiting for the first report", its last value
                # (from before the power cut) still shown.
                "sensor_waiting": not sensor_reading.get("ok", False) and system_status.waiting_since_boot(
                    sensor_reading.get("seconds_since_update"), sensor_reading.get("error"), uptime),
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
                # What the zone is heading for now: a temporary target
                # set from the dashboard if there is one, else the schedule's.
                "target_c": override["target"] if override else scheduled,
                "scheduled_target_c": scheduled,
                "override": {"target": override["target"], "until": override["until"].isoformat()} if override else None,
                "sensor_failsafe_active": thermostat.is_failsafe_active(zone.name),
                "sensor_unreachable_hours": thermostat.unreachable_hours(zone.name),
                "sensor_failsafe_hours": thermostat.sensor_failsafe_hours,
                # Set when an interlock drives this zone's valve right now
                # (e.g. a dehumidifier fed through a bathroom valve that's
                # excluded from cooling) - the dashboard's "why is it open"
                # for a valve the thermostat itself leaves alone.
                "valve_driver": _valve_driver(manager, valve_drivers.get(zone.valve)),
            })
        away_until = thermostat.schedule_store.get_away_until()
        return jsonify(
            ok=True,
            mode=thermostat.schedule_store.get_mode(),
            # Who set the season last, and when - the admin's season card.
            mode_changed=thermostat.schedule_store.get_mode_changed(),
            away_until=away_until.isoformat() if away_until else None,
            diagnostic_mode=thermostat.schedule_store.get_diagnostic_mode(),
            # For the chart's band around the target: the valve only
            # changes state once the temperature leaves target ± this.
            hysteresis_c=thermostat.hysteresis_c,
            # What the season does besides the zones: every switch that
            # follows it, and what it does in each season.
            season_effects={"rules": app.config["SEASON_RULES"](), "links": app.config["SEASON_LINKS"]()},
            # On by hand for a while, never by itself - see services/dehumidifier.py.
            dehumidifier=app.config["DEHUMIDIFIER"].status() if app.config.get("DEHUMIDIFIER") else None,
            zones=zones,
        )

    @app.route("/api/climate/mode", methods=["POST"])
    def api_climate_set_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        mode = data.get("mode")
        if mode not in ("heat", "cool"):
            return jsonify(ok=False, error="mode must be 'heat' or 'cool'"), 400
        previous = thermostat.schedule_store.get_mode()
        by = _who(request)
        thermostat.schedule_store.set_mode(mode, by=by)
        if previous != mode:
            logger.info("Season set to %s (was %s) from %s", mode, previous, by)
        # Re-evaluate right away instead of leaving valves showing stale
        # state until the background loops' next scheduled tick - a mode
        # switch changes every zone's heat/cool logic at once, and the
        # mode switches (a bypass valve) and mode-limited switches (a
        # dehumidifier) with it.
        thermostat.tick()
        interlock: InterlockController | None = app.config["INTERLOCK"]
        if interlock is not None:
            interlock.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/away", methods=["POST"])
    def api_climate_set_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        hours = data.get("hours")
        # Up to a year: beyond that the date overflows (a 500), and no
        # absence the dashboard offers comes anywhere near it.
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0 < hours <= AWAY_MAX_HOURS:
            return _fail("away_hours_invalid", f"hours must be a number between 0 and {AWAY_MAX_HOURS}", max=AWAY_MAX_HOURS)
        until = datetime.now() + timedelta(hours=hours)
        thermostat.schedule_store.set_away_until(until)
        thermostat.tick()
        return jsonify(ok=True, away_until=until.isoformat())

    @app.route("/api/climate/zones/<name>/override", methods=["POST"])
    def api_climate_set_override(name):
        """A zone's schedule set aside for a while - {"target": number or
        null (off), "hours": 0.5-24} or {"target": ..., "until":
        "next_change"}. Gone by itself when the time is up; see
        ScheduleStore's temporary targets."""
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        zone = next((z for z in thermostat.zones if z.name == name), None)
        if zone is None:
            return _fail("zone_unknown", f"no zone named {name!r}", 404, zone=name)
        mode = thermostat.schedule_store.get_mode()
        if mode == "cool" and not zone.cool_enabled:
            return _fail("override_zone_excluded", f"{name!r} is left out of cooling", 409, zone=name)
        data = _json_body()
        target = data.get("target")
        if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))
                                   or not math.isfinite(target) or not OVERRIDE_MIN_C <= target <= OVERRIDE_MAX_C):
            return _fail("override_target_range", f"target must be off (null) or {OVERRIDE_MIN_C}-{OVERRIDE_MAX_C}",
                         min=OVERRIDE_MIN_C, max=OVERRIDE_MAX_C)
        now = datetime.now()
        if data.get("until") == "next_change":
            # A schedule that never changes: a day, like the longest duration.
            until = thermostat.schedule_store.next_change(name, now) or now + timedelta(hours=OVERRIDE_MAX_HOURS)
        else:
            hours = data.get("hours")
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= OVERRIDE_MAX_HOURS:
                return _fail("override_hours_invalid", f"hours must be above 0 and at most {OVERRIDE_MAX_HOURS}",
                             max=OVERRIDE_MAX_HOURS)
            until = now + timedelta(hours=hours)
        thermostat.schedule_store.set_override(name, float(target) if target is not None else None, until)
        thermostat.tick()
        return jsonify(ok=True, override={"target": target, "until": until.isoformat()})

    @app.route("/api/climate/zones/<name>/override/cancel", methods=["POST"])
    def api_climate_cancel_override(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        thermostat.schedule_store.clear_override(name)
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/away/cancel", methods=["POST"])
    def api_climate_cancel_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        thermostat.schedule_store.set_away_until(None)
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/diagnostic-mode", methods=["POST"])
    def api_climate_set_diagnostic_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
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
            return _fail("no_thermostat", "no thermostat configured", 404)
        mode = request.args.get("mode") or thermostat.schedule_store.get_mode()
        if mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        return jsonify(ok=True, mode=mode, week=thermostat.schedule_store.get_week(name, mode))

    @app.route("/api/climate/zones/<name>/schedule", methods=["POST"])
    def api_climate_set_schedule(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        week, cells = data.get("week"), data.get("cells")
        if not isinstance(week, dict) and cells is None:
            return jsonify(ok=False, error="missing 'week' or 'cells'"), 400
        mode = data.get("mode")
        if mode is not None and mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        try:
            if cells is not None:
                # Just the hours that changed, merged into the week as it is
                # now - see ScheduleStore.set_cells().
                week = thermostat.schedule_store.set_cells(name, cells, mode)
            else:
                thermostat.schedule_store.set_week(name, week, mode)
        except ValueError as exc:
            return _refusal(exc, 400)
        # Same reasoning as the mode switch above: don't leave the valve
        # (and the status label derived from it) stale until the next
        # scheduled tick just because the new target was written to disk.
        thermostat.tick()
        return jsonify(ok=True, week=thermostat.schedule_store.get_week(name, mode))

    @app.route("/api/climate/zones/<name>/on-hours")
    def api_climate_on_hours(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        zone = next((z for z in thermostat.zones if z.name == name), None)
        if zone is None:
            return jsonify(ok=False, error=f"unknown zone '{name}'"), 404
        store: HistoryStore = app.config["HISTORY"]
        stats = store.on_stats(zone.valve, "is_on", app.config["HISTORY_POLL_INTERVAL"])
        return jsonify(
            ok=True,
            **{key: round(value["hours"], 2) for key, value in stats.items()},
            starts={key: value["starts"] for key, value in stats.items()},
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
            site_name=site_name(),
            t=app.config["TRANSLATIONS"],
            lang=config.web_language,
            # The address the Shellys' MQTT setting points at - see the
            # Devices section's guide to the Shelly app.
            board_host=f"{socket.gethostname()}.local",
        )

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
        restart_pending = role != app.config["ACTIVE_EXTRA_ROLES"]["outdoor"]
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
        restart_pending = role != app.config["ACTIVE_EXTRA_ROLES"]["vmc"]
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

    @app.route("/api/climate-admin/vmc-speeds")
    def api_vmc_speeds_get():
        """The VMC's night/day/boost speeds in use, and whether they were
        set here (else they're its device's own)."""
        device = _vmc_speed_device(app.config["MANAGER"])
        saved = app.config["VMC_SPEEDS"].get()
        return jsonify(ok=True, device=device, saved=saved is not None, speeds=_vmc_speeds_in_use(app, device))

    @app.route("/api/climate-admin/vmc-speeds", methods=["POST"])
    def api_vmc_speeds_save():
        """New speeds, in use at once: the buttons and programs show them,
        a program's hours move to them (its night stays night), and a
        unit running at one of the old speeds moves to its new value."""
        manager: DeviceManager = app.config["MANAGER"]
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        device = _vmc_speed_device(manager)
        old = _vmc_speeds_in_use(app, device)
        try:
            new = app.config["VMC_SPEEDS"].set(_json_body())
        except ValueError as exc:
            return _refusal(exc, 400)
        if old:
            moves = {old[k]: new[k] for k in vmc_speeds.SPEED_KEYS if k in old}
            schedule = vmc.store.get()
            week = vmc_speeds.remap_week(schedule["week"], old, new)
            if week != schedule["week"]:
                vmc.store.update({"week": week})
            vmc.speeds_changed(moves)
            _move_vmc_to_new_speed(manager, device, moves)
        return jsonify(ok=True, device=device, saved=True, speeds=new)

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

    # ---------------------------------------------------- settings ----

    @app.route("/api/climate-admin/settings")
    def api_panel_settings_get():
        store: panel_settings.PanelSettingsStore = app.config["PANEL_SETTINGS"]
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        saved = store.get()
        history: HistoryStore = app.config["HISTORY"]
        ceilings = {"history_max_db_mb": _history_ceiling_mb(history)}
        return jsonify(
            ok=True,
            settings=panel_settings.effective(config, saved, ceilings),
            # Next to "maximum history size": what's in there now, so the
            # ceiling is a number with something to compare it against -
            # and the disk the ceiling's own maximum comes from.
            history=_database_stats(),
            disk=_history_disk(history),
            restart_pending=panel_settings.needs_restart(saved, app.config["ACTIVE_PANEL_SETTINGS"]),
        )

    @app.route("/api/climate-admin/settings", methods=["POST"])
    def api_panel_settings_set():
        store: panel_settings.PanelSettingsStore = app.config["PANEL_SETTINGS"]
        data = _json_body()
        ceilings = {"history_max_db_mb": _history_ceiling_mb(app.config["HISTORY"])}
        try:
            saved = store.set(data.get("values") or {}, ceilings)
        except ValueError as exc:
            return _refusal(exc, 400)
        return jsonify(ok=True, saved=saved,
                       restart_required=panel_settings.needs_restart(
                           saved, app.config["ACTIVE_PANEL_SETTINGS"]))

    # ------------------------------------------------- maintenance ----
    # Destructive: each needs {"confirm": "RESET"} in the body, and saves a
    # backup on the board first - see services/maintenance.py.

    def _confirmed() -> bool:
        return (_json_body()).get("confirm") == "RESET"

    @app.route("/api/climate-admin/maintenance")
    def api_maintenance_info():
        maintenance: Maintenance = app.config["MAINTENANCE"]
        return jsonify(ok=True, backups=maintenance.list_backups(),
                       has_thermostat=maintenance.schedule_store is not None,
                       restart_supported=is_supported())

    @app.route("/api/climate-admin/diagnostics.zip")
    def api_diagnostics_download():
        """Everything needed to understand this board from afar, in one file
        - see services/diagnostics.py. No passwords or keys in it."""
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        software = app.config["SOFTWARE_VERSION"] or {}
        data = diagnostics.build_zip(
            _system_status_payload(),
            app.config["MAINTENANCE"].backup_zip(include_credentials=False),
            config.resolve("watchdog.log"),
            system_status.WIFI_WATCHDOG_LOG,
            service_events=system_status.SERVICE_WATCHDOG_LOG,
            version=" ".join(str(v) for v in (software.get("commit"), software.get("date")) if v),
        )
        name = f"localhome-diagnostics-{datetime.now():%Y%m%d-%H%M}.zip"
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=name)

    @app.route("/api/climate-admin/maintenance/backup.zip")
    def api_maintenance_backup_download():
        maintenance: Maintenance = app.config["MAINTENANCE"]
        data = maintenance.backup_zip(include_credentials=False)
        name = f"localhome-backup-{datetime.now():%Y%m%d-%H%M}.zip"
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=name)

    @app.route("/api/climate-admin/maintenance/clear-history", methods=["POST"])
    def api_maintenance_clear_history():
        if not _confirmed():
            return jsonify(ok=False, error='type RESET to confirm'), 400
        return jsonify(ok=True, **app.config["MAINTENANCE"].clear_history())

    @app.route("/api/climate-admin/maintenance/clear-schedules", methods=["POST"])
    def api_maintenance_clear_schedules():
        if not _confirmed():
            return jsonify(ok=False, error='type RESET to confirm'), 400
        try:
            result = app.config["MAINTENANCE"].clear_schedules()
        except ValueError as exc:
            return _refusal(exc, 409)
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is not None:
            thermostat.tick()  # no schedule left: close the valves now, not in a minute
        return jsonify(ok=True, **result)

    @app.route("/api/climate-admin/maintenance/factory-reset", methods=["POST"])
    def api_maintenance_factory_reset():
        if not _confirmed():
            return jsonify(ok=False, error='type RESET to confirm'), 400
        result = app.config["MAINTENANCE"].factory_reset()
        # The running process still holds the old mapping in memory.
        restarting = is_supported()
        if restarting:
            schedule_restart()
        return jsonify(ok=True, restarting=restarting, **result)

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


def listen_addresses(host: str, port: int, ipv6: bool | None = None) -> str:
    """Where the web server listens, as waitress's `listen`. "Every
    address" (0.0.0.0, "::" or empty) is IPv4 *and* IPv6: on a router that
    hands out IPv6 (a Wind3 HUB, say) a phone or PC resolves the board's
    .local name to an IPv6 address too and tries that first - an
    IPv4-only server refuses it, and the page "refuses the connection"
    by name while it opens by IP. A specific address is used as given."""
    if host not in ("0.0.0.0", "", "::"):
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    if ipv6 is None:
        ipv6 = _ipv6_available()
    return f"0.0.0.0:{port} [::]:{port}" if ipv6 else f"0.0.0.0:{port}"


def _ipv6_available() -> bool:
    """False where the kernel has IPv6 switched off - binding [::] would
    stop the server from starting at all."""
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


# Worker threads for the web server. One dashboard tab fires ~20 requests
# at once on load (every card, chart and stat) - 8 serves that without
# queueing, with room for a second device.
WEB_THREADS = 8


# The kind of device a request came from, by its browser's own words,
# and its address: "iPhone (192.168.1.40)". Enough to tell the family's
# phones from the PC on the desk; nothing is stored about anyone.
_DEVICE_WORDS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                 ("Macintosh", "Mac"), ("Linux", "Linux"))


def _who(req) -> str:
    agent = req.headers.get("User-Agent", "")
    device = next((name for word, name in _DEVICE_WORDS if word in agent), None)
    address = req.remote_addr or "?"
    return f"{device} ({address})" if device else address


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    config = app.config["LOCALHOME_CONFIG"]
    # Waitress, not Flask's own app.run(): that one is werkzeug's
    # development server, which Flask itself says not to use in
    # production. Waitress is a production WSGI server that's still a
    # single multi-threaded process - which matters here: create_app()
    # starts the thermostat, pollers and history recorder inside this
    # process, and a multi-process server (gunicorn's workers) would start
    # one copy of each per worker, all commanding the same valves.
    # Pure Python, so it installs the same on the board and a dev PC.
    # It logs no per-request lines (those were ~98% of werkzeug's log
    # volume, all written to the board's eMMC by journald).
    from waitress import serve

    # Tells systemd it's alive while the page answers and the loops run -
    # a hang gets LocalHome restarted, not only a crash. Does nothing where
    # systemd didn't ask for it; see services/service_watchdog.py.
    host = "127.0.0.1" if config.web_host in ("0.0.0.0", "", "::") else config.web_host
    ServiceWatchdog(f"http://{host}:{config.web_port}/api/server-time", lambda: _loops(app),
                    event_log=config.resolve("watchdog.log")).start()
    # Stopped by systemd - a restart, an update: a normal exit, so what's
    # still only in memory is written first (the MQTT devices' last
    # payloads - drivers/mqtt/state_cache.py). Python's default for
    # SIGTERM is to die on the spot.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(app, listen=listen_addresses(config.web_host, config.web_port), threads=WEB_THREADS, ident="LocalHome")


if __name__ == "__main__":
    main()
