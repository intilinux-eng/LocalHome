import pytest

from localhome.core.registry import create, known_drivers, register_driver


def test_register_and_create_round_trip():
    @register_driver("test_kind", "test_type_single")
    def _create_one(options):
        return {"received": options}

    result = create("test_kind", "test_type_single", {"a": 1})

    assert result == [{"received": {"a": 1}}]


def test_factory_returning_a_list_is_passed_through():
    @register_driver("test_kind", "test_type_list")
    def _create_many(options):
        return ["one", "two"]

    assert create("test_kind", "test_type_list", {}) == ["one", "two"]


def test_unknown_driver_raises_a_helpful_error():
    with pytest.raises(ValueError, match="No driver registered"):
        create("test_kind", "does_not_exist", {})


def test_a_driver_taken_out_says_so_and_what_to_do():
    with pytest.raises(ValueError, match="no longer has the Tuya air-quality sensor .*delete this entry"):
        create("climate", "tuya_air_quality", {"devices_file": "devices.json"})
    with pytest.raises(ValueError, match="no longer has the generic MQTT shutter"):
        create("cover", "mqtt_json", {"name": "Shutter"})
    # The same type stays for what's still there: a zone's sensor, a valve.
    assert {("climate", "mqtt_json"), ("switch", "mqtt_json")} <= set(known_drivers())


def test_duplicate_registration_is_rejected():
    @register_driver("test_kind", "test_type_dup")
    def _first(options):
        return None

    with pytest.raises(ValueError, match="Duplicate"):
        @register_driver("test_kind", "test_type_dup")
        def _second(options):
            return None
