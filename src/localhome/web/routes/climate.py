"""The thermostat: the zones as the home page shows them, the season,
away, a zone changed by hand for a while, each zone's weekly schedule,
and the hours each zone ran and its temperatures' days."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, request

from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import OUTDOOR_SENSOR_NAME, setup_todo
from localhome.services.dehumidifier import DehumidifierTimer
from localhome.services.dewpoint import dew_point_c
from localhome.services.history import RUN_YEARS, HistoryStore, temp_days_of
from localhome.services.interlock import InterlockController
from localhome.services import system_status
from localhome.services.system_clock import local_after
from localhome.services.thermostat import MODES, ThermostatController, target_tier
from localhome.services import run_hours
from localhome.services.vmc_schedule import AWAY_CHOICES, VmcScheduleController
from localhome.web.routes.common import _fail, _json_body, _refusal, _who, logger


def _valve_driver(manager: DeviceManager, link) -> dict | None:
    if link is None:
        return None
    reading = manager.sensor_reading(link.leader)
    return {"name": link.leader, "ok": bool(reading.get("ok")), "on": bool(reading.get("ok") and reading.get("is_on"))}


AWAY_MAX_HOURS = 24 * 366
# A zone's temporary target (see api_climate_set_override): what the
# dashboard offers - a few hours (up to a day), until the schedule's next
# change, or until it's changed back by hand.
OVERRIDE_MAX_HOURS = 24
OVERRIDE_MIN_C, OVERRIDE_MAX_C = 5, 35


def _valve_zones(thermostat) -> list[dict]:
    """The zones with a valve, for their hours: {"name", "valve"}."""
    if thermostat is None:
        return []
    return [{"name": z.name, "valve": z.valve} for z in thermostat.zones if z.valve_configured]


def _sensor_zones(thermostat) -> list[dict]:
    """The zones with a sensor, for their temperatures' days: {"name", "sensor"}."""
    if thermostat is None:
        return []
    return [{"name": z.name, "sensor": z.sensor} for z in thermostat.zones if z.sensor_configured]


def register(app: Flask) -> None:
    """The thermostat's endpoints."""
    @app.route("/api/climate/zones")
    def api_climate_zones():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return jsonify(ok=True, mode=None, zones=[])
        manager: DeviceManager = app.config["MANAGER"]
        now = datetime.now()
        mode = thermostat.schedule_store.get_mode()
        # follower valve -> the interlock driving it in the current mode
        valve_drivers = {
            link.follower: link for link in app.config["INTERLOCK_LINKS"]
            if link.active_in_mode in (None, mode)
        }
        # For a sensor not heard from since the board came up - see sensor_waiting.
        uptime = system_status.board_uptime_seconds()
        zones = []
        # None while away: it holds every zone off (see the thermostat's tick).
        away_until = thermostat.schedule_store.get_away_until()
        overrides = {} if away_until else thermostat.schedule_store.get_overrides(now)
        for zone in thermostat.zones:
            sensor_reading = manager.sensor_reading(zone.sensor)
            override = overrides.get(zone.name)
            scheduled = thermostat.schedule_store.get_target(zone.name, now)
            target_now = override["target"] if override else scheduled
            opens_at, stops_at = thermostat.thresholds(target_now, mode) or (None, None)
            valve_reading = manager.sensor_reading(zone.valve)
            temperature_c = sensor_reading.get("temperature_c")
            humidity_pct = sensor_reading.get("humidity_pct")
            # humidity_pct == 0 is a broken/uncalibrated reading, not
            # "bone dry air" - math.log(0) would raise anyway, so this
            # also protects the endpoint from a single bad sensor value
            # taking the whole response down.
            dew_point = dew_point_c(temperature_c, humidity_pct) if temperature_c is not None and humidity_pct else None
            zones.append({
                "name": zone.name,
                "sensor": zone.sensor,
                "valve": zone.valve,
                "cool_enabled": zone.cool_enabled,
                "temperature_c": temperature_c,
                "humidity_pct": humidity_pct,
                "dew_point_c": dew_point,
                # Not shown for an H&T powered over USB-C - which is said instead.
                "battery_pct": None if sensor_reading.get("external_power") is True else sensor_reading.get("battery_pct"),
                "external_power": sensor_reading.get("external_power") is True,
                "wifi_rssi_dbm": sensor_reading.get("wifi_rssi_dbm"),
                "sensor_ok": sensor_reading.get("ok", False),
                # Its Shelly still to configure: no device to read or drive -
                # the cards say so instead of a fault.
                "sensor_configured": zone.sensor_configured,
                "valve_configured": zone.valve_configured,
                # Tagged on the zone's card ("simulated") - see static/home/zones.js.
                "sensor_simulated": manager.driver_types.get(zone.sensor) == "simulated",
                # A value typed by hand (the admin panel's "manual" mode):
                # the home page lets it be set from the zone's card.
                "sensor_manual": manager.driver_types.get(zone.sensor) == "manual_override",
                # Not heard from since the board came up, in its first
                # hours: "waiting for the first report", its last value
                # (from before the power cut) still shown.
                "sensor_waiting": not sensor_reading.get("ok", False) and system_status.waiting_since_boot(
                    sensor_reading.get("seconds_since_update"), sensor_reading.get("error"), uptime),
                "sensor_seconds_since_update": sensor_reading.get("seconds_since_update"),
                "sensor_error": sensor_reading.get("error"),
                # Set only for the "every MQTT topic went stale, but at
                # least one of them had a last-known value" fallback
                # (drivers/mqtt/sensor.py) - lets the dashboard show
                # "last known: X, stale for Yh" instead of blanking a
                # reading it still actually has.
                "sensor_last_value_age_seconds": sensor_reading.get("last_value_age_seconds"),
                "valve_on": valve_reading.get("is_on", False),
                "valve_ok": valve_reading.get("ok", False),
                "valve_seconds_since_update": valve_reading.get("seconds_since_update"),
                "valve_error": valve_reading.get("error"),
                # Not heard from since the board came up, in its first
                # minutes: "waiting for the valve", not yet "not answering".
                "valve_waiting": not valve_reading.get("ok", False) and system_status.waiting_since_boot(
                    valve_reading.get("seconds_since_update"), valve_reading.get("error"), uptime,
                    window=system_status.VALVE_FRESH_BOOT_SECONDS),
                # What the zone is heading for now: a temporary target
                # set from the dashboard if there is one, else the schedule's.
                "target_c": target_now,
                "scheduled_target_c": scheduled,
                # Its level and where its valve opens again and stops -
                # what the zone's card says (services/thermostat.py).
                "tier": target_tier(target_now, mode),
                "valve_opens_at": opens_at,
                "valve_stops_at": stops_at,
                "override": {"target": override["target"],
                             "until": override["until"].isoformat() if override["until"] else None} if override else None,
                "sensor_failsafe_active": thermostat.is_failsafe_active(zone.name),
                "sensor_unreachable_hours": thermostat.unreachable_hours(zone.name),
                "sensor_failsafe_hours": thermostat.sensor_failsafe_hours,
                # Set when an interlock drives this zone's valve right now
                # (e.g. a dehumidifier fed through a bathroom valve that's
                # excluded from cooling) - the dashboard's "why is it open"
                # for a valve the thermostat itself leaves alone.
                "valve_driver": _valve_driver(manager, valve_drivers.get(zone.valve)),
            })
        return jsonify(
            ok=True,
            mode=thermostat.schedule_store.get_mode(),
            # Who set the season last, and when - the admin's season card.
            mode_changed=thermostat.schedule_store.get_mode_changed(),
            away_until=away_until.isoformat() if away_until else None,
            diagnostic_mode=thermostat.schedule_store.get_diagnostic_mode(),
            # The valve stops at the target and opens again this far
            # from it - an economy target that much more on the saving side.
            hysteresis_c=thermostat.hysteresis_c,
            economy_tolerance_c=thermostat.economy_tolerance_c,
            # What the season does besides the zones: every switch that
            # follows it, and what it does in each season.
            season_effects={"rules": app.config["SEASON_RULES"](), "links": app.config["SEASON_LINKS"]()},
            # On by hand for a while, never by itself - see services/dehumidifier.py.
            dehumidifier=app.config["DEHUMIDIFIER"].status() if app.config.get("DEHUMIDIFIER") else None,
            # The Shellys still without an id, as saved in the admin panel.
            to_configure=setup_todo(app.config["LOCALHOME_CONFIG"], app.config["CLIMATE_MAP"]),
            zones=zones,
        )

    @app.route("/api/climate/mode", methods=["POST"])
    def api_climate_set_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        mode = data.get("mode")
        if mode not in ("heat", "cool"):
            return jsonify(ok=False, error="mode must be 'heat' or 'cool'"), 400
        previous = thermostat.schedule_store.get_mode()
        by = _who(request)
        thermostat.schedule_store.set_mode(mode, by=by)
        if previous != mode:
            logger.info("Season set to %s (was %s) from %s", mode, previous, by)
        # Re-evaluate right away instead of leaving valves showing stale
        # state until the background loops' next scheduled tick - a mode
        # switch changes every zone's heat/cool logic at once, and the
        # mode switches (a bypass valve) and mode-limited switches (a
        # dehumidifier) with it.
        thermostat.tick()
        interlock: InterlockController | None = app.config["INTERLOCK"]
        if interlock is not None:
            interlock.tick()
        return jsonify(ok=True)

    def _away_changed(thermostat: ThermostatController) -> None:
        """Every valve, and the dehumidifier with the valve it draws its
        water through, now - not at the loops' next tick."""
        thermostat.tick()
        interlock: InterlockController | None = app.config["INTERLOCK"]
        if interlock is not None:
            interlock.tick()
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is not None:
            timer.tick()

    @app.route("/api/climate/away", methods=["POST"])
    def api_climate_set_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        now = datetime.now()
        # What the VMC does meanwhile: night or off, as picked in the away
        # sheet - the last choice if none comes ("keep": not touched, for
        # tools/live_walk.py).
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        vmc_choice = data.get("vmc", vmc.store.get()["away_choice"])
        if vmc_choice not in AWAY_CHOICES:
            return _fail("away_vmc_invalid", f"vmc must be one of {', '.join(AWAY_CHOICES)}")
        if "until" in data:
            # The day back, from the home page's calendar: the board's own
            # time, "2026-10-15T18:00".
            try:
                until = datetime.fromisoformat(data["until"]).replace(tzinfo=None, second=0, microsecond=0)
            except (TypeError, ValueError):
                until = None
            if until is None or not now < until <= now + timedelta(hours=AWAY_MAX_HOURS):
                return _fail("away_until_invalid", "until must be a time ahead, within a year")
        else:
            hours = data.get("hours")
            # Up to a year: beyond that the date overflows (a 500), and no
            # absence the dashboard offers comes anywhere near it.
            if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 0 < hours <= AWAY_MAX_HOURS:
                return _fail("away_hours_invalid", f"hours must be a number between 0 and {AWAY_MAX_HOURS}", max=AWAY_MAX_HOURS)
            # Days on the wall clock: "a day" ends at the same time
            # tomorrow, the clocks changing in between or not.
            until = now + timedelta(hours=hours)
        thermostat.schedule_store.set_away_until(until)
        _away_changed(thermostat)
        vmc.go_away(vmc_choice)
        return jsonify(ok=True, away_until=until.isoformat(), vmc=vmc_choice)

    @app.route("/api/climate/zones/<name>/override", methods=["POST"])
    def api_climate_set_override(name):
        """A zone's schedule set aside for a while - {"target": number or
        null (off), "hours": 0.5-24}, {"target": ..., "until":
        "next_change"} or {"target": ..., "until": "manual"} (until it's
        changed back - or the season changes). Gone by itself when the
        time is up; see ScheduleStore's temporary targets."""
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        zone = next((z for z in thermostat.zones if z.name == name), None)
        if zone is None:
            return _fail("zone_unknown", f"no zone named {name!r}", 404, zone=name)
        if thermostat.schedule_store.get_away_until() is not None:
            # Away holds every zone off until it's ended ("I'm back").
            return _fail("override_while_away", "away mode is on", 409, zone=name)
        mode = thermostat.schedule_store.get_mode()
        if mode == "cool" and not zone.cool_enabled:
            return _fail("override_zone_excluded", f"{name!r} is left out of cooling", 409, zone=name)
        data = _json_body()
        target = data.get("target")
        if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))
                                   or not math.isfinite(target) or not OVERRIDE_MIN_C <= target <= OVERRIDE_MAX_C):
            return _fail("override_target_range", f"target must be off (null) or {OVERRIDE_MIN_C}-{OVERRIDE_MAX_C}",
                         min=OVERRIDE_MIN_C, max=OVERRIDE_MAX_C)
        now = datetime.now()
        if data.get("until") == "next_change":
            # A schedule that never changes: a day, like the longest duration.
            until = thermostat.schedule_store.next_change(name, now) or now + timedelta(hours=OVERRIDE_MAX_HOURS)
        elif data.get("until") == "manual":
            until = None
        else:
            hours = data.get("hours")
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= OVERRIDE_MAX_HOURS:
                return _fail("override_hours_invalid", f"hours must be above 0 and at most {OVERRIDE_MAX_HOURS}",
                             max=OVERRIDE_MAX_HOURS)
            until = local_after(now, hours * 3600)   # real hours, the night the clocks change too
        thermostat.schedule_store.set_override(name, float(target) if target is not None else None, until)
        thermostat.tick()
        return jsonify(ok=True, override={"target": target, "until": until.isoformat() if until else None})

    @app.route("/api/climate/zones/<name>/override/cancel", methods=["POST"])
    def api_climate_cancel_override(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        thermostat.schedule_store.clear_override(name)
        thermostat.tick()
        return jsonify(ok=True)

    @app.route("/api/climate/away/cancel", methods=["POST"])
    def api_climate_cancel_away():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        thermostat.schedule_store.set_away_until(None)
        _away_changed(thermostat)
        app.config["VMC_SCHEDULE"].tick()      # the VMC back as it was, now
        return jsonify(ok=True)

    @app.route("/api/climate/diagnostic-mode", methods=["POST"])
    def api_climate_set_diagnostic_mode():
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify(ok=False, error="'enabled' must be true or false"), 400
        thermostat.schedule_store.set_diagnostic_mode(enabled)
        if not enabled:
            # Resume real control immediately, same "don't wait for the
            # next scheduled tick" idiom as every other setter here -
            # otherwise the panel would show every valve frozen in
            # whatever the installer last left it at for up to a full
            # poll_interval_seconds after switching back to normal.
            # forget_valve_state() + resync_after_diagnostic_mode() first:
            # a manual toggle made while diagnostic mode was on wouldn't
            # otherwise be noticed by _apply()'s "already matches"
            # shortcut, and a zone whose sensor is currently unreachable
            # wouldn't be revisited by tick() at all (it skips such a
            # zone outright) - see both methods' docstrings.
            thermostat.forget_valve_state()
            thermostat.resync_after_diagnostic_mode()
            thermostat.tick()
            interlock: InterlockController | None = app.config["INTERLOCK"]
            if interlock:
                interlock.tick()
        return jsonify(ok=True, enabled=enabled)

    # Each season has its own grid (services/thermostat.py's SEASON_KEYS);
    # `mode` picks which one, defaulting to the current season so a
    # client that predates the split still reads/writes the live grid.
    @app.route("/api/climate/zones/<name>/schedule")
    def api_climate_get_schedule(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        mode = request.args.get("mode") or thermostat.schedule_store.get_mode()
        if mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        return jsonify(ok=True, mode=mode, week=thermostat.schedule_store.get_week(name, mode))

    @app.route("/api/climate/zones/<name>/schedule", methods=["POST"])
    def api_climate_set_schedule(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        data = _json_body()
        week, cells = data.get("week"), data.get("cells")
        if not isinstance(week, dict) and cells is None:
            return jsonify(ok=False, error="missing 'week' or 'cells'"), 400
        mode = data.get("mode")
        if mode is not None and mode not in MODES:
            return jsonify(ok=False, error=f"mode must be one of {MODES}"), 400
        if thermostat.schedule_store.get_away_until() is not None:
            # Away: nothing changes until "I'm back" - the week neither.
            return _fail("schedule_while_away", "away mode is on", 409, zone=name)
        if name in thermostat.schedule_store.get_overrides(datetime.now()):
            # Held by hand: its week changes once it's back to it - an edit
            # now would change nothing anyone sees, "back" included.
            return _fail("schedule_while_manual", f"{name!r} is held by hand", 409, zone=name)
        try:
            if cells is not None:
                # Just the hours that changed, merged into the week as it is
                # now - see ScheduleStore.set_cells().
                week = thermostat.schedule_store.set_cells(name, cells, mode)
            else:
                thermostat.schedule_store.set_week(name, week, mode)
        except ValueError as exc:
            return _refusal(exc, 400)
        # Same reasoning as the mode switch above: don't leave the valve
        # (and the status label derived from it) stale until the next
        # scheduled tick just because the new target was written to disk.
        thermostat.tick()
        return jsonify(ok=True, week=thermostat.schedule_store.get_week(name, mode))

    @app.route("/api/climate/run-hours")
    def api_climate_run_hours():
        """Each zone's hours open - the last 24 hours, each of the last 30
        days - and the plant's (any zone open, counted once)."""
        report = run_hours.zone_report(app.config["HISTORY"], _valve_zones(app.config["THERMOSTAT"]),
                                       app.config["HISTORY_POLL_INTERVAL"])
        return jsonify(ok=True, **report)

    @app.route("/api/climate/temps")
    def api_climate_temps():
        """Each zone's lowest and highest of each of the last 7 days, and
        when - today's live in /api/sensors' day_ranges."""
        report = run_hours.temp_report(app.config["HISTORY"], _sensor_zones(app.config["THERMOSTAT"]),
                                       app.config["HISTORY_POLL_INTERVAL"])
        return jsonify(ok=True, **report)

    @app.route("/api/climate/outdoor")
    def api_climate_outdoor():
        """The outdoor sensor's days as kept - six years of each day's mean,
        lowest and highest and when, today's so far - for the home page's
        outdoor sheet: days, weeks, months and years are made from them
        there. No outdoor sensor (none, or one still to configure): said,
        with whatever days there are - nothing to fail on."""
        manager: DeviceManager = app.config["MANAGER"]
        first = date(date.today().year - RUN_YEARS + 1, 1, 1)
        days = temp_days_of(app.config["HISTORY"], OUTDOOR_SENSOR_NAME, "temperature_c",
                            app.config["HISTORY_POLL_INTERVAL"], first)
        return jsonify(ok=True, configured=OUTDOOR_SENSOR_NAME in manager.pollers,
                       days=[{"day": day, "mean": mean, "min": low, "max": high, "min_at": low_at, "max_at": high_at}
                             for day, (mean, low, high, low_at, high_at) in sorted(days.items())])

    @app.route("/api/climate/zones/<name>/on-hours")
    def api_climate_on_hours(name):
        thermostat: ThermostatController | None = app.config["THERMOSTAT"]
        if thermostat is None:
            return _fail("no_thermostat", "no thermostat configured", 404)
        zone = next((z for z in thermostat.zones if z.name == name), None)
        if zone is None:
            return jsonify(ok=False, error=f"unknown zone '{name}'"), 404
        store: HistoryStore = app.config["HISTORY"]
        stats = store.on_stats(zone.valve, "is_on", app.config["HISTORY_POLL_INTERVAL"])
        return jsonify(
            ok=True,
            **{key: round(value["hours"], 2) for key, value in stats.items()},
            starts={key: value["starts"] for key, value in stats.items()},
        )
