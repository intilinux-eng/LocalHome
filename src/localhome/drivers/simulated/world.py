"""Tiny shared in-memory state so simulated drivers can react to each
other - e.g. a simulated valve's on/off state feeding into a simulated
climate sensor's drift, so opening it in the dashboard visibly nudges the
temperature over the next few reads. Same shared-module pattern
drivers/mqtt/client.py uses for its connection pool. Only meaningful
between two `type: simulated` driver instances; real drivers never touch
this, and it never talks to any real hardware.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

_lock = threading.Lock()
_switch_state: dict[str, bool] = {}
_subscribers: dict[str, list[Callable[[str, Any], None]]] = {}


def set_switch(name: str, is_on: bool) -> None:
    with _lock:
        _switch_state[name] = is_on


def get_switch(name: str, default: bool = False) -> bool:
    with _lock:
        return _switch_state.get(name, default)


# What a simulated device "publishes" when it reports - the same topics
# and payloads a real Shelly sends its broker - for whoever listens the
# way they'd listen to the real one (services/shelly_health.py).

def subscribe(topic: str, handler: Callable[[str, Any], None]) -> None:
    with _lock:
        _subscribers.setdefault(topic, []).append(handler)


def publish(topic: str, payload: Any) -> None:
    with _lock:
        handlers = list(_subscribers.get(topic, ()))
    for handler in handlers:
        handler(topic, payload)
