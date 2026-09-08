import json
import os
import threading

POSITIONS_FILE = "positions.json"

_lock = threading.Lock()
_active_timers = {}


def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return {}
    with open(POSITIONS_FILE) as f:
        return json.load(f)


def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def get_position(shutter, default=100):
    return load_positions().get(shutter["id"], default)


def is_calibrated(shutter):
    return shutter["id"] in load_positions()


def set_position(shutter, percent):
    positions = load_positions()
    positions[shutter["id"]] = percent
    save_positions(positions)


def read_travel_time(device, shutter, default=15):
    try:
        status = device.status()
        dps = status.get("dps", {})
        if "10" in dps:
            return float(dps["10"])
    except Exception:
        pass
    dp10 = shutter.get("mapping", {}).get("10", {})
    return float(dp10.get("values", {}).get("max", default))


def cancel_pending_move(device_id):
    with _lock:
        timer = _active_timers.pop(device_id, None)
    if timer:
        timer.cancel()


def move_to_percent(shutter, target_percent, get_device_fn):
    """0% = fully closed, 100% = fully open. Position is a best-effort
    estimate: these modules don't report real physical position, so we
    derive movement time from the configured travel_time (DP 10)."""
    device_id = shutter["id"]
    target_percent = max(0, min(100, int(target_percent)))

    cancel_pending_move(device_id)

    device = get_device_fn(shutter)
    current = get_position(shutter)
    delta = target_percent - current

    if abs(delta) < 1:
        return {"moved": False, "duration": 0, "position": current}

    travel_time = read_travel_time(device, shutter)
    duration = abs(delta) / 100.0 * travel_time
    direction = "open" if delta > 0 else "close"

    device.set_value(1, direction)

    def finish():
        try:
            get_device_fn(shutter).set_value(1, "stop")
        finally:
            set_position(shutter, target_percent)
            with _lock:
                _active_timers.pop(device_id, None)

    timer = threading.Timer(duration, finish)
    with _lock:
        _active_timers[device_id] = timer
    timer.start()

    return {"moved": True, "duration": duration, "direction": direction, "position": target_percent}
