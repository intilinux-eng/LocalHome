"""Each power meter's day - kWh and peak - kept for good, and the week,
month and year the home page shows from it (services/energy.py, the
`daily_energy` table in services/history.py, /api/energy/<name>)."""
import time
from datetime import date, datetime

import pytest

from localhome.services.energy import MAX_GAP_SECONDS, EnergyLedger, local_day, summary
from localhome.services.history import HistoryStore
from localhome.web.app import create_app


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(str(tmp_path / "history.db"))
    yield s
    s.close()


def at(text):
    return datetime.fromisoformat(text).timestamp()


def test_a_counter_is_taken_as_it_is_and_yesterday_from_its_own(store):
    """A Sonoff POWCT's day_kwh for today, its yesterday_kwh for yesterday -
    replaced each cycle, so a counter reset a minute late puts itself right."""
    ledger = EnergyLedger(store)
    ledger.sample("Casa", {"power_w": 300, "day_kwh": 6.4, "yesterday_kwh": 5.7}, at("2026-10-02T23:59:30"))
    ledger.sample("Casa", {"power_w": 250, "day_kwh": 6.5, "yesterday_kwh": 5.7}, at("2026-10-03T00:00:30"))  # not reset yet
    ledger.sample("Casa", {"power_w": 250, "day_kwh": 0.01, "yesterday_kwh": 6.5}, at("2026-10-03T00:01:30"))
    days = store.energy_days("Casa")
    assert days["2026-10-03"]["kwh"] == 0.01 and days["2026-10-02"]["kwh"] == 6.5
    assert days["2026-10-02"]["source"] == "meter"


def test_without_a_counter_the_power_is_integrated_but_not_across_a_gap(store):
    ledger = EnergyLedger(store)
    t = at("2026-10-02T10:00:00")
    for i in range(121):                                   # an hour at 1000 W, every 30 s
        ledger.sample("Plug", {"power_w": 1000}, t + i * 30)
    ledger.sample("Plug", {"power_w": 1000}, t + 3600 + MAX_GAP_SECONDS + 60)   # after a gap: not bridged
    day = store.energy_days("Plug")["2026-10-02"]
    assert day["kwh"] == pytest.approx(1.0) and day["source"] == "power"


def test_the_peak_is_the_highest_power_and_when(store):
    ledger = EnergyLedger(store)
    for power, ts in ((800, "10:00"), (3200, "19:40"), (900, "21:00")):
        ledger.sample("Casa", {"power_w": power, "day_kwh": 1.0}, at(f"2026-10-02T{ts}:00"))
    day = store.energy_days("Casa")["2026-10-02"]
    assert (day["peak_w"], day["peak_ts"]) == (3200, int(at("2026-10-02T19:40:00")))


def test_the_days_already_in_the_history_are_worked_out_once(store):
    t = int(at("2026-09-30T12:00:00"))
    for i in range(121):
        store.record("Casa", t + i * 30, {"power_w": 2000 if i == 60 else 500})
    ledger = EnergyLedger(store)
    assert ledger.backfill("Casa", today="2026-10-02") == 1
    assert ledger.backfill("Casa", today="2026-10-02") == 0, "only days without a row"
    day = store.energy_days("Casa")["2026-09-30"]
    assert day["kwh"] == pytest.approx(0.5 + 1500 * 30 / 3_600_000, rel=0.01)
    assert day["peak_w"] == 2000 and day["source"] == "power"


def test_week_month_and_year_add_up_and_say_when_estimated(store):
    store.set_day("Casa", "2026-09-28", 6.0, "meter")       # Monday
    store.add_day_kwh("Casa", "2026-09-29", 5.0)             # worked out from the power
    store.set_day("Casa", "2026-10-01", 5.7, "meter")
    store.set_day("Casa", "2026-10-02", 6.4, "meter")
    store.raise_day_peak("Casa", "2026-10-02", 3200, int(at("2026-10-02T19:40:00")))
    s = summary(store, "Casa", today=date(2026, 10, 2))
    assert s["today"]["kwh"] == 6.4 and s["today"]["peak_w"] == 3200 and s["yesterday"]["kwh"] == 5.7
    assert s["week"]["from"] == "2026-09-28" and s["week"]["kwh"] == 23.1 and s["week"]["estimated"] is True
    assert len(s["week"]["days"]) == 7 and s["week"]["days"][2] is None
    assert s["month"]["kwh"] == 12.1 and s["month"]["estimated"] is False and len(s["month"]["days"]) == 31
    assert s["year"]["kwh"] == 23.1
    assert [m["kwh"] for m in s["year"]["months"]][8:10] == [11.0, 12.1] and s["year"]["months"][0]["kwh"] is None
    assert s["since"] == "2026-09-28"


def test_clearing_the_history_clears_the_days_too(store):
    store.set_day("Casa", "2026-10-02", 6.4, "meter")
    store.clear()
    assert store.energy_days("Casa") == {}


def test_the_route_answers_for_a_power_meter_only(tmp_path):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        client = app.test_client()
        assert client.get("/api/energy/Nowhere").status_code == 404
        app.config["MANAGER"].sensor_kinds["Casa"] = "power_meter"
        app.config["HISTORY"].set_day("Casa", local_day(time.time()), 3.2, "meter")
        body = client.get("/api/energy/Casa").get_json()
        assert body["ok"] is True and body["today"]["kwh"] == 3.2 and body["week"]["kwh"] >= 3.2
    finally:
        app.config["HISTORY"].close()
