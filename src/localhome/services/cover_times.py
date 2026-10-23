"""Each cover's real travel time, up and down, set in the admin panel.

The curtain switches stop their motor on a timer set by hand with some
margin (a LoraTap module: 19 s for a shutter that takes about 16), so the
time they report is a little long, and a cover stopped half-way would read
lower than it is. The time measured once with a stopwatch, from the start
to the end of a run, puts the estimate right. Saved by cover name in
`cover_times.json` beside config.yaml; a cover with none set uses the time
its device reports.
"""
from __future__ import annotations

import json
import os
import threading

from localhome.util.jsonfile import load_json, save_json_atomic

DIRECTIONS = ("open", "close")
# Shutters, blinds, awnings: anything from a quick blind to a slow garage door.
MIN_SECONDS, MAX_SECONDS = 2, 300


class CoverTimes:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> dict:
        """{cover name: {"open": seconds, "close": seconds}} - what's set."""
        if not os.path.exists(self.path):
            return {}
        try:
            return load_json(self.path)
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, name: str, direction: str) -> float | None:
        return (self.all().get(name) or {}).get(direction)

    def put(self, name: str, times: dict) -> dict:
        """Sets a cover's times: each direction a number of seconds, or
        None to go back to the device's own. ValueError on a time out of
        range."""
        clean = {}
        for direction in DIRECTIONS:
            value = times.get(direction)
            if value in (None, ""):
                continue
            seconds = round(float(value), 1)
            if not MIN_SECONDS <= seconds <= MAX_SECONDS:
                raise ValueError(f"{name}: {direction} {seconds} s is outside {MIN_SECONDS}-{MAX_SECONDS} s")
            clean[direction] = seconds
        with self._lock:
            saved = self.all()
            if clean:
                saved[name] = clean
            else:
                saved.pop(name, None)
            save_json_atomic(self.path, saved)
        return clean
