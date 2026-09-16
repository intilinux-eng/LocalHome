"""MqttConnection.publish() itself (drivers/mqtt/client.py) - a real
paho client, no broker, nothing mocked: connect_async() only *schedules*
the handshake on a background thread, so a publish() called immediately
after construction is guaranteed to see a not-yet-connected client - not
a race, since a real TCP handshake can't complete synchronously inside
__init__. Every other MQTT test in this project fakes this class away
entirely (see tests/mqtt_fakes.py) - this file is the one place testing
the real class's own publish-confirmation behavior.
"""
from __future__ import annotations

import pytest

from localhome.drivers.mqtt.client import MqttConnection


def test_publish_before_connecting_raises_instead_of_silently_no_opping():
    # 192.0.2.1 (RFC 5737 TEST-NET-1) never connects, deterministically -
    # not relying on anything actually being unreachable on whatever
    # network the test happens to run on.
    conn = MqttConnection(host="192.0.2.1", port=1883, username=None, password=None)

    with pytest.raises(ConnectionError, match="MQTT publish"):
        conn.publish("some/topic", "payload")
