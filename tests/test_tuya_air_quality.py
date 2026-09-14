"""Exercises TuyaAirQualityDriver.read() against a fake tinytuya device -
no real hardware, no network. Found while auditing real collected history
data for correctness: a genuine device occasionally returns a stale dps
snapshot with real keys present but every value zeroed - see read()'s
all-zero check in air_quality.py.
"""
import pytest

from localhome.drivers.tuya.air_quality import TuyaAirQualityDriver


class FakeDevice:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


@pytest.fixture
def driver(monkeypatch):
    d = TuyaAirQualityDriver({"name": "Air Sensor", "id": "x", "key": "y", "ip": "1.2.3.4"})
    monkeypatch.setattr(
        "localhome.drivers.tuya.air_quality.make_device",
        lambda entry, version=None: d._fake_device,
    )
    return d


def test_read_maps_dps_ids_to_named_fields(driver):
    driver._fake_device = FakeDevice({"dps": {"2": 215, "3": 47}})  # 21.5C, 47%
    reading = driver.read()
    assert reading == {"ok": True, "name": "Air Sensor", "temperature_c": 21.5, "humidity_pct": 47}


def test_read_raises_when_dps_is_missing(driver):
    driver._fake_device = FakeDevice({"Error": "Network Error: Device Unreachable"})
    with pytest.raises(RuntimeError, match="Unreachable"):
        driver.read()


def test_read_raises_when_dps_is_empty(driver):
    driver._fake_device = FakeDevice({"dps": {}})
    with pytest.raises(RuntimeError):
        driver.read()


def test_read_rejects_an_all_zero_snapshot_as_a_bad_read(driver):
    # A real device has been observed returning exactly this: dps present
    # with both fields, but zeroed - not a plausible simultaneous 0C/0%RH
    # reading, so treated as a failed read rather than recorded as data.
    driver._fake_device = FakeDevice({"dps": {"2": 0, "3": 0}})
    with pytest.raises(RuntimeError, match="all-zero"):
        driver.read()


def test_read_keeps_a_genuine_single_zero_field(driver):
    # Only the *combination* of both being zero is treated as suspect -
    # a real 0C reading with plausible humidity must still go through.
    driver._fake_device = FakeDevice({"dps": {"2": 0, "3": 55}})
    reading = driver.read()
    assert reading == {"ok": True, "name": "Air Sensor", "temperature_c": 0.0, "humidity_pct": 55}
