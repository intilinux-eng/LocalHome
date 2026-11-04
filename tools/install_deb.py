"""Installs LocalHome's Debian package on a board from this PC - the first time,
or as an update: the newest arm64 package in dist/ (tools/build_deb.py) is
copied over and installed with apt, then the page has to answer.

    python tools/install_deb.py <board>               # reached as <board>.local
    python tools/install_deb.py <board> --host <ip>   # at this address instead
    python tools/install_deb.py <board> --usb         # over the USB cable (adb)
    python tools/install_deb.py <board> --check       # say what would happen, change nothing

The board has to answer with its name before anything is touched. Its password
is asked once (or read from LH_SSH_PW), for SSH and sudo, never written down.
A board running a git checkout of LocalHome moves onto the package: its
configuration and history are copied to /var/lib/localhome (packaging/deb/).
"""
from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_board import AdbBoard, SshBoard, sudo  # noqa: E402

DIST = Path(__file__).resolve().parents[1] / "dist"


def newest_package() -> Path:
    built = sorted(DIST.glob("localhome_*_arm64.deb"), key=lambda p: p.stat().st_mtime)
    if not built:
        raise SystemExit("no arm64 package in dist/ - build one: python tools/build_deb.py")
    return built[-1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("board", help="the board's name (its hostname)")
    parser.add_argument("--host", help="reach it at this address instead of <board>.local")
    parser.add_argument("--usb", action="store_true", help="over the USB cable (adb) instead of the network")
    parser.add_argument("--package", type=Path, help="this .deb instead of the newest in dist/")
    parser.add_argument("--check", action="store_true", help="say what would happen, change nothing")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    package = args.package or newest_package()

    password = os.environ.get("LH_SSH_PW") or getpass.getpass(f"{args.board}'s password: ")
    board = AdbBoard(args.board) if args.usb else SshBoard(args.board, password, args.host)
    try:
        name = board.out("hostname")
        if name != args.board:
            raise SystemExit(f"{args.board} answered as '{name}' - not touching it")
        if sudo(board, password, "true")[0]:
            raise SystemExit(f"{name}: sudo refused the password")
        arch = board.out("dpkg --print-architecture")
        if not package.name.endswith(f"_{arch}.deb"):
            raise SystemExit(f"{name} is {arch}, {package.name} isn't for it")
        installed = board.run("dpkg-query -W -f '${Version}' localhome")[1].strip()
        git_service = board.run("test -f /etc/systemd/system/localhome.service")[0] == 0
        print(f"{name}: {arch}, Debian {board.out('cat /etc/debian_version')}, "
              f"{'LocalHome ' + installed + ' from a package' if installed else 'no LocalHome package'}"
              f"{', a git install of LocalHome (it moves onto the package)' if git_service else ''}")
        print(f"package: {package.name} ({package.stat().st_size // 1024 // 1024} MB)")
        if args.check:
            return 0

        remote = f"/tmp/{package.name}"
        print("copying...")
        if args.usb:
            subprocess.run([board.adb, "-s", board.serial, "push", str(package), remote], check=True, capture_output=True)
        else:
            board.put(package.read_bytes(), remote)
        print("apt update...")
        sudo(board, password, "env DEBIAN_FRONTEND=noninteractive apt-get update -q")
        print("installing (apt brings what it needs: a few minutes the first time)...")
        started = time.time()
        code, out, err = board.run(f"sudo -S -p '' env DEBIAN_FRONTEND=noninteractive apt-get install -y {shlex.quote(remote)}",
                                   stdin=password + "\n", timeout=1800)
        board.run(f"rm -f {shlex.quote(remote)}")
        print((out + err).strip())
        if code:
            raise SystemExit(f"{name}: apt failed")
        print(f"installed in {int(time.time() - started)} s; waiting for the page...")
        # Asked on the board itself: it answers there whatever this PC can reach.
        ask = (f"python3 -c \"import urllib.request; "
               f"print(urllib.request.urlopen('http://127.0.0.1:{args.port}/api/server-time', timeout=5).status)\"")
        for _ in range(60):
            if board.run(ask)[1].strip() == "200":
                version = board.out("dpkg-query -W -f '${Version}' localhome")
                print(f"LocalHome {version} answers on {name}: http://{args.host or name + '.local'}:{args.port}/")
                return 0
            time.sleep(3)
        print(board.run("systemctl status localhome --no-pager -n 30")[1])
        raise SystemExit(f"{name}: the page didn't answer in 3 minutes")
    finally:
        board.close()


if __name__ == "__main__":
    sys.exit(main())
