"""Best-effort open/closed *position* (0-100%) for covers that only
report a raw motor command (open/close/stop), not an absolute position -
true of most inexpensive curtain-switch modules. Position is derived from
how long the motor has been told to run for, calibrated against each
device's own reported travel time. This works against any CoverDriver,
regardless of brand.

Also detects moves triggered by something other than this app (e.g. a
voice-assistant routine talking to the device directly) well enough to
keep the estimate roughly in sync: if the motor starts moving without us
having sent that command, and later stops, assume it ran to completion.
"""
from __future__ import annotations

import json
import os
import threading
import time

from localhome.core.interfaces import CoverDriver

OWN_COMMAND_GRACE_SECONDS = 60


class PositionStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._active_timers: dict[str, threading.Timer] = {}
        self._own_command_until: dict[str, float] = {}
        self._last_seen_state: dict[str, str] = {}
        self._pending_external_move: dict[str, str] = {}

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, positions: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)

    def get(self, cover_id: str, default: int = 100) -> int:
        return self._load().get(cover_id, default)

    def is_calibrated(self, cover_id: str) -> bool:
        return cover_id in self._load()

    def set(self, cover_id: str, percent: int) -> None:
        positions = self._load()
        positions[cover_id] = percent
        self._save(positions)

    def cancel_pending_move(self, cover_id: str) -> None:
        with self._lock:
            timer = self._active_timers.pop(cover_id, None)
        if timer:
            timer.cancel()

    def mark_own_command(self, cover_id: str, grace_seconds: float = OWN_COMMAND_GRACE_SECONDS) -> None:
        """Record that a move for `cover_id` was just triggered by this
        app, so observe_state() doesn't mistake it for an
        externally-triggered move."""
        with self._lock:
            self._own_command_until[cover_id] = time.time() + grace_seconds

    def observe_state(self, cover: CoverDriver, state: str) -> None:
        """Feed a freshly-polled raw motor state in, to catch covers moved
        by something other than this app. These devices report only the
        current motor command, not an absolute position, so the best we
        can do is: if we see a full open/close start that we didn't
        initiate, and then see the motor go back to stop, assume it ran
        to completion and record the resulting position as 100/0."""
        cover_id = cover.id
        with self._lock:
            previous = self._last_seen_state.get(cover_id)
            self._last_seen_state[cover_id] = state
            if previous == state:
                return

            if state in ("open", "close"):
                until = self._own_command_until.get(cover_id)
                own_move = cover_id in self._active_timers or (until is not None and time.time() < until)
                direction = None
                if not own_move:
                    self._pending_external_move[cover_id] = state
            elif state == "stop":
                direction = self._pending_external_move.pop(cover_id, None)
            else:
                direction = None

        if direction:
            self.set(cover_id, 100 if direction == "open" else 0)

    def move_to_percent(self, cover: CoverDriver, target_percent: int) -> dict:
        """0% = fully closed, 100% = fully open. Position is a
        best-effort estimate: derived from the configured travel time,
        not a real sensor reading."""
        cover_id = cover.id
        target_percent = max(0, min(100, int(target_percent)))

        self.cancel_pending_move(cover_id)

        current = self.get(cover_id)
        delta = target_percent - current
        if abs(delta) < 1:
            return {"moved": False, "duration": 0, "position": current}

        travel_time = cover.read_travel_time()
        duration = abs(delta) / 100.0 * travel_time
        direction = "open" if delta > 0 else "close"

        def finish():
            try:
                cover.send("stop")
            finally:
                self.set(cover_id, target_percent)
                with self._lock:
                    self._active_timers.pop(cover_id, None)

        # Register the timer (marking this as our own move) before sending
        # the command, so a concurrent observe_state() poll can't see the
        # motor start moving and mistake it for an externally-triggered move.
        timer = threading.Timer(duration, finish)
        with self._lock:
            self._active_timers[cover_id] = timer
        cover.send(direction)
        timer.start()

        return {"moved": True, "duration": duration, "direction": direction, "position": target_percent}
