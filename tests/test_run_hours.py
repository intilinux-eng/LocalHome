"""Each zone's valve hours and the plant's - any zone open, counted once -
day by day (kept once the samples are gone, six years), the last 24
hours; and a year month by month beside the heat meter's month-end
values: the kWh each month took, and per hour of the plant running."""
from datetime import date, datetime, timedelta

import pytest

from localhome.errors import InputError
from localhome.services import run_hours
from localhome.services.history import PLANT_KEY, HistoryStore, first_run_day, run_days_of
from localhome.web.app import create_app

STEP = 30                       # seconds between recordings, as on the board
ZONES = [{"name": "Stanze", "valve": "Stanze Valve"}, {"name": "Bagni", "valve": "Bagni Valve"}]
NOW = datetime(2026, 10, 12, 18, 0)


def at(day: date, hour: float) -> int:
    return int((datetime(day.year, day.month, day.day) + timedelta(hours=hour)).timestamp())


def record(store, day, hours, stanze=(), bagni=()):
    """Every 30 s over `hours` (from, to) of `day`: each valve open in its
    own (from, to) stretches, closed otherwise."""
    start, end = hours
    ts = at(day, start)
    while ts < at(day, end):
        hour = (ts - at(day, 0)) / 3600
        open_ = lambda spans: 1.0 if any(a <= hour < b for a, b in spans) else 0.0
        store.record_many(ts, {"Stanze Valve": {"is_on": open_(stanze)}, "Bagni Valve": {"is_on": open_(bagni)}})
        ts += STEP
    store.flush()


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(str(tmp_path / "history.db"))
    yield s
    s.close()


def test_the_plant_counts_overlapping_zones_once(store):
    day = NOW.date() - timedelta(days=1)
    # Stanze 8-11, Bagni 10-12: three hours and two, the plant four (8-12).
    record(store, day, (0, 24), stanze=[(8, 11)], bagni=[(10, 12)])
    days = run_days_of(store, ["Stanze Valve", "Bagni Valve"], "is_on", STEP, day, NOW)
    iso = day.isoformat()
    assert days["Stanze Valve"][iso] == (3 * 3600, 1)
    assert days["Bagni Valve"][iso] == (2 * 3600, 1)
    assert days[PLANT_KEY][iso] == (4 * 3600, 1)


def test_days_are_kept_once_the_samples_are_gone(store):
    day = NOW.date() - timedelta(days=3)
    record(store, day, (0, 24), stanze=[(6, 7.5), (18, 19)])
    first = run_days_of(store, ["Stanze Valve", "Bagni Valve"], "is_on", STEP, day, NOW)
    assert first["Stanze Valve"][day.isoformat()] == (2.5 * 3600, 2)
    store.prune(at(NOW.date(), 0))                        # the samples of that day gone
    again = run_days_of(store, ["Stanze Valve", "Bagni Valve"], "is_on", STEP, day, NOW)
    assert again["Stanze Valve"][day.isoformat()] == (2.5 * 3600, 2), "kept for good"
    store.clear()
    assert run_days_of(store, ["Stanze Valve"], "is_on", STEP, day, NOW)["Stanze Valve"] == {
        NOW.date().isoformat(): (0, 0)}, "cleared with the history"


def test_a_day_the_samples_dont_cover_whole_is_unknown_not_zero(store):
    yesterday = NOW.date() - timedelta(days=1)
    record(store, yesterday, (12, 24), stanze=[(13, 14)])     # recording began at noon
    days = run_days_of(store, ["Stanze Valve"], "is_on", STEP, yesterday - timedelta(days=1), NOW)
    assert yesterday.isoformat() not in days["Stanze Valve"]
    assert (yesterday - timedelta(days=1)).isoformat() not in days["Stanze Valve"]


def test_the_home_page_report_the_last_24_hours_and_30_days(store):
    yesterday = NOW.date() - timedelta(days=1)
    record(store, yesterday, (0, 24), stanze=[(20, 23)])
    record(store, NOW.date(), (0, 18), stanze=[(7, 8)], bagni=[(9, 10)])
    report = run_hours.zone_report(store, ZONES, STEP, now=NOW)
    assert len(report["days"]) == 30 and report["days"][-1] == "2026-10-12"
    stanze = report["zones"]["Stanze"]
    assert stanze["hours"][-2:] == [3.0, 1.0] and stanze["hours"][0] is None, "unknown before recording began"
    assert stanze["last_24h"] == 4.0, "from 18:00 yesterday: 20-23, then 7-8"
    assert report["plant"]["hours"][-1] == 2.0 and report["plant"]["last_24h"] == 5.0


def test_six_years_of_days_kept_and_before_the_samples_go(store):
    old, kept = date(NOW.year - 6, 12, 31), date(NOW.year - 5, 1, 1)
    with store._lock:
        conn = store._connect()
        for day in (old, kept):
            conn.execute("INSERT INTO daily_runs (key, day, seconds, starts) VALUES (?, ?, 3600, 1)",
                         (PLANT_KEY, day.isoformat()))
        conn.commit()
        conn.close()
    assert store.prune_runs(date(NOW.year - 5, 1, 1)) == 1
    assert first_run_day(store) == kept.isoformat()
    yesterday = NOW.date() - timedelta(days=1)
    record(store, yesterday, (0, 24), stanze=[(8, 9)])
    run_hours.keep_days(store, ZONES, STEP, 3, now=NOW)        # as the recorder does, once a day
    store.prune(at(NOW.date(), 0))
    days = run_days_of(store, ["Stanze Valve", "Bagni Valve"], "is_on", STEP, yesterday, NOW)
    assert days["Stanze Valve"][yesterday.isoformat()] == (3600, 1), "kept though no page asked for it"


def refused(call, code):
    with pytest.raises(InputError) as caught:
        call()
    assert caught.value.code == code


def meter_at(tmp_path, today=NOW.date()):
    return run_hours.HeatMeterStore(str(tmp_path / "heat_meter.json"), today=lambda: today)


def test_a_years_values_at_once_ended_months_counting_up(tmp_path):
    meter = meter_at(tmp_path)
    meter.set_year(2025, {"11": {"heat_kwh": 56370, "cool_kwh": 7119}, "12": {"heat_kwh": 56827, "cool_kwh": 7119}})
    meter.set_year(2026, {"01": {"heat_kwh": 57583, "cool_kwh": 7119}, "02": {"heat_kwh": 58100}})
    for year, months, code in (
            (2026, {"10": {"heat_kwh": 59000}}, "heat_meter_month_open"),          # October isn't over
            (2026, {"01": {"heat_kwh": 56000}}, "heat_meter_not_increasing"),       # below December
            (2025, {"12": {"heat_kwh": 57600}}, "heat_meter_not_increasing"),       # above January
            (2026, {"03": {"heat_kwh": -1}}, "heat_meter_value_invalid"),
            (2026, {"03": 5}, "heat_meter_value_invalid"),
            (2026, {"13": {"heat_kwh": 1}}, "heat_meter_year_invalid"),
            (NOW.year - 6, {"01": {"heat_kwh": 1}}, "heat_meter_year_invalid"),      # older than six years
            (NOW.year + 1, {}, "heat_meter_year_invalid")):
        refused(lambda: meter.set_year(year, months), code)
    # The year's months, all of them: one left out is gone.
    months = meter.set_year(2026, {"01": {"heat_kwh": 57583, "cool_kwh": 7119}, "02": None})
    assert list(months) == ["2025-11", "2025-12", "2026-01"]
    later = meter_at(tmp_path, today=date(2031, 3, 1))                          # five years on: 2026 the oldest
    assert list(later.get()) == ["2026-01"]


def test_a_year_month_by_month_the_kwh_beside_the_plants_hours(store, tmp_path):
    for day in range(1, 31):
        record(store, date(2026, 9, day), (0, 24), stanze=[(6, 10)], bagni=[(8, 12)])   # the plant 6 h a day
    record(store, NOW.date(), (0, 18), stanze=[(7, 8)])
    meter = meter_at(tmp_path)
    meter.set_year(2025, {"12": {"heat_kwh": 1000, "cool_kwh": 500}})
    meter.set_year(2026, {"01": {"heat_kwh": 1300, "cool_kwh": 500}, "08": {"heat_kwh": 1400, "cool_kwh": 600},
                          "09": {"heat_kwh": 1490, "cool_kwh": 600}})
    report = run_hours.year_report(store, ZONES, STEP, meter.get(), 2026, now=NOW)
    months = {m["month"]: m for m in report["months"]}
    assert list(months)[0] == "2026-01" and list(months)[-1] == "2026-10", "up to this month"
    january, february, september, october = (months[k] for k in ("2026-01", "2026-02", "2026-09", "2026-10"))
    assert (january["heat_kwh"], january["cool_kwh"], january["kwh"]) == (300.0, 0.0, 300.0), "December's before"
    assert january["plant_hours"] is None and january["kwh_per_hour"] is None, "no hours recorded then"
    assert february["kwh"] is None and february["heat_meter"] is None
    assert (september["kwh"], september["days_known"], september["plant_hours"]) == (90.0, 30, 180.0)
    assert september["kwh_per_hour"] == 0.5 and september["zones"] == {"Stanze": 120.0, "Bagni": 120.0}
    assert october["open"] and october["kwh"] is None and october["plant_hours"] == 1.0
    total = report["total"]
    # January and September: August's July is missing.
    assert (total["heat_kwh"], total["cool_kwh"], total["kwh"]) == (390.0, 0.0, 390.0)
    assert (total["plant_hours"], total["kwh_per_hour"], total["months_paired"]) == (181.0, 0.5, 1)
    assert run_hours.years_of(store, meter.get(), NOW.date()) == [2026, 2025]


ZONES_MAP = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]


def test_the_routes(tmp_path):
    import json
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\nthermostat:\n  mode: heat\n"
                                          "  schedules_file: schedules.json\n  poll_interval_seconds: 600\n",
                                          encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({"zones": ZONES_MAP}), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        c = app.test_client()
        hours = c.get("/api/climate/run-hours").get_json()
        assert set(hours["zones"]) == {"Stanze", "Bagni"} and len(hours["days"]) == 30
        usage = c.get("/api/climate-admin/usage").get_json()
        this_year = date.today().year
        assert usage["zones"] == ["Stanze", "Bagni"] and usage["year"] == this_year
        assert usage["years"] == [this_year, this_year - 1], "the one before, for the meter's 15 months"
        assert len(usage["months"]) == date.today().month and usage["this_month_hours"] is not None
        post = lambda body: c.post("/api/climate-admin/heat-meter/year", json=body)
        refusal = post({"year": this_year - 1, "months": {"12": {"heat_kwh": "x"}}})
        assert refusal.status_code == 400 and refusal.get_json()["code"] == "heat_meter_value_invalid"
        assert refusal.get_json()["params"] == {"month": f"12/{this_year - 1}"}
        saved = post({"year": this_year - 1, "months": {"12": {"heat_kwh": 10}}}).get_json()
        assert saved["year"] == this_year - 1 and saved["years"] == [this_year, this_year - 1]
        assert saved["months"][-1]["heat_meter"] == 10
        assert c.get(f"/api/climate-admin/usage?year={this_year - 1}").get_json()["year"] == this_year - 1
        assert c.get("/api/climate-admin/usage?year=1990").get_json()["year"] == this_year
    finally:
        app.config["HISTORY"].close()
