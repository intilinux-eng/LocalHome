import threading

from localhome.services.device_overrides import DeviceOverrideStore


def test_get_returns_empty_dict_for_an_unknown_device(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    assert store.get("Nonexistent") == {}


def test_set_and_get_round_trip(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True, order=2)
    assert store.get("Desk Lamp") == {"hidden": True, "order": 2}


def test_setting_a_field_to_none_clears_it(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True, order=2)
    store.set_override("Desk Lamp", hidden=None)
    assert store.get("Desk Lamp") == {"order": 2}


def test_clearing_every_field_removes_the_device_entry_entirely(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True)
    store.set_override("Desk Lamp", hidden=None)
    assert store.get_all() == {}


def test_two_separate_set_override_calls_on_different_fields_both_stick(tmp_path):
    # Guards the read-modify-write cycle: a naive "load once, save the
    # in-memory copy" implementation would lose the first call's field
    # when the second one saves.
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True)
    store.set_override("Desk Lamp", order=5)
    assert store.get("Desk Lamp") == {"hidden": True, "order": 5}


def test_set_order_writes_a_dense_index_for_every_name(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_order(["Desk Lamp", "Living Room Plug", "Kitchen Sensor"])
    assert store.get("Desk Lamp")["order"] == 0
    assert store.get("Living Room Plug")["order"] == 1
    assert store.get("Kitchen Sensor")["order"] == 2


def test_set_order_preserves_other_fields_already_set(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True)
    store.set_order(["Desk Lamp"])
    assert store.get("Desk Lamp") == {"hidden": True, "order": 0}


def test_falls_back_to_empty_on_a_corrupt_file(tmp_path):
    path = tmp_path / "device_overrides.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = DeviceOverrideStore(str(path))
    assert store.get_all() == {}


def test_reset_removes_a_device_entirely(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    store.set_override("Desk Lamp", hidden=True, order=1)
    store.reset("Desk Lamp")
    assert store.get("Desk Lamp") == {}


def test_concurrent_writes_to_different_devices_dont_clobber_each_other(tmp_path):
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    errors = []

    def writer(name, order):
        try:
            for _ in range(20):
                store.set_override(name, order=order)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"Device {i}", i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    all_devices = store.get_all()
    assert len(all_devices) == 8
    for i in range(8):
        assert all_devices[f"Device {i}"]["order"] == i
