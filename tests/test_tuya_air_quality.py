"""Exercises TuyaAirQualityDriver.read() against a fake LAN connection -
no real hardware, no network. Found while auditing real collected history
data for correctness: a genuine device occasionally returns a stale dps
snapshot with real keys present but every value zeroed - see read()'s
all-zero check in air_quality.py.
"""
import pytest

from localhome.drivers.tuya.air_quality import TuyaAirQualityDriver
from localhome.drivers.tuya.client import TuyaDeviceError


class FakeConnection:
    """Stands in for TuyaConnection: hands back dps, or the error it
    would have raised (see client.require_dps)."""

    def __init__(self):
        self.dps = {}
        self.error = None

    def status(self):
        if self.error:
            raise self.error
        return self.dps


@pytest.fixture
def driver():
    d = TuyaAirQualityDriver({"name": "Air Sensor", "id": "x", "key": "y", "ip": "1.2.3.4"})
    d._connection = FakeConnection()
    return d


def test_read_maps_dps_ids_to_named_fields(driver):
    driver._connection.dps = {"2": 215, "3": 47}  # 21.5C, 47%
    reading = driver.read()
    assert reading == {"ok": True, "name": "Air Sensor", "temperature_c": 21.5, "humidity_pct": 47}


def test_a_device_that_did_not_answer_is_a_failure(driver):
    driver._connection.error = TuyaDeviceError("Air Sensor: Network Error: Device Unreachable", code="905")
    with pytest.raises(TuyaDeviceError, match="Unreachable"):
        driver.read()


def test_read_rejects_an_all_zero_snapshot_as_a_bad_read(driver):
    # A real device has been observed returning exactly this: dps present
    # with both fields, but zeroed - not a plausible simultaneous 0C/0%RH
    # indoors, so it's a bad read to retry, not a measurement to record.
    driver._connection.dps = {"2": 0, "3": 0}
    with pytest.raises(RuntimeError, match="all-zero"):
        driver.read()


def test_read_keeps_a_genuine_single_zero_field(driver):
    driver._connection.dps = {"2": 0, "3": 41}  # 0.0C outdoors in winter is real
    assert driver.read()["temperature_c"] == 0
