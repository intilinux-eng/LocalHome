"""Generic time-series recorder + query API for any PollingDriver's
numeric fields, plus an optional power-budget alert for one power meter.

The schema is one row per (sensor, field, timestamp) rather than one
column per metric, so adding a new kind of sensor never requires a
migration: whatever numeric fields a driver's `read()` dict declares in
`history_fields` just start showing up as new rows.
"""
from __future__ import annotations

import atexit
import logging
import math
import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Samples wait in memory and reach the file together, at most this often.
# A 30 s cycle committed on its own rewrote the last index page of every
# sensor's field, and each meter's day row twice - some 100 KB a cycle
# for 2 KB of rows: measured on the production board, the history was
# most of LocalHome's ~0.6 GB a day on the eMMC. Ten cycles in one
# transaction rewrite those pages once. Whoever reads the history writes
# what's waiting first, so the charts, the hours run and the reports see
# every sample as before; a stop or a restart writes it on the way out. A
# power cut loses at most this much history.
WRITE_EVERY_SECONDS = 300
# Writes failing for hours (a disk gone read-only): past this many rows
# waiting - some 8 hours of a house's samples, ~3 MB, nothing next to a
# 2 GB UNO Q's memory - the oldest are dropped instead of the memory
# growing for ever. Normally 5 minutes wait: ~200 rows, a few tens of KB.
MAX_PENDING_ROWS = 20_000

_open_stores: "weakref.WeakSet[HistoryStore]" = weakref.WeakSet()


@atexit.register
def _write_waiting() -> None:
    """Stopping (web/app.py turns systemd's SIGTERM into a normal exit):
    every store's waiting samples are written before going."""
    for store in list(_open_stores):
        try:
            store.flush()
        except Exception:
            logger.warning("History: the last samples couldn't be written on the way out", exc_info=True)

# Bucket sizes chosen so each range renders a reasonable number of points.
RANGE_BUCKETS = {
    "6h": (6 * 3600, 60),
    "24h": (24 * 3600, 300),
    "7d": (7 * 24 * 3600, 3600),
}

# An "off" shorter than this between two "on" samples isn't the system
# switching off and on again: a LocalHome restart, or one odd reading,
# leaves a gap of a sample or two, while a real valve cycle lasts far
# longer.
MIN_OFF_SECONDS = 300

# daily_runs' key for the plant as a whole: any zone valve open, counted
# once however many are - what a heat meter's kWh go with.
PLANT_KEY = "*plant*"
# daily_runs' and daily_temps' years: this one and the five before.
RUN_YEARS = 6
# Pruning in pieces (HistoryStore.prune_some): rows per transaction, and
# the most a recorder's cycle spends on it before recording again - far
# under the 90 s (three 30 s intervals) after which its loop is "stalled".
PRUNE_CHUNK_ROWS = 20000
PRUNE_SECONDS_PER_CYCLE = 10
# A meter's own past days (a Meross plug keeps a month): read once a day,
# and again every 10 minutes until it has worked - right after a start the
# plug's cloud connection isn't up yet.
DEVICE_DAYS_EVERY_SECONDS = 86400
DEVICE_DAYS_RETRY_SECONDS = 600

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
    def __init__(self, db_path: str, write_every_seconds: float = WRITE_EVERY_SECONDS):
        self.db_path = db_path
        self.write_every_seconds = write_every_seconds
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        # Writes not in the file yet - [(sql, [params, ...])], in order -
        # since _pending_since (monotonic). Under _lock.
        self._pending: list[tuple[str, list[tuple]]] = []
        self._pending_rows = 0
        self._pending_since: float | None = None
        self._dropped_rows = 0
        # on_stats()'s counts before today, per (sensor, field) - dropped
        # whenever rows are deleted. Read and written under _lock.
        self._on_stats_cache: dict[tuple[str, str], tuple] = {}
        # oldest_ts(), once known - forgotten whenever rows are deleted.
        # Under _lock.
        self._oldest: int | None = None
        self._oldest_known = False
        self._init_schema()
        _open_stores.add(self)

    def _connect(self) -> _Borrowed:
        """One connection for the store's whole life, shared under a lock.
        A connection per call meant the last one closing checkpointed the
        whole WAL back into the database and deleted the -wal/-shm files,
        only for the next 30s cycle (or chart request) to recreate them -
        every cycle paid for the WAL and the database both, plus the file
        churn, on the board's eMMC. Held open, the WAL is folded back only
        every ~1000 pages (SQLite's auto-checkpoint). What's waiting to be
        written goes in first: every reader sees every sample."""
        self._lock.acquire()
        try:
            conn = self._open()
            if self._pending:
                try:
                    self._write_pending(conn)
                except sqlite3.Error:
                    # A reader still gets what's in the file; the samples
                    # wait for the next try (the recorder's own write says
                    # it's failing).
                    logger.exception("History: couldn't write the waiting samples - kept for the next try")
            return _Borrowed(conn, self._lock)
        except BaseException:
            self._lock.release()
            raise

    def _open(self) -> sqlite3.Connection:
        """The store's connection, opened the first time. Under _lock."""
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
        return self._conn

    # ------------------------------------------------ waiting writes ----

    def _queue(self, sql: str, rows: list[tuple]) -> None:
        """A write, kept until it's due with the others (WRITE_EVERY_SECONDS)."""
        with self._lock:
            self._pending.append((sql, rows))
            self._pending_rows += len(rows)
            if self._pending_since is None:
                self._pending_since = time.monotonic()
            while self._pending_rows > MAX_PENDING_ROWS and len(self._pending) > 1:
                _, dropped = self._pending.pop(0)
                self._pending_rows -= len(dropped)
                if not self._dropped_rows:
                    logger.warning("History: writes have been failing for a long time - the oldest "
                                   "waiting samples are being dropped")
                self._dropped_rows += len(dropped)
            if time.monotonic() - self._pending_since >= self.write_every_seconds:
                self.flush()

    def flush(self) -> None:
        """Writes whatever is waiting, now, in one transaction. Raises if
        it can't - it stays waiting for the next try."""
        with self._lock:
            if self._pending:
                self._write_pending(self._open())

    def _write_pending(self, conn: sqlite3.Connection) -> None:
        """Under _lock."""
        try:
            for sql, rows in self._pending:
                try:
                    conn.executemany(sql, rows)
                except sqlite3.IntegrityError:
                    # A row the table refuses would hold every other one
                    # back for ever: it loses only its own write.
                    logger.exception("History: a write was refused - dropped")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._pending, self._pending_rows, self._pending_since = [], 0, None
        self._dropped_rows = 0

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

    def prune_to_size(self, max_bytes: int, keep_seconds: int = 86400, max_seconds: float | None = None) -> int:
        """Removes the oldest day of samples at a time until the data fits
        in `max_bytes` - but never the last `keep_seconds`. Returns the
        rows removed. The file stops growing rather than shrinking: freed
        pages are reused. With `max_seconds`, it stops once that's spent,
        still over if it is - the recorder asks again (see prune_some())."""
        removed = 0
        started = time.monotonic()
        started_over = self.used_bytes() > max_bytes
        while self.used_bytes() > max_bytes:
            if max_seconds is not None and removed and time.monotonic() - started >= max_seconds:
                break
            conn = self._connect()
            try:
                oldest, newest = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
                if oldest is None or oldest >= newest - keep_seconds:
                    break
                cutoff = min(oldest + 86400, newest - keep_seconds)
                removed += conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
                conn.commit()
                self._forget_counts()
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
        """Removes every sample - and every day's energy and run time - and
        gives the space back to the disk."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM daily_energy")
            conn.execute("DELETE FROM daily_runs")
            conn.execute("DELETE FROM daily_temps")
            removed = conn.execute("DELETE FROM readings").rowcount
            conn.commit()
            self._forget_counts()
            conn.execute("VACUUM")
            # VACUUM writes the compacted database to the WAL: the file
            # only shrinks at a checkpoint, which the store's open
            # connection makes every ~1000 pages - on the board a cleared
            # history kept its 280 MB file. Folded back at once.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
            try:
                self.flush()
            except sqlite3.Error:
                logger.exception("History: the waiting samples couldn't be written before closing")
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
            # Each power meter's energy and peak, one row a day, never
            # pruned - a week, a month, the year so far (services/energy.py).
            # `source`: "meter" (its own counter) or "power" (integrated).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_energy (
                    sensor TEXT NOT NULL,
                    day TEXT NOT NULL,
                    kwh REAL,
                    source TEXT,
                    peak_w REAL,
                    peak_ts INTEGER,
                    PRIMARY KEY (sensor, day)
                )
                """
            )
            # Each zone valve's time open and its starts, and the plant's
            # (PLANT_KEY), one row a day, never pruned - see run_days().
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_runs (
                    key TEXT NOT NULL,
                    day TEXT NOT NULL,
                    seconds REAL NOT NULL,
                    starts INTEGER NOT NULL,
                    PRIMARY KEY (key, day)
                )
                """
            )
            # A sensor's day - mean, lowest, highest - kept six years, like
            # daily_runs: the outdoor temperature beside the plant's hours
            # (services/run_hours.py).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_temps (
                    sensor TEXT NOT NULL,
                    day TEXT NOT NULL,
                    mean_c REAL NOT NULL,
                    min_c REAL NOT NULL,
                    max_c REAL NOT NULL,
                    min_at INTEGER,
                    max_at INTEGER,
                    PRIMARY KEY (sensor, day)
                )
                """
            )
            # When the lowest and the highest came - a table from before
            # they were kept gets them, empty for the days it has.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_temps)")}
            for column in ("min_at", "max_at"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE daily_temps ADD COLUMN {column} INTEGER")
            conn.commit()
        finally:
            conn.close()

    def record(self, sensor: str, ts: int, fields: dict[str, float]) -> None:
        self.record_many(ts, {sensor: fields})

    def record_many(self, ts: int, samples: dict[str, dict[str, float]]) -> None:
        """Every sensor's fields for one cycle, written with the others when
        they're due (WRITE_EVERY_SECONDS). Recording sensor by sensor used
        to cost one connection and one commit each - ~40k commits a day on
        the real deployment, for rows that all share one timestamp anyway.
        A value that isn't a number (NaN, infinite) is left out: the table
        refuses it, and it used to take the whole cycle with it."""
        rows = [
            (ts, sensor, field, value)
            for sensor, fields in samples.items()
            for field, value in fields.items()
            if not (isinstance(value, float) and not math.isfinite(value))
        ]
        if rows:
            self._queue("INSERT INTO readings (ts, sensor, field, value) VALUES (?, ?, ?, ?)", rows)

    # ------------------------------------------------- daily energy ----

    def _day_write(self, sql: str, params: tuple) -> None:
        self._queue(sql, [params])

    def set_day(self, sensor: str, day: str, kwh: float, source: str) -> None:
        """A day's kWh as a meter's own counter says it - replaced, not added."""
        self._day_write(
            "INSERT INTO daily_energy (sensor, day, kwh, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(sensor, day) DO UPDATE SET kwh=excluded.kwh, source=excluded.source",
            (sensor, day, kwh, source))

    def add_day_kwh(self, sensor: str, day: str, kwh: float) -> None:
        """kWh worked out from the power, added to the day's."""
        self._day_write(
            "INSERT INTO daily_energy (sensor, day, kwh, source) VALUES (?, ?, ?, 'power') "
            "ON CONFLICT(sensor, day) DO UPDATE SET kwh=COALESCE(daily_energy.kwh, 0) + excluded.kwh, source='power' "
            "WHERE daily_energy.source IS NULL OR daily_energy.source = 'power'",
            (sensor, day, kwh))

    def raise_day_peak(self, sensor: str, day: str, power_w: float, ts: int) -> None:
        self._day_write(
            "INSERT INTO daily_energy (sensor, day, peak_w, peak_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(sensor, day) DO UPDATE SET peak_w=excluded.peak_w, peak_ts=excluded.peak_ts "
            "WHERE daily_energy.peak_w IS NULL OR excluded.peak_w > daily_energy.peak_w",
            (sensor, day, power_w, ts))

    def energy_days(self, sensor: str, since: str = "") -> dict[str, dict]:
        """{day: {"kwh", "source", "peak_w", "peak_ts"}} from `since` on."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT day, kwh, source, peak_w, peak_ts FROM daily_energy WHERE sensor = ? AND day >= ? ORDER BY day",
                (sensor, since)).fetchall()
        finally:
            conn.close()
        return {day: {"kwh": None if kwh is None else round(kwh, 3), "source": source,
                      "peak_w": peak_w, "peak_ts": peak_ts} for day, kwh, source, peak_w, peak_ts in rows}

    def first_energy_day(self, sensor: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT MIN(day) FROM daily_energy WHERE sensor = ?", (sensor,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

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

    def values_since(self, sensor: str, field: str, since_ts: int) -> list[tuple[int, float]]:
        """Every recorded value, oldest first - no buckets: for the few,
        sparse fields read whole (services/shelly_health.py)."""
        conn = self._connect()
        try:
            return [(row[0], row[1]) for row in conn.execute(
                "SELECT ts, value FROM readings WHERE sensor = ? AND field = ? AND ts >= ? ORDER BY ts ASC",
                (sensor, field, since_ts),
            )]
        finally:
            conn.close()

    def prune_days(self, before_day: date) -> int:
        """The valves' days and the temperatures' before `before_day` go:
        six years are kept."""
        with self._lock:
            conn = self._connect()
            try:
                removed = sum(conn.execute(f"DELETE FROM {table} WHERE day < ?", (before_day.isoformat(),)).rowcount
                              for table in ("daily_runs", "daily_temps"))
                conn.commit()
                return removed
            finally:
                conn.close()

    def prune(self, older_than_ts: int) -> int:
        """Deletes readings older than older_than_ts. The dashboard only
        ever queries up to a 7-day window, but nothing removed rows past
        that on its own - record() just keeps inserting, so the table
        would otherwise grow for as long as the app stays up, not just
        for 7 days. Called periodically by HistoryRecorder, not on every
        poll (a DELETE scan has no reason to run every 30s)."""
        removed, _ = self.prune_some(older_than_ts)
        return removed

    def prune_some(self, older_than_ts: int, max_seconds: float | None = None) -> tuple[int, bool]:
        """prune(), PRUNE_CHUNK_ROWS at a time, each in its own transaction -
        with `max_seconds`, stopping once that's spent: (rows removed, all
        done). Measured on the board: three million rows in one DELETE held
        the recorder's thread 85 s, a breath from the 90 that make its loop
        "stalled" and systemd restart LocalHome - which rolled the DELETE
        back, to start it again at the next start. In chunks the recorder
        goes on between them, and a restart keeps what was done."""
        started = time.monotonic()
        removed = 0
        while True:
            conn = self._connect()
            try:
                # Rows come in time order: the oldest are found first.
                count = conn.execute("DELETE FROM readings WHERE rowid IN "
                                     "(SELECT rowid FROM readings WHERE ts < ? LIMIT ?)",
                                     (older_than_ts, PRUNE_CHUNK_ROWS)).rowcount
                conn.commit()
            finally:
                conn.close()
            removed += count
            if count:
                self._forget_counts()
            if count < PRUNE_CHUNK_ROWS:
                return removed, True
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                return removed, False

    def _forget_counts(self) -> None:
        """Rows were deleted: what's remembered about them is counted again."""
        self._on_stats_cache.clear()
        self._oldest_known = False

    def oldest_ts(self) -> int | None:
        """When the oldest sample was taken - whether the samples still
        cover a day. Remembered: MIN(ts) reads the whole table (there's no
        index on the time alone), and the 7 days' tables asked it at every
        page refresh - with a year's history, most of their time on the
        board. Only a deletion can change it: new samples are newer."""
        with self._lock:
            if not self._oldest_known or self._oldest is None:   # none yet: until the first
                conn = self._connect()
                try:
                    self._oldest = conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
                finally:
                    conn.close()
                self._oldest_known = True
            return self._oldest

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


def _day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp())


def _on_in(conn, sensors: list[str], field: str, t0: int, t1: int, min_off: float) -> dict[str, tuple[int, int]]:
    """{sensor: (samples on, starts)} between t0 and t1, and PLANT_KEY's:
    the cycles with any of `sensors` on - one recording cycle writes them
    all with one timestamp, so a cycle is counted once however many are
    open. A start is an "on" sample more than `min_off` after the one
    before; the hour before t0 is read so a run carried over isn't one."""
    counted = """
        SELECT COUNT(*), COALESCE(SUM(after IS NULL OR after > :min_off), 0) FROM (
            SELECT ts, ts - LAG(ts) OVER (ORDER BY ts) AS after FROM ({source})
        ) WHERE ts >= :t0"""
    params = {"field": field, "t0": t0, "t1": t1, "lookback": t0 - 3600, "min_off": min_off}
    out = {}
    for sensor in sensors:
        row = conn.execute(counted.format(source="SELECT ts FROM readings WHERE sensor = :sensor AND field = :field "
                                                 "AND ts >= :lookback AND ts < :t1 AND value >= 0.5"),
                           {**params, "sensor": sensor}).fetchone()
        out[sensor] = (row[0], row[1])
    marks = ", ".join(f":s{i}" for i in range(len(sensors))) or "NULL"
    row = conn.execute(counted.format(source=f"SELECT DISTINCT ts FROM readings WHERE sensor IN ({marks}) "
                                             "AND field = :field AND ts >= :lookback AND ts < :t1 AND value >= 0.5"),
                       {**params, **{f"s{i}": sensor for i, sensor in enumerate(sensors)}}).fetchone()
    out[PLANT_KEY] = (row[0], row[1])
    return out


def temp_days_of(store: "HistoryStore", sensor: str, field: str, sample_interval_seconds: float,
                 first_day: date, now: datetime | None = None) -> dict[str, tuple]:
    """{"YYYY-MM-DD": (mean, lowest, highest, lowest at, highest at)} of
    `sensor`'s `field` from `first_day` to today - the times the first
    moment each was reached, epoch seconds (None for a day kept before
    they were). Days before today come from daily_temps, filled
    from the samples the first time they're asked while the samples cover
    them and hold at least half the day's cycles (a sensor silent for most
    of a day gives no mean worth comparing); today from the samples so far.
    A day neither kept nor covered is missing."""
    now = now or datetime.now()
    today = now.date()
    enough = 86400 / sample_interval_seconds / 2
    out: dict[str, tuple] = {}
    stats = ("SELECT AVG(value), MIN(value), MAX(value), COUNT(*) FROM readings "
             "WHERE sensor = ? AND field = ? AND ts >= ? AND ts < ?")
    first_at = ("SELECT MIN(ts) FROM readings WHERE sensor = ? AND field = ? AND ts >= ? AND ts < ? AND value = ?")

    def day_of(conn, t0: int, t1: int):
        mean, low, high, count = conn.execute(stats, (sensor, field, t0, t1)).fetchone()
        if not count:
            return None, 0
        low_at = conn.execute(first_at, (sensor, field, t0, t1, low)).fetchone()[0]
        high_at = conn.execute(first_at, (sensor, field, t0, t1, high)).fetchone()[0]
        return (round(mean, 2), low, high, low_at, high_at), count

    with store._lock:
        conn = store._connect()
        try:
            for day, mean, low, high, low_at, high_at in conn.execute(
                    "SELECT day, mean_c, min_c, max_c, min_at, max_at FROM daily_temps WHERE sensor = ? AND day >= ?",
                    (sensor, first_day.isoformat())):
                out[day] = (mean, low, high, low_at, high_at)
            oldest = store.oldest_ts()
            day = first_day
            stored = False
            while day < today:
                iso = day.isoformat()
                start = _day_start(day)
                if iso not in out and oldest is not None and oldest <= start:
                    kept, count = day_of(conn, start, _day_start(day + timedelta(days=1)))
                    if count >= enough:
                        out[iso] = kept
                        conn.execute("INSERT OR REPLACE INTO daily_temps (sensor, day, mean_c, min_c, max_c, min_at, max_at) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (sensor, iso, *kept))
                        stored = True
                day += timedelta(days=1)
            if stored:
                conn.commit()
            if first_day <= today:
                kept, count = day_of(conn, _day_start(today), int(now.timestamp()) + 1)
                if count:
                    out[today.isoformat()] = kept
        finally:
            conn.close()
    return out


def first_run_day(store: "HistoryStore") -> str | None:
    """The oldest day daily_runs keeps, if any."""
    with store._lock:
        conn = store._connect()
        try:
            return conn.execute("SELECT MIN(day) FROM daily_runs").fetchone()[0]
        finally:
            conn.close()


def run_days_of(store: "HistoryStore", sensors: list[str], field: str, sample_interval_seconds: float,
                first_day: date, now: datetime | None = None) -> dict[str, dict[str, tuple[float, int]]]:
    """{key: {"YYYY-MM-DD": (seconds on, starts)}} from `first_day` to
    today, for each of `sensors` and PLANT_KEY. Days before today come
    from daily_runs, filled from the samples the first time they're asked
    while the samples still cover them whole - the samples last a month,
    these for good; today from the samples, each time. A day neither kept
    nor covered (before LocalHome ran, or past the samples) is missing."""
    now = now or datetime.now()
    today = now.date()
    keys = [*sensors, PLANT_KEY]
    min_off = max(MIN_OFF_SECONDS, 3 * sample_interval_seconds)
    out: dict[str, dict[str, tuple[float, int]]] = {key: {} for key in keys}
    with store._lock:
        conn = store._connect()
        try:
            marks = ", ".join("?" for _ in keys)
            for key, day, seconds, starts in conn.execute(
                    f"SELECT key, day, seconds, starts FROM daily_runs WHERE key IN ({marks}) AND day >= ?",
                    (*keys, first_day.isoformat())):
                out[key][day] = (seconds, starts)
            oldest = store.oldest_ts()
            day = first_day
            stored = False
            while day < today:
                iso = day.isoformat()
                start = _day_start(day)
                # Whole in the samples: kept from now on.
                if any(iso not in out[key] for key in keys) and oldest is not None and oldest <= start:
                    counts = _on_in(conn, sensors, field, start, _day_start(day + timedelta(days=1)), min_off)
                    for key, (samples, starts) in counts.items():
                        out[key][iso] = (samples * sample_interval_seconds, starts)
                        conn.execute("INSERT OR REPLACE INTO daily_runs (key, day, seconds, starts) VALUES (?, ?, ?, ?)",
                                     (key, iso, samples * sample_interval_seconds, starts))
                    stored = True
                day += timedelta(days=1)
            if stored:
                conn.commit()
            if first_day <= today:
                counts = _on_in(conn, sensors, field, _day_start(today), int(now.timestamp()) + 1, min_off)
                for key, (samples, starts) in counts.items():
                    out[key][today.isoformat()] = (samples * sample_interval_seconds, starts)
        finally:
            conn.close()
    return out


def run_window_of(store: "HistoryStore", sensors: list[str], field: str, sample_interval_seconds: float,
                  seconds: int, now: datetime | None = None) -> dict[str, tuple[float, int]]:
    """{key: (seconds on, starts)} over the last `seconds` - the last 24
    hours - for each of `sensors` and PLANT_KEY."""
    now = now or datetime.now()
    until = int(now.timestamp()) + 1
    min_off = max(MIN_OFF_SECONDS, 3 * sample_interval_seconds)
    conn = store._connect()
    try:
        counts = _on_in(conn, sensors, field, until - 1 - seconds, until, min_off)
    finally:
        conn.close()
    return {key: (samples * sample_interval_seconds, starts) for key, (samples, starts) in counts.items()}


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
        # Rows removed so far by a day's prune still under way - None when
        # none is (see _maybe_prune()).
        self._pruned: int | None = None
        self._started = False
        # Optional callable -> {sensor_name: {field: value}} for values that
        # don't come from any driver's own reading - the thermostat's
        # applied per-zone target, set by create_app() once the thermostat
        # exists (it's built after this recorder).
        self.extra_samples = None
        # Each power meter's day - energy and peak - kept for good
        # (services/energy.py); set by create_app().
        self.energy = None
        # Optional callable: the valves' and the outdoor temperature's last
        # days into daily_runs and daily_temps before their samples go -
        # set by create_app() (services/run_hours.py).
        self.keep_runs = None
        # When the last cycle's samples were written, on the monotonic
        # clock - see ThermostatController.last_tick_ok_ts for why.
        self.last_write_ok_ts: float | None = None
        # Optional callable -> False while the board's clock can't be
        # trusted (not synchronized yet after a power-on, or no internet):
        # rows stamped with it would land hours or days in the past and
        # sit in the charts as if they'd happened then. Recording waits;
        # everything else in the cycle (the power-budget alert) goes on.
        self.clock_ready = None
        self.paused_for_clock = False
        # Optional callable -> system_status.disk_stats() of the disk the
        # history is on, set by create_app(): while that disk is low on
        # space, whatever filled it, the history stops growing - see
        # _check_disk().
        self.disk_stats = None
        # Optional services/day_range.py DayRange, set by create_app(): each
        # cycle's values feed today's minimum and maximum, in memory.
        self.day_range = None
        self.disk_hold_bytes: int | None = None
        self.disk_hold_since: float | None = None
        # When the meters' own past days are read next (import_device_energy()).
        self._device_days_due = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def backfill_energy(self) -> None:
        """Each power meter's days already in the history, before the first
        cycle adds to today (services/energy.py's backfill())."""
        if self.energy is None:
            return
        for name, poller in self.manager.pollers.items():
            if "power_w" in getattr(poller.driver, "history_fields", ()):
                try:
                    self.energy.backfill(name)
                except Exception:
                    logger.exception("Energy: couldn't work out %s's past days", name)

    def import_device_energy(self) -> dict[str, int]:
        """Each meter that keeps its own past days - a Meross plug's month,
        its driver's past_days_kwh() - into the ledger: once a day, from
        the first cycle the plug answers, and when asked (POST
        /api/energy/import, tools/import_meter_history.py - after the
        history was cleared by hand). {meter: days written}; a plug not
        connected yet raises."""
        done = {}
        for name, poller in self.manager.pollers.items():
            fetch = getattr(poller.driver, "past_days_kwh", None)
            if self.energy is not None and callable(fetch):
                done[name] = self.energy.import_device_days(name, fetch())
        return done

    def _maybe_import_device_energy(self) -> None:
        """import_device_energy() when it's due, beside the cycle: a cloud's
        answer takes seconds the samples mustn't wait. Until it has worked,
        tried again every DEVICE_DAYS_RETRY_SECONDS."""
        if self.energy is None or time.monotonic() < self._device_days_due:
            return
        if not any(callable(getattr(p.driver, "past_days_kwh", None)) for p in self.manager.pollers.values()):
            self._device_days_due = time.monotonic() + DEVICE_DAYS_EVERY_SECONDS
            return
        self._device_days_due = time.monotonic() + DEVICE_DAYS_RETRY_SECONDS

        def run():
            try:
                self.import_device_energy()
                self._device_days_due = time.monotonic() + DEVICE_DAYS_EVERY_SECONDS
            except Exception as exc:  # noqa: BLE001 - a plug not connected yet: next time
                logger.info("Energy: a device's past days not read yet (%s) - again in %d min",
                            exc, DEVICE_DAYS_RETRY_SECONDS // 60)

        threading.Thread(target=run, daemon=True).start()

    def _loop(self) -> None:
        # Each sample stands for one interval - the hours on are the "on"
        # samples times it (on_stats(), run_days_of()): the next is due an
        # interval after this one began, not after it ended, so a cycle that
        # writes or prunes (up to PRUNE_SECONDS_PER_CYCLE) doesn't stretch it
        # and leave those hours short.
        while True:
            began = time.monotonic()
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
            time.sleep(max(0.0, self.poll_interval_seconds - (time.monotonic() - began)))

    def poll_once(self) -> None:
        """One poll-everything-and-record cycle, split out of _loop() so a
        test can drive several cycles directly instead of starting a real
        thread and sleeping through real poll intervals to observe them."""
        now = int(time.time())
        batch: dict[str, dict[str, float]] = {}
        meters: dict[str, dict] = {}
        for name, poller in self.manager.pollers.items():
            fields = getattr(poller.driver, "history_fields", ())
            if not fields:
                continue
            reading = poller.get()
            if not reading.get("ok"):
                continue
            # Anything that measures its power - a meter, a plug - gets
            # its days kept (services/energy.py).
            if "power_w" in fields:
                meters[name] = reading
            values = {f: reading[f] for f in fields if isinstance(reading.get(f), (int, float))}
            if values:
                batch[name] = values
            if name == self.power_sensor and "power_w" in values:
                self._check_power_budget(values["power_w"])
        self._maybe_import_device_energy()
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
            self.last_write_ok_ts = time.monotonic()   # paused on purpose, not stalled
            return
        if self.paused_for_clock:
            logger.info("History resumed: the clock is synchronized")
            self.paused_for_clock = False
        self.store.record_many(now, batch)
        if self.day_range is not None:
            self.day_range.sample(now, batch)
        if self.energy is not None:
            for name, reading in meters.items():
                try:
                    self.energy.sample(name, reading, now)
                except Exception:
                    logger.exception("Energy: couldn't record %s's day", name)
        self.last_write_ok_ts = time.monotonic()
        self._maybe_prune(now)

    def _check_disk(self) -> None:
        """A disk low on space holds the history at the size it has: from
        then on its oldest days make room for the new ones inside the
        file, which stops growing - SQLite reuses the pages a delete
        frees. Giving them back to the disk would take a full copy of the
        file, and the space for that is just what's missing. Let go once
        the disk has room again."""
        if self.disk_stats is None:
            return
        disk = self.disk_stats()
        if disk.get("low"):
            if self.disk_hold_bytes is None:
                self.disk_hold_bytes = self.store.used_bytes()
                self.disk_hold_since = time.time()
                logger.warning("History disk almost full (%d MB free): the history is held at %d MB "
                               "until there's room again", (disk.get("free_bytes") or 0) // 1_000_000,
                               self.disk_hold_bytes // 1_000_000)
        elif self.disk_hold_bytes is not None:
            logger.info("History disk has room again: the history can grow up to its ceiling")
            self.disk_hold_bytes = None
            self.disk_hold_since = None

    def _maybe_prune(self, now: int) -> None:
        if (self.max_db_bytes or self.disk_stats) and now - self._last_size_check_ts >= 3600:  # cheap: three PRAGMAs
            self._last_size_check_ts = now
            try:
                self._check_disk()
                caps = [c for c in (self.max_db_bytes or None, self.disk_hold_bytes) if c is not None]
                cap = min(caps) if caps else None
                removed = self.store.prune_to_size(cap, max_seconds=PRUNE_SECONDS_PER_CYCLE) if cap else 0
                if removed:
                    logger.warning("History over its %d MB cap - removed its %d oldest rows",
                                   cap // 1_000_000, removed)
                    if self.store.used_bytes() > cap:
                        self._last_size_check_ts = 0.0     # still over: on with it next cycle
            except Exception:
                logger.exception("Failed to enforce the history size cap")
        # Once a day is plenty for a DELETE scan - in pieces, a few seconds a
        # cycle, until the day's is done (HistoryStore.prune_some()).
        if self._pruned is None and now - self._last_prune_ts < 86400:
            return
        if self._pruned is None:
            self._last_prune_ts = now
            self._pruned = 0
            if self.keep_runs is not None:
                try:
                    self.keep_runs()
                except Exception:
                    logger.exception("Failed to keep the valves' hours")
        try:
            removed, done = self.store.prune_some(now - int(self.retention_days * 86400),
                                                  max_seconds=PRUNE_SECONDS_PER_CYCLE)
            self._pruned += removed
            if not done:
                return
            if self._pruned:
                logger.info("Pruned %d history rows older than %s days", self._pruned, self.retention_days)
            self._pruned = None
            self.store.prune_days(date(datetime.now().year - RUN_YEARS + 1, 1, 1))
        except Exception:
            self._pruned = None
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
