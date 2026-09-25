"""LocalHome's status on the Arduino UNO Q's 13x8 LED matrix.

The dashboard already shows everything to someone with a phone. The
matrix is for whoever stands in front of the box without one - and above
all for when the network or LocalHome itself is down, which is exactly
when the dashboard can't help. So it shows health, not data:

- **Normal**: the season (flame / snowflake), or away (house) or
  diagnostic mode (wrench), plus one bar per zone - bright while that
  zone's valve is open, dim while it's closed.
- **A problem**: a blinking "!" and a two-character code (S1 = zone 1's
  sensor not answering, MQ = MQTT broker down, ...), taking turns with
  the normal screen when there's more than one thing to say.
- **At start-up**: the board's IP address scrolls past once, for when
  its .local name doesn't resolve.
- **Heartbeat**: the top-right pixel blinks with every frame - LocalHome
  is alive and drawing.

What LocalHome can't say about itself, the microcontroller says on its
own (deploy/led-matrix/localhome_matrix): a dot running along the bottom
until the first frame arrives, and a blinking X once frames have stopped
for 20 seconds - LocalHome stopped, crashed or hung.

Everything here that decides what's on the matrix is a pure function of
a MatrixState and a time, so it's tested without the board, and the admin
panel's legend is drawn by the same code that drives the LEDs.

The frames travel through arduino-router, the UNO Q's Linux-to-MCU
bridge: msgpack-RPC over a unix socket, calling "lh_frame" - provided by
the sketch - with 104 characters, '0'-'7', one per pixel from the top
left. On any other machine the socket isn't there and this stays off.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field

from localhome.services.log_throttle import FailureLog

logger = logging.getLogger(__name__)

W, H = 13, 8
ROUTER_SOCKET = "/var/run/arduino-router.sock"
FRAME_METHOD = "lh_frame"

# Brightness, out of 7. The matrix is bright and sits in a technical
# cabinet or a hallway: icons at half, only what changes state at full.
ICON = 4
BRIGHT = 7
DIM = 1
BEAT = 3

# It's a nicety, so it's kept cheap: one frame a second (the heartbeat
# is one second on, one off), the state re-read from memory every 30 s.
FRAME_SECONDS = 1.0          # normal screens: the heartbeat's own rhythm
SCROLL_STEP_SECONDS = 0.1    # one column per step - only while the IP scrolls by, once at start-up
SLOT_SECONDS = 3.0           # each problem code, and the normal screen between them
STATE_REFRESH_SECONDS = 30.0
# A broker connection that drops comes back by itself within seconds;
# MQ is for one that stays down.
BROKER_GRACE_SECONDS = 60.0


def _bitmap(rows: list[str]) -> list[list[int]]:
    return [[1 if c == "#" else 0 for c in row] for row in rows]


# 7x8 icons, left-aligned on the matrix.
ICONS = {
    "heat": _bitmap([
        "....#..",
        "...##..",
        "..###..",
        ".####..",
        ".#####.",
        "###.###",
        "##...##",
        ".#####.",
    ]),
    "cool": _bitmap([
        "#..#..#",
        ".#.#.#.",
        "..###..",
        "#######",
        "..###..",
        ".#.#.#.",
        "#..#..#",
        ".......",
    ]),
    "away": _bitmap([
        "...#...",
        "..#.#..",
        ".#...#.",
        "#######",
        ".#...#.",
        ".#.#.#.",
        ".#.#.#.",
        ".#####.",
    ]),
    "diagnostic": _bitmap([
        ".#...#.",
        ".#...#.",
        ".#####.",
        "..###..",
        "...#...",
        "...#...",
        "...#...",
        "...#...",
    ]),
    "idle": _bitmap([
        ".......",
        ".......",
        ".......",
        ".#####.",
        ".......",
        ".......",
        ".......",
        ".......",
    ]),
}

# 3x5 characters for problem codes and the IP address.
FONT = {ch: _bitmap(rows) for ch, rows in {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    "C": ["###", "#..", "#..", "#..", "###"],
    "D": ["##.", "#.#", "#.#", "#.#", "##."],
    "E": ["###", "#..", "##.", "#..", "###"],
    "F": ["###", "#..", "##.", "#..", "#.."],
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "K": ["#.#", "#.#", "##.", "#.#", "#.#"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "M": ["#.#", "###", "###", "#.#", "#.#"],
    "P": ["###", "#.#", "###", "#..", "#.."],
    "Q": [".#.", "#.#", "#.#", "#.#", ".##"],
    "S": ["###", "#..", "###", "..#", "###"],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "V": ["#.#", "#.#", "#.#", "#.#", ".#."],
    "W": ["#.#", "#.#", "###", "###", "#.#"],
    ".": ["...", "...", "...", "...", ".#."],
    "-": ["...", "...", "###", "...", "..."],
    " ": ["...", "...", "...", "...", "..."],
}.items()}

# system_status.problems() codes -> what the matrix shows. The zone
# devices get their own codes with the zone's number (S1, V2); anything
# else offline is just "a device" (DV) - the admin panel says which.
# Only what can be known from memory: Wi-Fi strength, clock sync,
# temperature and eMMC wear need subprocesses and stay in the admin
# panel's system status.
PROBLEM_CODES = {
    "integration_failed": "CF",
    "loop_stalled": "LP",
    "disk_low": "DK",
}
# "Not heard from yet" right after a boot is normal for a battery sensor -
# the system status says so itself; a blinking "!" for it would teach
# people to ignore the matrix.
IGNORED_PROBLEMS = {"device_not_heard_yet"}


@dataclass
class MatrixState:
    thermostat: bool = False          # is there a thermostat at all
    mode: str = "heat"                # "heat" / "cool"
    away: bool = False
    diagnostic: bool = False
    zones: list = field(default_factory=list)       # per zone, valve open: True / False / None (unknown)
    zone_names: list = field(default_factory=list)  # same order - for the admin panel's words, not the LEDs
    problems: list = field(default_factory=list)    # [{"code": "S1", "subject": "..."}], most serious first


def problem_codes(problems: list[dict], zones: list, broker_down: bool = False) -> list[dict]:
    """system_status.problems() (most serious first) as matrix codes, each
    code once. `zones` are the thermostat's Zone objects, in panel order:
    a zone's sensor or valve being offline is S<n> / V<n>."""
    by_device = {}
    for number, zone in enumerate(zones, start=1):
        by_device[zone.sensor] = f"S{number}"
        by_device[zone.valve] = f"V{number}"
    found, seen = [], set()

    def add(code, subject):
        if code not in seen:
            seen.add(code)
            found.append({"code": code, "subject": subject})

    if broker_down:
        # Every MQTT device goes offline with it; the one code that says
        # why comes first, before the list of devices it took down.
        add("MQ", "MQTT")
    for problem in problems:
        if problem["code"] in IGNORED_PROBLEMS:
            continue
        if problem["code"] == "device_offline":
            add(by_device.get(problem["subject"], "DV"), problem["subject"])
        elif problem["code"] in PROBLEM_CODES:
            add(PROBLEM_CODES[problem["code"]], problem["subject"])
    return found


# ---------------------------------------------------------------- drawing --

def blank() -> list[list[int]]:
    return [[0] * W for _ in range(H)]


def _paint(grid, bitmap, x0, y0, level):
    for y, row in enumerate(bitmap):
        for x, on in enumerate(row):
            if on and 0 <= y0 + y < H and 0 <= x0 + x < W:
                grid[y0 + y][x0 + x] = level


def _beat(grid, on: bool) -> None:
    if on:
        grid[0][W - 1] = BEAT


def render_normal(state: MatrixState, beat: bool = False) -> list[list[int]]:
    grid = blank()
    if not state.thermostat or not state.zones:
        icon = "idle"
    elif state.diagnostic:
        icon = "diagnostic"
    elif state.away:
        icon = "away"
    else:
        icon = "cool" if state.mode == "cool" else "heat"
    _paint(grid, ICONS[icon], 0, 0, ICON)
    # One bar per zone on the right, two columns wide, under the heartbeat.
    for index, valve_open in enumerate(state.zones[:2]):
        x0 = 8 + index * 3
        for y in range(2, H):
            for x in (x0, x0 + 1):
                if valve_open is True:
                    grid[y][x] = BRIGHT
                elif valve_open is False:
                    grid[y][x] = DIM
                elif y == H - 1:
                    grid[y][x] = DIM      # unknown: just the base of the bar
    _beat(grid, beat)
    return grid


def render_problem(code: str, blink_on: bool = True, beat: bool = False) -> list[list[int]]:
    grid = blank()
    if blink_on:
        for y in range(0, 5):
            grid[y][1] = BRIGHT
        grid[6][1] = BRIGHT
    for index, char in enumerate(code[:2]):
        _paint(grid, FONT.get(char, FONT[" "]), 4 + index * 4, 1, BRIGHT)
    _beat(grid, beat)
    return grid


def text_width(text: str) -> int:
    return len(text) * 4


def render_scroll(text: str, offset: int, beat: bool = False) -> list[list[int]]:
    """`text` in the 3x5 font entering from the right: offset 0 is an
    empty screen, offset W + text_width(text) has it gone again."""
    grid = blank()
    x = W - offset
    for char in text:
        _paint(grid, FONT.get(char, FONT[" "]), x, 1, BRIGHT)
        x += 4
    _beat(grid, beat)
    return grid


def render_silence(level: int = BRIGHT) -> list[list[int]]:
    """What the sketch draws by itself once frames stop - the same X,
    drawn here only for the admin panel's legend."""
    grid = blank()
    for y in range(H):
        grid[y][3 + y] = level
        grid[y][10 - y] = level
    return grid


def render_waiting(position: int = 4) -> list[list[int]]:
    """What the sketch draws before the first frame: a dot running along
    the bottom row. For the legend."""
    grid = blank()
    grid[H - 1][position % W] = BRIGHT
    return grid


def scroll_seconds(text: str) -> float:
    return (W + text_width(text)) * SCROLL_STEP_SECONDS


def screen_at(state: MatrixState, seconds: float, ip: str | None = None) -> dict:
    """What the matrix shows `seconds` after LocalHome started: the IP
    scrolling by once, then the normal screen - taking turns with each
    problem code when there are any. Returns {"screen", "frame", ...}."""
    beat = int(seconds / FRAME_SECONDS) % 2 == 0
    if ip:
        scroll = scroll_seconds(ip)
        if seconds < scroll:
            offset = int(seconds / SCROLL_STEP_SECONDS)
            return {"screen": "boot", "ip": ip, "frame": render_scroll(ip, offset, beat)}
        seconds -= scroll
    slots = [p for p in state.problems] + ([None] if state.problems else [])
    if slots:
        slot = slots[int(seconds // SLOT_SECONDS) % len(slots)]
        if slot is not None:
            blink = int(seconds / FRAME_SECONDS) % 2 == 0
            return {"screen": "problem", "code": slot["code"], "subject": slot["subject"],
                    "frame": render_problem(slot["code"], blink, beat)}
    return {"screen": "normal", "frame": render_normal(state, beat)}


def to_wire(grid: list[list[int]]) -> str:
    return "".join(str(max(0, min(7, int(v)))) for row in grid for v in row)


def legend() -> list[dict]:
    """Every screen the matrix can show, drawn by the same functions - the
    admin panel's legend can't drift from what the LEDs really do."""
    heat = MatrixState(thermostat=True, mode="heat", zones=[True, False])
    return [
        {"key": "heat", "frame": render_normal(heat, beat=True)},
        {"key": "cool", "frame": render_normal(MatrixState(thermostat=True, mode="cool", zones=[False, True]), True)},
        {"key": "away", "frame": render_normal(MatrixState(thermostat=True, away=True, zones=[False, False]), True)},
        {"key": "diagnostic", "frame": render_normal(MatrixState(thermostat=True, diagnostic=True, zones=[True, False]), True)},
        {"key": "idle", "frame": render_normal(MatrixState(), True)},
        {"key": "problem", "frame": render_problem("S1", True, True)},
        {"key": "boot", "frame": render_scroll("192.168", 9, True)},
        {"key": "silence", "frame": render_silence()},
        {"key": "waiting", "frame": render_waiting()},
    ]


# ------------------------------------------------------------ the bridge --

class RouterError(Exception):
    """The router answered with an error - e.g. the sketch providing
    lh_frame isn't loaded ("method lh_frame not available")."""


class RouterClient:
    """msgpack-RPC to arduino-router: [0, id, method, params] out,
    [1, id, error, result] back. One short connection per call - at two
    frames a second that's nothing, and nothing stale to recover from."""

    def __init__(self, path: str = ROUTER_SOCKET, timeout: float = 2.0, connect=None):
        self.path = path
        self.timeout = timeout
        self._connect = connect or self._unix_connect
        self._id = 0

    def _unix_connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.path)
        return sock

    def available(self) -> bool:
        return os.path.exists(self.path)

    def call(self, method: str, *params):
        import msgpack

        self._id = (self._id + 1) % 1_000_000
        msg_id = self._id
        sock = self._connect()
        try:
            sock.sendall(msgpack.packb([0, msg_id, method, list(params)]))
            unpacker = msgpack.Unpacker(raw=False)
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("arduino-router closed the connection")
                unpacker.feed(chunk)
                for message in unpacker:
                    if isinstance(message, list) and len(message) == 4 and message[0] == 1 and message[1] == msg_id:
                        if message[2] is not None:
                            error = message[2]
                            raise RouterError(error[1] if isinstance(error, list) and len(error) > 1 else str(error))
                        return message[3]
        finally:
            sock.close()


# ------------------------------------------------------------ the service --

class LedMatrixService:
    """Sends a frame every half second (every tenth while the IP scrolls)
    and keeps what it last sent for the admin panel's live mirror.

    Off by itself where there's no arduino-router socket (a PC, a
    Raspberry Pi). Where there is one but the sketch isn't loaded, it says
    so in status() and keeps checking - flashing the sketch later needs no
    LocalHome restart."""

    RETRY_SECONDS = 30.0

    def __init__(self, state_provider, client: RouterClient | None = None, ip_provider=None, clock=time.monotonic):
        self.state_provider = state_provider
        self.client = client or RouterClient()
        self.ip_provider = ip_provider
        self.clock = clock
        self._lock = threading.Lock()
        # The loop and the admin panel's mirror can both compute a screen.
        self._compute_lock = threading.Lock()
        self._failures = FailureLog(logger)
        self._state = MatrixState()
        self._state_ts = None
        self._started_ts = None
        self._ip = None
        self._status = "starting"      # starting / no_router / no_sketch / running / error
        self._error = None
        self._screen = None
        self._frames_sent = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="led-matrix").start()

    def _loop(self) -> None:
        while True:
            try:
                delay = self.step()
                self._failures.succeeded("LED matrix")
            except Exception:
                self._failures.failed("LED matrix", "LED matrix loop failed, retrying")
                delay = self.RETRY_SECONDS
            time.sleep(delay)

    def _screen_now(self, now: float) -> dict:
        """The screen for this moment, re-reading the state when it's due."""
        with self._compute_lock:
            if self._started_ts is None:
                self._started_ts = now
                try:
                    self._ip = self.ip_provider() if self.ip_provider else None
                except Exception:
                    self._ip = None
            if self._state_ts is None or now - self._state_ts >= STATE_REFRESH_SECONDS:
                self._state_ts = now
                try:
                    self._state = self.state_provider()
                    self._failures.succeeded("LED matrix state")
                except Exception:
                    self._failures.failed("LED matrix state", "LED matrix: couldn't read the system state")
            screen = screen_at(self._state, now - self._started_ts, self._ip)
        with self._lock:
            self._screen = screen
        return screen

    def step(self) -> float:
        """One frame. Returns how long to wait before the next one.

        Where there's nothing to draw on - no matrix, no sketch - it does
        nothing at all but look again in RETRY_SECONDS."""
        if not self.client.available():
            self._set_status("no_router", None)
            return self.RETRY_SECONDS
        screen = self._screen_now(self.clock())
        try:
            self.client.call(FRAME_METHOD, to_wire(screen["frame"]))
        except RouterError as exc:
            self._set_status("no_sketch" if "not available" in str(exc) else "error", str(exc))
            return self.RETRY_SECONDS
        except OSError as exc:
            self._set_status("error", str(exc))
            return self.RETRY_SECONDS
        self._frames_sent += 1
        self._set_status("running", None)
        return SCROLL_STEP_SECONDS if screen["screen"] == "boot" else FRAME_SECONDS

    def _set_status(self, status: str, error: str | None) -> None:
        if status != self._status:
            if status == "running":
                logger.info("LED matrix: drawing")
            elif status == "no_sketch":
                logger.warning("LED matrix: the sketch isn't loaded on the microcontroller - see "
                               "docs/quick-installation-arduino-uno-q.md (%s)", error)
            elif status == "error":
                logger.warning("LED matrix: %s", error)
        with self._lock:
            self._status = status
            self._error = error

    def status(self) -> dict:
        if self._status != "running":
            # Not drawing (no matrix here, or no sketch): the loop only
            # wakes every 30 seconds, so the admin panel's mirror is
            # computed when it asks - live while someone watches, no work
            # while nobody does.
            self._screen_now(self.clock())
        with self._lock:
            screen = dict(self._screen) if self._screen else None
            return {"status": self._status, "error": self._error, "frames_sent": self._frames_sent,
                    "screen": screen, "state": asdict(self._state)}
