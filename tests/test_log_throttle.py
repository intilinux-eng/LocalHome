"""A device that stays unreachable is logged in full once, then briefly
every interval - not a traceback on every control-loop tick."""
import logging

from localhome.services import log_throttle
from localhome.services.log_throttle import FailureLog


def _fail(log, key):
    try:
        raise ConnectionError("broker down")
    except ConnectionError:
        log.failed(key, "Failed to close %r", key)


def test_first_failure_in_full_then_throttled_then_recovery(caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(log_throttle.time, "monotonic", lambda: now[0])
    log = FailureLog(logging.getLogger("t"), interval=600)
    caplog.set_level(logging.INFO)

    for _ in range(30):  # 30 ticks within the interval
        _fail(log, "Bagni Valve")
        now[0] += 10
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is not None

    now[0] += 600
    _fail(log, "Bagni Valve")
    assert len(caplog.records) == 2 and "still failing" in caplog.records[1].getMessage()
    assert caplog.records[1].exc_info is None

    log.succeeded("Bagni Valve")
    log.succeeded("Bagni Valve")  # only once
    assert [r.getMessage() for r in caplog.records[2:]] == ["Bagni Valve is working again"]


def test_devices_are_throttled_separately(caplog):
    log = FailureLog(logging.getLogger("t"))
    _fail(log, "A")
    _fail(log, "B")
    _fail(log, "A")
    assert len(caplog.records) == 2
