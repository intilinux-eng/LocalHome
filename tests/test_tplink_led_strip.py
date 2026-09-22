"""The TP-Link drivers open a real python-kasa connection on every single
call - a poll every 20s, per driver, forever. A kasa Device holds a live
aiohttp session that the event loop ending does *not* tear down, so
"forgot to disconnect" here isn't a tidiness issue but a resource leak
that compounds: it leaked roughly one OS thread plus one aiohttp session per
read, which on this project's real four-driver setup (two strips x
switch+brightness) was ~17k leaked connections a day and took the
process past 4.5GB of RAM in nine hours before it was found.

Nothing about that failure is visible in a normal functional test - the
readings were all correct the whole time - so these tests assert the
lifecycle itself: every path that opens a device must also close it,
including when the body raises.
"""
import asyncio

import pytest

# python-kasa is the optional "tplink" extra (GPL - see
# THIRD_PARTY_NOTICES.md): an install without it has no driver to test.
pytest.importorskip("kasa")

from localhome.drivers.tplink import led_strip  # noqa: E402


class FakeLight:
    def __init__(self, brightness=42):
        self.brightness = brightness
        self.set_calls = []

    async def set_brightness(self, value):
        self.set_calls.append(value)


class FakeDevice:
    """Records its own connect/disconnect lifecycle so a test can assert
    the driver closed it, which is the whole point here."""

    def __init__(self, with_light=True):
        self.is_on = True
        self.disconnect_calls = 0
        self.update_calls = 0
        self.power_calls = []
        self.modules = {led_strip.Module.Light: FakeLight()} if with_light else {}

    async def update(self):
        self.update_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1

    async def turn_on(self):
        self.power_calls.append("on")

    async def turn_off(self):
        self.power_calls.append("off")


@pytest.fixture
def fake_kasa(monkeypatch):
    """Stands in for Discover.discover_single, handing back one FakeDevice
    per call the way the real one hands back one connection per call."""
    created = []

    async def _discover_single(host, username=None, password=None, timeout=None):
        device = FakeDevice()
        created.append(device)
        return device

    monkeypatch.setattr(led_strip.Discover, "discover_single", _discover_single)
    return created


def _switch():
    return led_strip.TplinkLedStripDriver(name="Strip", host="10.0.0.5", username="u", password="p")


def _brightness():
    return led_strip.TplinkLedStripBrightnessDriver(name="Strip Brightness", host="10.0.0.5", username="u", password="p")


def test_read_closes_the_device(fake_kasa):
    reading = _switch().read()

    assert reading["ok"] is True
    assert len(fake_kasa) == 1
    assert fake_kasa[0].disconnect_calls == 1


def test_every_repeated_read_closes_its_own_device(fake_kasa):
    # The leak this guards was invisible in a single call - it only showed
    # up as an ever-growing pile across thousands of polls.
    driver = _switch()
    for _ in range(25):
        driver.read()

    assert len(fake_kasa) == 25
    assert all(device.disconnect_calls == 1 for device in fake_kasa)


def test_turn_on_and_off_close_the_device(fake_kasa):
    driver = _switch()
    driver.turn_on()
    driver.turn_off()

    assert [call for device in fake_kasa for call in device.power_calls] == ["on", "off"]
    assert all(device.disconnect_calls == 1 for device in fake_kasa)


def test_brightness_read_and_set_close_the_device(fake_kasa):
    driver = _brightness()
    reading = driver.read()
    driver.set_value(60)

    assert reading["value"] == 42
    assert fake_kasa[-1].modules[led_strip.Module.Light].set_calls == [60]
    assert all(device.disconnect_calls == 1 for device in fake_kasa)


def test_device_is_closed_even_when_the_call_fails(monkeypatch):
    # The failure path matters more than the happy one: this project's own
    # strips were answering 403 for a while, and a driver that only closes
    # on success turns an already-broken device into a memory leak too.
    device = FakeDevice(with_light=False)

    async def _discover_single(host, username=None, password=None, timeout=None):
        return device

    monkeypatch.setattr(led_strip.Discover, "discover_single", _discover_single)

    with pytest.raises(RuntimeError, match="no dimmable light module"):
        _brightness().read()

    assert device.disconnect_calls == 1


def test_device_is_closed_even_when_update_raises(monkeypatch):
    device = FakeDevice()

    async def _boom():
        raise RuntimeError("device unreachable")

    device.update = _boom

    async def _discover_single(host, username=None, password=None, timeout=None):
        return device

    monkeypatch.setattr(led_strip.Discover, "discover_single", _discover_single)

    with pytest.raises(RuntimeError, match="device unreachable"):
        _switch().read()

    assert device.disconnect_calls == 1


def test_context_manager_yields_an_updated_device(fake_kasa):
    async def _use():
        async with led_strip._device("10.0.0.5", "u", "p") as dev:
            assert dev.update_calls == 1  # callers get a device that already has fresh state
            assert dev.disconnect_calls == 0  # ...and that's still open while they use it
            return dev

    device = asyncio.run(_use())
    assert device.disconnect_calls == 1
