"""tools/deploy_board.py's decisions: which way an update goes, and what
--rollback goes back to."""
import importlib.util
from pathlib import Path

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
