"""Unit test for services/self_restart.py - never actually calls
os.execv (that would replace the pytest process itself), just confirms
schedule_restart() hands off to a background thread that calls it with
the expected re-exec arguments.
"""
import sys

from localhome.services import self_restart


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
