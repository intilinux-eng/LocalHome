"""SyncPoller/AsyncPoller are the one piece every driver goes through, so a
bug here affects every sensor/switch, and only shows up after real uptime -
a device that's fine for the first hour but wedges after a transient
failure looks identical to "it works" in a five-minute manual check. These
tests compress many poll cycles (and the exact boot-race that used to kill
AsyncPoller for good, see poller.py) into fast, deterministic runs instead
of actually leaving hardware running for days to find out.
"""
import asyncio

from localhome.core.poller import AsyncPoller, SyncPoller


class FlakySyncDriver:
    def __init__(self, fail_on: set[int] = frozenset()):
        self.poll_interval_seconds = 0  # unused - tests call poll_once() directly, never _loop()
        self.fail_on = fail_on
        self.calls = 0

    def read(self):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError(f"simulated failure on call {self.calls}")
        return {"ok": True, "temperature_c": float(self.calls)}


def test_sync_poller_caches_a_failed_reading_instead_of_raising():
    poller = SyncPoller(FlakySyncDriver(fail_on={1}))
    poller.poll_once()
    assert poller.get()["ok"] is False


def test_sync_poller_recovers_on_the_next_cycle_after_a_failure():
    poller = SyncPoller(FlakySyncDriver(fail_on={2}))
    poller.poll_once()  # ok
    poller.poll_once()  # raises inside the driver, caught
    assert poller.get()["ok"] is False
    poller.poll_once()  # healthy again
    assert poller.get()["ok"] is True


def test_sync_poller_survives_hundreds_of_cycles_with_occasional_failures():
    driver = FlakySyncDriver(fail_on={n for n in range(1, 501) if n % 37 == 0})
    poller = SyncPoller(driver)

    for _ in range(500):
        poller.poll_once()  # must never raise, whatever the driver does

    assert driver.calls == 500
    assert poller.get()["ok"] is True  # call 500 isn't one of the failing ones


class FlakyAsyncDriver:
    def __init__(self, setup_fails_times: int = 0, read_fails_on: set[int] = frozenset()):
        self.poll_interval_seconds = 0
        self.setup_calls = 0
        self.setup_fails_times = setup_fails_times
        self.read_calls = 0
        self.read_fails_on = read_fails_on

    async def async_setup(self):
        self.setup_calls += 1
        if self.setup_calls <= self.setup_fails_times:
            raise RuntimeError("broker not reachable yet")

    async def async_read(self):
        self.read_calls += 1
        if self.read_calls in self.read_fails_on:
            raise RuntimeError(f"simulated failure on read {self.read_calls}")
        return {"ok": True, "is_on": True}


def test_async_poller_setup_retries_instead_of_dying_forever():
    # The bug this guards: a one-shot async_setup() failure (e.g. the MQTT
    # broker or the network itself not up yet when LocalHome starts) used
    # to disable this poller for the rest of the process's life - no
    # retry, ever, since _run() returned right after logging the error.
    driver = FlakyAsyncDriver(setup_fails_times=2)
    poller = AsyncPoller(driver)

    assert asyncio.run(poller.setup_once()) is False
    assert poller.get()["ok"] is False
    assert asyncio.run(poller.setup_once()) is False
    assert asyncio.run(poller.setup_once()) is True  # third attempt succeeds
    assert driver.setup_calls == 3


def test_async_poller_read_survives_a_failure_and_recovers():
    driver = FlakyAsyncDriver(read_fails_on={2})
    poller = AsyncPoller(driver)
    assert asyncio.run(poller.setup_once()) is True

    asyncio.run(poller.poll_once())
    assert poller.get()["ok"] is True
    asyncio.run(poller.poll_once())  # raises inside the driver, caught
    assert poller.get()["ok"] is False
    asyncio.run(poller.poll_once())
    assert poller.get()["ok"] is True


# --------------------------------------------------------- last_update_ts ----
# A driver that optionally exposes `last_update_ts` (the MQTT drivers do,
# from the underlying message's own arrival time - see
# drivers/mqtt/sensor.py/switch.py/number.py) gets its freshness reported
# from *that*, not from whenever poll_once() happened to run. Without
# this, re-polling a push-based MQTT cache every poll_interval_seconds
# made an untouched reading look freshly updated every single cycle, even
# hours after the device itself last said anything - exactly backwards
# for a "how stale is this really" display.

class DriverWithOwnTimestamp:
    def __init__(self, ts):
        self.poll_interval_seconds = 0
        self._ts = ts

    def read(self):
        return {"ok": True, "value": 1}

    @property
    def last_update_ts(self):
        return self._ts


def test_sync_poller_reports_freshness_from_the_drivers_own_timestamp(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.core.poller.time.time", lambda: now["t"])
    driver = DriverWithOwnTimestamp(ts=now["t"] - 500)  # the underlying reading is already 500s old
    poller = SyncPoller(driver)

    poller.poll_once()
    now["t"] += 10  # 10 more seconds pass before the dashboard asks

    assert poller.get()["seconds_since_update"] == 510


def test_sync_poller_falls_back_to_poll_time_without_a_last_update_ts(monkeypatch):
    # Regression guard: every non-MQTT driver (Tuya, Meross, simulated,
    # ...) has no last_update_ts at all and must keep behaving exactly
    # as before - freshness is simply "when did poll_once() last run".
    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.core.poller.time.time", lambda: now["t"])
    poller = SyncPoller(FlakySyncDriver())

    poller.poll_once()
    now["t"] += 10

    assert poller.get()["seconds_since_update"] == 10


def test_async_poller_reports_freshness_from_the_drivers_own_timestamp(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr("localhome.core.poller.time.time", lambda: now["t"])

    class AsyncDriverWithOwnTimestamp:
        poll_interval_seconds = 0

        async def async_read(self):
            return {"ok": True, "value": 1}

        @property
        def last_update_ts(self):
            return now["t"] - 500

    poller = AsyncPoller(AsyncDriverWithOwnTimestamp())
    asyncio.run(poller.poll_once())
    now["t"] += 10

    assert poller.get()["seconds_since_update"] == 510



class _FlakyDriver:
    poll_interval_seconds = 20
    offline_backoff_max_seconds = 120

    def __init__(self):
        self.ok = False

    def read(self):
        if not self.ok:
            raise RuntimeError("no RPC reply - device is probably offline")
        return {"ok": True}


def test_a_device_that_stays_offline_is_asked_less_and_less_often():
    driver = _FlakyDriver()
    poller = SyncPoller(driver)

    delays = []
    for _ in range(6):
        poller.poll_once()
        delays.append(poller.next_delay())

    assert delays == [40, 80, 120, 120, 120, 120]
    driver.ok = True
    poller.poll_once()
    assert poller.next_delay() == 20  # back to normal as soon as it answers


def test_drivers_without_the_option_keep_their_interval():
    driver = _FlakyDriver()
    driver.offline_backoff_max_seconds = None
    poller = SyncPoller(driver)
    for _ in range(5):
        poller.poll_once()
    assert poller.next_delay() == 20


def test_a_successful_command_ends_the_back_off_at_once():
    import threading
    import time as _time

    driver = _FlakyDriver()
    driver.poll_interval_seconds = 5
    poller = SyncPoller(driver)
    for _ in range(4):
        poller.poll_once()
    assert poller.next_delay() == 80  # slowed down while it was off

    reads = []
    original = driver.read
    driver.read = lambda: (reads.append(1), original())[1]
    driver.ok = True
    threading.Thread(target=poller._loop, daemon=True).start()
    _time.sleep(0.2)
    reads.clear()

    poller.wake()  # e.g. the dashboard just switched it on
    _time.sleep(0.3)

    assert reads, "not re-read right after the wake"
    assert poller.next_delay() == 5
    assert poller.get()["ok"] is True
