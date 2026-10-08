"""deploy/watchdog/localhome-wifi-watchdog.sh, run for real - with the
router, the Wi-Fi and the broker's connections played by stub commands,
and the minutes by its uptime: when do the devices "not reach the board"?

Seen in a board's watchdog log: its H&T, caught by a check while it
reported, then asleep - "no Shelly connected for 10 min", the Wi-Fi
reconnected twice every 2 hours on a network that was fine. Only a
connection still there a minute later counts now: a mains Shelly, or an
H&T on USB-C, which stays connected."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "watchdog" / "localhome-wifi-watchdog.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

STUBS = {
    "nmcli": 'echo "nmcli $*" >> "$FAKE_CALLS"\n'
             'case "$*" in\n'
             '  "-t -f DEVICE,TYPE device") echo "wlan0:wifi" ;;\n'
             '  "-g GENERAL.CONNECTION device show wlan0") echo "Casa" ;;\n'
             'esac\n',
    "ip": 'echo "default via 192.168.1.1 dev wlan0"\n',
    "ping": "exit 0\n",
    "ss": 'cat "$FAKE_SS" 2>/dev/null\n',
    "systemctl": 'echo "systemctl $*" >> "$FAKE_CALLS"\n',
}


class Board:
    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_bytes(f"#!/bin/sh\n{body}".encode())
            stub.chmod(0o755)
        self.state, self.data = tmp_path / "state", tmp_path / "data"
        self.state.mkdir()
        (self.state / "boot_reconnect_done").touch()      # past the once-per-boot reconnect
        self.ss, self.calls = tmp_path / "ss.txt", tmp_path / "calls.txt"

    def minute(self, uptime: int, *peers: str) -> None:
        """One run of the watchdog, `peers` connected to the broker."""
        self.ss.write_text("".join(f"0 0 192.168.1.28:1883 {peer}\n" for peer in peers))
        # The stubs first, then sh's own tools (on Windows, Git's - not its sort.exe).
        path = os.pathsep.join([str(self.bin), str(Path(SH).parent), os.environ["PATH"]])
        env = dict(os.environ, PATH=path,
                   LOCALHOME_WIFI_WATCHDOG_STATE=str(self.state), LOCALHOME_WIFI_WATCHDOG_DATA=str(self.data),
                   LOCALHOME_WIFI_WATCHDOG_UPTIME=str(uptime), FAKE_SS=str(self.ss), FAKE_CALLS=str(self.calls))
        subprocess.run([SH, str(SCRIPT)], env=env, check=True, capture_output=True, timeout=30)

    def log(self) -> list[str]:
        path = self.data / "wifi-watchdog.log"
        return [" ".join(line.split()[2:]) for line in path.read_text().splitlines()] if path.exists() else []

    def reconnects(self) -> int:
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return sum(1 for call in calls if call == "nmcli connection up Casa")


def test_a_battery_sensor_waking_never_reconnects_the_wifi(tmp_path):
    board = Board(tmp_path)
    for minute in [*range(0, 14), *range(120, 134)]:     # two reports, 2 hours apart, and the minutes after
        awake = minute % 120 == 0
        board.minute(1000 + minute * 60, *([f"192.168.1.30:{51000 + minute}"] if awake else []))
    assert board.log() == [] and board.reconnects() == 0


def test_devices_that_stayed_and_are_gone_reconnect_the_wifi(tmp_path):
    """A mains Shelly - or an H&T on USB-C - connected minute after minute,
    then nobody for 10 minutes: the router has stopped passing them."""
    board = Board(tmp_path)
    for minute in range(3):
        board.minute(1000 + minute * 60, "192.168.1.34:50000")
    for minute in range(3, 14):
        board.minute(1000 + minute * 60)
    assert board.log() == ["lan_silent 10"] and board.reconnects() == 1
    board.minute(1000 + 14 * 60, "192.168.1.30:51000")        # a battery sensor waking: still silent
    assert board.log() == ["lan_silent 10"]
    board.minute(1000 + 15 * 60, "192.168.1.34:50100")        # the Shelly back, a new connection...
    board.minute(1000 + 16 * 60, "192.168.1.34:50100")        # ...still there a minute later
    assert board.log()[-1].startswith("lan_back")
