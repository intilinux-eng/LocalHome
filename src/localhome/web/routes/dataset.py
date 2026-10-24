"""The house's long-term record, out (services/house_dataset.py): how much
it holds, and CSV files - for a notebook or a model by default, machine-
readable on purpose: fixed English headers, a decimal point, times in UTC
beside the board's own; or ?format=sheet for a spreadsheet, like the usage
CSV: the panel's language for the headers, its separator and decimal mark,
shares of time as percents, the local time first.

?from=2026-10-01&to=2026-10-21 (the board's days, both included; the
last 7 days without), ?what=slots (5 minutes a row, a column group per
zone) or events. Up to the moment it's asked: the 5 minutes going on are
written first. Written a batch at a time: years of rows don't have to fit
in memory."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request

from localhome.web.routes.common import _fail

HOUSE_COLUMNS = ("season", "away", "diagnostic", "outdoor_c", "outdoor_rh", "vmc_pct", "vmc_on",
                 "dehumidifier_on", "bypass_on", "flags")
ZONE_COLUMNS = ("temp_c", "rh", "target_c", "valve_open", "by_hand", "flags")
EVENT_COLUMNS = ("device", "what", "value", "why")
# Shares of the 5 minutes (0-1): a percent in a spreadsheet.
SHARES = {"vmc_on", "dehumidifier_on", "bypass_on", "valve_open"}


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return value


def _times(ts: float) -> tuple[str, str]:
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), utc.astimezone().isoformat(timespec="seconds")


class _Sheet:
    """The spreadsheet's way: the panel's headers, decimal mark and
    separator; the board's local time first (the hour October repeats is
    told apart by the UTC beside it)."""

    def __init__(self, texts: dict):
        self.cols = texts["dataset_cols"]
        self.decimal = texts["usage_csv_decimal"]
        self.separator = texts["usage_csv_separator"]

    def cell(self, column: str, value):
        if value is None:
            return ""
        if column in SHARES:
            value = round(value * 100)
        if isinstance(value, float):
            return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", self.decimal)
        return value

    @staticmethod
    def times(ts: float) -> tuple[str, str]:
        utc = datetime.fromtimestamp(ts, timezone.utc)
        return utc.astimezone().strftime("%Y-%m-%d %H:%M:%S"), utc.strftime("%Y-%m-%d %H:%M:%S")

    def head(self, columns, zones=()) -> list[str]:
        return [self.cols["time_local"], self.cols["time_utc"], *(self.cols[c] for c in columns),
                *(self.cols[f"zone_{c}"].format(zone=zone) for zone in zones for c in ZONE_COLUMNS)]


def _range() -> tuple[float, float, str]:
    """The days asked for, as UTC seconds [start, end) and a name for the file."""
    today = date.today()
    first = date.fromisoformat(request.args["from"]) if request.args.get("from") else today - timedelta(days=6)
    last = date.fromisoformat(request.args["to"]) if request.args.get("to") else today
    start = datetime.combine(first, datetime.min.time()).timestamp()
    end = datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp()
    return start, end, f"{first:%Y%m%d}-{last:%Y%m%d}"


def _lines(rows, sheet: _Sheet | None = None):
    out = io.StringIO()
    if sheet:
        # The BOM: a spreadsheet then reads it as UTF-8 (°C, the zones' names).
        out.write("﻿")
        writer = csv.writer(out, delimiter=sheet.separator, lineterminator="\r\n")
    else:
        writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
        if out.tell() > 64 * 1024:
            yield out.getvalue()
            out.seek(0)
            out.truncate()
    yield out.getvalue()


def register(app: Flask) -> None:
    def store(up_to_now: bool = False):
        dataset = app.config.get("HOUSE_DATASET")
        if dataset and up_to_now:
            try:
                dataset.checkpoint()
            except Exception:  # what's written already still goes out
                app.logger.exception("House dataset: writing the minutes going on before a download failed")
        return dataset.store if dataset else None

    @app.route("/api/climate-admin/dataset")
    def api_dataset():
        found = store()
        if found is None:
            return jsonify(ok=True, enabled=False)
        return jsonify(ok=True, enabled=True, **found.summary())

    @app.route("/api/climate-admin/dataset.csv")
    def api_dataset_csv():
        what = request.args.get("what", "slots")
        if what not in ("slots", "events"):
            return _fail("dataset_what_invalid", "what must be slots or events")
        try:
            start, end, name = _range()
        except ValueError:
            return _fail("dataset_dates_invalid", "from/to must be dates like 2026-10-21")
        found = store(up_to_now=True)
        if found is None:
            return _fail("dataset_off", "the house dataset is switched off", 404)
        sheet = _Sheet(app.config["TRANSLATIONS"]["climate_admin"]) if request.args.get("format") == "sheet" else None
        times = sheet.times if sheet else _times
        cell = sheet.cell if sheet else (lambda column, value: _cell(value))
        if what == "events":
            head = sheet.head(EVENT_COLUMNS) if sheet else ["time_utc", "time_local", *EVENT_COLUMNS]
            rows = (head, *([*times(ts), device, kind, cell("value", value), why or ""]
                            for ts, device, kind, value, why in found.events(start, end)))
        else:
            zones = found.zones(start, end)
            head = sheet.head(("samples", *HOUSE_COLUMNS), zones) if sheet else \
                ["time_utc", "time_local", "samples", *HOUSE_COLUMNS,
                 *(f"{zone}_{column}" for zone in zones for column in ZONE_COLUMNS)]

            def slot_rows():
                yield head
                for house, by_zone in found.slots(start, end):
                    yield [*times(house["ts"]), house["samples"], *(cell(c, house[c]) for c in HOUSE_COLUMNS),
                           *(cell(c, (by_zone.get(zone) or {}).get(c)) for zone in zones for c in ZONE_COLUMNS)]
            rows = slot_rows()
        file = f"{app.config['TRANSLATIONS']['climate_admin']['dataset_file'] if sheet else 'house'}-{what}-{name}.csv"
        return Response(_lines(rows, sheet), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{file}"', "Cache-Control": "no-store"})
