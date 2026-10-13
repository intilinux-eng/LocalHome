"""Turns a copy of a board's history into a long-running installation's,
to try LocalHome with a full database without waiting a year:

  - samples: the days it already has, copied back in time until they
    cover --days (60 by default: past the 30 a board keeps, so its first
    prune has a month to remove, and past the 300 MB ceiling);
  - each day's summaries, a year back (--summary-days 400) before the
    samples' days: every zone's hours and the plant's (daily_runs), every
    temperature's lowest and highest (daily_temps), every meter's kWh and
    peak (daily_energy) - by the season, with a day's ups and downs;
  - with --heat-meter FILE, the heat meter's month-end readings for the
    last two years (config/heat_meter.json's format).

    python tools/seed_year_history.py history.db [--days 60] [--summary-days 400]
                                      [--heat-meter heat_meter.json]

Only the standard library: it runs where the database is - on a board,
with LocalHome stopped or on a copy. It writes into the database it's
given: never one somebody needs (a board's real history is cleared after,
from the admin panel's maintenance).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import time
from datetime import date, datetime, timedelta

DAY = 86400
# The days the board computes from its own samples (keep_days: the 30 kept
# and two more): older days' summaries are the ones made up here.
COMPUTED_DAYS = 32


def season(day: date) -> float:
    """-1 midwinter (mid-January) .. +1 midsummer (mid-July)."""
    return math.sin(2 * math.pi * (day.timetuple().tm_yday - 105) / 365)


def at(day: date, hour: float) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp() + hour * 3600)


def copy_samples(conn, days: float) -> int:
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
    if lo is None:
        raise SystemExit("no samples to copy")
    span = hi - lo
    target = hi - days * DAY
    copies, k = 0, 1
    while lo - k * span > target - span:
        shift = k * span
        conn.execute("INSERT INTO readings (ts, sensor, field, value) "
                     "SELECT ts - ?, sensor, field, value FROM readings WHERE ts BETWEEN ? AND ? AND ts - ? >= ?",
                     (shift, lo, hi, shift, target))
        conn.commit()
        copies += 1
        k += 1
        print(f"  samples copied back {shift / DAY:.1f} days", flush=True)
    return copies


def keys(conn, table: str, column: str, fallback: str) -> list[str]:
    found = [r[0] for r in conn.execute(f"SELECT DISTINCT {column} FROM {table}")]
    return found or [r[0] for r in conn.execute(fallback)]


def summaries(conn, summary_days: int, rng: random.Random) -> int:
    today = date.today()
    newest = today - timedelta(days=COMPUTED_DAYS + 1)
    runs = keys(conn, "daily_runs", "key", "SELECT DISTINCT sensor FROM readings WHERE field = 'is_on' AND sensor LIKE '% Valve'")
    temps = keys(conn, "daily_temps", "sensor", "SELECT DISTINCT sensor FROM readings WHERE field = 'temperature_c'")
    meters = keys(conn, "daily_energy", "sensor", "SELECT DISTINCT sensor FROM readings WHERE field = 'power_w'")
    rows = 0
    for n in range(summary_days):
        day = newest - timedelta(days=n)
        s = season(day)
        iso = day.isoformat()
        heating, cooling = s < -0.25, s > 0.55
        for key in runs:
            # Hours the valve was open: the colder, the longer; none between
            # the seasons; the bathrooms sit out the cooling.
            hours = 0.0
            if heating:
                hours = (2 + 6 * (-s - 0.25)) * rng.uniform(0.7, 1.3)
            elif cooling and "Bagni" not in key:
                hours = (1 + 5 * (s - 0.55)) * rng.uniform(0.6, 1.4)
            starts = 0 if hours == 0 else rng.randint(2, 9)
            conn.execute("INSERT OR IGNORE INTO daily_runs (key, day, seconds, starts) VALUES (?, ?, ?, ?)",
                         (key, iso, round(hours * 3600), starts))
            rows += 1
        for sensor in temps:
            outdoor = "Outdoor" in sensor
            mean = (13 + 11 * s if outdoor else 22.5 + 3 * s) + rng.gauss(0, 1.5 if outdoor else 0.4)
            spread = rng.uniform(3, 7) if outdoor else rng.uniform(0.4, 1.4)
            conn.execute("INSERT OR IGNORE INTO daily_temps (sensor, day, mean_c, min_c, max_c, min_at, max_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (sensor, iso, round(mean, 2), round(mean - spread / 2, 1), round(mean + spread / 2, 1),
                          at(day, rng.uniform(4, 7.5)), at(day, rng.uniform(13, 18))))
            rows += 1
        for meter in meters:
            kwh = (6 + 5 * abs(s)) * rng.uniform(0.75, 1.3) * (1 if "Casa" in meter or "POWCT" in meter else 0.15)
            conn.execute("INSERT OR IGNORE INTO daily_energy (sensor, day, kwh, source, peak_w, peak_ts) "
                         "VALUES (?, ?, ?, 'meter', ?, ?)",
                         (meter, iso, round(kwh, 2), round(rng.uniform(1500, 4500) * (kwh / 10)),
                          at(day, rng.uniform(12, 21))))
            rows += 1
    conn.commit()
    return rows


def heat_meter(path: str, rng: random.Random) -> None:
    """Month-end readings, cumulative as the meter's display, for the last
    24 months - heat in winter, cold in summer."""
    today = date.today()
    first = date(today.year - 2, today.month, 1)
    heat = cool = 0.0
    months = {}
    month = first
    while month < date(today.year, today.month, 1):
        s = season(month + timedelta(days=14))
        heat += max(0.0, -s) * rng.uniform(300, 520)
        cool += max(0.0, s - 0.3) * rng.uniform(150, 300)
        months[f"{month.year}-{month.month:02d}"] = {"heat_kwh": round(heat), "cool_kwh": round(cool)}
        month = (month + timedelta(days=32)).replace(day=1)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"months": months}, fh, indent=2)
    print(f"  heat meter: {len(months)} months in {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db")
    parser.add_argument("--days", type=float, default=60, help="the samples' days back (60)")
    parser.add_argument("--summary-days", type=int, default=400, help="the summaries' days before the samples' (400)")
    parser.add_argument("--heat-meter", help="write the heat meter's readings to this file")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    started = time.time()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    copy_samples(conn, args.days)
    after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    rows = summaries(conn, args.summary_days, rng)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    if args.heat_meter:
        heat_meter(args.heat_meter, rng)
    print(f"samples {before} -> {after}, {rows} summary rows, {os.path.getsize(args.db) / 1e6:.0f} MB, "
          f"{time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
