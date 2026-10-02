"""Updates LocalHome on a board from this PC, by its name - over the
network (<name>.local, SSH) or, with --usb, over the USB cable (adb).

    python tools/deploy_board.py antonio              # the latest commit of the board's branch
    python tools/deploy_board.py carlucci --usb       # the same, over the USB cable
    python tools/deploy_board.py antonio --check      # say what would happen, change nothing
    python tools/deploy_board.py antonio --rollback   # back to the commit before the last update
    python tools/deploy_board.py antonio --branch main --allow-older   # another branch, even an older one

What goes to the board is a commit of this repository: committed work
only, never what's uncommitted here. By default the latest commit of the
branch the board is already on - a board on feature/redesign stays on
it; --branch picks another, and one that would take commits away is
refused unless --allow-older says so.

Then, in order: only the commits the board is missing travel, as a git
bundle; the board's branch moves to the new commit; if pyproject.toml or
constraints.txt changed, its Python environment is reinstalled (if that
fails the code goes back and nothing restarts); LocalHome restarts and
the script waits for the page to answer, then prints any warning logged
since. Each update is noted on the board (~/.localhome-deploys), which is
what --rollback goes back to. A board with local changes to tracked files
is left alone.

The board's password is asked once (or read from LH_SSH_PW): used for
SSH and for sudo, never written anywhere. Needs paramiko for the network
(pip install -e .[deploy]); --usb needs adb, which comes with the
Arduino IDE / App Lab and is found on its own.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import glob
import hashlib
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER = "arduino"
HISTORY = "~/.localhome-deploys"
BUNDLE = "~/.localhome-deploy.bundle"
# Files whose change means the board's Python environment needs reinstalling.
DEPENDENCY_FILES = ("pyproject.toml", "constraints.txt")
ANSWER_TIMEOUT_SECONDS = 120


# --------------------------------------------------------------- this PC --

def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def known_here(commit: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT,
                          capture_output=True).returncode == 0


def is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT).returncode == 0


def plan_update(head: str, target: str, ancestor=is_ancestor) -> str:
    """"same", "forward" (only adds commits), "older" (takes commits away)
    or "diverged" (both)."""
    if head == target:
        return "same"
    if ancestor(head, target):
        return "forward"
    if ancestor(target, head):
        return "older"
    return "diverged"


def last_update(history: str, head: str) -> tuple[str, str] | None:
    """(commit before, branch) of the update that brought the board to
    `head`, from the board's ~/.localhome-deploys - None if there's none."""
    for line in reversed(history.splitlines()):
        parts = line.split()
        if len(parts) == 4 and parts[2] == head:
            return parts[1], parts[3]
    return None


def commits_between(older: str, newer: str) -> list[str]:
    out = git("log", "--oneline", "--no-decorate", f"{older}..{newer}")
    return out.splitlines() if out else []


# ------------------------------------------------------------- the board --

class Board:
    """One board, reached over SSH or adb: run() a shell command there,
    put() a file there."""

    name: str

    def run(self, command: str, stdin: str | None = None, timeout: float = 300) -> tuple[int, str, str]:
        raise NotImplementedError

    def put(self, data: bytes, remote: str) -> None:
        code, out, err = self.run(f"base64 -d > {remote} && sha256sum {remote}", stdin=base64.b64encode(data).decode())
        if code or out.split()[:1] != [hashlib.sha256(data).hexdigest()]:
            raise SystemExit(f"the file didn't reach {self.name} intact: {err.strip() or out.strip()}")

    def out(self, command: str) -> str:
        code, out, err = self.run(command)
        if code:
            raise SystemExit(f"on {self.name}, `{command}` failed: {(err or out).strip()}")
        return out.strip()

    def close(self) -> None:
        pass


class SshBoard(Board):
    def __init__(self, name: str, password: str):
        try:
            import paramiko
        except ImportError:
            raise SystemExit("the network needs paramiko: pip install -e .[deploy]  (or use --usb)")
        self.name = name
        address = resolve_ipv4(f"{name}.local")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(address, username=USER, password=password, timeout=15,
                                look_for_keys=False, allow_agent=False)
        except paramiko.AuthenticationException:
            raise SystemExit(f"{name}: wrong password")
        except OSError as exc:
            raise SystemExit(f"{name}.local ({address}) doesn't answer SSH: {exc}")

    def run(self, command, stdin=None, timeout=300):
        channel_in, channel_out, channel_err = self.client.exec_command(command, timeout=timeout)
        if stdin is not None:
            channel_in.write(stdin)
        channel_in.channel.shutdown_write()
        out = channel_out.read().decode("utf-8", "replace")
        err = channel_err.read().decode("utf-8", "replace")
        return channel_out.channel.recv_exit_status(), out, err

    def close(self):
        self.client.close()


def resolve_ipv4(host: str) -> str:
    """The board by name, over IPv4: its .local name also resolves to an
    IPv6 link-local address, which SSH from Windows may not answer on."""
    try:
        return socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        raise SystemExit(f"{host} isn't found on this network - is it on, and on the same Wi-Fi? "
                         "(or plug it in by USB and add --usb)")


class AdbBoard(Board):
    def __init__(self, name: str):
        self.name = name
        self.adb = find_adb()
        listing = subprocess.run([self.adb, "devices"], capture_output=True, text=True).stdout
        serials = [line.split()[0] for line in listing.splitlines()[1:] if line.strip().endswith("device")]
        found = {}
        for serial in serials:
            self.serial = serial
            found[serial] = self.run("hostname")[1].strip()
            if found[serial] == name:
                return
        on_usb = ", ".join(found.values()) or "none"
        raise SystemExit(f"{name} isn't connected by USB (boards on USB: {on_usb})")

    def run(self, command, stdin=None, timeout=300):
        result = subprocess.run([self.adb, "-s", self.serial, "shell", command], input=stdin, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return result.returncode, result.stdout, result.stderr


def find_adb() -> str:
    candidates = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Arduino15", "packages", "arduino", "tools", "adb", "*", "adb*"),
                  os.path.expanduser("~/.arduino15/packages/arduino/tools/adb/*/adb"),
                  os.path.expanduser("~/Library/Arduino15/packages/arduino/tools/adb/*/adb")]
    for pattern in candidates:
        for path in sorted(glob.glob(pattern), reverse=True):
            if os.path.basename(path).lower() in ("adb", "adb.exe"):
                return path
    try:
        subprocess.run(["adb", "version"], capture_output=True, check=True)
        return "adb"
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("adb not found - it comes with the Arduino IDE / App Lab")


def sudo(board: Board, password: str, command: str) -> tuple[int, str, str]:
    return board.run(f"sudo -S -p '' {command}", stdin=password + "\n")


# ------------------------------------------------------------------ steps --

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Update LocalHome on a board from this PC.")
    parser.add_argument("board", help="the board's name, e.g. antonio (reached as antonio.local)")
    parser.add_argument("--usb", action="store_true", help="over the USB cable (adb) instead of the network")
    parser.add_argument("--branch", help="deploy this branch's latest commit (default: the board's branch)")
    parser.add_argument("--allow-older", action="store_true", help="allow a commit that takes commits away")
    parser.add_argument("--rollback", action="store_true", help="back to the commit before the last update")
    parser.add_argument("--check", action="store_true", help="say what would happen, change nothing")
    parser.add_argument("--service", default="localhome", help="the systemd service (default: localhome)")
    parser.add_argument("--port", type=int, default=5000, help="the web page's port (default: 5000)")
    args = parser.parse_args()

    password = os.environ.get("LH_SSH_PW") or getpass.getpass(f"{args.board}'s password: ")
    board = AdbBoard(args.board) if args.usb else SshBoard(args.board, password)
    try:
        return deploy(board, password, args)
    finally:
        board.close()


def deploy(board: Board, password: str, args) -> int:
    name = board.out("hostname")
    if name != args.board:
        raise SystemExit(f"{args.board} answered as '{name}' - not touching it")
    if sudo(board, password, "true")[0]:
        raise SystemExit(f"{name}: sudo refused the password")
    repo = board.out(f"systemctl show {shlex.quote(args.service)} -p WorkingDirectory --value")
    if not repo:
        raise SystemExit(f"{name} has no '{args.service}' service")
    in_repo = f"git -C {shlex.quote(repo)}"
    head = board.out(f"{in_repo} rev-parse HEAD")
    branch = board.out(f"{in_repo} branch --show-current")
    print(f"{name}: {repo}, {branch or 'no branch'} at {head[:7]}")

    if args.rollback:
        previous = last_update(board.run(f"cat {HISTORY}")[1], head)
        if previous is None:
            raise SystemExit(f"{name}: no update to roll back - its {HISTORY} doesn't say how it got to {head[:7]}")
        target, target_branch = previous
    else:
        target_branch = args.branch or branch
        if not target_branch:
            raise SystemExit(f"{name} isn't on a branch - say which with --branch")
        target = git("rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}")
        if git("status", "--porcelain", "--untracked-files=no"):
            print("note: this repository has uncommitted changes - they are not deployed, only commits are")

    if not known_here(head):
        raise SystemExit(f"{name} is on {head[:7]}, which this repository doesn't have - update it by hand")
    kind = plan_update(head, target)
    if kind == "same" and target_branch == branch:
        print(f"{name} already runs {target[:7]} ({git('log', '-1', '--format=%s', target)}) - nothing to do")
        return 0
    if kind == "diverged":
        raise SystemExit(f"{target_branch} ({target[:7]}) and {name} ({head[:7]}) have each moved on - update by hand")
    if kind == "older" and not (args.rollback or args.allow_older):
        lost = commits_between(target, head)
        raise SystemExit(f"{target_branch} ({target[:7]}) is older than what {name} runs: {len(lost)} commit(s) "
                         f"would go away:\n  " + "\n  ".join(lost[:15]) + "\nadd --allow-older if that's intended")
    local_changes = board.out(f"{in_repo} status --porcelain --untracked-files=no")
    if local_changes:
        raise SystemExit(f"{name} has local changes to tracked files - not touching it:\n{local_changes}")

    if kind == "forward":
        print(f"{len(commits_between(head, target))} new commit(s) for {name}:")
        for line in commits_between(head, target)[:30]:
            print(f"  + {line}")
    elif kind == "older":
        print(f"back from {head[:7]} to {target[:7]}, removing:")
        for line in commits_between(target, head)[:30]:
            print(f"  - {line}")
    if target_branch != branch:
        print(f"branch: {branch or '-'} -> {target_branch}")
    changed = git("diff", "--name-only", head, target).splitlines()
    reinstall = any(path in DEPENDENCY_FILES for path in changed)
    if reinstall:
        print("dependencies changed: the board's Python environment will be reinstalled")
    if any(path.startswith("deploy/") for path in changed):
        print(f"note: deploy/ changed - afterwards, on {name}: cd {repo} && sudo sh deploy/tune-board.sh")

    # The new commits, sent and fetched - this much --check does too: it
    # proves the transfer works, and touches nothing but FETCH_HEAD.
    # A rollback goes back to a commit the board already had: nothing to send.
    if kind == "forward" and not args.rollback:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, "update.bundle")
            git("bundle", "create", bundle, f"{head}..refs/heads/{target_branch}")
            board.put(Path(bundle).read_bytes(), BUNDLE)
        fetched = board.out(f"{in_repo} fetch -q {BUNDLE} {shlex.quote(target_branch)} && {in_repo} rev-parse FETCH_HEAD")
        board.run(f"rm -f {BUNDLE}")
        if fetched != target:
            raise SystemExit(f"{name} received {fetched[:7]}, not {target[:7]} - nothing changed")
    if args.check:
        print("--check: the password works and the transfer works; nothing was changed")
        return 0

    board.out(f"{in_repo} checkout -q -B {shlex.quote(target_branch)} {target}")
    board.out(f"echo {datetime.now().strftime('%Y-%m-%dT%H:%M')} {head} {target} {shlex.quote(target_branch)} >> {HISTORY}")
    print(f"{name}: code at {target[:7]}")
    if reinstall:
        print("reinstalling (a minute or two)...")
        code, out, err = board.run(f"cd {shlex.quote(repo)} && .venv/bin/pip install -q -e . -c constraints.txt", timeout=1200)
        if code:
            board.out(f"{in_repo} checkout -q -B {shlex.quote(branch or target_branch)} {head}")
            raise SystemExit(f"pip failed - the code went back to {head[:7]}, LocalHome wasn't restarted:\n{(err or out)[-2000:]}")

    since = board.out("date +%s")
    if sudo(board, password, f"systemctl restart {shlex.quote(args.service)}")[0]:
        raise SystemExit(f"{name}: the restart was refused")
    print("restarting LocalHome...")
    url = f"http://127.0.0.1:{args.port}/api/server-time"
    deadline = time.time() + ANSWER_TIMEOUT_SECONDS
    answering = False
    while time.time() < deadline:
        time.sleep(3)
        if board.run(f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 {url}")[1].strip() == "200":
            answering = True
            break
    warnings = board.run(f"journalctl -u {shlex.quote(args.service)} --since @{since} -p warning --no-pager -q")[1].strip()
    if warnings:
        print("warnings since the restart:\n" + warnings)
    if not answering:
        print(f"{name}: LocalHome doesn't answer after {ANSWER_TIMEOUT_SECONDS} s - "
              f"look at: journalctl -u {args.service} -n 50\n"
              f"back to the previous version: python tools/deploy_board.py {name}"
              f"{' --usb' if args.usb else ''} --rollback")
        return 1
    print(f"{name}: done - {target[:7]} ({git('log', '-1', '--format=%s', target)}), page answering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
