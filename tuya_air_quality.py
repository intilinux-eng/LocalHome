"""Background poller for the Tuya air quality sensor (9-in-1, category
"hjjcy"). Unlike the temperature/humidity sensor, this one broadcasts on
the LAN (protocol v3.5) and can be polled directly and quickly, no cloud
API involved.
"""
import json
import threading
import time

import tinytuya

DEVICES_FILE = "devices.json"
SENSOR_CATEGORY = "hjjcy"
POLL_INTERVAL_SECONDS = 30

# dps id -> (field name, value transform)
FIELDS = {
    "1": ("air_quality_index", lambda v: v),
    "2": ("temperature_c", lambda v: v / 10),
    "3": ("humidity_pct", lambda v: v),
    "4": ("co2_ppm", lambda v: v),
    "5": ("ch2o_mgm3", lambda v: v / 1000),
    "6": ("voc_mgm3", lambda v: v / 1000),
    "7": ("pm25_ugm3", lambda v: v),
    "9": ("pm10_ugm3", lambda v: v),
    "22": ("battery_pct", lambda v: v),
}

_started = False
_lock = threading.Lock()
_latest = {"ok": False, "error": "not started yet"}
_last_update_ts = None


def start_background():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_poll_loop, daemon=True).start()


def _find_sensor():
    devices = json.load(open(DEVICES_FILE))
    for d in devices:
        if d.get("category") == SENSOR_CATEGORY and d.get("ip"):
            return d
    return None


def _poll_loop():
    global _latest, _last_update_ts

    while True:
        sensor = _find_sensor()
        if sensor is None:
            with _lock:
                _latest = {"ok": False, "error": "no hjjcy sensor with an ip in devices.json"}
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        try:
            device = tinytuya.Device(sensor["id"], sensor["ip"], sensor["key"], version=sensor.get("version", 3.5))
            device.set_socketTimeout(5)
            status = device.status()
            dps = status.get("dps")
            if not dps:
                raise RuntimeError(status.get("Error") or f"unexpected response: {status}")

            reading = {"ok": True, "name": sensor.get("name")}
            for dp_id, (key, transform) in FIELDS.items():
                if dp_id in dps:
                    reading[key] = transform(dps[dp_id])

            with _lock:
                _latest = reading
                _last_update_ts = time.time()
        except Exception as exc:
            with _lock:
                _latest = {"ok": False, "error": str(exc)}

        time.sleep(POLL_INTERVAL_SECONDS)


def get_air_quality_reading():
    with _lock:
        reading = dict(_latest)
    reading["seconds_since_update"] = None if _last_update_ts is None else round(time.time() - _last_update_ts)
    return reading
