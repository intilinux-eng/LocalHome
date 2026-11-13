#!/usr/bin/env python3
"""Starts LocalHome for the Linux desktop: `python3 desktop/linux/localhome-desktop.py`
from the repository, /usr/bin/localhome-desktop once installed."""
import sys

from localhome_desktop.app import main

sys.exit(main())
