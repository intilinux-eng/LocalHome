#!/usr/bin/env python3
import time

from flask import Flask, jsonify, render_template, request

import ewelink_power
import history
import meross_plug
import position_control as pc
import tuya_air_quality
from shutters import cmd_action, find_shutter, get_device, load_shutters

app = Flask(__name__)
ewelink_power.start_background()
history.start_background()
tuya_air_quality.start_background()
meross_plug.start_background()


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
        state = dps.get("1", "unknown")
        pc.observe_state(shutter, state)
        return jsonify(
            ok=True,
            state=state,
            percent=pc.get_position(shutter),
            calibrated=pc.is_calibrated(shutter),
        )
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502


@app.route("/api/action/<name>/<action>", methods=["POST"])
def api_action(name, action):
    if action not in ("open", "close", "stop"):
        return jsonify(ok=False, error="azione non valida"), 400
    shutters = load_shutters()
    try:
        targets = shutters if name.lower() == "all" else [find_shutter(shutters, name)]
    except SystemExit:
        return jsonify(ok=False, error="tapparella non trovata"), 404

    for shutter in targets:
        pc.cancel_pending_move(shutter["id"])
        pc.mark_own_command(shutter["id"])

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
        return jsonify(ok=False, error="parametro 'percent' mancante"), 400
    shutters = load_shutters()
    try:
        shutter = find_shutter(shutters, name)
    except SystemExit:
        return jsonify(ok=False, error="tapparella non trovata"), 404
    try:
        result = pc.move_to_percent(shutter, data["percent"], get_device)
        return jsonify(ok=True, **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502


@app.route("/api/energy")
def api_energy():
    reading = ewelink_power.get_power_reading()
    if reading.get("ok"):
        peak = history.query_peak_today()
        if reading["power_w"] >= peak["power_w"]:
            peak = {"power_w": reading["power_w"], "ts": int(time.time())}
        reading["peak_today_w"] = peak["power_w"]
        reading["peak_today_ts"] = peak["ts"]
        reading["contract_limit_w"] = history.CONTRACT_LIMIT_W
        reading["available_power_w"] = history.AVAILABLE_POWER_W
        reading["trip_risk_w"] = history.TRIP_RISK_W
    return jsonify(reading)


@app.route("/api/air-quality")
def api_air_quality():
    return jsonify(tuya_air_quality.get_air_quality_reading())


@app.route("/api/plug")
def api_plug():
    return jsonify(meross_plug.get_plug_reading())


@app.route("/api/plug/<action>", methods=["POST"])
def api_plug_action(action):
    if action not in ("on", "off"):
        return jsonify(ok=False, error="azione non valida"), 400
    try:
        meross_plug.set_power(action == "on")
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502
    return jsonify(ok=True)


@app.route("/api/energy/history")
def api_energy_history():
    range_key = request.args.get("range", "24h")
    if range_key not in history.RANGE_BUCKETS:
        return jsonify(ok=False, error="intervallo non valido"), 400
    return jsonify(
        ok=True,
        points=history.query_power_history(range_key),
        contract_limit_w=history.CONTRACT_LIMIT_W,
        available_power_w=history.AVAILABLE_POWER_W,
        trip_risk_w=history.TRIP_RISK_W,
    )


@app.route("/api/climate/history")
def api_climate_history():
    range_key = request.args.get("range", "24h")
    if range_key not in history.RANGE_BUCKETS:
        return jsonify(ok=False, error="intervallo non valido"), 400
    return jsonify(ok=True, points=history.query_climate_history(range_key))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
