"""Follows every cover all the time, so where each one is stays right
whoever moves it - LocalHome, a wall button, an app.

A cover that says each change by itself (a Tuya curtain switch, on a
connection kept open: see drivers/tuya/cover.py) is heard as it moves; one
that can't is asked its state every few seconds. Each state goes to
services.position_control, which turns the runs into a position. Whether
each cover is reached is kept in the same shape the dashboard's own reads
used to leave (app.config["COVER_READS"]), so one that stops answering
shows in the system status at once, page open or not.

One thread per cover: a handful of shutters, and a module that's off only
costs its own thread's wait.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
POLL_MAX_SECONDS = 300


class CoverWatcher:
    def __init__(self, manager, positions, reads: dict):
        self.manager = manager
        self.positions = positions
        self.reads = reads
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for cover in self.manager.covers.values():
            thread = threading.Thread(target=self._follow, args=(cover,), name=f"cover-{cover.name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def _follow(self, cover) -> None:
        try:
            if cover.listen(lambda state: self._state(cover, state),
                            lambda ok, error: self._link(cover, ok, error), self._stop):
                return
        except Exception:
            logger.exception("%s: listening failed - asking it every %s s instead", cover.name, POLL_SECONDS)
        self._poll(cover)

    def _poll(self, cover) -> None:
        wait = POLL_SECONDS
        while not self._stop.is_set():
            try:
                state = cover.status()["state"]
                self._link(cover, True, None)
                self._state(cover, state)
                wait = POLL_SECONDS
            except Exception as exc:  # not answering: said, and asked again later
                self._link(cover, False, str(exc))
                wait = min(wait * 2, POLL_MAX_SECONDS)
            self._stop.wait(wait)

    def _state(self, cover, state) -> None:
        if state in ("open", "close", "stop"):
            self.positions.observe_state(cover, state)

    def _link(self, cover, ok: bool, error: str | None) -> None:
        now = time.time()
        previous = self.reads.get(cover.name) or {}
        if ok:
            self.reads[cover.name] = {"ok": True, "at": now}
        else:
            failures = (previous.get("failures", 0) if not previous.get("ok") else 0) + 1
            self.reads[cover.name] = {"ok": False, "error": error, "code": None, "at": now, "failures": failures}
