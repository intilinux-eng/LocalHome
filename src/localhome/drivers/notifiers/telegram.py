"""Telegram push notifications. A bot's sendMessage API is a single HTTPS
POST with no OAuth/session dance, which makes it a simple default
notification channel for a local-first project.

Implement core.interfaces.Notifier for anything else (Pushover, ntfy, a
local MQTT topic, ...) - see docs/adding-a-driver.md.
"""
from __future__ import annotations

import requests

from localhome.core.interfaces import Notifier
from localhome.core.registry import register_driver
from localhome.util.jsonfile import load_json


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            resp = requests.post(url, json={"chat_id": self._chat_id, "text": text}, timeout=10)
            return resp.ok
        except requests.RequestException:
            return False


@register_driver("notifier", "telegram")
def _create(options: dict) -> TelegramNotifier:
    credentials = load_json(options["secrets_file"])
    return TelegramNotifier(bot_token=credentials["bot_token"], chat_id=credentials["chat_id"])
