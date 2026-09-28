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

# An "off" shorter than this between two "on" samples isn't the system
# switching off and on again: a LocalHome restart, or one odd reading,
# leaves a gap of a sample or two, while a real valve cycle lasts far
# longer. The dashboard's list of runs uses the same rule (MIN_OFF_SECONDS
# in app.js), so its count agrees with on_stats().
MIN_OFF_SECONDS = 300

# Boundaries for the "on-hours" stats (see HistoryStore.on_stats) -
# a callable rather than a plain timestamp so it's evaluated at call time.
_ON_HOURS_RANGE_START = {
    "today": lambda now: datetime(now.year, now.month, now.day),
    "week": lambda now: datetime(now.year, now.month, now.day) - timedelta(days=now.weekday()),  # Monday
    "month": lambda now: datetime(now.year, now.month, 1),
}


class _Borrowed:
    """The store's one connection, lent to a single caller at a time:
    close() hands it back (releases the lock) instead of closing it."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        self._lock.release()


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        # on_stats()'s counts before today, per (sensor, field) - dropped
        # whenever rows are deleted. Read and written under _lock.
        self._on_stats_cache: dict[tuple[str, str], tuple] = {}
        self._init_schema()

    def _connect(self) -> _Borrowed:
        """One connection for the store's whole life, shared under a lock.
        A connection per call meant the last one closing checkpointed the
        whole WAL back into the database and deleted the -wal/-shm files,
        only for the next 30s cycle (or chart request) to recreate them -
        every cycle paid for the WAL and the database both, plus the file
        churn, on the board's eMMC. Held open, the WAL is folded back only
        every ~1000 pages (SQLite's auto-checkpoint)."""
        self._lock.acquire()
        try:
            if self._conn is None:
                # timeout: another process (the CLI, a backup) may hold the
                # database briefly - wait instead of failing the cycle.
                conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
                # With WAL (set once in _init_schema), NORMAL only fsyncs at
                # checkpoints instead of on every commit. A power cut can
                # lose the last few seconds of samples, never corrupt the
                # file - the right trade for a board meant to run for years
                # on eMMC/SD, where every fsync is flash wear.
                conn.execute("PRAGMA synchronous=NORMAL")
                # SQLite never shrinks the -wal file by itself: one large
                # transaction (a retention prune) left it at 25 MB on the
                # board, bigger than the database. Truncated back to this
                # after each checkpoint.
                conn.execute("PRAGMA journal_size_limit=8388608")
                self._conn = conn
            return _Borrowed(self._conn, self._lock)
        except BaseException:
            self._lock.release()
            raise

    def used_bytes(self) -> int:
        """Space the data takes inside the file - not counting pages freed
        by earlier deletes, which SQLite reuses before growing the file."""
        conn = self._connect()
        try:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        finally:
            conn.close()
        return (pages - free) * page_size

    def prune_to_size(self, max_bytes: int, keep_seconds: int = 86400) -> int:
        """Removes the oldest day of samples at a time until the data fits
        in `max_bytes` - but never the last `keep_seconds`. Returns the
        rows removed. The file stops growing rather than shrinking: freed
        pages are reused."""
        removed = 0
        started_over = self.used_bytes() > max_bytes
        while self.used_bytes() > max_bytes:
            conn = self._connect()
            try:
                oldest, newest = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
                if oldest is None or oldest >= newest - keep_seconds:
                    break
                cutoff = min(oldest + 86400, newest - keep_seconds)
                removed += conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
                conn.commit()
                self._on_stats_cache.clear()
            finally:
                conn.close()
        if started_over and removed:
            # Worth knowing months later: "the ceiling was reached on the
            # 3rd" explains a chart that stops there.
            self._set_meta("size_prune_ts", str(int(time.time())))
            self._set_meta("size_prune_rows", str(removed))
        return removed

    def _set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()

    def _meta(self) -> dict:
        conn = self._connect()
        try:
            return {key: value for key, value in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.OperationalError:
            return {}   # a database from before this table existed
        finally:
            conn.close()

    def clear(self) -> int:
        """Removes every sample and gives the space back to the disk."""
        conn = self._connect()
        try:
            removed = conn.execute("DELETE FROM readings").rowcount
            conn.commit()
            self._on_stats_cache.clear()
            conn.execute("VACUUM")
            return removed
        finally:
            conn.close()

    def backup_to(self, path: str) -> None:
        """A consistent copy of the database, taken while it stays in use."""
        conn = self._connect()
        try:
            target = sqlite3.connect(path)
            try:
                conn.backup(target)
            finally:
                target.close()
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _init_schema(self) -> None:
        import os

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = self._connect()
        try:
            # Persistent in the file itself. Also lets the dashboard's chart
            # queries read while a recording cycle is mid-write.
            conn.execute("PRAGMA journal_mode=WAL")
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
            # Notes about this database itself - currently only the last
            # time the size ceiling forced days out (see prune_to_size).
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.commit()
        finally:
            conn.close()

    def record(self, sensor: str, ts: int, fields: dict[str, float]) -> None:
        self.record_many(ts, {sensor: fields})

    def record_many(self, ts: int, samples: dict[str, dict[str, float]]) -> None:
        """Every sensor's fields for one cycle, in a single transaction.
        Recording sensor by sensor used to cost one connection and one
        commit each - ~40k commits a day on the real deployment, for rows
        that all share one timestamp anyway."""
        rows = [
            (ts, sensor, field, value)
            for sensor, fields in samples.items()
            for field, value in fields.items()
        ]
        if not rows:
            return
        conn = self._connect()
        try:
            conn.executemany("INSERT INTO readings (ts, sensor, field, value) VALUES (?, ?, ?, ?)", rows)
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
            if cur.rowcount:
                self._on_stats_cache.clear()
            return cur.rowcount
        finally:
            conn.close()

    def stats(self, now: int | None = None) -> dict:
        """Size and growth of the history, for the admin panel's system
        status: file size on disk (the database plus its WAL), total rows,
        rows written in the last 24h and the oldest sample kept. Scans the
        whole table - callers cache it rather than asking on every poll."""
        import os

        now = int(time.time()) if now is None else now
        file_bytes = 0
        for suffix in ("", "-wal"):
            try:
                file_bytes += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        conn = self._connect()
        try:
            rows, oldest_ts, newest_ts = conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM readings").fetchone()
            (rows_24h,) = conn.execute("SELECT COUNT(*) FROM readings WHERE ts >= ?", (now - 86400,)).fetchone()
            (sensors,) = conn.execute("SELECT COUNT(DISTINCT sensor) FROM readings").fetchone()
            # What the space is actually spent on: one row per kind of
            # measurement, biggest first. Only measurements are in here -
            # schedules, settings and the zone mapping are their own files.
            by_field = [{"field": field, "rows": count} for field, count in conn.execute(
                "SELECT field, COUNT(*) FROM readings GROUP BY field ORDER BY COUNT(*) DESC")]
        finally:
            conn.close()
        meta = self._meta()
        return {"file_bytes": file_bytes, "used_bytes": self.used_bytes(), "rows": rows, "rows_24h": rows_24h,
                "oldest_ts": oldest_ts, "newest_ts": newest_ts, "sensors": sensors, "by_field": by_field,
                "size_prune_ts": int(meta["size_prune_ts"]) if meta.get("size_prune_ts") else None,
                "size_prune_rows": int(meta["size_prune_rows"]) if meta.get("size_prune_rows") else None}

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

    def on_stats(self, sensor: str, field: str, sample_interval_seconds: float, now: datetime | None = None) -> dict:
        """For "today" (since local midnight), "week" (since Monday) and
        "month" (since the 1st): the hours `field` - a 0/1 recording, e.g.
        a valve's `is_on` - was on, and how many times it went from off to
        on. Hours count the samples at >= 0.5, each worth one recording
        interval: only accurate while `sample_interval_seconds` matches
        HistoryRecorder's cadence. A new start is an "on" sample more than
        MIN_OFF_SECONDS after the previous one - off, or not recorded at
        all (LocalHome or the board was off) - so a restart's blip isn't
        one.

        The dashboard asks every 30s per zone, and a month is ~90k samples
        per valve: what's before today can't change until midnight, so
        it's counted once a day (again after a clear or prune) and each
        call only reads today's samples."""
        now = now or datetime.now()
        starts = {key: int(start_of(now).timestamp()) for key, start_of in _ON_HOURS_RANGE_START.items()}
        today = starts["today"]
        min_off = max(MIN_OFF_SECONDS, 3 * sample_interval_seconds)
        cache_key = (sensor, field, today, starts["week"], starts["month"])
        with self._lock:  # a clear() can't slip between counting and caching
            before_today = self._on_stats_cache.get((sensor, field))
            if before_today is None or before_today[0] != cache_key:
                counts = self._count_on(sensor, field, {"week": starts["week"], "month": starts["month"]}, today, min_off)
                before_today = (cache_key, counts)
                self._on_stats_cache[(sensor, field)] = before_today
            today_counts = self._count_on(sensor, field, {"today": today}, int(now.timestamp()) + 1, min_off)["today"]
        totals = {
            "today": today_counts,
            "week": [a + b for a, b in zip(before_today[1]["week"], today_counts)],
            "month": [a + b for a, b in zip(before_today[1]["month"], today_counts)],
        }
        return {
            key: {"hours": samples * sample_interval_seconds / 3600.0, "starts": run_starts}
            for key, (samples, run_starts) in totals.items()
        }

    def _count_on(self, sensor: str, field: str, since: dict[str, int], until_ts: int, min_off: float) -> dict:
        """on_stats() for samples before `until_ts`: for each range in
        `since` (name -> start), the samples at >= 0.5 and the starts -
        only the "on" samples are read. The hour before the earliest start
        is read too, so a run carried over midnight (or Monday, or the 1st)
        isn't a new start."""
        columns = ", ".join(
            f"COALESCE(SUM(ts >= :{key}), 0), COALESCE(SUM(ts >= :{key} AND (after IS NULL OR after > :min_off)), 0)"
            for key in since
        )
        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT {columns} FROM (
                    SELECT ts, ts - LAG(ts) OVER (ORDER BY ts) AS after
                    FROM readings
                    WHERE sensor = :sensor AND field = :field AND ts >= :lookback AND ts < :until AND value >= 0.5
                )
                """,
                {**since, "sensor": sensor, "field": field, "min_off": min_off,
                 "lookback": min(since.values()) - 3600, "until": until_ts},
            ).fetchone()
        finally:
            conn.close()
        return {key: (row[2 * i], row[2 * i + 1]) for i, key in enumerate(since)}


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
        max_db_bytes: int | None = None,
    ):
        self.manager = manager
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self.power_budget = power_budget
        self.power_sensor = power_sensor
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.retention_days = retention_days
        # A hard ceiling on top of retention_days: more devices online than
        # planned can't make the history outgrow the board's storage.
        self.max_db_bytes = max_db_bytes
        self._last_size_check_ts = 0.0
        self._alert_active = False
        self._last_alert_ts = 0.0
        self._last_prune_ts = 0.0
        self._started = False
        # Optional callable -> {sensor_name: {field: value}} for values that
        # don't come from any driver's own reading - the thermostat's
        # applied per-zone target, set by create_app() once the thermostat
        # exists (it's built after this recorder).
        self.extra_samples = None
        # When the last cycle's samples were written - see
        # ThermostatController.last_tick_ok_ts for why this is tracked.
        self.last_write_ok_ts: float | None = None
        # Optional callable -> False while the board's clock can't be
        # trusted (not synchronized yet after a power-on, or no internet):
        # rows stamped with it would land hours or days in the past and
        # sit in the charts as if they'd happened then. Recording waits;
        # everything else in the cycle (the power-budget alert) goes on.
        self.clock_ready = None
        self.paused_for_clock = False

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
        batch: dict[str, dict[str, float]] = {}
        for name, poller in self.manager.pollers.items():
            fields = getattr(poller.driver, "history_fields", ())
            if not fields:
                continue
            reading = poller.get()
            if not reading.get("ok"):
                continue
            values = {f: reading[f] for f in fields if isinstance(reading.get(f), (int, float))}
            if values:
                batch[name] = values
            if name == self.power_sensor and "power_w" in values:
                self._check_power_budget(values["power_w"])
        if self.extra_samples is not None:
            try:
                for name, values in self.extra_samples().items():
                    batch.setdefault(name, {}).update(values)
            except Exception:
                logger.exception("Failed to collect extra history samples - recording the rest")
        if self.clock_ready is not None and not self.clock_ready():
            if not self.paused_for_clock:
                logger.warning("History paused: the board's clock isn't synchronized yet - "
                               "readings would be stamped with the wrong time")
                self.paused_for_clock = True
            self.last_write_ok_ts = time.time()   # paused on purpose, not stalled
            return
        if self.paused_for_clock:
            logger.info("History resumed: the clock is synchronized")
            self.paused_for_clock = False
        self.store.record_many(now, batch)
        self.last_write_ok_ts = time.time()
        self._maybe_prune(now)

    def _maybe_prune(self, now: int) -> None:
        if self.max_db_bytes and now - self._last_size_check_ts >= 3600:  # cheap: three PRAGMAs
            self._last_size_check_ts = now
            try:
                removed = self.store.prune_to_size(self.max_db_bytes)
                if removed:
                    logger.warning("History over its %d MB cap - removed its %d oldest rows",
                                   self.max_db_bytes // 1_000_000, removed)
            except Exception:
                logger.exception("Failed to enforce the history size cap")
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
