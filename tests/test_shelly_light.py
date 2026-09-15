"""Exercise the Shelly RPC-over-MQTT light driver (drivers/mqtt/rpc.py +
drivers/mqtt/shelly_light.py) against a FakeMqttConnection: a background
thread plays the role of the real device, replying to whatever request
was just published - no real broker, no network, but the real
request/response correlation and JSON-shape logic, including the
RPC-error and reply-timeout paths a live-broker test on 2026-09-15
confirmed matter for this real device (see docs/integrations/mqtt.md).
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from localhome.drivers.mqtt.rpc import ShellyRpcClient
from localhome.drivers.mqtt.shelly_light import ShellyLightNumberDriver, ShellyLightSwitchDriver
from tests.mqtt_fakes import FakeMqttConnection

BROKER = {"host": "unused-in-tests"}
DEVICE_ID = "shelly0110dimg3-testmac"


@pytest.fixture
def fake_connection(monkeypatch):
    conn = FakeMqttConnection()
    monkeypatch.setattr("localhome.drivers.mqtt.rpc.get_connection", lambda broker: conn)
    return conn


def _respond_once(fake_connection, reply_id: str, result: dict | None = None, error: dict | None = None):
    """Wait for the next request the driver publishes, then deliver a
    matching reply on the sender's own reply topic - mirrors how a real
    Shelly device echoes {"id", "src"} back to "<src>/rpc"."""

    def worker():
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if fake_connection.published:
                topic, payload = fake_connection.published[-1]
                request = json.loads(payload)
                reply = {"id": request["id"], "src": DEVICE_ID, "dst": request["src"]}
                if error is not None:
                    reply["error"] = error
                else:
                    reply["result"] = result or {}
                fake_connection.deliver(f"{request['src']}/rpc", json.dumps(reply))
                return
            time.sleep(0.005)

    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------ rpc ----

def test_rpc_client_returns_the_result_of_a_matching_reply(fake_connection):
    client = ShellyRpcClient(BROKER, DEVICE_ID, reply_id="localhome", timeout=1.0)
    _respond_once(fake_connection, "localhome", result={"output": True, "brightness": 51.0})

    result = client.call("Light.GetStatus", {"id": 0})

    assert result == {"output": True, "brightness": 51.0}
    assert len(fake_connection.published) == 1
    topic, payload = fake_connection.published[0]
    assert topic == f"{DEVICE_ID}/rpc"
    request = json.loads(payload)
    assert request["src"] == "localhome"
    assert request["method"] == "Light.GetStatus"
    assert request["params"] == {"id": 0}


def test_rpc_client_raises_on_an_error_reply(fake_connection):
    client = ShellyRpcClient(BROKER, DEVICE_ID, reply_id="localhome", timeout=1.0)
    _respond_once(fake_connection, "localhome", error={"code": 404, "message": "not found"})

    with pytest.raises(RuntimeError, match="RPC error"):
        client.call("Light.GetStatus", {"id": 0})


def test_rpc_client_raises_when_no_reply_arrives_in_time(fake_connection):
    client = ShellyRpcClient(BROKER, DEVICE_ID, reply_id="localhome", timeout=0.1)

    with pytest.raises(RuntimeError, match="no RPC reply"):
        client.call("Light.GetStatus", {"id": 0})


# ------------------------------------------------------------- switch ----

def test_switch_reads_output_field_as_is_on(fake_connection):
    driver = ShellyLightSwitchDriver(name="VMC Dimmer", broker=BROKER, device_id=DEVICE_ID)
    _respond_once(fake_connection, "localhome", result={"output": True, "brightness": 51.0})

    reading = driver.read()

    assert reading == {"ok": True, "name": "VMC Dimmer", "is_on": True}


def test_switch_turn_on_sends_a_light_set_rpc_call(fake_connection):
    driver = ShellyLightSwitchDriver(name="VMC Dimmer", broker=BROKER, device_id=DEVICE_ID)
    _respond_once(fake_connection, "localhome")

    driver.turn_on()

    assert len(fake_connection.published) == 1
    topic, payload = fake_connection.published[0]
    request = json.loads(payload)
    assert topic == f"{DEVICE_ID}/rpc"
    assert request["method"] == "Light.Set"
    assert request["params"] == {"id": 0, "on": True}


# ------------------------------------------------------------- number ----

def test_number_reads_brightness_as_value(fake_connection):
    driver = ShellyLightNumberDriver(name="VMC Dimmer Speed", broker=BROKER, device_id=DEVICE_ID)
    _respond_once(fake_connection, "localhome", result={"output": True, "brightness": 51.0})

    reading = driver.read()

    assert reading == {"ok": True, "name": "VMC Dimmer Speed", "value": 51.0}


def test_number_set_value_clamps_and_sends_a_light_set_rpc_call(fake_connection):
    driver = ShellyLightNumberDriver(
        name="VMC Dimmer Speed", broker=BROKER, device_id=DEVICE_ID, min_value=0, max_value=100,
    )
    _respond_once(fake_connection, "localhome")

    driver.set_value(150)

    assert len(fake_connection.published) == 1
    topic, payload = fake_connection.published[0]
    request = json.loads(payload)
    assert topic == f"{DEVICE_ID}/rpc"
    assert request["method"] == "Light.Set"
    assert request["params"] == {"id": 0, "brightness": 100.0}
