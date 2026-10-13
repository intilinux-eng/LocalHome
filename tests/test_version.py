"""The version whoever uses LocalHome sees: one number, the same in
pyproject.toml, at the bottom of the home page and in the admin's System
status - and the changelog's latest release."""
import re
from pathlib import Path

from localhome import __version__
from localhome.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_three_numbers_and_pyproject_says_the_same():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__


def test_the_changelog_has_a_section_for_it():
    assert f"\n## {__version__} - " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_home_page_and_the_system_status_show_it(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("web:\n  port: 5000\n  language: it\nintegrations: []\n", encoding="utf-8")
    client = create_app(str(config)).test_client()
    assert f"Versione {__version__}" in client.get("/").get_data(as_text=True)
    assert client.get("/api/system-status").get_json()["software"]["version"] == __version__
