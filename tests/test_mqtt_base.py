import json

import pytest

from localhome.drivers.mqtt.base import MqttJsonState, command_payload, require_payload
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
