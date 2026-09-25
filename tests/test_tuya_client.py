"""The shared Tuya LAN layer: tinytuya reports failures as a payload, and
a device's address can change under it.

Both matter in a real installation: five covers once sat on the dashboard
reading "unknown" for days because a stale local key came back as data,
and a router handing out a new address is the other way a device "stops
working" while being perfectly fine.
"""
import json

import pytest

from localhome.drivers.tuya.client import (
    TuyaAddressBook,
    TuyaConnection,
    TuyaDeviceError,
    load_devices,
    require_dps,
)

ENTRY = {"id": "dev1", "name": "Kitchen Blind", "key": "k", "ip": "192.168.1.10"}


def test_dps_comes_back_as_it_is():
    assert require_dps({"dps": {"1": "open"}}, "Blind") == {"1": "open"}


def test_a_stale_local_key_says_what_to_do():
    with pytest.raises(TuyaDeviceError) as exc:
        require_dps({"Error": "Check device key or version", "Err": "914"}, "Blind")

    assert "Blind" in str(exc.value) and "tinytuya wizard" in str(exc.value)
    assert not exc.value.unreachable  # the device answered; its key is wrong


def test_no_answer_is_flagged_as_unreachable():
    with pytest.raises(TuyaDeviceError) as exc:
        require_dps({"Error": "Network Error: Device Unreachable", "Err": "905"}, "Blind")

    assert exc.value.unreachable
    assert "IP changed" in str(exc.value)


def test_an_empty_response_is_an_error_not_an_empty_reading():
    with pytest.raises(TuyaDeviceError):
        require_dps({}, "Blind")


def test_a_device_without_an_address_is_left_out_and_logged(tmp_path, caplog):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps([ENTRY, {"id": "dev2", "name": "Spare", "key": "k"}]), encoding="utf-8")

    devices = load_devices(str(path))

    assert [d["id"] for d in devices] == ["dev1"]
    assert "Spare" in caplog.text and "no 'ip'" in caplog.text


# --------------------------------------------------------- address book --

def test_a_moved_device_is_found_and_remembered(tmp_path):
    cache = tmp_path / "tuya_addresses.json"
    book = TuyaAddressBook(str(cache), finder=lambda device_id: "192.168.1.55")

    assert book.ip_for(ENTRY) == "192.168.1.10"  # what devices.json says
    assert book.rediscover(ENTRY) == "192.168.1.55"
    assert book.ip_for(ENTRY) == "192.168.1.55"
    assert json.loads(cache.read_text(encoding="utf-8"))["dev1"]["ip"] == "192.168.1.55"

    # ...and it's still there for the next start, without asking again.
    again = TuyaAddressBook(str(cache), finder=lambda device_id: pytest.fail("should not scan"))
    assert again.ip_for(ENTRY) == "192.168.1.55"


def test_the_same_address_is_not_reported_as_a_move(tmp_path):
    book = TuyaAddressBook(str(tmp_path / "a.json"), finder=lambda device_id: "192.168.1.10")
    assert book.rediscover(ENTRY) is None


def test_scanning_is_throttled(tmp_path):
    calls = []

    def finder(device_id):
        calls.append(device_id)
        return None

    book = TuyaAddressBook(str(tmp_path / "a.json"), finder=finder, retry_after_seconds=300)
    book.rediscover(ENTRY, now=1000)
    book.rediscover(ENTRY, now=1100)  # a device that's simply off: don't scan again
    book.rediscover(ENTRY, now=1400)

    assert len(calls) == 2


def test_a_finder_that_blows_up_doesnt_break_the_poll(tmp_path):
    def finder(device_id):
        raise OSError("no network")

    assert TuyaAddressBook(str(tmp_path / "a.json"), finder=finder).rediscover(ENTRY) is None


# ----------------------------------------------------------- connection --

class FakeTinytuya:
    """Answers only at `good_ip`; anywhere else looks unreachable."""

    def __init__(self, good_ip, dps=None):
        self.good_ip = good_ip
        self.dps = dps or {"1": "open"}
        self.asked = []

    def device(self, device_id, ip, key, version=3.4):
        self.asked.append(ip)
        fake = type("Dev", (), {})()
        fake.set_socketTimeout = lambda *_: None
        fake.status = lambda: ({"dps": self.dps} if ip == self.good_ip
                               else {"Error": "Network Error: Device Unreachable", "Err": "905"})
        fake.set_value = lambda dp, value: ({"dps": self.dps} if ip == self.good_ip
                                            else {"Error": "Network Error: Device Unreachable", "Err": "905"})
        return fake


@pytest.fixture
def fake_tinytuya(monkeypatch):
    def install(good_ip):
        fake = FakeTinytuya(good_ip)
        monkeypatch.setattr("localhome.drivers.tuya.client.tinytuya.Device", fake.device)
        return fake
    return install


def test_a_device_that_moved_is_looked_up_and_the_call_retried(tmp_path, fake_tinytuya):
    fake = fake_tinytuya("192.168.1.55")
    book = TuyaAddressBook(str(tmp_path / "a.json"), finder=lambda device_id: "192.168.1.55")

    assert TuyaConnection(ENTRY, addresses=book).status() == {"1": "open"}

    assert fake.asked == ["192.168.1.10", "192.168.1.55"]  # old address, then the new one
    assert book.ip_for(ENTRY) == "192.168.1.55"  # next call goes straight there


def test_a_device_that_is_simply_off_reports_the_failure(tmp_path, fake_tinytuya):
    fake_tinytuya("192.168.1.99")
    book = TuyaAddressBook(str(tmp_path / "a.json"), finder=lambda device_id: None)

    with pytest.raises(TuyaDeviceError) as exc:
        TuyaConnection(ENTRY, addresses=book).status()

    assert exc.value.unreachable


def test_a_wrong_key_is_not_chased_around_the_network(tmp_path, monkeypatch):
    """A key error means the device is right there - scanning for it
    would only add a delay to every single poll."""
    def device(device_id, ip, key, version=3.4):
        fake = type("Dev", (), {})()
        fake.set_socketTimeout = lambda *_: None
        fake.status = lambda: {"Error": "Check device key or version", "Err": "914"}
        return fake

    monkeypatch.setattr("localhome.drivers.tuya.client.tinytuya.Device", device)
    book = TuyaAddressBook(str(tmp_path / "a.json"), finder=lambda device_id: pytest.fail("should not scan"))

    with pytest.raises(TuyaDeviceError, match="wizard"):
        TuyaConnection(ENTRY, addresses=book).status()
