import threading
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


def test_a_full_run_by_someone_else_ends_fully_open(tmp_path):
    # A wall button opens it: the module says "open", and "stop" when its
    # run timer is over - a run as long as the travel time reached the end.
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 0)

    store.observe_state(cover, "open", at=1000.0)
    store.observe_state(cover, "stop", at=1010.0)

    assert store.get(cover.id) == 100


def test_a_run_stopped_half_way_ends_half_way(tmp_path):
    # Stopped by LocalHome 5 s into a 10 s run: half open. (A wall button's
    # start is reported about 1.2 s late - that's counted too.)
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 0)

    store.mark_own_command(cover.id)
    store.observe_state(cover, "open", at=time.time())
    store.observe_state(cover, "stop", at=time.time() + 5.0)
    assert store.get(cover.id) == 50

    other = FakeCover("c2", travel_time=10.0)
    store.set(other.id, 50)
    store.observe_state(other, "close", at=2000.0)  # by the wall button: started at 1998.8
    store.observe_state(other, "stop", at=2001.3)
    assert store.get(other.id) == 25


def test_a_part_run_from_nowhere_known_says_nothing_until_a_full_one(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)

    store.observe_state(cover, "close", at=1000.0)
    store.observe_state(cover, "stop", at=1003.0)
    assert store.where(cover.id) is None

    store.observe_state(cover, "close", at=1100.0)
    store.observe_state(cover, "stop", at=1110.0)
    assert store.where(cover.id) == 0


def test_the_measured_travel_time_is_the_one_used(tmp_path):
    # The module's timer has a margin (19 s for a 16 s run): the time
    # measured with a stopwatch, set in the admin panel, wins.
    store = PositionStore(str(tmp_path / "positions.json"),
                          travel_times=lambda cover, direction: 20.0 if direction == "open" else None)
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 0)
    assert store.travel_time(cover, "open") == 20.0 and store.travel_time(cover, "close") == 10.0

    store.mark_own_command(cover.id)
    store.observe_state(cover, "open", at=time.time())
    store.observe_state(cover, "stop", at=time.time() + 10.0)
    assert store.get(cover.id) == 50


def test_where_it_is_moves_while_it_runs(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=10.0)
    store.set(cover.id, 0)

    store.mark_own_command(cover.id)
    store.observe_state(cover, "open", at=time.time() - 4.0)

    assert store.run_of(cover.id)["direction"] == "open"
    assert 35 <= store.where(cover.id) <= 45


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


def test_an_own_run_stopped_at_once_moves_nothing(tmp_path):
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1")
    store.set(cover.id, 0)

    store.mark_own_command(cover.id)
    store.observe_state(cover, "open")
    store.observe_state(cover, "stop")

    assert store.get(cover.id) == 0


def test_a_move_to_percent_is_left_to_its_own_timer(tmp_path):
    # Its target is known: the module's reports of the same run change nothing.
    store = PositionStore(str(tmp_path / "positions.json"))
    cover = FakeCover("c1", travel_time=1.0)
    store.set(cover.id, 0)

    store.move_to_percent(cover, 60)
    store.observe_state(cover, "open")
    assert store.run_of(cover.id) is None
    time.sleep(0.8)
    assert store.get(cover.id) == 60


def test_set_survives_many_covers_finishing_at_the_same_instant(tmp_path):
    # Each cover's move_to_percent() finishes on its own threading.Timer
    # (see finish() in move_to_percent()) - close enough moves can call
    # set() for *different* cover ids at close enough to the same
    # instant to race on positions.json's read-modify-write cycle.
    # Before set() took its own lock around that cycle, the slower
    # thread's save() could silently overwrite a faster thread's
    # just-written position for a *different* cover, since both read
    # the same on-disk snapshot before either had written back.
    store = PositionStore(str(tmp_path / "positions.json"))
    cover_ids = [f"cover-{i}" for i in range(20)]
    barrier = threading.Barrier(len(cover_ids))

    def set_one(cover_id):
        barrier.wait()  # line every thread up so the writes actually overlap
        store.set(cover_id, 42)

    threads = [threading.Thread(target=set_one, args=(cid,)) for cid in cover_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for cover_id in cover_ids:
        assert store.get(cover_id) == 42, f"{cover_id} lost its position to a concurrent write"
