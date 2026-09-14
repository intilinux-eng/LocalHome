"""Generic time-series recorder + query API for any PollingDriver's
numeric fields, plus an optional power-budget alert for one power meter.

The schema is one row per (sensor, field, timestamp) rather than one
column per metric, so adding a new kind of sensor never requires a
migration: whatever numeric fields a driver's `read()` dict declares in
`history_fields` just start showing up as new rows.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Bucket sizes chosen so each range renders a reasonable number of points.
RANGE_BUCKETS = {
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 24 * 3600, 3600),
}

# Boundaries for the "on-hours" stats (see HistoryStore.sum_on_hours_range) -
# a callable rather than a plain timestamp so it's evaluated at call time.
_ON_HOURS_RANGE_START = {
    "today": lambda now: datetime(now.year, now.month, now.day),
    "week": lambda now: datetime(now.year, now.month, now.day) - timedelta(days=now.weekday()),  # Monday
    "month": lambda now: datetime(now.year, now.month, 1),
}


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        import os

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    ts INTEGER NOT NULL,
                    sensor TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_sensor_field_ts ON readings(sensor, field, ts)")
            conn.commit()
        finally:
            conn.close()

    def record(self, sensor: str, ts: int, fields: dict[str, float]) -> None:
        if not fields:
            return
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO readings (ts, sensor, field, value) VALUES (?, ?, ?, ?)",
                [(ts, sensor, field, value) for field, value in fields.items()],
            )
            conn.commit()
        finally:
            conn.close()

    def query(self, sensor: str, field: str, range_key: str) -> list[dict]:
        window_seconds, bucket_seconds = RANGE_BUCKETS.get(range_key, RANGE_BUCKETS["24h"])
        since = int(time.time()) - window_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT (ts / ?) * ? AS bucket, AVG(value)
                FROM readings
                WHERE sensor = ? AND field = ? AND ts >= ?
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                (bucket_seconds, bucket_seconds, sensor, field, since),
            ).fetchall()
        finally:
            conn.close()
        return [{"ts": row[0], "value": round(row[1], 2)} for row in rows]

    def prune(self, older_than_ts: int) -> int:
        """Deletes readings older than older_than_ts. The dashboard only
        ever queries up to a 7-day window, but nothing removed rows past
        that on its own - record() just keeps inserting, so the table
        would otherwise grow for as long as the app stays up, not just
        for 7 days. Called periodically by HistoryRecorder, not on every
        poll (a DELETE scan has no reason to run every 30s)."""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM readings WHERE ts < ?", (older_than_ts,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def query_peak_today(self, sensor: str, field: str) -> dict:
        now = time.localtime()
        start_of_today = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst))
        since = int(time.mktime(start_of_today))
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value, ts FROM readings WHERE sensor = ? AND field = ? AND ts >= ? "
                "ORDER BY value DESC LIMIT 1",
                (sensor, field, since),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"value": 0.0, "ts": None}
        return {"value": row[0], "ts": row[1]}

    def sum_on_hours(self, sensor: str, field: str, since_ts: int, until_ts: int, sample_interval_seconds: float) -> float:
        """Approximate hours `field` (a boolean-as-0/1 recording, e.g. a
        valve's `is_on`) was "on" between since_ts and until_ts: counts
        recorded samples at value >= 0.5 and multiplies by how much time
        each sample represents. Only accurate if `sample_interval_seconds`
        matches the actual recording cadence (HistoryRecorder's
        poll_interval_seconds) - a slower/faster recorder would
        under/overcount, since this has no way to tell a genuine gap in
        readings from time that was simply never sampled."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE sensor = ? AND field = ? AND ts >= ? AND ts < ? AND value >= 0.5",
                (sensor, field, since_ts, until_ts),
            ).fetchone()
        finally:
            conn.close()
        samples_on = row[0] if row else 0
        return samples_on * sample_interval_seconds / 3600.0

    def sum_on_hours_range(self, sensor: str, field: str, range_key: str, sample_interval_seconds: float) -> float:
        """Same as sum_on_hours(), for one of the fixed ranges dashboard
        cards show: "today" (since local midnight), "week" (since Monday
        midnight) or "month" (since the 1st, midnight)."""
        start_of = _ON_HOURS_RANGE_START.get(range_key)
        if start_of is None:
            raise ValueError(f"range_key must be one of {sorted(_ON_HOURS_RANGE_START)}")
        since_ts = int(start_of(datetime.now()).timestamp())
        return self.sum_on_hours(sensor, field, since_ts, int(time.time()) + 1, sample_interval_seconds)


@dataclass
class PowerBudget:
    """A three-zone tolerance model for a home power meter: many
    residential meters/breakers allow indefinite draw up to a "safe"
    limit, tolerate a higher "trip risk" limit for a while before
    tripping, and trip quickly above that. The ratios below are a
    reasonable generic default modeled after typical European
    residential contracts - check your own meter/provider/breaker
    documentation for the numbers that actually apply to you and set
    `contract_limit_w` (and, if needed, the ratios) in config.yaml.
    """

    contract_limit_w: float
    safe_margin: float = 1.10
    trip_risk_ratio: float = 4 / 3

    @property
    def available_power_w(self) -> float:
        return self.contract_limit_w * self.safe_margin

    @property
    def trip_risk_w(self) -> float:
        return self.contract_limit_w * self.trip_risk_ratio


class HistoryRecorder:
    """Background loop: polls the device manager's sensors, writes their
    declared `history_fields` to a HistoryStore, and raises/clears a
    power-budget alert through the manager's notifier."""

    def __init__(
        self,
        manager,
        store: HistoryStore,
        poll_interval_seconds: float = 30,
        power_budget: PowerBudget | None = None,
        power_sensor: str | None = None,
        alert_cooldown_seconds: float = 600,
        retention_days: float = 30,
    ):
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self.power_budget = power_budget
        self.power_sensor = power_sensor
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.retention_days = retention_days
        self._alert_active = False
        self._last_alert_ts = 0.0
        self._last_prune_ts = 0.0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                # Without this, one failed write (e.g. a transient SQLite
                # "database is locked" under concurrent access) killed this
                # thread forever - history recording would silently stop
                # for the rest of the process's life, with nothing else
                # about the app showing anything wrong. Same self-healing
                # shape as ThermostatController/InterlockController's loops.
                logger.exception("History recorder loop failed, will retry next interval")
            time.sleep(self.poll_interval_seconds)

    def poll_once(self) -> None:
        """One poll-everything-and-record cycle, split out of _loop() so a
        test can drive several cycles directly instead of starting a real
        thread and sleeping through real poll intervals to observe them."""
        now = int(time.time())
        for name, poller in self.manager.pollers.items():
            fields = getattr(poller.driver, "history_fields", ())
            if not fields:
                continue
            reading = poller.get()
            if not reading.get("ok"):
                continue
            values = {f: reading[f] for f in fields if isinstance(reading.get(f), (int, float))}
            self.store.record(name, now, values)
            if name == self.power_sensor and "power_w" in values:
                self._check_power_budget(values["power_w"])
        self._maybe_prune(now)

    def _maybe_prune(self, now: int) -> None:
        if now - self._last_prune_ts < 86400:  # once a day is plenty for a DELETE scan
            return
        self._last_prune_ts = now
        try:
            removed = self.store.prune(now - int(self.retention_days * 86400))
            if removed:
                logger.info("Pruned %d history rows older than %s days", removed, self.retention_days)
        except Exception:
            logger.exception("Failed to prune old history rows")

    def _check_power_budget(self, power_w: float) -> None:
        if self.power_budget is None:
            return
        now = time.time()
        threshold = self.power_budget.available_power_w
        if power_w >= threshold:
            if not self._alert_active or (now - self._last_alert_ts) >= self.alert_cooldown_seconds:
                self.manager.notify(f"High power draw: {power_w:.0f} W (safe limit {threshold:.0f} W)")
                self._last_alert_ts = now
            self._alert_active = True
        elif self._alert_active:
            self.manager.notify(f"Power draw back to normal: {power_w:.0f} W")
            self._alert_active = False
