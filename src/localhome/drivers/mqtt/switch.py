"""Generic MQTT on/off switch, same JSON-mapping approach as sensor.py
plus a command topic. Covers Shelly/Tasmota/Zigbee2MQTT-style smartplugs
without a brand-specific driver - see docs/integrations/mqtt.md.

config.yaml options (in addition to sensor.py's broker_file/fields):
  command_topic: where on/off commands are published
  state_field: dotted path to the on/off state in the JSON payload
               (default "state")
  payload_on / payload_off: the values that mean on/off, both for reading
               state_field and for what gets sent to command_topic
               (default "ON"/"OFF" - Tasmota/Zigbee2MQTT's convention)
  command_payload_field: if set, commands are sent as JSON
               `{command_payload_field: payload_on|payload_off}` (the
               Zigbee2MQTT convention, e.g. field "state") instead of the
               raw value as the payload (Tasmota's convention)

Example - a Zigbee2MQTT smartplug:
  kind: switch
  type: mqtt_json
  name: "Living Room Lamp"
  broker_file: "mqtt_broker.json"
  state_topic: "zigbee2mqtt/Living Room Lamp"
  command_topic: "zigbee2mqtt/Living Room Lamp/set"
  command_payload_field: "state"
  fields:
    power_w: "power"
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import MqttJsonState, command_payload, require_payload
from localhome.drivers.mqtt.client import load_broker_config
from localhome.drivers.mqtt.paths import extract_path


class MqttSwitchDriver(SwitchDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        state_topic: str,
        command_topic: str,
        fields: dict[str, str] | None = None,
        state_field: str = "state",
        payload_on: str = "ON",
        payload_off: str = "OFF",
        command_payload_field: str | None = None,
        poll_interval_seconds: float = 20.0,
        stale_after_seconds: float | None = None,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self._fields = fields or {}
        self.history_fields = tuple(self._fields)
        self._command_topic = command_topic
        self._state_field = state_field
        self._payload_on = payload_on
        self._payload_off = payload_off
        self._command_payload_field = command_payload_field
        self._state = MqttJsonState(broker, state_topic, stale_after_seconds=stale_after_seconds)

    def read(self) -> dict[str, Any]:
        payload = require_payload(self._state)
        raw_state = extract_path(payload, self._state_field)
        reading: dict[str, Any] = {"ok": True, "name": self.name, "is_on": raw_state == self._payload_on}
        for field, path in self._fields.items():
            value = extract_path(payload, path)
            if value is not None:
                reading[field] = value
        return reading

    def turn_on(self) -> None:
        self._send(self._payload_on)

    def turn_off(self) -> None:
        self._send(self._payload_off)

    def _send(self, value: str) -> None:
        self._state.publish(self._command_topic, command_payload(value, self._command_payload_field))


def _create(options: dict) -> MqttSwitchDriver:
    return MqttSwitchDriver(
        name=options["name"],
        broker=load_broker_config(options),
        state_topic=options["state_topic"],
        command_topic=options["command_topic"],
        fields=options.get("fields"),
        state_field=options.get("state_field", "state"),
        payload_on=options.get("payload_on", "ON"),
        payload_off=options.get("payload_off", "OFF"),
        command_payload_field=options.get("command_payload_field"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
        stale_after_seconds=options.get("stale_after_seconds"),
    )


register_driver("switch", "mqtt_json")(_create)
