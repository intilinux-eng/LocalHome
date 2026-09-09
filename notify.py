"""Telegram push notifications, used for power-threshold alerts (see history.py).

A bot's sendMessage API is a single HTTPS POST with no OAuth/session dance,
which is simpler and more stable than Alexa's unofficial cookie-based API.
"""
import json

import requests

SECRETS_FILE = "secrets_telegram.json"


def send(text: str) -> bool:
    try:
        creds = json.load(open(SECRETS_FILE))
    except FileNotFoundError:
        return False

    url = f"https://api.telegram.org/bot{creds['bot_token']}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": creds["chat_id"], "text": text}, timeout=10)
        return resp.ok
    except requests.RequestException:
        return False
