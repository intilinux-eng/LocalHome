"""Each zone's valve hours and the plant's - any zone open, counted once -
day by day (kept once the samples are gone, six years), the last 24
hours; and a year month by month beside the heat meter's month-end
values: the kWh each month took, and per hour of the plant running."""
from datetime import date, datetime, timedelta

import pytest

from localhome.errors import InputError
from localhome.services import run_hours
from localhome.services.history import PLANT_KEY, HistoryStore, first_run_day, run_days_of, temp_days_of
from localhome.web.app import create_app

STEP = 30                       # seconds between recordings, as on the board
ZONES = [{"name": "Stanze", "valve": "Stanze Valve"}, {"name": "Bagni", "valve": "Bagni Valve"}]
NOW = datetime(2026, 10, 12, 18, 0)
OUTDOOR = "Outdoor Sensor"


def at(day: date, hour: float) -> int:
    return int((datetime(day.year, day.month, day.day) + timedelta(hours=hour)).timestamp())


def record(store, day, hours, stanze=(), bagni=(), outdoor=None):
    """Every 30 s over `hours` (from, to) of `day`: each valve open in its
    own (from, to) stretches, closed otherwise; and the outdoor sensor's
    temperature, `outdoor(hour)`, while it gives one."""
    start, end = hours
    ts = at(day, start)
    while ts < at(day, end):
        hour = (ts - at(day, 0)) / 3600
        open_ = lambda spans: 1.0 if any(a <= hour < b for a, b in spans) else 0.0
        sample = {"Stanze Valve": {"is_on": open_(stanze)}, "Bagni Valve": {"is_on": open_(bagni)}}
        if outdoor is not None and outdoor(hour) is not None:
            sample[OUTDOOR] = {"temperature_c": outdoor(hour)}
        store.record_many(ts, sample)
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
    assert store.prune_days(date(NOW.year - 5, 1, 1)) == 1
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


def test_the_outdoor_days_mean_kept_once_the_samples_go(store):
    two, one = NOW.date() - timedelta(days=2), NOW.date() - timedelta(days=1)
    record(store, two, (0, 24), outdoor=lambda h: 10.0 if h < 12 else 20.0)
    record(store, one, (0, 24), outdoor=lambda h: 8.0 if h < 6 else None)        # silent from 6 on
    days = temp_days_of(store, OUTDOOR, "temperature_c", STEP, two, NOW)
    assert days[two.isoformat()][:3] == (15.0, 10.0, 20.0)
    assert days[two.isoformat()][3:] == (at(two, 0), at(two, 12)), "when the lowest and the highest first came"
    assert one.isoformat() not in days, "a quarter of the day: no mean worth comparing"
    run_hours.keep_days(store, ZONES, STEP, 3, now=NOW, temps=[OUTDOOR])
    store.prune(at(NOW.date(), 0))
    assert temp_days_of(store, OUTDOOR, "temperature_c", STEP, two, NOW)[two.isoformat()] ==         (15.0, 10.0, 20.0, at(two, 0), at(two, 12)), "kept with their times"
    # Both days' hours - each valve's and the plant's - and the one mean.
    assert store.prune_days(NOW.date()) == 2 * 3 + 1


def test_each_zones_last_7_days_lowest_and_highest_and_when(store):
    yesterday = NOW.date() - timedelta(days=1)
    rooms = lambda h: 25.0 + abs(h - 5) / 10                                  # lowest at 5:00, highest at the end
    store_zone = lambda day, hours, f: record(store, day, hours, outdoor=f)
    store_zone(yesterday, (0, 24), rooms)
    store_zone(NOW.date(), (0, 18), lambda h: 26.0)
    report = run_hours.temp_report(store, [{"name": "Stanze", "sensor": OUTDOOR}], STEP, now=NOW)
    assert len(report["days"]) == 7 and report["days"][-1] == NOW.date().isoformat()
    *older, day_before, today = report["zones"]["Stanze"]
    assert older == [None] * 5, "not recorded"
    assert (day_before["min"], day_before["max"]) == (25.0, 26.9)
    assert day_before["min_at"] == at(yesterday, 5) and day_before["max_at"] >= at(yesterday, 23.9)
    assert (today["min"], today["max"], today["min_at"]) == (26.0, 26.0, at(NOW.date(), 0))


def test_degree_days_the_year_before_and_this_months_estimate(store, tmp_path):
    for day in range(1, 31):                    # September: 6 h a day at 14 °C
        record(store, date(2026, 9, day), (0, 24), stanze=[(6, 10)], bagni=[(8, 12)], outdoor=lambda h: 14.0)
    for day in range(1, 12):                    # October so far: the same at 12 °C
        record(store, date(2026, 10, day), (0, 24), stanze=[(6, 10)], bagni=[(8, 12)], outdoor=lambda h: 12.0)
    record(store, NOW.date(), (0, 18), stanze=[(7, 8)], outdoor=lambda h: 12.0)
    meter = meter_at(tmp_path)
    meter.set_year(2025, {"08": {"heat_kwh": 1000, "cool_kwh": 500}, "09": {"heat_kwh": 1100, "cool_kwh": 500}})
    meter.set_year(2026, {"08": {"heat_kwh": 1400, "cool_kwh": 600}, "09": {"heat_kwh": 1490, "cool_kwh": 600}})
    report = run_hours.year_report(store, ZONES, STEP, meter.get(), 2026, now=NOW, mode="heat", outdoor=OUTDOOR)
    september, october = report["months"][-2:]
    # 30 days of 20 - 14 °C; 90 kWh over them.
    assert (september["outdoor_c"], september["degree_days"], september["kwh_per_dd"]) == (14.0, 180, 0.5)
    assert september["last_year"]["kwh"] == 100.0 and report["months"][0]["last_year"]["kwh"] is None
    # 67 hours so far at September's 0.5 kWh an hour.
    assert october["plant_hours"] == 67.0 and october["estimate"] == {
        "kwh": 34, "per_hour": 0.5, "kind": "heat", "months": ["2026-09"]}
    assert (october["outdoor_c"], october["degree_days"], october["kwh_per_dd"]) == (12.0, 96, None)
    total = report["total"]
    assert (total["degree_days"], total["kwh_per_dd"]) == (276, 0.5)
    assert total["last_year"] == {"months": 1, "kwh": 90.0, "last_kwh": 100.0}, "the same months, the year before"
    cooling = run_hours.year_report(store, ZONES, STEP, meter.get(), 2026, now=NOW, mode="cool", outdoor=OUTDOOR)
    assert "estimate" not in cooling["months"][-1], "no month of cooling to go by"


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
    assert report["before"] == {"heat_kwh": 1000.0, "cool_kwh": 500.0}, "December, for January as it's typed"
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
    # The six years kept, each one open to its meter values; with something: this one, and 2025's December.
    assert run_hours.years_of(NOW.date()) == [2026, 2025, 2024, 2023, 2022, 2021]
    assert run_hours.years_with_data(store, meter.get(), NOW.date()) == [2026, 2025]

    # For the CSV: every month of the six years, oldest first, the same as the year's report.
    kept = run_hours.kept_months(store, ZONES, STEP, meter.get(), now=NOW)
    assert [r["month"] for r in kept][:2] == ["2021-01", "2021-02"] and kept[-1]["month"] == "2026-10"
    assert len(kept) == 5 * 12 + 10
    assert {r["month"]: r for r in kept}["2026-09"] == {k: v for k, v in september.items() if k != "last_year"}
    assert {r["month"]: r for r in kept}["2025-12"]["heat_meter"] == 1000.0


ZONES_MAP = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]


def test_the_routes(tmp_path):
    import json
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\n  language: it\nintegrations: []\nthermostat:\n  mode: heat\n"
                                          "  schedules_file: schedules.json\n  poll_interval_seconds: 600\n",
                                          encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({"zones": ZONES_MAP}), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        c = app.test_client()
        temps = c.get("/api/climate/temps").get_json()
        assert set(temps["zones"]) == {"Stanze", "Bagni"} and len(temps["days"]) == 7
        hours = c.get("/api/climate/run-hours").get_json()
        assert set(hours["zones"]) == {"Stanze", "Bagni"} and len(hours["days"]) == 30
        usage = c.get("/api/climate-admin/usage").get_json()
        this_year = date.today().year
        assert usage["zones"] == ["Stanze", "Bagni"] and usage["year"] == this_year
        assert usage["years"] == list(range(this_year, this_year - 6, -1)), "the six kept, each one to fill in"
        assert usage["years_with_data"] == [this_year]
        assert len(usage["months"]) == date.today().month and usage["this_month_hours"] is not None
        post = lambda body: c.post("/api/climate-admin/heat-meter/year", json=body)
        refusal = post({"year": this_year - 1, "months": {"12": {"heat_kwh": "x"}}})
        assert refusal.status_code == 400 and refusal.get_json()["code"] == "heat_meter_value_invalid"
        assert refusal.get_json()["params"] == {"month": f"12/{this_year - 1}"}
        saved = post({"year": this_year - 1, "months": {"12": {"heat_kwh": 10}}}).get_json()
        assert saved["year"] == this_year - 1 and saved["years_with_data"] == [this_year, this_year - 1]
        assert saved["months"][-1]["heat_meter"] == 10
        oldest = post({"year": this_year - 5, "months": {"03": {"heat_kwh": 4.5}}}).get_json()
        assert oldest["year"] == this_year - 5 and this_year - 5 in oldest["years_with_data"], "the oldest kept, filled in"
        assert c.get(f"/api/climate-admin/usage?year={this_year - 1}").get_json()["year"] == this_year - 1
        assert c.get("/api/climate-admin/usage?year=1990").get_json()["year"] == this_year

        # The CSV: every month kept, in the panel's language - its separator and decimal mark.
        res = c.get("/api/climate-admin/usage.csv")
        assert res.status_code == 200 and res.mimetype == "text/csv"
        assert "attachment" in res.headers["Content-Disposition"]
        text = res.get_data(as_text=True)
        assert text.startswith("﻿"), "read as UTF-8 by a spreadsheet"
        lines = text[1:].splitlines()
        head = lines[0].split(";")
        assert head[:4] == ["Mese", "Giorni", "Giorni con le ore", "Impianto acceso (ore)"]
        assert head[4:6] == ["Stanze (ore)", "Bagni (ore)"] and head[-1] == "Nota"
        assert len(lines) == 1 + 5 * 12 + date.today().month
        rows = {line.split(";")[0]: line.split(";") for line in lines[1:]}
        assert rows[f"{this_year - 5}-03"][head.index("Lettura caldo (kWh)")] == "4,500"
        assert rows[f"{this_year - 1}-12"][head.index("Lettura caldo (kWh)")] == "10,000"
        assert rows[f"{this_year}-{date.today().month:02d}"][-1] == "mese in corso"
    finally:
        app.config["HISTORY"].close()
