"""Process, host and storage health for the admin panel's system status.

Everything here is read from the standard library and /proc, not psutil:
one more compiled dependency isn't worth it for a handful of numbers,
and the board this runs on in production is Linux anyway. Where a value
can't be read on the current platform it's None, and the UI shows a dash.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from localhome.util import eventlog

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


def _meminfo(keys=("MemTotal", "MemAvailable"), proc_root: str = "/proc") -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open(os.path.join(proc_root, "meminfo"), encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in keys:
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


# The zones worth naming on their own, by what the UNO Q calls them
# ("wlan-thermal", "gpu-thermal", "mdm0-thermal") - the CPU has its own row.
_NAMED_ZONES = {"wlan": "wifi", "gpu": "gpu", "mdm": "modem"}


def board_hardware(sys_root: str = "/sys", proc_root: str = "/proc", base_root: str = "/") -> dict:
    """What the board's hardware says about itself, all from files - no
    subprocess, so the admin panel can ask on every refresh:

    - which board: model, chip, serial number (from the device tree);
    - how warm, beyond the CPU: the Wi-Fi chip, the GPU, the modem, and
      where the kernel starts slowing the CPU down to cool it (its first
      "passive" trip point - 90°C on the UNO Q);
    - how fast the CPU runs, whether heat has capped it, and how much of
      the time since boot it spent at full speed;
    - the zram swap (compressed RAM, not the eMMC): in use means RAM ran
      short at some point;
    - the Wi-Fi's own error counters since boot.

    Anything this machine doesn't have is simply left out (a PC)."""
    tree = os.path.join(base_root, "proc", "device-tree")
    soc = os.path.join(sys_root, "devices", "soc0")
    found: dict[str, Any] = {
        "model": _read_text(os.path.join(tree, "model")),
        "serial": _read_text(os.path.join(tree, "serial-number")),
        "soc": " ".join(v for v in (_read(os.path.join(soc, "family")), _read(os.path.join(soc, "machine"))) if v) or None,
    }

    thermal = os.path.join(sys_root, "class", "thermal")
    temps: dict[str, float] = {}
    throttle = []
    try:
        zones = sorted(z for z in os.listdir(thermal) if z.startswith("thermal_zone"))
    except OSError:
        zones = []
    for zone in zones:
        kind = (_read(os.path.join(thermal, zone, "type")) or "").lower()
        try:
            temp = int(_read(os.path.join(thermal, zone, "temp"))) / 1000
        except (TypeError, ValueError):
            continue
        for prefix, name in _NAMED_ZONES.items():
            if kind.startswith(prefix):
                temps[name] = round(max(temp, temps.get(name, temp)), 1)
        if "cpu" in kind:
            for trip in range(8):
                if _read(os.path.join(thermal, zone, f"trip_point_{trip}_type")) == "passive":
                    try:
                        throttle.append(int(_read(os.path.join(thermal, zone, f"trip_point_{trip}_temp"))) / 1000)
                    except (TypeError, ValueError):
                        pass
    found["temps"] = temps
    found["throttle_c"] = min(throttle) if throttle else None

    cpu = os.path.join(sys_root, "devices", "system", "cpu")
    now, top, capped = [], None, False
    at_top = total = 0
    try:
        cores = sorted(c for c in os.listdir(cpu) if re.fullmatch(r"cpu\d+", c))
    except OSError:
        cores = []
    seen_policies = set()
    for core in cores:
        freq = os.path.join(cpu, core, "cpufreq")
        try:
            now.append(int(_read(os.path.join(freq, "scaling_cur_freq"))) // 1000)
            hardware_max = int(_read(os.path.join(freq, "cpuinfo_max_freq"))) // 1000
            allowed_max = int(_read(os.path.join(freq, "scaling_max_freq"))) // 1000
        except (TypeError, ValueError):
            continue
        top = max(top or 0, hardware_max)
        capped = capped or allowed_max < hardware_max
        # One table per group of cores that share a clock (all four, here).
        policy = _read(os.path.join(freq, "related_cpus"))
        if policy in seen_policies:
            continue
        seen_policies.add(policy)
        for line in (_read(os.path.join(freq, "stats", "time_in_state")) or "").splitlines():
            try:
                khz, ticks = (int(v) for v in line.split())
            except ValueError:
                continue
            total += ticks
            at_top += ticks if khz // 1000 >= hardware_max else 0
    if now:
        found["cpu_mhz_now"] = max(now)
        found["cpu_mhz_max"] = top
        found["cpu_capped"] = capped
        found["cpu_top_share"] = round(at_top / total, 3) if total else None

    swap = _meminfo(("SwapTotal", "SwapFree"), proc_root)
    if swap.get("SwapTotal"):
        found["swap_total_bytes"] = swap["SwapTotal"]
        found["swap_used_bytes"] = swap["SwapTotal"] - swap.get("SwapFree", 0)

    net = os.path.join(sys_root, "class", "net")
    try:
        wifi = next((i for i in sorted(os.listdir(net)) if os.path.isdir(os.path.join(net, i, "wireless"))), None)
    except OSError:
        wifi = None
    if wifi:
        counters = {}
        for name in ("rx_packets", "tx_packets", "rx_errors", "tx_errors"):
            try:
                counters[name] = int(_read(os.path.join(net, wifi, "statistics", name)))
            except (TypeError, ValueError):
                pass
        if len(counters) == 4:
            found["wifi_packets"] = counters["rx_packets"] + counters["tx_packets"]
            found["wifi_errors"] = counters["rx_errors"] + counters["tx_errors"]
    return found


def _read_text(path: str) -> str | None:
    """A device-tree string: NUL-terminated, sometimes not plain ASCII."""
    try:
        with open(path, "rb") as fh:
            return fh.read().rstrip(b"\0").decode(errors="replace").strip() or None
    except OSError:
        return None


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return round(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def board_uptime_seconds() -> float | None:
    """How long the board has been up - None off Linux."""
    return _uptime_seconds()


def waiting_since_boot(age_seconds: float | None, error: str | None, uptime_seconds: float | None,
                       window: float | None = None) -> bool:
    """A device not heard from since the board came up, in its first
    hours: a battery sensor waiting for its next report - "not yet", not
    "offline". Its last value, kept across the power cut (drivers/mqtt/
    state_cache.py), is older than the boot itself - or there is none.
    Only MQTT's own silence: a device answering with an error (a light
    strip timing out at an address it no longer has) is offline, and
    says why."""
    if uptime_seconds is None or uptime_seconds >= (FRESH_BOOT_SECONDS if window is None else window):
        return False
    if error and "no MQTT message" not in error:
        return False
    return age_seconds is None or age_seconds > uptime_seconds


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
        "hardware": board_hardware(),
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


def boot_history(limit: int = 5, run=None, uptime=None) -> list[dict]:
    """The board's last boots, newest first, each with how that session
    ended: "running" (this one), "clean" (a proper shutdown or reboot) or
    "crash" (the power went, or the board hung) - from the system's own
    login records (`last`). Empty where they aren't available.

    `last` notes a boot's time at the boot, before NTP has set the clock:
    a UNO Q - no clock battery - starts from the time it was switched off,
    and a board off from 18:25 to 20:03 said it came up at 18:25 (seen on
    a test board). This boot's time comes from the kernel's uptime
    instead, like boot_times(); the earlier ones can only be as `last`
    noted them."""
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
    uptime = _uptime_seconds() if uptime is None else uptime
    for boot in boots:
        if boot["ended"] == "running" and uptime is not None:
            boot["booted_at"] = round(time.time() - uptime)
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
# The same for a mains-powered Shelly - a zone's valve: it's back on the
# broker within a minute or two of the board (the Wi-Fi watchdog's
# reconnect two minutes in included); past this, it's offline.
VALVE_FRESH_BOOT_SECONDS = 5 * 60

WIFI_WEAK_PERCENT = 30


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def emmc_written_bytes(proc_root: str = "/proc") -> int | None:
    """Bytes written to the board's eMMC since boot (/proc/diskstats)."""
    for line in (_read(os.path.join(proc_root, "diskstats")) or "").splitlines():
        fields = line.split()
        if len(fields) > 9 and re.fullmatch(r"mmcblk\d+", fields[2]):
            return int(fields[9]) * 512
    return None


class WriteRate:
    """The eMMC's writes over the last hour. The average since boot looked
    alarming right after a restart or an update - 1.3 GB/day, 38 minutes
    in, on a board writing 0.26 - so it's sampled every 5 minutes, one
    number read from /proc, and the last hour's rate is what's shown.

    The samples are kept in RAM (/dev/shm) across LocalHome's own
    restarts - an update or a settings change used to start the 10
    minutes over - and go with a reboot, like the counter they read and
    the monotonic clock they're timed by. Never on the eMMC itself."""

    WINDOW = 3600
    MIN_SPAN = 600          # less than 10 minutes of samples: not a rate yet
    KEEP_PATH = "/dev/shm/localhome-write-rate.json"

    def __init__(self, read=emmc_written_bytes, clock=time.monotonic, keep_path: str | None = KEEP_PATH):
        self.read = read
        self.clock = clock
        self.keep_path = keep_path
        self.samples: list[tuple[float, int]] = self._kept()

    def _kept(self) -> list[tuple[float, int]]:
        if not self.keep_path:
            return []
        try:
            with open(self.keep_path, encoding="utf-8") as f:
                return [(float(t), int(w)) for t, w in json.load(f)]
        except (OSError, ValueError, TypeError):
            return []

    def _keep(self) -> None:
        if not self.keep_path:
            return
        try:
            with open(self.keep_path, "w", encoding="utf-8") as f:
                json.dump(self.samples, f)
        except OSError:
            pass

    def sample(self) -> None:
        written = self.read()
        if written is None:
            return
        now = self.clock()
        if self.samples and (written < self.samples[-1][1] or now < self.samples[-1][0]):
            self.samples = []          # the counter or the clock went back: a reboot
        self.samples.append((now, written))
        self.samples = [s for s in self.samples if now - s[0] <= self.WINDOW]
        self._keep()

    def per_day(self) -> tuple[int, int] | None:
        """(bytes per day, minutes measured) over the window, or None."""
        if len(self.samples) < 2:
            return None
        (t0, w0), (t1, w1) = self.samples[0], self.samples[-1]
        if t1 - t0 < self.MIN_SPAN:
            return None
        return round((w1 - w0) / (t1 - t0) * 86400), round((t1 - t0) / 60)

    def start(self, every: float = 300) -> None:
        def loop():
            while True:
                try:
                    self.sample()
                except Exception:
                    pass
                time.sleep(every)
        threading.Thread(target=loop, daemon=True, name="emmc-write-rate").start()


def storage_health(sys_root: str = "/sys", proc_root: str = "/proc", uptime_seconds: float | None = None,
                   recent: tuple[int, int] | None = None) -> dict | None:
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
            written = emmc_written_bytes(proc_root)
            per_day, basis, minutes = None, None, None
            if recent is not None:
                (per_day, minutes), basis = recent, "recent"
            elif written is not None and uptime_seconds:
                per_day, basis = round(written / uptime_seconds * 86400), "since_boot"
            return {
                "name": _read(os.path.join(base, "name")),
                # 0x01 = 0-10% used ... 0x0A = 90-100%, 0x0B = past its rated life
                "wear_percent_max": min(worst, 11) * 10 if worst else None,
                "pre_eol": {1: "normal", 2: "warning", 3: "urgent"}.get(pre_eol, "unknown"),
                "written_since_boot_bytes": written,
                "written_per_day_bytes": per_day,
                # "recent": over the last `per_day_minutes` (up to an hour);
                # "since_boot": the first minutes after a boot, start-up included.
                "written_per_day_basis": basis,
                "per_day_minutes": minutes,
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
    needs: reachable or not, why not, how old its data is - and whether
    it's a simulated stand-in: one left simulated looked like a real
    device answering in the panel's list (seen: two valves, their Shellys
    connected, still simulated)."""
    devices = []
    driver_types = getattr(manager, "driver_types", {}) or {}
    for name, poller in manager.pollers.items():
        reading = poller.get()
        devices.append(
            {
                "name": name,
                "kind": manager.sensor_kinds.get(name, "unknown"),
                "ok": bool(reading.get("ok")),
                "error": None if reading.get("ok") else reading.get("error"),
                "seconds_since_update": reading.get("seconds_since_update"),
                "simulated": driver_types.get(name) == "simulated",
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


# What deploy/tune-board.sh sets up for running unattended on Wi-Fi, all
# year - each one a file it writes, so checking is a stat, not a command.
WIFI_CONF = "/etc/NetworkManager/conf.d/99-localhome-wifi.conf"
WIFI_WATCHDOG_ENABLED = "/etc/systemd/system/timers.target.wants/localhome-wifi-watchdog.timer"
WIFI_WATCHDOG_LOG = "/var/lib/localhome/wifi-watchdog.log"
# Written by systemd's stop hook when the watchdog restarted a hung LocalHome
# (deploy/watchdog/localhome-service-stopped.sh) - it couldn't say so itself.
SERVICE_WATCHDOG_LOG = "/var/lib/localhome/service-watchdog.log"
HARDWARE_WATCHDOG_CONF = "/etc/systemd/system.conf.d/localhome-watchdog.conf"
HARDWARE_WATCHDOG_DEVICE = "/dev/watchdog"
NETWORKMANAGER_DIR = "/etc/NetworkManager"
POWER_CONF = "/etc/tmpfiles.d/localhome-power.conf"
SLEEP_TARGET = "/etc/systemd/system/sleep.target"        # masked: a link to /dev/null
RESTART_DROPINS = [f"/etc/systemd/system/{unit}.service.d/localhome-restart.conf"
                   for unit in ("mosquitto", "avahi-daemon", "NetworkManager")]
# The cgroup systemd runs LocalHome in: its memory.max is the MemoryMax set
# by tune-board.sh ("max" when there's none).
CGROUP_ROOT = "/sys/fs/cgroup"
OWN_CGROUP = "/proc/self/cgroup"


def memory_limit_bytes(own_cgroup: str | None = None, root: str | None = None) -> int | None:
    """This process's memory limit, in bytes - 0 when there's none, None
    where it can't be told (no cgroup v2)."""
    own_cgroup, root = own_cgroup or OWN_CGROUP, root or CGROUP_ROOT
    try:
        with open(own_cgroup, encoding="ascii") as f:
            path = next((line.strip().split("::", 1)[1] for line in f if line.startswith("0::")), None)
        if path is None:
            return None
        with open(os.path.join(root, path.lstrip("/"), "memory.max"), encoding="ascii") as f:
            value = f.read().strip()
    except (OSError, IndexError):
        return None
    return 0 if value == "max" else int(value) if value.isdigit() else None


def board_protections(env=os.environ) -> dict | None:
    """Which of tune-board.sh's protections are on - {"items": {key: True /
    False / None (doesn't apply here)}, "events": the Wi-Fi watchdog's last
    actions}. None where none of it applies: no NetworkManager (a PC,
    Docker)."""
    if not os.path.isdir(NETWORKMANAGER_DIR):
        return None
    items = {
        "wifi_always_on": os.path.exists(WIFI_CONF),
        "wifi_watchdog": os.path.exists(WIFI_WATCHDOG_ENABLED),
        "hardware_watchdog": os.path.exists(HARDWARE_WATCHDOG_CONF) if os.path.exists(HARDWARE_WATCHDOG_DEVICE) else None,
        # systemd sets it for a service with WatchdogSec - see service_watchdog.py.
        "service_watchdog": bool(env.get("WATCHDOG_USEC")),
        "power_saving_off": os.path.exists(POWER_CONF) and os.path.realpath(SLEEP_TARGET) == "/dev/null",
        "services_restart": all(os.path.exists(path) for path in RESTART_DROPINS),
        # Only for LocalHome run by systemd: by hand, there's no service to limit.
        "memory_limit": (memory_limit_bytes() or 0) > 0 if env.get("INVOCATION_ID") else None,
    }
    return {"items": items}


# The watchdogs' event logs - see util/eventlog.py. The names stay here,
# where the watchdogs and the tests have always found them.
EVENT_LOG_KEEP, append_event, read_events = eventlog.EVENT_LOG_KEEP, eventlog.append_event, eventlog.read_events
# Written to LocalHome's log by a Shelly relay's driver, not a watchdog:
# its own safety timer put it back at rest (drivers/mqtt/switch.py).
SHELLY_EVENT_CODES = {"safety_timer"}


# Over now, but worth knowing for a while: a Shelly's safety timer that
# acted means the board, LocalHome or the network stopped for 15 minutes.
# Not a problem - nothing to fix by then - so not in the red count.
RECENT_SECONDS = 72 * 3600


def recent_events(localhome_log: str, now: float | None = None) -> list[dict]:
    """The safety timers that acted in the last 72 hours, newest first -
    every one, from the log the drivers write them to (each at the time
    it happened - drivers/mqtt/switch.py)."""
    now = time.time() if now is None else now
    found = []
    for event in read_events(localhome_log, "shelly"):
        if event["code"] in SHELLY_EVENT_CODES:
            at = eventlog.event_time(event)
            if at is not None and 0 <= now - at < RECENT_SECONDS:
                found.append({"code": event["code"], "subject": event["values"], "at": at})
    return sorted(found, key=lambda e: e["at"], reverse=True)


def watchdog_history(localhome_log: str, wifi_log: str = WIFI_WATCHDOG_LOG,
                     service_log: str = SERVICE_WATCHDOG_LOG, limit: int = 10) -> list[dict]:
    """Both watchdogs' actions, and the Shelly relays' safety timers,
    newest first - the last `limit`, what the admin page lists. The
    diagnostics zip carries the whole logs."""
    events = (read_events(localhome_log, "localhome") + read_events(service_log, "localhome")
              + read_events(wifi_log, "wifi"))
    for event in events:
        if event["code"] in SHELLY_EVENT_CODES:
            event["source"] = "shelly"
    return sorted(events, key=lambda e: e["when"], reverse=True)[:limit]


# ------------------------------------ can the devices reach the board --
# Seen on the production board: the router passing pings but not
# connections to the board, while the board itself was on the Wi-Fi and
# fine - nothing in its own view of the network was wrong. What was: the
# Shellys had stopped reaching its MQTT broker.
MQTT_PORT = 1883
LAN_SILENT_SECONDS = 600          # no mains Shelly connected for 10 min, when some were
BATTERY_SILENT_SECONDS = 8100     # every battery sensor past its 2 h report, and then some


def lan_mqtt_clients(port: int = MQTT_PORT, paths=("/proc/net/tcp", "/proc/net/tcp6")) -> int | None:
    """Devices on the LAN connected to this board's MQTT broker right now:
    established connections to `port` from anything but the board itself.
    From /proc - no command run. None where there's no /proc (not Linux)."""
    peers = lan_mqtt_peers(port, paths)
    return None if peers is None else len(peers)


def lan_mqtt_peers(port: int = MQTT_PORT, paths=("/proc/net/tcp", "/proc/net/tcp6")) -> set[str] | None:
    """Each of those connections by its far end - address and port, as
    /proc writes them: the same one later is the same connection still."""
    peers = None
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        peers = peers if peers is not None else set()
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "01":        # 01: ESTABLISHED
                continue
            local, remote = parts[1], parts[2]
            if int(local.rsplit(":", 1)[1], 16) != port:
                continue
            host = remote.rsplit(":", 1)[0]
            # 127.x (also as ::ffff:127.x) and ::1: the board talking to itself.
            if host.endswith("7F") or host == "00000000000000000000000001000000":
                continue
            peers.add(remote)
    return peers


# A connection this old is a device that stays connected: a mains Shelly,
# an H&T on USB-C. A battery sensor's lasts seconds - it wakes, reports
# and goes back to sleep.
STAY_SECONDS = 60


class LanWatch:
    """How long every device that stays connected to the broker has been
    gone - having been there. Only a connection that lasts counts: a
    battery sensor caught while it reported made a working network look
    broken ten minutes later, when it was asleep again (seen in a board's
    watchdog log, every 2 hours) - and an installation whose sensors all
    sleep never keeps one connected, which isn't a fault either. An H&T
    on USB-C, connected all the time, counts like a mains Shelly."""

    def __init__(self):
        self.seen = False
        self.silent_since = None
        self.first_seen: dict[str, float] = {}     # each connection there now, since when

    def update(self, peers: set[str] | None, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        if peers is None:
            return None
        self.first_seen = {peer: self.first_seen.get(peer, now) for peer in peers}
        if any(now - since >= STAY_SECONDS for since in self.first_seen.values()):
            self.seen, self.silent_since = True, None
            return None
        if not self.seen:
            return None
        if self.silent_since is None:
            self.silent_since = now
        return now - self.silent_since


def battery_sensors_silent(devices: list[dict], battery_names: set[str], uptime_seconds: float | None,
                           listening_seconds: float | None = None, mains_names: set[str] = frozenset()) -> bool:
    """Every battery sensor past its 2-hour report, by a margin - they all
    report on their own, so all of them silent at once is the network,
    not the sensors. Not in the first 2 h 15 min after a boot, nor after
    LocalHome itself started (`listening_seconds`, its age by default): a
    report that came while it was stopped never reached it - their
    reports aren't kept by the broker. Seen on the production board after
    a 16-minute stop: both reports missed, "RT" on a network that was
    fine. Nor while a mains Shelly (`mains_names`) answers through the
    same network: then it's the sensors - seen with both renamed in the
    Shelly app, the VMC answering, and "restart the router"."""
    sensors = [d for d in devices if d["name"] in battery_names]
    listening = process_age_seconds() if listening_seconds is None else listening_seconds
    if not sensors or listening < BATTERY_SILENT_SECONDS or (
            uptime_seconds is not None and uptime_seconds < BATTERY_SILENT_SECONDS):
        return False
    if any(d["ok"] for d in devices if d["name"] in mains_names):
        return False
    return all(d.get("seconds_since_update") is None or d["seconds_since_update"] > BATTERY_SILENT_SECONDS
               for d in sensors)


def loop_health(name: str, last_ok_ts: float | None, interval_seconds: float, now: float | None = None) -> dict:
    """Whether a background loop is still completing its cycles. Stalled
    after three missed intervals - but not during the first ones after
    startup, when a loop that hasn't finished its first cycle yet is
    normal. `last_ok_ts` and `now` on the monotonic clock, which the
    board's clock being set (hours at once, after a power cut) can't move."""
    now = time.monotonic() if now is None else now
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
    protections: dict | None = None, lan_silent_seconds: float | None = None, battery_silent: bool = False,
    shelly: list[dict] = (),
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
    if (lan_silent_seconds or 0) >= LAN_SILENT_SECONDS or battery_silent:
        minutes = int(lan_silent_seconds // 60) if lan_silent_seconds else None
        found.append({"code": "lan_unreachable", "subject": f"{minutes} min" if minutes else "2 h",
                      "detail": "the router answers the board, but the devices' connections don't reach it"})
    for loop in loops:
        if loop["stalled"]:
            found.append({"code": "loop_stalled", "subject": loop["name"], "detail": None})
    if disk.get("low"):
        found.append({"code": "disk_low", "subject": disk["path"], "detail": None})
    if host and host.get("clock_synced") is False:
        found.append({"code": "clock_unsynced", "subject": "NTP", "detail": None})
    for key, on in ((protections or {}).get("items") or {}).items():
        if on is False:
            found.append({"code": "protection_missing", "subject": key, "detail": "sudo sh deploy/tune-board.sh"})
    wifi = (network or {}).get("wifi") or {}
    if wifi.get("signal_percent") is not None and wifi["signal_percent"] < WIFI_WEAK_PERCENT:
        found.append({"code": "wifi_weak", "subject": wifi.get("ssid") or "Wi-Fi", "detail": f"{wifi['signal_percent']}%"})
    if cpu_temp_c is not None and cpu_temp_c >= CPU_HOT_C:
        found.append({"code": "cpu_hot", "subject": f"{cpu_temp_c:.0f}°C", "detail": None})
    hardware = (host or {}).get("hardware") or {}
    if hardware.get("cpu_capped"):
        # The kernel has lowered the CPU's top speed to cool it down.
        found.append({"code": "cpu_throttled", "subject": f"{hardware.get('cpu_mhz_max')} MHz", "detail": None})
    if storage and (storage["pre_eol"] in ("warning", "urgent") or (storage["wear_percent_max"] or 0) >= 80):
        found.append({"code": "storage_wear", "subject": storage.get("name") or "eMMC",
                      "detail": f"{storage['wear_percent_max']}% - {storage['pre_eol']}"})
    # A board that came up minutes ago hasn't heard from its battery
    # sensors yet - they speak every couple of hours. That's "not yet",
    # not "offline", and calling it offline right after every power cut
    # is how a problems list gets ignored. Their last value, kept over
    # the power cut, is from before the boot: that's still "not yet".
    uptime = (host or {}).get("uptime_seconds")
    for device in devices:
        if not device["ok"]:
            waiting = waiting_since_boot(device.get("seconds_since_update"), device.get("error"), uptime)
            code = "device_not_heard_yet" if waiting else "device_offline"
            found.append({"code": code, "subject": device["name"], "detail": device["error"]})
    # What the Shellys say about themselves: too hot, a weak signal, battery
    # reports that didn't get through - see services/shelly_health.py.
    found += shelly
    return found
