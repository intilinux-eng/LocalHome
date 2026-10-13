"""Flask application factory: builds the house - devices, thermostat,
history, the VMC's and the dehumidifier's controllers - and registers the
endpoints, one module per area in web/routes/.

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
import signal
import socket
import subprocess
import sys

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from localhome.config import load_config
from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import (
    EXTRA_SWITCH_FIELDS,
    OUTDOOR_SENSOR_NAME,
    SEASON_MODES,
    apply_to_config,
    extra_roles,
    open_climate_map,
    panel_season_rules,
)
from localhome.services.day_range import DayRange
from localhome.services.dehumidifier import DehumidifierSettingsStore, DehumidifierTimer
from localhome.services.history import HistoryRecorder, HistoryStore, PowerBudget
from localhome.services import led_matrix, panel_settings
from localhome.services.maintenance import Maintenance
from localhome.services.manual_override import ManualOverrideStore
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
from localhome.services.service_watchdog import ServiceWatchdog
from localhome.services import system_status
from localhome.services.thermostat import ScheduleStore, ThermostatController, Zone
from localhome.services import vmc_speeds
from localhome.services.energy import EnergyLedger
from localhome.services import run_hours
from localhome.services.vmc_auto import VmcAuto, VmcAutoSettingsStore
from localhome.services.vmc_filters import VmcFilterStore
from localhome.services.vmc_schedule import VmcScheduleController, VmcScheduleStore
from localhome.web.auth import register_auth
from localhome.web.i18n import load_translations
from localhome.web.transfer import register_transfer
from localhome.web.routes import register_routes
from localhome.web.routes.climate import _sensor_zones, _valve_zones
from localhome.web.routes.common import _wake_poller
from localhome.web.routes.plant import _auto_zones, _speeds_in_use, _vmc_speed_device
from localhome.web.routes.system import (
    _history_ceiling_mb,
    _history_disk,
    _lan_ip,
    _loops,
    _matrix_state,
    _start_shelly_health,
)

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

    # Versioned static files kept by the browser, text sent gzipped - see web/transfer.py.
    register_transfer(app, asset_version)

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
    climate_map = open_climate_map(config)
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

    def held_off_while_away(name: str) -> bool:
        """Left to whoever uses it in this season (the dehumidifier) - and
        nobody's home: off until they're back (services/interlock.py)."""
        if thermostat is None or thermostat.schedule_store.get_away_until() is None:
            return False
        mode = thermostat.schedule_store.get_mode()
        return any(r["switch"] == name and r[mode] == "manual" for r in season_rules())

    auth_cfg = (config.raw.get("web") or {}).get("auth")
    if auth_cfg:
        register_auth(app, config.resolve(auth_cfg["secrets_file"]))

    positions_path = config.resolve(config.raw.get("positions_file", "positions.json"))
    positions = PositionStore(positions_path)

    history_cfg = config.raw.get("history", {})
    db_path = config.resolve(history_cfg.get("db_file", "data/history.db"))
    store = HistoryStore(db_path, write_every_seconds=history_cfg.get("write_every_seconds", 300))

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
    # Today's outdoor minimum and maximum, for the home page: in memory,
    # fed by the recorder - see services/day_range.py.
    day_range = DayRange([(OUTDOOR_SENSOR_NAME, "temperature_c")])
    try:
        day_range.seed(store)
    except Exception:
        logger.exception("Outdoor minimum/maximum: couldn't read today's history - from now on")
    recorder.day_range = day_range

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
            Zone(name=z["name"], sensor=z["sensor"], valve=z["valve"], cool_enabled=z.get("cool_enabled", True),
                 sensor_configured=z.get("sensor_configured", True), valve_configured=z.get("valve_configured", True))
            for z in thermostat_cfg.get("zones", [])
        ]
        thermostat = ThermostatController(
            manager,
            schedule_store,
            zones,
            hysteresis_c=thermostat_cfg.get("hysteresis_c", 0.5),
            economy_tolerance_c=thermostat_cfg.get("economy_tolerance_c", 0.5),
            poll_interval_seconds=thermostat_cfg.get("poll_interval_seconds", 60),
            sensor_failsafe_hours=thermostat_cfg.get("sensor_failsafe_hours", 3.0),
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
        # The valves' hours of each day kept before their samples go.
        recorder.keep_runs = lambda: run_hours.keep_days(
            store, _valve_zones(thermostat), history_poll_interval, int(recorder.retention_days) + 2,
            temps=[OUTDOOR_SENSOR_NAME, *(z["sensor"] for z in _sensor_zones(thermostat))])
        # Each zone's lowest and highest today, and when - with the outdoor's.
        try:
            day_range.watch([(z["sensor"], "temperature_c") for z in _sensor_zones(thermostat)], store)
        except Exception:
            logger.exception("Zones' minimum/maximum: couldn't read today's history - from now on")

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
            away_provider=lambda: schedule_store.get_away_until() is not None,
        )
        interlock_followers = {link.leader: link.follower for link in interlocks}
        thermostat.interlock_owned_valves = {link.follower: link.active_in_mode for link in interlocks}
        interlock_links = interlocks

    # The ventilation unit's own day/night speed program (services/
    # vmc_schedule.py) - independent of the thermostat, and re-read from
    # its file every tick so the admin panel can change it without a
    # restart.
    vmc_schedule = VmcScheduleStore(config.resolve("vmc_schedule.json"))
    # Its automatic adjustments - a shower, humid outdoor air, night
    # cooling (services/vmc_auto.py) - from the zones' sensors and the
    # outdoor one, set in the admin panel and read at every check.
    vmc_auto_settings = VmcAutoSettingsStore(config.resolve("vmc_auto.json"))
    # Its speeds, as set in the admin panel over the device's own. Read
    # through the manager and this store directly: the loop's first check
    # runs before app.config is filled.
    vmc_speed_store = vmc_speeds.VmcSpeedStore(config.resolve("vmc_speeds.json"))
    vmc = VmcScheduleController(
        manager, vmc_schedule,
        auto=VmcAuto(
            manager, vmc_auto_settings,
            season=lambda: thermostat.schedule_store.get_mode() if thermostat else None,
            zones=lambda: _auto_zones(thermostat),
            outdoor_sensor=OUTDOOR_SENSOR_NAME,
            device=lambda: _vmc_speed_device(manager),
            speeds=lambda device: _speeds_in_use(manager, vmc_speed_store, device),
        ),
        # Away from home, at night speed or off - see api_climate_set_away.
        away_until=lambda: thermostat.schedule_store.get_away_until() if thermostat else None,
        device=lambda: _vmc_speed_device(manager),
        speeds=lambda device: _speeds_in_use(manager, vmc_speed_store, device),
    )

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
        if thermostat.schedule_store.get_diagnostic_mode() or held_off_while_away(name):
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
    app.config["DAY_RANGE"] = day_range
    app.config["HISTORY_POLL_INTERVAL"] = history_poll_interval
    # The heat meter's readings, typed in the admin panel - beside the
    # plant's hours (services/run_hours.py).
    app.config["HEAT_METER"] = run_hours.HeatMeterStore(config.resolve("heat_meter.json"))
    app.config["RECORDER"] = recorder
    app.config["POWER_BUDGET"] = power_budget
    app.config["POWER_SENSOR"] = power_sensor
    app.config["THERMOSTAT"] = thermostat
    app.config["INTERLOCK"] = interlock
    app.config["VMC_SCHEDULE"] = vmc
    app.config["VMC_AUTO_SETTINGS"] = vmc_auto_settings
    app.config["DEHUMIDIFIER"] = dehumidifier
    app.config["DEHUMIDIFIER_SETTINGS"] = dehumidifier_settings
    # The VMC's night/day/boost speeds as set in the admin panel, laid over
    # its device's presets - see services/vmc_speeds.py.
    app.config["VMC_SPEEDS"] = vmc_speed_store
    # When its filters were last cleaned, and how often - the home card's
    # reminder; see services/vmc_filters.py.
    app.config["VMC_FILTERS"] = VmcFilterStore(config.resolve("vmc_filters.json"))
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
    app.config["HELD_OFF_WHILE_AWAY"] = held_off_while_away
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
        history_files=[app.config["HEAT_METER"].path],
    )

    register_routes(app)
    # The UNO Q's LED matrix: health at a glance for whoever is standing
    # in front of the box - off by itself on anything that isn't a UNO Q
    # (no arduino-router socket). See services/led_matrix.py.
    matrix = led_matrix.LedMatrixService(lambda: _matrix_state(app), ip_provider=_lan_ip,
                                         name_provider=lambda: app.config["SITE_NAME"]())
    app.config["LED_MATRIX"] = matrix
    matrix.start()
    return app


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
