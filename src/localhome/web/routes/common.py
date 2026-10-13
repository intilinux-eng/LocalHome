"""What every area's endpoints share: reading a request's JSON, refusing
with a code the pages translate, who a request came from, and waking a
device's poller after a command. The log keeps the name it had before the
endpoints were split by area ("localhome.web.app"): the board's log lines,
and whatever filters them, read as they did."""
from __future__ import annotations

import logging

from flask import jsonify, request

from localhome.core.manager import DeviceManager

logger = logging.getLogger("localhome.web.app")


def _wake_poller(manager: DeviceManager, name: str) -> None:
    """The command went through: re-read that device now. For a paired
    switch/number (the VMC's on/off and speed) the partner too - one
    device, so both cards should come back together."""
    names = {name, manager.paired_switch.get(name)}
    names |= {number for number, switch in manager.paired_switch.items() if switch == name}
    for key in names - {None}:
        wake = getattr(manager.pollers.get(key), "wake", None)
        if wake is not None:
            wake()


def _fail(code: str, message: str, status: int = 400, **params):
    """A refusal with a code the pages translate (errors.<code>) - the
    English message stays for logs and API callers. See localhome/errors.py."""
    return jsonify(ok=False, error=message, code=code, params=params), status


def _refusal(exc: Exception, status: int = 400):
    """A validation error as an answer: its code and parameters when it's
    an InputError, the plain message otherwise."""
    return jsonify(ok=False, error=str(exc), code=getattr(exc, "code", None),
                   params=getattr(exc, "params", None) or {}), status


def _json_body() -> dict:
    """The request's JSON body when it's an object, else an empty one. A
    body that parses but isn't an object (a bare number, a string) used to
    reach `.get()` in every POST route and come back as a 500."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# The kind of device a request came from, by its browser's own words,
# and its address: "iPhone (192.168.1.40)". Enough to tell the family's
# phones from the PC on the desk; nothing is stored about anyone.
_DEVICE_WORDS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                 ("Macintosh", "Mac"), ("Linux", "Linux"))


def _who(req) -> str:
    agent = req.headers.get("User-Agent", "")
    device = next((name for word, name in _DEVICE_WORDS if word in agent), None)
    address = req.remote_addr or "?"
    return f"{device} ({address})" if device else address
