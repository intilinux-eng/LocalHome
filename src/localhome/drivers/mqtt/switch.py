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
  status_command: what makes the device report its state on the status
               topic, sent to command_topic ("status_update" on a Shelly
               Gen2/Gen3): at start - the last state kept on disk is from
               before - and when it's back online, before anything is
               renewed from what it said then.
  event_log:   a held relay back at rest by its own timer - nobody
               renewed it - is written here ("safety_timer <name>", at the
               time it happened), and its reading carries that time
               (safety_rest_at) - see services/system_status.py.
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
import threading
import time
from typing import Any

from localhome.core.interfaces import SwitchDriver
from localhome.core.registry import register_driver
from localhome.drivers.mqtt.base import Availability, MqttJsonState, command_payload, require_payload
from localhome.drivers.mqtt.client import load_broker_config, state_cache_path
from localhome.drivers.mqtt.paths import extract_path
from localhome.util.eventlog import append_event, event_time, read_events

logger = logging.getLogger(__name__)

# Renewals per hold: a 15-minute hold is renewed every 5 minutes - two
# renewals in a row can be lost to the Wi-Fi without the relay noticing,
# and a home router isn't kept busy for years by a relay that's just on.
RENEWALS_PER_HOLD = 3
# Right after a command the device is re-read before it has said anything
# (a relay answers within a moment): for this long, unless it has spoken
# since, a reading says what was commanded rather than the state before.
# Past it, whatever the device last said - it may not have heard.
CONFIRM_SECONDS = 15
# Asking the device for its state (status_command): a Plus 1 answers in
# 10 ms, so a reading waits for it this long at most. Unanswered - not
# connected yet, say, right after the board itself has started - it's
# asked again 1, 2, 4 minutes later, then every 5: never more, and at
# once when it says it's online again.
ANSWER_WAIT_SECONDS = 2.0
ASK_AGAIN_SECONDS = 60
ASK_AGAIN_MAX_SECONDS = 300
SAFETY_TIMER_EVENT = "safety_timer"


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
        status_command: str | None = None,
        event_log: str | None = None,
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
        # What LocalHome last told it - None until it says anything. Only
        # that is renewed: right after "off" a reading can still say on
        # (the relay hasn't confirmed yet), and renewing that would turn
        # it straight back on.
        self._commanded: bool | None = None
        self._commanded_at: float | None = None   # wall clock, as the MQTT receive time
        self._status_command = status_command
        # Set by any message from the device since this started, or since
        # it was last seen offline: until then, what's known of it may be
        # from before (see _current()).
        self._heard = threading.Event()
        self._new_message = threading.Event()
        self._asks = 0                                 # unanswered, in a row
        self._asked_at: float | None = None            # monotonic
        self._online_asked_at: float | None = None     # its availability message, when last asked
        # When its own timer last put it back at rest - kept in event_log,
        # so a restart of LocalHome doesn't forget it.
        self._event_log = event_log
        self._safety_rest_at: float | None = self._last_safety_rest()
        self._state = MqttJsonState(broker, state_topic, stale_after_seconds=stale_after_seconds, cache_path=cache_path,
                                    on_message=self._on_status)
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
            self._heard.clear()        # back online, it's asked first
            raise RuntimeError(offline_reason)
        if self._status_command and not self._current():
            self._ask()
        payload = require_payload(self._state)
        raw_state = extract_path(payload, self._state_field)
        reading: dict[str, Any] = {"ok": True, "name": self.name, "is_on": raw_state == self._payload_on}
        for field, path in self._fields.items():
            value = extract_path(payload, path)
            if value is not None:
                reading[field] = value
        if self._safety_rest_at is not None:
            reading["safety_rest_at"] = self._safety_rest_at
        if (self._commanded_at is not None and (self._state.get_received_at() or 0) <= self._commanded_at
                and time.time() - self._commanded_at < CONFIRM_SECONDS):
            reading["is_on"] = self._commanded
        # Out of rest: LocalHome holds it while it runs - what it last
        # commanded, or, after a restart of LocalHome (nothing commanded
        # yet), whatever it finds, before the relay's timer runs out.
        if self._hold_seconds and reading["is_on"] != self._rest_on and self._commanded in (None, reading["is_on"]):
            self._renew(reading["is_on"])
        return reading

    def _current(self) -> bool:
        """Whether what's known of the device is what it is now: it has
        spoken since LocalHome started (or since it was back online) - and,
        held out of rest, within its hold: silent for longer, its timer has
        run out by now, or someone else is holding it."""
        if not self._heard.is_set():
            return False
        if not self._hold_seconds:
            return True
        payload, received_at = self._state.get_payload(), self._state.get_received_at()
        if payload is None or received_at is None or self._is_on(payload) == self._rest_on:
            return True
        return time.time() - received_at <= self._hold_seconds

    def _ask(self) -> None:
        """Ask the device for its state and wait a moment for the answer -
        not more often than ASK_AGAIN_SECONDS, doubling while unanswered,
        unless it has just said it's online. Its answer goes through
        _on_status() like any message."""
        now = time.monotonic()
        online_at = self._availability.received_at()
        if self._asks and online_at == self._online_asked_at:
            wait = min(ASK_AGAIN_SECONDS * 2 ** (self._asks - 1), ASK_AGAIN_MAX_SECONDS)
            if now - self._asked_at < wait:
                return
        self._asked_at, self._online_asked_at = now, online_at
        self._asks += 1
        self._new_message.clear()
        try:
            self._send(self._status_command)
        except Exception as exc:
            # No broker yet: asked again later, said once.
            if self._asks == 1:
                logger.info("%s: couldn't ask for its state yet - %s", self.name, exc)
            return
        self._new_message.wait(ANSWER_WAIT_SECONDS)

    def _is_on(self, payload: Any) -> bool:
        return extract_path(payload, self._state_field) == self._payload_on

    def _at_rest_by_timer(self, payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("source") == "timer" and self._is_on(payload) == self._rest_on

    def _on_status(self, previous: Any, previous_at: float | None, payload: Any, received_at: float) -> None:
        """Every message from the device, as it comes (the MQTT thread).
        A held relay that went from anything else to "at rest, by its
        timer" ("source": "timer" on a Shelly) wasn't renewed for its whole
        hold - LocalHome stopped, or couldn't reach it. Noted at the time
        it happened: its last hold heard, plus that hold's length - by
        LocalHome's own clock, not the device's - or now, if that isn't
        known."""
        self._asks = 0
        self._heard.set()
        self._new_message.set()
        if not self._hold_seconds or previous is None:
            return            # nothing to compare with - when it happened is unknown
        if not self._at_rest_by_timer(payload) or self._at_rest_by_timer(previous):
            return            # not so, or already so - asked again after a restart
        at = received_at
        duration = previous.get("timer_duration") if isinstance(previous, dict) else None
        if (previous_at is not None and self._is_on(previous) != self._rest_on
                and isinstance(duration, (int, float)) and not isinstance(duration, bool)):
            at = min(received_at, previous_at + duration)
        self._note_safety_rest(at)

    def _note_safety_rest(self, at: float) -> None:
        if self._safety_rest_at is not None and abs(at - self._safety_rest_at) < 60:
            return
        self._safety_rest_at = at
        logger.warning("%s: back at rest by its own safety timer at %s - its hold wasn't renewed",
                       self.name, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)))
        if self._event_log:
            try:
                append_event(self._event_log, SAFETY_TIMER_EVENT, self.name, at=at)
            except OSError as exc:
                logger.warning("%s: couldn't write it to %s - %s", self.name, self._event_log, exc)

    def _last_safety_rest(self) -> float | None:
        if not self._event_log:
            return None
        times = [event_time(e) for e in read_events(self._event_log, "")
                 if e["code"] == SAFETY_TIMER_EVENT and e["values"] == self.name]
        times = [t for t in times if t is not None]
        return max(times) if times else None

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

    def _ask_first(self) -> None:
        """A command before the device has been heard since LocalHome
        started (or came back online) - the thermostat sends one blind
        while a valve has no reading yet: ask first, in the same breath.
        The device takes them in order, so its answer says what it was
        before the command changes it - a safety timer that acted
        included, which the command would otherwise wipe out."""
        if self._status_command and not self._heard.is_set():
            try:
                self._send(self._status_command)
            except Exception:
                pass                 # the command itself will say why

    def turn_on(self) -> None:
        self._ask_first()
        self._send(self._held_command(True))
        self._commanded, self._commanded_at = True, time.time()
        self._renewed_at = time.monotonic() if self._hold_seconds and not self._rest_on else None

    def turn_off(self) -> None:
        self._ask_first()
        self._send(self._held_command(False))
        self._commanded, self._commanded_at = False, time.time()
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
        status_command=options.get("status_command"),
        event_log=options.get("event_log"),
        poll_interval_seconds=options.get("poll_interval_seconds", 20.0),
        stale_after_seconds=options.get("stale_after_seconds"),
        cache_path=state_cache_path(options),
        availability_topic=options.get("availability_topic"),
        available_payload=options.get("available_payload", "true"),
    )


register_driver("switch", "mqtt_json")(_create)
