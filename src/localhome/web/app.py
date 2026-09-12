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

from flask import Flask, jsonify, render_template, request

from localhome.config import LocalHomeConfig, load_config
from localhome.core.manager import DeviceManager
from localhome.services.history import HistoryRecorder, HistoryStore, PowerBudget, RANGE_BUCKETS
from localhome.services.position_control import PositionStore
from localhome.web.auth import register_auth
from localhome.web.i18n import load_translations


def create_app(config_path: str | None = None) -> Flask:
    app = Flask(__name__)
    config = load_config(config_path)
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

    recorder = HistoryRecorder(
        manager,
        store,
        poll_interval_seconds=history_cfg.get("poll_interval_seconds", 30),
        power_budget=power_budget,
        power_sensor=power_sensor,
    )

    manager.start()
    recorder.start()

    app.config["LOCALHOME_CONFIG"] = config
    app.config["TRANSLATIONS"] = load_translations(config.web_language)
    app.config["MANAGER"] = manager
    app.config["POSITIONS"] = positions
    app.config["HISTORY"] = store
    app.config["POWER_BUDGET"] = power_budget
    app.config["POWER_SENSOR"] = power_sensor

    _register_routes(app)
    return app


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

    @app.route("/api/devices")
    def api_devices():
        manager: DeviceManager = app.config["MANAGER"]
        power_budget: PowerBudget | None = app.config["POWER_BUDGET"]
        power_sensor = app.config["POWER_SENSOR"]

        sensors = []
        for name in manager.pollers:
            entry = {
                "name": name,
                "kind": manager.sensor_kinds.get(name, "unknown"),
                "controllable": name in manager.switches or name in manager.numbers,
            }
            if power_budget is not None and name == power_sensor:
                entry["power_budget"] = {
                    "contract_limit_w": power_budget.contract_limit_w,
                    "available_power_w": power_budget.available_power_w,
                    "trip_risk_w": power_budget.trip_risk_w,
                }
            number = manager.numbers.get(name)
            if number is not None:
                entry["range"] = {"min": number.min_value, "max": number.max_value, "unit": number.unit}
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
        try:
            switch.turn_on() if action == "on" else switch.turn_off()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    config = app.config["LOCALHOME_CONFIG"]
    app.run(host=config.web_host, port=config.web_port, debug=False)


if __name__ == "__main__":
    main()
