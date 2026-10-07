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

import atexit
import json
import logging
import threading
import time
from typing import Any

from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# Written at most this often, by a thread of its own. save_cached() is
# called on every MQTT message, in the MQTT client's network thread: a
# read, a write and an fsync of the whole file each time held that thread
# for as long as the disk took - seen on the production board right after
# a boot, LocalHome's connection went 90 s without a keepalive and the
# broker dropped it (valve commands wait on that same thread). Now the
# message only updates memory; the file follows within these seconds, the
# newest payload of each topic in one write. A power cut loses at most
# that much - a reading the device sends again anyway.
FLUSH_SECONDS = 5.0
# A chatty topic - a meter reporting every few seconds - had the whole
# file rewritten and fsynced every 5 s: ~17,000 times a day on the
# production board, for readings it sends again within seconds of any
# restart. Now a topic that keeps talking (its last messages less than
# QUIET_SECONDS apart) is written at most every REWRITE_SECONDS; news from
# a quiet one - a valve switching, a battery sensor waking, what this
# cache is for - still goes out within FLUSH_SECONDS, and takes whatever
# else is waiting with it. Stopping writes everything (atexit).
QUIET_SECONDS = 60.0
REWRITE_SECONDS = 300.0

# Guards the file's read-modify-write cycle - one lock for every path is
# simpler than one per path, and still fully safe within this single
# process (the project's existing JSON stores make the same trade-off -
# see services/position_control.py).
_lock = threading.Lock()
# What's waiting to be written: path -> {key: entry}. Its own lock, so a
# message never waits on a write in progress.
_pending: dict[str, dict[str, dict]] = {}
_pending_lock = threading.Lock()
# Under _pending_lock, by (path, key): what's waiting that's news, to be
# written at once; each topic's last message time and the gap before it;
# when each topic last reached the file (monotonic).
_urgent: set[tuple[str, str]] = set()
_rhythm: dict[tuple[str, str], tuple[float, float | None]] = {}
_written_at: dict[tuple[str, str], float] = {}
_dirty = threading.Event()
_writer: threading.Thread | None = None


def _key(broker: dict, topic: str) -> str:
    return f"{broker.get('host')}:{broker.get('port', 1883)}/{topic}"


def _read_all(cache_path: str) -> dict:
    try:
        return load_json(cache_path)
    except (json.JSONDecodeError, OSError):
        return {}


def load_cached(cache_path: str, broker: dict, topic: str) -> tuple[Any, float] | None:
    """Returns (payload, received_at) if this topic has a cached value
    from a previous run (or from this one, not on disk yet), or None (a
    fresh setup, or a cache that predates this topic being added)."""
    with _lock:
        data = _read_all(cache_path)
    with _pending_lock:
        data.update(_pending.get(cache_path, {}))
    entry = data.get(_key(broker, topic))
    if entry is None:
        # Same topic under a different broker address - the broker was
        # re-addressed (e.g. an IP replaced by "localhost" or a .local
        # name), not replaced: without this, every battery sensor lost its
        # last reading and read as unreachable until it next woke up. The
        # newest match wins if several addresses have one.
        matches = [e for k, e in data.items() if k.split("/", 1)[-1] == topic and isinstance(e, dict)]
        entry = max(matches, key=lambda e: e.get("received_at") or 0, default=None)
    if entry is None:
        return None
    return entry.get("payload"), entry.get("received_at")


def save_cached(cache_path: str, broker: dict, topic: str, payload: Any, received_at: float) -> None:
    """Keeps this payload as the topic's last one - in memory at once, on
    disk within FLUSH_SECONDS, or REWRITE_SECONDS for a topic that keeps
    talking (flush() writes it now). Never touches the disk itself: it's
    called from the MQTT thread."""
    global _writer
    key = _key(broker, topic)
    with _pending_lock:
        _pending.setdefault(cache_path, {})[key] = {"payload": payload, "received_at": received_at}
        # News unless this message and the one before it both came less
        # than QUIET_SECONDS after their previous one: a change right after
        # another (on, then off) is still news.
        last_at, last_gap = _rhythm.get((cache_path, key), (None, None))
        gap = None if last_at is None else received_at - last_at
        _rhythm[(cache_path, key)] = (received_at, gap)
        if gap is None or gap >= QUIET_SECONDS or last_gap is None or last_gap >= QUIET_SECONDS:
            _urgent.add((cache_path, key))
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_write_loop, daemon=True, name="mqtt-state-cache")
            _writer.start()
    _dirty.set()


def _due_in(cache_path: str, key: str, now: float) -> float:
    """Seconds until this waiting entry should be written - 0: now."""
    if (cache_path, key) in _urgent or (cache_path, key) not in _written_at:
        return 0.0
    return max(0.0, REWRITE_SECONDS - (now - _written_at[(cache_path, key)]))


def flush(due_only: bool = False) -> float | None:
    """Writes whatever is waiting, now - one atomic write per file. With
    due_only (the writer thread), a file only once something in it is
    due; returns the seconds until the next is, or None if nothing waits."""
    now = time.monotonic()
    wait = None
    with _pending_lock:
        work = {}
        for cache_path, entries in list(_pending.items()):
            if due_only:
                due_in = min(_due_in(cache_path, key, now) for key in entries)
                if due_in > 0:
                    wait = due_in if wait is None else min(wait, due_in)
                    continue
            work[cache_path] = _pending.pop(cache_path)
            for key in entries:
                _urgent.discard((cache_path, key))
    for cache_path, entries in work.items():
        with _lock:
            data = _read_all(cache_path)
            data.update(entries)
            try:
                save_json_atomic(cache_path, data)
            except OSError:
                logger.exception("Failed to persist MQTT state cache to %s - will retry", cache_path)
                with _pending_lock:
                    for key, entry in entries.items():
                        _pending.setdefault(cache_path, {}).setdefault(key, entry)
                        _urgent.add((cache_path, key))
                wait = FLUSH_SECONDS
                continue
        written = time.monotonic()
        with _pending_lock:
            for key in entries:
                _written_at[(cache_path, key)] = written
    return wait


# Stopping (web/app.py turns systemd's SIGTERM into a normal exit): what's
# waiting is written before going.
atexit.register(flush)


def _write_loop() -> None:
    timeout = None
    while True:
        if _dirty.wait(timeout):
            time.sleep(FLUSH_SECONDS)    # what else comes meanwhile goes in the same write
        _dirty.clear()
        try:
            timeout = flush(due_only=True)
        except Exception:
            logger.exception("MQTT state cache: write failed, will retry")
            timeout = FLUSH_SECONDS
        if timeout is not None:
            timeout = max(timeout, FLUSH_SECONDS)
