"""Process, host and storage health for the admin panel's system status.

Everything here is read from the standard library and /proc, not psutil:
one more compiled dependency isn't worth it for a handful of numbers,
and the board this runs on in production is Linux anyway. Where a value
can't be read on the current platform it's None, and the UI shows a dash.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

# When this process started, as close to it as this module gets: it's
# imported while create_app() builds everything else. Monotonic, never
# the wall clock: the UNO Q has no battery-backed clock, so it boots
# with the time it last saved (often the evening before) and NTP fixes
# it moments later - a wall-clock timestamp taken in between stays wrong
# by however long the board was off. Wall-clock times are derived from
# this when asked (process_started_at()).
PROCESS_STARTED_MONO = time.monotonic()


def process_age_seconds() -> float:
    return time.monotonic() - PROCESS_STARTED_MONO


def process_started_at(now: float | None = None) -> float:
    """This process's start, on today's (corrected) wall clock."""
    return (time.time() if now is None else now) - process_age_seconds()

# CPU temperature worth flagging. Idle, the UNO Q's cores sit around
# 45°C; boards like it start throttling in the 85-95°C range.
CPU_HOT_C = 80

# Disk-space thresholds for flagging a problem - the history database
# and the JSON stores all live on the same (on the board, eMMC) disk,
# and a full disk fails every one of their writes at once.
DISK_LOW_FRACTION = 0.10
DISK_LOW_BYTES = 500 * 1024 * 1024

_cpu_lock = threading.Lock()
# (monotonic clock, process CPU seconds) at the previous reading - seeded
# here so the first reading doesn't count the CPU spent before this import.
_cpu_last: tuple[float, float] = (PROCESS_STARTED_MONO, time.process_time())


def _proc_status() -> dict[str, int]:
    """VmRSS/VmHWM/Threads from /proc/self/status, in kB (or a count)."""
    values: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmHWM", "Threads"):
                    values[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return values


def _windows_memory() -> tuple[int, int] | None:
    """(current, peak) working set in bytes, on Windows - for a dev machine."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize, counters.PeakWorkingSetSize
    except Exception:
        return None


def _cpu_percent() -> float | None:
    """This process's CPU use since the previous call (or since startup,
    on the first one), as a percentage of one core."""
    global _cpu_last
    now, cpu = time.monotonic(), time.process_time()
    with _cpu_lock:
        last_wall, last_cpu = _cpu_last
        _cpu_last = (now, cpu)
    elapsed = now - last_wall
    if elapsed <= 0:
        return None
    return round(100 * (cpu - last_cpu) / elapsed, 1)


def process_stats() -> dict:
    status = _proc_status()
    rss = status.get("VmRSS", 0) * 1024 or None
    peak = status.get("VmHWM", 0) * 1024 or None
    if rss is None:
        windows = _windows_memory()
        if windows:
            rss, peak = windows
    return {
        "pid": os.getpid(),
        "uptime_seconds": round(process_age_seconds()),
        "rss_bytes": rss,
        "peak_rss_bytes": peak,
        # /proc counts every OS thread (including ones native libraries
        # start); threading only knows Python's own. Prefer the former.
        "threads": status.get("Threads") or threading.active_count(),
        "cpu_percent": _cpu_percent(),
        "started_at": round(process_started_at()),
        "python": sys.version.split()[0],
    }


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return values


def cpu_temperature_c(sys_root: str = "/sys") -> float | None:
    """The hottest CPU thermal zone, in °C - zones whose type names the
    CPU ("cpuss0-thermal" on the UNO Q, "cpu-thermal" on a Pi), or every
    zone if none does. None where the kernel exposes none (a PC)."""
    base = os.path.join(sys_root, "class", "thermal")
    try:
        zones = [z for z in os.listdir(base) if z.startswith("thermal_zone")]
    except OSError:
        return None
    readings = []
    for zone in zones:
        kind = _read(os.path.join(base, zone, "type")) or ""
        raw = _read(os.path.join(base, zone, "temp"))
        try:
            readings.append((kind, int(raw) / 1000))
        except (TypeError, ValueError):
            continue
    cpu = [t for kind, t in readings if "cpu" in kind.lower()]
    values = cpu or [t for _, t in readings]
    return round(max(values), 1) if values else None


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return round(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def boot_times() -> dict:
    """When the board came up, and when LocalHome started on it. Both
    matter for reading a device's silence: a last reading older than
    these was never missed - nobody was listening.

    Computed on every call, from the kernel's uptime and this process's
    monotonic age: a value taken once at startup was taken before NTP had
    corrected the clock, and a board switched on at 17:11 said it had been
    up since 23:59 the evening before ("on for 17h")."""
    now = time.time()
    uptime = _uptime_seconds()
    return {
        "booted_at": round(now - uptime) if uptime is not None else None,
        "started_at": round(process_started_at(now)),
    }


def host_stats() -> dict:
    meminfo = _meminfo()
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    uptime = _uptime_seconds()
    return {
        "cpu_count": os.cpu_count(),
        "load_average": load,
        "mem_total_bytes": meminfo.get("MemTotal"),
        "mem_available_bytes": meminfo.get("MemAvailable"),
        "uptime_seconds": uptime,
        "booted_at": round(time.time() - uptime) if uptime is not None else None,
        "cpu_temp_c": cpu_temperature_c(),
        "os": _os_name(),
        "kernel": platform.release(),
        "clock_synced": _clock_synced(),
    }


def _os_name() -> str | None:
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


# systemd-timesyncd creates this file once it has synchronized the clock
# since boot - checking it is a stat, cheap enough for every page poll and
# the LED matrix, where running timedatectl each time wouldn't be.
TIMESYNC_DIR = "/run/systemd/timesync"


def clock_synced_fast() -> bool | None:
    """Whether the clock has been synchronized over NTP since boot. None
    where systemd-timesyncd isn't what keeps the time (a PC, another NTP
    client): then nothing is claimed either way."""
    if not os.path.isdir(TIMESYNC_DIR):
        return None
    return os.path.exists(os.path.join(TIMESYNC_DIR, "synchronized"))


def _clock_synced() -> bool | None:
    """Whether systemd says the clock is synchronized over NTP - the
    schedules run on it. None where there's no timedatectl (a PC)."""
    fast = clock_synced_fast()
    if fast is not None:
        return fast
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return {"yes": True, "no": False}.get(value)


_LAST_LINE = re.compile(
    r"^reboot\s+system boot\s+\S+\s+(\w{3} \w{3} +\d+ [\d:]+ \d{4})\s+-\s+(still running|crash|\w{3} \w{3} +\d+ [\d:]+ \d{4})"
)


def boot_history(limit: int = 5, run=None) -> list[dict]:
    """The board's last boots, newest first, each with how that session
    ended: "running" (this one), "clean" (a proper shutdown or reboot) or
    "crash" (the power went, or the board hung) - from the system's own
    login records (`last`). Empty where they aren't available."""
    try:
        out = (run or subprocess.run)(["last", "-x", "-F", "-n", str(limit), "reboot"],
                                      capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    boots = []
    for line in out.stdout.splitlines():
        match = _LAST_LINE.match(line)
        if not match:
            continue
        try:
            booted = time.mktime(time.strptime(re.sub(r" +", " ", match.group(1)), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        end = match.group(2)
        boots.append({"booted_at": round(booted),
                      "ended": "running" if end == "still running" else "crash" if end == "crash" else "clean"})
    return boots


def _nmcli_fields(line: str) -> list[str]:
    """Splits one `nmcli -t` line: fields are ':'-separated, and a ':' or
    '\\' inside a value (an SSID, a MAC address) is escaped with '\\'."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def _nmcli(*args: str) -> list[list[str]] | None:
    try:
        out = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None  # no NetworkManager here (a PC, a container)
    if out.returncode != 0:
        return None
    return [_nmcli_fields(line) for line in out.stdout.splitlines() if line]


def network_status(run=_nmcli) -> dict:
    """The Wi-Fi network the board is on, as NetworkManager reports it
    (read-only - nothing here changes the connection), plus the names it
    answers to. `wifi` is None when there's no NetworkManager or no Wi-Fi
    in use (a wired board, a PC)."""
    hostname = socket.gethostname()
    status = {"hostname": hostname, "mdns_name": f"{hostname}.local", "wifi": None}
    devices = run("-f", "DEVICE,TYPE,STATE", "device") or []
    wifi_device = next((d[0] for d in devices if len(d) >= 3 and d[1] == "wifi" and d[2] == "connected"), None)
    if wifi_device is None:
        return status
    active = next((row for row in (run("-f", "ACTIVE,SSID,SIGNAL,FREQ,CHAN,SECURITY", "device", "wifi", "list",
                                        "ifname", wifi_device, "--rescan", "no") or [])
                   if row and row[0] == "yes"), None)
    details = {}
    for row in run("-f", "IP4.ADDRESS,IP4.GATEWAY,GENERAL.HWADDR", "device", "show", wifi_device) or []:
        if len(row) >= 2:
            details[row[0].split("[")[0]] = ":".join(row[1:])
    wifi = {"device": wifi_device, "ip": (details.get("IP4.ADDRESS") or "").split("/")[0] or None,
            "gateway": details.get("IP4.GATEWAY") or None, "mac": details.get("GENERAL.HWADDR") or None}
    if active and len(active) >= 6:
        freq = re.match(r"(\d+)", active[3] or "")
        wifi.update(
            ssid=active[1] or None,
            signal_percent=int(active[2]) if active[2].isdigit() else None,
            band="5 GHz" if freq and int(freq.group(1)) >= 4900 else "2.4 GHz" if freq else None,
            channel=int(active[4]) if active[4].isdigit() else None,
            security=active[5] or None,
        )
    status["wifi"] = wifi
    return status


# Weak enough that dropped MQTT messages and slow pages become likely.
# How long after a boot a device that hasn't reported yet is still
# "waiting" rather than "offline" - a Shelly H&T's own heartbeat ceiling.
FRESH_BOOT_SECONDS = 2 * 3600

WIFI_WEAK_PERCENT = 30


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def storage_health(sys_root: str = "/sys", proc_root: str = "/proc", uptime_seconds: float | None = None) -> dict | None:
    """The board's eMMC as it reports itself: wear in 10% steps (JEDEC
    DEVICE_LIFE_TIME_EST, the worse of its two estimates), whether it
    has entered its reserve blocks (PRE_EOL_INFO), and how much has been
    written since boot, per day. None where there's no eMMC to ask (a PC,
    an SD card)."""
    mmc_host = os.path.join(sys_root, "class", "mmc_host")
    try:
        hosts = sorted(os.listdir(mmc_host))
    except OSError:
        return None
    for host in hosts:
        try:
            cards = sorted(os.listdir(os.path.join(mmc_host, host)))
        except OSError:
            continue
        for card in cards:
            base = os.path.join(mmc_host, host, card)
            life_time = _read(os.path.join(base, "life_time"))
            if not life_time or _read(os.path.join(base, "type")) != "MMC":
                continue
            try:
                steps = [int(v, 16) for v in life_time.split()]
                pre_eol = int(_read(os.path.join(base, "pre_eol_info")) or "0", 16)
            except ValueError:
                continue
            worst = max(steps) if steps else 0
            written = None
            for line in (_read(os.path.join(proc_root, "diskstats")) or "").splitlines():
                fields = line.split()
                if len(fields) > 9 and re.fullmatch(r"mmcblk\d+", fields[2]):
                    written = int(fields[9]) * 512
                    break
            per_day = None
            if written is not None and uptime_seconds:
                per_day = round(written / uptime_seconds * 86400)
            return {
                "name": _read(os.path.join(base, "name")),
                # 0x01 = 0-10% used ... 0x0A = 90-100%, 0x0B = past its rated life
                "wear_percent_max": min(worst, 11) * 10 if worst else None,
                "pre_eol": {1: "normal", 2: "warning", 3: "urgent"}.get(pre_eol, "unknown"),
                "written_since_boot_bytes": written,
                "written_per_day_bytes": per_day,
            }
    return None


def disk_stats(path: str) -> dict:
    """Space on the disk holding `path` (the history database's folder)."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"path": path, "total_bytes": None, "free_bytes": None, "low": False}
    low = usage.free < DISK_LOW_BYTES or usage.free < usage.total * DISK_LOW_FRACTION
    return {"path": path, "total_bytes": usage.total, "free_bytes": usage.free, "low": low}


def device_health(manager) -> list[dict]:
    """Every polled device's latest reading reduced to what a diagnosis
    needs: reachable or not, why not, and how old its data is."""
    devices = []
    for name, poller in manager.pollers.items():
        reading = poller.get()
        devices.append(
            {
                "name": name,
                "kind": manager.sensor_kinds.get(name, "unknown"),
                "ok": bool(reading.get("ok")),
                "error": None if reading.get("ok") else reading.get("error"),
                "seconds_since_update": reading.get("seconds_since_update"),
            }
        )
    return devices


def cover_health(last_reads: dict, now: float | None = None) -> list[dict]:
    """Covers are the one kind of device nothing polls in the background:
    they're read when the dashboard asks. This turns what those reads
    last said into the same shape device_health() produces, so an
    unreachable cover shows up in the system status like anything else.
    A cover nobody has opened the dashboard for yet simply isn't listed."""
    now = time.time() if now is None else now
    return [
        {
            "name": name,
            "kind": "cover",
            "ok": bool(read.get("ok")),
            "error": None if read.get("ok") else read.get("error"),
            "seconds_since_update": round(now - read["at"]) if read.get("at") else None,
        }
        for name, read in sorted(last_reads.items())
    ]


def loop_health(name: str, last_ok_ts: float | None, interval_seconds: float, now: float | None = None) -> dict:
    """Whether a background loop is still completing its cycles. Stalled
    after three missed intervals - but not during the first ones after
    startup, when a loop that hasn't finished its first cycle yet is
    normal."""
    now = time.time() if now is None else now
    grace = 3 * interval_seconds
    if last_ok_ts is None:
        stalled = process_age_seconds() > grace
        ago = None
    else:
        ago = round(now - last_ok_ts)
        stalled = ago > grace
    return {"name": name, "interval_seconds": interval_seconds, "last_ok_seconds_ago": ago, "stalled": stalled}


def _short_error(error: str) -> str:
    """The part of a driver error worth showing: the list of every
    registered driver that "no driver registered" errors end with is
    useful in the log, noise in a list of problems."""
    return error.split(" Available kind/type combinations:")[0].strip()


def problems(
    devices: list[dict], failed: list[dict], disk: dict, loops: list[dict] = (), storage: dict | None = None,
    cpu_temp_c: float | None = None, network: dict | None = None, host: dict | None = None,
) -> list[dict]:
    """The things worth a technician's attention, most serious first.
    `code` keys the UI's own translated wording; the rest is detail.

    A driver module skipped for a missing optional package isn't one of
    them on its own - nobody may be using it. If an integration does use
    it, that integration is in `failed`, with the install command in its
    error (see core/registry.py's create())."""
    found = []
    for item in failed:
        found.append({"code": "integration_failed", "subject": item.get("name") or f"{item['kind']}/{item['type']}",
                      "detail": _short_error(item["error"])})
    for loop in loops:
        if loop["stalled"]:
            found.append({"code": "loop_stalled", "subject": loop["name"], "detail": None})
    if disk.get("low"):
        found.append({"code": "disk_low", "subject": disk["path"], "detail": None})
    if host and host.get("clock_synced") is False:
        found.append({"code": "clock_unsynced", "subject": "NTP", "detail": None})
    wifi = (network or {}).get("wifi") or {}
    if wifi.get("signal_percent") is not None and wifi["signal_percent"] < WIFI_WEAK_PERCENT:
        found.append({"code": "wifi_weak", "subject": wifi.get("ssid") or "Wi-Fi", "detail": f"{wifi['signal_percent']}%"})
    if cpu_temp_c is not None and cpu_temp_c >= CPU_HOT_C:
        found.append({"code": "cpu_hot", "subject": f"{cpu_temp_c:.0f}°C", "detail": None})
    if storage and (storage["pre_eol"] in ("warning", "urgent") or (storage["wear_percent_max"] or 0) >= 80):
        found.append({"code": "storage_wear", "subject": storage.get("name") or "eMMC",
                      "detail": f"{storage['wear_percent_max']}% - {storage['pre_eol']}"})
    # A board that came up minutes ago hasn't heard from its battery
    # sensors yet - they speak every couple of hours. That's "not yet",
    # not "offline", and calling it offline right after every power cut
    # is how a problems list gets ignored.
    just_booted = (host or {}).get("uptime_seconds") is not None and host["uptime_seconds"] < FRESH_BOOT_SECONDS
    for device in devices:
        if not device["ok"]:
            never_heard = device.get("seconds_since_update") is None
            code = "device_not_heard_yet" if (never_heard and just_booted) else "device_offline"
            found.append({"code": code, "subject": device["name"], "detail": device["error"]})
    return found
