"""Loads config.yaml and resolves the relative paths inside it (device
inventories, secrets files, the positions/history data files) against the
config file's own directory - so the app behaves the same regardless of
the current working directory it's started from.

See config/config.example.yaml for the file this reads, and
docs/configuration.md for the full reference.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_ENV_VAR = "LOCALHOME_CONFIG"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = _REPO_ROOT / "config" / "config.example.yaml"


@dataclass
class Integration:
    kind: str
    type: str
    options: dict = field(default_factory=dict)


@dataclass
class LocalHomeConfig:
    path: Path
    web_host: str
    web_port: int
    web_language: str
    integrations: list[Integration]
    notifier: Integration | None
    raw: dict

    def resolve(self, relative_path: str) -> str:
        """Resolve a config-relative path against the directory the
        config file itself lives in."""
        p = Path(relative_path)
        return str(p if p.is_absolute() else (self.path.parent / p))


def load_config(path: str | Path | None = None) -> LocalHomeConfig:
    config_path = Path(path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)

    if not config_path.exists():
        hint = (
            f"Copy config/config.example.yaml to config/config.yaml and edit it, "
            f"or set {CONFIG_ENV_VAR} to point at your own config file."
            if config_path == DEFAULT_CONFIG_PATH
            else ""
        )
        raise FileNotFoundError(f"Config file not found: {config_path}. {hint}".strip())

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    web = raw.get("web", {})
    integrations = [
        Integration(
            kind=entry["kind"],
            type=entry["type"],
            options={k: v for k, v in entry.items() if k not in ("kind", "type")},
        )
        for entry in raw.get("integrations", [])
    ]

    notifications = raw.get("notifications")
    notifier = (
        Integration(
            kind="notifier",
            type=notifications["type"],
            options={k: v for k, v in notifications.items() if k != "type"},
        )
        if notifications
        else None
    )

    return LocalHomeConfig(
        path=config_path,
        web_host=web.get("host", "0.0.0.0"),
        web_port=int(web.get("port", 5000)),
        web_language=web.get("language", "en"),
        integrations=integrations,
        notifier=notifier,
        raw=raw,
    )
