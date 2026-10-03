"""What the admin panel shows of config/mqtt_broker.json: where the
broker is and whether with a user. The file is written by hand (see
docs/integrations/mqtt.md, "Configure the broker connection") - set once
at installation, then a restart. The password never leaves the board:
only whether there is one.
"""
from __future__ import annotations

from localhome.util.jsonfile import load_json


def get_broker(path: str) -> dict:
    try:
        broker = load_json(path)
    except (OSError, ValueError):
        broker = {}
    return {
        "host": broker.get("host"),
        "port": broker.get("port", 1883),
        "username": broker.get("username"),
        "has_password": bool(broker.get("password")),
    }
