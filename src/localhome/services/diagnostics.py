"""One download with what's needed to understand a board from afar - the
admin panel's "Download diagnostics" (GET /api/climate-admin/diagnostics.zip).

Someone on site presses a button and sends the file; whoever reads it sees
what the board saw, without SSH:

- summary.txt - the rest in a few lines, in the board's language: how long
  it's been on and how each session ended, when it was off, the Wi-Fi,
  the devices, the problems, what the watchdogs did;
- system-status.json - the admin panel's system status as it is now;
- localhome.log, network.log, mosquitto.log - this boot's journal for
  LocalHome; for NetworkManager, wpa_supplicant and the Wi-Fi watchdog
  (without systemd's lines about each of its runs); for the MQTT broker -
  each Shelly connecting and leaving. The journal lives in RAM on a tuned
  board: earlier boots are gone;
- watchdog-localhome.log, watchdog-service.log, watchdog-wifi.log - the
  watchdogs' own history, which does survive reboots;
- resource-log.csv - the resource log's recent lines, if it's installed;
- config-backup.zip - the same backup as Maintenance's, without passwords
  or keys.

Each part that can't be had here (no journalctl on a PC, no resource log)
is left out and listed in README.txt - the download itself never fails for it.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import time
import zipfile
from collections import deque
from datetime import datetime

from localhome.services import system_status

logger = logging.getLogger(__name__)

JOURNAL_LINES = 5000
NETWORK_LINES = 2000
BROKER_LINES = 3000
RESOURCE_LOG_LINES = 3000   # every 10 minutes: about three weeks
SUMMARY_EVENTS = 40         # the watchdogs' last actions on the summary
SUMMARY_RUNS = 30           # the board's last times on
# The Wi-Fi watchdog runs every minute, and systemd notes each run in three
# lines ("Starting", "Deactivated successfully", "Finished"): a board's
# network.log held 1999 of them in its 2000 lines - half a day, and one
# line of the network's own. Left out, the same lines cover days.
_TIMER_NOISE = re.compile(r"systemd\[1\]: (?:Starting|Started|Finished) localhome-wifi-watchdog\.service"
                          r"|localhome-wifi-watchdog\.service: (?:Deactivated successfully|Consumed )")
# 5 GHz channels the router has to leave at once when it hears a radar
# (DFS): the Wi-Fi drops for a minute or more each time.
DFS_CHANNELS = range(52, 145)
# Two lines of the resource log further apart than this (it writes one
# every 10 minutes): the board was off in between.
OFF_GAP_SECONDS = 900


def _journal(*units: str, lines: int = JOURNAL_LINES) -> str | None:
    args = ["journalctl", "-b", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    for unit in units:
        args += ["-u", unit]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _network_log(journal) -> str | None:
    text = journal("NetworkManager", "wpa_supplicant", "localhome-wifi-watchdog", lines=NETWORK_LINES * 10)
    if text is None:
        return None
    kept = [line for line in text.splitlines(keepends=True) if not _TIMER_NOISE.search(line)]
    return "".join(kept[-NETWORK_LINES:])


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


# ------------------------------------------------------------ summary --

def _translator(translations: dict):
    """tr("section.key", name=value): the board's wording, its {name}
    placeholders filled - the key itself where there's no wording."""
    def tr(key: str, **values) -> str:
        section, _, name = key.partition(".")
        text = (translations.get(section) or {}).get(name)
        if not isinstance(text, str):
            return key
        return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
    return tr


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _duration(seconds: float, tr) -> str:
    """Like "3 d 2 h", "1 h 36 min" or "45 s", in the board's units."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} {tr('diagnostics.unit_second')}"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    parts = [f"{days} {tr('diagnostics.unit_day')}"] if days else []
    if hours:
        parts.append(f"{hours} {tr('diagnostics.unit_hour')}")
    if not days and rest // 60:
        parts.append(f"{rest // 60} {tr('diagnostics.unit_minute')}")
    return " ".join(parts)


def power_runs(resource_csv: str) -> tuple[list[tuple[float, float]], int]:
    """Each time the board was on - (booted, last seen), oldest first -
    from the resource log's lines (deploy/resource-log: one every 10
    minutes, with the uptime), and how many NUL bytes the file holds: a
    line being written when the power went leaves them. A line that
    doesn't parse is skipped - the header, or a tail starting mid-file.
    Each run's boot is worked out from its latest line: the first ones
    can carry the clock from before the board synchronized it."""
    runs: list[list[float]] = []
    last_up = None
    for row in csv.reader(io.StringIO(resource_csv.replace("\x00", ""))):
        try:
            seen = datetime.fromisoformat(row[0]).timestamp()
            up = float(row[1])
        except (IndexError, ValueError):
            continue
        if last_up is None or up < last_up:
            runs.append([seen - up, seen])
        else:
            runs[-1] = [seen - up, seen]
        last_up = up
    return [(boot, seen) for boot, seen in runs], resource_csv.count("\x00")


def _event_text(event: dict, tr) -> str:
    """A watchdog's action in words - as the admin panel says it."""
    values = event.get("values", "").split()
    first = values[0] if values else ""
    if event["code"] in ("back", "lan_back") and first.isdigit():
        first = _duration(int(first), tr)
    text = tr(f"climate_admin.wd_{event['code']}", a=first, b=values[1] if len(values) > 1 else "",
              all=event.get("values", ""))
    if text.startswith("climate_admin."):
        return f"{event['code']} {event.get('values', '')}".strip()
    if event["code"] == "lan_back" and first:
        text += " " + tr("diagnostics.after", duration=first)
    return text


def summary(status: dict, translations: dict, version: str = "", resource_csv: str | None = None,
            events: list[dict] = (), now: float | None = None) -> str:
    """The download's first page, in the board's language: what a person
    reading it by hand wants first - how long the board has been on and
    how each session ended, when it was off, its Wi-Fi, the devices, the
    problems, what the watchdogs did. The other files have the detail."""
    tr = _translator(translations)
    now = time.time() if now is None else now
    host, process = status.get("host") or {}, status.get("process") or {}
    network = status.get("network") or {}
    wifi = network.get("wifi") or {}
    out = [tr("diagnostics.title"),
           tr("diagnostics.board", name=network.get("mdns_name") or network.get("hostname") or "?",
              ip=wifi.get("ip") or "?", version=version or "?"),
           tr("diagnostics.generated", when=_when(now))]

    def section(heading: str, lines: list[str]) -> None:
        out.extend(["", f"== {heading} ==", *[f"  {line}" for line in lines]])

    lines = []
    if host.get("uptime_seconds") is not None:
        lines.append(tr("diagnostics.up_since", duration=_duration(host["uptime_seconds"], tr),
                        since=_when(now - host["uptime_seconds"])))
    if process.get("uptime_seconds") is not None:
        lines.append(tr("diagnostics.localhome_since", duration=_duration(process["uptime_seconds"], tr),
                        since=_when(now - process["uptime_seconds"])))
    section(tr("diagnostics.up_heading"), lines or [tr("diagnostics.unknown")])

    boots = [b for b in status.get("boots") or [] if b.get("booted_at")]
    if boots:
        section(tr("diagnostics.boots_heading"), [tr("climate_admin.system_boots_note")] + [
            f"{_when(b['booted_at'])}  {tr('climate_admin.system_boot_' + str(b.get('ended')))}" for b in boots])

    runs, nul = power_runs(resource_csv) if resource_csv else ([], 0)
    if runs:
        lines, previous = [], None
        for boot, seen in runs[-SUMMARY_RUNS:]:
            if previous is not None and boot - previous > OFF_GAP_SECONDS:
                lines.append("    " + tr("diagnostics.off_for", duration=_duration(boot - previous, tr)))
            lines.append(tr("diagnostics.on_for", start=_when(boot), end=_when(seen), duration=_duration(seen - boot, tr)))
            previous = seen
        lines.append(tr("diagnostics.longest", duration=_duration(max(seen - boot for boot, seen in runs), tr)))
        if nul:
            lines.append(tr("diagnostics.nul_bytes", count=nul))
        section(tr("diagnostics.power_heading", since=_when(runs[0][0])), lines)

    lines = []
    if wifi.get("ssid"):
        channel = wifi.get("channel")
        lines.append(tr("diagnostics.wifi", ssid=wifi["ssid"], band=wifi.get("band") or "?",
                        channel=channel if channel is not None else "?",
                        signal=wifi["signal_percent"] if wifi.get("signal_percent") is not None else "?"))
        if isinstance(channel, int) and channel in DFS_CHANNELS and "5" in str(wifi.get("band") or ""):
            lines.append(tr("diagnostics.dfs_channel", channel=channel))
    else:
        lines.append(tr("diagnostics.no_wifi"))
    errors = (host.get("hardware") or {}).get("wifi_errors")
    if errors:
        lines.append(tr("diagnostics.wifi_errors", count=errors))
    section(tr("diagnostics.network_heading"), lines)

    lines = []
    for device in status.get("devices") or []:
        name, age = device.get("name", "?"), device.get("seconds_since_update")
        if not device.get("ok"):
            lines.append(tr("diagnostics.device_error", name=name, error=device.get("error") or "?"))
        elif age is not None:
            lines.append(tr("diagnostics.device_ok", name=name, age=_duration(age, tr)))
        else:
            lines.append(tr("diagnostics.device_ok_now", name=name))
    section(tr("diagnostics.devices_heading"), lines or [tr("diagnostics.none")])

    lines = []
    for problem in status.get("problems") or []:
        code, subject = problem.get("code"), problem.get("subject") or ""
        if code == "protection_missing":
            subject = tr(f"climate_admin.protection_{subject}")
        text = tr(f"climate_admin.system_problem_{code}", subject=subject)
        if text.startswith("climate_admin."):
            text = f"{code} {subject}".strip()
        if problem.get("detail"):
            text += f" - {problem['detail']}"
        lines.append(text)
    section(tr("diagnostics.problems_heading"), lines or [tr("diagnostics.no_problems")])

    shown = list(events)[:SUMMARY_EVENTS][::-1]      # newest first in, oldest first out
    section(tr("diagnostics.watchdog_heading", count=len(shown)),
            [f"{event.get('when', '')[:16]}  {_event_text(event, tr)}" for event in shown] or [tr("diagnostics.none")])

    lines = []
    if process.get("rss_bytes"):
        lines.append(tr("diagnostics.memory", rss=round(process["rss_bytes"] / 1e6),
                        available=round((host.get("mem_available_bytes") or 0) / 1e6),
                        total=round((host.get("mem_total_bytes") or 0) / 1e6)))
    storage = status.get("storage") or {}
    if storage.get("written_per_day_bytes") is not None:
        lines.append(tr("diagnostics.flash", written=round(storage["written_per_day_bytes"] / 1e6),
                        wear=storage.get("wear_percent_max", "?")))
    if host.get("cpu_temp_c") is not None:
        lines.append(tr("diagnostics.cpu_temp", temp=round(host["cpu_temp_c"])))
    section(tr("diagnostics.resources_heading"), lines or [tr("diagnostics.unknown")])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- zip --

def build_zip(status: dict, config_backup: bytes, localhome_events: str, wifi_events: str,
              version: str = "", journal=_journal, resource_log: str | None = None,
              service_events: str | None = None, translations: dict | None = None) -> bytes:
    resource = _tail(resource_log or resource_log_path(), RESOURCE_LOG_LINES)
    if translations is None:
        from localhome.web.i18n import DEFAULT_LANGUAGE, load_translations
        translations = load_translations(DEFAULT_LANGUAGE)
    try:
        events = system_status.watchdog_history(localhome_events, wifi_events, service_events or "",
                                                limit=SUMMARY_EVENTS)
        summary_text = summary(status, translations, version, resource, events)
    except Exception:
        # The download never fails for one of its parts.
        logger.exception("Diagnostics: couldn't write the summary")
        summary_text = None
    parts: dict[str, str | bytes | None] = {
        "summary.txt": summary_text,
        "system-status.json": json.dumps(status, indent=2, ensure_ascii=False, default=str),
        "localhome.log": journal("localhome"),
        "network.log": _network_log(journal),
        "mosquitto.log": journal("mosquitto", lines=BROKER_LINES),
        "watchdog-localhome.log": _tail(localhome_events, 1000),
        "watchdog-wifi.log": _tail(wifi_events, 1000),
        "watchdog-service.log": _tail(service_events, 1000),
        "resource-log.csv": resource,
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
