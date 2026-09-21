"""Unit test for services/self_restart.py - never actually calls
os.execv (that would replace the pytest process itself), just confirms
schedule_restart() hands off to a background thread that calls it with
the expected re-exec arguments.
"""
import sys

import pytest

from localhome.services import self_restart


def test_is_supported_is_false_on_windows(monkeypatch):
    monkeypatch.setattr(self_restart.sys, "platform", "win32")
    assert self_restart.is_supported() is False


def test_is_supported_is_true_on_linux(monkeypatch):
    monkeypatch.setattr(self_restart.sys, "platform", "linux")
    assert self_restart.is_supported() is True


def test_schedule_restart_calls_execv_with_the_current_interpreter_and_argv(monkeypatch):
    monkeypatch.setattr(self_restart.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(self_restart.os, "execv", lambda path, args: calls.append((path, args)))

    original_thread_cls = self_restart.threading.Thread
    started_threads = []

    def capture_thread(*args, **kwargs):
        thread = original_thread_cls(*args, **kwargs)
        started_threads.append(thread)
        return thread

    monkeypatch.setattr(self_restart.threading, "Thread", capture_thread)

    self_restart.schedule_restart()
    started_threads[0].join(timeout=2)

    assert calls == [(sys.executable, [sys.executable] + sys.argv)]



def test_restart_releases_werkzeugs_inherited_server_socket(monkeypatch):
    # werkzeug marks its listening socket inheritable and records it in
    # WERKZEUG_SERVER_FD; surviving the exec, it kept the port bound and
    # the relaunched process failed with "Address already in use".
    monkeypatch.setenv("WERKZEUG_SERVER_FD", "7")
    monkeypatch.setattr(self_restart.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(self_restart.os, "set_inheritable", lambda fd, flag: calls.append(("set_inheritable", fd, flag)))
    monkeypatch.setattr(self_restart.os, "execv", lambda path, args: calls.append(("execv",)))

    self_restart._restart()

    assert calls == [("set_inheritable", 7, False), ("execv",)]
    assert "WERKZEUG_SERVER_FD" not in self_restart.os.environ


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fds; restart is disabled on Windows anyway")
def test_released_socket_is_really_not_inherited(monkeypatch):
    import socket

    server = socket.socket()
    try:
        server.set_inheritable(True)
        monkeypatch.setenv("WERKZEUG_SERVER_FD", str(server.fileno()))
        self_restart._release_server_socket()
        assert server.get_inheritable() is False
    finally:
        server.close()
