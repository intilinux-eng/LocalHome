import time
from datetime import datetime

import pytest

from localhome.services.history import HistoryRecorder, HistoryStore, PowerBudget


def test_record_and_query_averages_within_a_bucket(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    # "6h" buckets are 60s wide (see RANGE_BUCKETS) - align to a bucket
    # boundary so these two points always land in the same one,
    # regardless of what second the test happens to run on.
    bucket_start = (int(time.time()) // 60) * 60

    store.record("meter", bucket_start + 5, {"power_w": 100.0})
    store.record("meter", bucket_start + 10, {"power_w": 200.0})

    points = store.query("meter", "power_w", "6h")

    assert len(points) == 1
    assert points[0]["value"] == 150.0


def test_query_is_scoped_to_sensor_and_field(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    now = int(time.time())

    store.record("meter-a", now, {"power_w": 100.0, "voltage_v": 230.0})
    store.record("meter-b", now, {"power_w": 999.0})

    points = store.query("meter-a", "power_w", "24h")

    assert [p["value"] for p in points] == [100.0]


def test_query_peak_today_returns_the_highest_value_and_its_timestamp(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    # All three today, whenever the test runs - a minute after midnight,
    # "a minute ago" was yesterday.
    start_of_today = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    now = max(int(time.time()), start_of_today + 120)

    store.record("meter", now - 60, {"power_w": 500.0})
    store.record("meter", now - 30, {"power_w": 1200.0})
    store.record("meter", now, {"power_w": 800.0})

    peak = store.query_peak_today("meter", "power_w")

    assert peak["value"] == 1200.0
    assert peak["ts"] == now - 30


def test_query_peak_today_with_no_data_returns_zero(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    assert store.query_peak_today("meter", "power_w") == {"value": 0.0, "ts": None}


def test_power_budget_zones_scale_off_contract_limit():
    budget = PowerBudget(contract_limit_w=3000)
    assert budget.available_power_w == pytest.approx(3300.0)
    assert round(budget.trip_risk_w) == 4000


# Wednesday: "today" starts on the 23rd, "week" on Monday the 21st, "month"
# on the 1st.
ON_STATS_NOW = datetime(2026, 9, 23, 12, 0)


def _at(*args) -> int:
    return int(datetime(*args).timestamp())


def test_on_stats_counts_hours_and_starts(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    since = _at(2026, 9, 23, 8, 0)

    # 30s apart, as the recorder writes a valve's is_on: on, off for 6
    # minutes, on again - 2 starts, 5 samples on.
    for i, is_on in enumerate([True] * 3 + [False] * 12 + [True] * 2):
        store.record("valve", since + i * 30, {"is_on": is_on})

    today = store.on_stats("valve", "is_on", 30, now=ON_STATS_NOW)["today"]

    assert today["hours"] == pytest.approx(5 * 30 / 3600.0)
    assert today["starts"] == 2


def test_a_short_off_is_not_a_new_start(tmp_path):
    # A restart's blip: one sample off, then nothing recorded for a minute.
    store = HistoryStore(str(tmp_path / "history.db"))
    since = _at(2026, 9, 23, 8, 0)
    for ts, is_on in [(0, True), (30, True), (60, False), (150, True), (180, True)]:
        store.record("valve", since + ts, {"is_on": is_on})

    today = store.on_stats("valve", "is_on", 30, now=ON_STATS_NOW)["today"]

    assert today["starts"] == 1
    assert today["hours"] == pytest.approx(4 * 30 / 3600.0)


def test_on_stats_scopes_each_range(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record("valve", _at(2026, 9, 23, 9, 0), {"is_on": True})     # today
    store.record("valve", _at(2026, 9, 21, 9, 0), {"is_on": True})     # Monday
    store.record("valve", _at(2026, 9, 5, 9, 0), {"is_on": True})      # earlier this month
    store.record("valve", _at(2026, 8, 20, 9, 0), {"is_on": True})     # last month
    store.record("valve", _at(2026, 9, 23, 13, 0), {"is_on": True})    # after "now"

    stats = store.on_stats("valve", "is_on", 3600, now=ON_STATS_NOW)

    assert {k: v["hours"] for k, v in stats.items()} == {"today": 1, "week": 2, "month": 3}
    assert {k: v["starts"] for k, v in stats.items()} == {"today": 1, "week": 2, "month": 3}


def test_a_run_carried_over_midnight_is_not_a_new_start(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record("valve", _at(2026, 9, 22, 23, 59, 30), {"is_on": True})
    store.record("valve", _at(2026, 9, 23, 0, 0, 0), {"is_on": True})
    store.record("valve", _at(2026, 9, 23, 0, 0, 30), {"is_on": False})

    stats = store.on_stats("valve", "is_on", 30, now=ON_STATS_NOW)

    assert stats["today"] == {"hours": pytest.approx(30 / 3600.0), "starts": 0}
    assert stats["week"]["starts"] == 1


def test_on_stats_with_no_history(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))

    stats = store.on_stats("valve", "is_on", 30, now=ON_STATS_NOW)

    assert stats == {key: {"hours": 0, "starts": 0} for key in ("today", "week", "month")}


def test_on_stats_reads_days_before_today_once(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    store.record("valve", _at(2026, 9, 21, 9, 0), {"is_on": True})
    assert store.on_stats("valve", "is_on", 3600, now=ON_STATS_NOW)["week"]["hours"] == 1

    # Today is read on every call; the days before it come from the count
    # made on the first call of the day...
    store.record("valve", _at(2026, 9, 23, 9, 0), {"is_on": True})
    store.record("valve", _at(2026, 9, 22, 9, 0), {"is_on": True})
    stats = store.on_stats("valve", "is_on", 3600, now=ON_STATS_NOW)
    assert (stats["today"]["hours"], stats["week"]["hours"]) == (1, 2)

    # ...recounted the next day, or as soon as rows are deleted.
    tomorrow = store.on_stats("valve", "is_on", 3600, now=datetime(2026, 9, 24, 12, 0))
    assert tomorrow["week"]["hours"] == 3
    store.clear()
    assert store.on_stats("valve", "is_on", 3600, now=ON_STATS_NOW)["week"]["hours"] == 0


# --------------------------------------------------------- HistoryRecorder --

class FakeDriver:
    def __init__(self, fields):
        self.history_fields = fields


class FakePoller:
    def __init__(self, fields, values):
        self.driver = FakeDriver(fields)
        self._values = values

    def get(self):
        return {"ok": True, **self._values}


class FakeManager:
    def __init__(self, pollers):
        self.pollers = pollers


def test_poll_once_raises_on_a_failing_write_but_leaves_no_broken_state_behind(tmp_path):
    # _loop() (not exercised directly here - it's an unconditional `while
    # True`) wraps poll_once() in try/except and just retries next
    # interval, the same self-healing shape as the thermostat/interlock
    # loops. What actually matters, and what this checks, is that a single
    # failed cycle doesn't leave poll_once() itself broken for next time -
    # catching the exception from outside is only safe if that's true.
    store = HistoryStore(str(tmp_path / "history.db"))
    real_record_many = store.record_many
    calls = {"n": 0}

    def flaky_record_many(ts, samples):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated db error")
        real_record_many(ts, samples)

    store.record_many = flaky_record_many
    manager = FakeManager({"sensor": FakePoller(("temperature_c",), {"temperature_c": 21.0})})
    recorder = HistoryRecorder(manager, store, poll_interval_seconds=0)

    recorder.poll_once()  # call 1: succeeds
    with pytest.raises(RuntimeError):
        recorder.poll_once()  # call 2: store.record raises
    recorder.poll_once()  # call 3: succeeds again

    # Raw row count, not query() - calls 1 and 3 land in the same 5-minute
    # bucket that groups by (this test runs in milliseconds), which would
    # collapse them into one averaged point and hide a missing row.
    conn = store._connect()
    row_count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.close()
    assert row_count == 2  # calls 1 and 3 both landed - call 2's failure didn't block anything after it


def test_prune_removes_only_rows_older_than_the_cutoff(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    now = int(time.time())
    store.record("sensor", now - 40 * 86400, {"temperature_c": 10.0})  # old - should go
    store.record("sensor", now - 1 * 86400, {"temperature_c": 20.0})  # recent - should stay

    removed = store.prune(now - 30 * 86400)

    assert removed == 1
    points = store.query("sensor", "temperature_c", "7d")
    assert [p["value"] for p in points] == [20.0]


# ------------------------------------------------ eMMC-friendly writes -----
# The real deployment runs on flash storage for years. Recording sensor by
# sensor used to mean one connection and one commit per sensor per cycle
# (~40k commits a day); now it's one transaction per cycle, in WAL mode.

def test_database_runs_in_wal_mode(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def _rows_in_the_file(db_path):
    """What another reader of the file sees - not the store's own memory."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    finally:
        conn.close()


def test_cycles_wait_in_memory_and_reach_the_file_together(tmp_path, monkeypatch):
    # A 30 s cycle committed on its own rewrote the last index page of
    # every sensor's field: most of the board's flash writes. Now the
    # cycles go in together, every write_every_seconds, one transaction.
    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path, write_every_seconds=300)
    manager = FakeManager({
        "a": FakePoller(("temperature_c",), {"temperature_c": 21.0}),
        "b": FakePoller(("temperature_c", "humidity_pct"), {"temperature_c": 22.0, "humidity_pct": 50.0}),
        "c": FakePoller(("power_w",), {"power_w": 300.0}),
    })
    writes = []
    real_write = store._write_pending
    monkeypatch.setattr(store, "_write_pending", lambda conn: (writes.append(len(store._pending)), real_write(conn)))
    recorder = HistoryRecorder(manager, store, poll_interval_seconds=0)
    recorder._last_prune_ts = time.time()  # the once-a-day prune is a read of its own
    for _ in range(5):
        recorder.poll_once()
    assert writes == [] and _rows_in_the_file(db_path) == 0
    store._pending_since -= 300            # five minutes on
    recorder.poll_once()
    assert len(writes) == 1, "six cycles, one transaction"
    assert _rows_in_the_file(db_path) == 6 * 4
    store.close()


def test_whoever_reads_sees_the_waiting_samples(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"), write_every_seconds=300)
    now = int(time.time())
    store.record("Rooms Sensor", now, {"temperature_c": 21.5})
    store.add_day_kwh("Meter", "2026-10-07", 1.25)
    assert [p["value"] for p in store.query("Rooms Sensor", "temperature_c", "6h")] == [21.5]
    assert store.energy_days("Meter")["2026-10-07"]["kwh"] == 1.25
    store.close()


def test_stopping_writes_what_is_waiting(tmp_path):
    from localhome.services import history

    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path, write_every_seconds=300)
    store.record("Rooms Sensor", 1000, {"temperature_c": 21.5})
    history._write_waiting()               # what runs at exit (SIGTERM included)
    assert _rows_in_the_file(db_path) == 1
    store.record("Rooms Sensor", 1030, {"temperature_c": 21.0})
    store.close()
    assert _rows_in_the_file(db_path) == 2


def test_a_write_that_failed_is_kept_for_the_next_try(tmp_path, monkeypatch):
    import sqlite3

    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path, write_every_seconds=0)
    real_open = store._open

    class FullDisk:
        def __init__(self, conn):
            self.conn = conn

        def executemany(self, *args):
            raise sqlite3.OperationalError("database or disk is full")

        def rollback(self):
            self.conn.rollback()

    monkeypatch.setattr(store, "_open", lambda: FullDisk(real_open()))
    with pytest.raises(sqlite3.OperationalError):
        store.record("Rooms Sensor", 1000, {"temperature_c": 21.5})
    monkeypatch.setattr(store, "_open", real_open)
    store.record("Rooms Sensor", 1030, {"temperature_c": 21.0})
    assert _rows_in_the_file(db_path) == 2
    store.close()


def test_a_value_that_is_not_a_number_loses_only_itself(tmp_path):
    db_path = str(tmp_path / "history.db")
    store = HistoryStore(db_path, write_every_seconds=0)
    store.record_many(1000, {"Rooms Sensor": {"temperature_c": float("nan"), "humidity_pct": 55.0},
                             "Meter": {"power_w": 300.0}})
    assert _rows_in_the_file(db_path) == 2
    store.close()


def test_extra_samples_are_recorded_alongside_driver_readings(tmp_path):
    # How the thermostat's applied target reaches the chart: merged into the
    # zone sensor's own row set for the same cycle.
    store = HistoryStore(str(tmp_path / "history.db"))
    manager = FakeManager({"Rooms Sensor": FakePoller(("temperature_c",), {"temperature_c": 21.0})})
    recorder = HistoryRecorder(manager, store, poll_interval_seconds=0)
    recorder.extra_samples = lambda: {"Rooms Sensor": {"target_c": 20.0}}

    recorder.poll_once()

    conn = store._connect()
    try:
        rows = dict(conn.execute("SELECT field, value FROM readings WHERE sensor = 'Rooms Sensor'").fetchall())
    finally:
        conn.close()
    assert rows == {"temperature_c": 21.0, "target_c": 20.0}


def test_a_failing_extra_samples_provider_does_not_lose_the_cycle(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    manager = FakeManager({"a": FakePoller(("temperature_c",), {"temperature_c": 21.0})})
    recorder = HistoryRecorder(manager, store, poll_interval_seconds=0)

    def broken():
        raise RuntimeError("thermostat not ready")

    recorder.extra_samples = broken
    recorder.poll_once()

    conn = store._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_wal_is_kept_between_cycles(tmp_path):
    # A connection per call deleted the -wal file on every close, so each
    # 30s cycle wrote the WAL and the database both. Held open, it stays.
    import os

    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        for ts in range(1000, 1005):
            store.record_many(ts, {"Rooms Sensor": {"temperature_c": 20.0 + ts % 3}})
            store.query("Rooms Sensor", "temperature_c", "24h")
        assert os.path.exists(str(tmp_path / "history.db-wal"))
    finally:
        store.close()


def test_one_connection_is_safe_across_threads(tmp_path):
    import threading
    import time as _time

    store = HistoryStore(str(tmp_path / "history.db"))
    now = int(_time.time())
    errors = []

    def writer(n):
        try:
            for i in range(50):
                store.record_many(now - i, {f"Sensor {n}": {"value": float(i)}})
                store.query(f"Sensor {n}", "value", "6h")
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert store.stats(now)["rows"] == 200
    finally:
        store.close()



def test_the_database_is_held_under_its_size_cap(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        for day in range(10, 0, -1):  # ten days, ~60 sensors a sample
            samples = {f"Sensor {i}": {"temperature_c": 20.0 + i, "humidity_pct": 50.0} for i in range(60)}
            for minute in range(0, 1440, 30):
                store.record_many(now - day * 86400 + minute * 60, samples)
        before = store.used_bytes()
        cap = before // 2

        removed = store.prune_to_size(cap)

        assert removed > 0 and store.used_bytes() <= cap
        oldest = store.stats(now)["oldest_ts"]
        assert oldest >= now - 10 * 86400 + 86400  # the oldest day went first
    finally:
        store.close()


def test_the_panel_can_say_what_the_history_is_made_of(tmp_path):
    """The size setting shows what's in there: the admin panel has no
    other way to answer "300 MB of what?"."""
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        for minute in range(10):
            store.record_many(now - minute * 60, {
                "Rooms Sensor": {"temperature_c": 21.0, "humidity_pct": 50.0},
                "Rooms Valve": {"is_on": 1.0},
            })

        stats = store.stats(now)

        assert stats["sensors"] == 2
        assert stats["rows"] == 30
        assert [f["field"] for f in stats["by_field"]][:1] != []
        by_field = {f["field"]: f["rows"] for f in stats["by_field"]}
        assert by_field == {"temperature_c": 10, "humidity_pct": 10, "is_on": 10}
        assert stats["newest_ts"] == now and stats["oldest_ts"] == now - 9 * 60
        assert stats["used_bytes"] > 0
        assert stats["size_prune_ts"] is None, "the ceiling has never been reached here"
    finally:
        store.close()


def test_the_day_the_ceiling_was_reached_is_remembered(tmp_path):
    """Months later, a history that starts mid-August explains itself
    only if the database remembers being cut back."""
    path = tmp_path / "history.db"
    store = HistoryStore(str(path))
    try:
        now = int(time.time())
        for day in range(10, 0, -1):
            samples = {f"Sensor {i}": {"temperature_c": 20.0 + i} for i in range(60)}
            for minute in range(0, 1440, 30):
                store.record_many(now - day * 86400 + minute * 60, samples)
        cap = store.used_bytes() // 2

        removed = store.prune_to_size(cap)

        stats = store.stats(now)
        assert removed > 0
        assert stats["size_prune_rows"] == removed
        assert abs(stats["size_prune_ts"] - now) < 60
    finally:
        store.close()

    # And it survives a restart - it's in the database, not in memory.
    reopened = HistoryStore(str(path))
    try:
        assert reopened.stats()["size_prune_ts"] is not None
    finally:
        reopened.close()


def test_a_prune_that_removes_nothing_is_not_remembered_as_a_ceiling(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        store.record_many(now - 60, {"Rooms Sensor": {"temperature_c": 21.0}})

        store.prune_to_size(10_000_000)   # miles under the cap

        assert store.stats(now)["size_prune_ts"] is None
    finally:
        store.close()


def test_the_last_day_is_never_removed_to_fit_the_cap(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        store.record_many(now - 60, {"Rooms Sensor": {"temperature_c": 21.0}})
        store.prune_to_size(1)  # impossibly small
        assert store.stats(now)["rows"] == 1
    finally:
        store.close()



def _fill_days(store, first_day, days, now):
    samples = {f"Sensor {i}": {"temperature_c": 20.0 + i, "humidity_pct": 50.0} for i in range(60)}
    for day in range(first_day, first_day - days, -1):
        for minute in range(0, 1440, 30):
            store.record_many(now - day * 86400 + minute * 60, samples)


def _logical_bytes(store):
    """The database's own size, free pages included - what the file holds
    once the WAL is checkpointed."""
    conn = store._connect()
    try:
        return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def test_a_disk_almost_full_holds_the_history_where_it_is(tmp_path):
    """Whatever filled the disk, the history stops taking more of it: the
    oldest days make room for the new ones inside the file."""
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        _fill_days(store, 20, 10, now)          # ten days, from 20 to 11 days ago
        disk = {"low": True, "free_bytes": 300_000_000}
        recorder = HistoryRecorder(manager=None, store=store, max_db_bytes=2_000_000_000)
        recorder.disk_stats = lambda: disk
        recorder._maybe_prune(now)
        held = recorder.disk_hold_bytes
        assert held == store.used_bytes() and recorder.disk_hold_since is not None
        sizes = []
        for day in range(10, 6, -1):            # four more days, an hourly check after each
            _fill_days(store, day, 1, now)
            recorder._last_size_check_ts = 0
            recorder._maybe_prune(now)
            assert store.used_bytes() <= held
            sizes.append(_logical_bytes(store))
        assert sizes[-1] <= sizes[0], "past the first check the file grows no more"
        assert store.stats(now)["oldest_ts"] >= now - 20 * 86400 + 4 * 86400, "the oldest days went"

        disk["low"] = False                     # room again: up to the ceiling as before
        recorder._last_size_check_ts = 0
        recorder._maybe_prune(now)
        assert recorder.disk_hold_bytes is None and recorder.disk_hold_since is None
        rows = store.stats(now)["rows"]
        _fill_days(store, 6, 1, now)
        recorder._last_size_check_ts = 0
        recorder._maybe_prune(now)
        assert store.stats(now)["rows"] > rows, "nothing removed once there's room"
    finally:
        store.close()


def test_a_disk_with_room_and_no_ceiling_removes_nothing(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        _fill_days(store, 5, 3, now)
        rows = store.stats(now)["rows"]
        recorder = HistoryRecorder(manager=None, store=store, max_db_bytes=None)
        recorder.disk_stats = lambda: {"low": False}
        recorder._maybe_prune(now)
        assert recorder.disk_hold_bytes is None
        assert store.stats(now)["rows"] == rows
    finally:
        store.close()


def test_nothing_is_recorded_while_the_clock_is_not_synchronized(tmp_path):
    """After a power-on without internet the board's clock is the evening
    before's: rows stamped with it would sit in the charts hours in the
    past. Recording waits for the clock; the loop still counts as alive."""
    from localhome.services.history import HistoryRecorder

    class Poller:
        class driver:
            history_fields = ("temperature_c",)

        def get(self):
            return {"ok": True, "temperature_c": 21.0}

    class Manager:
        pollers = {"Rooms Sensor": Poller()}

    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        recorder = HistoryRecorder(Manager(), store)
        synced = {"value": False}
        recorder.clock_ready = lambda: synced["value"]

        recorder.poll_once()
        assert store.stats()["rows"] == 0
        assert recorder.paused_for_clock is True
        assert recorder.last_write_ok_ts is not None, "paused on purpose, not a stalled loop"

        synced["value"] = True
        recorder.poll_once()
        assert store.stats()["rows"] == 1
        assert recorder.paused_for_clock is False
    finally:
        store.close()


def test_the_wal_file_is_kept_from_staying_large(tmp_path):
    """SQLite never shrinks the -wal file by itself; with a limit, a
    checkpoint cuts it back after one big transaction."""
    store = HistoryStore(str(tmp_path / "history.db"))
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 8388608
    finally:
        conn.close()
        store.close()
