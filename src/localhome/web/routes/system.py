"""The board's own health and upkeep: the system status the admin panel
shows, the problems the home page's health chip counts, the Shelly health
check and the LED matrix; the panel's settings, a restart, and maintenance
(backups, diagnostics, clearing the history or the schedules, a factory
reset)."""
from __future__ import annotations

import io
import os
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_file

from localhome.config import LocalHomeConfig
from localhome.core.manager import DeviceManager
from localhome.core.registry import skipped_driver_modules
from localhome.services.history import HistoryRecorder, HistoryStore
from localhome.services import diagnostics, led_matrix, panel_settings
from localhome.services.maintenance import Maintenance
from localhome.drivers.mqtt import base as mqtt_base, client as mqtt_client
from localhome.drivers.mqtt.rpc import ShellyRpcClient
from localhome.drivers.simulated import world as simulated_world
from localhome.services.self_restart import is_supported, schedule_restart
from localhome.services import shelly_health
from localhome.services import system_status
from localhome.services.thermostat import ThermostatController
from localhome.web.routes.common import _json_body, _refusal, logger


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
    # Shellys on the mains, through the broker: a relay, a dimmer.
    mains = {name for name, driver in manager.driver_types.items()
             if driver in ("mqtt_json", "shelly_light")} - battery
    return {
        "lan_silent_seconds": app.config["LAN_WATCH"].update(system_status.lan_mqtt_peers()),
        "battery_silent": system_status.battery_sensors_silent(devices, battery, uptime_seconds, mains_names=mains),
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


def register(app: Flask) -> None:
    """The board's health, settings, restart and maintenance."""
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

    @app.route("/api/climate-admin/shelly-health/revision")
    def api_shelly_health_revision():
        """What the Shellys said last, as a short mark from memory - the
        admin page reads the whole list again when it moves."""
        health = app.config.get("SHELLY_HEALTH")
        return jsonify(ok=True, revision=health.revision() if health else "")

    @app.route("/api/climate-admin/shelly-health")
    def api_shelly_health():
        """Each Shelly's own status and its last day and week - see
        services/shelly_health.py. An empty list without any Shelly."""
        health = app.config.get("SHELLY_HEALTH")
        return jsonify(ok=True, devices=health.devices() if health else [],
                       hot_c=shelly_health.HOT_C, switch_ons_24h_max=shelly_health.SWITCH_ONS_24H_MAX,
                       heartbeat_seconds=shelly_health.HEARTBEAT_SECONDS,
                       low_battery_pct=shelly_health.LOW_BATTERY_PCT)

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
            translations=app.config["TRANSLATIONS"],
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
