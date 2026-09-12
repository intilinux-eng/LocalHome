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

logger = logging.getLogger(__name__)

# Bucket sizes chosen so each range renders a reasonable number of points.
RANGE_BUCKETS = {
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 24 * 3600, 3600),
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
    ):
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self.power_budget = power_budget
        self.power_sensor = power_sensor
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self._alert_active = False
        self._last_alert_ts = 0.0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
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
            time.sleep(self.poll_interval_seconds)

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
