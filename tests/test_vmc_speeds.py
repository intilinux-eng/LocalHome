"""The VMC's night/day/boost speeds set in the admin panel
(services/vmc_speeds.py): checked, laid over the device's presets with no
restart, followed by a program and by a unit running at an old speed."""
import time

import pytest

from localhome.services import vmc_speeds
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations:
  - kind: switch
    type: simulated
    name: "VMC"
    initial_on: {on}
  - kind: number
    type: simulated
    name: "VMC Speed"
    paired_switch: "VMC"
    on_with_value: true
    initial_value: {value}
    presets:
      - {{key: night, value: 20}}
      - {{key: day, value: 50}}
      - {{key: boost, value: 100}}
"""
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def test_speeds_are_whole_percentages_each_above_the_one_before():
    assert vmc_speeds.validate({"night": 25.4, "day": 50, "boost": 100}) == {"night": 25, "day": 50, "boost": 100}
    for bad, code in [({"night": 0, "day": 50, "boost": 100}, "vmc_speeds_invalid"),
                      ({"night": 20, "day": 50, "boost": 101}, "vmc_speeds_invalid"),
                      ({"night": 20, "day": 50}, "vmc_speeds_invalid"),
                      ({"night": 50, "day": 50, "boost": 100}, "vmc_speeds_order"),
                      ({"night": 60, "day": 50, "boost": 100}, "vmc_speeds_order")]:
        with pytest.raises(ValueError) as error:
            vmc_speeds.validate(bad)
        assert error.value.code == code


def test_a_week_moves_to_the_new_speeds():
    week = {d: [20] * 7 + [50] * 15 + [100, 20] for d in DAYS}
    moved = vmc_speeds.remap_week(week, {"night": 20, "day": 50, "boost": 100}, {"night": 25, "day": 55, "boost": 90})
    assert moved["mon"] == [25] * 7 + [55] * 15 + [90, 25]


@pytest.fixture
def board(tmp_path):
    def start(on=True, value=20):
        (tmp_path / "config.yaml").write_text(CONFIG.format(on=str(on).lower(), value=value), encoding="utf-8")
        app = create_app(str(tmp_path / "config.yaml"))
        client = app.test_client()
        deadline = time.time() + 5
        while client.get("/api/sensors/VMC%20Speed").get_json().get("value") is None and time.time() < deadline:
            time.sleep(0.05)
        return app, client
    return start


def _presets(client):
    devices = client.get("/api/devices").get_json()["sensors"]
    return [p["value"] for p in next(d for d in devices if d["name"] == "VMC Speed")["presets"]]


def test_the_device_speeds_until_some_are_set_then_those_at_once(board):
    _, client = board()
    body = client.get("/api/climate-admin/vmc-speeds").get_json()
    assert body["device"] == "VMC Speed" and body["saved"] is False
    assert body["speeds"] == {"night": 20, "day": 50, "boost": 100}

    res = client.post("/api/climate-admin/vmc-speeds", json={"night": 25, "day": 50, "boost": 100})

    assert res.get_json()["ok"] is True
    assert _presets(client) == [25, 50, 100], "no restart needed"
    assert client.get("/api/climate-admin/vmc-speeds").get_json()["saved"] is True


def test_a_program_keeps_its_meaning_and_a_unit_at_night_moves_to_the_new_night(board):
    _, client = board(on=True, value=20)
    week = {d: [20] * 7 + [50] * 15 + [20] * 2 for d in DAYS}
    client.post("/api/vmc-schedule", json={"week": week})

    client.post("/api/climate-admin/vmc-speeds", json={"night": 25, "day": 50, "boost": 100})

    assert client.get("/api/vmc-schedule").get_json()["schedule"]["week"]["mon"][:8] == [25] * 7 + [50]
    assert client.get("/api/sensors/VMC%20Speed").get_json()["value"] == 25


def test_a_unit_that_is_off_stays_off(board):
    _, client = board(on=False, value=20)

    client.post("/api/climate-admin/vmc-speeds", json={"night": 25, "day": 50, "boost": 100})

    assert client.get("/api/sensors/VMC").get_json()["is_on"] is False
    assert client.get("/api/sensors/VMC%20Speed").get_json()["value"] == 20


def test_wrong_speeds_are_refused_and_nothing_changes(board):
    _, client = board()

    res = client.post("/api/climate-admin/vmc-speeds", json={"night": 60, "day": 50, "boost": 100})

    assert res.status_code == 400 and res.get_json()["code"] == "vmc_speeds_order"
    assert _presets(client) == [20, 50, 100]
