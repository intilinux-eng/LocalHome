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


def test_a_connection_lost_and_back_is_said_in_the_log(caplog):
    """On the production board the broker dropped LocalHome's connection
    for 90 s without a keepalive: only the broker's log told. Now
    LocalHome's own log says it went, why, and how long it was gone."""
    import logging
    import time

    conn = MqttConnection(host="192.0.2.1", port=1883, username=None, password=None)
    ok = type("Reason", (), {"is_failure": False, "__str__": lambda self: "Success"})()
    lost = type("Reason", (), {"is_failure": True, "__str__": lambda self: "Keep alive timeout"})()
    with caplog.at_level(logging.INFO, logger="localhome.drivers.mqtt.client"):
        conn._on_connect(conn._client, None, None, ok, None)
        conn._on_disconnect(conn._client, None, None, lost, None)
        conn._on_disconnect(conn._client, None, None, lost, None)       # retries failing: said once
        conn.down_since = time.monotonic() - 92
        conn._on_connect(conn._client, None, None, ok, None)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["MQTT broker 192.0.2.1:1883: connected",
                        "MQTT broker 192.0.2.1:1883: connection lost (Keep alive timeout) - reconnecting",
                        "MQTT broker 192.0.2.1:1883: connected again after 92 s"]
    assert conn.down_since is None
