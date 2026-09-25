"""The UNO Q's LED matrix: what it shows, when, and how it gets there.

Everything that decides what's on the matrix is a pure function, so it's
tested here without the board; the bridge is tested against a fake
arduino-router speaking the same msgpack-RPC.
"""
import socket
import threading

import msgpack
import pytest

from localhome.services import led_matrix as lm
from localhome.services.thermostat import Zone
from localhome.web.app import create_app


def _valid(grid):
    return len(grid) == lm.H and all(len(row) == lm.W for row in grid) and \
        all(0 <= v <= 7 for row in grid for v in row)


def _lit(grid):
    return {(y, x) for y, row in enumerate(grid) for x, v in enumerate(row) if v}


ZONES = [Zone(name="Stanze", sensor="Stanze Sensor", valve="Stanze Valve"),
         Zone(name="Bagni", sensor="Bagni Sensor", valve="Bagni Valve")]


# ------------------------------------------------------------- drawing ----

def test_every_screen_fits_the_matrix_and_goes_over_the_wire_as_104_digits():
    frames = [entry["frame"] for entry in lm.legend()]
    frames += [lm.render_problem(code) for code in ("S1", "V2", "MQ", "DK", "WF", "HT", "EM", "CK", "CF", "LP", "DV")]
    frames += [lm.render_scroll("192.168.1.25", offset) for offset in range(0, 70)]
    for grid in frames:
        assert _valid(grid)
        wire = lm.to_wire(grid)
        assert len(wire) == 104 and set(wire) <= set("01234567")


def test_every_problem_code_can_be_drawn():
    codes = {*lm.PROBLEM_CODES.values(), "S1", "S2", "V1", "V2", "DV", "MQ"}
    for code in codes:
        assert all(char in lm.FONT for char in code), code


def test_each_state_has_its_own_picture():
    pictures = {
        "heat": lm.render_normal(lm.MatrixState(thermostat=True, mode="heat", zones=[False])),
        "cool": lm.render_normal(lm.MatrixState(thermostat=True, mode="cool", zones=[False])),
        "away": lm.render_normal(lm.MatrixState(thermostat=True, away=True, zones=[False])),
        "diagnostic": lm.render_normal(lm.MatrixState(thermostat=True, diagnostic=True, zones=[False])),
        "idle": lm.render_normal(lm.MatrixState()),
    }
    lit = {name: frozenset(_lit(grid)) for name, grid in pictures.items()}
    assert len(set(lit.values())) == len(lit), "two states would look the same"


def test_a_zone_bar_is_bright_while_its_valve_is_open():
    grid = lm.render_normal(lm.MatrixState(thermostat=True, zones=[True, False]))
    zone_1 = [grid[y][8] for y in range(2, lm.H)]
    zone_2 = [grid[y][11] for y in range(2, lm.H)]
    assert set(zone_1) == {lm.BRIGHT}
    assert set(zone_2) == {lm.DIM}


def test_a_valve_nobody_has_heard_from_shows_only_the_base_of_its_bar():
    grid = lm.render_normal(lm.MatrixState(thermostat=True, zones=[None]))
    assert [grid[y][8] for y in range(2, lm.H)] == [0, 0, 0, 0, 0, lm.DIM]


def test_one_zone_means_one_bar():
    grid = lm.render_normal(lm.MatrixState(thermostat=True, zones=[True]))
    assert all(grid[y][11] == 0 and grid[y][12] == 0 for y in range(2, lm.H))


def test_the_heartbeat_is_the_top_right_pixel():
    state = lm.MatrixState(thermostat=True, zones=[False])
    assert lm.render_normal(state, beat=True)[0][12] == lm.BEAT
    assert lm.render_normal(state, beat=False)[0][12] == 0


# ------------------------------------------------------ what, and when ----

def test_the_ip_scrolls_by_first_then_the_normal_screen():
    state = lm.MatrixState(thermostat=True, zones=[True])
    ip = "192.168.1.25"

    assert lm.screen_at(state, 0.5, ip)["screen"] == "boot"
    assert lm.screen_at(state, lm.scroll_seconds(ip) - 0.05, ip)["screen"] == "boot"
    assert lm.screen_at(state, lm.scroll_seconds(ip) + 0.5, ip)["screen"] == "normal"


def test_without_an_ip_it_starts_on_the_normal_screen():
    assert lm.screen_at(lm.MatrixState(), 0.0, None)["screen"] == "normal"


def test_problems_take_turns_with_the_normal_screen():
    state = lm.MatrixState(thermostat=True, zones=[True], problems=[
        {"code": "S1", "subject": "Stanze Sensor"}, {"code": "DK", "subject": "/"}])
    seen = [lm.screen_at(state, t)["screen"] + (lm.screen_at(state, t).get("code") or "")
            for t in (0.1, lm.SLOT_SECONDS + 0.1, 2 * lm.SLOT_SECONDS + 0.1, 3 * lm.SLOT_SECONDS + 0.1)]
    assert seen == ["problemS1", "problemDK", "normal", "problemS1"]


def test_the_exclamation_mark_blinks():
    state = lm.MatrixState(problems=[{"code": "S1", "subject": "x"}])
    on = lm.screen_at(state, 0.1)["frame"]
    off = lm.screen_at(state, lm.FRAME_SECONDS + 0.1)["frame"]
    assert on[0][1] == lm.BRIGHT and off[0][1] == 0
    assert [row[4:11] for row in on[1:6]] == [row[4:11] for row in off[1:6]], "the code itself stays put"


# ------------------------------------------------------- problem codes ----

def test_a_zone_device_offline_gets_its_zone_number():
    problems = [
        {"code": "device_offline", "subject": "Bagni Valve", "detail": "x"},
        {"code": "device_offline", "subject": "Stanze Sensor", "detail": "x"},
        {"code": "device_offline", "subject": "VMC", "detail": "x"},
    ]
    assert [p["code"] for p in lm.problem_codes(problems, ZONES)] == ["V2", "S1", "DV"]


def test_the_broker_comes_first_when_it_is_down():
    problems = [{"code": "device_offline", "subject": "Stanze Sensor", "detail": "x"}]
    assert [p["code"] for p in lm.problem_codes(problems, ZONES, broker_down=True)] == ["MQ", "S1"]


def test_not_heard_yet_after_a_boot_is_not_a_problem_on_the_matrix():
    problems = [{"code": "device_not_heard_yet", "subject": "Stanze Sensor", "detail": None}]
    assert lm.problem_codes(problems, ZONES) == []


def test_each_code_once_and_system_problems_mapped():
    problems = [
        {"code": "disk_low", "subject": "/", "detail": None},
        {"code": "device_offline", "subject": "Lamp", "detail": None},
        {"code": "device_offline", "subject": "Plug", "detail": None},
        {"code": "loop_stalled", "subject": "thermostat", "detail": None},
        {"code": "something_new", "subject": "?", "detail": None},
    ]
    assert [p["code"] for p in lm.problem_codes(problems, ZONES)] == ["DK", "DV", "LP"]


def test_what_needs_a_subprocess_stays_off_the_matrix():
    """Wi-Fi strength, clock sync, temperature and eMMC wear need nmcli,
    timedatectl or sysfs digging: the admin panel's system status shows
    them; the matrix, running all day, doesn't go looking."""
    problems = [{"code": code, "subject": "x", "detail": None}
                for code in ("wifi_weak", "clock_unsynced", "cpu_hot", "storage_wear")]
    assert lm.problem_codes(problems, ZONES) == []


# ------------------------------------------------------------ the bridge --

class FakeRouter:
    """Answers msgpack-RPC requests on the other end of a socketpair, for
    as long as the connection stays open."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.connections = 0
        self.sockets = []

    def connect(self):
        ours, theirs = socket.socketpair()
        self.connections += 1
        self.sockets.append(theirs)
        threading.Thread(target=self._serve, args=(theirs,), daemon=True).start()
        return ours

    def _serve(self, sock):
        unpacker = msgpack.Unpacker(raw=False)
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            unpacker.feed(chunk)
            for request in unpacker:
                self.requests.append(request)
                error, result = self.answer(request)
                sock.sendall(msgpack.packb([1, request[1], error, result]))


def test_the_client_speaks_msgpack_rpc():
    router = FakeRouter(lambda req: (None, True))
    client = lm.RouterClient(connect=router.connect)

    assert client.call("lh_frame", "0" * 104) is True
    kind, _, method, params = router.requests[0]
    assert (kind, method, params) == (0, "lh_frame", ["0" * 104])


def test_one_connection_serves_every_frame():
    """86,400 frames a day must not be 86,400 connections for the router."""
    router = FakeRouter(lambda req: (None, True))
    client = lm.RouterClient(connect=router.connect)

    for _ in range(50):
        assert client.call("lh_frame", "0" * 104) is True

    assert router.connections == 1 and len(router.requests) == 50


def test_a_dropped_connection_is_opened_again_on_the_next_call():
    router = FakeRouter(lambda req: (None, True))
    client = lm.RouterClient(connect=router.connect)
    client.call("lh_frame", "0" * 104)

    router.sockets[0].close()          # the router restarts
    with pytest.raises(OSError):
        client.call("lh_frame", "0" * 104)
    assert client.call("lh_frame", "0" * 104) is True

    assert router.connections == 2


def test_a_method_nobody_provides_is_an_error_not_a_result():
    router = FakeRouter(lambda req: ([2, "method lh_frame not available"], None))
    client = lm.RouterClient(connect=router.connect)

    with pytest.raises(lm.RouterError, match="not available"):
        client.call("lh_frame", "0" * 104)


# ------------------------------------------------------------ the service --

class FakeClient:
    def __init__(self, present=True, error=None):
        self.present = present
        self.error = error
        self.frames = []

    def available(self):
        return self.present

    def call(self, method, frame):
        if self.error:
            raise self.error
        self.frames.append(frame)
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_service_draws_and_says_so():
    client, clock = FakeClient(), Clock()
    service = lm.LedMatrixService(lambda: lm.MatrixState(thermostat=True, zones=[True]), client=client, clock=clock)

    service.step()

    assert service.status()["status"] == "running"
    assert len(client.frames) == 1 and len(client.frames[0]) == 104
    assert service.status()["screen"]["screen"] == "normal"


def test_no_router_means_off_quietly():
    client = FakeClient(present=False)
    service = lm.LedMatrixService(lambda: lm.MatrixState(), client=client, clock=Clock())

    wait = service.step()

    assert service.status()["status"] == "no_router"
    assert client.frames == [] and wait == lm.LedMatrixService.RETRY_SECONDS


def test_a_missing_sketch_is_reported_and_retried_later_not_every_frame():
    clock = Clock()
    client = FakeClient(error=lm.RouterError("method lh_frame not available"))
    service = lm.LedMatrixService(lambda: lm.MatrixState(), client=client, clock=clock)

    wait = service.step()
    assert service.status()["status"] == "no_sketch"
    assert wait == lm.LedMatrixService.RETRY_SECONDS, "not asked again on every frame"

    client.error = None
    clock.now += wait
    service.step()
    assert service.status()["status"] == "running", "the sketch loaded later is picked up with no restart"


def test_one_hiccup_is_retried_at_once_a_repeated_failure_backs_off():
    """A single lost connection must not leave the matrix dark for long
    enough (20 s) that the sketch says LocalHome has stopped."""
    client, clock = FakeClient(), Clock()
    service = lm.LedMatrixService(lambda: lm.MatrixState(), client=client, clock=clock)
    service.step()                                   # running

    client.error = ConnectionError("arduino-router closed the connection")
    assert service.step() == lm.LedMatrixService.QUICK_RETRY_SECONDS
    assert service.step() == lm.LedMatrixService.RETRY_SECONDS

    client.error = None
    service.step()
    assert service.status()["status"] == "running"


def test_where_there_is_no_matrix_it_does_no_work_at_all():
    calls = []

    def provider():
        calls.append(1)
        return lm.MatrixState()

    service = lm.LedMatrixService(provider, client=FakeClient(present=False), clock=Clock())
    for _ in range(3):
        service.step()
    assert calls == [], "the state isn't even read"


def test_the_state_is_reread_now_and_then_not_every_frame():
    calls = []

    def provider():
        calls.append(1)
        return lm.MatrixState()

    clock = Clock()
    service = lm.LedMatrixService(provider, client=FakeClient(), clock=clock)
    for _ in range(5):
        service.step()
        clock.now += lm.FRAME_SECONDS
    assert len(calls) == 1
    clock.now += lm.STATE_REFRESH_SECONDS
    service.step()
    assert len(calls) == 2



def test_where_there_is_no_matrix_the_mirror_still_follows_the_clock():
    """The loop sleeps 30 s where there's nothing to draw on; the admin
    panel's mirror is computed when it asks instead, so it stays live."""
    clock = Clock()
    service = lm.LedMatrixService(lambda: lm.MatrixState(thermostat=True, zones=[True]),
                                  client=FakeClient(present=False), clock=clock)
    service.step()
    first = service.status()["screen"]["frame"]
    clock.now += lm.FRAME_SECONDS
    second = service.status()["screen"]["frame"]
    assert first != second, "the heartbeat moved"

# ---------------------------------------------------- the broker, for MQ --

def test_the_broker_is_known_down_from_the_client_itself(monkeypatch):
    """No probe of the broker: the shared MQTT client already knows
    whether it's connected, and since when it isn't."""
    from localhome.drivers.mqtt import client as mqtt_client

    class Conn:
        def __init__(self, down_since):
            self.down_since = down_since

    monkeypatch.setattr(mqtt_client, "_pool", {("a", 1883, None): Conn(None)})
    assert mqtt_client.broker_down_for(now=1000.0) == 0.0

    monkeypatch.setattr(mqtt_client, "_pool", {("a", 1883, None): Conn(None), ("b", 1883, None): Conn(900.0)})
    assert mqtt_client.broker_down_for(now=1000.0) == 100.0

    monkeypatch.setattr(mqtt_client, "_pool", {})
    assert mqtt_client.broker_down_for(now=1000.0) == 0.0, "no MQTT at all is not a broker down"


# ------------------------------------------------------------------ API --

def test_the_admin_panel_gets_status_mirror_and_legend(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(path))
    try:
        data = app.test_client().get("/api/climate-admin/led-matrix?legend=1").get_json()
        polled = app.test_client().get("/api/climate-admin/led-matrix").get_json()
    finally:
        app.config["HISTORY"].close()

    assert data["ok"] is True
    assert data["status"] in ("starting", "no_router", "running", "no_sketch", "error")
    keys = [entry["key"] for entry in data["legend"]]
    assert keys == ["heat", "cool", "away", "diagnostic", "idle", "problem", "boot", "silence", "waiting"]
    assert all(_valid(entry["frame"]) for entry in data["legend"])
    assert {"S1", "V1", "MQ", "DK"} <= set(data["codes"])
    assert "legend" not in polled and polled["screen"] is not None, "the frequent poll stays small"
