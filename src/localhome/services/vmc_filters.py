"""The VMC's filters: when they were last cleaned and how often they need
it - the reminder every serious unit has (a Maico AERA 320's manual: its
own filters and the vents' at least every 3 months). The interval and the
day they were last cleaned are set in the admin panel; the home page's
VMC card says when they're due - from SOON_DAYS before - and "cleaned
today" is said from either. Kept in config/vmc_filters.json; with no day
recorded there's no reminder, nothing to nag about on a new board.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from typing import Callable

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_DAYS = 90
INTERVAL_RANGE = (14, 365)
SOON_DAYS = 14


def _validate(values: dict, today: date) -> dict:
    if not isinstance(values, dict):
        raise InputError("request_invalid", "the filters' settings must be an object")
    clean = {}
    if "interval_days" in values:
        days = values["interval_days"]
        low, high = INTERVAL_RANGE
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not low <= days <= high:
            raise InputError("setting_range", f"interval_days: must be between {low} and {high}",
                             key="interval_days", min=low, max=high)
        clean["interval_days"] = int(round(days))
    if "cleaned_on" in values:
        value = values["cleaned_on"]
        if value is None:
            clean["cleaned_on"] = None
        else:
            try:
                day = date.fromisoformat(str(value))
            except ValueError:
                raise InputError("vmc_filters_date_invalid", f"cleaned_on: a date like 2026-10-12, got {value!r}")
            if day > today:
                raise InputError("vmc_filters_date_invalid", "cleaned_on can't be in the future")
            clean["cleaned_on"] = day.isoformat()
    return clean


class VmcFilterStore:
    def __init__(self, path: str, today: Callable[[], date] = date.today):
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
        try:
            saved = _validate(data, self.today())
        except InputError:
            logger.warning("Ignoring invalid %s - no filter reminder until it's set again", self.path)
            saved = {}
        return {"interval_days": saved.get("interval_days", DEFAULT_INTERVAL_DAYS),
                "cleaned_on": saved.get("cleaned_on")}

    def set(self, values: dict) -> dict:
        with self._lock:
            clean = {**self.get(), **_validate(values, self.today())}
            save_json_atomic(self.path, clean)
        return self.status()

    def cleaned_today(self) -> dict:
        return self.set({"cleaned_on": self.today().isoformat()})

    def status(self) -> dict:
        """For the pages: "unknown" (never recorded), "ok", "soon" (due
        within SOON_DAYS) or "due", with the day they're due and the days
        left (negative once past)."""
        saved = self.get()
        status = {**saved, "due_on": None, "days_left": None, "state": "unknown", "soon_days": SOON_DAYS}
        if saved["cleaned_on"]:
            due = date.fromisoformat(saved["cleaned_on"]) + timedelta(days=saved["interval_days"])
            left = (due - self.today()).days
            status.update(due_on=due.isoformat(), days_left=left,
                          state="due" if left <= 0 else "soon" if left <= SOON_DAYS else "ok")
        return status
