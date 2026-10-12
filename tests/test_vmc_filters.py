"""The VMC's filter reminder: nothing until a cleaning is recorded, then
"soon" two weeks before the interval is up and "due" from that day;
cleaned today starts it over; the interval and the day are set in the
admin panel, checked."""
from datetime import date

import pytest

from localhome.errors import InputError
from localhome.services.vmc_filters import VmcFilterStore
from localhome.web.app import create_app


def store(tmp_path, today):
    clock = {"today": today}
    return VmcFilterStore(str(tmp_path / "vmc_filters.json"), today=lambda: clock["today"]), clock


def test_no_reminder_until_a_cleaning_is_recorded(tmp_path):
    filters, _ = store(tmp_path, date(2026, 10, 12))
    assert filters.status()["state"] == "unknown" and filters.status()["interval_days"] == 90


def test_soon_two_weeks_before_then_due_and_cleaned_starts_over(tmp_path):
    filters, clock = store(tmp_path, date(2026, 10, 12))
    filters.cleaned_today()
    assert filters.status()["due_on"] == "2027-01-10" and filters.status()["state"] == "ok"
    clock["today"] = date(2026, 12, 27)
    assert (filters.status()["state"], filters.status()["days_left"]) == ("soon", 14)
    clock["today"] = date(2027, 1, 10)
    assert filters.status()["state"] == "due"
    clock["today"] = date(2027, 1, 20)
    assert (filters.status()["state"], filters.status()["days_left"]) == ("due", -10)
    filters.cleaned_today()
    assert (filters.status()["state"], filters.status()["cleaned_on"]) == ("ok", "2027-01-20")


def test_the_interval_and_a_past_day_are_set_and_checked(tmp_path):
    filters, _ = store(tmp_path, date(2026, 10, 12))
    status = filters.set({"interval_days": 60, "cleaned_on": "2026-09-01"})
    assert (status["due_on"], status["state"]) == ("2026-10-31", "ok")
    for bad, code in (({"interval_days": 5}, "setting_range"), ({"cleaned_on": "2026-13-01"}, "vmc_filters_date_invalid"),
                      ({"cleaned_on": "2026-10-13"}, "vmc_filters_date_invalid")):
        with pytest.raises(InputError) as refused:
            filters.set(bad)
        assert refused.value.code == code
    assert filters.get() == {"interval_days": 60, "cleaned_on": "2026-09-01"}, "refusals change nothing"


def test_the_routes_and_the_status_with_the_vmc_answer(tmp_path):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        c = app.test_client()
        assert c.get("/api/vmc-schedule").get_json()["filters"]["state"] == "unknown"
        assert c.post("/api/vmc-filters/cleaned", json={}).get_json()["filters"]["state"] == "ok"
        assert c.get("/api/vmc-schedule").get_json()["filters"]["cleaned_on"] == date.today().isoformat()
        saved = c.post("/api/climate-admin/vmc-filters", json={"interval_days": 120}).get_json()
        assert saved["filters"]["interval_days"] == 120
        refused = c.post("/api/climate-admin/vmc-filters", json={"cleaned_on": "yesterday"})
        assert refused.status_code == 400 and refused.get_json()["code"] == "vmc_filters_date_invalid"
    finally:
        app.config["HISTORY"].close()
