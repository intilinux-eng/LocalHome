import json

import pytest

from localhome.drivers.mqtt.base import Availability, MqttJsonState, command_payload, require_payload
from tests.mqtt_fakes import FakeMqttConnection

BROKER = {"host": "unused-in-tests"}


def test_raw_payload_when_no_field_given():
    assert command_payload("ON", None) == "ON"
    assert command_payload(42.5, None) == "42.5"


def test_json_payload_when_field_given():
    assert json.loads(command_payload("ON", "state")) == {"state": "ON"}
    assert json.loads(command_payload(42.5, "brightness")) == {"brightness": 42.5}


# --------------------------------------------------- staleness detection ----
# MQTT is push-based: a dead device just stops publishing, and without
# this, the last message it ever sent would be cached and returned as a
# fine, current "ok": True reading forever - see require_payload()'s
# docstring-equivalent comment in base.py for why this is opt-in
# (stale_after_seconds=None) rather than always on.

@pytest.fixture
def fake_connection(monkeypatch):
    conn = FakeMqttConnection()
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda broker: conn)
    return conn


@pytest.fixture
def fake_clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.drivers.mqtt.base.time.time", lambda: now["t"])

    def advance(seconds):
        now["t"] += seconds

    return advance


def test_stale_after_seconds_unset_never_expires_a_message(fake_connection, fake_clock):
    state = MqttJsonState(BROKER, "topic")  # stale_after_seconds defaults to None
    fake_connection.deliver("topic", json.dumps({"ok": True}))

    fake_clock(10_000)  # a switch that only publishes on change can go quiet this long and still be fine

    assert require_payload(state) == {"ok": True}


def test_stale_after_seconds_accepts_a_message_within_the_window(fake_connection, fake_clock):
    state = MqttJsonState(BROKER, "topic", stale_after_seconds=300)
    fake_connection.deliver("topic", json.dumps({"ok": True}))

    fake_clock(299)

    assert require_payload(state) == {"ok": True}


def test_stale_after_seconds_rejects_a_message_past_the_window(fake_connection, fake_clock):
    state = MqttJsonState(BROKER, "topic", stale_after_seconds=300)
    fake_connection.deliver("topic", json.dumps({"ok": True}))

    fake_clock(301)

    with pytest.raises(RuntimeError, match="no MQTT message in"):
        require_payload(state)


def test_a_fresh_message_resets_the_staleness_clock(fake_connection, fake_clock):
    state = MqttJsonState(BROKER, "topic", stale_after_seconds=300)
    fake_connection.deliver("topic", json.dumps({"reading": 1}))
    fake_clock(299)
    fake_connection.deliver("topic", json.dumps({"reading": 2}))  # device is alive after all
    fake_clock(299)

    assert require_payload(state) == {"reading": 2}


# ------------------------------------------------------------ availability ----
# A separate, opt-in "is this device even connected" signal (most
# commonly a device's own MQTT Last Will and Testament) - see
# drivers/mqtt/base.py's Availability docstring for why this exists
# alongside stale_after_seconds rather than instead of it.

def test_availability_is_none_when_no_topic_is_configured(fake_connection):
    availability = Availability(BROKER, None)
    assert availability.offline_reason() is None


def test_availability_is_none_before_anything_is_ever_received(fake_connection):
    availability = Availability(BROKER, "shellyht-XXXX/online")
    assert availability.offline_reason() is None


def test_availability_is_none_when_the_device_reports_online(fake_connection):
    availability = Availability(BROKER, "shellyht-XXXX/online")
    fake_connection.deliver("shellyht-XXXX/online", "true")
    assert availability.offline_reason() is None


def test_availability_reports_offline_when_the_device_says_so(fake_connection):
    availability = Availability(BROKER, "shellyht-XXXX/online")
    fake_connection.deliver("shellyht-XXXX/online", "false")
    reason = availability.offline_reason()
    assert reason is not None
    assert "offline" in reason


def test_availability_payload_match_is_case_insensitive(fake_connection):
    availability = Availability(BROKER, "topic", available_payload="ON")
    fake_connection.deliver("topic", "on")
    assert availability.offline_reason() is None


def test_availability_custom_available_payload(fake_connection):
    # Not every ecosystem uses Shelly's "true"/"false" - available_payload
    # lets a device that reports e.g. "online"/"offline" work the same way.
    availability = Availability(BROKER, "topic", available_payload="online")
    fake_connection.deliver("topic", "offline")
    assert availability.offline_reason() is not None
