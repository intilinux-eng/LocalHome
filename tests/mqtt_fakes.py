"""A fake MqttConnection for driver tests: same subscribe()/publish()
surface as the real one (drivers/mqtt/client.py), entirely in-memory -
no socket, no broker process. `deliver()` simulates an incoming message
exactly like the real connection's on_message dispatch would.
"""
from __future__ import annotations


class FakeMqttConnection:
    def __init__(self):
        self.subscribers: dict[str, list] = {}
        self.published: list[tuple[str, str]] = []
        # Optional: called with (topic, payload) as each message is
        # published - a device answering at once, without a thread.
        self.on_publish = None

    def subscribe(self, topic, callback):
        self.subscribers.setdefault(topic, []).append(callback)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))
        if self.on_publish is not None:
            self.on_publish(topic, payload)

    def deliver(self, topic: str, payload) -> None:
        payload_bytes = payload.encode() if isinstance(payload, str) else payload
        for callback in self.subscribers.get(topic, []):
            callback(topic, payload_bytes)
