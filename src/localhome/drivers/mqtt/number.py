"""Generic MQTT-controlled analog value: a dimmer, a fan speed, a 0-10V
output driven from an MQTT-bridged relay/controller (e.g. a Shelly
0-10V/PWM dimmer add-on) - anything set to a number in a range rather
than toggled or driven open/closed. See core/interfaces.py's
`NumberDriver` and docs/integrations/mqtt.md.

config.yaml options (in addition to sensor.py's broker_file):
  command_topic: where the target value is published
  value_field: dotted path to the current value in the JSON state
               payload (default "value")
  min_value / max_value / unit: range shown on the dashboard slider and
               clamped against before sending a command (defaults 0-100, "%")
  command_payload_field: if set, commands are sent as JSON
               `{command_payload_field: value}` instead of the raw
               number as the payload

Example - a hypothetical Shelly 0-10V dimmer bridged over MQTT:
  kind: number
  type: mqtt_json
  name: "VMC Dimmer"
  broker_file: "mqtt_broker.json"
  state_topic: "shellies/vmc-dimmer/status"
  command_topic: "shellies/vmc-dimmer/set"
  value_field: "brightness"
  command_payload_field: "brightness"
  min_value: 0
  max_value: 100
  unit: "%"
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import NumberDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import MqttJsonState, command_payload, require_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path


class MqttNumberDriver(NumberDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        state_topic: str,
        command_topic: str,
        value_field: str = "value",
        min_value: float = 0.0,
        max_value: float = 100.0,
        unit: str = "%",
        command_payload_field: str | None = None,
        poll_interval_seconds: float = 20.0,
        stale_after_seconds: float | None = None,
        cache_path: str | None = None,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self.min_value = min_value
        self.max_value = max_value
        self.unit = unit
        self.history_fields = ("value",)
        self._value_field = value_field
        self._command_topic = command_topic
        self._command_payload_field = command_payload_field
        self._state = MqttJsonState(broker, state_topic, stale_after_seconds=stale_after_seconds, cache_path=cache_path)

    def read(self) -> dict[str, Any]:
        payload = require_payload(self._state)
        value = extract_path(payload, self._value_field)
        if value is None:
            raise RuntimeError(f"field '{self._value_field}' not present in the last MQTT message")
        return {"ok": True, "name": self.name, "value": float(value)}

    def set_value(self, value: float) -> None:
        # max()/min() preserve whichever operand's type "won" the
        # comparison, so clamping against int min/max_value (e.g. from
        # `min_value: 0` in YAML) could silently publish an int instead
        # of a float - wrap the result to keep the payload consistently
        # numeric-as-float regardless of how the range was configured.
        clamped = float(max(self.min_value, min(self.max_value, float(value))))
        self._state.publish(self._command_topic, command_payload(clamped, self._command_payload_field))


def _create(options: dict) -> MqttNumberDriver:
    return MqttNumberDriver(
        name=options["name"],
        broker=load_broker_config(options),
        state_topic=options["state_topic"],
        command_topic=options["command_topic"],
        value_field=options.get("value_field", "value"),
        min_value=options.get("min_value", 0.0),
        max_value=options.get("max_value", 100.0),
        unit=options.get("unit", "%"),
        command_payload_field=options.get("command_payload_field"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
        stale_after_seconds=options.get("stale_after_seconds"),
        cache_path=state_cache_path(options),
    )


register_driver("number", "mqtt_json")(_create)
