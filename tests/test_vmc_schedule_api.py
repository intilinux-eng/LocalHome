"""The VMC program's routes, used by its card on the dashboard: read,
save (applied at once, no restart) and the status shown on the card."""
import json

import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""

DAY_AND_NIGHT = {d: [20] * 7 + [50] * 15 + [20] * 2 for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
SCHEDULE = {"enabled": True, "target": "VMC Speed", "week": DAY_AND_NIGHT}


class FakeNumber:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    number = FakeNumber()
    app.config["MANAGER"].numbers["VMC Speed"] = number
    yield app.test_client(), number, tmp_path
    app.config["HISTORY"].close()


def test_defaults_before_anything_is_saved(client):
    c, _, _ = client

    body = c.get("/api/vmc-schedule").get_json()

    assert body["ok"] is True
    assert body["schedule"]["enabled"] is False
    assert body["schedule"]["week"]["mon"][7] == 50 and body["schedule"]["week"]["mon"][22] == 25, "day 7-22"
    assert body["status"] == {"enabled": False}


def test_saving_applies_the_speed_right_away(client):
    c, number, tmp_path = client

    body = c.post("/api/vmc-schedule", json=SCHEDULE).get_json()

    assert body["ok"] is True
    assert number.values, "the current period's speed is set on save, without a restart"
    assert number.values[-1] in (50, 20)
    assert json.loads((tmp_path / "vmc_schedule.json").read_text(encoding="utf-8"))["target"] == "VMC Speed"


def test_a_bad_schedule_is_refused_with_a_reason(client):
    c, number, _ = client

    res = c.post("/api/vmc-schedule", json={**SCHEDULE, "week": {**DAY_AND_NIGHT, "mon": [20] * 23 + [150]}})

    assert res.status_code == 400
    assert res.get_json()["code"] == "vmc_week_invalid"
    assert number.values == []


def test_the_card_sees_the_running_program(client):
    c, _, _ = client
    c.post("/api/vmc-schedule", json=SCHEDULE)

    body = c.get("/api/vmc-schedule").get_json()

    assert body["ok"] is True and body["status"]["enabled"] is True
    assert body["status"]["value"] in (50, 20)
    assert body["status"]["until"] in ("07:00", "22:00")
    assert body["schedule"]["target"] == "VMC Speed"


WEEK = {d: [20] * 7 + [100] + [50] * 11 + [100] + [50] * 2 + [20] * 2
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def test_a_painted_week_is_saved(client):
    c, _, tmp_path = client

    body = c.post("/api/vmc-schedule", json={"week": WEEK}).get_json()

    assert body["ok"] is True and body["schedule"]["week"] == WEEK
    assert json.loads((tmp_path / "vmc_schedule.json").read_text(encoding="utf-8"))["week"] == WEEK


def test_turning_it_on_keeps_the_painted_week(client):
    c, number, _ = client
    c.post("/api/vmc-schedule", json={"week": WEEK})

    body = c.post("/api/vmc-schedule", json={"enabled": True, "target": "VMC Speed"}).get_json()

    assert body["schedule"]["week"] == WEEK and body["status"]["enabled"] is True
    assert number.values, "the speed of the hour is applied at once"


def test_a_bad_week_is_refused(client):
    c, number, _ = client

    res = c.post("/api/vmc-schedule", json={"week": {**WEEK, "sun": [20] * 23}})

    assert res.status_code == 400 and res.get_json()["code"] == "vmc_week_invalid"
    assert number.values == []


def test_a_timed_boost_and_its_cancel(client):
    c, number, _ = client

    body = c.post("/api/vmc-schedule/boost", json={"device": "VMC Speed", "value": 100, "minutes": 30}).get_json()

    assert body["ok"] is True and body["status"]["override"]["value"] == 100
    assert number.values == [100]
    body = c.post("/api/vmc-schedule/boost/cancel").get_json()
    assert "override" not in body["status"]


@pytest.mark.parametrize("bad", [
    {"device": "VMC Speed", "value": 100, "minutes": 0},
    {"device": "VMC Speed", "value": 140, "minutes": 30},
    {"device": "Nowhere", "value": 100, "minutes": 30},
    {"device": "VMC Speed", "value": "100", "minutes": 30},
])
def test_a_bad_boost_is_refused(client, bad):
    c, number, _ = client

    res = c.post("/api/vmc-schedule/boost", json=bad)

    assert res.status_code == 400 and res.get_json()["code"] == "vmc_override_invalid"
    assert number.values == []


def test_what_the_route_answered_sent_back_whole_keeps_the_week(client):
    """A page posting back the whole schedule it read changes only what it
    changed."""
    c, _, _ = client
    schedule = c.post("/api/vmc-schedule", json={"week": WEEK}).get_json()["schedule"]

    body = c.post("/api/vmc-schedule", json={**schedule, "enabled": True, "target": "VMC Speed"}).get_json()

    assert body["ok"] is True and body["schedule"]["week"] == WEEK
