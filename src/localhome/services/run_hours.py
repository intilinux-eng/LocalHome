"""How long each zone's valve was open, and the plant's - any of them
open, counted once: the last 24 hours, day by day for the home page,
month by month for the admin panel - and, beside the plant's hours, the
heat meter's own readings, typed in the admin panel (a heat meter like a
QUNDIS Q heat 5.5 keeps 15 month-end values on its display, heat and
cooling apart): the kWh those hours took, per hour, and in euros with a
price per kWh.

The hours come from the valves' recorded state (services/history.py:
the samples for a month, then one row a day kept for good). They are
the time the plant was asked to run, not its energy: the heat meter's
kWh are what it delivered, and their ratio is what an hour costs.
"""
from __future__ import annotations

import threading
from calendar import monthrange
from datetime import date, datetime, timedelta

from localhome.errors import InputError
from localhome.services.history import PLANT_KEY, HistoryStore, run_days_of, run_window_of
from localhome.util.jsonfile import load_json, save_json_atomic

FIELD = "is_on"
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


def month_report(history: HistoryStore, zones: list[dict], interval: float, months: int = 12,
                 now: datetime | None = None) -> list[dict]:
    """For the admin panel, newest first: each of the last `months`
    months' hours - the plant's and each zone's - its starts, and how many
    of its days are known (the rest before LocalHome ran, or lost)."""
    now = now or datetime.now()
    today = now.date()
    year, month = today.year, today.month
    starts_of = []
    for _ in range(months):
        starts_of.append(date(year, month, 1))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    sensors = [z["valve"] for z in zones]
    by_day = run_days_of(history, sensors, FIELD, interval, starts_of[-1], now)
    out = []
    for first in starts_of:
        last = min(today, date(first.year, first.month, monthrange(first.year, first.month)[1]))
        days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
        known = [d for d in days if d in by_day[PLANT_KEY]]
        total = lambda key: _hours(sum(by_day[key].get(d, (0, 0))[0] for d in days))
        out.append({"month": first.strftime("%Y-%m"), "days": len(days), "days_known": len(known),
                    "plant_hours": total(PLANT_KEY),
                    "plant_starts": sum(by_day[PLANT_KEY].get(d, (0, 0))[1] for d in days),
                    "zones": {z["name"]: total(z["valve"]) for z in zones}})
    return out


class HeatMeterStore:
    """config/heat_meter.json: the heat meter's readings, typed in the
    admin panel - {"day", "heat_kwh", "cool_kwh"} (cumulative, as its
    display shows them; cooling only on a meter with the cooling range) -
    and an optional price per kWh."""

    def __init__(self, path: str, today=date.today):
        self.path = path
        self.today = today
        self._lock = threading.Lock()

    def get(self) -> dict:
        try:
            data = load_json(self.path)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        readings = [r for r in data.get("readings") or [] if isinstance(r, dict) and r.get("day")]
        price = data.get("price_eur_kwh")
        return {"readings": sorted(readings, key=lambda r: r["day"]),
                "price_eur_kwh": price if isinstance(price, (int, float)) and not isinstance(price, bool) else None}

    def _save(self, data: dict) -> dict:
        save_json_atomic(self.path, data)
        return data

    def add(self, values: dict) -> dict:
        """A reading - replacing the one of its day - each register at
        least the reading before and at most the one after: the meter
        only counts up."""
        try:
            day = date.fromisoformat(str(values.get("day")))
        except ValueError:
            raise InputError("heat_meter_day_invalid", "day: a date like 2026-10-31")
        if day > self.today():
            raise InputError("heat_meter_day_invalid", "day: not in the future")
        reading = {"day": day.isoformat()}
        for key, required in (("heat_kwh", True), ("cool_kwh", False)):
            value = values.get(key)
            if value is None and not required:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1e9:
                raise InputError("heat_meter_value_invalid", f"{key}: a reading in kWh, 0 or more", key=key)
            reading[key] = round(float(value), 3)
        with self._lock:
            data = self.get()
            others = [r for r in data["readings"] if r["day"] != reading["day"]]
            before = [r for r in others if r["day"] < reading["day"]]
            after = [r for r in others if r["day"] > reading["day"]]
            for key in ("heat_kwh", "cool_kwh"):
                if key not in reading:
                    continue
                low = next((r[key] for r in reversed(before) if key in r), None)
                high = next((r[key] for r in after if key in r), None)
                if (low is not None and reading[key] < low) or (high is not None and reading[key] > high):
                    raise InputError("heat_meter_not_increasing",
                                     f"{key}: the meter only counts up - between {low} and {high}", key=key)
            data["readings"] = sorted(others + [reading], key=lambda r: r["day"])
            return self._save(data)

    def remove(self, day: str) -> dict:
        with self._lock:
            data = self.get()
            data["readings"] = [r for r in data["readings"] if r["day"] != day]
            return self._save(data)

    def set_price(self, price) -> dict:
        if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 <= price <= 10):
            raise InputError("heat_meter_price_invalid", "price_eur_kwh: euros per kWh, 0 to 10, or null")
        with self._lock:
            data = self.get()
            data["price_eur_kwh"] = None if price is None else round(float(price), 4)
            return self._save(data)


def meter_report(history: HistoryStore, zones: list[dict], interval: float, meter: dict,
                 now: datetime | None = None) -> list[dict]:
    """Between each two readings, newest first: the kWh the meter counted
    (heat, cooling, both), the plant's hours from the day after the first
    reading to the day of the second (a reading is the meter at the end
    of its day), how many of those days' hours are known, kWh per hour,
    and with a price the euros, in all and per hour."""
    readings = meter["readings"]
    if len(readings) < 2:
        return []
    sensors = [z["valve"] for z in zones]
    first = date.fromisoformat(readings[0]["day"]) + timedelta(days=1)
    plant = run_days_of(history, sensors, FIELD, interval, first, now)[PLANT_KEY]
    price = meter.get("price_eur_kwh")
    out = []
    for a, b in zip(readings, readings[1:]):
        start, end = date.fromisoformat(a["day"]) + timedelta(days=1), date.fromisoformat(b["day"])
        days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        known = [d for d in days if d in plant]
        hours = _hours(sum(plant[d][0] for d in known))
        heat = round(b["heat_kwh"] - a["heat_kwh"], 3)
        cool = round(b["cool_kwh"] - a["cool_kwh"], 3) if "cool_kwh" in a and "cool_kwh" in b else None
        kwh = round(heat + (cool or 0), 3)
        row = {"from": a["day"], "to": b["day"], "days": len(days), "days_known": len(known),
               "heat_kwh": heat, "cool_kwh": cool, "kwh": kwh, "plant_hours": hours,
               "kwh_per_hour": round(kwh / hours, 2) if hours > 0 else None,
               "eur": round(kwh * price, 2) if price is not None else None}
        row["eur_per_hour"] = round(row["eur"] / hours, 2) if row["eur"] is not None and hours > 0 else None
        out.append(row)
    return list(reversed(out))
