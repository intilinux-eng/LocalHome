"""Two small, generic building blocks for a physical dependency that has
nothing to do with the thermostat's own temperature-driven logic:

- `Interlock` forces one switch to mirror another switch's on-state.
  The motivating case: a "water-based" dehumidifier that draws chilled
  water through a specific zone's valve to condense moisture out of the
  air, so that valve must be open whenever the dehumidifier runs, even
  though the zone itself may be excluded from normal cooling control
  (see `Zone.cool_enabled` in services/thermostat.py) - a bathroom's
  valve staying closed to avoid condensation on its own radiant panel
  doesn't mean it can stay closed when something else downstream needs
  the water. Nothing about this is dehumidifier-specific; any "switch A
  being on requires switch B to also be on" relationship fits.

- `ModeSwitch` forces one switch to mirror the thermostat's current
  mode directly, with no leader switch involved. The motivating case: a
  cooling-only bypass valve that must always be open in "cool" and
  always closed otherwise - unlike an `Interlock` follower, which is
  left alone entirely outside its `active_in_mode` so whatever else
  owns it (typically the thermostat) isn't fought over, a `ModeSwitch`
  has no other owner at all, so leaving its mode actively closes it
  rather than leaving it in whatever state it was.

- `ModeLimitedSwitch` is a switch someone controls by hand, but only
  while the thermostat is in one mode; in any other mode it's forced off.
  The motivating case: a dehumidifier, usable only while cooling - switch
  to heating and it's turned off (and its card hidden, via
  `visible_in_mode`), instead of staying on unseen.

- `SeasonRule` is the general form of the two above: for each season,
  what a switch does - "on" or "off" (forced by LocalHome) or "manual"
  (left to whoever uses it). A bypass valve is {heat: off, cool: on}, a
  dehumidifier {heat: off, cool: manual}. Rules come from a provider
  called on every tick, so ones edited in the admin panel apply without
  a restart. See season_rules_of() for how the older two map onto it.

Away mode switches off what's left to whoever uses it ("manual", a
ModeLimitedSwitch in its mode): nobody home runs the dehumidifier. What
the season forces on or off (a bypass valve) is the plant's, and stays.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from localhome.services.log_throttle import FailureLog

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


@dataclass
class ModeSwitch:
    switch: str
    active_in_mode: str


@dataclass
class ModeLimitedSwitch:
    switch: str
    only_in_mode: str


SEASON_ACTIONS = ("on", "off", "manual")


@dataclass
class SeasonRule:
    switch: str
    # mode -> "on" / "off" / "manual"; a mode not listed is "manual".
    actions: dict

    def action(self, mode: str | None) -> str:
        return self.actions.get(mode, "manual")


def season_rules_of(mode_switches, mode_limited, modes=("heat", "cool")) -> list[SeasonRule]:
    """The older ModeSwitch/ModeLimitedSwitch forms, as SeasonRules."""
    rules = [SeasonRule(m.switch, {mode: "on" if mode == m.active_in_mode else "off" for mode in modes})
             for m in mode_switches]
    rules += [SeasonRule(m.switch, {mode: "manual" if mode == m.only_in_mode else "off" for mode in modes})
              for m in mode_limited]
    return rules


class InterlockController:
    def __init__(
        self,
        manager,
        interlocks: list[Interlock],
        mode_switches: list[ModeSwitch] = (),
        mode_provider=None,
        mode_limited: list[ModeLimitedSwitch] = (),
        diagnostic_mode_provider=None,
        poll_interval_seconds: float = 20,
        season_rules_provider=None,
        away_provider=None,
    ):
        self.manager = manager
        self.interlocks = interlocks
        self.mode_switches = list(mode_switches)
        self.mode_limited = list(mode_limited)
        self._missing_warned: set[str] = set()
        self._failures = FailureLog(logger)
        self.mode_provider = mode_provider  # callable returning the current thermostat mode, or None
        self.diagnostic_mode_provider = diagnostic_mode_provider  # callable returning bool, or None
        self.away_provider = away_provider  # callable returning bool (away mode on), or None
        # callable returning the current list[SeasonRule], or None - read on
        # every tick, so a rule changed in the admin panel applies at once.
        self.season_rules_provider = season_rules_provider
        self.poll_interval_seconds = poll_interval_seconds
        self._started = False
        # As in ThermostatController: the loop thread and the request
        # thread (a season change ticks both controllers) must not
        # interleave commands to the same switch.
        self._tick_lock = threading.RLock()

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
        with self._tick_lock:
            self._tick()

    def _tick(self) -> None:
        if self.diagnostic_mode_provider and self.diagnostic_mode_provider():
            # Same "hands off everything" stance as
            # ThermostatController.tick() - a leader/follower or
            # mode-switch relationship fighting the installer's manual
            # toggle in /climate-admin would defeat the point of a
            # diagnosis mode entirely.
            return
        current_mode = self.mode_provider() if self.mode_provider else None
        away = bool(self.away_provider and self.away_provider())
        for mode_switch in self.mode_switches:
            self._apply(mode_switch.switch, current_mode == mode_switch.active_in_mode)
        for limited in self.mode_limited:
            # Left to the user inside its mode - unless nobody's home; only
            # ever forced off.
            if away or current_mode != limited.only_in_mode:
                self._apply(limited.switch, False)
        for rule in (self.season_rules_provider() if self.season_rules_provider else []):
            action = rule.action(current_mode)
            if action != "manual":
                self._apply(rule.switch, action == "on")
            elif away:
                self._apply(rule.switch, False)
        # Last: a leader switched off above takes its follower with it in
        # this same tick.
        for link in self.interlocks:
            if link.active_in_mode is not None and link.active_in_mode != current_mode:
                continue
            leader_reading = self.manager.sensor_reading(link.leader)
            # A leader that isn't answering can't be confirmed running, so
            # its follower closes: e.g. a dehumidifier gone offline mustn't
            # leave the bathroom valve feeding it open indefinitely.
            leader_on = bool(leader_reading.get("ok") and leader_reading.get("is_on"))
            self._apply(link.follower, leader_on)

    def _apply(self, follower_name: str, desired_on: bool) -> None:
        switch = self.manager.switches.get(follower_name)
        if switch is None:
            # Normal on an apartment without that device (no bypass valve
            # wired yet): say so once, not on every tick for as long as the
            # process runs.
            if follower_name not in self._missing_warned:
                self._missing_warned.add(follower_name)
                logger.warning("%r is named in the thermostat config but isn't a known switch - skipping it", follower_name)
            return
        current = self.manager.sensor_reading(follower_name)
        if current.get("ok") and bool(current.get("is_on")) == desired_on:
            return
        try:
            switch.turn_on() if desired_on else switch.turn_off()
        except Exception:
            self._failures.failed(follower_name, "Failed to %s %r", "switch on" if desired_on else "switch off", follower_name)
            return
        self._failures.succeeded(follower_name)
        poller = self.manager.pollers.get(follower_name)
        if poller is not None:
            poller.cache.merge({"is_on": desired_on})
