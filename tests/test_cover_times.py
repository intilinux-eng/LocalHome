"""The shutters as the pages see them: where each one is (/api/covers, no
device asked), and each one's travel time measured up and down, set in the
admin panel and used from the next run."""
import time

import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations:
  - kind: cover
    type: simulated
    name: "Tapparella Cucina"
    travel_time_seconds: 0.5
  - kind: cover
    type: simulated
    name: "Tapparella Bagno"
    unreachable: true
"""


def wait_for(condition, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def board(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(config_path))
    yield app, app.test_client()
    app.config["COVER_WATCH"].stop()
    app.config["HISTORY"].close()


def covers(client):
    return {c["name"]: c for c in client.get("/api/covers").get_json()["covers"]}


def test_where_each_one_is_and_whether_it_is_reached(board):
    _, client = board
    assert wait_for(lambda: all(c["reached"] is not None for c in covers(client).values()))
    now = covers(client)
    assert now["Tapparella Cucina"]["reached"] is True and now["Tapparella Bagno"]["reached"] is False
    assert now["Tapparella Cucina"]["percent"] is None  # no full run seen yet

    client.post("/api/covers/Tapparella Cucina/action/open", json={})
    assert covers(client)["Tapparella Cucina"]["moving"] == "open"
    assert wait_for(lambda: covers(client)["Tapparella Cucina"]["moving"] is None)
    assert covers(client)["Tapparella Cucina"]["percent"] == 100


def test_travel_times_set_checked_and_cleared(board):
    app, client = board
    listed = {c["name"]: c for c in client.get("/api/climate-admin/covers").get_json()["covers"]}
    assert listed["Tapparella Cucina"]["device_s"] == 0.5 and listed["Tapparella Cucina"]["times"] == {}

    res = client.post("/api/climate-admin/covers/Tapparella Cucina/times", json={"open": 16, "close": "15.5"})
    assert res.get_json() == {"ok": True, "times": {"open": 16.0, "close": 15.5}}
    cucina = app.config["MANAGER"].covers["Tapparella Cucina"]
    assert app.config["POSITIONS"].travel_time(cucina, "close") == 15.5

    bad = client.post("/api/climate-admin/covers/Tapparella Cucina/times", json={"open": 1})
    assert bad.status_code == 400 and bad.get_json()["code"] == "cover_time_invalid"
    unknown = client.post("/api/climate-admin/covers/Garage/times", json={"open": 10})
    assert unknown.status_code == 404 and unknown.get_json()["code"] == "cover_unknown"

    client.post("/api/climate-admin/covers/Tapparella Cucina/times", json={"open": None, "close": None})
    assert app.config["COVER_TIMES"].all() == {}
    assert app.config["POSITIONS"].travel_time(cucina, "close") == 0.5
