"""Generic MQTT cover: open/close/stop over MQTT, for e.g. a Shelly 2PM
Gen2/Gen3 roller-shutter mode bridged over MQTT instead of its native
RPC API. Position is still estimated by services/position_control.py
exactly like the Tuya covers - this driver only reports the raw
open/close/stop motor state, same as them.

config.yaml options:
  broker_file (or inline broker: {...}): connection details
  state_topic / command_topic
  state_field: dotted path to the motor state in the JSON state payload
               (default "state")
  payload_open / payload_close / payload_stop: values for both reading
               state_field and what gets published to command_topic
  command_payload_field: if set, commands are sent as JSON
               `{command_payload_field: value}` instead of the raw value
  id: a stable identifier for position tracking (defaults to `name`)
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import CoverDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import MqttJsonState, command_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path


class MqttCoverDriver(CoverDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        state_topic: str,
        command_topic: str,
        cover_id: str | None = None,
        state_field: str = "state",
        payload_open: str = "open",
        payload_close: str = "close",
        payload_stop: str = "stop",
        command_payload_field: str | None = None,
        cache_path: str | None = None,
    ):
        self.id = cover_id or name
        self.name = name
        self._command_topic = command_topic
        self._state_field = state_field
        self._payloads = {"open": payload_open, "close": payload_close, "stop": payload_stop}
        self._command_payload_field = command_payload_field
        self._state = MqttJsonState(broker, state_topic, cache_path=cache_path)

    def status(self) -> dict[str, Any]:
        payload = self._state.get_payload()
        if payload is None:
            return {"state": "unknown"}
        raw_state = extract_path(payload, self._state_field)
        state = next((k for k, v in self._payloads.items() if v == raw_state), "unknown")
        return {"state": state, "raw": payload}

    def send(self, action: str) -> None:
        value = self._payloads[action]
        self._state.publish(self._command_topic, command_payload(value, self._command_payload_field))


def _create(options: dict) -> MqttCoverDriver:
    return MqttCoverDriver(
        name=options["name"],
        broker=load_broker_config(options),
        state_topic=options["state_topic"],
        command_topic=options["command_topic"],
        cover_id=options.get("id"),
        state_field=options.get("state_field", "state"),
        payload_open=options.get("payload_open", "open"),
        payload_close=options.get("payload_close", "close"),
        payload_stop=options.get("payload_stop", "stop"),
        command_payload_field=options.get("command_payload_field"),
        cache_path=state_cache_path(options),
    )


register_driver("cover", "mqtt_json")(_create)
