"""Best-effort open/closed *position* (0-100%) for covers that only
report a raw motor command (open/close/stop), not an absolute position -
true of most inexpensive curtain-switch modules. Position is derived from
how long the motor ran, against each cover's travel time: the one set in
the admin panel for that direction (measured with a stopwatch), else the
time the device itself reports. This works against any CoverDriver,
regardless of brand.

Every reported change goes through observe_state(), whoever moved the
cover - LocalHome, a wall button, an app: an open or close starts a run, the
next state ends it, and the run's length says how far it got. A run that
lasted the whole travel time reached the end (the curtain switches stop
their motor on a timer a little longer than the run, so their "stop"
after a full run means fully open or closed). A cover never seen making a
full run has no known position until it does: a part run from an unknown
place says nothing about where it ended.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from localhome.core.interfaces import CoverDriver
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

OWN_COMMAND_GRACE_SECONDS = 60
# A move not given by LocalHome (a wall button) is reported about this
# late: measured on a LoraTap module, 1.2 s after the press.
EXTERNAL_REPORT_LAG_SECONDS = 1.2


class PositionStore:
    def __init__(self, path: str, travel_times=None):
        """`travel_times(cover, direction)`: the measured seconds for a run
        that way, or None to use the time the device reports."""
        self.path = path
        self._travel_times = travel_times
        self._lock = threading.Lock()
        self._active_timers: dict[str, threading.Timer] = {}
        self._active_moves: dict[str, dict] = {}
        self._own_command_until: dict[str, float] = {}
        self._last_seen_state: dict[str, str] = {}
        self._runs: dict[str, dict] = {}

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s - treating all covers as uncalibrated", self.path)
            return {}

    def _save(self, positions: dict) -> None:
        save_json_atomic(self.path, positions)

    def get(self, cover_id: str, default: int = 100) -> int:
        """Current best-effort position. While a move is still running,
        this interpolates from where it started by the time elapsed,
        rather than returning the pre-move value until the motor stops -
        otherwise a status poll mid-move looks like the cover hasn't
        budged yet."""
        value = self.where(cover_id)
        return default if value is None else value

    def where(self, cover_id: str) -> int | None:
        """Like get(), but None when the position isn't known."""
        with self._lock:
            move = self._active_moves.get(cover_id)
            run = self._runs.get(cover_id)
        if move is not None:
            frac = min(1.0, (time.time() - move["start"]) / move["duration"]) if move["duration"] > 0 else 1.0
            return round(move["start_percent"] + (move["target_percent"] - move["start_percent"]) * frac)
        if run is not None:
            return self._run_position(run, time.time())
        return self._load().get(cover_id)

    def is_calibrated(self, cover_id: str) -> bool:
        return cover_id in self._load()

    def set(self, cover_id: str, percent: int) -> None:
        # Unlike get()'s read-only _load() (safe to leave unlocked -
        # save_json_atomic()'s rename means a concurrent reader only
        # ever sees a fully-old or fully-new file, never a torn one),
        # this is read-modify-write: two covers finishing their moves
        # close enough together (each on its own threading.Timer, see
        # move_to_percent()) could otherwise both read the same
        # on-disk snapshot and the second save() would silently
        # overwrite the first cover's just-written position.
        with self._lock:
            positions = self._load()
            positions[cover_id] = percent
            self._save(positions)

    def cancel_pending_move(self, cover_id: str) -> None:
        with self._lock:
            timer = self._active_timers.pop(cover_id, None)
            self._active_moves.pop(cover_id, None)
        if timer:
            timer.cancel()

    def mark_own_command(self, cover_id: str, grace_seconds: float = OWN_COMMAND_GRACE_SECONDS) -> None:
        """Record that a move for `cover_id` was just triggered by this
        app: observe_state() then takes its start as reported on time, not
        late like a wall button's."""
        with self._lock:
            self._own_command_until[cover_id] = time.time() + grace_seconds

    def travel_time(self, cover: CoverDriver, direction: str) -> float:
        """Seconds for a full run that way: measured, else the device's own."""
        measured = self._travel_times(cover, direction) if self._travel_times else None
        if measured:
            return float(measured)
        return self.device_travel_time(cover)

    @staticmethod
    def device_travel_time(cover: CoverDriver) -> float:
        """The run time the device itself reports (a Tuya module's timer,
        remembered by its driver once it has said it)."""
        return float(cover.read_travel_time())

    @staticmethod
    def _run_position(run: dict, at: float) -> int | None:
        if run["start_percent"] is None:
            return None
        sign = 1 if run["direction"] == "open" else -1
        moved = (at - run["start"]) / run["travel"] * 100 if run["travel"] > 0 else 100
        return round(max(0.0, min(100.0, run["start_percent"] + sign * moved)))

    def run_of(self, cover_id: str) -> dict | None:
        """The run going on, if any: {direction, start, travel}."""
        with self._lock:
            run = self._runs.get(cover_id)
        return dict(run) if run else None

    def observe_state(self, cover: CoverDriver, state: str, at: float | None = None) -> None:
        """A motor command the cover reported (heard as it happened, or
        polled), whoever gave it: an open or close starts a run from where
        the cover is, the next state ends it where the time says."""
        now = time.time() if at is None else at
        cover_id = cover.id
        with self._lock:
            previous = self._last_seen_state.get(cover_id)
            self._last_seen_state[cover_id] = state
            if previous == state or cover_id in self._active_timers:
                # Nothing new; or a move_to_percent() move, which knows its target.
                return
            run = self._runs.pop(cover_id, None)
            until = self._own_command_until.get(cover_id)
            own = until is not None and now < until
        if run is not None:
            elapsed = now - run["start"]
            if elapsed >= run["travel"]:
                self.set(cover_id, 100 if run["direction"] == "open" else 0)
            else:
                ended = self._run_position(run, now)
                if ended is not None:
                    self.set(cover_id, ended)
        if state in ("open", "close"):
            start = now if own else now - EXTERNAL_REPORT_LAG_SECONDS
            travel = self.travel_time(cover, state)
            start_percent = self._load().get(cover_id)
            with self._lock:
                self._runs[cover_id] = {"direction": state, "start": start, "start_percent": start_percent,
                                        "travel": travel}

    def move_to_percent(self, cover: CoverDriver, target_percent: int) -> dict:
        """0% = fully closed, 100% = fully open. Position is a
        best-effort estimate: derived from the travel time, not a real
        sensor reading."""
        cover_id = cover.id
        target_percent = max(0, min(100, int(target_percent)))

        self.cancel_pending_move(cover_id)

        current = self.get(cover_id)
        delta = target_percent - current
        if abs(delta) < 1:
            return {"moved": False, "duration": 0, "position": current}

        direction = "open" if delta > 0 else "close"
        duration = abs(delta) / 100.0 * self.travel_time(cover, direction)

        def finish():
            try:
                cover.send("stop")
            finally:
                self.set(cover_id, target_percent)
                with self._lock:
                    self._active_timers.pop(cover_id, None)
                    self._active_moves.pop(cover_id, None)

        # Register the timer (marking this as our own move) before sending
        # the command, so a concurrent observe_state() can't see the motor
        # start moving and take it for a run of its own.
        timer = threading.Timer(duration, finish)
        with self._lock:
            self._active_timers[cover_id] = timer
            self._runs.pop(cover_id, None)
            self._active_moves[cover_id] = {
                "start": time.time(),
                "start_percent": current,
                "target_percent": target_percent,
                "duration": duration,
            }
        cover.send(direction)
        timer.start()

        return {"moved": True, "duration": duration, "direction": direction, "position": target_percent}
