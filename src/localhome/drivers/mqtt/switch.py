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
  command_on / command_off: what gets sent to command_topic, when it
               isn't payload_on/off - a Shelly Gen2/Gen3 relay reports
               `"output": true` but takes "on"/"off" on its command topic
  hold_seconds / rest_state: a Shelly Gen2/Gen3 relay only. Out of its
               rest state ("off" by default) the relay is commanded
               "on,<hold_seconds>" - on for that long, then back to rest
               by itself - and the command is renewed while LocalHome
               runs, every hold_seconds/3. If LocalHome stops, the relay
               goes back to rest on its own; while it runs, it never
               drops (measured on a Plus 1: each renewal restarts the
               timer, the output stays on - docs/shelly-gen3.md).
  command_payload_field: if set, commands are sent as JSON
               `{command_payload_field: payload_on|payload_off}` (the
               Zigbee2MQTT convention, e.g. field "state") instead of the
               raw value as the payload (Tasmota's convention)
  availability_topic / available_payload: optional immediate offline
               detection via the device's own MQTT Last Will and
               Testament, instead of waiting for stale_after_seconds to
               elapse (which a switch that only publishes on state
               change may never even have set) - see
               drivers/mqtt/base.py's Availability.

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

import logging
import time
from typing import Any

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import Availability, MqttJsonState, command_payload, require_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path

logger = logging.getLogger(__name__)

# Renewals per hold: a 15-minute hold is renewed every 5 minutes - two
# renewals in a row can be lost to the Wi-Fi without the relay noticing,
# and a home router isn't kept busy for years by a relay that's just on.
RENEWALS_PER_HOLD = 3


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
        command_on: Any = None,
        command_off: Any = None,
        hold_seconds: float | None = None,
        rest_state: str = "off",
        poll_interval_seconds: float = 20.0,
        stale_after_seconds: float | None = None,
        cache_path: str | None = None,
        availability_topic: str | None = None,
        available_payload: str = "true",
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
        self._command_on = payload_on if command_on is None else command_on
        self._command_off = payload_off if command_off is None else command_off
        self._hold_seconds = int(hold_seconds) if hold_seconds else None
        self._rest_on = rest_state == "on"
        self._renewed_at: float | None = None
        self._renew_failed = False
        self._state = MqttJsonState(broker, state_topic, stale_after_seconds=stale_after_seconds, cache_path=cache_path)
        self._availability = Availability(broker, availability_topic, available_payload, cache_path=cache_path)

    def read(self) -> dict[str, Any]:
        # A definitive "the broker says this device disconnected" signal
        # (see drivers/mqtt/base.py's Availability) is checked *before*
        # require_payload() - a relay that only publishes on state
        # change can otherwise sit "on" for days with no
        # stale_after_seconds set at all, so availability may be the
        # only way this ever notices it went offline.
        offline_reason = self._availability.offline_reason()
        if offline_reason is not None:
            raise RuntimeError(offline_reason)
        payload = require_payload(self._state)
        raw_state = extract_path(payload, self._state_field)
        reading: dict[str, Any] = {"ok": True, "name": self.name, "is_on": raw_state == self._payload_on}
        for field, path in self._fields.items():
            value = extract_path(payload, path)
            if value is not None:
                reading[field] = value
        # Out of rest, whoever put it there: LocalHome holds it while it
        # runs - after a restart too, before the relay's timer runs out.
        if self._hold_seconds and reading["is_on"] != self._rest_on:
            self._renew(reading["is_on"])
        return reading

    def _renew(self, on: bool) -> None:
        now = time.monotonic()
        if self._renewed_at is not None and now - self._renewed_at < self._hold_seconds / RENEWALS_PER_HOLD:
            return
        try:
            self._send(self._held_command(on))
            self._renewed_at = now
            self._renew_failed = False
        except Exception as exc:
            # The relay keeps its state until its timer runs out: say it
            # once, try again at the next reading.
            if not self._renew_failed:
                logger.warning("%s: couldn't renew its hold - %s", self.name, exc)
            self._renew_failed = True

    def _held_command(self, on: bool) -> Any:
        command = self._command_on if on else self._command_off
        if self._hold_seconds and on != self._rest_on:
            return f"{command},{self._hold_seconds}"
        return command

    @property
    def last_update_ts(self) -> float | None:
        # Read by core/poller.py's SyncPoller.poll_once() instead of
        # stamping "now" on every successful poll (the default when a
        # driver doesn't define this) - MQTT is push-based, so re-polling
        # the *cache* every poll_interval_seconds otherwise makes an
        # untouched relay's reading look freshly-updated every cycle even
        # when the device itself hasn't said anything new in hours,
        # which is exactly backwards for a "how stale is this really"
        # freshness display.
        return self._state.get_received_at()

    def turn_on(self) -> None:
        self._send(self._held_command(True))
        self._renewed_at = time.monotonic() if self._hold_seconds and not self._rest_on else None

    def turn_off(self) -> None:
        self._send(self._held_command(False))
        self._renewed_at = time.monotonic() if self._hold_seconds and self._rest_on else None

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
        command_on=options.get("command_on"),
        command_off=options.get("command_off"),
        hold_seconds=options.get("hold_seconds"),
        rest_state=options.get("rest_state", "off"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
        stale_after_seconds=options.get("stale_after_seconds"),
        cache_path=state_cache_path(options),
        availability_topic=options.get("availability_topic"),
        available_payload=options.get("available_payload", "true"),
    )


register_driver("switch", "mqtt_json")(_create)
