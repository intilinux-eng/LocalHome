"""Every cover followed all the time, whoever moves it.

A shutter moved by its wall button - nobody told LocalHome - is heard
starting and stopping, and ends where the run took it. One that can't say
its changes by itself is asked every few seconds. One off the network is
said not reached, for the system status.
"""
import time
from types import SimpleNamespace

from localhome.drivers.simulated.cover import SimulatedCoverDriver
from localhome.services import cover_watch
from localhome.services.cover_watch import CoverWatcher
from localhome.services.position_control import PositionStore


def wait_for(condition, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def follow(tmp_path, *covers):
    positions = PositionStore(str(tmp_path / "positions.json"))
    reads = {}
    watcher = CoverWatcher(SimpleNamespace(covers={c.name: c for c in covers}), positions, reads)
    watcher.start()
    return watcher, positions, reads


def test_a_shutter_moved_by_its_button_is_followed_to_the_end(tmp_path):
    cover = SimulatedCoverDriver("Cucina", travel_time_seconds=0.4)
    watcher, positions, reads = follow(tmp_path, cover)
    try:
        assert wait_for(lambda: (reads.get("Cucina") or {}).get("ok"))
        assert positions.where(cover.id) is None  # never seen a full run yet

        cover.send("close")  # the wall button: the module says it by itself
        assert wait_for(lambda: positions.run_of(cover.id) is not None)
        assert positions.run_of(cover.id)["direction"] == "close"
        assert wait_for(lambda: positions.run_of(cover.id) is None)
        assert positions.where(cover.id) == 0
    finally:
        watcher.stop()


def test_one_off_the_network_is_said_not_reached(tmp_path):
    cover = SimulatedCoverDriver("Bagno", unreachable=True)
    watcher, _, reads = follow(tmp_path, cover)
    try:
        assert wait_for(lambda: "Bagno" in reads)
        assert reads["Bagno"]["ok"] is False and "not answering" in reads["Bagno"]["error"]
    finally:
        watcher.stop()


class PolledCover:
    """A cover that can't say its changes: only asked."""
    id = name = "Garage"

    def __init__(self):
        self.state = "stop"

    def status(self):
        return {"state": self.state}

    def send(self, action):
        self.state = action

    def read_travel_time(self, default=15.0):
        return 30.0

    def listen(self, on_state, on_link, stop):
        return False


def test_one_that_cant_say_its_changes_is_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(cover_watch, "POLL_SECONDS", 0.1)
    cover = PolledCover()
    watcher, positions, reads = follow(tmp_path, cover)
    try:
        assert wait_for(lambda: (reads.get("Garage") or {}).get("ok"))
        cover.state = "open"
        assert wait_for(lambda: (positions.run_of(cover.id) or {}).get("direction") == "open")
    finally:
        watcher.stop()
