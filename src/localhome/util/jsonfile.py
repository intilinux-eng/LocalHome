"""Tiny helper so drivers don't each repeat `json.load(open(path))`."""
from __future__ import annotations

import json
from typing import Any


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
