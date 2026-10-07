"""The whole chain a zone's valve follows, with Shelly-like devices on a
fake broker: a target set through the API, the thermostat, the MQTT
drivers - the H&T's readings, the Plus 1's held commands - and the relay,
in both seasons. The rule (services/thermostat.py's decide()): the valve
stops as soon as the room reaches the target and opens again the
hysteresis away from it on the season's side - above it cooling, below it
heating - and an economy target (cooling from 26°C, heating below 20°C)
the economy tolerance (0.5 by default) further on; in between it keeps
its state.

The devices answer as the real ones were measured to (docs/shelly-gen3.md):
"on,N" holds for N seconds, a plain "on"/"off" has no timer, anything else
is ignored, "status_update" answers with the status, and every change is
published with its source. Each test step checks the relay itself and what
LocalHome says of it."""
from __future__ import annotations

import json
import time

import pytest

from localhome.web.app import create_app

HT = "shellyhtg3-aaaaaaaaaaaa"
PLUS1 = "shellyplus1-bbbbbbbbbbbb"
HYSTERESIS = 0.5

CONFIG = """
web:
  port: 5000

thermostat:
  mode: "cool"
  hysteresis_c: 0.5
  schedules_file: "schedules.json"
"""

CLIMATE_MAP = {"zones": [
    {"name": "Stanze", "sensor_mode": "shelly", "sensor_device_id": HT,
     "valve_mode": "shelly", "valve_device_id": PLUS1, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]}


class Broker:
    """Each message to the topic's subscribers at once - the devices' own
    included - and the retained ones to whoever subscribes later."""

    def __init__(self):
        self.subscribers: dict[str, list] = {}
        self.retained: dict[str, bytes] = {}
        self.log: list[tuple[str, str]] = []

    def subscribe(self, topic, callback):
        self.subscribers.setdefault(topic, []).append(callback)
        if topic in self.retained:
            callback(topic, self.retained[topic])

    def publish(self, topic, payload, qos=0, retain=False):
        data = payload.encode() if isinstance(payload, str) else payload
        self.log.append((topic, data.decode(errors="replace")))
        if retain:
            self.retained[topic] = data
        for callback in list(self.subscribers.get(topic, [])):
            callback(topic, data)


class Plus1:
    """A Shelly Plus 1, as measured."""

    def __init__(self, broker: Broker, device_id: str):
        self.broker, self.id = broker, device_id
        self.output, self.source, self.timer, self.started = False, "init", None, None
        self.connected = True
        broker.subscribe(f"{device_id}/command/switch:0", self._command)
        broker.publish(f"{device_id}/online", "true", retain=True)

    def _command(self, topic, payload):
        if not self.connected:
            return
        text = payload.decode()
        if text == "status_update":
            self._status()
            return
        word, _, seconds = text.partition(",")
        if word not in ("on", "off"):
            return                                  # "True", "1"...: ignored without a word
        self.output, self.source = word == "on", "mqtt"
        self.timer, self.started = (float(seconds), time.time()) if seconds else (None, None)
        self._status()

    def _status(self):
        body = {"id": 0, "source": self.source, "output": self.output}
        if self.timer:
            body.update(timer_started_at=self.started, timer_duration=self.timer)
        self.broker.publish(f"{self.id}/status/switch:0", json.dumps(body))

    def timer_runs_out(self):
        """Its hold not renewed for its whole length: back off by itself."""
        self.output, self.source, self.timer = False, "timer", None
        self._status()

    def unplug(self):
        self.connected = False
        self.broker.publish(f"{self.id}/online", "false", retain=True)   # its Last Will

    def plug_in(self):
        """Power back: it starts off (its setting), says it's online - and,
        like the real one, doesn't publish its status unasked."""
        self.output, self.source, self.timer = False, "init", None
        self.connected = True
        self.broker.publish(f"{self.id}/online", "true", retain=True)


class Plant:
    def __init__(self, app, broker: Broker, relay: Plus1):
        self.app, self.broker, self.relay = app, broker, relay
        self.client = app.test_client()
        self.manager = app.config["MANAGER"]
        self.thermostat = app.config["THERMOSTAT"]

    def room(self, temperature: float):
        """The H&T reports - on its own, as it does on a 0.5° change."""
        self.broker.publish(f"{HT}/status/temperature:0", json.dumps({"id": 0, "tC": temperature}))
        self.manager.pollers["Stanze Sensor"].poll_once()
        self.settle()

    def target(self, target: float | None):
        """A temporary target from the home page."""
        body = self.client.post("/api/climate/zones/Stanze/override", json={"target": target, "hours": 1}).get_json()
        assert body["ok"], body
        self.settle()

    def season(self, mode: str):
        assert self.client.post("/api/climate/mode", json={"mode": mode}).get_json()["ok"]
        self.settle()

    def settle(self):
        """What the background loops would do within a few seconds."""
        valve = self.manager.pollers["Stanze Valve"]
        valve.poll_once()
        self.thermostat.tick()
        valve.poll_once()

    def zone(self) -> dict:
        return next(z for z in self.client.get("/api/climate/zones").get_json()["zones"] if z["name"] == "Stanze")

    def commands(self) -> list[str]:
        return [payload for topic, payload in self.broker.log if topic == f"{PLUS1}/command/switch:0"]


@pytest.fixture
def plant(tmp_path, monkeypatch):
    broker = Broker()
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda _: broker)
    monkeypatch.setattr("localhome.drivers.mqtt.rpc.get_connection", lambda _: broker)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps(CLIMATE_MAP), encoding="utf-8")
    (tmp_path / "mqtt_broker.json").write_text(json.dumps({"host": "fake-broker", "port": 1883}), encoding="utf-8")
    relay = Plus1(broker, PLUS1)
    app = create_app(str(tmp_path / "config.yaml"))
    return Plant(app, broker, relay)


# step: ("room", °C) or ("target", °C or None), then the relay expected
COOLING = [
    (("room", 26.0), None),
    (("target", 24.0), True),     # comfort: 26.0 at or above 24.5 opens
    (("room", 24.3), True),       # between 24.0 and 24.5: stays open
    (("room", 24.0), False),      # the target reached: stops
    (("room", 24.3), False),      # between: stays closed
    (("room", 24.5), True),       # half a degree above: opens again
    (("target", 26.0), False),    # economy: 24.5 at or below 26.0 stops
    (("room", 26.8), False),      # not yet - a comfort target would have opened at 26.5
    (("room", 27.0), True),       # economy opens a degree above
    (("room", 26.0), False),      # and stops at the target
    (("target", 23.0), True),     # 26.0 at or above 23.5: opens
    (("target", None), False),    # off
]
HEATING = [
    (("room", 18.0), None),
    (("target", 20.0), True),     # comfort: 18.0 at or below 19.5 opens
    (("room", 19.7), True),       # between 19.5 and 20.0: stays open
    (("room", 20.0), False),      # the target reached: stops
    (("room", 19.7), False),      # between: stays closed
    (("room", 19.5), True),       # half a degree below: opens again
    (("target", 19.0), False),    # economy: 19.5 at or above 19.0 stops
    (("room", 18.2), False),      # not yet - a comfort target would have opened at 18.5
    (("room", 18.0), True),       # economy opens a degree below
    (("room", 19.0), False),      # and stops at the target
    (("target", 21.0), True),     # 19.0 at or below 20.5: opens
    (("target", None), False),    # off
]


@pytest.mark.parametrize("mode,steps", [("cool", COOLING), ("heat", HEATING)])
def test_the_relay_follows_the_target_in_both_seasons(plant, mode, steps):
    plant.season(mode)
    for (what, value), expected in steps:
        plant.room(value) if what == "room" else plant.target(value)
        if expected is None:
            continue
        zone = plant.zone()
        where = f"{mode}: {what} {value} (room {zone['temperature_c']}, target {zone['target_c']})"
        assert plant.relay.output is expected, where
        assert zone["valve_on"] is expected and zone["valve_ok"] is True, where
        if expected:
            assert plant.relay.timer == 900, f"{where}: held, so it closes by itself if the board stops"
    sent = plant.commands()
    assert "on" not in sent and all(c in ("on,900", "off", "status_update") for c in sent), sent


def test_a_season_change_with_the_valve_open_closes_it_unless_the_new_season_wants_it(plant):
    plant.season("cool")
    plant.room(26.0)
    plant.target(24.0)
    assert plant.relay.output is True
    plant.season("heat")                    # the temporary target goes with the season: no target, closed
    assert plant.relay.output is False and plant.zone()["valve_on"] is False
    plant.target(28.0)                      # heating to 28 in a 26° room: opens
    assert plant.relay.output is True


def test_a_shelly_unplugged_and_back_is_found_off_and_opened_again(plant):
    plant.season("cool")
    plant.room(26.0)
    plant.target(24.0)
    assert plant.relay.output is True
    plant.relay.unplug()
    for _ in range(5):
        plant.settle()
    zone = plant.zone()
    assert zone["valve_ok"] is False, "said: its valve doesn't answer"
    before = len(plant.commands())
    for _ in range(5):
        plant.settle()
    assert len(plant.commands()) == before, "not commanded on every tick while it's gone"
    plant.relay.plug_in()                   # back off: power-on state "off"
    plant.settle()
    plant.settle()
    assert plant.relay.output is True, "asked, found off, opened again"
    assert plant.zone()["valve_on"] is True and plant.zone()["valve_ok"] is True


def test_a_hold_that_ran_out_is_noted_and_the_valve_opened_again(plant):
    plant.season("cool")
    plant.room(26.0)
    plant.target(24.0)
    plant.relay.timer_runs_out()            # its renewals didn't get through for 15 minutes
    plant.settle()
    assert plant.relay.output is True, "the thermostat still wants it open"
    recent = plant.client.get("/api/system-status").get_json()["recent"]
    assert [(e["code"], e["subject"]) for e in recent] == [("safety_timer", "Stanze Valve")]
