import time

import pytest

from localhome.services.history import HistoryStore, PowerBudget


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
