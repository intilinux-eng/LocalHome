"""What the Telegram bot says and does (services/telegram_bot.py reads the
messages and sends the answers): the home at a glance, as its page shows
it, and the simple commands - a zone's temperature, away and back, the
VMC's speed, the dehumidifier - typed, or by the buttons under an answer.

Every command goes through LocalHome's own endpoints, in-process: the
same checks and refusals as the home page's buttons, said in the pages'
own words (home.*, errors.<code>) in the board's language. What a zone
is doing is the home page's rule, ported from static/home/zones.js
(zoneStatus) and hero.js (renderHero, zoneLine) - change them together.

Buttons carry short data back when pressed, "<what>|<detail>": o the
overview; z|<zone>[|+ - on off auto]; a[|<hours>] away, b back; v[|set
<value>, on, off, boost <value> <minutes>, cancel] the VMC; d[|<hours>,
target, off] the dehumidifier; p the problems.
"""
from __future__ import annotations

import html
import logging
import math
import unicodedata
from datetime import datetime
from urllib.parse import quote

from localhome.services.telegram_bot import Reply

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# The home page's own choices (static/home/core.js).
AWAY_OPTIONS = (24, 72, 168)
DEFAULT_TARGET = {"heat": 21, "cool": 25}
TARGET_MIN, TARGET_MAX = 5, 35
STEP_C = 0.5
# A temperature by hand lasts until the schedule's next change - or this
# many hours without one, as the home page's first choice.
FIRST_HOURS = 4
VMC_BOOST_MINUTES = (30, 60, 120)
# Names the home page shows in place of a device's own (core.js).
DISPLAY_NAMES = {"Outdoor Sensor": "climate.outdoor_name", "Bathroom Bypass Valve": "climate.bypass_name",
                 "Dehumidifier": "climate.dehumidifier", "VMC": "home.vmc_name"}
OUTDOOR_SENSOR = "Outdoor Sensor"
BATTERY_SOON_PCT, BATTERY_NOW_PCT = 20, 10
# What a resident can make of a problem (hero.js); the rest is said as
# the admin panel says it, for whoever looks after the system.
RESIDENT_PROBLEMS = {"device_offline": "home.problem_offline", "integration_failed": "home.problem_offline",
                     "device_report_late": "home.problem_offline", "device_battery_low": "home.problem_battery",
                     "clock_unsynced": "home.problem_internet", "lan_unreachable": "home.problem_router"}
FOLLOWS_OFFLINE = {"device_reports_missed", "device_report_late"}
# The commands, by their English names - which always work; the board's
# language names them too (bot.commands in the locale files).
COMMANDS = ("status", "zone", "away", "back", "vmc", "dehumidifier", "problems", "help")
TONE_MARKS = {"active_heat": "🔥", "active_cool": "❄️", "ok": "🟢", "off": "⚪", "warn": "🟠", "bad": "🔴"}


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def _plain(word: str) -> str:
    """"Lunedì" and "lunedi", "STANZE" and "stanze" - the same word."""
    return "".join(c for c in unicodedata.normalize("NFD", word.casefold()) if unicodedata.category(c) != "Mn")


def _js_round(value: float) -> float:
    return math.floor(value * 10 + 0.5) / 10


def _when(iso) -> datetime | None:
    """A time from the API, the board's own wall clock."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def _number(word: str) -> float | None:
    try:
        value = float(word.replace(",", ".").rstrip("°c").rstrip("°"))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class Replies:
    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------ words --

    @property
    def _texts(self) -> dict:
        return self.app.config["TRANSLATIONS"]

    def tr(self, key: str, **params) -> str:
        node = self._texts
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        text = node if isinstance(node, str) else key
        for name, value in params.items():
            text = text.replace("{" + name + "}", str(value))
        return text

    def _words(self, key: str, english: tuple[str, ...]) -> set[str]:
        """A word typed after a command: English, or the board's language's
        (bot.words.<key>, comma-separated)."""
        return {_plain(w.strip()) for w in self.tr(f"bot.words.{key}").split(",") if w.strip()} | set(english)

    def fmt(self, value: float, digits: int = 1) -> str:
        text = f"{value:,.{digits}f}"
        if not str(self._texts.get("locale", "en")).lower().startswith("en"):
            text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
        return text

    def fmt_temp(self, value) -> str:
        return f"{self.fmt(value, 1)}°"

    def fmt_target(self, value) -> str:
        if value is None:
            return self.tr("home.off")
        return f"{self.fmt(value, 0 if float(value).is_integer() else 1)}°"

    def hhmm(self, when: datetime) -> str:
        return f"{when.hour:02d}:{when.minute:02d}"

    def until_text(self, when: datetime, now: datetime) -> str:
        days = (when.date() - now.date()).days
        if days == 0:
            return self.tr("home.until_at", time=self.hhmm(when))
        if days == 1:
            return self.tr("home.until_tomorrow", time=self.hhmm(when))
        return self.tr("home.until_day", day=self.tr(f"home.day_long.{DAYS[when.weekday()]}"), time=self.hhmm(when))

    def away_until_text(self, when: datetime, now: datetime) -> str:
        """Away can last weeks: a date past today, not just a weekday."""
        if when.date() == now.date():
            return self.until_text(when, now)
        months = (self._texts.get("home") or {}).get("months_long")
        month = months[when.month - 1] if isinstance(months, list) and len(months) == 12 else str(when.month)
        date = self.tr("home.date_at_this_year" if when.year == now.year else "home.date_at",
                       day=self.tr(f"home.day_long.{DAYS[when.weekday()]}"), date=when.day, month=month,
                       year=when.year, time=self.hhmm(when))
        return self.tr("home.until_when", when=date)

    def display_name(self, name: str) -> str:
        return self.tr(DISPLAY_NAMES[name]) if name in DISPLAY_NAMES else name

    def site(self) -> str:
        provider = self.app.config.get("SITE_NAME")
        return provider() if callable(provider) else "LocalHome"

    # ------------------------------------------------------ the endpoints --

    def _call(self, method: str, path: str, body: dict | None = None, who: str = "") -> dict:
        """One of LocalHome's own endpoints, in-process: past the web
        password (if any) - this request never came over the network - and
        logged as Telegram's, with whoever pressed."""
        client = self.app.test_client(use_cookies=False)
        resp = client.open(path, method=method, json=body if method == "POST" else None,
                           headers={"User-Agent": "LocalHome Telegram bot"},
                           environ_base={"REMOTE_ADDR": "127.0.0.1", "localhome.internal": True,
                                         "localhome.telegram_from": who})
        data = resp.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 400:
            data["ok"] = False
        data.setdefault("ok", resp.status_code < 400)
        return data

    def _get(self, path: str) -> dict:
        return self._call("GET", path)

    def _post(self, path: str, body: dict, who: str) -> dict:
        data = self._call("POST", path, body, who)
        # Who changed what from afar - the board's log keeps it.
        logger.info("Telegram (%s): %s %s - %s", who or "?", path, body,
                    "done" if data.get("ok") else data.get("code") or data.get("error"))
        return data

    def refusal(self, data: dict) -> str:
        if data.get("text"):
            return data["text"]
        code = data.get("code")
        if code:
            text = self.tr(f"errors.{code}", **(data.get("params") or {}))
            if text != f"errors.{code}":
                return text
        return self.tr("bot.failed", reason=data.get("error") or "?")

    # ------------------------------------------------------------- state --

    def _state(self) -> dict:
        climate = self._get("/api/climate/zones")
        devices = self._get("/api/devices").get("sensors") or []
        readings = self._get("/api/readings")
        vmc_speed = next((d for d in devices if d.get("kind") == "number" and d.get("paired_switch")
                          and d.get("presets")), None)
        vmc = self._get("/api/vmc-schedule") if vmc_speed else {}
        return {"climate": climate, "devices": devices, "readings": readings.get("readings") or {},
                "ranges": readings.get("day_ranges") or {}, "vmc_speed": vmc_speed, "vmc": vmc,
                "now": datetime.now()}

    def _thermostat(self):
        return self.app.config.get("THERMOSTAT")

    def _next_change(self, zone: str, now: datetime) -> datetime | None:
        thermostat = self._thermostat()
        if thermostat is None:
            return None
        try:
            return thermostat.schedule_store.next_change(zone, now)
        except (KeyError, ValueError):
            return None

    @staticmethod
    def zone_ready(zone: dict) -> bool:
        return zone.get("sensor_configured") is not False and zone.get("valve_configured") is not False

    @staticmethod
    def zone_excluded(zone: dict, climate: dict) -> bool:
        return climate.get("mode") == "cool" and zone.get("cool_enabled") is False

    def _setup_key(self, zone: dict, prefix: str) -> str:
        if zone.get("sensor_configured") is False and zone.get("valve_configured") is False:
            return f"{prefix}_both"
        return f"{prefix}_{'sensor' if zone.get('sensor_configured') is False else 'valve'}"

    @staticmethod
    def target_reached(zone: dict, climate: dict) -> bool:
        if zone.get("target_c") is None or zone.get("temperature_c") is None:
            return False
        h = climate.get("hysteresis_c") or 0.5
        gap = _js_round(zone["temperature_c"] - zone["target_c"])
        return -h <= gap <= 0 if climate.get("mode") == "cool" else 0 <= gap <= h

    def zone_status(self, zone: dict, climate: dict) -> dict:
        """The home page's zoneStatus(): {tone, text, active, reached}."""
        mode = climate.get("mode") or "heat"
        if not self.zone_ready(zone):
            return {"tone": "off", "text": self.tr(self._setup_key(zone, "home.st_setup"))}
        if mode == "cool" and not zone.get("cool_enabled", True):
            return {"tone": "off", "text": self.tr("home.st_excluded")}
        if zone.get("valve_ok") is False:
            return ({"tone": "warn", "text": self.tr("home.st_valve_waiting")} if zone.get("valve_waiting")
                    else {"tone": "bad", "text": self.tr("home.st_valve_offline")})
        if zone.get("sensor_failsafe_active"):
            return {"tone": "warn", "text": self.tr("home.st_failsafe")}
        if zone.get("sensor_manual") and not zone.get("sensor_ok"):
            return {"tone": "warn", "text": self.tr("home.st_manual_waiting")}
        if zone.get("sensor_waiting"):
            return {"tone": "off", "text": self.tr("home.st_first_report")}
        if not zone.get("sensor_ok") and zone.get("temperature_c") is None:
            return {"tone": "warn", "text": self.tr("home.st_waiting")}
        if not zone.get("sensor_ok"):
            return {"tone": "warn", "text": self.tr("home.st_stale")}
        if climate.get("away_until"):
            return {"tone": "off", "text": self.tr("home.st_away")}
        # Running as planned (`plain`): its target and until when follow.
        if zone.get("target_c") is None:
            return {"tone": "off", "plain": True, "text": self.tr("home.st_off")}
        if zone.get("valve_on"):
            return {"tone": f"active_{mode}", "active": True, "plain": True,
                    "text": self.tr("home.st_cooling" if mode == "cool" else "home.st_heating")}
        reached = self.target_reached(zone, climate)
        return {"tone": "ok", "reached": reached, "plain": True,
                "text": self.tr("home.st_reached" if reached else "home.st_idle")}

    def _controllable(self, zone: dict, climate: dict) -> bool:
        return self.zone_ready(zone) and not self.zone_excluded(zone, climate) and not climate.get("away_until")

    # ---------------------------------------------------------- overview --

    def overview(self) -> Reply:
        """The home at a glance, as the top of its page: what the plant is
        doing, outside, each zone, the dehumidifier and the VMC, the
        meters, what needs looking at."""
        s = self._state()
        climate, now = s["climate"], s["now"]
        zones = climate.get("zones") or []
        mode = climate.get("mode") or "heat"
        lines = [f"🏠 <b>{esc(self.site())}</b> · {esc(self.tr('home.season_cool' if mode == 'cool' else 'home.season_heat'))}",
                 self._hero(s)]
        outdoor = self._outdoor(s)
        if outdoor:
            lines += [""] + outdoor
        for zone in zones:
            lines += [""] + self._zone_lines(zone, climate, now)
        plant = [line for line in (self._dehumidifier_line(climate, now), self._vmc_line(s)) if line]
        plant += self._meter_lines(s)
        if plant:
            lines += [""] + plant
        problems = self._problem_list(zones)
        todo = climate.get("to_configure") or []
        lines.append("")
        if problems:
            lines.append(f"⚠️ <b>{esc(self.tr('home.health_problems', count=len(problems)))}</b>")
            lines += [f"• {esc(text)}" for text in problems.values()]
        elif not todo:
            lines.append(f"✅ {esc(self.tr('home.health_sheet_ok'))}")
        if todo:
            lines.append(f"🔧 {esc(self.tr('bot.to_configure', count=len(todo)))}")
        lines.append(f"<i>{esc(self.tr('bot.updated', time=self.hhmm(now)))}</i>")
        return Reply("\n".join(lines), self._overview_buttons(s))

    def _hero(self, s: dict) -> str:
        """renderHero()'s title, and while away what stays off."""
        climate, now = s["climate"], s["now"]
        zones = climate.get("zones") or []
        cool = climate.get("mode") == "cool"
        ready = [z for z in zones if self.zone_ready(z)]
        running = [z for z in ready if not self.zone_excluded(z, climate)]
        active = [z for z in running if self.zone_status(z, climate).get("active")]
        off = [z for z in running if z.get("target_c") is None]
        away = _when(climate.get("away_until"))
        if not zones:
            return f"<b>{esc(self.tr('home.no_zones'))}</b>"
        if not ready:
            return f"<b>{esc(self.tr('home.hero_setup'))}</b>"
        if away:
            title = f"🧳 <b>{esc(self.tr('home.hero_away', until=self.away_until_text(away, now)))}</b>"
            return f"{title}\n{esc(self._away_detail(s))}"
        if active:
            names = self._list([z["name"] for z in active])
            return f"{'❄️' if cool else '🔥'} <b>{esc(self.tr('home.hero_cooling' if cool else 'home.hero_heating', zones=names))}</b>"
        if running and len(off) == len(running):
            held = any(z.get("override") for z in off)
            key = f"home.{'hero_off_now' if held else 'hero_all_off'}_{'cool' if cool else 'heat'}"
            return f"<b>{esc(self.tr(key))}</b>"
        if running and not off and all(self.zone_status(z, climate).get("reached") for z in running):
            return f"✅ <b>{esc(self.tr('home.hero_all_ok'))}</b>"
        return f"<b>{esc(self.tr('home.hero_idle_cool' if cool else 'home.hero_idle_heat'))}</b>"

    def _away_detail(self, s: dict) -> str:
        climate = s["climate"]
        mode = climate.get("mode")
        names = {d.get("name") for d in s["devices"]}
        rules = (climate.get("season_effects") or {}).get("rules") or []
        devices = [self.display_name(r["switch"]) for r in rules if r.get(mode) == "manual" and r.get("switch") in names]
        held = ((s["vmc"] or {}).get("status") or {}).get("away")
        speed = s["vmc_speed"]
        vmc_name = self.display_name(speed["paired_switch"]) if held and speed else None
        if vmc_name and held.get("mode") == "off":
            devices.append(vmc_name)
        text = self.tr("home.hero_away_detail_devices" if devices else "home.hero_away_detail", devices=self._list(devices))
        if vmc_name and held.get("mode") != "off":
            text += " " + self.tr("home.hero_away_vmc_level", name=vmc_name, level=self.tr(f"vmc_presets.{held['mode']}"))
        return text

    def _list(self, names: list[str]) -> str:
        if len(names) < 2:
            return "".join(names)
        return f"{', '.join(names[:-1])} {self.tr('home.and')} {names[-1]}"

    def _outdoor(self, s: dict) -> list[str]:
        reading = s["readings"].get(OUTDOOR_SENSOR) or {}
        if reading.get("temperature_c") is None:
            return []
        device = next((d for d in s["devices"] if d.get("name") == OUTDOOR_SENSOR), {})
        parts = [f"🌡 {esc(self.tr('home.outside'))} <b>{self.fmt_temp(reading['temperature_c'])}</b>"]
        if reading.get("humidity_pct") is not None:
            parts.append(esc(self.tr("bot.humidity", rh=self.fmt(reading["humidity_pct"], 0))))
        today = (s["ranges"].get(OUTDOOR_SENSOR) or {}).get("temperature_c")
        if today and today.get("min") is not None and today.get("max") is not None:
            parts.append(esc(self.tr("bot.day_range", low=self.fmt_temp(today["min"]), high=self.fmt_temp(today["max"]))))
        if device.get("simulated"):
            parts.append(f"<i>{esc(self.tr('home.simulated'))}</i>")
        lines = [" · ".join(parts)]
        pct = reading.get("battery_pct")
        if pct is not None and not reading.get("external_power") and pct <= BATTERY_SOON_PCT:
            key = "home.battery_now" if pct <= BATTERY_NOW_PCT else "home.battery_soon"
            lines.append(f"🔋 {esc(self.tr(key, pct=round(pct)))}")
        return lines

    def _zone_head(self, zone: dict, climate: dict) -> str:
        status = self.zone_status(zone, climate)
        parts = [f"{TONE_MARKS.get(status['tone'], '⚪')} <b>{esc(zone['name'])}</b>"]
        if zone.get("temperature_c") is not None:
            parts.append(f"<b>{self.fmt_temp(zone['temperature_c'])}</b>")
        if zone.get("humidity_pct") is not None:
            parts.append(esc(self.tr("bot.humidity", rh=self.fmt(zone["humidity_pct"], 0))))
        if zone.get("sensor_simulated"):
            parts.append(f"<i>{esc(self.tr('home.simulated'))}</i>")
        return " · ".join(parts)

    def _zone_lines(self, zone: dict, climate: dict, now: datetime) -> list[str]:
        """Its head (name, temperature, humidity) and what it's doing: the
        status, then the target and until when - by hand or by the week."""
        status = self.zone_status(zone, climate)
        line = status["text"]
        override = zone.get("override")
        if status.get("plain"):
            until = self._override_until(override, now) if override else None
            change = None if override else self._next_change(zone["name"], now)
            if zone.get("target_c") is None:
                if override:
                    line += " · " + self.tr("bot.by_hand", until=until)
                elif change:
                    line += " " + self.until_text(change, now)
            else:
                target = self.fmt_target(zone["target_c"])
                if override:
                    line += " · " + self.tr("bot.target_by_hand", target=target, until=until)
                elif change:
                    line += " · " + self.tr("bot.target_until", target=target, until=self.until_text(change, now))
                else:
                    line += " · " + self.tr("bot.target", target=target)
        return [self._zone_head(zone, climate), esc(line)]

    def _override_until(self, override: dict, now: datetime) -> str:
        when = _when(override.get("until"))
        return self.until_text(when, now) if when else self.tr("home.until_changed")

    def _dehumidifier_line(self, climate: dict, now: datetime) -> str:
        d = climate.get("dehumidifier")
        if not d or d.get("on") is not True:
            return ""
        until = _when(d.get("until_at"))
        text = self.tr("home.line_dehum_until", until=self.until_text(until, now)) if until else self.tr("home.line_dehum")
        return f"💧 {esc(text)}"

    def _vmc_level(self, speed: dict, value) -> str:
        if value is None:
            return "--"
        if value == 0:
            return self.tr("home.vmc_off")
        preset = next((p for p in speed.get("presets") or [] if p.get("value") == value), None)
        name = self.tr(f"vmc_presets.{preset['key']}") if preset else self.tr("home.vmc_custom_level", pct=self.fmt(value, 0))
        return f"{name} ({self.fmt(value, 0)}%)" if preset else name

    def _vmc_line(self, s: dict) -> str:
        speed = s["vmc_speed"]
        if not speed:
            return ""
        status = (s["vmc"] or {}).get("status") or {}
        value = (s["readings"].get(speed["name"]) or {}).get("value")
        on = (s["readings"].get(speed["paired_switch"]) or {}).get("is_on") is not False
        name = esc(self.display_name(speed["paired_switch"]))
        now = s["now"]
        if status.get("away"):
            held = status["away"]
            level = self._vmc_level(speed, held.get("value"))
            text = self.tr("home.vmc_away_off") if held.get("mode") == "off" else self.tr("home.vmc_away_level", level=level)
        elif not on:
            text = self.tr("home.vmc_off")
        elif status.get("override"):
            boost = status["override"]
            until = _when(boost.get("until"))
            text = self.tr("home.vmc_boost_running", level=self._vmc_level(speed, boost.get("value")),
                           until=self.until_text(until, now) if until else "")
        else:
            text = self._vmc_level(speed, value)
            if status.get("enabled") and status.get("until"):
                text += " · " + self.tr("bot.vmc_next", level=self._vmc_level(speed, status.get("next_value")),
                                        time=status["until"])
        return f"💨 {name}: {esc(text)}"

    def _meter_lines(self, s: dict) -> list[str]:
        """The meters and the plugs that measure, as the home's energy
        tiles: power now, and today."""
        climate_devices = {n for z in s["climate"].get("zones") or [] for n in (z.get("sensor"), z.get("valve"))}
        speed = s["vmc_speed"]
        skip = climate_devices | ({speed["name"], speed["paired_switch"]} if speed else set())
        lines = []
        for device in s["devices"]:
            name = device.get("name")
            reading = s["readings"].get(name) or {}
            if name in skip or device.get("tab") == "climate" or reading.get("power_w") is None:
                continue
            if device.get("kind") not in ("power_meter", "switch"):
                continue
            today = reading.get("day_kwh")
            if today is None:
                today = ((self._get(f"/api/energy/{quote(name, safe='')}").get("today")) or {}).get("kwh")
            text = f"{self.fmt(reading['power_w'], 0)} W"
            if today is not None:
                text += " · " + self.tr("home.energy_today", kwh=self.fmt(today, 1))
            lines.append(f"⚡ {esc(self.display_name(name))}: {esc(text)}")
        return lines

    def _overview_buttons(self, s: dict) -> list:
        climate = s["climate"]
        rows = [[(f"🔄 {self.tr('bot.refresh')}", "o"),
                 (f"🏠 {self.tr('home.away_back')}", "b") if climate.get("away_until")
                 else (f"🧳 {self.tr('home.away_title')}", "a")]]
        zones = [z for z in climate.get("zones") or [] if self.zone_ready(z)]
        if zones:
            rows.append([(f"🌡 {z['name']}", f"z|{z['name']}") for z in zones[:4]])
        plant = []
        if s["vmc_speed"]:
            plant.append((f"💨 {self.display_name(s['vmc_speed']['paired_switch'])}", "v"))
        # Only in its season, as on the home page.
        if (climate.get("dehumidifier") or {}).get("allowed"):
            plant.append((f"💧 {self.tr('climate.dehumidifier')}", "d"))
        if plant:
            rows.append(plant)
        return rows

    # ----------------------------------------------------------- problems --

    def _subject_name(self, subject: str, zones: list[str]) -> str:
        for suffix, key in ((" Sensor", "home.subject_sensor"), (" Valve", "home.subject_valve")):
            if subject and subject.endswith(suffix) and subject[: -len(suffix)] in zones:
                return self.tr(key, zone=subject[: -len(suffix)])
        return self.display_name(subject or "")

    def problem_text(self, problem: dict, zones: list[str]) -> str:
        subject = self._subject_name(problem.get("subject") or "", zones)
        key = RESIDENT_PROBLEMS.get(problem.get("code"))
        if key:
            return self.tr(key, subject=subject)
        technical = self.tr(f"climate_admin.system_problem_{problem.get('code')}", subject=subject)
        if technical.startswith("climate_admin."):
            technical = str(problem.get("code"))
        return self.tr("home.problem_technical", what=technical)

    def _raw_problems(self) -> list[dict]:
        # The list the home page's badge and the LED matrix read.
        from localhome.web.routes.system import _quick_problems
        return _quick_problems(self.app)

    def _zone_names(self, zones: list[dict] | None = None) -> list[str]:
        if zones is not None:
            return [z.get("name") for z in zones]
        thermostat = self._thermostat()
        return [z.name for z in thermostat.zones] if thermostat is not None else []

    def _problem_list(self, zones: list[dict] | None = None, found: list[dict] | None = None) -> dict[str, str]:
        """{code|subject: what a resident reads}, as the home page's
        health sheet: a device that doesn't answer is one line."""
        found = self._raw_problems() if found is None else found
        found = [p for p in found if p.get("code") != "device_not_heard_yet"]
        offline = {p.get("subject") for p in found if RESIDENT_PROBLEMS.get(p.get("code")) == "home.problem_offline"}
        names = self._zone_names(zones)
        shown = {}
        for p in found:
            if p.get("code") in FOLLOWS_OFFLINE and p.get("subject") in offline:
                continue
            shown.setdefault(f"{p.get('code')}|{p.get('subject')}", self.problem_text(p, names))
        return shown

    def problems(self) -> tuple[dict[str, str], set[str]]:
        """For the bot's watch: the problems now, and the devices not
        heard from since the board came up (neither there nor solved)."""
        found = self._raw_problems()
        unknown = {p.get("subject") for p in found if p.get("code") == "device_not_heard_yet"}
        return self._problem_list(found=found), unknown

    def problems_message(self, new: list[str], solved: list[str]) -> str:
        lines = [f"🏠 <b>{esc(self.site())}</b>"]
        if new:
            lines.append(f"⚠️ <b>{esc(self.tr('bot.problems_new'))}</b>")
            lines += [f"• {esc(text)}" for text in new]
        if solved:
            lines.append(f"✅ <b>{esc(self.tr('bot.problems_solved'))}</b>")
            lines += [f"• {esc(text)}" for text in solved]
        return "\n".join(lines)

    def problems_reply(self) -> Reply:
        problems = self._problem_list()
        if not problems:
            return Reply(f"✅ {esc(self.tr('home.health_sheet_ok'))}", [[(f"🏠 {self.tr('bot.overview')}", "o")]])
        lines = [f"⚠️ <b>{esc(self.tr('home.health_problems', count=len(problems)))}</b>"]
        lines += [f"• {esc(text)}" for text in problems.values()]
        return Reply("\n".join(lines), [[(f"🏠 {self.tr('bot.overview')}", "o")]])

    def event(self, key: str, params: dict) -> str | None:
        """An alert from elsewhere (the power budget), in these words."""
        text = self.tr(f"bot.{key}", **{k: self.fmt(v, 0) if isinstance(v, (int, float)) else v
                                       for k, v in params.items()})
        if text == f"bot.{key}":
            return None
        return f"🏠 <b>{esc(self.site())}</b>\n⚡ {esc(text)}"

    def test_message(self) -> str:
        return f"✅ {esc(self.tr('bot.test_message', site=self.site()))}"

    # ------------------------------------------------------------ commands --

    def commands(self) -> list[dict]:
        """For Telegram's "/" menu, in the board's language."""
        return [{"command": self._command_name(c), "description": self.tr(f"bot.help.{c}")[:250]} for c in COMMANDS]

    def _command_name(self, command: str) -> str:
        name = self.tr(f"bot.commands.{command}")
        return name if name != f"bot.commands.{command}" else command

    def _command(self, word: str) -> str | None:
        word = _plain(word)
        if word == "start":
            return "status"
        for command in COMMANDS:
            if word in (command, _plain(self._command_name(command))):
                return command
        return None

    def help(self) -> Reply:
        lines = [f"<b>{esc(self.tr('bot.help_title', site=self.site()))}</b>", ""]
        for command in COMMANDS:
            lines.append(f"/{self._command_name(command)} - {esc(self.tr(f'bot.help.{command}'))}")
        lines += ["", esc(self.tr("bot.help_examples", zone=self._zone_names()[0] if self._zone_names() else "Stanze",
                                  away=self._command_name("away"), zone_cmd=self._command_name("zone"),
                                  vmc=self._command_name("vmc"))),
                  "", f"<i>{esc(self.tr('bot.help_buttons'))}</i>"]
        return Reply("\n".join(lines), [[(f"🏠 {self.tr('bot.overview')}", "o")]])

    def message(self, text: str, control: bool = True, who: str = "", stale: bool = False) -> Reply | None:
        """A message from an authorized chat: a command, answered."""
        if not text.startswith("/"):
            return Reply(esc(self.tr("bot.unknown")), [[(f"🏠 {self.tr('bot.overview')}", "o")]])
        words = text[1:].split()
        command = self._command(words[0].split("@", 1)[0]) if words else None
        args = words[1:]
        if command is None:
            return Reply(esc(self.tr("bot.unknown")), [[(f"🏠 {self.tr('bot.overview')}", "o")]])
        if command == "status":
            return self.overview()
        if command == "problems":
            return self.problems_reply()
        if command == "help":
            return self.help()
        handler = getattr(self, f"_cmd_{command}")
        return handler(args, control, who, stale)

    def _refuse(self, control: bool, stale: bool) -> Reply | None:
        """An action not carried out: from a chat that only looks, or sent
        while the board couldn't hear it."""
        if not control:
            return Reply(f"🔒 {esc(self.tr('bot.read_only'))}")
        if stale:
            return Reply(f"⏳ {esc(self.tr('bot.stale'))}", [[(f"🏠 {self.tr('bot.overview')}", "o")]])
        return None

    def _answer(self, data: dict, view) -> Reply:
        """An action's outcome, typed: done and how things are now, or why not."""
        if not data.get("ok"):
            return Reply(f"⛔ {esc(self.refusal(data))}", [[(f"🏠 {self.tr('bot.overview')}", "o")]])
        reply = view()
        return Reply(f"✅ {esc(self.tr('bot.done'))}\n\n{reply.text}", reply.buttons)

    # --------------------------------------------------------------- zones --

    def _find_zone(self, word: str, climate: dict) -> dict | None:
        zones = climate.get("zones") or []
        word = _plain(word)
        exact = [z for z in zones if _plain(z["name"]) == word]
        if exact:
            return exact[0]
        found = [z for z in zones if _plain(z["name"]).startswith(word)]
        return found[0] if len(found) == 1 else None

    def zone_view(self, name: str) -> Reply:
        climate = self._get("/api/climate/zones")
        zone = next((z for z in climate.get("zones") or [] if z["name"] == name), None)
        if zone is None:
            return Reply(esc(self.refusal({"code": "zone_unknown", "params": {"zone": name}})),
                         [[(f"⬅️ {self.tr('bot.back')}", "o")]])
        now = datetime.now()
        lines = self._zone_lines(zone, climate, now)
        rows = []
        if climate.get("away_until"):
            lines += ["", esc(self.tr("errors.override_while_away", zone=name))]
        elif self._controllable(zone, climate):
            if zone.get("target_c") is not None:
                target = zone["target_c"]
                rows.append([(f"− {self.fmt(STEP_C, 1)}°", f"z|{name}|-"), (self.fmt_target(target), f"z|{name}"),
                             (f"+ {self.fmt(STEP_C, 1)}°", f"z|{name}|+")])
                rows.append([(f"⏻ {self.tr('bot.zone_off')}", f"z|{name}|off")])
            else:
                mark = "❄️" if climate.get("mode") == "cool" else "🔥"
                rows.append([(f"{mark} {self.tr('bot.zone_on', target=self.fmt_target(self._last_target(zone, climate)))}",
                              f"z|{name}|on")])
            if zone.get("override"):
                rows[-1].append((f"📅 {self.tr('bot.zone_schedule')}", f"z|{name}|auto"))
                note = self.tr("bot.zone_by_hand_note")
            elif zone.get("target_c") is not None:
                note = self.tr("bot.zone_buttons_note", hours=FIRST_HOURS)
            else:
                note = self.tr("bot.zone_on_note", hours=FIRST_HOURS)
            lines += ["", f"<i>{esc(note)}</i>"]
        rows.append([(f"⬅️ {self.tr('bot.back')}", "o")])
        return Reply("\n".join(lines), rows)

    def _last_target(self, zone: dict, climate: dict) -> float:
        """lastTarget() of the home page: its target, or the week's highest."""
        if zone.get("target_c") is not None:
            return zone["target_c"]
        mode = climate.get("mode") or "heat"
        thermostat = self._thermostat()
        try:
            week = thermostat.schedule_store.get_week(zone["name"], mode) if thermostat else None
        except (KeyError, ValueError):
            week = None
        values = [v for day in (week or {}).values() for v in day if v is not None]
        return max(values) if values else DEFAULT_TARGET.get(mode, 21)

    def _hold(self, zone: dict, target, who: str) -> dict:
        """A temperature by hand (None: off). Changing one already held
        keeps its end; a new one lasts until the week's next change."""
        now = datetime.now()
        override = zone.get("override")
        body: dict = {"target": target}
        if override and override.get("until") is None:
            body["until"] = "manual"
        elif override:
            until = _when(override["until"])
            change = self._next_change(zone["name"], now)
            remaining = (until - now).total_seconds() / 3600 if until else 0
            if change and until and abs((change - until).total_seconds()) < 60:
                body["until"] = "next_change"
            elif 0 < remaining <= 24:
                body["hours"] = remaining
            else:
                body["until"] = "next_change"
        elif self._next_change(zone["name"], now):
            body["until"] = "next_change"
        else:
            body["hours"] = FIRST_HOURS
        return self._post(f"/api/climate/zones/{quote(zone['name'], safe='')}/override", body, who)

    def _zone_action(self, zone: dict, climate: dict, action: str, who: str, hours=None, target=None) -> dict:
        name = quote(zone["name"], safe="")
        if action == "auto":
            return self._post(f"/api/climate/zones/{name}/override/cancel", {}, who)
        if action in ("+", "-"):
            current = zone.get("target_c")
            if current is None:
                current = self._last_target(zone, climate)
            else:
                current += STEP_C if action == "+" else -STEP_C
            target = max(TARGET_MIN, min(TARGET_MAX, round(current * 2) / 2))
        elif action == "on":
            target = self._last_target(zone, climate)
        elif action == "off":
            target = None
        if hours is not None:
            return self._post(f"/api/climate/zones/{name}/override", {"target": target, "hours": hours}, who)
        return self._hold(zone, target, who)

    def _cmd_zone(self, args: list[str], control: bool, who: str, stale: bool) -> Reply:
        climate = self._get("/api/climate/zones")
        if not args:
            zones = [z for z in climate.get("zones") or [] if self.zone_ready(z)]
            return Reply(esc(self.tr("bot.pick_zone")), [[(f"🌡 {z['name']}", f"z|{z['name']}") for z in zones[:4]]])
        zone = self._find_zone(args[0], climate)
        if zone is None:
            names = self._list([z["name"] for z in climate.get("zones") or []])
            return Reply(esc(self.tr("bot.zone_unknown", zone=args[0], zones=names)))
        if len(args) == 1:
            return self.zone_view(zone["name"])
        refused = self._refuse(control, stale)
        if refused:
            return refused
        word = _plain(args[1])
        hours = self._hours(args[2]) if len(args) > 2 else None
        if len(args) > 2 and hours is None:
            return Reply(esc(self.tr("bot.zone_usage", zone_cmd=self._command_name("zone"), zone=zone["name"])))
        if word in self._words("schedule", ("schedule", "auto")):
            data = self._zone_action(zone, climate, "auto", who)
        elif word in self._words("off", ("off",)):
            data = self._zone_action(zone, climate, "off", who, hours=hours)
        elif _number(args[1]) is not None:
            data = self._zone_action(zone, climate, "set", who, hours=hours, target=_number(args[1]))
        else:
            return Reply(esc(self.tr("bot.zone_usage", zone_cmd=self._command_name("zone"), zone=zone["name"])))
        return self._answer(data, lambda: self.zone_view(zone["name"]))

    @staticmethod
    def _hours(word: str) -> float | None:
        word = _plain(word)
        for suffix in ("ore", "ora", "hours", "hour", "h"):
            if word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        value = _number(word)
        return value if value is not None and value > 0 else None

    # ---------------------------------------------------------------- away --

    def away_view(self) -> Reply:
        s = self._state()
        climate, now = s["climate"], s["now"]
        lines = []
        away = _when(climate.get("away_until"))
        if away:
            lines += [f"🧳 <b>{esc(self.tr('home.hero_away', until=self.away_until_text(away, now)))}</b>",
                      esc(self._away_detail(s))]
            rows = [[(f"🏠 {self.tr('home.away_back')}", "b")]]
        else:
            names = {d.get("name") for d in s["devices"]}
            rules = (climate.get("season_effects") or {}).get("rules") or []
            devices = [self.display_name(r["switch"]) for r in rules
                       if r.get(climate.get("mode")) == "manual" and r.get("switch") in names]
            lines.append(f"🧳 <b>{esc(self.tr('home.away_title'))}</b>")
            lines.append(f"• {esc(self.tr('home.away_point_zones'))}")
            if devices:
                lines.append(f"• {esc(self.tr('home.away_point_devices', devices=self._list(devices)))}")
            lines.append(f"• {esc(self.tr('home.away_point_plan'))}")
            lines += ["", esc(self.tr("home.away_for"))]
            rows = [[(self.tr(f"home.away_{h}"), f"a|{h}") for h in AWAY_OPTIONS]]
        rows.append([(f"⬅️ {self.tr('bot.back')}", "o")])
        return Reply("\n".join(lines), rows)

    def _cmd_away(self, args: list[str], control: bool, who: str, stale: bool) -> Reply:
        if not args:
            return self.away_view()
        refused = self._refuse(control, stale)
        if refused:
            return refused
        word = _plain(args[0])
        hours = None
        if any(word.endswith(s) for s in ("ore", "ora", "hours", "hour", "h")):
            hours = self._hours(word)
        else:
            for suffix in ("giorni", "giorno", "days", "day", "gg", "g", "d"):
                if word.endswith(suffix):
                    word = word[: -len(suffix)]
                    break
            days = _number(word)
            hours = days * 24 if days is not None and days > 0 else None
        if hours is None:
            return Reply(esc(self.tr("bot.away_usage", away=self._command_name("away"))))
        return self._answer(self._post("/api/climate/away", {"hours": hours}, who), self.overview)

    def _cmd_back(self, args: list[str], control: bool, who: str, stale: bool) -> Reply:
        refused = self._refuse(control, stale)
        if refused:
            return refused
        return self._answer(self._post("/api/climate/away/cancel", {}, who), self.overview)

    # ----------------------------------------------------------------- VMC --

    def vmc_view(self) -> Reply:
        s = self._state()
        speed = s["vmc_speed"]
        lines = []
        rows = []
        if not speed:
            lines.append(esc(self.tr("bot.no_vmc")))
        else:
            lines.append(self._vmc_line(s))
            status = (s["vmc"] or {}).get("status") or {}
            if status.get("away"):
                lines += ["", esc(self.tr("errors.vmc_away"))]
            else:
                presets = speed.get("presets") or []
                buttons = [(self.tr(f"vmc_presets.{p['key']}"), f"v|set|{p['value']}") for p in presets]
                rows += [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
                on = (s["readings"].get(speed["paired_switch"]) or {}).get("is_on") is not False
                power = next((d for d in s["devices"] if d.get("name") == speed["paired_switch"]), {})
                last = []
                if power.get("controllable"):
                    last.append((f"⏻ {self.tr('bot.vmc_off' if on else 'bot.vmc_on')}", "v|off" if on else "v|on"))
                if status.get("override"):
                    last.append((f"⏹ {self.tr('home.vmc_boost_cancel')}", "v|cancel"))
                elif presets:
                    top = max(p["value"] for p in presets)
                    lines += ["", esc(self.tr("home.vmc_boost_for", level=self._vmc_level(speed, top)))]
                    rows.append([(self.tr("home.vmc_minutes", m=m) if m < 60 else self.tr("home.vmc_hours", h=m // 60),
                                  f"v|boost|{top}|{m}") for m in VMC_BOOST_MINUTES])
                if last:
                    rows.append(last)
        rows.append([(f"⬅️ {self.tr('bot.back')}", "o")])
        return Reply("\n".join(lines), rows)

    def _vmc_action(self, action: str, who: str, value=None, minutes=None) -> dict:
        s = self._state()
        speed = s["vmc_speed"]
        if not speed:
            return {"ok": False, "error": "no VMC", "text": self.tr("bot.no_vmc")}
        status = (s["vmc"] or {}).get("status") or {}
        boosting = bool(status.get("override"))
        # Whole numbers, as the endpoints take them (a minute count is one).
        value = round(value) if isinstance(value, (int, float)) else value
        minutes = round(minutes) if isinstance(minutes, (int, float)) else minutes
        if action == "cancel":
            return self._post("/api/vmc-schedule/boost/cancel", {}, who)
        if action == "boost":
            return self._post("/api/vmc-schedule/boost", {"device": speed["name"], "value": value, "minutes": minutes}, who)
        # A speed or the switch by hand ends a timed one, so it isn't
        # undone later - as the home page's buttons do.
        if boosting and not status.get("away") and action in ("set", "off"):
            self._post("/api/vmc-schedule/boost/cancel", {}, who)
        if action == "set":
            return self._post(f"/api/sensors/{quote(speed['name'], safe='')}/set-value", {"value": value}, who)
        return self._post(f"/api/sensors/{quote(speed['paired_switch'], safe='')}/{action}", {}, who)

    def _cmd_vmc(self, args: list[str], control: bool, who: str, stale: bool) -> Reply:
        if not args:
            return self.vmc_view()
        refused = self._refuse(control, stale)
        if refused:
            return refused
        speed = next((d for d in self._get("/api/devices").get("sensors") or []
                      if d.get("kind") == "number" and d.get("paired_switch") and d.get("presets")), None)
        if not speed:
            return Reply(esc(self.tr("bot.no_vmc")))
        word = _plain(args[0])
        minutes = None
        if len(args) > 1:
            minutes = _number(_plain(args[1]).rstrip("min").rstrip("m"))
            if minutes is None or minutes <= 0:
                return Reply(esc(self.tr("bot.vmc_usage", vmc=self._command_name("vmc"))))
        if word in self._words("off", ("off",)):
            data = self._vmc_action("off", who)
        elif word in self._words("on", ("on",)):
            data = self._vmc_action("on", who)
        elif word in self._words("cancel", ("cancel", "stop")):
            data = self._vmc_action("cancel", who)
        else:
            value = None
            for preset in speed.get("presets") or []:
                if word in (_plain(preset["key"]), _plain(self.tr(f"vmc_presets.{preset['key']}"))):
                    value = preset["value"]
            if value is None:
                value = _number(word.rstrip("%"))
            if value is None:
                return Reply(esc(self.tr("bot.vmc_usage", vmc=self._command_name("vmc"))))
            value = round(value)
            data = (self._vmc_action("boost", who, value=value, minutes=round(minutes)) if minutes
                    else self._vmc_action("set", who, value=value))
        return self._answer(data, self.vmc_view)

    # -------------------------------------------------------- dehumidifier --

    def dehumidifier_view(self) -> Reply:
        climate = self._get("/api/climate/zones")
        d = climate.get("dehumidifier")
        now = datetime.now()
        lines = []
        rows = []
        if not d:
            lines.append(esc(self.tr("errors.no_dehumidifier")))
        else:
            name = esc(self.tr("climate.dehumidifier"))
            rh = f" · {esc(self.tr('home.dehum_rh', rh=d['humidity']))}" if d.get("humidity") is not None else ""
            if d.get("on"):
                until = _when(d.get("until_at"))
                state = self.tr("home.dehum_on_until", until=self.until_text(until, now)) if until else self.tr("home.line_dehum")
            else:
                state = self.tr("bot.dehumidifier_off")
            lines.append(f"💧 <b>{name}</b>: {esc(state)}{rh}")
            if climate.get("away_until"):
                lines += ["", esc(self.tr("errors.dehumidifier_away"))]
            elif not d.get("allowed", True):
                lines += ["", esc(self.tr("errors.dehumidifier_not_now"))]
            else:
                choices = [(self.tr("home.hours", h=self.fmt(h, 0 if float(h).is_integer() else 1)), f"d|{h}")
                           for h in d.get("durations") or []]
                if d.get("until_target"):
                    choices.append((self.tr("home.dehum_until_target", stop=d.get("stop_below")), "d|target"))
                rows += [choices[i:i + 3] for i in range(0, len(choices), 3)]
                if d.get("on"):
                    rows.append([(f"⏻ {self.tr('home.dehum_off')}", "d|off")])
                lines += ["", esc(self.tr("home.dehum_for"))] if choices else []
        rows.append([(f"⬅️ {self.tr('bot.back')}", "o")])
        return Reply("\n".join(lines), rows)

    def _dehumidifier_action(self, choice: str, who: str) -> dict:
        if choice == "off":
            return self._post("/api/dehumidifier/off", {}, who)
        if choice == "target":
            return self._post("/api/dehumidifier/on", {"until": "target"}, who)
        hours = _number(choice)
        if hours is not None and float(hours).is_integer():
            hours = int(hours)
        return self._post("/api/dehumidifier/on", {"hours": hours}, who)

    def _cmd_dehumidifier(self, args: list[str], control: bool, who: str, stale: bool) -> Reply:
        if not args:
            return self.dehumidifier_view()
        refused = self._refuse(control, stale)
        if refused:
            return refused
        word = _plain(args[0])
        if word in self._words("off", ("off",)):
            choice = "off"
        elif word in self._words("target", ("target",)):
            choice = "target"
        elif self._hours(word) is not None:
            choice = str(self._hours(word))
        else:
            return Reply(esc(self.tr("bot.dehumidifier_usage", dehumidifier=self._command_name("dehumidifier"))))
        return self._answer(self._dehumidifier_action(choice, who), self.dehumidifier_view)

    # ------------------------------------------------------------- buttons --

    def button(self, data: str, control: bool = True, who: str = "", stale: bool = False):
        """A button pressed: (the message as it should read now, a short
        note for the top of the screen)."""
        parts = data.split("|")
        kind, rest = parts[0], parts[1:]
        views = {"o": self.overview, "a": self.away_view, "v": self.vmc_view, "d": self.dehumidifier_view,
                 "p": self.problems_reply}
        if kind == "z" and len(rest) == 1:
            return self.zone_view(rest[0]), None
        if kind in views and not rest:
            return views[kind](), None
        if kind not in ("z", "a", "b", "v", "d") or (kind in ("z", "a", "v", "d") and not rest):
            return self.overview(), None
        if not control:
            return None, self.tr("bot.read_only")
        if stale:
            return self.overview(), self.tr("bot.stale_button")
        if kind == "z":
            climate = self._get("/api/climate/zones")
            zone = next((z for z in climate.get("zones") or [] if z["name"] == rest[0]), None)
            if zone is None:
                return self.overview(), self.refusal({"code": "zone_unknown", "params": {"zone": rest[0]}})
            outcome = self._zone_action(zone, climate, rest[1], who)
            return self.zone_view(zone["name"]), self._toast(outcome)
        if kind == "a":
            outcome = self._post("/api/climate/away", {"hours": _number(rest[0])}, who)
            return (self.overview() if outcome.get("ok") else self.away_view()), self._toast(outcome)
        if kind == "b":
            outcome = self._post("/api/climate/away/cancel", {}, who)
            return self.overview(), self._toast(outcome)
        if kind == "v":
            action = rest[0]
            if action == "set":
                outcome = self._vmc_action("set", who, value=_number(rest[1]))
            elif action == "boost":
                outcome = self._vmc_action("boost", who, value=_number(rest[1]), minutes=_number(rest[2]))
            else:
                outcome = self._vmc_action(action, who)
            return self.vmc_view(), self._toast(outcome)
        outcome = self._dehumidifier_action(rest[0], who)
        return self.dehumidifier_view(), self._toast(outcome)

    def _toast(self, outcome: dict) -> str:
        return f"✅ {self.tr('bot.done')}" if outcome.get("ok") else f"⛔ {self.refusal(outcome)}"
