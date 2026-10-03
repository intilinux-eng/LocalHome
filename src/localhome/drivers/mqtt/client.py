"""Shared MQTT connection pool.

Several MQTT-based drivers (a Shelly plug, a Zigbee2MQTT sensor, a
Tasmota relay...) commonly talk to the *same* broker. Rather than one
TCP/MQTT session per driver, `get_connection(broker)` hands back the same
`MqttConnection` for an identical (host, port, username), so N devices on
one broker share one real connection; each driver just calls
`.subscribe()`/`.publish()` on it.

Connects with `connect_async` + `loop_start()`, so construction never
blocks or raises even if the broker is briefly unreachable - paho's
built-in reconnect logic keeps retrying in the background, matching the
rest of this project's "one broken integration shouldn't block startup"
stance (see core/manager.py).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import paho.mqtt.client as mqtt

from localhome.util.jsonfile import load_json

DEFAULT_PORT = 1883

_pool: dict[tuple, "MqttConnection"] = {}
_pool_lock = threading.Lock()


class MqttConnection:
    def __init__(self, host: str, port: int, username: str | None, password: str | None, client_id: str | None = None):
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[str, bytes], None]]] = {}

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id or "")
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        # Since when this connection has been down (monotonic), None while
        # it's up. Starts "down": nothing is connected until the broker
        # has answered once. What the LED matrix's "MQ" reads - the client
        # already knows, so nothing needs to probe the broker for it.
        self.down_since: float | None = time.monotonic()
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if not getattr(reason_code, "is_failure", False):
            self.down_since = None
        with self._lock:
            topics = list(self._subscribers)
        for topic in topics:
            client.subscribe(topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if self.down_since is None:
            self.down_since = time.monotonic()

    def _on_message(self, client, userdata, message) -> None:
        with self._lock:
            callbacks = list(self._subscribers.get(message.topic, []))
        for callback in callbacks:
            try:
                callback(message.topic, message.payload)
            except Exception:
                pass  # a broken subscriber shouldn't take down the shared connection

    def subscribe(self, topic: str, callback: Callable[[str, bytes], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(topic, []).append(callback)
        self._client.subscribe(topic)

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            # rc is set synchronously by paho, before any real network
            # I/O - it can't confirm delivery, but it does catch the one
            # concrete, real failure mode this project has hit: calling
            # publish() while connect_async()'s handshake hasn't
            # finished yet (or mid-reconnect) silently no-ops instead of
            # raising, so every caller up the stack (thermostat/
            # interlock/_apply(), the /api/sensors/... routes) already
            # has a try/except ready for this - it just never fired.
            raise ConnectionError(f"MQTT publish to {topic!r} failed: {mqtt.error_string(info.rc)}")


def get_connection(broker: dict[str, Any]) -> MqttConnection:
    host = broker["host"]
    port = broker.get("port", DEFAULT_PORT)
    username = broker.get("username")
    key = (host, port, username)

    with _pool_lock:
        connection = _pool.get(key)
        if connection is None:
            connection = MqttConnection(
                host=host,
                port=port,
                username=username,
                password=broker.get("password"),
                client_id=broker.get("client_id"),
            )
            _pool[key] = connection
        return connection


def broker_down_for(now: float | None = None) -> float:
    """How long the longest-down broker connection has been down, in
    seconds (0 when every connection is up, or there are none)."""
    now = time.monotonic() if now is None else now
    with _pool_lock:
        downs = [c.down_since for c in _pool.values() if c.down_since is not None]
    return max((now - since for since in downs), default=0.0)


def connection_up(host: str, port: int) -> bool | None:
    """Whether LocalHome's own connection to the broker at host:port is
    up - None when it has none (no MQTT device on that broker)."""
    with _pool_lock:
        states = [c.down_since is None for (h, p, _), c in _pool.items() if (h, p) == (host, port)]
    return all(states) if states else None


def load_broker_config(options: dict) -> dict:
    """`broker_file` (resolved to an absolute path by DeviceManager) takes
    precedence; a plain inline `broker: {...}` mapping also works for a
    broker only one driver instance needs."""
    if "broker_file" in options:
        return load_json(options["broker_file"])
    return options.get("broker", {})


def state_cache_path(options: dict) -> str | None:
    """Where drivers/mqtt/state_cache.py persists this entry's last known
    payload(s): next to `broker_file`, same filename for every entry on
    that broker so they share one small file. None (no persistence) for
    an inline `broker: {...}` entry, since there's no file to anchor
    next to and no config.yaml directory to infer."""
    broker_file = options.get("broker_file")
    if not broker_file:
        return None
    return str(Path(broker_file).parent / "mqtt_state_cache.json")
