"""Tiny helpers so drivers/services don't each repeat the same
open()/json.load()/json.dump() boilerplate - and, for saving, don't each
independently need to get crash-safety right.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path: str, data: Any) -> None:
    """Write `data` as JSON to `path` without ever leaving a
    partially-written or truncated file behind if the process is
    interrupted mid-write (a power cut, a kill -9, a crash) - the naive
    `open(path, "w")` + `json.dump()` pattern truncates the file before
    writing the new content, so anything that interrupts that write
    leaves neither the old nor the new content, just a corrupt file that
    fails to parse on the next read.

    Instead this writes to a temporary file in the same directory,
    fsyncs it so the write actually reaches disk rather than sitting in
    a page cache buffer, and only then atomically renames it onto the
    real path - `os.replace()` is atomic on both POSIX and Windows, so a
    reader (including this same process on its next restart) only ever
    sees the complete old file or the complete new one, never a partial
    write. Used for anything this project needs to survive a restart
    with (positions.json, schedules.json) - see services/position_control.py
    and services/thermostat.py.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
