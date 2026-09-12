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
        self._client.on_message = self._on_message
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        with self._lock:
            topics = list(self._subscribers)
        for topic in topics:
            client.subscribe(topic)

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
        self._client.publish(topic, payload, qos=qos, retain=retain)


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


def load_broker_config(options: dict) -> dict:
    """`broker_file` (resolved to an absolute path by DeviceManager) takes
    precedence; a plain inline `broker: {...}` mapping also works for a
    broker only one driver instance needs."""
    if "broker_file" in options:
        return load_json(options["broker_file"])
    return options.get("broker", {})
