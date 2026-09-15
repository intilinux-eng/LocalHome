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
  state_topic: the MQTT topic to subscribe to - or a *list* of topics,
               for a device that splits one reading's fields across
               several topics (e.g. a Shelly H&T publishing temperature
               and humidity separately); each topic keeps its own cached
               payload, so one going stale/missing doesn't blank out a
               field that came from a different topic (see
               MqttSensorDriver.read() below)
  fields: {reading_field_name: "dotted.path.in.the.json.payload"} -
          looked up against whichever subscribed topic's payload has it
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

Example - a Shelly H&T publishing temperature, humidity and battery on
separate per-component topics (confirmed with mosquitto_sub -t '<id>/#'
-v - see docs/integrations/mqtt.md); battery_pct reads the standard
Shelly Gen2/Gen3 DevicePower component shape ({"battery": {"percent": N}}):
  kind: climate
  type: mqtt_json
  name: "Bathroom Sensor"
  broker_file: "mqtt_broker.json"
  state_topic:
    - "shellyht-XXXX/status/temperature:0"
    - "shellyht-XXXX/status/humidity:0"
    - "shellyht-XXXX/status/devicepower:0"
  fields:
    temperature_c: "tC"
    humidity_pct: "rh"
    battery_pct: "battery.percent"
"""
from __future__ import annotations

from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import MqttJsonState, require_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path


class MqttSensorDriver(PollingDriver):
    def __init__(
        self,
        name: str,
        broker: dict,
        state_topic: str | list[str],
        fields: dict[str, str],
        history_fields: tuple[str, ...] | None = None,
        poll_interval_seconds: float = 20.0,
        stale_after_seconds: float | None = None,
        cache_path: str | None = None,
    ):
        self.name = name
        self.poll_interval_seconds = poll_interval_seconds
        self._fields = fields
        self.history_fields = tuple(history_fields) if history_fields else tuple(fields)
        topics = state_topic if isinstance(state_topic, list) else [state_topic]
        self._states = [
            MqttJsonState(broker, topic, stale_after_seconds=stale_after_seconds, cache_path=cache_path)
            for topic in topics
        ]

    def read(self) -> dict[str, Any]:
        # Each subscribed topic is tried independently rather than
        # requiring all of them to have a fresh payload - a device that
        # splits fields across topics (a Shelly H&T's temperature and
        # humidity, say) still gives a partial-but-useful reading if only
        # one topic has reported so far, instead of the whole sensor
        # going "unreachable" until every topic happens to line up.
        reading: dict[str, Any] = {"ok": True, "name": self.name}
        errors = []
        for state in self._states:
            try:
                payload = require_payload(state)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            for field, path in self._fields.items():
                value = extract_path(payload, path)
                if value is not None:
                    reading[field] = value
        if len(errors) == len(self._states):
            raise RuntimeError("; ".join(errors))
        return reading


def _create(options: dict) -> MqttSensorDriver:
    return MqttSensorDriver(
        name=options["name"],
        broker=load_broker_config(options),
        state_topic=options["state_topic"],
        fields=options["fields"],
        history_fields=options.get("history_fields"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
        stale_after_seconds=options.get("stale_after_seconds"),
        cache_path=state_cache_path(options),
    )


register_driver("climate", "mqtt_json")(_create)
register_driver("power_meter", "mqtt_json")(_create)
