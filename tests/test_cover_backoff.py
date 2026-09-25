"""A cover that isn't answering is asked again less and less often.

Every open dashboard asks each cover's status every 8 seconds, and a
device that has stopped answering costs a full socket timeout per ask -
with five dead blinds the board spent most of its time waiting on them.
The failure is still reported immediately; it just isn't re-tried on
every single poll.
"""
import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations: []
"""


class FakeCover:
    id = "blind-1"
    name = "Kitchen Blind"

    def __init__(self):
        self.status_calls = 0
        self.send_calls = []
        self.fails = True

    def status(self):
        self.status_calls += 1
        if self.fails:
            raise RuntimeError("Kitchen Blind: Check device key or version")
        return {"state": "stop"}

    def send(self, action):
        self.send_calls.append(action)
        if self.fails:
            raise RuntimeError("Kitchen Blind: Check device key or version")

    def read_travel_time(self, default=15.0):
        return default


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    cover = FakeCover()
    app.config["MANAGER"].covers["Kitchen Blind"] = cover
    yield app.test_client(), cover, app
    app.config["HISTORY"].close()


def test_a_failing_cover_is_not_re_asked_on_every_poll(client):
    c, cover, _ = client

    for _ in range(5):
        res = c.get("/api/covers/Kitchen Blind/status")

    assert cover.status_calls == 1, "asked once, then the known failure is served"
    assert res.status_code == 502
    assert "Check device key" in res.get_json()["error"]
    assert res.get_json()["waiting"] is True


def test_the_wait_grows_and_then_it_is_tried_again(client):
    c, cover, app = client
    c.get("/api/covers/Kitchen Blind/status")

    # Still inside the first wait.
    app.config["COVER_READS"]["Kitchen Blind"]["at"] -= 20
    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 1

    # Past it: tried again, and the wait doubles for the next one.
    app.config["COVER_READS"]["Kitchen Blind"]["at"] -= 20
    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 2
    assert app.config["COVER_READS"]["Kitchen Blind"]["failures"] == 2
    app.config["COVER_READS"]["Kitchen Blind"]["at"] -= 40
    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 2, "60s wait after the second failure"


def test_a_cover_that_answers_again_goes_back_to_normal_polling(client):
    c, cover, app = client
    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 1

    cover.fails = False
    app.config["COVER_READS"]["Kitchen Blind"]["at"] -= 60  # the retry comes due
    assert c.get("/api/covers/Kitchen Blind/status").status_code == 200

    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 3, "a working cover is asked every time again"


def test_a_command_is_always_sent_even_while_waiting(client):
    c, cover, _ = client
    c.get("/api/covers/Kitchen Blind/status")

    res = c.post("/api/covers/Kitchen Blind/action/open")

    assert cover.send_calls == ["open"], "the person pressed the button: always try"
    assert res.status_code == 502


def test_a_command_that_works_clears_the_wait(client):
    c, cover, app = client
    c.get("/api/covers/Kitchen Blind/status")
    cover.fails = False

    c.post("/api/covers/Kitchen Blind/action/open")

    assert "Kitchen Blind" not in app.config["COVER_READS"]
    c.get("/api/covers/Kitchen Blind/status")
    assert cover.status_calls == 2
