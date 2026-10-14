"""Each day's energy and peak for everything that measures its power - a
meter, a smart plug - kept for good.

The history keeps 30 days of samples; a week, a month and the year so far
need each day's total instead - one small row per meter per day
(HistoryStore's `daily_energy`), never pruned: 365 rows a year.

A day's kWh comes, best first, from:

- the meter's own counters ("meter"): `day_kwh` for today, overwritten
  each cycle, and `yesterday_kwh` for yesterday - a Sonoff POWCT has
  both. Overwriting, not keeping the largest, lets a counter that resets
  a minute after midnight put today's row right a minute later.
- the power, integrated between samples ("power") - for a meter without
  counters, and for the days recorded before this ledger existed
  (backfill()). A gap longer than MAX_GAP_SECONDS isn't bridged: nobody
  was measuring, so such a day reads low, and says it's an estimate.

Each day's peak is the highest power sampled, with when.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Two samples further apart than this weren't measuring the same stretch:
# the power between them isn't known, so it isn't counted.
MAX_GAP_SECONDS = 300


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class EnergyLedger:
    def __init__(self, store):
        self.store = store
        self._last: dict[str, tuple[float, float]] = {}   # meter -> (ts, power_w) of its last sample

    def sample(self, meter: str, reading: dict, now: float) -> None:
        """One recording cycle's reading of one power meter."""
        day = local_day(now)
        power = _number(reading.get("power_w"))
        day_kwh = _number(reading.get("day_kwh"))
        if day_kwh is not None:
            self.store.set_day(meter, day, kwh=day_kwh, source="meter")
            yesterday = _number(reading.get("yesterday_kwh"))
            if yesterday is not None:
                self.store.set_day(meter, local_day(now - 86400), kwh=yesterday, source="meter")
        elif power is not None:
            last = self._last.get(meter)
            if last and local_day(last[0]) == day and 0 < now - last[0] <= MAX_GAP_SECONDS:
                self.store.add_day_kwh(meter, day, (last[1] + power) / 2 * (now - last[0]) / 3_600_000)
            else:
                self.store.add_day_kwh(meter, day, 0.0)
        if power is not None:
            self.store.raise_day_peak(meter, day, power, int(now))
            self._last[meter] = (now, power)

    def backfill(self, meter: str, today: str | None = None) -> int:
        """The days already in the history without a row of their own:
        their kWh from the recorded power, their peak - for a meter that
        starts being followed with days of samples behind it. Today too,
        for a meter without counters (one with them fills today itself)."""
        today = today or local_day(time.time())
        known = set(self.store.energy_days(meter))
        rows = self.store.values_since(meter, "power_w", 0)
        filled = 0
        by_day: dict[str, list[tuple[int, float]]] = {}
        for ts, value in rows:
            by_day.setdefault(local_day(ts), []).append((ts, value))
        for day, samples in sorted(by_day.items()):
            if day in known:
                continue
            kwh = sum((a[1] + b[1]) / 2 * (b[0] - a[0]) / 3_600_000
                      for a, b in zip(samples, samples[1:]) if 0 < b[0] - a[0] <= MAX_GAP_SECONDS)
            self.store.add_day_kwh(meter, day, kwh)
            peak_ts, peak_w = max(samples, key=lambda s: s[1])
            self.store.raise_day_peak(meter, day, peak_w, peak_ts)
            filled += 1
        if filled:
            logger.info("Energy: %s - %d day(s) worked out from the recorded power", meter, filled)
        return filled

    def import_device_days(self, meter: str, days: dict[str, float], today: str | None = None) -> int:
        """Past days as the device itself counted them (a Meross plug keeps
        about a month): written as its own counter's - over a day worked
        out from the power (a gap leaves it low) or missing, so the days
        "Cancella storico" took come back. Today is the samples'. Returns
        the days written."""
        today = today or local_day(time.time())
        known = self.store.energy_days(meter)
        written = 0
        for day, kwh in sorted(days.items()):
            kwh = _number(kwh)
            if day >= today or kwh is None or kwh < 0:
                continue
            if (known.get(day) or {}).get("source") == "meter" and known[day]["kwh"] == round(kwh, 3):
                continue
            self.store.set_day(meter, day, kwh=kwh, source="meter")
            written += 1
        if written:
            logger.info("Energy: %s - %d past day(s) from the device's own counter", meter, written)
        return written


def summary(store, meter: str, today: date | None = None) -> dict:
    """Today, yesterday, the last 7 days (today the last - never a week
    with one bar on a Monday), this month and this year by month - what the
    home page's energy card shows."""
    today = today or date.today()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    rows = store.energy_days(meter, since=min(week_start, year_start).isoformat())

    def day_row(d: date) -> dict | None:
        row = rows.get(d.isoformat())
        return {"day": d.isoformat(), **row} if row else None

    def span(start: date, end: date) -> dict:
        days = [day_row(start + timedelta(days=i)) for i in range((end - start).days + 1)]
        present = [d for d in days if d and d["kwh"] is not None]
        return {"from": start.isoformat(), "kwh": round(sum(d["kwh"] for d in present), 2),
                "estimated": any(d["source"] == "power" for d in present), "days": days}

    week = span(week_start, week_start + timedelta(days=6))
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    month = span(month_start, next_month - timedelta(days=1))
    months = []
    for m in range(1, 13):
        key = f"{today.year}-{m:02d}"
        present = [r for d, r in rows.items() if d.startswith(key) and r["kwh"] is not None]
        months.append({"month": key, "kwh": round(sum(r["kwh"] for r in present), 2) if present else None,
                       "estimated": any(r["source"] == "power" for r in present)})
    year_rows = [r for d, r in rows.items() if d >= year_start.isoformat() and r["kwh"] is not None]
    first = store.first_energy_day(meter)
    return {
        "today": day_row(today),
        "yesterday": day_row(today - timedelta(days=1)),
        "week": week,
        "month": month,
        "year": {"from": year_start.isoformat(), "kwh": round(sum(r["kwh"] for r in year_rows), 2),
                 "estimated": any(r["source"] == "power" for r in year_rows), "months": months},
        "since": first,
    }
