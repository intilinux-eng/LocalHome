"""Where LocalHome runs from: its source (a git checkout, a PC) or a
binary compiled by Nuitka, as the .deb installs it (packaging/).

Compiled, the package's data files (the web pages, the translations,
version.json) sit where each module's __file__ says - Nuitka keeps
them beside the compiled package - so the same lookups work in both;
what changes is the default configuration's place and how to start
this program again.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Nuitka defines __compiled__ in every module it compiles: the one
# reliable sign (an interpreter's name - python3.13 - says nothing).
COMPILED = "__compiled__" in globals()

# The .deb's configuration and data (packaging/debian/localhome.service
# says so too, with LOCALHOME_CONFIG).
PACKAGED_CONFIG = Path("/var/lib/localhome/config.yaml")
WEB_DIR_ENV = "LOCALHOME_WEB_DIR"
PACKAGE_DIR = Path(__file__).resolve().parent


def web_dir() -> Path:
    """static/, templates/ and locales/: localhome/web's own folder -
    LOCALHOME_WEB_DIR elsewhere, to try pages without rebuilding."""
    override = os.environ.get(WEB_DIR_ENV)
    if override and os.path.isdir(override):
        return Path(override)
    return PACKAGE_DIR / "web"


def build_info() -> dict | None:
    """version.json, written by the build next to the package:
    {"version", "commit", "date"} - None from a source checkout."""
    try:
        with open(PACKAGE_DIR / "version.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def restart_argv() -> list[str]:
    """What starts this same program again: the binary itself, compiled
    (its sys.executable); the interpreter with the script and arguments
    it was given, from source."""
    if COMPILED:
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, *sys.argv]
