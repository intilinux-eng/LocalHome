"""Fills a demo's history with two plausible days, so its charts and
statistics show something the moment it starts.

    python tools/seed_demo_history.py demo/config/config.yaml [--hours 48]

The rooms follow the demo's own sample week (demo/config/schedules.json)
through a small model of a radiant floor: slow to warm, slow to cool,
the valve opening below target - hysteresis and closing above it. Only
for a demo: it writes into whatever history database the config names.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localhome.config import load_config  # noqa: E402
from localhome.drivers.simulated.climate import HT_REST_C  # noqa: E402
from localhome.services.history import HistoryStore  # noqa: E402

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STEP = 300                      # one sample every 5 minutes
HEAT_RATE = 0.45 / 3600         # °C per second with the valve open
LOSS = 0.06 / 3600              # °C per second per °C above outdoors
HYSTERESIS = 0.25
# The last hours ease into where the demo's simulated sensors start - the
# season's resting temperature (drivers/simulated/climate.py) - so the
# chart meets the live reading.
EASE_HOURS = 3


def outdoor(ts: float) -> float:
    hour = datetime.fromtimestamp(ts).hour + datetime.fromtimestamp(ts).minute / 60
    return 13.0 + 5.0 * math.sin((hour - 9) / 24 * 2 * math.pi)


def target(week: dict, ts: float):
    moment = datetime.fromtimestamp(ts)
    return week[DAYS[moment.weekday()]][moment.hour]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--hours", type=float, default=48)
    args = parser.parse_args()
    config = load_config(args.config)
    history = config.raw.get("history") or {}
    store = HistoryStore(str(config.resolve(history.get("db_file", "data/history.db"))))
    schedules = json.loads(Path(config.resolve("schedules.json")).read_text(encoding="utf-8"))
    weeks = schedules.get("zones", {})
    live_c = HT_REST_C.get(schedules.get("mode"), HT_REST_C["heat"])
    rnd = random.Random(7)
    now = time.time()
    start = now - args.hours * 3600
    zones = {name: {"t": 19.0 + rnd.random(), "rh": 52 + rnd.random() * 6, "on": False} for name in weeks}
    rows = []
    ts = start
    while ts < now:
        batch = {}
        out = outdoor(ts)
        batch["Outdoor Sensor"] = {"temperature_c": round(out + rnd.uniform(-0.2, 0.2), 1),
                                   "humidity_pct": round(70 - (out - 13) * 2 + rnd.uniform(-1, 1), 1)}
        for name, state in zones.items():
            goal = target(weeks[name], ts)
            if goal is None:
                state["on"] = False
            elif state["t"] < goal - HYSTERESIS:
                state["on"] = True
            elif state["t"] > goal + HYSTERESIS:
                state["on"] = False
            state["t"] += STEP * ((HEAT_RATE if state["on"] else 0) - LOSS * (state["t"] - out))
            state["rh"] = min(70, max(40, state["rh"] + rnd.uniform(-0.4, 0.4) - (0.3 if state["on"] else -0.1)))
            reading = {"temperature_c": round(state["t"], 1), "humidity_pct": round(state["rh"], 1),
                       "battery_pct": 92.0, "wifi_rssi_dbm": -60.0}
            if goal is not None:
                reading["target_c"] = goal
            batch[f"{name} Sensor"] = reading
            batch[f"{name} Valve"] = {"is_on": 1.0 if state["on"] else 0.0}
        rows.append((ts, batch))
        ts += STEP
    last = {name: batch[f"{name} Sensor"]["temperature_c"] for name in zones}
    for ts, batch in rows:
        k = max(0.0, 1 - (now - ts) / (EASE_HOURS * 3600))
        for name in zones:
            reading = batch[f"{name} Sensor"]
            reading["temperature_c"] = round(reading["temperature_c"] + k * k * (live_c - last[name]), 1)
        store.record_many(int(ts), batch)
    print(f"{args.hours:.0f} h of demo history written to {store.db_path}")


if __name__ == "__main__":
    main()
