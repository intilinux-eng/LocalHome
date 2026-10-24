"""The house's long-term record: a slot is the average of its looks and a
valve's open time a share of it; events come at their own time and with
why, never twice and never for what was there before a start; the past
is filled from the history once, never over what was recorded live; six
years are kept; and the CSV export gives it all back, a column group a
zone."""
import csv
import io
import json
import sqlite3

import pytest

from localhome.services.house_dataset import (
    SLOT_SECONDS,
    DatasetStore,
    HouseDataset,
    Plant,
    ZoneSource,
)
from localhome.web.app import create_app

T0 = 1_800_000_000          # a slot's start: a multiple of 300


class Manager:
    """Readings by name; a sensor's message ages with the clock, as a real one's."""
    def __init__(self, clock):
        self.clock = clock
        self.readings = {}
        self.driver_types = {}

    def sensor_reading(self, name):
        reading = dict(self.readings.get(name, {"ok": False}))
        if "received_at" in reading:
            reading["seconds_since_update"] = round(self.clock() - reading.pop("received_at"))
        return reading


class Schedule:
    def __init__(self):
        self.mode, self.away, self.diagnostic, self.overrides = "heat", None, False, {}

    def get_mode(self):
        return self.mode

    def get_away_until(self):
        return self.away

    def get_diagnostic_mode(self):
        return self.diagnostic

    def get_overrides(self, now):
        return self.overrides


class Thermostat:
    def __init__(self):
        self.schedule_store = Schedule()
        self.targets = {"Stanze": 21.0}
        self.failsafe = set()

    def applied_target(self, zone):
        return self.targets.get(zone)

    def is_failsafe_active(self, zone):
        return zone in self.failsafe


class House:
    def __init__(self, tmp_path):
        self.now = T0
        self.manager = Manager(lambda: self.now)
        self.thermostat = Thermostat()
        self.store = DatasetStore(str(tmp_path / "house_dataset.db"))
        self.plant = Plant(zones=[ZoneSource("Stanze", "Stanze Sensor", "Stanze Valve")], outdoor="Outdoor Sensor")
        self.dataset = HouseDataset(self.store, self.manager, self.thermostat, self.plant, clock=lambda: self.now)
        self.sensor(20.0, age=100)
        self.valve(False)
        self.manager.readings["Outdoor Sensor"] = {"ok": True, "temperature_c": 8.0, "humidity_pct": 80.0,
                                                   "received_at": T0 - 600}

    def sensor(self, temp, age=0, rh=55.0):
        """A message from the Stanze sensor, `age` seconds ago."""
        self.manager.readings["Stanze Sensor"] = {"ok": True, "temperature_c": temp, "humidity_pct": rh,
                                                  "received_at": self.now - age}

    def valve(self, on, rest=None):
        self.manager.readings["Stanze Valve"] = {"ok": True, "is_on": on, "safety_rest_at": rest}

    def look(self, seconds=30):
        self.dataset.sample()
        self.now += seconds

    def slots(self):
        return list(self.store.slots(0, 4e9))

    def events(self):
        return list(self.store.events(0, 4e9))


def test_a_slot_is_the_average_of_its_looks_and_a_valve_its_share_of_open_time(tmp_path):
    house = House(tmp_path)
    for i in range(10):
        house.valve(i < 4)
        if i == 5:
            house.sensor(20.5)
        house.look()
    house.look()                                   # the next slot begins: this one is written
    ((plant, zones),) = house.slots()
    assert plant["ts"] == T0 and plant["samples"] == 10 and plant["season"] == "heat" and plant["away"] == 0
    assert plant["outdoor_c"] == 8.0
    stanze = zones["Stanze"]
    assert stanze["valve_open"] == pytest.approx(0.4)
    assert stanze["temp_c"] == pytest.approx(20.25) and stanze["target_c"] == 21.0 and stanze["by_hand"] == 0


def test_events_at_their_own_time_with_why_never_for_what_was_there_before(tmp_path):
    house = House(tmp_path)
    house.look()                                   # the first look: nothing is news
    house.sensor(20.5, age=5)                       # a new message, 5 s ago
    house.valve(True)
    house.look()
    house.look()                                    # the same message, the same state: nothing
    assert len([e for e in house.dataset._events if e[1] == "Stanze Sensor"]) == 2
    house.thermostat.schedule_store.overrides = {"Stanze": {"target": 23.0}}
    house.valve(False)
    house.look()
    house.thermostat.schedule_store.overrides = {}
    house.thermostat.schedule_store.away = "2026-10-25T18:00"
    house.valve(True)
    house.look()
    house.valve(False, rest=T0 + 100)               # the Shelly's own timer: nobody renewed it
    house.look()
    house.dataset.flush()
    events = [(ts, device, what, value, why) for ts, device, what, value, why in house.events()]
    report_at = T0 + 30 - 5
    assert (report_at, "Stanze Sensor", "temperature_c", 20.5, "report") in events
    assert (report_at, "Stanze Sensor", "humidity_pct", 55.0, "report") in events
    switches = [(what, why) for _, device, what, _, why in events if device == "Stanze Valve"]
    assert switches == [("on", "schedule"), ("off", "by_hand"), ("on", "away"), ("off", "safety_timer")]
    held = [(value, why) for _, device, what, value, why in events if what == "by_hand"]
    assert held == [(1.0, "23"), (0.0, "schedule")]
    assert [(what, value) for _, device, what, value, _ in events if device == "plant"] == [("away", 1.0)]


def test_a_safety_timer_from_before_a_start_isnt_taken_for_one(tmp_path):
    house = House(tmp_path)
    house.valve(True, rest=T0 - 86400)              # a day old, already there
    house.look()
    house.valve(False, rest=T0 - 86400)
    house.look()
    house.dataset.flush()
    assert [why for *_, why in house.events()] == ["schedule"]


def test_six_years_are_kept_then_the_oldest_go(tmp_path):
    house = House(tmp_path)
    for _ in range(11):
        house.look()
    house.store.write({"ts": T0 - 7 * 366 * 86400, "samples": 1, "season": "heat", "away": 0, "diagnostic": 0,
                       "outdoor_c": 5.0, "outdoor_rh": None, "vmc_pct": None, "vmc_on": None,
                       "dehumidifier_on": None, "bypass_on": None, "flags": None}, [], [])
    assert len(house.slots()) == 2
    assert house.store.prune(T0) == 1
    assert [plant["ts"] for plant, _ in house.slots()] == [T0]


def test_the_past_is_filled_from_the_history_once_and_never_over_what_was_recorded(tmp_path):
    history = tmp_path / "history.db"
    conn = sqlite3.connect(history)
    conn.execute("CREATE TABLE readings (ts INTEGER NOT NULL, sensor TEXT NOT NULL, field TEXT NOT NULL, value REAL NOT NULL)")
    rows = []
    for i in range(20):                             # two slots before T0, 30 s apart
        ts = T0 - 2 * SLOT_SECONDS + 30 * i
        rows += [(ts, "Stanze Sensor", "temperature_c", 19.0 + (i >= 10)), (ts, "Stanze Valve", "is_on", float(i % 2)),
                 (ts, "Stanze Sensor", "target_c", 21.0), (ts, "Outdoor Sensor", "temperature_c", 7.0)]
    rows.append((T0, "Stanze Sensor", "temperature_c", 99.0))   # the live slot's time: not taken
    conn.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    house = House(tmp_path)
    house.dataset.history_path = str(history)
    house.look()
    assert house.dataset.backfill() == 2
    assert house.dataset.backfill() == 0, "once in the file's life"
    for _ in range(10):
        house.look()
    slots = house.slots()
    assert [plant["ts"] for plant, _ in slots] == [T0 - 600, T0 - 300, T0]
    first, second, live = slots
    assert first[0]["flags"] == "backfill" and first[0]["season"] is None and first[0]["outdoor_c"] == 7.0
    assert first[1]["Stanze"]["temp_c"] == 19.0 and second[1]["Stanze"]["temp_c"] == 20.0
    assert first[1]["Stanze"]["valve_open"] == pytest.approx(0.5) and first[1]["Stanze"]["target_c"] == 21.0
    assert live[1]["Stanze"]["temp_c"] == 20.0 and live[0]["flags"] is None


# ------------------------------------------------------------ in the app --

CONFIG = """
web:
  port: 5000
integrations: []
thermostat:
  mode: "heat"
  schedules_file: "schedules.json"
  poll_interval_seconds: 600
"""
ZONES = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]


def _app(tmp_path, monkeypatch, config=CONFIG):
    monkeypatch.setattr(HouseDataset, "start", lambda self: None)
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({
        "zones": ZONES, "extra_sensors": {"outdoor_sensor_mode": "simulated", "outdoor_sensor_device_id": None},
        "extra_dimmers": {"vmc_mode": "simulated", "vmc_device_id": None}}), encoding="utf-8")
    return create_app(str(tmp_path / "config.yaml"))


def test_the_app_records_the_house_and_gives_it_back_as_csv(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        dataset = app.config["HOUSE_DATASET"]
        assert [z.name for z in dataset.plant.zones] == ["Stanze", "Bagni"]
        assert dataset.plant.outdoor == "Outdoor Sensor" and dataset.plant.vmc_speed()["paired_switch"]
        for _ in range(3):
            dataset.sample()
        dataset.flush()
        c = app.test_client()
        summary = c.get("/api/climate-admin/dataset").get_json()
        assert summary["enabled"] and summary["slots"] == 1 and summary["retention_years"] == 6
        answer = c.get("/api/climate-admin/dataset.csv")
        assert answer.status_code == 200 and answer.mimetype == "text/csv"
        rows = list(csv.reader(io.StringIO(answer.get_data(as_text=True))))
        head, row = rows[0], rows[1]
        assert head[:4] == ["time_utc", "time_local", "samples", "season"]
        assert {"Stanze_temp_c", "Stanze_valve_open", "Bagni_target_c", "Bagni_flags"} <= set(head)
        values = dict(zip(head, row))
        assert values["samples"] == "3" and values["season"] == "heat" and values["Stanze_flags"] == "simulated"
        assert c.get("/api/climate-admin/dataset.csv?what=events").get_data(as_text=True).startswith("time_utc,")
        refused = c.get("/api/climate-admin/dataset.csv?from=ieri")
        assert refused.status_code == 400 and refused.get_json()["code"] == "dataset_dates_invalid"
        assert c.get("/api/climate-admin/dataset.csv?what=all").get_json()["code"] == "dataset_what_invalid"
    finally:
        app.config["HISTORY"].close()


def test_switched_off_in_config_yaml(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, CONFIG + "dataset:\n  enabled: false\n")
    try:
        assert app.config["HOUSE_DATASET"] is None
        c = app.test_client()
        assert c.get("/api/climate-admin/dataset").get_json() == {"ok": True, "enabled": False}
        assert c.get("/api/climate-admin/dataset.csv").status_code == 404
    finally:
        app.config["HISTORY"].close()
