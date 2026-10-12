"""Loads the dashboard's UI strings for the language selected via
`web.language` in config.yaml (see docs/configuration.md#language).

Add a new language by copying locales/en.json to locales/<code>.json
and translating the values - every placeholder in curly braces (e.g.
`{percent}`) is substituted by the pages' tr() at render time and must
be kept.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"


def get_locales_dir() -> Path:
    env = os.environ.get("LOCALHOME_WEB_DIR")
    if env:
        candidate = Path(env) / "locales"
        if candidate.is_dir():
            return candidate
    exe = sys.executable
    is_frozen = getattr(sys, "frozen", False) or not (
        exe.endswith("python") or exe.endswith("python3") or exe.endswith("python.exe") or exe.endswith("python3.exe")
    )
    if is_frozen:
        exe_dir = Path(exe).parent
        for candidate in (
            exe_dir.parent / "share" / "localhome" / "web" / "locales",
            Path("/usr/local/share/localhome/web/locales"),
            Path("/opt/localhome/web/locales"),
            exe_dir / "web" / "locales",
            exe_dir.parent / "web" / "locales",
        ):
            if candidate.is_dir():
                return candidate
    return Path(__file__).parent / "locales"


def available_languages() -> list[str]:
    return sorted(p.stem for p in get_locales_dir().glob("*.json"))


def load_translations(language: str) -> dict:
    locales_dir = get_locales_dir()
    path = locales_dir / f"{language}.json"
    if not path.is_file():
        logger.warning(
            "Unknown web.language '%s' (available: %s), falling back to '%s'",
            language, ", ".join(available_languages()), DEFAULT_LANGUAGE,
        )
        path = locales_dir / f"{DEFAULT_LANGUAGE}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)
