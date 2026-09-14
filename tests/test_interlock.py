from localhome.services.interlock import Interlock, InterlockController


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


def test_unknown_leader_reading_is_skipped_gracefully():
    follower = FakeSwitch()
    manager = FakeManager(readings={}, switches={"Bathrooms Valve": follower})
    controller = InterlockController(manager, [Interlock(leader="Missing Leader", follower="Bathrooms Valve")])

    controller.tick()  # must not raise

    assert follower.calls == []
