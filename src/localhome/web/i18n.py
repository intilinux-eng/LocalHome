"""Loads the dashboard's UI strings for the language selected via
`web.language` in config.yaml (see docs/configuration.md#language).

Add a new language by copying locales/en.json to locales/<code>.json
and translating the values - every placeholder in curly braces (e.g.
`{percent}`) is substituted by app.js at render time and must be kept.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "en"


def available_languages() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def load_translations(language: str) -> dict:
    path = LOCALES_DIR / f"{language}.json"
    if not path.is_file():
        logger.warning(
            "Unknown web.language '%s' (available: %s), falling back to '%s'",
            language, ", ".join(available_languages()), DEFAULT_LANGUAGE,
        )
        path = LOCALES_DIR / f"{DEFAULT_LANGUAGE}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)
