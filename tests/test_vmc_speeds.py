"""The VMC's five speeds - minimum, night, day, high, boost - set in the
admin panel (services/vmc_speeds.py): checked, laid over the device's
presets with no restart, followed by a program and by a unit running at
an old speed; a file or a device with the first three gets the other two
worked out from them."""
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


FIVE = {"min": 10, "night": 25, "day": 50, "high": 75, "boost": 100}


def test_speeds_are_whole_percentages_each_above_the_one_before():
    assert vmc_speeds.validate({**FIVE, "night": 25.4}) == FIVE
    for bad, code in [({**FIVE, "min": 0}, "vmc_speeds_invalid"),
                      ({**FIVE, "boost": 101}, "vmc_speeds_invalid"),
                      ({"night": 20, "day": 50, "boost": 100}, "vmc_speeds_invalid"),
                      ({**FIVE, "min": 25}, "vmc_speeds_order"),
                      ({**FIVE, "high": 45}, "vmc_speeds_order"),
                      ({**FIVE, "night": 60}, "vmc_speeds_order")]:
        with pytest.raises(ValueError) as error:
            vmc_speeds.validate(bad)
        assert error.value.code == code


def test_the_minimum_and_high_are_worked_out_from_the_first_three():
    assert vmc_speeds.complete({"night": 25, "day": 50, "boost": 100}) == FIVE
    # 10 and 75 don't fit: half of night, halfway between day and boost
    assert vmc_speeds.complete({"night": 8, "day": 30, "boost": 60}) == {"min": 4, "night": 8, "day": 30, "high": 45,
                                                                          "boost": 60}
    assert "min" not in vmc_speeds.complete({"night": 1, "day": 50, "boost": 100})
    assert vmc_speeds.complete(FIVE) == FIVE, "set ones are kept"


def test_an_older_file_with_three_speeds_reads_as_five(tmp_path):
    path = tmp_path / "vmc_speeds.json"
    path.write_text('{"night": 25, "day": 50, "boost": 100}', encoding="utf-8")   # the production board's
    assert vmc_speeds.VmcSpeedStore(str(path)).get() == FIVE


def test_a_device_with_three_presets_shows_five_slowest_first():
    presets = [{"key": "night", "value": 20}, {"key": "day", "value": 50}, {"key": "boost", "value": 100},
               {"key": "other", "value": 33}]
    applied = vmc_speeds.apply(presets, None)
    assert [(p["key"], p["value"]) for p in applied] == [("min", 10), ("night", 20), ("day", 50), ("high", 75),
                                                         ("boost", 100), ("other", 33)]
    assert [p["value"] for p in vmc_speeds.apply(presets, {**FIVE, "min": 5})][:2] == [5, 25]


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
    assert body["speeds"] == {**FIVE, "night": 20}

    res = client.post("/api/climate-admin/vmc-speeds", json={**FIVE, "min": 5})

    assert res.get_json()["ok"] is True
    assert _presets(client) == [5, 25, 50, 75, 100], "no restart needed"
    assert client.get("/api/climate-admin/vmc-speeds").get_json()["saved"] is True


def test_a_program_keeps_its_meaning_and_a_unit_at_night_moves_to_the_new_night(board):
    _, client = board(on=True, value=20)
    week = {d: [20] * 7 + [50] * 15 + [20] * 2 for d in DAYS}
    client.post("/api/vmc-schedule", json={"week": week})

    client.post("/api/climate-admin/vmc-speeds", json=FIVE)

    assert client.get("/api/vmc-schedule").get_json()["schedule"]["week"]["mon"][:8] == [25] * 7 + [50]
    assert client.get("/api/sensors/VMC%20Speed").get_json()["value"] == 25


def test_a_unit_that_is_off_stays_off(board):
    _, client = board(on=False, value=20)

    client.post("/api/climate-admin/vmc-speeds", json=FIVE)

    assert client.get("/api/sensors/VMC").get_json()["is_on"] is False
    assert client.get("/api/sensors/VMC%20Speed").get_json()["value"] == 20


def test_wrong_speeds_are_refused_and_nothing_changes(board):
    _, client = board()

    res = client.post("/api/climate-admin/vmc-speeds", json={**FIVE, "night": 60})

    assert res.status_code == 400 and res.get_json()["code"] == "vmc_speeds_order"
    assert _presets(client) == [10, 20, 50, 75, 100]
