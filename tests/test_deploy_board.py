"""tools/deploy_board.py's decisions: which way an update goes, what
--rollback goes back to, and a board left on a history rewritten here."""
import importlib.util
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("deploy_board", Path(__file__).resolve().parents[1] / "tools" / "deploy_board.py")
deploy_board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_board)

# a - b - c on one line, x off b
HISTORY = {"a": set(), "b": {"a"}, "c": {"a", "b"}, "x": {"a", "b"}}


def ancestor(older, newer):
    return older in HISTORY[newer]


def test_an_update_adds_commits_takes_them_away_or_neither():
    assert deploy_board.plan_update("c", "c", ancestor) == "same"
    assert deploy_board.plan_update("a", "c", ancestor) == "forward"
    assert deploy_board.plan_update("c", "a", ancestor) == "older"
    assert deploy_board.plan_update("c", "x", ancestor) == "diverged"


def test_a_rollback_goes_back_to_the_commit_before_the_update_that_brought_the_board_here():
    history = ("2026-10-01T10:00 aaa bbb feature/redesign\n"
               "2026-10-02T10:00 bbb ccc feature/redesign\n")
    assert deploy_board.last_update(history, "ccc") == ("bbb", "feature/redesign")
    assert deploy_board.last_update(history, "bbb") == ("aaa", "feature/redesign")
    assert deploy_board.last_update(history, "zzz") is None
    assert deploy_board.last_update("", "ccc") is None


class FakeBoard:
    """A board answering as antonio does, on a commit from before this
    repository's history was rewritten; what it's asked, and what it's sent."""

    def __init__(self, head, fetched, changed):
        self.head, self.fetched, self.changed = head, fetched, changed
        self.commands, self.sent = [], []

    def out(self, command):
        self.commands.append(command)
        answers = {"hostname": "antonio", "systemctl show": "/home/arduino/progetto", "rev-parse HEAD": self.head,
                   "branch --show-current": "main", "rev-parse FETCH_HEAD": self.fetched, "diff --name-only": self.changed}
        return next((answer for key, answer in answers.items() if key in command), "")

    def run(self, command, stdin=None, timeout=None):
        self.commands.append(command)
        return 0, "", ""

    def put(self, data, path):
        self.sent.append(len(data))


def test_a_board_on_a_rewritten_history_moves_onto_this_one_only_when_asked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    target = git("rev-parse", "HEAD")
    monkeypatch.setattr(deploy_board, "ROOT", repo)
    old = "0123456789abcdef" * 2 + "01234567"
    board = FakeBoard(head=old, fetched=target, changed="pyproject.toml\nsrc/localhome/x.py")
    args = Namespace(board="antonio", usb=False, branch=None, allow_older=False, rollback=False, check=True,
                     resync=False, service="localhome", port=5000)
    with pytest.raises(SystemExit, match="--resync"):
        deploy_board.deploy(board, "secret", args)
    assert board.sent == [], "refused before anything travels"

    args.resync = True
    assert deploy_board.deploy(board, "secret", args) == 0
    assert len(board.sent) == 1, "the whole branch, once"
    assert any(f"diff --name-only {old} {target}" in c for c in board.commands), "what changed, read on the board"
    assert not any(" checkout " in c for c in board.commands), "--check: nothing moved"

    args.rollback = True
    with pytest.raises(SystemExit):
        deploy_board.deploy(board, "secret", args)      # never back across it
