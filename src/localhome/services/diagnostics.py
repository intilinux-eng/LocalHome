"""One download with what's needed to understand a board from afar - the
admin panel's "Download diagnostics" (GET /api/climate-admin/diagnostics.zip).

Someone on site presses a button and sends the file; whoever reads it sees
what the board saw, without SSH:

- system-status.json - the admin panel's system status as it is now;
- localhome.log, network.log - this boot's journal for LocalHome, and for
  NetworkManager and the Wi-Fi watchdog (the journal lives in RAM on a
  tuned board: earlier boots are gone);
- watchdog-localhome.log, watchdog-service.log, watchdog-wifi.log - the
  watchdogs' own history, which does survive reboots;
- resource-log.csv - the resource log's recent lines, if it's installed;
- config-backup.zip - the same backup as Maintenance's, without passwords
  or keys.

Each part that can't be had here (no journalctl on a PC, no resource log)
is left out and listed in README.txt - the download itself never fails for it.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
import zipfile
from collections import deque

JOURNAL_LINES = 5000
RESOURCE_LOG_LINES = 3000   # every 10 minutes: about three weeks


def _journal(*units: str, lines: int = JOURNAL_LINES) -> str | None:
    args = ["journalctl", "-b", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    for unit in units:
        args += ["-u", unit]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _tail(path: str | None, lines: int) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except OSError:
        return None


def resource_log_path(env=os.environ) -> str:
    """Where deploy/resource-log writes, by the same rule it uses."""
    return env.get("LOCALHOME_RESOURCE_LOG") or os.path.expanduser("~/localhome-resource-log.csv")


def build_zip(status: dict, config_backup: bytes, localhome_events: str, wifi_events: str,
              version: str = "", journal=_journal, resource_log: str | None = None,
              service_events: str | None = None) -> bytes:
    parts: dict[str, str | bytes | None] = {
        "system-status.json": json.dumps(status, indent=2, ensure_ascii=False, default=str),
        "localhome.log": journal("localhome"),
        "network.log": journal("NetworkManager", "localhome-wifi-watchdog", lines=2000),
        "watchdog-localhome.log": _tail(localhome_events, 1000),
        "watchdog-wifi.log": _tail(wifi_events, 1000),
        "watchdog-service.log": _tail(service_events, 1000),
        "resource-log.csv": _tail(resource_log or resource_log_path(), RESOURCE_LOG_LINES),
        "config-backup.zip": config_backup,
    }
    missing = [name for name, content in parts.items() if content is None]
    readme = (f"LocalHome diagnostics - {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
              f"Version: {version or '?'}\n\n"
              "Not available on this machine: " + (", ".join(missing) or "nothing - all included") + "\n\n"
              + (__doc__ or ""))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme)
        for name, content in parts.items():
            if content is not None:
                zf.writestr(name, content)
    return buffer.getvalue()
