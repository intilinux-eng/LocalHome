"""The board's own Telegram bot: the home at a glance and its simple
commands from a phone, anywhere, and a message when something needs
looking at.

One bot per LocalHome. Telegram hands a bot's messages to one reader at a
time: two boards on one token would take turns getting them (each told
"409 Conflict"), so every board gets a bot of its own - created with
@BotFather, connected in the admin panel (docs/integrations/telegram.md).

Nothing reaches the board from outside: it asks Telegram for what was
written to the bot (long polling, HTTPS out, as a browser does) - no port
opened on the router. Only the chats authorized in the admin panel get an
answer; anyone else gets not a word, and is listed in the panel to be
authorized there. What each command says and does is in
services/telegram_replies.py; the problems told come from the same list
as the home page's.

The token and the chats are in config/secrets_telegram.json, written by
the admin panel: never shown back, never sent anywhere but to Telegram -
nor logged (an error from `requests` carries the URL, and the URL the
token: only its kind is kept).
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from localhome.core.interfaces import Notifier
from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# What a token from @BotFather looks like: the bot's number, a colon, ~35
# letters. Checked before asking Telegram, so a paste gone wrong (a space,
# half of it) is said as such.
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")
# One request waits up to this long for a message - one connection held
# open, not one a second.
POLL_SECONDS = 50
# A command older than this isn't carried out: sent while the board was
# off or offline, it may no longer be wanted ("away" from yesterday
# evening). Said so instead.
MAX_AGE_SECONDS = 300
# Telegram's limit is 4096 characters a message; a little under it, for
# the escapes.
MESSAGE_LIMIT = 3800
# Chats that wrote to the bot without being authorized, for the admin
# panel to authorize: the latest few, for a day.
WAITING_MAX = 10
WAITING_KEEP_SECONDS = 24 * 3600
# Telegram unreachable: tried again after these, then every 2 minutes.
BACKOFF_SECONDS = (5, 10, 30, 60, 120)
# Another reader of the same bot (409): tried again now and then - the
# other one may be gone.
CONFLICT_RETRY_SECONDS = 60
# The token refused (revoked in @BotFather): not asked again every
# minute - the admin panel's new token wakes the bot at once.
TOKEN_RETRY_SECONDS = 15 * 60
# Nothing to do without a token: woken by the admin panel.
IDLE_SECONDS = 3600
# Problems: looked at every minute, told once they've lasted 5 minutes
# (a device restarting isn't news), solved once gone for 10.
WATCH_EVERY_SECONDS = 60
NOTIFY_AFTER_SECONDS = 300
RESOLVED_AFTER_SECONDS = 600


class TelegramError(Exception):
    """Telegram said no: `status` is its code (401 a token it doesn't
    know, 409 another reader of the same bot, 429 too many requests), 0
    when it couldn't be reached at all."""

    def __init__(self, status: int, description: str, retry_after: float | None = None):
        super().__init__(description)
        self.status = status
        self.retry_after = retry_after


class TelegramApi:
    """The Bot API: one HTTPS POST a call."""

    def __init__(self, token: str):
        self._url = f"{API_BASE}/bot{token}/"
        self._session = requests.Session()

    def call(self, method: str, http_timeout: float = 20, **params):
        try:
            resp = self._session.post(self._url + method, json=params, timeout=http_timeout)
        except requests.RequestException as exc:
            # The exception's text has the URL in it - the token.
            raise TelegramError(0, type(exc).__name__) from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, f"HTTP {resp.status_code}") from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code") or resp.status_code, str(data.get("description") or ""),
                                (data.get("parameters") or {}).get("retry_after"))
        return data.get("result")


@dataclass
class Reply:
    """A message: Telegram's HTML, and its buttons - rows of (label,
    data), the data coming back when one is pressed."""
    text: str
    buttons: list = field(default_factory=list)


def _chat_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value.strip()):
        return int(value.strip())
    return None


class TelegramStore:
    """config/secrets_telegram.json: {"bot_token": "...", "chats": [{"id",
    "name", "kind", "notify", "control"}]} - `notify` gets the problems'
    messages, `control` may change things (else it only looks). The older
    notifier's file - a token and one "chat_id" - reads as that chat, told
    everything and allowed everything. Written private to the board's
    user (save_json_atomic's temporary file is 0600, and the rename keeps
    it)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            return self._read()

    def update(self, change) -> dict:
        """change(data) edits it; then saved, whole or not at all."""
        with self._lock:
            data = self._read()
            change(data)
            save_json_atomic(self.path, data)
            return data

    def _read(self) -> dict:
        try:
            raw = load_json(self.path)
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            logger.warning("Telegram bot file %s unreadable (%s): the bot stays off", self.path, type(exc).__name__)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        token = raw.get("bot_token")
        token = token.strip() if isinstance(token, str) and token.strip() else None
        chats = []
        for item in raw.get("chats") or []:
            chat_id = _chat_id(item.get("id")) if isinstance(item, dict) else None
            if chat_id is None or any(c["id"] == chat_id for c in chats):
                continue
            chats.append({"id": chat_id, "name": str(item.get("name") or ""),
                          "kind": "group" if item.get("kind") == "group" else "private",
                          "notify": item.get("notify", True) is not False,
                          "control": item.get("control", True) is not False})
        legacy = _chat_id(raw.get("chat_id"))
        if legacy is not None and not any(c["id"] == legacy for c in chats):
            chats.append({"id": legacy, "name": "", "kind": "private" if legacy > 0 else "group",
                          "notify": True, "control": True})
        return {"bot_token": token, "chats": chats}


class NotifiedStore:
    """The problems already told, {key: what was said}: a restart doesn't
    tell them again, and says "solved" for one solved meanwhile."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] | None = None

    def get(self) -> dict[str, str]:
        with self._lock:
            if self._data is None:
                try:
                    raw = load_json(self.path) if self.path else {}
                except (OSError, ValueError):
                    raw = {}
                self._data = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
            return dict(self._data)

    def change(self, added: dict[str, str], removed: list[str]) -> None:
        data = self.get()
        data.update(added)
        for key in removed:
            data.pop(key, None)
        with self._lock:
            self._data = data
            if self.path:
                try:
                    save_json_atomic(self.path, data)
                except OSError:
                    logger.warning("Couldn't save %s: told problems may be told again after a restart", self.path)

    def clear(self) -> None:
        self.change({}, list(self.get()))


def _person(sender: dict | None) -> str:
    """Who pressed or wrote, for the log: "Antonio Rossi"."""
    if not sender:
        return ""
    name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
    return name or (f"@{sender['username']}" if sender.get("username") else str(sender.get("id", "")))


def _chat_name(chat: dict) -> str:
    if chat.get("type") == "private":
        name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
        if chat.get("username"):
            name = f"{name} (@{chat['username']})" if name else f"@{chat['username']}"
        return name
    return chat.get("title") or ""


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Under Telegram's length, cut between lines - a tag is never cut, as
    every line closes its own."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current or not parts:
        parts.append(current)
    return parts


def _keyboard(buttons: list) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]}


class TelegramBot(Notifier):
    """Reads what's written to the bot and answers, in a thread of its
    own; looks at the problems every minute in another. Also the board's
    notifier: the power alert (services/history.py) goes to the chats
    that get the problems."""

    def __init__(self, store: TelegramStore, replies, notified: NotifiedStore | None = None,
                 api_factory=TelegramApi, clock=time.time):
        self.store = store
        self.replies = replies
        self._notified = notified or NotifiedStore(None)
        self._api_factory = api_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._started = False
        self._started_at = clock()
        # The reading side (the poll thread's own).
        self._token: str | None = None
        self._api = None
        self._ready = False
        self._offset: int | None = None
        self._first_batch = True
        self._retry = 0
        # The sending side, shared: its own connection, one call at a time.
        self._sender = None
        self._sender_token: str | None = None
        self._send_lock = threading.Lock()
        self._status = {"status": "off", "error": None}
        self._bot: dict | None = None
        self._waiting: dict[int, dict] = {}
        self._first_seen: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    # ------------------------------------------------------------ running --

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._poll_loop, daemon=True, name="telegram-bot").start()
        threading.Thread(target=self._watch_loop, daemon=True, name="telegram-watch").start()

    def _poll_loop(self) -> None:
        while True:
            try:
                delay = self.poll_once()
            except Exception:
                logger.exception("Telegram bot round failed, will retry")
                delay = BACKOFF_SECONDS[-1]
            if delay:
                self._wake.wait(delay)
            self._wake.clear()

    def poll_once(self) -> float:
        """One round: what was written to the bot, read and answered. The
        seconds to wait before the next."""
        token = self.store.get()["bot_token"]
        if not token:
            self._token, self._api, self._ready, self._bot = None, None, False, None
            self._set_status("off")
            return IDLE_SECONDS
        if token != self._token:
            self._token, self._api, self._ready = token, self._api_factory(token), False
            self._offset, self._first_batch, self._retry = None, True, 0
        try:
            if not self._ready:
                if self._status["status"] == "off":
                    self._set_status("connecting")
                self._bot = self._api.call("getMe")
                # A webhook left set (another program, an old test) stops
                # getUpdates; this bot is LocalHome's.
                self._api.call("deleteWebhook")
                self._api.call("setMyCommands", commands=self.replies.commands())
                self._ready = True
            updates = self._api.call("getUpdates", http_timeout=POLL_SECONDS + 15, offset=self._offset,
                                     timeout=POLL_SECONDS, allowed_updates=["message", "callback_query"])
        except TelegramError as exc:
            return self._failed(exc)
        self._retry = 0
        self._set_status("running")
        first, self._first_batch = self._first_batch, False
        for update in updates or []:
            self._offset = int(update.get("update_id", 0)) + 1
            try:
                self.handle(update, backlog=first)
            except Exception:
                logger.exception("Telegram: a message couldn't be answered")
        return 0

    def _failed(self, exc: TelegramError) -> float:
        if exc.status in (401, 404):
            self._ready = False
            self._set_status("token_invalid", exc)
            return TOKEN_RETRY_SECONDS
        if exc.status == 409:
            self._set_status("conflict", exc)
            return CONFLICT_RETRY_SECONDS
        if exc.status == 429:
            return min(max(float(exc.retry_after or 5), 1.0), 300.0)
        self._retry += 1
        self._set_status("offline" if exc.status == 0 else "error", exc)
        return BACKOFF_SECONDS[min(self._retry, len(BACKOFF_SECONDS)) - 1]

    def _set_status(self, status: str, exc: Exception | None = None) -> None:
        with self._lock:
            before = self._status["status"]
            self._status = {"status": status, "error": str(exc) if exc else None}
        if status == before:
            return
        name = f"@{self._bot['username']}" if self._bot and self._bot.get("username") else "the bot"
        if status == "running":
            logger.info("Telegram: %s connected", name)
        elif status == "token_invalid":
            logger.warning("Telegram refused the bot's token (%s) - set a new one in the admin panel", exc)
        elif status == "conflict":
            logger.warning("Telegram: something else reads %s's messages too (%s) - "
                           "each LocalHome needs a bot of its own", name, exc)
        elif status in ("offline", "error"):
            logger.warning("Telegram not reachable (%s), will retry", exc)

    # ----------------------------------------------------------- answering --

    def handle(self, update: dict, backlog: bool = False) -> None:
        """One update: a message or a button pressed. `backlog`: from the
        first batch after a start - a button pressed then may be hours
        old (it carries no date of its own)."""
        if "callback_query" in update:
            self._handle_button(update["callback_query"], backlog)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        allowed = self._allowed(chat.get("id"))
        if allowed is None:
            self._note_waiting(chat)
            return
        text = (message.get("text") or "").strip()
        if not text or not self._for_me(text, chat):
            return
        stale = self._clock() - (message.get("date") or 0) > MAX_AGE_SECONDS
        reply = self.replies.message(text, control=allowed["control"], who=_person(message.get("from")), stale=stale)
        if reply is not None:
            self._send(chat["id"], reply)

    def _handle_button(self, query: dict, backlog: bool) -> None:
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        allowed = self._allowed(chat.get("id"))
        if allowed is None:
            self._call_send("answerCallbackQuery", callback_query_id=query.get("id"))
            return
        view, toast = self.replies.button(str(query.get("data") or ""), control=allowed["control"],
                                          who=_person(query.get("from")), stale=backlog)
        params = {"callback_query_id": query.get("id")}
        if toast:
            params["text"] = toast[:190]
        self._call_send("answerCallbackQuery", **params)
        if view is not None and message.get("message_id"):
            self._call_send("editMessageText", chat_id=chat["id"], message_id=message["message_id"],
                            text=split_message(view.text)[0], parse_mode="HTML", disable_web_page_preview=True,
                            reply_markup=_keyboard(view.buttons))

    def _for_me(self, text: str, chat: dict) -> bool:
        """In a group, only commands - the family's chatter isn't answered -
        and not those addressed to another bot there (/stato@other_bot)."""
        if chat.get("type") == "private":
            return True
        if not text.startswith("/"):
            return False
        _, _, addressed = text.split()[0].partition("@")
        mine = (self._bot or {}).get("username") or ""
        return not addressed or addressed.lower() == mine.lower()

    def _allowed(self, chat_id) -> dict | None:
        chat_id = _chat_id(chat_id)
        return next((c for c in self.store.get()["chats"] if c["id"] == chat_id), None) if chat_id is not None else None

    def _note_waiting(self, chat: dict) -> None:
        chat_id = _chat_id(chat.get("id"))
        kind = chat.get("type")
        if chat_id is None or kind not in ("private", "group", "supergroup"):
            return
        with self._lock:
            new = chat_id not in self._waiting
            self._waiting[chat_id] = {"id": chat_id, "name": _chat_name(chat),
                                      "kind": "private" if kind == "private" else "group", "at": self._clock()}
            for old in sorted(self._waiting.values(), key=lambda w: w["at"])[:-WAITING_MAX]:
                del self._waiting[old["id"]]
        if new:
            logger.info("Telegram: a chat not authorized wrote to the bot - authorize it in the admin panel")

    # ------------------------------------------------------------- sending --

    def _sender_api(self):
        token = self.store.get()["bot_token"]
        if not token:
            return None
        with self._lock:
            if token != self._sender_token:
                self._sender, self._sender_token = self._api_factory(token), token
            return self._sender

    def _call_send(self, method: str, **params) -> str | None:
        """None when it went through, else why not."""
        api = self._sender_api()
        if api is None:
            return "no bot"
        for attempt in range(2):
            try:
                with self._send_lock:
                    api.call(method, **params)
                return None
            except TelegramError as exc:
                if exc.status == 429 and attempt == 0 and (exc.retry_after or 0) <= 30:
                    time.sleep(exc.retry_after or 1)
                    continue
                if "message is not modified" in str(exc):
                    return None          # "refresh" with nothing new
                logger.warning("Telegram %s failed: %s", method, exc)
                return str(exc) or f"error {exc.status}"
        return "too many requests"

    def _send(self, chat_id: int, reply: Reply) -> str | None:
        parts = split_message(reply.text)
        error = None
        for i, part in enumerate(parts):
            params = {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
            if i == len(parts) - 1 and reply.buttons:
                params["reply_markup"] = _keyboard(reply.buttons)
            error = self._call_send("sendMessage", **params) or error
        return error

    def _tell(self, text: str) -> bool:
        """To every chat that gets the problems; True when one got it."""
        chats = [c for c in self.store.get()["chats"] if c["notify"]]
        results = [self._send(c["id"], Reply(text)) is None for c in chats]
        return any(results)

    def send(self, text: str) -> bool:
        return self._tell(html.escape(text))

    def send_event(self, key: str, params: dict, text: str) -> bool:
        return self._tell(self.replies.event(key, params) or html.escape(text))

    # ------------------------------------------------------------ problems --

    def _watch_loop(self) -> None:
        while True:
            time.sleep(WATCH_EVERY_SECONDS)
            try:
                self.watch_problems()
            except Exception:
                logger.exception("Telegram: problems check failed, will retry")

    def watch_problems(self, now: float | None = None) -> None:
        """The problems the home page lists: told once they've lasted a
        few minutes, and "solved" once gone a while - in one message. A
        device not heard from since the board came up is neither: its
        problem, told before a restart, stays told."""
        now = self._clock() if now is None else now
        data = self.store.get()
        if not data["bot_token"] or not any(c["notify"] for c in data["chats"]):
            return
        present, unknown = self.replies.problems()
        for key in present:
            self._first_seen.setdefault(key, now)
            self._last_seen[key] = now
        for key in list(self._first_seen):
            if key not in present:
                del self._first_seen[key]
        told = self._notified.get()
        new = {key: text for key, text in present.items()
               if key not in told and now - self._first_seen[key] >= NOTIFY_AFTER_SECONDS}
        solved = [key for key in told if key not in present and key.split("|", 1)[-1] not in unknown
                  and now - self._last_seen.get(key, self._started_at) >= RESOLVED_AFTER_SECONDS]
        if not new and not solved:
            return
        if self._tell(self.replies.problems_message(list(new.values()), [told[k] for k in solved])):
            self._notified.change(new, solved)

    # --------------------------------------------------------- admin panel --

    def status(self) -> dict:
        """What the admin panel shows - never the token."""
        data = self.store.get()
        authorized = {c["id"] for c in data["chats"]}
        now = self._clock()
        with self._lock:
            status = dict(self._status)
            waiting = [{"id": w["id"], "name": w["name"], "kind": w["kind"], "seconds_ago": round(now - w["at"])}
                       for w in sorted(self._waiting.values(), key=lambda w: -w["at"])
                       if w["id"] not in authorized and now - w["at"] < WAITING_KEEP_SECONDS]
        if not data["bot_token"]:
            status = {"status": "off", "error": None}
        bot = self._bot if data["bot_token"] else None
        return {"configured": bool(data["bot_token"]), **status,
                "bot": {"username": bot.get("username"), "name": bot.get("first_name")} if bot else None,
                "chats": data["chats"], "waiting": waiting}

    def connect(self, token: str) -> dict:
        """A token from the admin panel: tried with Telegram first, saved
        only if it knows it - then the bot starts on it at once."""
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise InputError("telegram_token_format", "that isn't a bot token from @BotFather")
        try:
            bot = self._api_factory(token).call("getMe")
        except TelegramError as exc:
            if exc.status in (401, 404):
                raise InputError("telegram_token_rejected", "Telegram doesn't know this token") from None
            raise InputError("telegram_unreachable", f"Telegram not reachable ({exc})") from None
        self.store.update(lambda data: data.update(bot_token=token))
        self._bot = bot
        self._set_status("connecting")
        self._wake.set()
        return {"username": bot.get("username"), "name": bot.get("first_name")}

    def forget(self) -> None:
        """The bot taken off this board: its token and chats gone."""
        self.store.update(lambda data: data.update(bot_token=None, chats=[]))
        self._notified.clear()
        with self._lock:
            self._waiting.clear()
        self._wake.set()

    def set_chat(self, chat_id, notify: bool | None = None, control: bool | None = None) -> dict:
        """A chat that wrote authorized (told everything, allowed
        everything), or an authorized one's two choices changed."""
        chat_id = _chat_id(chat_id)
        with self._lock:
            waiting = self._waiting.get(chat_id)

        def change(data):
            chat = next((c for c in data["chats"] if c["id"] == chat_id), None)
            if chat is None:
                if waiting is None:
                    raise InputError("telegram_chat_unknown", "no such chat has written to the bot")
                chat = {"id": chat_id, "name": waiting["name"], "kind": waiting["kind"], "notify": True, "control": True}
                data["chats"].append(chat)
            if notify is not None:
                chat["notify"] = bool(notify)
            if control is not None:
                chat["control"] = bool(control)

        data = self.store.update(change)
        return next(c for c in data["chats"] if c["id"] == chat_id)

    def remove_chat(self, chat_id) -> None:
        chat_id = _chat_id(chat_id)
        self.store.update(lambda data: data.update(chats=[c for c in data["chats"] if c["id"] != chat_id]))

    def test_message(self, chat_id) -> None:
        """A message to that chat now, from the admin panel."""
        chat = self._allowed(chat_id)
        if chat is None:
            raise InputError("telegram_chat_unknown", "no such chat is authorized")
        error = self._send(chat["id"], Reply(self.replies.test_message()))
        if error:
            raise InputError("telegram_send_failed", f"Telegram didn't deliver it: {error}", detail=error)
