#!/usr/bin/env python3
"""Convenience entry point: `python run.py` starts the web dashboard
without requiring `pip install -e .` first. Equivalent to running
`localhome` after an editable install."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from localhome.web.app import main

if __name__ == "__main__":
    main()
