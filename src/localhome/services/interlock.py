"""Forces one switch to mirror another switch's on-state - a small,
generic building block for a physical dependency between two devices
that has nothing to do with the thermostat's own temperature-driven
logic. The motivating case: a "water-based" dehumidifier that draws
chilled water through a specific zone's valve to condense moisture out
of the air, so that valve must be open whenever the dehumidifier runs,
even though the zone itself may be excluded from normal cooling control
(see `Zone.cool_enabled` in services/thermostat.py) - a bathroom's valve
staying closed to avoid condensation on its own radiant panel doesn't
mean it can stay closed when something else downstream needs the water.

Nothing about this is dehumidifier-specific; any "switch A being on
requires switch B to also be on" relationship fits.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Interlock:
    leader: str
    follower: str
    # If set, this interlock only takes effect while the thermostat's
    # shared mode matches (e.g. "cool") - outside that mode, the follower
    # switch is left alone entirely, the same "hands off, not even a
    # forced close" stance Zone.cool_enabled takes, so whatever else
    # normally owns that switch (e.g. the thermostat, in heat mode)
    # doesn't get fought over it. None means "always active".
    active_in_mode: str | None = None


class InterlockController:
    def __init__(
        self,
        manager,
        interlocks: list[Interlock],
        mode_provider=None,
        poll_interval_seconds: float = 20,
    ):
        self.manager = manager
        self.interlocks = interlocks
        self.mode_provider = mode_provider  # callable returning the current thermostat mode, or None
        self.poll_interval_seconds = poll_interval_seconds
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Interlock control loop failed, will retry next interval")
            time.sleep(self.poll_interval_seconds)

    def tick(self) -> None:
        current_mode = self.mode_provider() if self.mode_provider else None
        for link in self.interlocks:
            if link.active_in_mode is not None and link.active_in_mode != current_mode:
                continue
            leader_reading = self.manager.sensor_reading(link.leader)
            if not leader_reading.get("ok"):
                continue
            self._apply(link.follower, bool(leader_reading.get("is_on")))

    def _apply(self, follower_name: str, desired_on: bool) -> None:
        switch = self.manager.switches.get(follower_name)
        if switch is None:
            logger.warning("Interlock follower %r is not a known switch, skipping", follower_name)
            return
        current = self.manager.sensor_reading(follower_name)
        if current.get("ok") and bool(current.get("is_on")) == desired_on:
            return
        try:
            switch.turn_on() if desired_on else switch.turn_off()
        except Exception:
            logger.exception("Failed to %s interlock follower %r", "open" if desired_on else "close", follower_name)
            return
        poller = self.manager.pollers.get(follower_name)
        if poller is not None:
            poller.cache.merge({"is_on": desired_on})
