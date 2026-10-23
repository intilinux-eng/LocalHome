"""All the shutters at once: each asked on its own, at the same time.

One by one, a shutter off the network held the others back for its whole
socket timeout, and its error left the ones after it without the command.
Now the others move all the same, the answer says which ones didn't answer
and which are still being tried, and it doesn't wait for a slow one.
"""
import threading
import time

import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""


class FakeCover:
    def __init__(self, name, fails=False, delay=0.0):
        self.id = f"id-{name}"
        self.name = name
        self.fails = fails
        self.delay = delay
        self.sent = []
        self.done = threading.Event()

    def status(self):
        return {"state": self.sent[-1] if self.sent else "stop"}

    def send(self, action):
        time.sleep(self.delay)
        if self.fails:
            self.done.set()
            raise RuntimeError(f"{self.name}: not answering")
        self.sent.append(action)
        self.done.set()

    def read_travel_time(self, default=15.0):
        return default


@pytest.fixture
def board(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    app.config["COVER_ALL_WAIT_SECONDS"] = 0.5

    def with_covers(*covers):
        app.config["MANAGER"].covers.update({c.name: c for c in covers})
        return app.test_client()

    yield app, with_covers
    app.config["HISTORY"].close()


def test_one_not_answering_leaves_the_others_moving(board):
    app, with_covers = board
    dead, kitchen, lounge = FakeCover("Bagno", fails=True), FakeCover("Cucina"), FakeCover("Soggiorno")
    client = with_covers(dead, kitchen, lounge)

    res = client.post("/api/covers/all/action/open", json={})

    assert res.status_code == 200
    body = res.get_json()
    assert body["sent"] == ["Cucina", "Soggiorno"]
    assert [f["name"] for f in body["failed"]] == ["Bagno"]
    assert body["pending"] == []
    assert kitchen.sent == ["open"] and lounge.sent == ["open"]
    positions = app.config["POSITIONS"]
    assert positions.get(kitchen.id) == 100 and positions.get(lounge.id) == 100


def test_a_slow_one_is_said_waiting_and_the_answer_does_not_wait_for_it(board):
    _, with_covers = board
    slow, quick = FakeCover("Camera", delay=2.0), FakeCover("Cucina")
    client = with_covers(slow, quick)

    started = time.monotonic()
    body = client.post("/api/covers/all/action/close", json={}).get_json()

    assert time.monotonic() - started < 1.5
    assert body["sent"] == ["Cucina"] and body["pending"] == ["Camera"]
    assert slow.done.wait(5) and slow.sent == ["close"]  # it got the command all the same


def test_none_answering_is_an_error_naming_them(board):
    _, with_covers = board
    client = with_covers(FakeCover("Bagno", fails=True), FakeCover("Cucina", fails=True))

    res = client.post("/api/covers/all/action/stop", json={})

    assert res.status_code == 502
    body = res.get_json()
    assert body["ok"] is False and [f["name"] for f in body["failed"]] == ["Bagno", "Cucina"]


def test_one_shutter_by_itself_as_before(board):
    _, with_covers = board
    dead, kitchen = FakeCover("Bagno", fails=True), FakeCover("Cucina")
    client = with_covers(dead, kitchen)

    assert client.post("/api/covers/Cucina/action/close", json={}).status_code == 200
    assert kitchen.sent == ["close"]
    assert client.post("/api/covers/Bagno/action/close", json={}).status_code == 502
    assert client.post("/api/covers/Garage/action/close", json={}).status_code == 404
    assert client.post("/api/covers/Cucina/action/jump", json={}).status_code == 400
