"""Shared plumbing for every generic MQTT-JSON driver: subscribe to one
topic, cache the latest parsed JSON payload, extract fields from it on
demand. `sensor.py`/`switch.py`/`number.py`/`cover.py` each wrap this in
whatever interface (PollingDriver/SwitchDriver/NumberDriver/CoverDriver)
their `kind` needs.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from localhome.drivers.mqtt.client import get_connection
from localhome.drivers.mqtt.paths import extract_path
from localhome.drivers.mqtt.state_cache import load_cached, save_cached


class MqttJsonState:
    def __init__(
        self,
        broker: dict,
        state_topic: str,
        stale_after_seconds: float | None = None,
        cache_path: str | None = None,
        on_message=None,
    ):
        self._lock = threading.Lock()
        self._payload: Any = None  # a dict for JSON payloads, a str otherwise
        self._received_at: float | None = None
        # Opt-in, off by default: MQTT is push-based, not polled, so
        # there's no way to tell "device is offline" from "device just
        # hasn't changed state" by silence alone unless you know it's
        # *supposed* to publish periodically (see require_payload() below
        # and docs/integrations/mqtt.md#detecting-an-offline-device). A
        # switch that only publishes on state change can legitimately go
        # quiet for hours without being broken - defaulting this off
        # avoids flagging that as "unreachable".
        self.stale_after_seconds = stale_after_seconds
        self._broker = broker
        self._topic = state_topic
        self._cache_path = cache_path
        # Optional: called with (previous payload, its received_at, payload,
        # received_at) on every message from the device - not the cached
        # one - for a driver that wants to see each change, not just the
        # latest at its next reading. The cached payload is the first
        # "previous".
        self._on_message_hook = on_message
        if cache_path is not None:
            cached = load_cached(cache_path, broker, state_topic)
            if cached is not None:
                self._payload, self._received_at = cached
        self._connection = get_connection(broker)
        self._connection.subscribe(state_topic, self._on_message)

    def _on_message(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            # Not every device sends JSON - Shelly Gen1 relays just
            # publish "on"/"off" as the raw payload, for example. Keep it
            # as a plain string; extract_path(data, "") returns it as-is.
            data = payload.decode(errors="replace")
        received_at = time.time()
        with self._lock:
            previous, previous_at = self._payload, self._received_at
            self._payload = data
            self._received_at = received_at
        if self._cache_path is not None:
            save_cached(self._cache_path, self._broker, self._topic, data, received_at)
        if self._on_message_hook is not None:
            self._on_message_hook(previous, previous_at, data, received_at)

    def get_payload(self) -> Any:
        with self._lock:
            return self._payload

    def get_received_at(self) -> float | None:
        with self._lock:
            return self._received_at

    def seconds_since_message(self) -> float | None:
        with self._lock:
            received_at = self._received_at
        return None if received_at is None else time.time() - received_at

    def extract(self, path: str) -> Any:
        payload = self.get_payload()
        return None if payload is None else extract_path(payload, path)

    def publish(self, topic: str, payload: str) -> None:
        self._connection.publish(topic, payload)


class Availability:
    """Optional, opt-in per-device online/offline signal from a separate
    MQTT topic - most commonly a device's own Last Will and Testament
    (LWT), which the *broker* republishes the instant that device's MQTT
    connection actually drops (`<id>/online` on Shelly Gen2+, `true`/
    `false`, retained). That's a faster, more precise "is this device
    really there" signal than stale_after_seconds's "hasn't published a
    regular reading in a while" inference, which can't tell a genuinely
    dead device apart from one that's alive but has nothing new to say
    yet (a switch that only publishes on state change, say) unless you
    already know its expected reporting cadence. Costs nothing when not
    configured (availability_topic left unset - see mqtt_json's
    `_create()` factories) - offline_reason() always returns None then,
    so every existing config.yaml keeps behaving exactly as before.

    Deliberately its own tiny class rather than another constructor
    argument on MqttJsonState itself: an availability topic isn't a
    *reading* to extract fields from, it's a gate other state's
    readings get checked against - see sensor.py/switch.py/number.py's
    read()/async_read() for how each folds offline_reason() in.
    """

    def __init__(self, broker: dict, topic: str | None, available_payload: str = "true", cache_path: str | None = None,
                 on_change=None):
        # `on_change()`: called when it says something new - online or
        # offline - so its device's reading follows at once, not at the
        # next poll (core/manager.py wires it to the device's poller).
        self._on_change = on_change
        self._state = MqttJsonState(broker, topic, cache_path=cache_path,
                                    on_message=self._said if on_change else None) if topic else None
        self._available_payload = available_payload

    def _said(self, previous, previous_at, payload, received_at) -> None:
        if payload != previous:
            self._on_change()

    def offline_reason(self) -> str | None:
        """None if there's no availability topic configured, or nothing
        has ever arrived on it yet (silently deferring to the ordinary
        stale_after_seconds check instead of guessing), or its last
        message says the device is online. Otherwise, the reason to
        treat this device as offline *right now*, regardless of how
        fresh its other topics still look."""
        if self._state is None:
            return None
        payload = self._state.get_payload()
        if payload is None:
            return None
        if str(payload).strip().lower() == self._available_payload.strip().lower():
            return None
        return f"marked offline on its availability topic (last payload: {payload!r})"

    def received_at(self) -> float | None:
        """When its availability topic last said anything - a device saying
        "online" again is a new time here. None without a topic."""
        return None if self._state is None else self._state.get_received_at()


def require_payload(state: MqttJsonState) -> Any:
    payload = state.get_payload()
    if payload is None:
        raise RuntimeError("no MQTT message received yet")
    age = state.seconds_since_message()
    if state.stale_after_seconds is not None and age is not None and age > state.stale_after_seconds:
        raise RuntimeError(
            f"no MQTT message in {age:.0f}s (stale_after_seconds={state.stale_after_seconds:.0f}) - "
            "device is probably offline"
        )
    return payload


def command_payload(value: Any, payload_field: str | None) -> str:
    """Most Tasmota/Shelly-style setups take the raw value as the MQTT
    payload; Zigbee2MQTT-style setups expect a small JSON object instead
    (`{"state": "ON"}`) - `command_payload_field` in config.yaml picks
    which one."""
    if payload_field:
        return json.dumps({payload_field: value})
    return str(value)
