"""The house's long-term record, out (services/house_dataset.py): how much
it holds, and CSV files for a notebook or a model - machine-readable on
purpose, unlike the usage CSV for a spreadsheet: fixed English headers,
a decimal point, times in UTC beside the board's own.

?from=2026-10-01&to=2026-10-21 (the board's days, both included; the
last 7 days without), ?what=slots (5 minutes a row, a column group per
zone) or events. Written a batch at a time: years of rows don't have to
fit in memory."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request

from localhome.web.routes.common import _fail

HOUSE_COLUMNS = ("season", "away", "diagnostic", "outdoor_c", "outdoor_rh", "vmc_pct", "vmc_on",
                 "dehumidifier_on", "bypass_on", "flags")
ZONE_COLUMNS = ("temp_c", "rh", "target_c", "valve_open", "by_hand", "flags")


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return value


def _times(ts: float) -> tuple[str, str]:
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), utc.astimezone().isoformat(timespec="seconds")


def _range() -> tuple[float, float, str]:
    """The days asked for, as UTC seconds [start, end) and a name for the file."""
    today = date.today()
    first = date.fromisoformat(request.args["from"]) if request.args.get("from") else today - timedelta(days=6)
    last = date.fromisoformat(request.args["to"]) if request.args.get("to") else today
    start = datetime.combine(first, datetime.min.time()).timestamp()
    end = datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp()
    return start, end, f"{first:%Y%m%d}-{last:%Y%m%d}"


def _lines(rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
        if out.tell() > 64 * 1024:
            yield out.getvalue()
            out.seek(0)
            out.truncate()
    yield out.getvalue()


def register(app: Flask) -> None:
    def store():
        dataset = app.config.get("HOUSE_DATASET")
        return dataset.store if dataset else None

    @app.route("/api/climate-admin/dataset")
    def api_dataset():
        found = store()
        if found is None:
            return jsonify(ok=True, enabled=False)
        return jsonify(ok=True, enabled=True, **found.summary())

    @app.route("/api/climate-admin/dataset.csv")
    def api_dataset_csv():
        found = store()
        if found is None:
            return _fail("dataset_off", "the house dataset is switched off", 404)
        try:
            start, end, name = _range()
        except ValueError:
            return _fail("dataset_dates_invalid", "from/to must be dates like 2026-10-21")
        what = request.args.get("what", "slots")
        if what == "events":
            rows = (["time_utc", "time_local", "device", "what", "value", "why"],
                    *([*_times(ts), device, kind, _cell(value), why or ""]
                      for ts, device, kind, value, why in found.events(start, end)))
        elif what == "slots":
            zones = found.zones(start, end)
            head = ["time_utc", "time_local", "samples", *HOUSE_COLUMNS,
                    *(f"{zone}_{column}" for zone in zones for column in ZONE_COLUMNS)]

            def slot_rows():
                yield head
                for house, by_zone in found.slots(start, end):
                    yield [*_times(house["ts"]), house["samples"], *(_cell(house[c]) for c in HOUSE_COLUMNS),
                           *(_cell((by_zone.get(zone) or {}).get(c)) for zone in zones for c in ZONE_COLUMNS)]
            rows = slot_rows()
        else:
            return _fail("dataset_what_invalid", "what must be slots or events")
        return Response(_lines(rows), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="house-{what}-{name}.csv"',
                                 "Cache-Control": "no-store"})
