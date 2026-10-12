"""How long each zone's valve was open, and the plant's - any of them
open, counted once: the last 24 hours and day by day for the home page;
for the admin panel a year month by month, beside the heat meter's
month-end values typed there (a heat meter like a QUNDIS Q heat 5.5
keeps the last 15 on its display, heat and cooling apart): the kWh each
month took, per hour of the plant running and per degree day of the
outdoor sensor's, beside the same month the year before, and this
month's estimated. Six years kept.

The hours come from the valves' recorded state (services/history.py:
the samples for a month, then one row a day). They are the time the
plant was asked to run, not its energy: the heat meter's kWh are what
it delivered, and their ratio is what an hour takes.
"""
from __future__ import annotations

import threading
from calendar import monthrange
from datetime import date, datetime, timedelta

from localhome.errors import InputError
from localhome.services.history import (PLANT_KEY, RUN_YEARS, HistoryStore, first_run_day, run_days_of,
                                        run_window_of, temp_days_of)
from localhome.util.jsonfile import load_json, save_json_atomic

FIELD = "is_on"
REGISTERS = ("heat_kwh", "cool_kwh")
# Degree days (gradi giorno, as Italy counts them): each day, 20 °C less
# its outdoor mean, when below.
DD_BASE_C = 20.0
DAY_SECONDS = 86400


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


def zone_report(history: HistoryStore, zones: list[dict], interval: float, days: int = 30,
                now: datetime | None = None) -> dict:
    """For the home page: the last 24 hours and each of the last `days`
    days (today included, oldest first) - hours and starts - for each zone
    ({"name", "valve"}) and the plant."""
    now = now or datetime.now()
    sensors = [z["valve"] for z in zones]
    first = now.date() - timedelta(days=days - 1)
    by_day = run_days_of(history, sensors, FIELD, interval, first, now)
    window = run_window_of(history, sensors, FIELD, interval, DAY_SECONDS, now)
    day_list = [(first + timedelta(days=i)).isoformat() for i in range(days)]

    def one(key: str) -> dict:
        known = by_day[key]
        return {"hours": [_hours(known[d][0]) if d in known else None for d in day_list],
                "starts": [known[d][1] if d in known else None for d in day_list],
                "last_24h": _hours(window[key][0]), "last_24h_starts": window[key][1]}

    return {"days": day_list, "zones": {z["name"]: one(z["valve"]) for z in zones}, "plant": one(PLANT_KEY)}


def keep_days(history: HistoryStore, zones: list[dict], interval: float, days: int,
              now: datetime | None = None, temps: list[str] = ()) -> None:
    """The last `days` days' hours into daily_runs, and the temperatures'
    (`temps`: the outdoor sensor, the zones') into daily_temps, while the
    samples still have them - once a day, before the old samples go
    (HistoryRecorder): no day is lost because no page asked for it in time."""
    now = now or datetime.now()
    first = now.date() - timedelta(days=days)
    if zones:
        run_days_of(history, [z["valve"] for z in zones], FIELD, interval, first, now)
    for sensor in temps:
        temp_days_of(history, sensor, "temperature_c", interval, first, now)


def temp_report(history: HistoryStore, zones: list[dict], interval: float, days: int = 7,
                now: datetime | None = None) -> dict:
    """For the home page: each zone's ({"name", "sensor"}) last `days`
    days, today included, oldest first - its lowest and highest and when
    (epoch seconds), None for a day not recorded."""
    now = now or datetime.now()
    first = now.date() - timedelta(days=days - 1)
    day_list = [(first + timedelta(days=i)).isoformat() for i in range(days)]

    def one(sensor: str) -> list:
        kept = temp_days_of(history, sensor, "temperature_c", interval, first, now)
        return [None if d not in kept else {"min": round(kept[d][1], 1), "max": round(kept[d][2], 1),
                                            "min_at": kept[d][3], "max_at": kept[d][4]} for d in day_list]

    return {"days": day_list, "zones": {z["name"]: one(z["sensor"]) for z in zones}}


def first_year(today: date) -> int:
    """The oldest year kept: this one and the five before."""
    return today.year - RUN_YEARS + 1


def _month_label(key: str) -> str:
    return f"{key[5:7]}/{key[:4]}"


class HeatMeterStore:
    """config/heat_meter.json: the heat meter's month-end values, as its
    display keeps them (a QUNDIS Q heat 5.5: the last 15 months, heat and
    cooling apart, totals that only count up), typed in the admin panel a
    year at a time - {"months": {"2026-01": {"heat_kwh", "cool_kwh"}}} -
    a month only once it's over; the last six years kept."""

    def __init__(self, path: str, today=date.today):
        self.path = path
        self.today = today
        self._lock = threading.Lock()

    def get(self) -> dict[str, dict]:
        try:
            data = load_json(self.path)
        except (OSError, ValueError):
            data = {}
        months = data.get("months") if isinstance(data, dict) else None
        oldest = f"{first_year(self.today())}-01"
        return {k: v for k, v in sorted((months or {}).items())
                if isinstance(v, dict) and len(k) == 7 and k >= oldest}

    def set_year(self, year, months) -> dict[str, dict]:
        """The year's months at once - {"01": {"heat_kwh", "cool_kwh"} or
        none for it} - each over already, each register at least the month
        before's and at most the month after's: the meter only counts up."""
        today = self.today()
        if isinstance(year, bool) or not isinstance(year, int) or not first_year(today) <= year <= today.year:
            raise InputError("heat_meter_year_invalid", f"year: {first_year(today)} to {today.year}")
        if not isinstance(months, dict):
            raise InputError("heat_meter_year_invalid", "months: {\"01\": {...}, ...}")
        mine = {}
        for number, values in months.items():
            if str(number) not in {f"{m:02d}" for m in range(1, 13)}:
                raise InputError("heat_meter_year_invalid", f"months: 01 to 12, not {number}")
            key = f"{year}-{number}"
            if values is not None and not isinstance(values, dict):
                raise InputError("heat_meter_value_invalid", f"{key}: {{\"heat_kwh\", \"cool_kwh\"}}",
                                 month=_month_label(key))
            month = {}
            for register in REGISTERS:
                value = (values or {}).get(register)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1e9:
                    raise InputError("heat_meter_value_invalid", f"{key} {register}: kWh, 0 or more",
                                     month=_month_label(key))
                month[register] = round(float(value), 3)
            if month and (year, int(number)) >= (today.year, today.month):
                raise InputError("heat_meter_month_open", f"{key}: not over yet", month=_month_label(key))
            if month:
                mine[key] = month
        with self._lock:
            kept = {k: v for k, v in self.get().items() if not k.startswith(f"{year}-")}
            kept.update(mine)
            kept = dict(sorted(kept.items()))
            for register in REGISTERS:
                last = None
                for key, month in kept.items():
                    if register not in month:
                        continue
                    if last is not None and month[register] < last:
                        raise InputError("heat_meter_not_increasing", f"{key} {register}: below the month before",
                                         month=_month_label(key))
                    last = month[register]
            save_json_atomic(self.path, {"months": kept})
            return kept


def _before(key: str) -> str:
    year, month = int(key[:4]), int(key[5:])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def _kind(row: dict) -> str:
    """A month's kind by what its kWh were mostly: heat or cooling."""
    return "cool" if (row["cool_kwh"] or 0) > (row["heat_kwh"] or 0) else "heat"


def _rows(by_day: dict, temps: dict, meter: dict[str, dict], zones: list[dict], year: int, today: date) -> list[dict]:
    """Each month of `year` up to this one: the meter's values and the kWh
    it counted (its value less the month before's - January's December),
    the plant's and each zone's hours, the outdoor mean and degree days
    when every day's temperature is known, kWh per hour where a month has
    both its kWh and all its hours, and heat kWh per degree day."""
    rows = []
    for number in range(1, 13 if year < today.year else today.month + 1):
        key = f"{year}-{number:02d}"
        first = date(year, number, 1)
        last = min(today, date(year, number, monthrange(year, number)[1]))
        days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
        known = [d for d in days if d in by_day[PLANT_KEY]]
        hours = lambda k: _hours(sum(by_day[k].get(d, (0, 0))[0] for d in known)) if known else None
        values, before = meter.get(key, {}), meter.get(_before(key), {})
        used = {r: round(values[r] - before[r], 3) if r in values and r in before else None for r in REGISTERS}
        kwh = None if all(v is None for v in used.values()) else round(sum(v or 0 for v in used.values()), 3)
        means = [temps[d][0] for d in days if d in temps]
        whole_temps = len(means) == len(days)
        row = {"month": key, "open": (year, number) == (today.year, today.month),
               "heat_meter": values.get("heat_kwh"), "cool_meter": values.get("cool_kwh"), **used, "kwh": kwh,
               "days": len(days), "days_known": len(known), "plant_hours": hours(PLANT_KEY),
               "zones": {z["name"]: hours(z["valve"]) for z in zones},
               "outdoor_c": round(sum(means) / len(means), 1) if whole_temps else None,
               "degree_days": round(sum(max(0.0, DD_BASE_C - m) for m in means)) if whole_temps else None}
        whole = known and len(known) == len(days) and row["plant_hours"] and not row["open"]
        row["kwh_per_hour"] = round(kwh / row["plant_hours"], 2) if whole and kwh is not None else None
        dd = row["degree_days"]
        row["kwh_per_dd"] = (round(used["heat_kwh"] / dd, 2)
                             if dd and used["heat_kwh"] is not None and not row["open"] else None)
        rows.append(row)
    return rows


def year_report(history: HistoryStore, zones: list[dict], interval: float, meter: dict[str, dict], year: int,
                now: datetime | None = None, mode: str | None = None, outdoor: str | None = None) -> dict:
    """A year month by month, up to this one (see _rows), each beside the
    same month the year before; this month's kWh estimated - its hours so
    far at the kWh per hour of the last months of the same kind (heat or
    cooling, the plant's `mode` now), up to three in the last year; the
    year's total, with the same months the year before; and the December
    before's values, for January's usage as it's typed."""
    now = now or datetime.now()
    today = now.date()
    sensors = [z["valve"] for z in zones]
    by_day = run_days_of(history, sensors, FIELD, interval, date(year - 1, 1, 1), now)
    temps = temp_days_of(history, outdoor, "temperature_c", interval, date(year - 1, 1, 1), now) if outdoor else {}
    rows = _rows(by_day, temps, meter, zones, year, today)
    earlier = _rows(by_day, temps, meter, zones, year - 1, today)
    same = {r["month"][5:]: r for r in earlier}
    for row in rows:
        before = same.get(row["month"][5:])
        row["last_year"] = {k: before[k] for k in ("kwh", "plant_hours", "kwh_per_hour", "kwh_per_dd", "degree_days")}

    now_row = rows[-1] if rows and rows[-1]["open"] else None
    if now_row and now_row["plant_hours"]:
        since = f"{today.year - 1}-{today.month:02d}"
        pool = [r for r in reversed(earlier + rows) if r["kwh_per_hour"] is not None and r["kwh"] and r["month"] >= since]
        kind = mode if mode in ("heat", "cool") else (_kind(pool[0]) if pool else None)
        basis = [r for r in pool if _kind(r) == kind][:3]
        if basis:
            rate = sum(r["kwh"] for r in basis) / sum(r["plant_hours"] for r in basis)
            now_row["estimate"] = {"kwh": round(now_row["plant_hours"] * rate), "per_hour": round(rate, 2),
                                   "kind": kind, "months": sorted(r["month"] for r in basis)}

    def total(pick, among=rows):
        values = [v for r in among if (v := pick(r)) is not None]
        return round(sum(values), 2) if values else None

    paired = [r for r in rows if r["kwh_per_hour"] is not None]
    paired_hours = sum(r["plant_hours"] for r in paired)
    by_dd = [r for r in rows if r["kwh_per_dd"] is not None]
    both = [r for r in rows if r["kwh"] is not None and r["last_year"]["kwh"] is not None]
    december = meter.get(f"{year - 1}-12", {})
    return {"year": year, "months": rows, "before": {r: december.get(r) for r in REGISTERS},
            "total": {**{k: total(lambda r, k=k: r[k]) for k in ("heat_kwh", "cool_kwh", "kwh", "plant_hours")},
                      "zones": {z["name"]: total(lambda r, n=z["name"]: r["zones"][n]) for z in zones},
                      "kwh_per_hour": round(sum(r["kwh"] for r in paired) / paired_hours, 2) if paired_hours else None,
                      "months_paired": len(paired),
                      "degree_days": total(lambda r: r["degree_days"]),
                      "kwh_per_dd": (round(sum(r["heat_kwh"] for r in by_dd) / sum(r["degree_days"] for r in by_dd), 2)
                                     if by_dd else None),
                      "last_year": {"months": len(both), "kwh": total(lambda r: r["kwh"], both),
                                    "last_kwh": total(lambda r: r["last_year"]["kwh"], both)} if both else None}}


def years_of(history: HistoryStore, meter: dict[str, dict], today: date) -> list[int]:
    """The years there's something for, newest first - this one and the
    one before always (the meter's 15 months reach into it), none before
    the six kept."""
    firsts = [int(k[:4]) for k in meter] + ([int(d[:4])] if (d := first_run_day(history)) else [])
    oldest = max(first_year(today), min([today.year - 1, *firsts]))
    return list(range(today.year, oldest - 1, -1))
