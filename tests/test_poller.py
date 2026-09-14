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
