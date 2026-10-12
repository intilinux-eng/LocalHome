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


# ------------------------------------------------------------ the package --

class FakeBoard(deploy_board.Board):
    """A board that answers from a table of commands, and remembers them."""

    def __init__(self, answers):
        self.name = "prova"
        self.answers = answers
        self.ran = []

    def run(self, command, stdin=None, timeout=300):
        self.ran.append(command)
        for start, answer in self.answers.items():
            if start in command:
                return answer
        return 0, "", ""


class Args:
    board = "prova"
    deb = None
    rollback = False
    check = False
    usb = False
    service = "localhome"
    port = 5000
    branch = None
    allow_older = False


def test_the_packages_version_from_its_name():
    assert deploy_board.deb_version("dist/localhome_0.1.0+git202610121530.abc1234_arm64.deb") == \
        "0.1.0+git202610121530.abc1234"
    import pytest
    with pytest.raises(SystemExit):
        deploy_board.deb_version("other_1.0_amd64.deb")


def test_the_installed_package_or_none():
    installed = FakeBoard({"dpkg-query": (0, "install ok installed 0.1.0+git1.abc", "")})
    assert deploy_board.packaged_version(installed) == "0.1.0+git1.abc"
    assert deploy_board.packaged_version(FakeBoard({"dpkg-query": (1, "", "")})) is None


def test_a_rollback_takes_the_newest_kept_package_that_isnt_running():
    kept = ["/var/cache/localhome/debs/localhome_3_arm64.deb", "/var/cache/localhome/debs/localhome_2_arm64.deb"]
    assert deploy_board.previous_deb(kept, "3").endswith("localhome_2_arm64.deb")
    assert deploy_board.previous_deb(kept[:1], "3") is None


def test_a_board_on_the_package_refuses_a_git_update():
    import pytest
    board = FakeBoard({"hostname": (0, "prova\n", ""), "dpkg-query": (0, "install ok installed 0.1.0+git1.abc", "")})
    with pytest.raises(SystemExit, match="--deb"):
        deploy_board.deploy(board, "pw", Args())
    assert not any("git " in command for command in board.ran), "nothing done with git there"


def test_check_on_a_package_says_what_and_changes_nothing(tmp_path):
    deb = tmp_path / "localhome_0.1.0+git2.def_arm64.deb"
    deb.write_bytes(b"!<arch>\n")
    args = Args()
    args.deb, args.check = str(deb), True
    board = FakeBoard({"hostname": (0, "prova\n", ""), "dpkg-query": (0, "install ok installed 0.1.0+git1.abc", "")})
    assert deploy_board.deploy(board, "pw", args) == 0
    assert not any("apt-get" in command or "install -D" in command for command in board.ran)
