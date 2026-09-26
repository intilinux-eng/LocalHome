"""Lets the climate admin panel edit config/mqtt_broker.json directly
from the web - previously always a hand-edit (see docs/integrations/
mqtt.md's "Configure the broker connection" step). This is a bigger
exposure than the rest of the panel (device ids only): a saved MQTT
password can now be written through an unauthenticated LAN form unless
the operator has turned on `web.auth` (see web/auth.py) - a deliberate
trade for being able to do a full install from a tablet, not an
oversight.

The password is never read back out to the browser once saved -
get_broker() only reports whether one is currently set, never its
value - matching how every other "edit a secret" form on the web
behaves, and see set_broker()'s `change_password` for how the browser
asks to actually change it.
"""
from __future__ import annotations

from localhome.errors import InputError
from localhome.util.jsonfile import load_json, save_json_atomic


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


def set_broker(
    path: str,
    host,
    port,
    username: str | None,
    password: str | None,
    change_password: bool,
) -> None:
    """Validates before writing anything, like every other store in this
    project (see services/climate_map_store.py's set_zones()). `password`
    is only looked at when `change_password` is true - otherwise whatever
    password is already on disk (if any) is kept untouched, so leaving
    the field blank in the form never silently drops it back to
    anonymous."""
    if not isinstance(host, str) or not host.strip():
        raise InputError("broker_host_required", "host is required")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise InputError("broker_port_invalid", "port must be a number between 1 and 65535")

    broker: dict = {"host": host.strip(), "port": port}
    if username and username.strip():
        broker["username"] = username.strip()

    if change_password:
        if password:
            broker["password"] = password
    else:
        try:
            existing = load_json(path)
        except (OSError, ValueError):
            existing = {}
        if existing.get("password"):
            broker["password"] = existing["password"]

    save_json_atomic(path, broker)
