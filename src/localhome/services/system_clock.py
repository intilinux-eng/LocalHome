"""What the *server's* clock says, for the web UI to show and cross-check.

The thermostat picks each hour's target from `datetime.now()` - the
server's own local wall-clock time, in whatever timezone the OS is set
to (services/thermostat.py). A board left on its factory UTC default
(a fresh Debian image, including the Arduino UNO Q's) therefore applies
every schedule slot hours off, while any clock drawn from the browser's
own time keeps showing the right hour and hides it completely. This was
live in production for the first deployment: 08:00 in the house, 06:00
slot being applied.

So the UI shows this instead of `new Date()`, and compares it against
the browser to flag a mismatch - see static/server_clock.js.
"""
from __future__ import annotations

import os
from datetime import datetime


def system_timezone_name() -> str | None:
    """Best-effort IANA name ("Europe/Rome") of the zone the OS is set
    to. None where there's no such name to read - Windows, or an
    /etc/localtime that isn't a link into the zoneinfo database - which
    is fine: the offset and abbreviation are always available, this is
    just the most recognizable way to tell an installer what's set."""
    try:
        target = os.path.realpath("/etc/localtime").replace("\\", "/")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def server_time(now: datetime | None = None) -> dict:
    # An aware `now` keeps its own zone (lets a test pin one exactly);
    # otherwise it's the system's local time, the one the thermostat uses.
    local = now if (now is not None and now.tzinfo is not None) else (now or datetime.now()).astimezone()
    offset = local.utcoffset()
    return {
        "epoch_ms": int(local.timestamp() * 1000),
        "utc_offset_minutes": int(offset.total_seconds() // 60) if offset is not None else 0,
        "tz_abbrev": local.tzname(),
        "tz_name": system_timezone_name(),
    }
