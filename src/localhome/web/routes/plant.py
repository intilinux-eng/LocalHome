"""The plant's own devices beyond the zones: the VMC (its speeds, its
week, a boost, its filters, the automatic program) and the dehumidifier
(its timer and settings) - the residents' controls and the panel's."""
from __future__ import annotations


from flask import Flask, jsonify

from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import OUTDOOR_SENSOR_NAME, measured_outdoor
from localhome.services.dehumidifier import DehumidifierSettingsStore, DehumidifierTimer
from localhome.errors import InputError
from localhome.services import vmc_speeds
from localhome.services import vmc_auto
from localhome.services.vmc_auto import VmcAutoSettingsStore
from localhome.services.vmc_schedule import VmcScheduleController
from localhome.web.routes.common import _fail, _json_body, _refusal, _wake_poller, logger


def _vmc_speed_device(manager: DeviceManager) -> str | None:
    """The VMC's speed: the level paired with a switch that has speed
    presets - the same one the home page's VMC card shows."""
    for name, switch in manager.paired_switch.items():
        if name in manager.numbers and (manager.ui_options.get(name) or {}).get("presets") and switch:
            return name
    return None


def _auto_zones(thermostat) -> list[dict]:
    """The zones with a sensor, for the VMC's adjustments to go by."""
    if thermostat is None:
        return []
    return [{"name": z.name, "sensor": z.sensor, "cool_enabled": z.cool_enabled}
            for z in thermostat.zones if z.sensor_configured]


def _vmc_auto_answer(app: Flask) -> dict:
    """The VMC's adjustments for the admin panel: the settings and their
    defaults, the zones they can go by and the ones they go by now, what
    they'd need that isn't there, and the one under way."""
    manager: DeviceManager = app.config["MANAGER"]
    settings = app.config["VMC_AUTO_SETTINGS"].get()
    zones = _auto_zones(app.config["THERMOSTAT"])
    bathroom, reference = vmc_auto.zones_for(settings, zones)
    return {"settings": settings, "defaults": vmc_auto.DEFAULTS, "ranges": vmc_auto.RANGES,
            "zones": [{"name": z["name"], "cool_enabled": z["cool_enabled"]} for z in zones],
            "bathroom": bathroom["name"] if bathroom else None, "reference": reference["name"] if reference else None,
            "vmc": _vmc_speed_device(manager) is not None,
            # A simulated outdoor counts as none for them - said apart, as
            # it's on the page all the same.
            "outdoor": measured_outdoor(manager) is not None,
            "outdoor_simulated": manager.driver_types.get(OUTDOOR_SENSOR_NAME) == "simulated",
            "speeds": _vmc_speeds_in_use(app, _vmc_speed_device(manager)),
            "under_way": app.config["VMC_SCHEDULE"].status().get("auto")}


def _speeds_in_use(manager: DeviceManager, store: vmc_speeds.VmcSpeedStore, device: str | None) -> dict:
    """The VMC's five speeds in use: the device's presets, with those set
    in the admin panel over them."""
    presets = (manager.ui_options.get(device) or {}).get("presets") or [] if device else []
    return vmc_speeds.of_presets(vmc_speeds.apply(presets, store.get()))


def _vmc_speeds_in_use(app: Flask, device: str | None) -> dict:
    manager: DeviceManager = app.config["MANAGER"]
    return _speeds_in_use(manager, app.config["VMC_SPEEDS"], device)


def _move_vmc_to_new_speed(manager: DeviceManager, device: str | None, moves: dict) -> None:
    """A unit running at an old speed goes to its new value - one that's
    off stays off (setting a speed would switch it on)."""
    switch = manager.paired_switch.get(device) if device else None
    if not device or (switch and manager.sensor_reading(switch).get("is_on") is not True):
        return
    current = manager.sensor_reading(device).get("value")
    target = moves.get(round(current)) if isinstance(current, (int, float)) else None
    if target is None or target == round(current):
        return
    try:
        manager.numbers[device].set_value(target)
    except Exception as exc:
        logger.warning("VMC speeds: couldn't move %s from %s%% to %s%% (%s)", device, current, target, exc)
        return
    manager.pollers[device].cache.merge({"value": target})
    _wake_poller(manager, device)


def register(app: Flask) -> None:
    """The VMC's and the dehumidifier's endpoints."""
    def vmc_answer(vmc: VmcScheduleController, schedule: dict | None = None):
        return jsonify(ok=True, schedule=schedule or vmc.store.get(), status=vmc.status(),
                       filters=app.config["VMC_FILTERS"].status())

    @app.route("/api/vmc-filters/cleaned", methods=["POST"])
    def api_vmc_filters_cleaned():
        """The filters cleaned today - from the home card's reminder or the
        admin panel: the reminder starts over."""
        return jsonify(ok=True, filters=app.config["VMC_FILTERS"].cleaned_today())

    @app.route("/api/climate-admin/vmc-filters")
    def api_vmc_filters_get():
        return jsonify(ok=True, filters=app.config["VMC_FILTERS"].status())

    @app.route("/api/climate-admin/vmc-filters", methods=["POST"])
    def api_vmc_filters_save():
        """{"interval_days"} and/or {"cleaned_on": "2026-10-12" or null}:
        how often, and when they were last cleaned if not today."""
        body = {k: v for k, v in _json_body().items() if k in ("interval_days", "cleaned_on")}
        try:
            return jsonify(ok=True, filters=app.config["VMC_FILTERS"].set(body))
        except InputError as exc:
            return _refusal(exc, 400)

    @app.route("/api/vmc-schedule")
    def api_vmc_schedule_get():
        """The ventilation unit's weekly program, read and written from
        its own card on the dashboard - it's a daily control, not part of
        the installation the admin panel configures."""
        return vmc_answer(app.config["VMC_SCHEDULE"])

    @app.route("/api/vmc-schedule", methods=["POST"])
    def api_vmc_schedule_save():
        """Only what the body names changes: {"enabled", "target"} turns it
        on or off, {"week"} replaces the week."""
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        if vmc.holding_away():
            return _fail("vmc_away", "the VMC's program changes after coming back home", 409)
        body = {k: v for k, v in _json_body().items() if k in ("enabled", "target", "week")}
        try:
            schedule = vmc.store.update(body)
        except ValueError as exc:
            return _refusal(exc, 400)
        # Applied straight away, so saving is the whole action - no
        # restart, and no waiting for the next tick to see it work.
        vmc.tick()
        return vmc_answer(vmc, schedule)

    @app.route("/api/vmc-schedule/boost", methods=["POST"])
    def api_vmc_boost():
        """{"device", "value", "minutes"}: that speed for that long, then
        back to the program - or to how the unit was, with it off."""
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        if vmc.holding_away():
            return _fail("vmc_away", "no timed speed while away", 409)
        body = _json_body()
        try:
            value = body.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
                raise InputError("vmc_override_invalid", "a speed must be between 0 and 100")
            vmc.boost(str(body.get("device") or ""), round(value), body.get("minutes"))
        except ValueError as exc:
            return _refusal(exc, 400)
        return vmc_answer(vmc)

    @app.route("/api/dehumidifier")
    def api_dehumidifier():
        """The dehumidifier switched on by hand for a while: on until when,
        the reference humidity, the thresholds - None without one."""
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        return jsonify(ok=True, dehumidifier=timer.status() if timer else None)

    @app.route("/api/dehumidifier/on", methods=["POST"])
    def api_dehumidifier_on():
        """{"hours": one of the offered durations} or {"until": "target"}:
        on for that long, or until the room is dry enough. Refused (409)
        with the air dry enough, or out of its season."""
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is None:
            return _fail("no_dehumidifier", "no dehumidifier configured", 404)
        if app.config["HELD_OFF_WHILE_AWAY"](timer.switch):
            return _fail("dehumidifier_away", "away mode is on", 409)
        body = _json_body()
        try:
            status = timer.switch_on(body.get("hours"), until=body.get("until"))
        except InputError as exc:
            return _refusal(exc, 400 if exc.code == "dehumidifier_hours_invalid" else 409)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        return jsonify(ok=True, dehumidifier=status)

    @app.route("/api/dehumidifier/settings")
    def api_dehumidifier_settings():
        """Its thresholds and the ways it can be switched on - the admin
        panel's; the defaults, and the durations one can offer."""
        from localhome.services import dehumidifier as dehum
        store: DehumidifierSettingsStore = app.config["DEHUMIDIFIER_SETTINGS"]
        return jsonify(ok=True, settings=store.get(), defaults=dehum.DEFAULTS, ranges=dehum.RANGES,
                       duration_choices=list(dehum.DURATION_CHOICES), configured=app.config["DEHUMIDIFIER"] is not None)

    @app.route("/api/dehumidifier/settings", methods=["POST"])
    def api_dehumidifier_settings_save():
        """Only what the body names changes; `{"reset": true}` puts the
        defaults back. Takes effect at once - no restart."""
        store: DehumidifierSettingsStore = app.config["DEHUMIDIFIER_SETTINGS"]
        body = _json_body()
        try:
            if body.get("reset") is True:
                from localhome.services.dehumidifier import DEFAULTS
                settings = store.set(dict(DEFAULTS))
            else:
                settings = store.set(body)
        except InputError as exc:
            return _refusal(exc, 400)
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is not None:
            timer.tick()        # a lower stop threshold switches it off now, not in 30 s
        return jsonify(ok=True, settings=settings, dehumidifier=timer.status() if timer else None)

    @app.route("/api/dehumidifier/off", methods=["POST"])
    def api_dehumidifier_off():
        timer: DehumidifierTimer | None = app.config["DEHUMIDIFIER"]
        if timer is None:
            return _fail("no_dehumidifier", "no dehumidifier configured", 404)
        try:
            status = timer.switch_off()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 502
        return jsonify(ok=True, dehumidifier=status)

    @app.route("/api/vmc-schedule/boost/cancel", methods=["POST"])
    def api_vmc_boost_cancel():
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        vmc.cancel_boost()
        return vmc_answer(vmc)

    @app.route("/api/climate-admin/vmc-speeds")
    def api_vmc_speeds_get():
        """The VMC's night/day/boost speeds in use, and whether they were
        set here (else they're its device's own)."""
        device = _vmc_speed_device(app.config["MANAGER"])
        saved = app.config["VMC_SPEEDS"].get()
        return jsonify(ok=True, device=device, saved=saved is not None, speeds=_vmc_speeds_in_use(app, device))

    @app.route("/api/climate-admin/vmc-speeds", methods=["POST"])
    def api_vmc_speeds_save():
        """New speeds, in use at once: the buttons and programs show them,
        a program's hours move to them (its night stays night), and a
        unit running at one of the old speeds moves to its new value."""
        manager: DeviceManager = app.config["MANAGER"]
        vmc: VmcScheduleController = app.config["VMC_SCHEDULE"]
        device = _vmc_speed_device(manager)
        old = _vmc_speeds_in_use(app, device)
        try:
            new = app.config["VMC_SPEEDS"].set(_json_body())
        except ValueError as exc:
            return _refusal(exc, 400)
        if old:
            moves = {old[k]: new[k] for k in vmc_speeds.SPEED_KEYS if k in old}
            schedule = vmc.store.get()
            week = vmc_speeds.remap_week(schedule["week"], old, new)
            if week != schedule["week"]:
                vmc.store.update({"week": week})
            vmc.speeds_changed(moves)
            _move_vmc_to_new_speed(manager, device, moves)
        return jsonify(ok=True, device=device, saved=True, speeds=new)

    @app.route("/api/climate-admin/vmc-auto")
    def api_vmc_auto_get():
        return jsonify(ok=True, **_vmc_auto_answer(app))

    @app.route("/api/climate-admin/vmc-auto", methods=["POST"])
    def api_vmc_auto_save():
        """Only what the body names changes; `{"reset": true}` puts the
        defaults back. Takes effect at once - no restart."""
        store: VmcAutoSettingsStore = app.config["VMC_AUTO_SETTINGS"]
        body = _json_body()
        try:
            if body.get("reset") is True:
                store.set(dict(vmc_auto.DEFAULTS))
            else:
                names = {z["name"] for z in _auto_zones(app.config["THERMOSTAT"])}
                for key in vmc_auto.ZONE_KEYS:
                    if isinstance(body.get(key), str) and body[key] not in names:
                        raise InputError("vmc_auto_zone_unknown", f"{key}: no zone named {body[key]!r}",
                                         zone=body[key])
                store.set(body)
        except InputError as exc:
            return _refusal(exc, 400)
        app.config["VMC_SCHEDULE"].tick()     # a change shows on the unit now, not in a minute
        return jsonify(ok=True, **_vmc_auto_answer(app))
