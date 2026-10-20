"""The admin panel's Telegram section: the board's bot connected with its
token from @BotFather, the chats that wrote to it authorized - each told
the problems or not, allowed to change things or only to look - a test
message, the bot taken off. See services/telegram_bot.py.

The token goes in, never out: no answer here carries it."""
from __future__ import annotations

from flask import Flask, jsonify

from localhome.errors import InputError
from localhome.services.telegram_bot import TelegramBot
from localhome.web.routes.common import _fail, _json_body, _refusal, logger


def register(app: Flask) -> None:
    def bot() -> TelegramBot:
        return app.config["TELEGRAM"]

    @app.route("/api/climate-admin/telegram")
    def api_telegram():
        return jsonify(ok=True, **bot().status())

    @app.route("/api/climate-admin/telegram/token", methods=["POST"])
    def api_telegram_token():
        """{"token"}: tried with Telegram, then saved - refused as it is
        when it isn't a token, Telegram doesn't know it, or can't be
        reached."""
        try:
            found = bot().connect(_json_body().get("token"))
        except InputError as exc:
            return _refusal(exc, 502 if exc.code == "telegram_unreachable" else 400)
        logger.info("Telegram bot @%s connected from the admin panel", found.get("username"))
        return jsonify(ok=True, **bot().status())

    @app.route("/api/climate-admin/telegram/remove", methods=["POST"])
    def api_telegram_remove():
        bot().forget()
        logger.info("Telegram bot removed from the admin panel")
        return jsonify(ok=True, **bot().status())

    @app.route("/api/climate-admin/telegram/chats/<chat_id>", methods=["POST"])
    def api_telegram_chat(chat_id):
        """A chat that wrote, authorized; or {"notify", "control"} changed."""
        body = _json_body()
        values = {}
        for key in ("notify", "control"):
            if key in body:
                if not isinstance(body[key], bool):
                    return _fail("telegram_choice_invalid", f"{key} must be true or false")
                values[key] = body[key]
        try:
            bot().set_chat(chat_id, **values)
        except InputError as exc:
            return _refusal(exc, 404)
        return jsonify(ok=True, **bot().status())

    @app.route("/api/climate-admin/telegram/chats/<chat_id>/remove", methods=["POST"])
    def api_telegram_chat_remove(chat_id):
        bot().remove_chat(chat_id)
        return jsonify(ok=True, **bot().status())

    @app.route("/api/climate-admin/telegram/chats/<chat_id>/test", methods=["POST"])
    def api_telegram_chat_test(chat_id):
        try:
            bot().test_message(chat_id)
        except InputError as exc:
            return _refusal(exc, 404 if exc.code == "telegram_chat_unknown" else 502)
        return jsonify(ok=True)
