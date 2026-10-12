"""Away from home the VMC runs at its minimum or night speed, or is off -
the resident's choice in the away sheet, remembered, night unless one is made
- over everything else (program, automatics, a timed boost, a hand pick),
and goes back to its program, or as it was, on return."""
import json
from datetime import datetime, timedelta

import pytest

from localhome.services.vmc_auto import DEFAULTS, VmcAuto, VmcAutoSettingsStore
from localhome.services.vmc_schedule import DAYS, VmcScheduleController, VmcScheduleStore
from localhome.web.app import create_app

SPEEDS = {"min": 10, "night": 25, "day": 50, "high": 75, "boost": 100}
TUESDAY = datetime(2026, 1, 13)


def at(hour, minute=0, days=0):
    return TUESDAY + timedelta(days=days, hours=hour, minutes=minute)


class FakeNumber:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)


class FakeSwitch:
    def __init__(self):
        self.calls = []

    def turn_on(self):
        self.calls.append("on")

    def turn_off(self):
        self.calls.append("off")


class FakePoller:
    def __init__(self, reading=None):
        self.reading = dict(reading or {})
        self.cache = self

    def merge(self, data):
        self.reading.update(data)

    def get(self):
        return dict(self.reading)


class FakeManager:
    def __init__(self, value=50, on=True):
        self.numbers = {"VMC Speed": FakeNumber()}
        self.switches = {"VMC": FakeSwitch()}
        self.paired_switch = {"VMC Speed": "VMC"}
        self.pollers = {"VMC Speed": FakePoller({"value": value}), "VMC": FakePoller({"is_on": on}),
                        "Bagni Sensor": FakePoller({"ok": True, "temperature_c": 22, "humidity_pct": 55})}

    def sensor_reading(self, name):
        poller = self.pollers.get(name)
        return poller.get() if poller else {"ok": False}

    @property
    def written(self):
        return self.numbers["VMC Speed"].values

    @property
    def switched(self):
        return self.switches["VMC"].calls

    def reading(self):
        return self.pollers["VMC Speed"].reading.get("value"), self.pollers["VMC"].reading.get("is_on")


class House:
    """The VMC's controller with the house's absence in its hands."""

    def __init__(self, tmp_path, program=None, value=50, on=True, auto=None):
        self.manager = FakeManager(value, on)
        self.until = None
        store = VmcScheduleStore(str(tmp_path / "vmc_schedule.json"))
        if program:
            store.set({"enabled": True, "target": "VMC Speed", "week": {d: list(program) for d in DAYS}})
        self.auto_store = VmcAutoSettingsStore(str(tmp_path / "vmc_auto.json"))
        if auto:
            self.auto_store.set(auto)
        self.store = store
        self.control = self.build()

    def build(self):
        auto = VmcAuto(self.manager, self.auto_store, season=lambda: "heat",
                       zones=lambda: [{"name": "Bagni", "sensor": "Bagni Sensor", "cool_enabled": False}],
                       outdoor_sensor=None, device=lambda: "VMC Speed", speeds=lambda device: SPEEDS)
        return VmcScheduleController(self.manager, self.store, auto=auto, away_until=lambda: self.until,
                                     device=lambda: "VMC Speed", speeds=lambda device: SPEEDS)

    def leave(self, mode, now, hours=24):
        self.until = now + timedelta(hours=hours)
        self.control.go_away(mode, now)

    def back(self, now):
        self.until = None
        self.control.tick(now)


def test_away_at_night_speed_then_back_as_it_was(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("night", at(9))
    assert house.manager.reading() == (25, True)
    assert house.control.status(now=at(9))["away"] == {"mode": "night", "device": "VMC Speed", "value": 25}
    house.back(at(18))
    assert house.manager.written == [25, 50] and "away" not in house.control.status(now=at(18))


def test_away_at_the_minimum_then_back_as_it_was(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("min", at(9))
    assert house.manager.reading() == (10, True)
    assert house.control.status(now=at(9))["away"] == {"mode": "min", "device": "VMC Speed", "value": 10}
    house.back(at(18))
    assert house.manager.reading() == (50, True)


def test_away_off_then_back_on_at_its_speed(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("off", at(9))
    assert house.manager.switched == ["off"] and house.manager.reading() == (50, False)
    house.back(at(18))
    assert house.manager.written == [50] and house.manager.switched == ["off", "on"]


def test_off_before_leaving_off_again_on_return(tmp_path):
    house = House(tmp_path, value=50, on=False)
    house.leave("night", at(9))
    assert house.manager.reading() == (25, True), "night means ventilating"
    house.back(at(18))
    assert house.manager.reading()[1] is False


def test_away_wins_over_the_program_and_back_its_speed(tmp_path):
    house = House(tmp_path, program=[25] * 7 + [50] * 15 + [25] * 2)
    house.control.tick(now=at(8))
    house.leave("off", at(9))
    house.control.tick(now=at(22, 1))           # the program's night begins: nothing
    house.control.tick(now=at(7, 1, days=1))    # its day: nothing
    assert house.manager.written == [50]
    house.back(at(12, days=1))
    assert house.manager.written == [50, 50] and house.manager.reading() == (50, True)


def test_away_wins_over_the_automatics(tmp_path):
    house = House(tmp_path, value=50, auto={**DEFAULTS, "shower": True})
    house.leave("night", at(9))
    for minute, rh in enumerate([55, 70, 85]):
        house.manager.pollers["Bagni Sensor"].reading["humidity_pct"] = rh
        house.control.tick(now=at(9, minute + 1))
    assert house.manager.written == [25]


def test_held_while_away_changed_elsewhere_it_s_put_back(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("night", at(9))
    house.manager.pollers["VMC Speed"].reading["value"] = 100     # the Shelly app
    house.control.tick(now=at(9, 5))
    assert house.manager.written == [25, 25]
    house.manager.pollers["VMC"].reading["is_on"] = False
    house.control.tick(now=at(9, 6))
    assert house.manager.reading() == (25, True)


def test_a_timed_boost_ends_and_back_home_is_from_before_it(tmp_path):
    house = House(tmp_path, value=50)
    house.control.boost("VMC Speed", 100, 60, now=at(9))
    house.leave("night", at(9, 10))
    assert house.store.get()["override"] is None
    house.back(at(18))
    assert house.manager.written[-1] == 50


def test_another_length_keeps_what_it_was_before(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("night", at(9))
    house.leave("off", at(10), hours=72)        # changed in the sheet: off, three days
    house.back(at(18))
    assert house.manager.reading() == (50, True)


def test_the_choice_is_remembered_and_keep_touches_nothing(tmp_path):
    house = House(tmp_path, value=50)
    assert house.store.get()["away_choice"] == "night"
    house.leave("off", at(9))
    house.back(at(18))
    assert house.store.get()["away_choice"] == "off"
    before = list(house.manager.written), list(house.manager.switched)
    house.leave("keep", at(19))
    house.back(at(20))
    assert (house.manager.written, house.manager.switched) == before
    assert house.store.get()["away_choice"] == "off", "keep isn't a choice to remember"


def test_a_restart_while_away_keeps_holding_and_still_knows_how_it_was(tmp_path):
    house = House(tmp_path, value=50)
    house.leave("night", at(9))
    house.control = house.build()                # LocalHome restarted
    house.manager.pollers["VMC Speed"].reading["value"] = 40
    house.control.tick(now=at(10))
    assert house.manager.reading() == (25, True)
    house.back(at(18))
    assert house.manager.reading() == (50, True)


# ------------------------------------------------------------------ API --

ZONES = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]
CONFIG = """
web:
  port: 5000
integrations: []
thermostat:
  mode: "heat"
  schedules_file: "schedules.json"
  poll_interval_seconds: 600
"""


@pytest.fixture
def app(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({
        "zones": ZONES, "extra_dimmers": {"vmc_mode": "simulated", "vmc_device_id": None}}), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    yield app
    app.config["HISTORY"].close()


def vmc_names(app):
    manager = app.config["MANAGER"]
    speed = next(n for n, s in manager.paired_switch.items() if n in manager.numbers)
    return speed, manager.paired_switch[speed]


def test_away_from_the_page_holds_the_vmc_refuses_its_controls_and_back_puts_it_back(app):
    c = app.test_client()
    speed, switch = vmc_names(app)
    manager = app.config["MANAGER"]
    assert c.post(f"/api/sensors/{speed}/set-value", json={"value": 50}).status_code == 200
    answer = c.post("/api/climate/away", json={"hours": 24, "vmc": "night"}).get_json()
    assert answer["ok"] and answer["vmc"] == "night"
    night = c.get("/api/climate-admin/vmc-speeds").get_json()["speeds"]["night"]
    assert manager.sensor_reading(speed)["value"] == night
    assert c.get("/api/vmc-schedule").get_json()["status"]["away"]["mode"] == "night"
    for path, body in ((f"/api/sensors/{speed}/set-value", {"value": 100}), (f"/api/sensors/{switch}/off", {}),
                       ("/api/vmc-schedule/boost", {"device": speed, "value": 100, "minutes": 30}),
                       ("/api/vmc-schedule", {"enabled": False})):
        refused = c.post(path, json=body)
        assert refused.status_code == 409 and refused.get_json()["code"] == "vmc_away", path
    assert c.post("/api/climate/away/cancel", json={}).get_json()["ok"]
    assert manager.sensor_reading(speed)["value"] == 50
    assert c.post(f"/api/sensors/{speed}/set-value", json={"value": 100}).status_code == 200


def test_without_a_choice_the_last_one_and_a_wrong_one_is_refused(app):
    c = app.test_client()
    assert c.post("/api/climate/away", json={"hours": 24}).get_json()["vmc"] == "night"
    c.post("/api/climate/away", json={"hours": 24, "vmc": "off"})
    c.post("/api/climate/away/cancel", json={})
    assert c.post("/api/climate/away", json={"hours": 24}).get_json()["vmc"] == "off"
    c.post("/api/climate/away/cancel", json={})
    refused = c.post("/api/climate/away", json={"hours": 24, "vmc": "max"})
    assert refused.status_code == 400 and refused.get_json()["code"] == "away_vmc_invalid"
    assert c.get("/api/climate/zones").get_json()["away_until"] is None, "refused before going away"
