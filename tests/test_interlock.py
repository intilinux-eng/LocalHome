from localhome.services.interlock import Interlock, InterlockController, ModeLimitedSwitch, ModeSwitch


class FakeSwitch:
    def __init__(self):
        self.calls: list[str] = []

    def turn_on(self):
        self.calls.append("on")

    def turn_off(self):
        self.calls.append("off")


class FakePoller:
    def __init__(self):
        self.merged: list[dict] = []
        self.cache = self

    def merge(self, partial):
        self.merged.append(partial)


class FakeManager:
    def __init__(self, readings: dict[str, dict], switches: dict[str, FakeSwitch]):
        self._readings = readings
        self.switches = switches
        self.pollers = {name: FakePoller() for name in switches}

    def sensor_reading(self, name):
        return self._readings.get(name, {"ok": False})


def test_follower_turns_on_when_leader_is_on():
    follower = FakeSwitch()
    manager = FakeManager(
        readings={"Dehumidifier": {"ok": True, "is_on": True}, "Bathrooms Valve": {"ok": True, "is_on": False}},
        switches={"Bathrooms Valve": follower},
    )
    controller = InterlockController(manager, [Interlock(leader="Dehumidifier", follower="Bathrooms Valve")])

    controller.tick()

    assert follower.calls == ["on"]


def test_follower_turns_off_when_leader_is_off():
    follower = FakeSwitch()
    manager = FakeManager(
        readings={"Dehumidifier": {"ok": True, "is_on": False}, "Bathrooms Valve": {"ok": True, "is_on": True}},
        switches={"Bathrooms Valve": follower},
    )
    controller = InterlockController(manager, [Interlock(leader="Dehumidifier", follower="Bathrooms Valve")])

    controller.tick()

    assert follower.calls == ["off"]


def test_follower_is_not_recommanded_when_already_matching():
    follower = FakeSwitch()
    manager = FakeManager(
        readings={"Dehumidifier": {"ok": True, "is_on": True}, "Bathrooms Valve": {"ok": True, "is_on": True}},
        switches={"Bathrooms Valve": follower},
    )
    controller = InterlockController(manager, [Interlock(leader="Dehumidifier", follower="Bathrooms Valve")])

    controller.tick()

    assert follower.calls == []


def test_scoped_to_a_mode_does_nothing_outside_it():
    follower = FakeSwitch()
    manager = FakeManager(
        readings={"Dehumidifier": {"ok": True, "is_on": True}, "Bathrooms Valve": {"ok": True, "is_on": False}},
        switches={"Bathrooms Valve": follower},
    )
    controller = InterlockController(
        manager,
        [Interlock(leader="Dehumidifier", follower="Bathrooms Valve", active_in_mode="cool")],
        mode_provider=lambda: "heat",
    )

    controller.tick()

    assert follower.calls == []  # not in cool mode - thermostat owns this valve instead


def test_scoped_to_a_mode_acts_when_mode_matches():
    follower = FakeSwitch()
    manager = FakeManager(
        readings={"Dehumidifier": {"ok": True, "is_on": True}, "Bathrooms Valve": {"ok": True, "is_on": False}},
        switches={"Bathrooms Valve": follower},
    )
    controller = InterlockController(
        manager,
        [Interlock(leader="Dehumidifier", follower="Bathrooms Valve", active_in_mode="cool")],
        mode_provider=lambda: "cool",
    )

    controller.tick()

    assert follower.calls == ["on"]


def test_unreachable_leader_closes_its_follower():
    # A dehumidifier that stopped answering can't be confirmed running, so
    # the bathroom valve feeding it closes instead of staying open.
    follower = FakeSwitch()
    manager = FakeManager(readings={"Bathrooms Valve": {"ok": True, "is_on": True}}, switches={"Bathrooms Valve": follower})
    controller = InterlockController(manager, [Interlock(leader="Missing Leader", follower="Bathrooms Valve")])

    controller.tick()  # must not raise

    assert follower.calls == ["off"]


# ----------------------------------------------------------- ModeSwitch ----
# No leader switch involved at all - a bypass valve with no other owner,
# fully driven by the thermostat's current mode.

def test_mode_switch_turns_on_when_mode_matches():
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bypass Valve": {"ok": True, "is_on": False}}, switches={"Bypass Valve": switch})
    controller = InterlockController(
        manager, [], [ModeSwitch(switch="Bypass Valve", active_in_mode="cool")], mode_provider=lambda: "cool",
    )

    controller.tick()

    assert switch.calls == ["on"]


def test_mode_switch_turns_off_when_mode_does_not_match():
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bypass Valve": {"ok": True, "is_on": True}}, switches={"Bypass Valve": switch})
    controller = InterlockController(
        manager, [], [ModeSwitch(switch="Bypass Valve", active_in_mode="cool")], mode_provider=lambda: "heat",
    )

    controller.tick()

    # Unlike an Interlock follower, a ModeSwitch has no other owner, so
    # leaving its mode actively closes it rather than leaving it alone.
    assert switch.calls == ["off"]


def test_mode_switch_is_not_recommanded_when_already_matching():
    switch = FakeSwitch()
    manager = FakeManager(readings={"Bypass Valve": {"ok": True, "is_on": True}}, switches={"Bypass Valve": switch})
    controller = InterlockController(
        manager, [], [ModeSwitch(switch="Bypass Valve", active_in_mode="cool")], mode_provider=lambda: "cool",
    )

    controller.tick()

    assert switch.calls == []


def test_interlocks_and_mode_switches_apply_independently_in_the_same_tick():
    follower = FakeSwitch()
    bypass = FakeSwitch()
    manager = FakeManager(
        readings={
            "Dehumidifier": {"ok": True, "is_on": False},
            "Bathrooms Valve": {"ok": True, "is_on": False},
            "Bypass Valve": {"ok": True, "is_on": False},
        },
        switches={"Bathrooms Valve": follower, "Bypass Valve": bypass},
    )
    controller = InterlockController(
        manager,
        [Interlock(leader="Dehumidifier", follower="Bathrooms Valve", active_in_mode="cool")],
        [ModeSwitch(switch="Bypass Valve", active_in_mode="cool")],
        mode_provider=lambda: "cool",
    )

    controller.tick()

    # Dehumidifier is off, so its interlock leaves the bathroom valve
    # alone - but the bypass valve always opens in cool mode regardless.
    assert follower.calls == []
    assert bypass.calls == ["on"]


def test_diagnostic_mode_stops_every_interlock_and_mode_switch():
    follower = FakeSwitch()
    bypass = FakeSwitch()
    manager = FakeManager(
        readings={
            "Dehumidifier": {"ok": True, "is_on": True},
            "Bathrooms Valve": {"ok": True, "is_on": False},
            "Bypass Valve": {"ok": True, "is_on": False},
        },
        switches={"Bathrooms Valve": follower, "Bypass Valve": bypass},
    )
    controller = InterlockController(
        manager,
        [Interlock(leader="Dehumidifier", follower="Bathrooms Valve")],
        [ModeSwitch(switch="Bypass Valve", active_in_mode="cool")],
        mode_provider=lambda: "cool",
        diagnostic_mode_provider=lambda: True,
    )

    controller.tick()

    # Both would normally fire (leader on, mode matches) - diagnostic
    # mode leaves every switch exactly as the installer set it manually.
    assert follower.calls == []
    assert bypass.calls == []


# ---------------------------------------------------- ModeLimitedSwitch ----
# A dehumidifier: switched by hand, but only while cooling.

def _limited(mode, is_on):
    switch = FakeSwitch()
    manager = FakeManager(readings={"Dehumidifier": {"ok": True, "is_on": is_on}}, switches={"Dehumidifier": switch})
    controller = InterlockController(
        manager, [], mode_provider=lambda: mode,
        mode_limited=[ModeLimitedSwitch(switch="Dehumidifier", only_in_mode="cool")],
    )
    return controller, switch


def test_mode_limited_switch_is_forced_off_outside_its_mode():
    controller, switch = _limited("heat", is_on=True)
    controller.tick()
    assert switch.calls == ["off"]


def test_mode_limited_switch_is_left_to_the_user_in_its_mode():
    for is_on in (True, False):
        controller, switch = _limited("cool", is_on=is_on)
        controller.tick()
        assert switch.calls == []


def test_a_missing_switch_is_reported_once_not_every_tick(caplog):
    # An apartment without a bypass valve wired yet, while the config names it.
    controller = InterlockController(
        FakeManager(readings={}, switches={}), [], [ModeSwitch(switch="Bathroom Bypass Valve", active_in_mode="cool")],
        mode_provider=lambda: "cool",
    )
    for _ in range(5):
        controller.tick()
    warnings = [r for r in caplog.records if "Bathroom Bypass Valve" in r.getMessage()]
    assert len(warnings) == 1
