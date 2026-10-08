"""Today's lowest and highest value of a few readings - the outdoor
temperature's minimum and maximum on the home page.

Kept in memory and fed by the history recorder's own cycle (the same
samples it writes), not read from the history at every page refresh: a
history read writes out the rows waiting in memory first
(HistoryStore._connect()), and a page asking every 20 s would bring back
the frequent small writes the write-behind is there to avoid. The
history is read once, at start-up, for what today already had; from
then on each cycle's values update it, and the first one after midnight
starts the new day.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _midnight(ts: float) -> int:
    return int(datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class DayRange:
    def __init__(self, watched: list[tuple[str, str]], clock=time.time):
        self.watched = set(watched)
        self.clock = clock
        self._lock = threading.Lock()
        self._ranges: dict[tuple[str, str], dict] = {}

    def seed(self, store) -> None:
        """What today already had, from the history - once, at start-up."""
        now = self.clock()
        for sensor, field in self.watched:
            for ts, value in store.values_since(sensor, field, _midnight(now)):
                self._add(sensor, field, ts, value)

    def sample(self, ts: float, batch: dict[str, dict[str, float]]) -> None:
        """One cycle's values: {sensor: {field: value}}."""
        for sensor, values in batch.items():
            for field, value in values.items():
                if (sensor, field) in self.watched:
                    self._add(sensor, field, ts, value)

    def _add(self, sensor: str, field: str, ts: float, value: float) -> None:
        day = _day(ts)
        with self._lock:
            current = self._ranges.get((sensor, field))
            if current is None or current["day"] != day:
                self._ranges[(sensor, field)] = {"day": day, "min": value, "max": value, "count": 1}
                return
            current["min"] = min(current["min"], value)
            current["max"] = max(current["max"], value)
            current["count"] += 1

    def get(self, sensor: str, field: str) -> dict | None:
        """{"min", "max"} so far today - None before two values today (one
        says nothing a reading doesn't), or when today has none yet."""
        with self._lock:
            current = self._ranges.get((sensor, field))
            if current is None or current["day"] != _day(self.clock()) or current["count"] < 2:
                return None
            return {"min": round(current["min"], 1), "max": round(current["max"], 1)}
