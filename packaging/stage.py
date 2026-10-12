"""Lays out LocalHome's .deb before dpkg-deb packs it (packaging/build.sh):

    /opt/localhome/                         the program Nuitka built (its .dist folder)
    /usr/lib/systemd/system/localhome.service
    /usr/lib/localhome/                     tune-board.sh, its watchdog/ and resource-log/,
                                            the factory configuration
    /etc/default/localhome                  whether the install tunes the board, --lean or not
    /etc/mosquitto/conf.d/localhome.conf    the broker the Shellys reach
    DEBIAN/                                 control, conffiles, postinst, prerm, postrm

    python3 packaging/stage.py <program.dist> <repo> <stage> <version>

The standard library only: it runs in the build machine as it is, and in
the tests on a PC.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ("postinst", "prerm", "postrm")


def _copy(source: Path, target: Path, mode: int = 0o644) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # All text: line ends as Linux wants them, whatever the checkout did.
    target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    os.chmod(target, mode)


def _size_kib(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) // 1024 + 1


def stage(dist: Path, repo: Path, out: Path, version: str) -> Path:
    """The package's tree in `out` (emptied first); returns DEBIAN/control's path."""
    if out.exists():
        shutil.rmtree(out)
    debian = HERE / "debian"
    shutil.copytree(dist, out / "opt" / "localhome")
    _copy(debian / "localhome.service", out / "usr/lib/systemd/system/localhome.service")
    lib = out / "usr/lib/localhome"
    _copy(repo / "deploy/tune-board.sh", lib / "tune-board.sh", 0o755)
    for folder in ("watchdog", "resource-log"):
        for source in sorted((repo / "deploy" / folder).iterdir()):
            _copy(source, lib / folder / source.name, 0o755 if source.suffix == ".sh" else 0o644)
    _copy(repo / "config/config.climate.example.yaml", lib / "config.example.yaml")
    _copy(debian / "default", out / "etc/default/localhome")
    _copy(debian / "mosquitto-localhome.conf", out / "etc/mosquitto/conf.d/localhome.conf")
    control_dir = out / "DEBIAN"
    for name in SCRIPTS:
        _copy(debian / name, control_dir / name, 0o755)
    _copy(debian / "conffiles", control_dir / "conffiles")
    control = (debian / "control.in").read_text(encoding="utf-8")
    control = control.replace("@VERSION@", version).replace("@SIZE@", str(_size_kib(out)))
    path = control_dir / "control"
    path.write_bytes(control.replace("\r\n", "\n").encode("utf-8"))
    os.chmod(path, 0o644)
    return path


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    stage(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
