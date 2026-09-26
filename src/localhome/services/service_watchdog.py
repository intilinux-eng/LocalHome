"""Telling systemd that LocalHome is alive - the other half of
deploy/tune-board.sh's step 8 (Type=notify, WatchdogSec=120).

A crash was always restarted; a hang wasn't - a deadlocked web server or
a control loop stuck on a device that never answers left the board
looking on and doing nothing, for as long as nobody came by. Now systemd
hears "ready" once the web page first answers, then "alive" every third
of WatchdogSec for as long as

- the web page answers (a request to itself), and
- no control loop is stalled (system_status.loop_health: three missed
  cycles).

When either fails it stays silent - and says why in the log, once - and
systemd restarts LocalHome when WatchdogSec runs out.

Nothing at all where systemd didn't ask for it (no NOTIFY_SOCKET): a PC,
Docker, the tests, a board whose service hasn't been set up for it.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)


def notify(message: str, env=os.environ) -> bool:
    """One sd_notify message; False where systemd isn't listening."""
    address = env.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]   # an abstract socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())
    return True


def _fetch(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10) as response:
        response.read()


class ServiceWatchdog:
    def __init__(self, url: str, loops_provider, env=os.environ, sender=notify, fetch=_fetch,
                 event_log: str | None = None):
        self.url = url
        # Where what it did is kept for the admin panel's watchdog history
        # - see system_status.append_event().
        self.event_log = event_log
        self.loops_provider = loops_provider
        self.env = env
        self.sender = sender
        self.fetch = fetch
        usec = int(env.get("WATCHDOG_USEC") or 0)
        # Three chances within the timeout: one missed ping isn't a restart.
        self.interval = usec / 1_000_000 / 3 if usec else None
        self.ready = False
        self._last_problem = None

    @property
    def wanted(self) -> bool:
        return bool(self.env.get("NOTIFY_SOCKET"))

    def problem(self) -> str | None:
        """Why LocalHome shouldn't say it's alive right now, or None."""
        try:
            self.fetch(self.url)
        except Exception as exc:
            return f"its own web page doesn't answer ({exc})"
        stalled = [loop["name"] for loop in self.loops_provider() if loop.get("stalled")]
        if stalled:
            return f"control loop stalled: {', '.join(stalled)}"
        return None

    def step(self) -> None:
        problem = self.problem()
        if problem and not self.ready:
            return   # still starting: the page not answering yet is normal
        if problem:
            if problem != self._last_problem:
                logger.error("Not telling systemd LocalHome is alive: %s - it will restart LocalHome "
                             "if this lasts", problem)
                self._event("unhealthy", problem)
            self._last_problem = problem
            return
        if self._last_problem:
            logger.info("LocalHome is healthy again")
            self._event("healthy")
            self._last_problem = None
        if not self.ready:
            self.sender("READY=1", self.env)
            self.ready = True
            logger.info("Told systemd LocalHome is ready%s",
                        f" - alive every {self.interval:.0f}s" if self.interval else "")
        elif self.interval:
            self.sender("WATCHDOG=1", self.env)

    def _event(self, code: str, *values) -> None:
        if not self.event_log:
            return
        from localhome.services.system_status import append_event
        try:
            append_event(self.event_log, code, *values)
        except OSError:
            logger.warning("Couldn't write the watchdog history to %s", self.event_log)

    def note_start(self) -> None:
        """At start-up: if the last thing on record is "unhealthy", this is
        most likely systemd's restart for it - say so in the history."""
        if not self.event_log:
            return
        from localhome.services.system_status import read_events
        events = read_events(self.event_log, "localhome")
        if events and events[-1]["code"] == "unhealthy":
            self._event("restarted")

    def start(self) -> None:
        if not self.wanted:
            return
        self.note_start()
        threading.Thread(target=self._loop, daemon=True, name="service-watchdog").start()

    def _loop(self) -> None:
        time.sleep(2)   # the server is still starting
        while True:
            try:
                self.step()
            except Exception:
                logger.exception("Service watchdog step failed")
            # Before "ready", look often: systemd is waiting for it.
            time.sleep(self.interval if self.ready and self.interval else 2)
