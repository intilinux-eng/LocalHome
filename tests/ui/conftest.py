"""Browser tests: a real LocalHome server and a real Chromium.

Marked `ui` and left out of a plain `pytest` run (pyproject.toml's
addopts) - they need Playwright and its browser, and take a minute or
two. To run them:

    pip install -e ".[dev,ui]"
    python -m playwright install chromium
    pytest -m ui

One server per session, from a throwaway config written here: two zones
from the admin panel's zone map, one of them with a name built to break
careless HTML ("Bagno <img ...>"), the other with an apostrophe, the
outdoor sensor and VMC simulated, the two extra switches - all simulated,
nothing on the network. Italian, so an untranslated text shows.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from .helpers import ZONE_A, ZONE_B, Opened

ROOT = Path(__file__).resolve().parents[2]

CONFIG = """\
web:
  host: "127.0.0.1"
  port: {port}
  language: "it"

integrations:
  # A light with its own level, listed before the VMC's speed - as on the
  # real board, where the Tapo strips come first (see test_home.py).
  - kind: switch
    type: simulated
    name: "Lampada"
  - kind: number
    type: simulated
    name: "Lampada Livello"
    paired_switch: "Lampada"
    initial_value: 80
  - kind: switch
    type: simulated
    name: "Dehumidifier"
    dashboard_tab: climate
    visible_in_mode: "cool"
  - kind: switch
    type: simulated
    name: "Bathroom Bypass Valve"
    dashboard_tab: climate
    visible_in_mode: "cool"

thermostat:
  mode: "cool"
  hysteresis_c: 0.5
  schedules_file: "schedules.json"
  poll_interval_seconds: 60
  mode_switches:
    - switch: "Bathroom Bypass Valve"
      active_in_mode: "cool"
  mode_limited_switches:
    - switch: "Dehumidifier"
      only_in_mode: "cool"

history:
  db_file: "data/history.db"
  poll_interval_seconds: 30
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """The base URL of a LocalHome started for this session."""
    home = tmp_path_factory.mktemp("localhome")
    port = _free_port()
    (home / "config.yaml").write_text(CONFIG.format(port=port), encoding="utf-8")
    zone = {"sensor_mode": "simulated", "sensor_device_id": None,
            "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True}
    (home / "climate_map.json").write_text(
        json.dumps({"zones": [dict(zone, name=ZONE_A), dict(zone, name=ZONE_B)]}), encoding="utf-8")
    log_path = home / "server.log"
    env = dict(os.environ, LOCALHOME_CONFIG=str(home / "config.yaml"), PYTHONUNBUFFERED="1")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while True:
        try:
            urllib.request.urlopen(base + "/api/server-time", timeout=2)
            break
        except OSError:
            if proc.poll() is not None or time.time() > deadline:
                proc.kill()
                pytest.fail("LocalHome didn't start:\n" + log_path.read_text(encoding="utf-8")[-3000:])
            time.sleep(0.3)
    yield base
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def api(server):
    """api(path) GETs, api(path, body) POSTs - JSON both ways."""
    def call(path, body=None):
        req = urllib.request.Request(server + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as res:
            return json.load(res)
    return call


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        chromium = p.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def open_page(browser, server):
    """open_page(path, width) -> Opened, in a fresh context (its own
    storage, like another phone)."""
    contexts = []

    def _open(path="/", width=1280, height=900):
        context = browser.new_context(viewport={"width": width, "height": height})
        contexts.append(context)
        opened = Opened(context.new_page())
        opened.page.goto(server + path)
        return opened

    yield _open
    for context in contexts:
        context.close()
