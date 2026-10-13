"""Months of a board in minutes: LocalHome built as a board builds it - the
factory climate (Stanze and Bagni, the bypass, the dehumidifier, the
outdoor sensor, the VMC), every device simulated - with its clock run
fast. Each step of simulated time, every loop's cycle is called in turn
(the devices read, the history recorded, the thermostat, the interlocks,
the VMC, the dehumidifier), two pages ask what a PC and a phone ask, and
a resident has their days: 22° for two hours in the evening, a zone by
hand until changed back, the week edited on Sundays, a weekend away, the
VMC boosted, a few summer days with the dehumidifier.

What has to hold after months, as a list of problems (none: exit 0):
nothing in memory, in the threads, on disk or in the history grows
without end; a day costs the board the same at the end as at the start,
an answer as long; each day has its summary; no error in the log.

    python tools/accelerated_months.py [--days 90] [--step 60] [--start 2026-10-15]
        [--retention 30] [--clients 2] [--ask-every 300] [--no-trace] [--out DIR]

On a board, from its checkout (a process and a folder of its own - it
never touches the board's LocalHome, its files or its devices):

    .venv/bin/python tools/accelerated_months.py --days 90 --no-trace --out /tmp/months

--no-trace: the process's memory (RSS) measured instead of Python's own
allocations, traced - which slows every one of them about four times.

tests/test_long_run_months.py runs a shorter one in the unit suite.
"""
from __future__ import annotations

import argparse
import gc
import inspect
import json
import logging
import random
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))     # a checkout, installed or not

STANZE, BAGNI = "Stanze", "Bagni"

CONFIG = """\
web:
  host: "127.0.0.1"
  port: 5999
  language: "it"

integrations:
  - kind: switch
    type: simulated
    name: "Dehumidifier"
    dashboard_tab: climate
    visible_in_mode: "cool"
  - kind: switch
    type: simulated
    name: "Bathroom Bypass Valve"
    dashboard_tab: climate
    visible_in_mode: "cool"

thermostat:
  mode: "heat"
  hysteresis_c: 0.5
  schedules_file: "schedules.json"
  poll_interval_seconds: 60
  mode_switches:
    - switch: "Bathroom Bypass Valve"
      active_in_mode: "cool"
  mode_limited_switches:
    - switch: "Dehumidifier"
      only_in_mode: "cool"

history:
  db_file: "data/history.db"
  poll_interval_seconds: {step}
  retention_days: {retention}
"""

CLIMATE_MAP = {
    "zones": [
        {"name": STANZE, "sensor_mode": "simulated", "sensor_device_id": None,
         "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
        {"name": BAGNI, "sensor_mode": "simulated", "sensor_device_id": None,
         "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
    ],
    "extra_sensors": {"outdoor_sensor_mode": "simulated", "outdoor_sensor_device_id": None},
    "extra_dimmers": {"vmc_mode": "simulated", "vmc_device_id": None},
}


def _week(mode: str) -> dict:
    """A family's week: up early on weekdays, out during the day, home in
    the evening; the weekend at home."""
    comfort, economy, night = (21, 19, 18) if mode == "heat" else (25, 27, None)
    week = {}
    for i, day in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
        hours = [night] * 24
        if i < 5:
            hours[6:8] = [comfort] * 2
            hours[8:17] = [economy] * 9
            hours[17:23] = [comfort] * 6
        else:
            hours[8:23] = [comfort] * 15
        week[day] = hours
    return week


# --------------------------------------------------------------- the clock --
# time.time()/monotonic()/sleep() and every localhome module's datetime and
# date answer from this one, so each loop's own arithmetic - a 30-second
# flush, a daily prune, "until 18:00" - runs on simulated time.

class FastClock:
    def __init__(self, start: float):
        self.start = self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _patch_clock(clock: FastClock) -> callable:
    real = {name: getattr(time, name) for name in ("time", "time_ns", "monotonic", "sleep", "localtime",
                                                   "gmtime", "strftime", "ctime")}
    mono0 = real["monotonic"]()

    def localtime(*secs):
        return real["localtime"](secs[0] if secs and secs[0] is not None else clock.now)

    def gmtime(*secs):
        return real["gmtime"](secs[0] if secs and secs[0] is not None else clock.now)

    replace = {
        "time": lambda: clock.now,
        "time_ns": lambda: int(clock.now * 1e9),
        "monotonic": lambda: mono0 + (clock.now - clock.start),
        "sleep": lambda seconds: clock.advance(max(0.0, seconds)),
        "localtime": localtime,
        "gmtime": gmtime,
        "strftime": lambda fmt, *t: real["strftime"](fmt, t[0] if t else localtime()),
        "ctime": lambda *secs: real["ctime"](secs[0] if secs and secs[0] is not None else clock.now),
    }
    for name, fn in replace.items():
        setattr(time, name, fn)

    def restore():
        for name, fn in real.items():
            setattr(time, name, fn)
    return restore


def _fast_types(clock: FastClock):
    class FastDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now, tz)

        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now)

    class FastDate(date):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now)
    return FastDatetime, FastDate


def _patch_modules(fast_datetime, fast_date, patched: dict) -> None:
    """Every localhome module's `datetime`/`date` - again after each import
    a lazy one makes."""
    for name, module in list(sys.modules.items()):
        if not name.startswith("localhome") or module is None:
            continue
        for attr, real, fast in (("datetime", datetime, fast_datetime), ("date", date, fast_date)):
            if getattr(module, attr, None) is real:
                setattr(module, attr, fast)
                patched[(name, attr)] = (module, real)


# ----------------------------------------------------------------- loops --

def _no_threads() -> callable:
    """Each loop's start() does nothing: this drives their cycles itself."""
    from localhome.core.manager import DeviceManager
    from localhome.services.dehumidifier import DehumidifierTimer
    from localhome.services.history import HistoryRecorder
    from localhome.services.interlock import InterlockController
    from localhome.services.led_matrix import LedMatrixService
    from localhome.services.shelly_health import ShellyHealth
    from localhome.services.system_status import WriteRate
    from localhome.services.thermostat import ThermostatController
    from localhome.services.vmc_schedule import VmcScheduleController

    classes = (DeviceManager, HistoryRecorder, ThermostatController, InterlockController, VmcScheduleController,
               DehumidifierTimer, ShellyHealth, WriteRate, LedMatrixService)
    saved = {cls: cls.__dict__["start"] for cls in classes}
    for cls in classes:
        cls.start = lambda self, *args, **kwargs: None

    def restore():
        for cls, start in saved.items():
            cls.start = start
    return restore


class Board:
    """The app and its loops, one cycle at a time."""

    def __init__(self, app):
        self.app = app
        c = app.config
        self.manager, self.recorder = c["MANAGER"], c["RECORDER"]
        self.thermostat, self.interlock = c["THERMOSTAT"], c["INTERLOCK"]
        self.vmc, self.dehumidifier = c["VMC_SCHEDULE"], c["DEHUMIDIFIER"]
        self.recorder.clock_ready = lambda: True        # a PC can't tell: the board's is synced
        self.pollers = [p for p in self.manager.pollers.values() if not inspect.iscoroutinefunction(p.poll_once)]
        self.client = app.test_client()

    def cycle(self) -> None:
        for poller in self.pollers:
            poller.poll_once()
        self.recorder.poll_once()
        if self.thermostat:
            self.thermostat.tick()
        if self.interlock:
            self.interlock.tick()
        self.vmc.tick()
        if self.dehumidifier:
            self.dehumidifier.tick()


# --------------------------------------------------------------- the run --

class Run:
    def __init__(self, home: Path, days: int, step: int, start: datetime, retention: int, clients: int,
                 ask_every: int, trace: bool = True, seed: int = 7):
        self.home, self.days, self.step, self.retention, self.trace = home, days, step, retention, trace
        self.clients, self.ask_every = clients, ask_every
        self.start = start
        self.rng = random.Random(seed)
        random.seed(seed)                           # the simulated sensors' wander
        # Each answer's time, today's - summed up at the end of each day:
        # months of them, kept one by one, were this run's own growth.
        self.today_answers: dict[str, list] = {}         # path -> [[seconds, ...], failed]
        self.answer_days: list[tuple[int, dict]] = []    # (day, {path: (count, failed, median, p95)})
        self.refused: list[str] = []
        self.daily: list[dict] = []
        self.logged = Counter()
        self.errors: list[str] = []
        self.vmc_device = None

    # -- what the pages ask ----------------------------------------------

    def ask(self, day: int, path: str, body: dict | None = None, expect_ok: bool = True) -> dict | None:
        t0 = time.perf_counter()
        response = (self.board.client.post(path, json=body) if body is not None else self.board.client.get(path))
        seconds = time.perf_counter() - t0
        if body is None:
            entry = self.today_answers.setdefault(path.split("?")[0], [[], 0])
            entry[0].append(seconds)
            entry[1] += response.status_code >= 400
        elif expect_ok and response.status_code >= 400:
            self.refused.append(f"day {day} POST {path} {body} -> {response.status_code} {response.get_data(as_text=True)[:200]}")
        try:
            return response.get_json(silent=True)
        except Exception:
            return None

    def refresh(self, day: int) -> None:
        """A page's refresh - the home's, every 20 s on a phone or a PC."""
        for _ in range(self.clients):
            self.ask(day, "/api/server-time")
            self.ask(day, "/api/climate/zones")
            self.ask(day, "/api/readings")
            self.ask(day, "/api/problems")
            self.ask(day, "/api/vmc-schedule")
            self.ask(day, "/api/climate/run-hours")
            self.ask(day, "/api/climate/temps")
            for zone, sensor, valve in self.zones:
                mode = "heat" if self.mode == "heat" else "cool"
                self.ask(day, f"/api/climate/zones/{zone}/schedule?mode={mode}")
                self.ask(day, f"/api/sensors/{sensor}/history?field=temperature_c&range=24h")
                self.ask(day, f"/api/sensors/{valve}/history?field=is_on&range=24h")
                self.ask(day, f"/api/climate/zones/{zone}/on-hours")

    def admin(self, day: int) -> None:
        """The installer's panel, now and then."""
        for path in ("/api/system-status", "/api/climate-admin/settings", "/api/climate-admin/map",
                     "/api/devices", "/api/dehumidifier"):
            self.ask(day, path)

    # -- a resident's day ------------------------------------------------

    def plans(self, day: int, today: datetime) -> list[tuple[datetime, callable]]:
        """What happens on `day`, at what time."""
        at = lambda h, m=0: today.replace(hour=h, minute=m)
        plans = []
        weekday = today.weekday()
        away = day % 21 == 18 and weekday < 5                  # a weekend away now and then
        if day % 2 == 0:
            plans.append((at(18, 5), lambda: self.ask(day, f"/api/climate/zones/{STANZE}/override",
                                                       {"target": 22 if self.mode == "heat" else 24, "hours": 2},
                                                       expect_ok=not self.away)))
        if day % 3 == 1:
            plans.append((at(21, 10), lambda: self.ask(day, f"/api/climate/zones/{BAGNI}/override",
                                                       {"target": 23 if self.mode == "heat" else None,
                                                        "until": "manual"},
                                                       expect_ok=not self.away and self.mode == "heat")))
            plans.append((at(23, 40), lambda: self.ask(day, f"/api/climate/zones/{BAGNI}/override/cancel", {},
                                                       expect_ok=False)))
        if weekday == 6:
            def edit_week():
                week = _week(self.mode)
                week["sat"][7] = week["sat"][8]                  # up an hour earlier on Saturdays
                for zone, _, _ in self.zones:
                    self.ask(day, f"/api/climate/zones/{zone}/schedule", {"week": week, "mode": self.mode})
            plans.append((at(10, 30), edit_week))
        if away:
            plans.append((at(17, 50), lambda: self.go_away(day)))
        if self.vmc_device and not self.away:
            plans.append((at(20, 0), lambda: self.ask(day, "/api/vmc-schedule/boost",
                                                      {"device": self.vmc_device, "value": 100, "minutes": 30},
                                                      expect_ok=not self.away)))
        if day % 30 == 15:
            plans.append((at(11, 0), lambda: self.ask(day, "/api/vmc-filters/cleaned", {})))
        # A few summer days in the middle: the season, the dehumidifier.
        summer = self.days // 2
        # The simulated rooms stay near 50%: the installer's thresholds
        # lowered for these days, so it runs - by hand and by itself.
        dehumidifier = self.board.dehumidifier is not None
        if day == summer:
            plans.append((at(9, 0), lambda: self.set_mode(day, "cool")))
            if dehumidifier:
                plans.append((at(9, 5), lambda: self.ask(day, "/api/dehumidifier/settings",
                                                         {"start_from": 40, "stop_below": 35, "auto": True})))
        if summer <= day < summer + 3 and dehumidifier:
            plans.append((at(15, 0), lambda: self.ask(day, "/api/dehumidifier/on", {"hours": 2}, expect_ok=False)))
        if day == summer + 3:
            plans.append((at(9, 0), lambda: self.set_mode(day, "heat")))
            if dehumidifier:
                plans.append((at(9, 5), lambda: self.ask(day, "/api/dehumidifier/settings", {"reset": True})))
        if day % 7 == 3:
            plans.append((at(12, 0), lambda: self.admin(day)))
        return sorted(plans, key=lambda p: p[0])

    def go_away(self, day: int) -> None:
        self.ask(day, "/api/climate/away", {"hours": 48})
        self.away = True
        self.back_at = self.clock.now + 48 * 3600

    def set_mode(self, day: int, mode: str) -> None:
        self.ask(day, "/api/climate/mode", {"mode": mode})
        self.mode = mode
        for zone, _, _ in self.zones:
            self.ask(day, f"/api/climate/zones/{zone}/schedule", {"week": _week(mode), "mode": mode})

    # -- measuring -------------------------------------------------------

    def measure(self, day: int, today: datetime, real_seconds: float) -> dict:
        db = self.home / "data" / "history.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            oldest = conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
            summaries = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("daily_runs", "daily_temps", "daily_energy")}
        finally:
            conn.close()
        files = {str(p.relative_to(self.home)): p.stat().st_size for p in self.home.rglob("*") if p.is_file()}
        gc.collect()                                # what's alive, not what waits to be collected
        return {"day": day, "date": today.strftime("%Y-%m-%d"),
                "real_seconds": round(real_seconds, 2), "threads": threading.active_count(),
                "rss_mb": _rss_mb(), "traced_kb": round(tracemalloc.get_traced_memory()[0] / 1024)
                if tracemalloc.is_tracing() else None,
                "objects": len(gc.get_objects()), "rows": rows, "oldest": oldest, **summaries,
                "db_kb": round(sum(v for k, v in files.items() if k.startswith("data")) / 1024), "files": files}

    # -- the whole of it -------------------------------------------------

    def run(self) -> dict:
        (self.home / "data").mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text(CONFIG.format(step=self.step, retention=self.retention), encoding="utf-8")
        (self.home / "climate_map.json").write_text(json.dumps(CLIMATE_MAP), encoding="utf-8")
        self.clock = FastClock(self.start.timestamp())
        fast_datetime, fast_date = _fast_types(self.clock)
        patched: dict = {}
        handler = _Counter(self)
        root = logging.getLogger()
        threads_before = threading.active_count()
        restore_clock = _patch_clock(self.clock)
        restore_threads = None
        try:
            import localhome.web.app as app_module
            restore_threads = _no_threads()
            _patch_modules(fast_datetime, fast_date, patched)
            root.addHandler(handler)
            app = app_module.create_app(str(self.home / "config.yaml"))
            _patch_modules(fast_datetime, fast_date, patched)
            self.board = Board(app)
            threads_after_start = threading.active_count()
            thermostat = self.board.thermostat
            self.zones = [(z.name, z.sensor, z.valve) for z in thermostat.zones] if thermostat else []
            self.mode, self.away, self.back_at = "heat", False, None
            vmc_device = self.board.vmc.device() if self.board.vmc.device else None
            self.vmc_device = vmc_device
            for zone, _, _ in self.zones:
                self.ask(0, f"/api/climate/zones/{zone}/schedule", {"week": _week("heat"), "mode": "heat"})
            if vmc_device:
                self.ask(0, "/api/vmc-schedule", {"enabled": True, "target": vmc_device,
                                                  "day": {"at": "07:00", "value": 50},
                                                  "night": {"at": "22:00", "value": 20}})
            watch_from = min(self.retention + 2, self.days // 2)
            snapshot_from = None
            for day in range(self.days):
                t0 = time.perf_counter()
                today = datetime.fromtimestamp(self.clock.now).replace(hour=0, minute=0, second=0, microsecond=0)
                tomorrow = (today + timedelta(days=1)).timestamp()
                plans = self.plans(day, today)
                next_ask = self.clock.now
                while self.clock.now < tomorrow:
                    self.clock.advance(self.step)
                    self.board.cycle()
                    while plans and plans[0][0].timestamp() <= self.clock.now:
                        plans.pop(0)[1]()
                    if self.away and self.clock.now >= self.back_at:
                        self.away = False
                    if self.clock.now >= next_ask:
                        self.refresh(day)
                        next_ask = self.clock.now + self.ask_every
                _patch_modules(fast_datetime, fast_date, patched)
                if day == watch_from and self.trace:
                    gc.collect()
                    tracemalloc.start(TRACE_FRAMES)
                if day == watch_from + 1 and self.trace:
                    gc.collect()
                    snapshot_from = tracemalloc.take_snapshot()
                self.answer_days.append((day, {path: _day_stats(*entry) for path, entry in self.today_answers.items()}))
                self.today_answers = {}
                self.daily.append(self.measure(day, today, time.perf_counter() - t0))
            gc.collect()
            snapshot_end = tracemalloc.take_snapshot() if tracemalloc.is_tracing() else None
            growth = []
            if snapshot_from and snapshot_end:
                # LocalHome's own: not what this run keeps of its answers.
                mine = [tracemalloc.Filter(False, __file__), tracemalloc.Filter(False, tracemalloc.__file__)]
                stats = snapshot_end.filter_traces(mine).compare_to(snapshot_from.filter_traces(mine), "traceback")
                growth = [(s.size_diff, s.count_diff, "\n".join(s.traceback.format()[-6:])) for s in stats[:12]]
                growth_total = sum(s.size_diff for s in stats)
            else:
                growth_total = None
            threads_end = threading.active_count()
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
            if getattr(self, "board", None) is not None:
                self.board.app.config["HISTORY"].close()
            root.removeHandler(handler)
            for (_, attr), (module, real) in patched.items():
                setattr(module, attr, real)
            if restore_threads:
                restore_threads()
            restore_clock()
        report = {"days": self.days, "step": self.step, "retention": self.retention, "watch_from": watch_from,
                  "traced": self.trace,
                  "threads": {"before": threads_before, "after_start": threads_after_start, "end": threads_end},
                  "daily": self.daily, "growth_total": growth_total, "growth": growth,
                  "logged": [[*k, n] for k, n in self.logged.most_common(20)], "errors": self.errors,
                  "refused": self.refused}
        report["answers"] = _answer_summary(self.answer_days, self.days, watch_from)
        report["problems"] = problems(report)
        return report


def _rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except Exception:
        try:
            with open("/proc/self/status", encoding="ascii") as f:
                kb = next(int(line.split()[1]) for line in f if line.startswith("VmRSS:"))
            return round(kb / 1000, 1)
        except Exception:
            return None


class _Counter(logging.Handler):
    """Every warning and error the run logs, counted by what it says."""

    def __init__(self, run: Run):
        super().__init__(logging.WARNING)
        self.run = run

    def emit(self, record):
        self.run.logged[(record.levelname, record.name, str(record.msg)[:120])] += 1
        if record.levelno >= logging.ERROR or record.exc_info:
            if len(self.run.errors) < 30:
                self.run.errors.append(f"{record.levelname} {record.name}: {record.getMessage()[:300]}"
                                       + (f"\n{logging.Formatter().formatException(record.exc_info)[-1500:]}"
                                          if record.exc_info else ""))


# ----------------------------------------------------------- the verdict --

# Python's allocations are traced from the watched days on, each with the
# calls that made it - few: tracing slows every allocation.
TRACE_FRAMES = 3

# What "without end" means here: per simulated day, past the first days
# (the history full, every summary in place).
MEMORY_GROWTH_PER_DAY = 32 * 1024        # Python's own, traced
RSS_GROWTH_PER_DAY = 0.5                 # MB, the process's, untraced: the allocator's own ups and downs on top
FILE_GROWTH_PER_DAY = 2 * 1024           # any file but the history
COST_GROWTH = 1.5                        # a day's real time, the last days against the first watched
ANSWER_GROWTH = 2.0                      # an answer's median time, likewise


def _day_stats(seconds: list[float], failed: int) -> tuple:
    s = sorted(seconds)
    return len(s), failed, s[len(s) // 2], s[int(len(s) * 0.95)]


def _answer_summary(answer_days, days, watch_from) -> dict:
    """{path: {"first": s, "last": s, "p95": s, "count", "bad"}}: a day's
    median, typical of the watched days' first and last thirds; the worst
    day's 95th percentile."""
    # From the day after tracing began: the same pace throughout.
    span = max(1, (days - watch_from - 1) // 3)
    first, last = range(watch_from + 1, watch_from + 1 + span), range(days - span, days)
    out: dict = {}
    for day, paths in answer_days:
        for path, (count, failed, median, p95) in paths.items():
            entry = out.setdefault(path, {"first": [], "last": [], "count": 0, "bad": 0, "p95": 0.0})
            entry["count"] += count
            entry["bad"] += failed
            entry["p95"] = max(entry["p95"], p95)
            if day in first:
                entry["first"].append(median)
            if day in last:
                entry["last"].append(median)
    median = lambda v: sorted(v)[len(v) // 2] if v else None
    return {path: {"first": median(e["first"]), "last": median(e["last"]), "count": e["count"], "bad": e["bad"],
                   "p95": e["p95"]} for path, e in sorted(out.items())}


def problems(report: dict) -> list[str]:
    found = []
    daily, w = report["daily"], report["watch_from"]
    watched = [d for d in daily if d["day"] > w]
    t = report["threads"]
    if t["end"] > t["after_start"]:
        found.append(f"threads: {t['after_start']} after the start, {t['end']} at the end")
    if report["growth_total"] is not None and len(watched) > 1:
        per_day = report["growth_total"] / (len(watched) - 1)
        if per_day > MEMORY_GROWTH_PER_DAY:
            found.append(f"memory: +{per_day / 1024:.0f} KB a day after day {w + 1} "
                         f"(at most {MEMORY_GROWTH_PER_DAY // 1024}) - see 'growth'")
    if len(watched) > 2:
        first, last = watched[0], watched[-1]
        span = last["day"] - first["day"]
        for name, size in last["files"].items():
            if name.startswith("data"):
                continue
            before = first["files"].get(name, 0)
            if (size - before) / span > FILE_GROWTH_PER_DAY:
                found.append(f"file {name}: {before} -> {size} bytes in {span} days")
        third = max(1, len(watched) // 3)
        if not report["traced"] and all(d["rss_mb"] for d in watched):
            early_rss = sum(d["rss_mb"] for d in watched[:third]) / third
            late_rss = sum(d["rss_mb"] for d in watched[-third:]) / third
            per_day = (late_rss - early_rss) / max(1, len(watched) - third)
            if per_day > RSS_GROWTH_PER_DAY:
                found.append(f"memory: the process {early_rss:.0f} -> {late_rss:.0f} MB, "
                             f"+{per_day:.2f} MB a day (at most {RSS_GROWTH_PER_DAY})")
        if last["rows"] > first["rows"] * 1.25:
            found.append(f"history: {first['rows']} -> {last['rows']} rows with {report['retention']} days kept")
        early = sum(d["real_seconds"] for d in watched[:third]) / third
        late = sum(d["real_seconds"] for d in watched[-third:]) / third
        if late > early * COST_GROWTH and late - early > 0.2:
            found.append(f"a day's cost: {early:.2f} s at first, {late:.2f} s at the end")
    if daily:
        last = daily[-1]
        kept_days = (datetime.strptime(last["date"], "%Y-%m-%d") - datetime.fromtimestamp(last["oldest"])).days \
            if last["oldest"] else 0
        if kept_days > report["retention"] + 1:
            found.append(f"history: {kept_days} days of samples kept, {report['retention']} asked")
        if last["daily_runs"] == 0 or last["daily_temps"] == 0:
            found.append(f"daily summaries: {last['daily_runs']} runs, {last['daily_temps']} temperatures")
    for path, s in report["answers"].items():
        if s["bad"]:
            found.append(f"answers: {s['bad']} of {s['count']} {path} failed")
        if s["first"] and s["last"] and s["last"] > s["first"] * ANSWER_GROWTH and s["last"] - s["first"] > 0.02:
            found.append(f"answers: {path} {s['first'] * 1000:.0f} ms at first, {s['last'] * 1000:.0f} ms at the end")
    found += [f"refused: {r}" for r in report["refused"][:10]]
    found += [f"logged: {e}" for e in report["errors"][:10]]
    return found


def summary(report: dict) -> str:
    lines = [f"{report['days']} days, a step every {report['step']} s, {report['retention']} days kept; "
             f"watched from day {report['watch_from'] + 1}",
             "day  date        real s  rss MB  traced KB  objects    rows  db KB  runs  temps"]
    every = max(1, len(report["daily"]) // 15)
    for d in report["daily"]:
        if d["day"] % every == 0 or d is report["daily"][-1]:
            lines.append(f"{d['day']:>3}  {d['date']}  {d['real_seconds']:>6.2f}  {d['rss_mb'] or 0:>6.1f}  "
                         f"{d['traced_kb'] if d['traced_kb'] is not None else '-':>9}  {d['objects']:>7}  "
                         f"{d['rows']:>6}  {d['db_kb']:>5}  {d['daily_runs']:>4}  {d['daily_temps']:>5}")
    t = report["threads"]
    lines.append(f"threads: {t['before']} before, {t['after_start']} after the start, {t['end']} at the end")
    if report["growth_total"] is not None:
        lines.append(f"memory traced from day {report['watch_from'] + 2}: {report['growth_total'] / 1024:+.0f} KB")
    lines.append("answers (median first/last third of the watched days, p95, count):")
    for path, s in report["answers"].items():
        fmt = lambda v: f"{v * 1000:.1f}" if v is not None else "-"
        lines.append(f"  {path:<45} {fmt(s['first']):>6} {fmt(s['last']):>6} {fmt(s['p95']):>6} ms  {s['count']}")
    if report["logged"]:
        lines.append("logged (warnings and errors):")
        lines += [f"  {n:>5}  {level} {name}: {msg}" for level, name, msg, n in report["logged"][:12]]
    lines.append("problems: " + ("none" if not report["problems"] else ""))
    lines += [f"  - {p}" for p in report["problems"]]
    if report["problems"] and report["growth"]:
        lines.append("memory growing most (bytes, blocks, where):")
        lines += [f"  {size:+} {count:+}\n    " + where.replace("\n", "\n    ") for size, count, where in report["growth"][:5]]
    return "\n".join(lines)


def run(home: Path, days: int = 90, step: int = 60, start: str = "2026-10-15", retention: int = 30,
        clients: int = 2, ask_every: int = 300, trace: bool = True) -> dict:
    return Run(Path(home), days, step, datetime.fromisoformat(start), retention, clients, ask_every, trace).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--step", type=int, default=60, help="simulated seconds between two cycles of the loops")
    parser.add_argument("--start", default="2026-10-15")
    parser.add_argument("--retention", type=int, default=30)
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--ask-every", type=int, default=300, help="simulated seconds between two refreshes of the pages")
    parser.add_argument("--no-trace", action="store_true", help="the process's memory, not Python's allocations traced")
    parser.add_argument("--out", help="the run's folder (kept); a temporary one otherwise")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    home = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="localhome-months-"))
    started = time.perf_counter()
    report = run(home, args.days, args.step, args.start, args.retention, args.clients, args.ask_every,
                 trace=not args.no_trace)
    print(summary(report))
    print(f"\n{time.perf_counter() - started:.0f} s in all; the run in {home}")
    (home / "report.json").write_text(json.dumps({k: v for k, v in report.items() if k != "daily"} |
                                                 {"daily": [{k: v for k, v in d.items() if k != "files"}
                                                            for d in report["daily"]]}, indent=1), encoding="utf-8")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
