"""Exercise the actual driver logic (JSON parsing, field mapping, command
construction, clamping) against a FakeMqttConnection - no real broker, no
network, but real driver code, including the plain-string-payload path
that a live-broker test on 2026-09-12 caught a genuine bug in (see
services history / THIRD_PARTY_NOTICES for context - non-JSON payloads
used to be silently dropped).
"""
from __future__ import annotations

import json

import pytest

from localhome.drivers.mqtt.cover import MqttCoverDriver
from localhome.drivers.mqtt.number import MqttNumberDriver
from localhome.drivers.mqtt.sensor import MqttSensorDriver
from localhome.drivers.mqtt.switch import MqttSwitchDriver
from tests.mqtt_fakes import FakeMqttConnection

BROKER = {"host": "unused-in-tests"}


@pytest.fixture
def fake_connection(monkeypatch):
    conn = FakeMqttConnection()
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda broker: conn)
    return conn


# --------------------------------------------------------------- sensor ----

def test_sensor_raises_before_any_message_is_received(fake_connection):
    driver = MqttSensorDriver(
        name="Workshop Plug", broker=BROKER, state_topic="tele/plug/SENSOR",
        fields={"power_w": "ENERGY.Power"},
    )
    with pytest.raises(RuntimeError, match="no MQTT message"):
        driver.read()


def test_sensor_extracts_nested_json_fields(fake_connection):
    driver = MqttSensorDriver(
        name="Workshop Plug", broker=BROKER, state_topic="tele/plug/SENSOR",
        fields={"power_w": "ENERGY.Power", "voltage_v": "ENERGY.Voltage"},
    )
    fake_connection.deliver("tele/plug/SENSOR", json.dumps({"ENERGY": {"Power": 42.3, "Voltage": 231.0}}))

    reading = driver.read()

    assert reading == {"ok": True, "name": "Workshop Plug", "power_w": 42.3, "voltage_v": 231.0}


def test_sensor_history_fields_default_to_all_mapped_fields(fake_connection):
    driver = MqttSensorDriver(
        name="s", broker=BROKER, state_topic="t", fields={"power_w": "p", "voltage_v": "v"},
    )
    assert set(driver.history_fields) == {"power_w", "voltage_v"}


# ---------------------------------------------------- multi-topic sensor ----
# A Shelly H&T (and presumably other devices) splits one logical reading's
# fields across separate per-component topics rather than one combined
# payload - state_topic accepts a list for exactly this.

def test_sensor_merges_fields_from_multiple_topics(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    fake_connection.deliver("shelly/status/humidity:0", json.dumps({"rh": 47.0}))

    reading = driver.read()

    assert reading == {"ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5, "humidity_pct": 47.0}


def test_sensor_gives_a_partial_reading_when_only_some_topics_have_reported(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    fake_connection.deliver("shelly/status/temperature:0", json.dumps({"tC": 21.5}))
    # humidity topic never delivered anything yet

    reading = driver.read()

    assert reading == {"ok": True, "name": "Bathroom Sensor", "temperature_c": 21.5}


def test_sensor_raises_only_when_every_topic_has_nothing(fake_connection):
    driver = MqttSensorDriver(
        name="Bathroom Sensor", broker=BROKER,
        state_topic=["shelly/status/temperature:0", "shelly/status/humidity:0"],
        fields={"temperature_c": "tC", "humidity_pct": "rh"},
    )
    with pytest.raises(RuntimeError, match="no MQTT message"):
        driver.read()


# --------------------------------------------------------------- switch ----

def test_switch_reads_state_and_extra_fields(fake_connection):
    driver = MqttSwitchDriver(
        name="Living Room Lamp", broker=BROKER,
        state_topic="zigbee2mqtt/lamp", command_topic="zigbee2mqtt/lamp/set",
        fields={"power_w": "power"}, command_payload_field="state",
    )
    fake_connection.deliver("zigbee2mqtt/lamp", json.dumps({"state": "ON", "power": 8.5}))

    reading = driver.read()

    assert reading["is_on"] is True
    assert reading["power_w"] == 8.5


def test_switch_commands_use_the_json_payload_field_when_configured(fake_connection):
    driver = MqttSwitchDriver(
        name="Living Room Lamp", broker=BROKER,
        state_topic="zigbee2mqtt/lamp", command_topic="zigbee2mqtt/lamp/set",
        command_payload_field="state",
    )

    driver.turn_off()
    driver.turn_on()

    assert fake_connection.published == [
        ("zigbee2mqtt/lamp/set", json.dumps({"state": "OFF"})),
        ("zigbee2mqtt/lamp/set", json.dumps({"state": "ON"})),
    ]


def test_switch_commands_send_the_raw_payload_by_default(fake_connection):
    driver = MqttSwitchDriver(
        name="Tasmota Plug", broker=BROKER,
        state_topic="tele/plug/STATE", command_topic="cmnd/plug/POWER",
    )

    driver.turn_on()

    assert fake_connection.published == [("cmnd/plug/POWER", "ON")]


def test_switch_handles_a_plain_non_json_payload(fake_connection):
    # Regression test: Shelly Gen1-style relays publish a bare "on"/"off"
    # string, not JSON - state_field="" means "the whole payload, as-is".
    driver = MqttSwitchDriver(
        name="Gen1 Relay", broker=BROKER,
        state_topic="shellies/relay0/relay/0", command_topic="shellies/relay0/relay/0/command",
        state_field="", payload_on="on", payload_off="off", fields={},
    )
    fake_connection.deliver("shellies/relay0/relay/0", "on")

    reading = driver.read()

    assert reading["is_on"] is True


# --------------------------------------------------------------- number ----

def test_number_reads_the_current_value(fake_connection):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness",
    )
    fake_connection.deliver("shellies/vmc-dimmer/status", json.dumps({"brightness": 55}))

    assert driver.read()["value"] == 55.0


def test_number_clamps_set_value_to_the_configured_range(fake_connection):
    driver = MqttNumberDriver(
        name="VMC Dimmer", broker=BROKER,
        state_topic="shellies/vmc-dimmer/status", command_topic="shellies/vmc-dimmer/set",
        value_field="brightness", command_payload_field="brightness",
        min_value=0, max_value=100,
    )

    driver.set_value(150)
    driver.set_value(-10)

    assert fake_connection.published == [
        ("shellies/vmc-dimmer/set", json.dumps({"brightness": 100.0})),
        ("shellies/vmc-dimmer/set", json.dumps({"brightness": 0.0})),
    ]


# ---------------------------------------------------------------- cover ----

def test_cover_reports_unknown_state_before_any_message(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )
    assert driver.status()["state"] == "unknown"


def test_cover_maps_state_and_defaults_id_to_name(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )
    fake_connection.deliver("shellies/shutter1/status", json.dumps({"state": "open"}))

    assert driver.status()["state"] == "open"
    assert driver.id == "MQTT Shutter"


def test_cover_send_publishes_the_configured_action_payload(fake_connection):
    driver = MqttCoverDriver(
        name="MQTT Shutter", broker=BROKER,
        state_topic="shellies/shutter1/status", command_topic="shellies/shutter1/set",
    )

    driver.send("stop")

    assert fake_connection.published == [("shellies/shutter1/set", "stop")]
