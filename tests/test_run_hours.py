"""Each zone's valve hours and the plant's - any zone open, counted once -
day by day (kept for good once the samples are gone), the last 24 hours,
month by month; and the heat meter's readings beside them: kWh, kWh per
hour of the plant running, euros."""
from datetime import date, datetime, timedelta

import pytest

from localhome.errors import InputError
from localhome.services import run_hours
from localhome.services.history import PLANT_KEY, HistoryStore, run_days_of
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


def test_months_with_their_days_known(store):
    record(store, date(2026, 9, 29), (12, 24), stanze=[(13, 15)])     # recording began at noon
    record(store, date(2026, 9, 30), (0, 24), stanze=[(0, 2)], bagni=[(1, 3)])
    record(store, NOW.date(), (0, 18), stanze=[(7, 8)])
    october, september = run_hours.month_report(store, ZONES, STEP, months=2, now=NOW)
    assert (september["month"], september["days"]) == ("2026-09", 30)
    # The 29th began the recording: unknown; the 30th known whole.
    assert september["days_known"] == 1 and september["plant_hours"] == 3.0
    assert september["zones"] == {"Stanze": 2.0, "Bagni": 2.0}
    assert october["days"] == 12 and october["plant_hours"] == 1.0


def test_meter_readings_count_up_and_are_checked(tmp_path):
    meter = run_hours.HeatMeterStore(str(tmp_path / "heat_meter.json"), today=lambda: date(2026, 10, 12))
    meter.add({"day": "2026-09-30", "heat_kwh": 1000, "cool_kwh": 50})
    meter.add({"day": "2026-10-11", "heat_kwh": 1120.5})
    for bad, code in (({"day": "2026-10-13", "heat_kwh": 1200}, "heat_meter_day_invalid"),
                      ({"day": "2026-10-05", "heat_kwh": 900}, "heat_meter_not_increasing"),
                      ({"day": "2026-10-05", "heat_kwh": 1200}, "heat_meter_not_increasing"),
                      ({"day": "2026-10-05", "heat_kwh": -1}, "heat_meter_value_invalid"),
                      ({"day": "2026-10-05"}, "heat_meter_value_invalid")):
        with pytest.raises(InputError) as refused:
            meter.add(bad)
        assert refused.value.code == code
    meter.add({"day": "2026-10-11", "heat_kwh": 1121})        # the same day: replaced
    assert [r["heat_kwh"] for r in meter.get()["readings"]] == [1000, 1121]
    with pytest.raises(InputError):
        meter.set_price(-1)
    assert meter.set_price(0.12)["price_eur_kwh"] == 0.12
    assert meter.remove("2026-09-30")["readings"] == [{"day": "2026-10-11", "heat_kwh": 1121.0}]


def test_between_two_readings_kwh_per_hour_and_euros(store, tmp_path):
    for day in (date(2026, 10, 10), date(2026, 10, 11)):
        record(store, day, (0, 24), stanze=[(6, 10)], bagni=[(8, 12)])     # the plant 6 h a day
    meter = run_hours.HeatMeterStore(str(tmp_path / "heat_meter.json"), today=lambda: NOW.date())
    meter.add({"day": "2026-10-09", "heat_kwh": 1000})
    meter.add({"day": "2026-10-11", "heat_kwh": 1060})
    meter.set_price(0.1)
    (period,) = run_hours.meter_report(store, ZONES, STEP, meter.get(), now=NOW)
    assert (period["from"], period["to"], period["days"], period["days_known"]) == ("2026-10-09", "2026-10-11", 2, 2)
    assert (period["kwh"], period["plant_hours"], period["kwh_per_hour"]) == (60.0, 12.0, 5.0)
    assert (period["eur"], period["eur_per_hour"]) == (6.0, 0.5)


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
        assert usage["zones"] == ["Stanze", "Bagni"] and len(usage["months"]) == 12
        today = date.today().isoformat()
        assert c.post("/api/climate-admin/heat-meter/reading", json={"day": today, "heat_kwh": 10}).get_json()["ok"]
        refused = c.post("/api/climate-admin/heat-meter/reading", json={"day": today, "heat_kwh": "x"})
        assert refused.status_code == 400 and refused.get_json()["code"] == "heat_meter_value_invalid"
        priced = c.post("/api/climate-admin/heat-meter/price", json={"price_eur_kwh": 0.15}).get_json()
        assert priced["meter"]["price_eur_kwh"] == 0.15
        gone = c.post("/api/climate-admin/heat-meter/reading/delete", json={"day": today}).get_json()
        assert gone["meter"]["readings"] == []
    finally:
        app.config["HISTORY"].close()
