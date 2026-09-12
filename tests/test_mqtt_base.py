import json

from localhome.drivers.mqtt.base import command_payload


def test_raw_payload_when_no_field_given():
    assert command_payload("ON", None) == "ON"
    assert command_payload(42.5, None) == "42.5"


def test_json_payload_when_field_given():
    assert json.loads(command_payload("ON", "state")) == {"state": "ON"}
    assert json.loads(command_payload(42.5, "brightness")) == {"brightness": 42.5}
