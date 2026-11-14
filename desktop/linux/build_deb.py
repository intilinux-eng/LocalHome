#!/usr/bin/env python3
"""Builds dist/localhome-desktop_<version>_all.deb: LocalHome for the Linux
desktop, for Debian and Ubuntu (KDE and others). Plain Python and the shared
page (desktop/web): one package for every processor, built in seconds with
dpkg-deb, no Docker. Qt comes from the system's own packages (Depends).

    python3 desktop/linux/build_deb.py
    sudo apt install ./dist/localhome-desktop_<version>_all.deb
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
ROOT = DESKTOP.parent
DIST = ROOT / "dist"
LIB = "usr/lib/localhome-desktop"
ICON_SIZES = (48, 64, 128, 256)

CONTROL = """Package: localhome-desktop
Version: {version}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6, python3-pyqt6.qtwebengine
Maintainer: LocalHome <noreply@localhome.invalid>
Homepage: https://github.com/intilinux-eng/LocalHome
Description: LocalHome on the desktop: the house at a glance
 A window and a tray icon for a LocalHome board: the zones with their
 comfort and charts, outside, the power, the VMC, the shutters, away, and
 the state of each device. A notice when a zone turns on, when there is
 something to check, when the board cannot be reached. It uses only the
 board's existing API; nothing on the board changes.
"""

LAUNCHER = """#!/bin/sh
# LocalHome for the Linux desktop (package localhome-desktop).
exec /usr/bin/python3 -s /usr/lib/localhome-desktop/localhome-desktop.py "$@"
"""

COPYRIGHT = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: LocalHome
Source: https://github.com/intilinux-eng/LocalHome

Files: *
License: AGPL-3.0-or-later
 LocalHome is free software under the GNU Affero General Public License,
 version 3 or later. See the LICENSE file of the project.
"""


def version() -> str:
    text = (HERE / "localhome_desktop" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def icons(stage: Path) -> None:
    """The app's icon (desktop/app.ico) at the sizes the desktop asks for."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is necessary to make the icons: sudo apt install python3-pil")
    source = Image.open(DESKTOP / "app.ico")
    biggest = max(source.ico.sizes())
    image = source.ico.getimage(biggest).convert("RGBA")
    for size in ICON_SIZES:
        folder = stage / f"usr/share/icons/hicolor/{size}x{size}/apps"
        folder.mkdir(parents=True)
        image.resize((size, size), Image.Resampling.LANCZOS).save(folder / "localhome-desktop.png")
    image.resize((256, 256), Image.Resampling.LANCZOS).save(stage / LIB / "icon.png")


def build() -> Path:
    v = version()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "pkg"
        lib = stage / LIB
        shutil.copytree(HERE / "localhome_desktop", lib / "localhome_desktop",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(HERE / "localhome-desktop.py", lib / "localhome-desktop.py")
        (lib / "web").mkdir()
        for file in sorted((DESKTOP / "web").iterdir()):
            if file.suffix in (".html", ".js", ".css"):
                shutil.copy2(file, lib / "web" / file.name)
        icons(stage)
        launcher = stage / "usr/bin/localhome-desktop"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(LAUNCHER, encoding="utf-8")
        apps = stage / "usr/share/applications"
        apps.mkdir(parents=True)
        shutil.copy2(HERE / "localhome-desktop.desktop", apps / "localhome-desktop.desktop")
        doc = stage / "usr/share/doc/localhome-desktop"
        doc.mkdir(parents=True)
        (doc / "copyright").write_text(COPYRIGHT, encoding="utf-8")
        debian = stage / "DEBIAN"
        debian.mkdir()
        (debian / "control").write_text(CONTROL.format(version=v), encoding="utf-8")
        # Files readable by all, programs runnable by all, nothing writable but by root.
        for path in stage.rglob("*"):
            path.chmod(0o755 if path.is_dir() or path == launcher else 0o644)
        DIST.mkdir(exist_ok=True)
        out = DIST / f"localhome-desktop_{v}_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(out)],
                       check=True, stdout=subprocess.DEVNULL)
    return out


if __name__ == "__main__":
    package = build()
    listing = subprocess.run(["dpkg-deb", "-c", str(package)], capture_output=True, text=True, check=True).stdout
    print(f"{package} ({package.stat().st_size // 1024} KB, {len(listing.splitlines())} entries)")
