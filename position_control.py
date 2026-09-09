import json
import os
import threading
import time

POSITIONS_FILE = "positions.json"
OWN_COMMAND_GRACE_SECONDS = 60

_lock = threading.Lock()
_active_timers = {}
_own_command_until = {}
_last_seen_state = {}
_pending_external_move = {}


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


def mark_own_command(device_id, grace_seconds=OWN_COMMAND_GRACE_SECONDS):
    """Record that a move for `device_id` was just triggered by this app
    (not, e.g., an Alexa routine acting directly on the device), so
    observe_state() doesn't mistake it for an externally-triggered move."""
    with _lock:
        _own_command_until[device_id] = time.time() + grace_seconds


def is_own_move(device_id):
    with _lock:
        if device_id in _active_timers:
            return True
        until = _own_command_until.get(device_id)
        return until is not None and time.time() < until


def observe_state(shutter, state):
    """Feed a freshly-polled raw motor state ("open"/"close"/"stop") in, to
    catch shutters moved by something other than this app (e.g. an Alexa
    routine talking to the device directly). These curtain switches don't
    report an absolute position, only the current motor command, so the
    best we can do is: if we see a full open/close start that we didn't
    initiate, and then see the motor go back to stop, assume it ran to
    completion and record the resulting position as 100/0."""
    device_id = shutter["id"]

    with _lock:
        previous = _last_seen_state.get(device_id)
        _last_seen_state[device_id] = state
        if previous == state:
            return

        if state in ("open", "close"):
            until = _own_command_until.get(device_id)
            own_move = device_id in _active_timers or (until is not None and time.time() < until)
            if not own_move:
                _pending_external_move[device_id] = state
            direction = None
        elif state == "stop":
            direction = _pending_external_move.pop(device_id, None)
        else:
            direction = None

    if direction:
        set_position(shutter, 100 if direction == "open" else 0)


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

    def finish():
        try:
            get_device_fn(shutter).set_value(1, "stop")
        finally:
            set_position(shutter, target_percent)
            with _lock:
                _active_timers.pop(device_id, None)

    # Register the timer (marking this as our own move) before sending the
    # command, so a concurrent observe_state() poll can't see the motor
    # start moving and mistake it for an externally-triggered move.
    timer = threading.Timer(duration, finish)
    with _lock:
        _active_timers[device_id] = timer
    device.set_value(1, direction)
    timer.start()

    return {"moved": True, "duration": duration, "direction": direction, "position": target_percent}
