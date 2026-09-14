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

_lock = threading.Lock()
_switch_state: dict[str, bool] = {}


def set_switch(name: str, is_on: bool) -> None:
    with _lock:
        _switch_state[name] = is_on


def get_switch(name: str, default: bool = False) -> bool:
    with _lock:
        return _switch_state.get(name, default)
