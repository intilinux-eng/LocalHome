"""Running from source or compiled: the web folder, the build's
version.json read before git, how the program starts again."""
import json
import sys

from localhome import config, runtime
from localhome.web import app as web_app


def test_from_source_the_web_folder_is_the_packages(monkeypatch, tmp_path):
    monkeypatch.delenv(runtime.WEB_DIR_ENV, raising=False)
    assert (runtime.web_dir() / "templates").is_dir() and (runtime.web_dir() / "locales" / "it.json").is_file()
    monkeypatch.setenv(runtime.WEB_DIR_ENV, str(tmp_path))
    assert runtime.web_dir() == tmp_path, "pages tried without rebuilding"


def test_from_source_not_compiled_and_the_checkouts_config():
    assert runtime.COMPILED is False
    assert config.DEFAULT_CONFIG_PATH.parts[-2:] == ("config", "config.yaml")
    assert runtime.restart_argv() == [sys.executable, *sys.argv]


def test_the_builds_version_comes_before_git(monkeypatch, tmp_path):
    (tmp_path / "version.json").write_text(json.dumps({"version": "0.1.0+git1", "commit": "abc1234",
                                                       "date": "2026-10-12T10:00:00+02:00"}), encoding="utf-8")
    monkeypatch.setattr(runtime, "PACKAGE_DIR", tmp_path)
    assert runtime.build_info()["version"] == "0.1.0+git1"
    assert web_app._software_version() == {"commit": "abc1234", "date": "2026-10-12T10:00:00+02:00"}
    monkeypatch.setattr(runtime, "PACKAGE_DIR", tmp_path / "none")
    assert runtime.build_info() is None
