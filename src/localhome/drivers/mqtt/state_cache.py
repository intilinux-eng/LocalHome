"""Persists each MQTT topic's last known payload across restarts.

Without this, every driver in this package starts from a blank slate on
every restart: `MqttJsonState` had no message yet, so `read()` raises
"no MQTT message received yet" until the device happens to publish
again - which for a battery sensor that only wakes on a threshold change
(or a worst-case multi-hour ceiling) can take a long time, and for a
device that publishes with MQTT's `retain` flag unset, a freshly
subscribing client gets nothing at all from the broker itself in the
meantime (confirmed against a real Shelly H&T - see
docs/integrations/mqtt.md).

That's not just a cosmetic "unreachable" flash on the dashboard: the
thermostat (services/thermostat.py) correctly leaves a zone's valve
alone while its sensor reads as not-`ok`, rather than acting on a
missing/zero temperature - but "leave it alone" for up to that sensor's
whole worst-case reporting window after *every* restart means a valve
can sit in a now-stale state for hours before the thermostat gets to
make a fresh decision again. Seeding from the last known reading (with
its real, possibly-old timestamp) lets `stale_after_seconds` keep doing
its actual job - trust a recent-enough last value, only flag it
unreachable once it's genuinely too old - instead of every restart
forcing a full blackout window first.

One shared JSON file per broker directory, keyed by "host:port/topic" so
two different brokers can't collide on an identical topic string across
files; the file only ever holds one entry per topic (overwritten on
every message), so there's no unbounded growth to prune, unlike a time
series. Every write goes through the same atomic save used for
schedules.json/positions.json, so a power cut mid-write can't corrupt it.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# Guards the whole read-modify-write cycle below, not just one file - a
# handful of topics across a couple of cache files is little enough
# traffic that one lock is simpler than one per path, and still fully
# safe within this single process (the project's existing JSON stores
# make the same single-process trade-off - see services/position_control.py).
_lock = threading.Lock()


def _key(broker: dict, topic: str) -> str:
    return f"{broker.get('host')}:{broker.get('port', 1883)}/{topic}"


def _read_all(cache_path: str) -> dict:
    try:
        return load_json(cache_path)
    except (json.JSONDecodeError, OSError):
        return {}


def load_cached(cache_path: str, broker: dict, topic: str) -> tuple[Any, float] | None:
    """Returns (payload, received_at) if this topic has a cached value
    from a previous run, or None (a fresh setup, or a cache that
    predates this topic being added)."""
    with _lock:
        entry = _read_all(cache_path).get(_key(broker, topic))
    if entry is None:
        return None
    return entry.get("payload"), entry.get("received_at")


def save_cached(cache_path: str, broker: dict, topic: str, payload: Any, received_at: float) -> None:
    with _lock:
        data = _read_all(cache_path)
        data[_key(broker, topic)] = {"payload": payload, "received_at": received_at}
        try:
            save_json_atomic(cache_path, data)
        except OSError:
            logger.exception("Failed to persist MQTT state cache to %s - will retry on the next message", cache_path)
