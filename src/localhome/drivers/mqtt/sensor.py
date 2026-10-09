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
  availability_topic: optional - a separate topic that reports this
               device online/offline (most commonly its own MQTT Last
               Will and Testament), checked on every read() *in
               addition to* the per-topic staleness check above. Marks
               the reading unreachable immediately when the broker says
               the device disconnected, rather than waiting for
               stale_after_seconds to elapse - see
               drivers/mqtt/base.py's Availability and
               docs/integrations/mqtt.md.
  available_payload: the payload on availability_topic that means
               "online" (default "true", Shelly Gen2/Gen3's convention)
  valid_ranges: optional {reading_field_name: [min, max]} - a value outside
               it (or not a number) is left out of the reading and named
               in its "rejected" list: a sensor element failing can send
               -127 or 0 % as readily as a real value
  required_fields: optional - the reading isn't "ok" without a valid value
               for each of these (an H&T's temperature: the thermostat then
               treats the sensor as unreachable - its own safe path -
               instead of acting on nothing, or on nonsense)

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

import logging
import math
from typing import Any

from localhome.core.interfaces import PollingDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import Availability, MqttJsonState, require_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path

logger = logging.getLogger(__name__)


def _within(value: Any, limits: list | tuple) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and limits[0] <= value <= limits[1])


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
        availability_topic: str | None = None,
        available_payload: str = "true",
        valid_ranges: dict[str, list] | None = None,
        required_fields: list[str] | None = None,
    ):
        self.name = name
        self._ranges = dict(valid_ranges or {})
        self._required = list(required_fields or [])
        self._rejections_logged: set[str] = set()
        self.poll_interval_seconds = poll_interval_seconds
        self._fields = fields
        self.history_fields = tuple(history_fields) if history_fields else tuple(fields)
        topics = state_topic if isinstance(state_topic, list) else [state_topic]
        self._states = [
            MqttJsonState(broker, topic, stale_after_seconds=stale_after_seconds, cache_path=cache_path)
            for topic in topics
        ]
        # Set by the device manager: reads this device again at once - its
        # availability saying online or offline doesn't wait for the poll.
        self.wake_poller = None
        self._availability = Availability(broker, availability_topic, available_payload, cache_path=cache_path,
                                          on_change=lambda: self.wake_poller and self.wake_poller())

    def read(self) -> dict[str, Any]:
        # Each subscribed topic is tried independently rather than
        # requiring all of them to have a fresh payload - a device that
        # splits fields across topics (a Shelly H&T's temperature and
        # humidity, say) still gives a partial-but-useful reading if only
        # one topic has reported so far, instead of the whole sensor
        # going "unreachable" until every topic happens to line up.
        # A topic gone quiet while the others go on is dropped, not kept at
        # its last value: a Shelly H&T republishes every component at every
        # wake (seen on the production board, on batteries and on USB-C
        # alike), so a temperature that stops while the rest goes on is a
        # failing element - the thermostat must stop trusting it.
        reading: dict[str, Any] = {"ok": True, "name": self.name}
        errors = []
        rejected: list[str] = []
        for state in self._states:
            try:
                payload = require_payload(state)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            self._take(payload, reading, rejected, keep=False)
        # A definitive "the broker says this device disconnected" signal
        # overrides even a still-within-window regular reading - see
        # drivers/mqtt/base.py's Availability.
        offline_reason = self._availability.offline_reason()
        if offline_reason is not None:
            errors.append(offline_reason)
        if offline_reason is not None or len(errors) == len(self._states):
            # Every subscribed topic is stale/missing - a battery sensor
            # (e.g. a Shelly H&T) between wake cycles hits this often,
            # not just genuine offline hardware. Raising here (the old
            # behavior) would discard the device's last known reading
            # entirely, via SyncPoller's generic try/except turning any
            # exception into a bare {"ok": False, "error": ...} - the
            # dashboard would blank a value it still actually has.
            # get_payload() doesn't care about staleness, only
            # require_payload() does, so fall back to it for whatever
            # each topic last had, however old, and report the oldest
            # topic's age so the UI can say "last known: X, Yh ago"
            # instead of just an error. Still ok: False either way - a
            # control loop (thermostat/interlock) must not act on this
            # as if it were fresh; see core/poller.py's callers.
            ages = []
            for state in self._states:
                payload = state.get_payload()
                if payload is None:
                    continue
                self._take(payload, reading, rejected, keep=True)
                age = state.seconds_since_message()
                if age is not None:
                    ages.append(age)
            if not ages:
                # Never received anything on *any* topic - nothing to
                # fall back to, so this really is "unreachable" (a
                # fresh/never-wired device), not "stale". Raise as
                # before; SyncPoller turns this into the same bare
                # {"ok": False, "error": ...} it always has.
                raise RuntimeError("; ".join(errors))
            reading["ok"] = False
            reading["error"] = "; ".join(errors)
            reading["last_value_age_seconds"] = min(ages)
        if rejected:
            reading["rejected"] = rejected
        missing = [field for field in self._required if field not in reading]
        if missing and reading["ok"]:
            reading["ok"] = False
            reading["error"] = "; ".join(rejected) or f"no {', '.join(missing)} in its messages"
        return reading

    def _take(self, payload: Any, reading: dict, rejected: list[str], keep: bool) -> None:
        """This payload's fields into the reading - over what's there, or
        only where nothing is (`keep`) - each within its valid range."""
        for field, path in self._fields.items():
            value = extract_path(payload, path)
            if value is None or (keep and field in reading):
                continue
            limits = self._ranges.get(field)
            if limits is not None and not _within(value, limits):
                note = f"{field} {value!r} out of {limits[0]}..{limits[1]}"
                if note not in rejected:
                    rejected.append(note)
                if note not in self._rejections_logged:
                    self._rejections_logged.add(note)
                    logger.warning("%s: %s - left out of its reading", self.name, note)
                continue
            reading[field] = value

    @property
    def last_update_ts(self) -> float | None:
        timestamps = [s.get_received_at() for s in self._states]
        valid = [t for t in timestamps if t is not None]
        return max(valid) if valid else None


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
        availability_topic=options.get("availability_topic"),
        available_payload=options.get("available_payload", "true"),
        valid_ranges=options.get("valid_ranges"),
        required_fields=options.get("required_fields"),
    )


register_driver("climate", "mqtt_json")(_create)
register_driver("power_meter", "mqtt_json")(_create)
