"""The admin panel's VMC program routes: read, save (applied at once,
no restart) and the small status the dashboard shows."""
import json

import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""

SCHEDULE = {"enabled": True, "target": "VMC Speed",
            "day": {"at": "07:00", "value": 50}, "night": {"at": "22:00", "value": 20}}


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

    body = c.get("/api/climate-admin/vmc-schedule").get_json()

    assert body["ok"] is True
    assert body["schedule"]["enabled"] is False
    assert body["schedule"]["day"] == {"at": "07:00", "value": 50}
    assert body["status"] == {"ok": True, "enabled": False} or body["status"] == {"enabled": False}
    assert "VMC Speed" in body["devices"]


def test_saving_applies_the_speed_right_away(client):
    c, number, tmp_path = client

    body = c.post("/api/climate-admin/vmc-schedule", json=SCHEDULE).get_json()

    assert body["ok"] is True
    assert number.values, "the current period's speed is set on save, without a restart"
    assert number.values[-1] in (50, 20)
    assert json.loads((tmp_path / "vmc_schedule.json").read_text(encoding="utf-8"))["target"] == "VMC Speed"


def test_a_bad_schedule_is_refused_with_a_reason(client):
    c, number, _ = client

    res = c.post("/api/climate-admin/vmc-schedule", json={**SCHEDULE, "day": {"at": "25:00", "value": 50}})

    assert res.status_code == 400
    assert "07:00" in res.get_json()["error"]
    assert number.values == []


def test_the_dashboard_sees_the_running_program(client):
    c, _, _ = client
    c.post("/api/climate-admin/vmc-schedule", json=SCHEDULE)

    body = c.get("/api/vmc-schedule").get_json()

    assert body["ok"] is True and body["enabled"] is True
    assert body["period"] in ("day", "night")
    assert body["value"] in (50, 20)
    assert body["until"] in ("07:00", "22:00")
