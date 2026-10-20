"""The board's Telegram bot, against a Telegram of its own: what it answers
and to whom, what its buttons and commands change - through the same
endpoints as the home page, with their refusals in the board's words - the
problems it tells, and the admin panel's side: the token in, never out."""
import json
import time
from datetime import datetime, timedelta

import pytest

from localhome.services.telegram_bot import (
    MAX_AGE_SECONDS,
    NOTIFY_AFTER_SECONDS,
    RESOLVED_AFTER_SECONDS,
    NotifiedStore,
    TelegramBot,
    TelegramError,
    TelegramStore,
    split_message,
)
from localhome.web.app import create_app

TOKEN = "123456789:" + "A" * 35
UNKNOWN_TOKEN = "987654321:" + "B" * 35
ME, FAMILY, STRANGER = 1111, 3333, 2222

CONFIG = """
web:
  port: 5000
  language: "it"
integrations:
  - kind: switch
    type: simulated
    name: "Dehumidifier"
    dashboard_tab: climate
    visible_in_mode: "cool"
thermostat:
  mode: "heat"
  hysteresis_c: 0.5
  schedules_file: "schedules.json"
  poll_interval_seconds: 600
  mode_limited_switches:
    - switch: "Dehumidifier"
      only_in_mode: "cool"
"""
ZONES = [
    {"name": "Stanze", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
    {"name": "Bagni", "sensor_mode": "simulated", "sensor_device_id": None,
     "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
]


class Telegram:
    """Telegram, as far as the bot can tell: the bots it knows, what's
    waiting to be read, what it was sent."""

    def __init__(self):
        self.bots = {TOKEN: {"id": 42, "is_bot": True, "username": "casa_test_bot", "first_name": "Casa"}}
        self.updates = []
        self.sent = []
        self.down = None
        self.next_id = 1

    def api(self, token):
        telegram = self

        class Api:
            def call(self, method, http_timeout=20, **params):
                return telegram.call(token, method, params)
        return Api()

    def call(self, token, method, params):
        if self.down:
            raise self.down
        if token not in self.bots:
            raise TelegramError(401, "Unauthorized")
        if method == "getMe":
            return self.bots[token]
        if method == "getUpdates":
            waiting, self.updates = self.updates, []
            return waiting
        self.sent.append((method, params))
        return True

    def write(self, chat_id, text, age=0):
        self.updates.append({"update_id": self.next_id, "message": {
            "message_id": self.next_id, "date": int(time.time() - age), "text": text,
            "chat": {"id": chat_id, "type": "private", "first_name": "Antonio", "username": "antonio"},
            "from": {"id": chat_id, "first_name": "Antonio"}}})
        self.next_id += 1

    def press(self, chat_id, data):
        self.updates.append({"update_id": self.next_id, "callback_query": {
            "id": f"q{self.next_id}", "data": data, "from": {"id": chat_id, "first_name": "Antonio"},
            "message": {"message_id": 7, "chat": {"id": chat_id, "type": "private"}}}})
        self.next_id += 1

    def said(self, chat_id=None):
        """What chats were shown: new messages and messages changed."""
        return [p for m, p in self.sent if m in ("sendMessage", "editMessageText")
                and (chat_id is None or p["chat_id"] == chat_id)]

    def toasts(self):
        return [p.get("text") for m, p in self.sent if m == "answerCallbackQuery"]

    def buttons(self, message):
        return [b["callback_data"] for row in message.get("reply_markup", {}).get("inline_keyboard", []) for b in row]


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Driven by hand, one round at a time: no threads of its own.
    monkeypatch.setattr(TelegramBot, "start", lambda self: None)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({
        "zones": ZONES, "extra_sensors": {"outdoor_sensor_mode": "simulated", "outdoor_sensor_device_id": None},
        "extra_dimmers": {"vmc_mode": "simulated", "vmc_device_id": None}}), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    telegram = Telegram()
    bot = app.config["TELEGRAM"]
    bot._api_factory = telegram.api
    yield app, bot, telegram
    app.config["HISTORY"].close()


def authorize(bot, *chats, control=True, notify=True):
    def change(data):
        data["bot_token"] = TOKEN
        data["chats"] = [{"id": c, "name": "Antonio", "kind": "private", "notify": notify, "control": control}
                         for c in chats]
    bot.store.update(change)


def zone(app, name):
    zones = app.test_client().get("/api/climate/zones").get_json()["zones"]
    return next(z for z in zones if z["name"] == name)


# ------------------------------------------------------------ who's told --

def test_a_stranger_gets_not_a_word_and_waits_in_the_admin_panel_to_be_authorized(home):
    app, bot, telegram = home
    authorize(bot)                       # the bot, no chat yet
    telegram.write(STRANGER, "/start")
    bot.poll_once()
    assert telegram.said() == [], "not even 'not authorized'"
    admin = app.test_client()
    waiting = admin.get("/api/climate-admin/telegram").get_json()["waiting"]
    assert [(w["id"], w["name"], w["kind"]) for w in waiting] == [(STRANGER, "Antonio (@antonio)", "private")]

    allowed = admin.post(f"/api/climate-admin/telegram/chats/{STRANGER}", json={}).get_json()
    assert allowed["ok"] and allowed["waiting"] == []
    assert [(c["id"], c["notify"], c["control"]) for c in allowed["chats"]] == [(STRANGER, True, True)]
    telegram.write(STRANGER, "/stato")
    bot.poll_once()
    assert len(telegram.said(STRANGER)) == 1


def test_a_chat_that_only_looks_sees_the_home_and_changes_nothing(home):
    app, bot, telegram = home
    authorize(bot, ME, control=False)
    telegram.write(ME, "/fuori 3")
    telegram.press(ME, "a|24")
    telegram.write(ME, "/stato")
    bot.poll_once()
    said = telegram.said(ME)
    assert said[0]["text"].startswith("🔒") and "solo guardare" in said[0]["text"]
    assert "Questa chat può solo guardare" in telegram.toasts()[0]
    assert "Stanze" in said[-1]["text"], "the home, still"
    assert app.test_client().get("/api/climate/zones").get_json()["away_until"] is None


def test_a_command_sent_while_the_board_couldnt_hear_it_isnt_carried_out(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.press(ME, "a|24")           # pressed before the board came up: no date of its own
    telegram.write(ME, "/fuori 3", age=MAX_AGE_SECONDS + 60)
    telegram.write(ME, "/stato", age=MAX_AGE_SECONDS + 60)
    bot.poll_once()
    assert telegram.toasts()[0].startswith("Premuto mentre LocalHome non era raggiungibile")
    said = telegram.said(ME)
    assert said[1]["text"].startswith("⏳"), "said, not done"
    assert "Stanze" in said[2]["text"], "an old look at the home is still answered"
    assert app.test_client().get("/api/climate/zones").get_json()["away_until"] is None
    telegram.write(ME, "/fuori 3")       # sent now: done
    bot.poll_once()
    assert app.test_client().get("/api/climate/zones").get_json()["away_until"] is not None


# ---------------------------------------------------------------- what's said --

def test_the_overview_says_the_home_pages_words_and_offers_its_buttons(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/stato")
    bot.poll_once()
    (message,) = telegram.said(ME)
    assert message["parse_mode"] == "HTML"
    text = message["text"]
    assert "<b>LocalHome</b> · Inverno · riscaldamento" in text
    assert "<b>Stanze</b>" in text and "<b>Bagni</b>" in text
    assert "Aggiornato alle" in text
    assert {"o", "a", "z|Stanze", "z|Bagni", "v"} <= set(telegram.buttons(message))


def test_help_and_anything_else(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/aiuto")
    telegram.write(ME, "ciao")
    telegram.write(ME, "/qualcosa")
    bot.poll_once()
    help_text, hello, unknown = (m["text"] for m in telegram.said(ME))
    assert "/stato" in help_text and "/zona" in help_text and "/fuori" in help_text
    assert hello == unknown and hello.startswith("Non ho capito")


def test_the_commands_menu_is_in_the_boards_language(home):
    app, bot, telegram = home
    authorize(bot, ME)
    bot.poll_once()
    (menu,) = [p for m, p in telegram.sent if m == "setMyCommands"]
    names = [c["command"] for c in menu["commands"]]
    assert names == ["stato", "zona", "fuori", "rientrato", "vmc", "deumidificatore", "problemi", "aiuto"]


# ------------------------------------------------------------ what's changed --

def test_a_zone_by_hand_then_its_buttons_then_back_to_its_week(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/zona stanze 22")
    bot.poll_once()
    assert telegram.said(ME)[-1]["text"].startswith("✅ Fatto")
    assert zone(app, "Stanze")["override"]["target"] == 22
    for press, expected in (("+", 22.5), ("-", 22), ("-", 21.5)):
        telegram.press(ME, f"z|Stanze|{press}")
        bot.poll_once()
        assert zone(app, "Stanze")["override"]["target"] == expected
    assert telegram.toasts()[-1] == "✅ Fatto"
    edited = [p for m, p in telegram.sent if m == "editMessageText"][-1]
    assert "21,5° a mano" in edited["text"] and "z|Stanze|auto" in telegram.buttons(edited)
    telegram.press(ME, "z|Stanze|off")
    bot.poll_once()
    assert zone(app, "Stanze")["override"]["target"] is None
    telegram.press(ME, "z|Stanze|auto")
    bot.poll_once()
    assert zone(app, "Stanze")["override"] is None


def test_zones_typed_in_either_language_for_a_while(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/zone bag 19 2h")
    bot.poll_once()
    held = zone(app, "Bagni")["override"]
    assert held["target"] == 19
    until = datetime.fromisoformat(held["until"])
    assert abs((until - datetime.now()) - timedelta(hours=2)) < timedelta(minutes=2)
    telegram.write(ME, "/zona Bagni spenta")
    telegram.write(ME, "/zona bagni programma")
    telegram.write(ME, "/zona cucina 20")
    bot.poll_once()
    assert zone(app, "Bagni")["override"] is None
    assert "Non trovo la zona «cucina»" in telegram.said(ME)[-1]["text"]


def test_a_refusal_is_said_in_the_boards_words(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/zona stanze 40")
    bot.poll_once()
    assert telegram.said(ME)[-1]["text"] == "⛔ La temperatura deve essere tra 5 e 35 °C, oppure spento."


def test_away_and_back_by_command_and_by_button(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/fuori 3")
    bot.poll_once()
    away = datetime.fromisoformat(app.test_client().get("/api/climate/zones").get_json()["away_until"])
    assert abs(away - (datetime.now() + timedelta(days=3))) < timedelta(minutes=2)
    reply = telegram.said(ME)[-1]
    assert "Fuori casa" in reply["text"] and "b" in telegram.buttons(reply)
    telegram.write(ME, "/zona stanze 21")
    bot.poll_once()
    assert telegram.said(ME)[-1]["text"].startswith("⛔ Sei fuori casa")
    telegram.press(ME, "b")
    bot.poll_once()
    assert app.test_client().get("/api/climate/zones").get_json()["away_until"] is None
    telegram.press(ME, "a|24")
    bot.poll_once()
    assert app.test_client().get("/api/climate/zones").get_json()["away_until"] is not None


def test_the_vmc_by_its_buttons(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.press(ME, "v")
    bot.poll_once()
    view = [p for m, p in telegram.sent if m == "editMessageText"][-1]
    sets = [b for b in telegram.buttons(view) if b.startswith("v|set|")]
    assert len(sets) == 5, "a button a level"
    manager = app.config["MANAGER"]
    speed = next(n for n, s in manager.paired_switch.items() if n in manager.numbers)
    day = int(sets[2].split("|")[2])
    telegram.press(ME, sets[2])
    bot.poll_once()
    assert manager.sensor_reading(speed)["value"] == day
    boost = next(b for b in telegram.buttons([p for m, p in telegram.sent if m == "editMessageText"][-1])
                 if b.startswith("v|boost|"))
    telegram.press(ME, boost)
    bot.poll_once()
    assert app.test_client().get("/api/vmc-schedule").get_json()["status"]["override"]
    telegram.press(ME, "v|cancel")
    bot.poll_once()
    assert "override" not in app.test_client().get("/api/vmc-schedule").get_json()["status"]
    telegram.write(ME, "/vmc notte")
    bot.poll_once()
    assert telegram.said(ME)[-1]["text"].startswith("✅ Fatto")


def test_the_dehumidifier_out_of_its_season_says_so(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.write(ME, "/deumidificatore 7")
    bot.poll_once()
    assert telegram.said(ME)[-1]["text"].startswith("⛔ Durata non valida")


# --------------------------------------------------------------- what's told --

def test_problems_are_told_once_they_last_and_solved_once_gone(home, monkeypatch):
    app, bot, telegram = home
    authorize(bot, ME)
    now = {"problems": {"device_offline|Stanze Sensor": "Il sensore di Stanze non risponde."}, "unknown": set()}
    monkeypatch.setattr(bot.replies, "problems", lambda: (now["problems"], now["unknown"]))
    bot.watch_problems(now=1000)
    assert telegram.said() == [], "a device restarting isn't news"
    bot.watch_problems(now=1000 + NOTIFY_AFTER_SECONDS)
    (told,) = telegram.said(ME)
    assert "Da controllare" in told["text"] and "Il sensore di Stanze non risponde." in told["text"]
    bot.watch_problems(now=1100 + NOTIFY_AFTER_SECONDS)
    assert len(telegram.said()) == 1, "told once"

    now["problems"] = {}                 # last seen at 1400
    bot.watch_problems(now=1500)
    assert len(telegram.said()) == 1, "gone a moment isn't solved"
    bot.watch_problems(now=1400 + RESOLVED_AFTER_SECONDS)
    solved = telegram.said(ME)[-1]["text"]
    assert "Risolto" in solved and "Il sensore di Stanze non risponde." in solved


def test_a_restart_doesnt_tell_a_problem_again_nor_solve_one_not_heard_from_yet(home, tmp_path):
    app, bot, telegram = home
    authorize(bot, ME)
    path = str(tmp_path / "telegram_notified.json")
    NotifiedStore(path).change({"device_offline|Stanze Sensor": "Il sensore di Stanze non risponde.",
                                "device_battery_low|Outdoor Sensor": "Batterie."}, [])
    restarted = TelegramBot(bot.store, bot.replies, NotifiedStore(path), api_factory=telegram.api, clock=lambda: 5000)
    restarted.replies = type("Now", (), {"problems": lambda self: (
        {"device_battery_low|Outdoor Sensor": "Batterie."}, {"Stanze Sensor"})})()
    restarted.watch_problems(now=5000 + RESOLVED_AFTER_SECONDS + 1)
    assert telegram.said() == []


def test_a_chat_without_notifications_isnt_told(home, monkeypatch):
    app, bot, telegram = home
    authorize(bot, ME, notify=False)
    monkeypatch.setattr(bot.replies, "problems", lambda: ({"disk_low|/": "Disco pieno."}, set()))
    bot.watch_problems(now=0)
    bot.watch_problems(now=NOTIFY_AFTER_SECONDS)
    assert telegram.said() == []


def test_the_power_alert_in_the_boards_language(home):
    app, bot, telegram = home
    authorize(bot, ME, FAMILY)
    app.config["MANAGER"].notify("High power draw: 3500 W (safe limit 3300 W)", "power_high", power=3500, limit=3300)
    texts = [m["text"] for m in telegram.said()]
    assert len(texts) == 2 and "Consumo alto: 3.500 W (il limite sicuro è 3.300 W)" in texts[0]


# ----------------------------------------------------- the admin panel's side --

def test_the_token_goes_in_and_never_comes_out(home, tmp_path):
    app, bot, telegram = home
    admin = app.test_client()
    first = admin.get("/api/climate-admin/telegram").get_json()
    assert first["configured"] is False and first["status"] == "off"
    refused = admin.post("/api/climate-admin/telegram/token", json={"token": "not a token"})
    assert refused.status_code == 400 and refused.get_json()["code"] == "telegram_token_format"
    refused = admin.post("/api/climate-admin/telegram/token", json={"token": UNKNOWN_TOKEN})
    assert refused.status_code == 400 and refused.get_json()["code"] == "telegram_token_rejected"
    telegram.down = TelegramError(0, "ConnectionError")
    refused = admin.post("/api/climate-admin/telegram/token", json={"token": TOKEN})
    assert refused.status_code == 502 and refused.get_json()["code"] == "telegram_unreachable"
    assert not (tmp_path / "secrets_telegram.json").exists(), "nothing saved that Telegram didn't take"

    telegram.down = None
    connected = admin.post("/api/climate-admin/telegram/token", json={"token": f"  {TOKEN} "})
    assert connected.get_json()["bot"] == {"username": "casa_test_bot", "name": "Casa"}
    assert connected.get_json()["status"] == "connecting", "not 'off' while its thread wakes up"
    assert TOKEN not in connected.get_data(as_text=True)
    assert TOKEN not in admin.get("/api/climate-admin/telegram").get_data(as_text=True)
    assert json.loads((tmp_path / "secrets_telegram.json").read_text(encoding="utf-8"))["bot_token"] == TOKEN

    assert admin.post(f"/api/climate-admin/telegram/chats/{ME}/test").status_code == 404
    admin.post(f"/api/climate-admin/telegram/chats/{STRANGER}", json={})
    authorize(bot, ME)
    assert admin.post(f"/api/climate-admin/telegram/chats/{ME}", json={"control": "yes"}).status_code == 400
    assert admin.post(f"/api/climate-admin/telegram/chats/{ME}", json={"control": False}).get_json()["chats"][0]["control"] is False
    assert admin.post(f"/api/climate-admin/telegram/chats/{ME}/test").get_json()["ok"]
    assert "Messaggio di prova da LocalHome" in telegram.said(ME)[-1]["text"]

    gone = admin.post("/api/climate-admin/telegram/remove").get_json()
    assert gone["configured"] is False and gone["chats"] == []
    assert json.loads((tmp_path / "secrets_telegram.json").read_text(encoding="utf-8"))["bot_token"] is None


def test_telegram_saying_no_is_said_in_the_admin_panel(home):
    app, bot, telegram = home
    authorize(bot, ME)
    telegram.down = TelegramError(409, "Conflict: terminated by other getUpdates request")
    assert bot.poll_once() > 0
    assert bot.status()["status"] == "conflict"
    telegram.down = TelegramError(0, "ConnectionError")
    first, second = bot.poll_once(), bot.poll_once()
    assert bot.status()["status"] == "offline" and second > first, "backing off"
    telegram.down = TelegramError(401, "Unauthorized")
    bot.poll_once()
    assert bot.status()["status"] == "token_invalid"
    telegram.down = None
    bot.poll_once()
    assert bot.status()["status"] == "running"


def test_the_bot_passes_the_web_password_a_browser_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(TelegramBot, "start", lambda self: None)
    (tmp_path / "secrets_web.json").write_text(json.dumps({"username": "casa", "password": "segreta"}), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(CONFIG.replace('  language: "it"', '  language: "it"\n  auth:\n    secrets_file: "secrets_web.json"'),
                                          encoding="utf-8")
    (tmp_path / "climate_map.json").write_text(json.dumps({"zones": ZONES}), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        assert app.test_client().get("/api/climate/zones").status_code == 401
        telegram = Telegram()
        bot = app.config["TELEGRAM"]
        bot._api_factory = telegram.api
        authorize(bot, ME)
        telegram.write(ME, "/stato")
        bot.poll_once()
        assert "<b>Stanze</b>" in telegram.said(ME)[0]["text"]
    finally:
        app.config["HISTORY"].close()


# ---------------------------------------------------------------- its file --

def test_the_older_notifiers_file_reads_as_one_chat_told_and_allowed_everything(tmp_path):
    path = tmp_path / "secrets_telegram.json"
    path.write_text(json.dumps({"bot_token": TOKEN, "chat_id": str(ME)}), encoding="utf-8")
    assert TelegramStore(str(path)).get() == {"bot_token": TOKEN, "chats": [
        {"id": ME, "name": "", "kind": "private", "notify": True, "control": True}]}


def test_a_long_answer_is_cut_between_lines():
    text = "\n".join(f"riga {i:03d} " + "x" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts) == text
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_in_a_group_only_the_commands_for_this_bot_are_answered(home):
    app, bot, telegram = home
    group = -100555
    authorize(bot, group)
    bot.poll_once()                      # connected: it knows its own name
    for text in ("ciao a tutti", "/stato@altro_bot", "/stato@casa_test_bot", "/stato"):
        telegram.updates.append({"update_id": telegram.next_id, "message": {
            "message_id": telegram.next_id, "date": int(time.time()), "text": text,
            "chat": {"id": group, "type": "supergroup", "title": "Famiglia"}, "from": {"id": ME, "first_name": "Antonio"}}})
        telegram.next_id += 1
    bot.poll_once()
    assert len(telegram.said(group)) == 2
