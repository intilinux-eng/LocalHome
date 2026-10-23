"""The devices of config.yaml, by kind: the list the pages build their
cards from, every reading at once, a sensor's history, a switch or a
number commanded, covers and their position, energy meters' days."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for

from flask import Flask, jsonify, request

from localhome.core.manager import DeviceManager
from localhome.services.day_range import DayRange
from localhome.services.history import HistoryStore, PowerBudget, RANGE_BUCKETS
from localhome.services.position_control import PositionStore
from localhome.services.interlock import InterlockController
from localhome.services.thermostat import ThermostatController
from localhome.services import vmc_speeds
from localhome.services.energy import summary as energy_summary
from localhome.web.routes.common import _fail, _json_body, _wake_poller


def register(app: Flask) -> None:
    """The devices' cards, readings, commands and history."""
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

    # "All" asks every cover at once, each on its own thread. One by one, a
    # shutter off the network held the others back for its whole socket
    # timeout, and its error left the ones after it without the command.
    # The answer comes when they've all replied, or after this long: the
    # ones still trying go on, and are said as still waiting.
    COVER_ALL_WAIT_SECONDS = 4.0
    cover_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cover")

    def _send_cover(cover, action: str) -> None:
        positions: PositionStore = app.config["POSITIONS"]
        cover.send(action)
        app.config["COVER_READS"].pop(cover.name, None)  # it answered: poll it normally again
        if action in ("open", "close"):
            positions.set(cover.id, 100 if action == "open" else 0)

    def _cover_all(action: str):
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]
        covers = list(manager.covers.values())
        for cover in covers:
            positions.cancel_pending_move(cover.id)
            positions.mark_own_command(cover.id)
        futures = {cover_pool.submit(_send_cover, cover, action): cover.name for cover in covers}
        done, _ = wait_for(futures, timeout=app.config.get("COVER_ALL_WAIT_SECONDS", COVER_ALL_WAIT_SECONDS))
        outcome = {}
        for future in done:
            exc = future.exception()
            outcome[futures[future]] = None if exc is None else exc
        sent = [c.name for c in covers if c.name in outcome and outcome[c.name] is None]
        failed = [{"name": c.name, "error": str(outcome[c.name]), "code": getattr(outcome[c.name], "code", None)}
                  for c in covers if outcome.get(c.name) is not None]
        pending = [c.name for c in covers if c.name not in outcome]
        if covers and not sent and not pending:
            return jsonify(ok=False, error="no cover answered", sent=[], failed=failed, pending=[]), 502
        return jsonify(ok=True, sent=sent, failed=failed, pending=pending)

    @app.route("/api/covers/<name>/action/<action>", methods=["POST"])
    def api_cover_action(name, action):
        if action not in ("open", "close", "stop"):
            return jsonify(ok=False, error="invalid action"), 400
        if name.lower() == "all":
            return _cover_all(action)
        manager: DeviceManager = app.config["MANAGER"]
        positions: PositionStore = app.config["POSITIONS"]
        cover = manager.covers.get(name)
        if cover is None:
            return jsonify(ok=False, error=f"unknown cover '{name}'"), 404
        positions.cancel_pending_move(cover.id)
        positions.mark_own_command(cover.id)
        try:
            _send_cover(cover, action)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc), code=getattr(exc, "code", None)), 502
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
        # Today's minimum and maximum where kept (the outdoor temperature):
        # {sensor: {field: {"min", "max"}}}.
        day_range: DayRange = app.config["DAY_RANGE"]
        ranges: dict[str, dict] = {}
        for sensor, field in day_range.watched:
            today = day_range.get(sensor, field)
            if today is not None:
                ranges.setdefault(sensor, {})[field] = today
        return jsonify(ok=True, readings={name: manager.sensor_reading(name) for name in manager.pollers},
                       day_ranges=ranges)

    def vmc_held_away(name: str) -> bool:
        """The VMC's speed or its switch, held at night or off while away:
        nothing changes it until "I'm back"."""
        away = app.config["VMC_SCHEDULE"].holding_away()
        return bool(away) and name in (away["device"], app.config["MANAGER"].paired_switch.get(away["device"]))

    @app.route("/api/sensors/<name>/<action>", methods=["POST"])
    def api_sensor_action(name, action):
        if action not in ("on", "off"):
            return jsonify(ok=False, error="invalid action"), 400
        manager: DeviceManager = app.config["MANAGER"]
        switch = manager.switches.get(name)
        if switch is None:
            return jsonify(ok=False, error=f"'{name}' is not a controllable switch"), 404
        if vmc_held_away(name):
            return _fail("vmc_away", f"'{name}' stays as away mode set it until back home", 409)
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        diagnostic_mode = bool(thermostat and thermostat.schedule_store.get_diagnostic_mode())
        if app.config["FORCED_BY_SEASON"](name) and not diagnostic_mode:
            return _fail("switch_mode_controlled", f"'{name}' is controlled automatically by the thermostat mode", 409, name=name)
        if action == "on" and app.config["HELD_OFF_WHILE_AWAY"](name) and not diagnostic_mode:
            return _fail("switch_away", f"'{name}' stays off while away", 409, name=name)
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
        if vmc_held_away(name):
            return _fail("vmc_away", f"'{name}' stays as away mode set it until back home", 409)
        try:
            number.set_value(data["value"])
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        manager.pollers[name].cache.merge({"value": data["value"]})
        _wake_poller(manager, name)
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

    @app.route("/api/energy/import", methods=["POST"])
    def api_energy_import():
        """The meters' own past days read now - a Meross plug keeps a month:
        what "Cancella storico" took comes back without waiting for the
        daily read (tools/import_meter_history.py). {"imported": {meter: days}}."""
        recorder = app.config["RECORDER"]
        try:
            return jsonify(ok=True, imported=recorder.import_device_energy())
        except Exception as exc:  # noqa: BLE001 - a cloud not answering, said as such
            return _fail("energy_import_failed", str(exc)[:200], 502)

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
