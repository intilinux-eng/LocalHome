"""The house's long-term record, to understand how it behaves - and one day
to fit a model of it: every 5 minutes each zone's temperature, humidity,
target and how long its valve was open, the outdoor air, the VMC, the
dehumidifier and the bypass, the season and away; and every event at its
own time - a sensor's report, a relay switching and why, the season,
away, a zone held by hand.

Kept apart from the history (services/history.py), which stays as it is:
its own SQLite file (data/house_dataset.db), its own limit - 6 years, the
oldest rows go first - and nothing else in LocalHome reads it. It only
reads what the rest already knows: the device manager's readings, the
thermostat's targets and schedule, the dehumidifier's and the VMC's
status. At its first start it fills the past from the history, read
only (the history keeps a month). Exported as CSV: web/routes/dataset.py.

Times are UTC seconds (the clocks changing twice a year don't touch
them); a slot is the 5 minutes starting at its `ts`. What happened in a
slot is said as an average: a temperature's over the slot, a valve's as
the share of the slot it was open (0 to 1). Why a relay switched is
inferred from what was in force when it did - the season, away, a zone
held by hand, the diagnostic mode - not told by whoever switched it.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

SLOT_SECONDS = 300
SAMPLE_SECONDS = 30
RETENTION_YEARS = 6
PRUNE_EVERY_SECONDS = 24 * 3600
# The history keeps 30 days by default: the backfill looks no further.
BACKFILL_DAYS = 31
# A sensor's report is its reading's time moving on. The age it comes with
# is rounded to the second and read every 30 s: a few seconds of drift are
# the same message.
REPORT_JITTER_SECONDS = 3


@dataclass
class ZoneSource:
    name: str
    sensor: str
    valve: str
    cool_enabled: bool = True


@dataclass
class Plant:
    """What the record reads, by device name - None where this home has
    no such device. `vmc_speed` returns the VMC's speed device as the
    device list has it (its name and paired switch), or None."""
    zones: list[ZoneSource]
    outdoor: str | None = None
    vmc_speed: Callable[[], dict | None] = lambda: None
    dehumidifier: str | None = None
    bypass: str | None = None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class _Slot:
    """5 minutes being gathered: every sample's values, by column."""
    start: int
    samples: int = 0
    values: dict[str, list[float]] = field(default_factory=dict)
    last: dict[str, object] = field(default_factory=dict)
    flags: dict[str, set[str]] = field(default_factory=dict)

    def add(self, key: str, value) -> None:
        if value is not None:
            self.values.setdefault(key, []).append(float(value))

    def keep(self, key: str, value) -> None:
        if value is not None:
            self.last[key] = value

    def flag(self, owner: str, *names: str) -> None:
        self.flags.setdefault(owner, set()).update(names)

    def mean(self, key: str) -> float | None:
        return _mean(self.values.get(key, []))

    def any(self, key: str) -> int | None:
        values = self.values.get(key)
        return None if not values else int(max(values) > 0)

    def flags_of(self, owner: str) -> str | None:
        return ",".join(sorted(self.flags.get(owner, ()))) or None


class DatasetStore:
    """The file: 5-minute slots (the plant's and each zone's) and events.
    One connection for its whole life, as the history's, written once a
    slot - one small transaction every 5 minutes."""

    def __init__(self, path: str, retention_years: float = RETENTION_YEARS):
        self.path = path
        self.retention_seconds = retention_years * 365.25 * 24 * 3600
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._lock:
            conn = self._open()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS house_slots (
                    ts INTEGER PRIMARY KEY, samples INTEGER NOT NULL,
                    season TEXT, away INTEGER, diagnostic INTEGER,
                    outdoor_c REAL, outdoor_rh REAL, vmc_pct REAL, vmc_on REAL,
                    dehumidifier_on REAL, bypass_on REAL, flags TEXT
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS zone_slots (
                    ts INTEGER NOT NULL, zone TEXT NOT NULL,
                    temp_c REAL, rh REAL, target_c REAL, valve_open REAL, by_hand INTEGER, flags TEXT,
                    PRIMARY KEY (ts, zone)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS events (
                    ts REAL NOT NULL, device TEXT NOT NULL, what TEXT NOT NULL, value REAL, why TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            conn.commit()

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            # As the history's: a power cut loses the last seconds, never the file.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=8388608")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def write(self, house: dict | None, zones: list[dict], events: list[tuple]) -> None:
        with self._lock:
            conn = self._open()
            with conn:
                if house is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO house_slots VALUES (:ts, :samples, :season, :away, :diagnostic,"
                        " :outdoor_c, :outdoor_rh, :vmc_pct, :vmc_on, :dehumidifier_on, :bypass_on, :flags)", house)
                conn.executemany(
                    "INSERT OR REPLACE INTO zone_slots VALUES (:ts, :zone, :temp_c, :rh, :target_c, :valve_open,"
                    " :by_hand, :flags)", zones)
                conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", events)

    def prune(self, now: float) -> int:
        """The rows past the limit, gone: how many."""
        cutoff = now - self.retention_seconds
        with self._lock:
            conn = self._open()
            with conn:
                removed = conn.execute("DELETE FROM house_slots WHERE ts < ?", (cutoff,)).rowcount
                removed += conn.execute("DELETE FROM zone_slots WHERE ts < ?", (cutoff,)).rowcount
                removed += conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        return removed

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._open().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._open()
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def summary(self) -> dict:
        with self._lock:
            conn = self._open()
            slots, first, last = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM house_slots").fetchone()
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        size = sum(os.path.getsize(p) for p in (self.path, self.path + "-wal") if os.path.exists(p))
        return {"slots": slots, "events": events, "first_ts": first, "last_ts": last,
                "size_mb": round(size / 1e6, 2), "retention_years": round(self.retention_seconds / (365.25 * 86400), 1)}

    def zones(self, start: float, end: float) -> list[str]:
        """The zones with slots in the range, in the order first seen."""
        with self._lock:
            rows = self._open().execute(
                "SELECT zone, MIN(ts) FROM zone_slots WHERE ts >= ? AND ts < ? GROUP BY zone ORDER BY 2, 1",
                (start, end)).fetchall()
        return [r[0] for r in rows]

    def slots(self, start: float, end: float, batch: int = 2000):
        """Each slot in the range, oldest first: (the plant's row, {zone: its row}).
        Read a batch at a time: six years don't have to fit in memory."""
        after = start - 1
        while True:
            with self._lock:
                conn = self._open()
                conn.row_factory = sqlite3.Row
                try:
                    houses = conn.execute("SELECT * FROM house_slots WHERE ts > ? AND ts < ? ORDER BY ts LIMIT ?",
                                          (after, end, batch)).fetchall()
                    if not houses:
                        return
                    zone_rows = conn.execute("SELECT * FROM zone_slots WHERE ts >= ? AND ts <= ?",
                                             (houses[0]["ts"], houses[-1]["ts"])).fetchall()
                finally:
                    conn.row_factory = None
            by_ts: dict[int, dict] = {}
            for z in zone_rows:
                by_ts.setdefault(z["ts"], {})[z["zone"]] = dict(z)
            for h in houses:
                yield dict(h), by_ts.get(h["ts"], {})
            after = houses[-1]["ts"]

    def events(self, start: float, end: float, batch: int = 5000):
        after_rowid = 0
        while True:
            with self._lock:
                rows = self._open().execute(
                    "SELECT rowid, ts, device, what, value, why FROM events"
                    " WHERE rowid > ? AND ts >= ? AND ts < ? ORDER BY rowid LIMIT ?",
                    (after_rowid, start, end, batch)).fetchall()
            if not rows:
                return
            for row in rows:
                yield row[1:]
            after_rowid = rows[-1][0]

    def backfill_from_history(self, history_path: str, plant: Plant, until_ts: float,
                              days: float = BACKFILL_DAYS) -> int:
        """The slots before `until_ts` from the history, read only: its 30 s
        samples averaged per 5 minutes. The season, away and the events
        aren't in it - left empty, the rows flagged "backfill". How many
        slots were added."""
        if not os.path.exists(history_path):
            return 0
        start = until_ts - days * 86400
        uri = "file:" + os.path.abspath(history_path).replace("\\", "/") + "?mode=ro"
        history = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            def series(sensor: str | None, name: str) -> dict[int, float]:
                if not sensor:
                    return {}
                rows = history.execute(
                    f"SELECT (ts / {SLOT_SECONDS}) * {SLOT_SECONDS}, AVG(value) FROM readings"
                    " WHERE sensor = ? AND field = ? AND ts >= ? AND ts < ? GROUP BY 1",
                    (sensor, name, start, until_ts)).fetchall()
                return {int(ts): value for ts, value in rows}

            vmc = plant.vmc_speed() or {}
            house = {
                "outdoor_c": series(plant.outdoor, "temperature_c"),
                "outdoor_rh": series(plant.outdoor, "humidity_pct"),
                "vmc_pct": series(vmc.get("name"), "value"),
                "vmc_on": series(vmc.get("paired_switch"), "is_on"),
                "dehumidifier_on": series(plant.dehumidifier, "is_on"),
                "bypass_on": series(plant.bypass, "is_on"),
            }
            zones = {z.name: {"temp_c": series(z.sensor, "temperature_c"), "rh": series(z.sensor, "humidity_pct"),
                              "target_c": series(z.sensor, "target_c"), "valve_open": series(z.valve, "is_on")}
                     for z in plant.zones}
        finally:
            history.close()
        stamps = sorted({ts for columns in [house, *zones.values()] for s in columns.values() for ts in s})
        if not stamps:
            return 0
        house_rows = [{"ts": ts, "samples": 0, "season": None, "away": None, "diagnostic": None,
                       **{key: s.get(ts) for key, s in house.items()}, "flags": "backfill"} for ts in stamps]
        zone_rows = [{"ts": ts, "zone": name, **{key: s.get(ts) for key, s in columns.items()},
                      "by_hand": None, "flags": "backfill"}
                     for name, columns in zones.items() for ts in stamps
                     if any(ts in s for s in columns.values())]
        with self._lock:
            conn = self._open()
            with conn:
                # Never over what was recorded live.
                conn.executemany(
                    "INSERT OR IGNORE INTO house_slots VALUES (:ts, :samples, :season, :away, :diagnostic,"
                    " :outdoor_c, :outdoor_rh, :vmc_pct, :vmc_on, :dehumidifier_on, :bypass_on, :flags)", house_rows)
                conn.executemany(
                    "INSERT OR IGNORE INTO zone_slots VALUES (:ts, :zone, :temp_c, :rh, :target_c, :valve_open,"
                    " :by_hand, :flags)", zone_rows)
        return len(stamps)


class HouseDataset:
    """Looks at the house every 30 seconds, in a thread of its own, and
    writes a slot every 5 minutes with the events seen meanwhile."""

    def __init__(self, store: DatasetStore, manager, thermostat, plant: Plant, dehumidifier=None, vmc=None,
                 clock_synced: Callable[[], bool | None] = lambda: True, history_path: str | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.manager = manager
        self.thermostat = thermostat
        self.plant = plant
        self.dehumidifier = dehumidifier
        self.vmc = vmc
        self.clock_synced = clock_synced
        self.history_path = history_path
        self.clock = clock
        self._slot: _Slot | None = None
        self._events: list[tuple] = []
        self._seen: dict[str, object] = {}
        self._last_prune = 0.0
        self._started = False
        # The thread's looks and a download's checkpoint() take turns.
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="house-dataset").start()

    def _loop(self) -> None:
        try:
            self.backfill()
        except Exception:
            logger.exception("House dataset: filling the past from the history failed - going on without it")
        while True:
            try:
                self.sample()
            except Exception:
                logger.exception("House dataset: a look at the house failed, will retry")
            time.sleep(SAMPLE_SECONDS)

    def backfill(self) -> int:
        """Once in the file's life: the past the history still has."""
        if self.history_path is None or self.store.meta("backfilled"):
            return 0
        until = int(self.clock() // SLOT_SECONDS) * SLOT_SECONDS
        added = self.store.backfill_from_history(self.history_path, self.plant, until)
        self.store.set_meta("backfilled", str(until))
        if added:
            logger.info("House dataset: %d slots of the past filled from the history", added)
        return added

    # ------------------------------------------------------------ a look --

    def sample(self) -> None:
        with self._lock:
            self._look()

    def _look(self) -> None:
        now = self.clock()
        start = int(now // SLOT_SECONDS) * SLOT_SECONDS
        if self._slot is not None and self._slot.start != start:
            self.flush()
        if self._slot is None:
            self._slot = _Slot(start)
        slot = self._slot
        slot.samples += 1
        reading = self.manager.sensor_reading
        types = getattr(self.manager, "driver_types", {})
        schedule = self.thermostat.schedule_store if self.thermostat else None
        season = schedule.get_mode() if schedule else None
        away = schedule.get_away_until() is not None if schedule else False
        diagnostic = bool(schedule and schedule.get_diagnostic_mode())
        overrides = schedule.get_overrides(datetime.now()) if schedule and not away else {}
        slot.keep("season", season)
        slot.add("away", int(away))
        slot.add("diagnostic", int(diagnostic))
        if diagnostic:
            slot.flag("house", "diagnostic")
        if self.clock_synced() is False:
            slot.flag("house", "clock")
        self._plant_events(now, season, away, diagnostic)

        outdoor = reading(self.plant.outdoor) if self.plant.outdoor else {}
        if outdoor.get("ok"):
            slot.add("outdoor_c", outdoor.get("temperature_c"))
            slot.add("outdoor_rh", outdoor.get("humidity_pct"))
            if types.get(self.plant.outdoor) == "simulated":
                slot.flag("house", "simulated")
        self._reports(now, self.plant.outdoor, outdoor)

        vmc = self.plant.vmc_speed()
        if vmc:
            speed, power = reading(vmc["name"]), reading(vmc.get("paired_switch") or "")
            on = power.get("is_on") if power.get("ok") else None
            if on is not None:
                slot.add("vmc_on", int(bool(on)))
            if speed.get("ok") and speed.get("value") is not None:
                slot.add("vmc_pct", speed["value"] if on is not False else 0)
                self._change(now, vmc["name"], "speed", speed["value"], self._vmc_why)
            self._switched(now, vmc.get("paired_switch"), power, self._vmc_why)
        for key, name, why in (("dehumidifier_on", self.plant.dehumidifier, self._dehumidifier_why),
                               ("bypass_on", self.plant.bypass, lambda on: "diagnostic" if diagnostic else "season")):
            if not name:
                continue
            state = reading(name)
            if state.get("ok") and state.get("is_on") is not None:
                slot.add(key, int(bool(state["is_on"])))
            self._switched(now, name, state, why)

        for zone in self.plant.zones:
            sensor, valve = reading(zone.sensor), reading(zone.valve)
            key = f"zone:{zone.name}"
            if sensor.get("ok"):
                slot.add(f"{key}:temp_c", sensor.get("temperature_c"))
                slot.add(f"{key}:rh", sensor.get("humidity_pct"))
            target = self.thermostat.applied_target(zone.name) if self.thermostat else None
            slot.keep(f"{key}:target_c", target)
            if target is not None:
                slot.add(f"{key}:has_target", 1)
            if valve.get("ok") and valve.get("is_on") is not None:
                slot.add(f"{key}:valve_open", int(bool(valve["is_on"])))
            slot.add(f"{key}:by_hand", int(zone.name in overrides))
            if "simulated" in (types.get(zone.sensor), types.get(zone.valve)):
                slot.flag(key, "simulated")
            if types.get(zone.sensor) == "manual_override":
                slot.flag(key, "manual")
            if self.thermostat and self.thermostat.is_failsafe_active(zone.name):
                slot.flag(key, "failsafe")
            if diagnostic:
                slot.flag(key, "diagnostic")
            self._reports(now, zone.sensor, sensor)
            # The Shelly's own timer acting (no renewal came): its time moved
            # while watched - not one from before a start.
            rest, rest_before = valve.get("safety_rest_at"), self._seen.get(("rest", zone.valve), "unseen")
            self._seen[("rest", zone.valve)] = rest
            timer = rest_before != "unseen" and rest is not None and rest != rest_before
            self._switched(now, zone.valve, valve,
                           lambda on, z=zone, t=timer: self._valve_why(z, on, t, season, away, diagnostic, overrides))
            self._change(now, zone.name, "by_hand", 1 if zone.name in overrides else 0,
                         lambda value, z=zone: self._held_detail(overrides.get(z.name)))
        if now - self._last_prune >= PRUNE_EVERY_SECONDS:
            self._last_prune = now
            removed = self.store.prune(now)
            if removed:
                logger.info("House dataset: %d rows past the limit removed", removed)

    def flush(self) -> None:
        """The slot gathered so far, written with the events seen meanwhile."""
        with self._lock:
            slot, events = self._slot, self._events
            self._slot, self._events = None, []
            self._write(slot, events)

    def checkpoint(self) -> None:
        """What's gathered so far written now, the slot going on: a download
        then has the house up to this moment. The slot's row is written
        again, whole, when it ends; the events written now aren't kept back
        to be written twice."""
        with self._lock:
            events, self._events = self._events, []
            self._write(self._slot, events)

    def _write(self, slot: _Slot | None, events: list[tuple]) -> None:
        if slot is None or not slot.samples:
            if events:
                self.store.write(None, [], events)
            return
        house = {"ts": slot.start, "samples": slot.samples, "season": slot.last.get("season"),
                 "away": slot.any("away"), "diagnostic": slot.any("diagnostic"),
                 "outdoor_c": slot.mean("outdoor_c"), "outdoor_rh": slot.mean("outdoor_rh"),
                 "vmc_pct": slot.mean("vmc_pct"), "vmc_on": slot.mean("vmc_on"),
                 "dehumidifier_on": slot.mean("dehumidifier_on"), "bypass_on": slot.mean("bypass_on"),
                 "flags": slot.flags_of("house")}
        zones = []
        for zone in self.plant.zones:
            key = f"zone:{zone.name}"
            zones.append({"ts": slot.start, "zone": zone.name, "temp_c": slot.mean(f"{key}:temp_c"),
                          "rh": slot.mean(f"{key}:rh"),
                          "target_c": slot.last.get(f"{key}:target_c") if slot.any(f"{key}:has_target") else None,
                          "valve_open": slot.mean(f"{key}:valve_open"), "by_hand": slot.any(f"{key}:by_hand"),
                          "flags": slot.flags_of(key)})
        self.store.write(house, zones, events)

    # ------------------------------------------------------------ events --

    def _event(self, ts: float, device: str, what: str, value, why: str | None) -> None:
        self._events.append((round(ts, 1), device, what, None if value is None else float(value), why))

    def _reports(self, now: float, name: str | None, reading: dict) -> None:
        """A sensor's new message, at the time it arrived: its values."""
        if not name or not reading.get("ok") or reading.get("seconds_since_update") is None:
            return
        at = now - reading["seconds_since_update"]
        before = self._seen.get(("report", name))
        self._seen[("report", name)] = at
        if before is None or at <= before + REPORT_JITTER_SECONDS:
            return     # the first look after a start, or the same message
        for what in ("temperature_c", "humidity_pct"):
            if reading.get(what) is not None:
                self._event(at, name, what, reading[what], "report")

    def _switched(self, now: float, name: str | None, reading: dict, why: Callable[[bool], str]) -> None:
        if not name or not reading.get("ok") or reading.get("is_on") is None:
            return
        on = bool(reading["is_on"])
        before = self._seen.get(("on", name))
        self._seen[("on", name)] = on
        if before is not None and before != on:
            self._event(now, name, "on" if on else "off", int(on), why(on))

    def _change(self, now: float, device: str, what: str, value, why: Callable) -> None:
        before = self._seen.get((what, device), "unseen")
        self._seen[(what, device)] = value
        if before != "unseen" and before != value:
            self._event(now, device, what, value, why(value))

    def _plant_events(self, now: float, season, away: bool, diagnostic: bool) -> None:
        self._change(now, "plant", "season", None if season is None else (1 if season == "heat" else 0),
                     lambda value: season)
        self._change(now, "plant", "away", int(away), lambda value: "away" if away else "back")
        self._change(now, "plant", "diagnostic", int(diagnostic), lambda value: "diagnostic")

    @staticmethod
    def _held_detail(override: dict | None) -> str:
        if not override:
            return "schedule"
        target = override.get("target")
        return "off" if target is None else f"{target:g}"

    def _valve_why(self, zone: ZoneSource, on: bool, timer: bool, season, away, diagnostic, overrides) -> str:
        if diagnostic:
            return "diagnostic"
        if not on and timer:
            return "safety_timer"
        if away:
            return "away"
        if season == "cool" and not zone.cool_enabled:
            return "dehumidifier"
        if zone.name in overrides:
            return "by_hand"
        if self.thermostat and self.thermostat.is_failsafe_active(zone.name):
            return "failsafe"
        return "schedule"

    def _dehumidifier_why(self, on: bool) -> str:
        status = self.dehumidifier.status() if self.dehumidifier else {}
        if on:
            return status.get("mode") or "outside"
        return (status.get("stopped") or {}).get("why") or "off"

    def _vmc_why(self, value) -> str:
        status = self.vmc.status() if self.vmc else {}
        if status.get("away"):
            return "away"
        if status.get("override"):
            return "timed"
        if status.get("auto"):
            return f"auto:{status['auto'].get('reason')}"
        return "program" if status.get("enabled") else "by_hand"
