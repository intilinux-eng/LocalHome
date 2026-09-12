import time

from localhome.services.position_control import PositionStore


class FakeCover:
    """A minimal CoverDriver stand-in: records what it was told, with no
    real device on the other end."""

    def __init__(self, cover_id: str, travel_time: float = 10.0):
        self.id = cover_id
        self.name = cover_id
        self.sent: list[str] = []
        self._travel_time = travel_time

    def status(self):
        return {"state": "unknown"}

    def send(self, action: str) -> None:
        self.sent.append(action)

    def read_travel_time(self, default: float = 15.0) -> float:
        return self._travel_time


def test_defaults_to_100_percent_when_uncalibrated(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    assert store.get("unknown-cover") == 100
    assert store.is_calibrated("unknown-cover") is False


def test_move_to_percent_sends_direction_and_estimates_duration(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 0)  # starts fully closed

    result = store.move_to_percent(cover, 50)

    assert result["moved"] is True
    assert result["direction"] == "open"
    assert result["duration"] == 5.0  # half the travel time for a 50% move
    assert cover.sent == ["open"]


def test_move_to_percent_is_a_noop_when_already_there(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1")
    store.set(cover.id, 80)

    result = store.move_to_percent(cover, 80)

    assert result["moved"] is False
    assert cover.sent == []


def test_observe_state_records_completed_external_move(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1")
    store.set(cover.id, 0)

    # Something other than this app (e.g. a voice-assistant routine) drives
    # the motor directly: we see it start opening, then stop.
    store.observe_state(cover, "open")
    store.observe_state(cover, "stop")

    assert store.get(cover.id) == 100


def test_get_interpolates_towards_the_target_while_a_move_is_in_flight(tmp_path):
    # Regression test: a status poll partway through a slider-triggered move
    # used to still report the pre-move position (the stored value only
    # updates when the motor timer fires), which made the dashboard slider
    # snap back to its old spot until the whole move finished.
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=1.0)
    store.set(cover.id, 100)

    store.move_to_percent(cover, 0)
    time.sleep(0.4)

    mid = store.get(cover.id)
    assert 0 < mid < 100

    time.sleep(0.8)  # past the 1s travel time - the timer has fired by now
    assert store.get(cover.id) == 0


def test_cancel_pending_move_stops_interpolating(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 100)

    store.move_to_percent(cover, 0)
    time.sleep(0.1)
    assert store.get(cover.id) < 100

    store.cancel_pending_move(cover.id)

    # No sensor to confirm how far it actually got, so this falls back to
    # the last committed value rather than a frozen mid-flight guess.
    assert store.get(cover.id) == 100


def test_observe_state_ignores_moves_this_app_triggered(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1")
    store.set(cover.id, 0)

    store.mark_own_command(cover.id)
    store.observe_state(cover, "open")
    store.observe_state(cover, "stop")

    # Not attributed to an external move, so the position estimate is left
    # alone - move_to_percent()/finish() is what would normally update it.
    assert store.get(cover.id) == 0
