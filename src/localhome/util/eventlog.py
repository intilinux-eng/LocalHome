"""Event logs - one line per event, "<date> <time> <code> [values...]",
rare enough to live on disk and survive the reboots they're about: what
the watchdogs did (services/service_watchdog.py, the Wi-Fi one), and a
Shelly relay back at rest by its own safety timer (drivers/mqtt/switch.py).
The admin panel words each code in its own language.
"""
from __future__ import annotations

import threading
import time

EVENT_LOG_KEEP = 200
# The watchdog and the MQTT thread can both write the same log.
_lock = threading.Lock()


def append_event(path: str, code: str, *values, at: float | None = None) -> None:
    """One line in an event log, keeping the last EVENT_LOG_KEEP - dated
    now, or `at` for something that happened earlier and is known only
    now."""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
    line = " ".join([when, code, *[str(v) for v in values]])
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        lines = (lines + [line])[-EVENT_LOG_KEEP:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def read_events(path: str, source: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        parts = line.split(" ", 3)
        if len(parts) >= 3:
            events.append({"when": f"{parts[0]} {parts[1]}", "source": source, "code": parts[2],
                           "values": parts[3] if len(parts) == 4 else ""})
    return events


def event_time(event: dict) -> float | None:
    """An event's date and time as a timestamp, None if it doesn't parse."""
    try:
        return time.mktime(time.strptime(event["when"], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, ValueError, OverflowError):
        return None
