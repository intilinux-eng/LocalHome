#!/usr/bin/env python3
from flask import Flask, jsonify, render_template, request

import ewelink_power
import history
import position_control as pc
from shutters import cmd_action, find_shutter, get_device, load_shutters

app = Flask(__name__)
ewelink_power.start_background()
history.start_background()


@app.route("/")
def index():
    shutters = load_shutters()
    names = [s["name"] for s in shutters]
    return render_template("index.html", names=names)


@app.route("/api/status/<name>")
def api_status(name):
    shutters = load_shutters()
    shutter = find_shutter(shutters, name)
    try:
        d = get_device(shutter)
        status = d.status()
        dps = status.get("dps", {})
        return jsonify(
            ok=True,
            state=dps.get("1", "unknown"),
            percent=pc.get_position(shutter),
            calibrated=pc.is_calibrated(shutter),
        )
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502


@app.route("/api/action/<name>/<action>", methods=["POST"])
def api_action(name, action):
    if action not in ("open", "close", "stop"):
        return jsonify(ok=False, error="invalid action"), 400
    shutters = load_shutters()
    try:
        targets = shutters if name.lower() == "all" else [find_shutter(shutters, name)]
    except SystemExit:
        return jsonify(ok=False, error="shutter not found"), 404

    for shutter in targets:
        pc.cancel_pending_move(shutter["id"])

    try:
        cmd_action(shutters, name, action)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502

    if action in ("open", "close"):
        for shutter in targets:
            pc.set_position(shutter, 100 if action == "open" else 0)

    return jsonify(ok=True)


@app.route("/api/position/<name>", methods=["POST"])
def api_position(name):
    data = request.get_json(silent=True) or {}
    if "percent" not in data:
        return jsonify(ok=False, error="missing 'percent'"), 400
    shutters = load_shutters()
    try:
        shutter = find_shutter(shutters, name)
    except SystemExit:
        return jsonify(ok=False, error="shutter not found"), 404
    try:
        result = pc.move_to_percent(shutter, data["percent"], get_device)
        return jsonify(ok=True, **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502


@app.route("/api/energy")
def api_energy():
    reading = ewelink_power.get_power_reading()
    if reading.get("ok"):
        reading["peak_today_w"] = max(history.query_peak_today(), reading["power_w"])
        reading["contract_limit_w"] = history.CONTRACT_LIMIT_W
        reading["available_power_w"] = history.AVAILABLE_POWER_W
        reading["trip_risk_w"] = history.TRIP_RISK_W
    return jsonify(reading)


@app.route("/api/energy/history")
def api_energy_history():
    range_key = request.args.get("range", "24h")
    if range_key not in history.RANGE_BUCKETS:
        return jsonify(ok=False, error="invalid range"), 400
    return jsonify(
        ok=True,
        points=history.query_power_history(range_key),
        contract_limit_w=history.CONTRACT_LIMIT_W,
        available_power_w=history.AVAILABLE_POWER_W,
        trip_risk_w=history.TRIP_RISK_W,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
