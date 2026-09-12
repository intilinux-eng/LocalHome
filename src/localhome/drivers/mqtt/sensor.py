"""Generic read-only MQTT sensor: subscribes to one JSON topic and maps
whatever fields you point it at into a reading, so any device that
publishes JSON telemetry over MQTT - Shelly, Tasmota, Zigbee2MQTT,
ESPHome, a few lines of a microcontroller sketch - works without a
brand-specific driver. See docs/integrations/mqtt.md for real examples.

Registered under both `climate` and `power_meter`: the logic is
identical, `kind` in config.yaml is what decides which dashboard card it
gets and which of the two the `energy:` alert can target.

config.yaml options:
  broker_file (or inline broker: {...}): connection details
  state_topic: the MQTT topic to subscribe to
  fields: {reading_field_name: "dotted.path.in.the.json.payload"}
  history_fields: optional subset of `fields` worth recording (defaults
                   to all of them)

Example - a Tasmota plug's periodic energy telemetry:
  kind: power_meter
  type: mqtt_json
  name: "Workshop Plug"
  broker_file: "mqtt_broker.json"
  state_topic: "tele/tasmota_workshop/SENSOR"
  fields:
    power_w: "ENERGY.Power"
    voltage_v: "ENERGY.Voltage"
    current_a: "ENERGY.Current"
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import MqttJsonState, require_payload
from localhome.drivers.mqtt.client import load_broker_config
from localhome.drivers.mqtt.paths import extract_path


class MqttSensorDriver(PollingDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        state_topic: str,
        fields: dict[str, str],
        history_fields: tuple[str, ...] | None = None,
        poll_interval_seconds: float = 20.0,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self._fields = fields
        self.history_fields = tuple(history_fields) if history_fields else tuple(fields)
        self._state = MqttJsonState(broker, state_topic)

    def read(self) -> dict[str, Any]:
        payload = require_payload(self._state)
        reading: dict[str, Any] = {"ok": True, "name": self.name}
        for field, path in self._fields.items():
            value = extract_path(payload, path)
            if value is not None:
                reading[field] = value
        return reading


def _create(options: dict) -> MqttSensorDriver:
    return MqttSensorDriver(
        name=options["name"],
        broker=load_broker_config(options),
        state_topic=options["state_topic"],
        fields=options["fields"],
        history_fields=options.get("history_fields"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
    )


register_driver("climate", "mqtt_json")(_create)
register_driver("power_meter", "mqtt_json")(_create)
