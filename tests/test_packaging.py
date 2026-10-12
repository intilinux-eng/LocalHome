"""The .deb as far as a PC can check it: the tree packaging/stage.py lays
out, the maintainer scripts, the version, how Docker is reached. The
build itself (Nuitka under QEMU) runs with tools/build_deb.py."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = _load(ROOT / "packaging" / "stage.py", "stage")
build_deb = _load(ROOT / "tools" / "build_deb.py", "build_deb")


def test_the_packages_tree(tmp_path):
    dist = tmp_path / "localhome.dist"
    (dist / "localhome" / "web" / "static").mkdir(parents=True)
    (dist / "localhome.bin").write_bytes(b"\x7fELF")
    out = tmp_path / "stage"
    control = stage.stage(dist, ROOT, out, "0.1.0+git202610121530.abc1234").read_text(encoding="utf-8")
    assert "Version: 0.1.0+git202610121530.abc1234\n" in control and "Architecture: arm64" in control
    assert "\r" not in control and "Installed-Size: " in control
    for path in ("opt/localhome/localhome.bin", "usr/lib/systemd/system/localhome.service",
                 "usr/lib/localhome/tune-board.sh", "usr/lib/localhome/watchdog/localhome-service-stopped.sh",
                 "usr/lib/localhome/config.example.yaml", "etc/default/localhome",
                 "etc/mosquitto/conf.d/localhome.conf", "DEBIAN/postinst", "DEBIAN/prerm", "DEBIAN/postrm"):
        assert (out / path).is_file(), path
    for conffile in (out / "DEBIAN" / "conffiles").read_text(encoding="utf-8").split():
        assert (out / conffile.lstrip("/")).is_file(), conffile
    for script in ("DEBIAN/postinst", "usr/lib/localhome/tune-board.sh"):
        assert b"\r\n" not in (out / script).read_bytes(), "Linux line ends"
        if os.name != "nt":
            assert (out / script).stat().st_mode & 0o111
    unit = (out / "usr/lib/systemd/system/localhome.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/localhome/localhome" in unit and "LOCALHOME_CONFIG=/var/lib/localhome/config.yaml" in unit
    assert "/usr/lib/localhome/watchdog/localhome-service-stopped.sh" in unit, "the stop hook the package ships"


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell here")
@pytest.mark.parametrize("script", ["packaging/debian/postinst", "packaging/debian/prerm",
                                    "packaging/debian/postrm", "packaging/build.sh", "deploy/tune-board.sh"])
def test_the_scripts_parse(script):
    assert subprocess.run(["sh", "-n", str(ROOT / script)]).returncode == 0


def test_a_later_commit_is_a_higher_version():
    older = build_deb.version_of("0.1.0", "202610121530", "abc1234")
    newer = build_deb.version_of("0.1.0", "202610121531", "0000000")
    assert older == "0.1.0+git202610121530.abc1234"
    # dpkg compares the digits as numbers, the rest by character: the time decides.
    assert newer.split("+git")[1][:12] > older.split("+git")[1][:12]


def test_docker_on_the_path_or_through_wsl():
    assert build_deb.docker_command(lambda name: "/bin/docker" if name == "docker" else None) == ["docker"]
    assert build_deb.docker_command(lambda name: "wsl.exe" if name == "wsl" else None) == ["wsl", "docker"]
    with pytest.raises(SystemExit):
        build_deb.docker_command(lambda name: None)
    assert build_deb.docker_path(Path("/x/y"), ["docker"]) == str(Path("/x/y"))


def test_check_says_what_it_would_build(capsys, monkeypatch):
    monkeypatch.setattr(build_deb, "docker_command", lambda: ["docker"])
    build_deb.main(["--check"])
    out = capsys.readouterr().out
    assert "LocalHome 0.1.0+git" in out and "_arm64.deb" in out
