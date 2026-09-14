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


class MqttJsonState:
    def __init__(self, broker: dict, state_topic: str, stale_after_seconds: float | None = None):
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
        with self._lock:
            self._payload = data
            self._received_at = time.time()

    def get_payload(self) -> Any:
        with self._lock:
            return self._payload

    def seconds_since_message(self) -> float | None:
        with self._lock:
            received_at = self._received_at
        return None if received_at is None else time.time() - received_at

    def extract(self, path: str) -> Any:
        payload = self.get_payload()
        return None if payload is None else extract_path(payload, path)

    def publish(self, topic: str, payload: str) -> None:
        self._connection.publish(topic, payload)


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
