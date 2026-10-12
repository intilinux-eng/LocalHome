"""Restarts this LocalHome process in place, re-exec'ing the same
interpreter and arguments it was started with - the same idiom
uvicorn/gunicorn use for --reload. Exists so the climate admin panel
can offer a "Restart LocalHome" button instead of every saved change
(a new zone mode, a broker setting, an extra switch) requiring someone
to SSH in and restart the process by hand before it takes effect.

On Linux (the deployment target) this is a real execve() syscall. Python
sockets are non-inheritable across exec by default (PEP 446), so the
listening socket closes with the old image. The exception was werkzeug's
development server (Flask's app.run(), used before Waitress): it marks
its socket inheritable on purpose and records it in WERKZEUG_SERVER_FD,
so the relaunched process found port 5000 still bound ("Address already
in use", seen on the real board). _release_server_socket() still undoes
that, for anyone running the app under that server.

On Windows this is considerably worse than "a small window that might
race" - confirmed empirically, not just in theory: the C runtime's
execv is spawn-the-child-then-wait-then-exit, not a true replace, so
the old process keeps the port bound for however long the new one
takes to start. Observed leaving two (in one case three, chained)
python.exe processes alive at once, requiring a manual
`Stop-Process`/kill to untangle - worse than the plain manual restart
this feature replaces. Don't rely on the button while developing on
Windows; it's here for the real target.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from localhome import runtime


def is_supported() -> bool:
    """False on Windows - see the module docstring above. The route
    calling schedule_restart() must check this first and refuse
    cleanly instead of leaving two processes fighting over one port."""
    return not sys.platform.startswith("win")


def _release_server_socket() -> None:
    """Keeps werkzeug's listening socket from surviving the exec - see
    the module docstring."""
    fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
    if fd is None:
        return
    try:
        os.set_inheritable(int(fd), False)
    except (OSError, ValueError):
        pass  # already closed, or not a number - nothing left to release


def _restart() -> None:
    # A short delay so the HTTP response confirming the request was
    # received can flush back to the browser before the process
    # actually turns over.
    time.sleep(1.0)
    _release_server_socket()
    argv = runtime.restart_argv()
    os.execv(argv[0], argv)


def schedule_restart() -> None:
    threading.Thread(target=_restart, daemon=True).start()
