import time

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
    now = int(time.time())

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


def test_sum_on_hours_counts_only_samples_at_or_above_half(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    since = 1000

    # 4 "on" samples and 2 "off" samples, 30s apart - a valve driver would
    # record its is_on reading (True/False) exactly like this.
    for i, is_on in enumerate([True, True, False, True, False, True]):
        store.record("valve", since + i * 30, {"is_on": is_on})

    hours = store.sum_on_hours("valve", "is_on", since, since + 6 * 30 + 1, sample_interval_seconds=30)

    assert hours == pytest.approx(4 * 30 / 3600.0)


def test_sum_on_hours_range_scopes_to_today(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    now = int(time.time())

    store.record("valve", now - 10, {"is_on": True})
    store.record("valve", now - 2 * 86400, {"is_on": True})  # two days ago - outside "today"

    today_hours = store.sum_on_hours_range("valve", "is_on", "today", sample_interval_seconds=30)

    assert today_hours == pytest.approx(30 / 3600.0)


def test_sum_on_hours_range_rejects_unknown_range(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    with pytest.raises(ValueError):
        store.sum_on_hours_range("valve", "is_on", "decade", sample_interval_seconds=30)


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


def test_a_whole_cycle_is_one_transaction(tmp_path, monkeypatch):
    store = HistoryStore(str(tmp_path / "history.db"))
    manager = FakeManager({
        "a": FakePoller(("temperature_c",), {"temperature_c": 21.0}),
        "b": FakePoller(("temperature_c", "humidity_pct"), {"temperature_c": 22.0, "humidity_pct": 50.0}),
        "c": FakePoller(("power_w",), {"power_w": 300.0}),
    })
    connects = {"n": 0}
    real_connect = store._connect

    def counting_connect():
        connects["n"] += 1
        return real_connect()

    monkeypatch.setattr(store, "_connect", counting_connect)
    recorder = HistoryRecorder(manager, store, poll_interval_seconds=0)
    recorder._last_prune_ts = time.time()  # the once-a-day prune is its own, separate connection
    recorder.poll_once()

    assert connects["n"] == 1  # three sensors, one connection/commit
    conn = real_connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 4
    finally:
        conn.close()


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


def test_the_last_day_is_never_removed_to_fit_the_cap(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    try:
        now = int(time.time())
        store.record_many(now - 60, {"Rooms Sensor": {"temperature_c": 21.0}})
        store.prune_to_size(1)  # impossibly small
        assert store.stats(now)["rows"] == 1
    finally:
        store.close()
