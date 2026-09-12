from localhome.drivers.mqtt.paths import extract_path


def test_flat_key():
    assert extract_path({"temperature": 21.5}, "temperature") == 21.5


def test_nested_dotted_path():
    payload = {"ENERGY": {"Power": 12.3, "Voltage": 230}}
    assert extract_path(payload, "ENERGY.Power") == 12.3
    assert extract_path(payload, "ENERGY.Voltage") == 230


def test_missing_key_returns_none():
    assert extract_path({"ENERGY": {"Power": 12.3}}, "ENERGY.Current") is None
    assert extract_path({}, "missing") is None


def test_path_into_a_non_dict_returns_none():
    assert extract_path({"ENERGY": 5}, "ENERGY.Power") is None


def test_traverses_into_a_key_that_itself_contains_a_colon():
    # Shelly-style keys like "switch:0" are a single JSON key, not two path
    # segments - only "." splits a path, so this still resolves correctly.
    payload = {"switch:0": {"apower": 42}}
    assert extract_path(payload, "switch:0.apower") == 42


def test_empty_path_returns_the_whole_payload():
    # The convention a non-JSON payload needs (e.g. Shelly Gen1's bare
    # "on"/"off" string) - see drivers/mqtt/base.py.
    assert extract_path("on", "") == "on"
    assert extract_path({"a": 1}, "") == {"a": 1}
