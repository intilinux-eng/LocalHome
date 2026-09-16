"""Unit tests for services/mqtt_broker_config.py - reading/writing
config/mqtt_broker.json from the climate admin panel instead of always
hand-editing it (see docs/integrations/mqtt.md).
"""
import json

import pytest

from localhome.services.mqtt_broker_config import get_broker, set_broker


def test_get_broker_defaults_when_no_file_exists_yet(tmp_path):
    result = get_broker(str(tmp_path / "mqtt_broker.json"))
    assert result == {"host": None, "port": 1883, "username": None, "has_password": False}


def test_set_and_get_round_trip_without_a_password(tmp_path):
    path = str(tmp_path / "mqtt_broker.json")
    set_broker(path, "192.168.1.21", 1883, "", None, change_password=False)

    result = get_broker(path)

    assert result == {"host": "192.168.1.21", "port": 1883, "username": None, "has_password": False}


def test_set_broker_with_a_new_password_never_reads_back_the_value(tmp_path):
    path = str(tmp_path / "mqtt_broker.json")
    set_broker(path, "192.168.1.21", 1883, "mqtt_user", "s3cret", change_password=True)

    result = get_broker(path)

    assert result["has_password"] is True
    assert "password" not in result
    # The raw file does have it, though - get_broker() only redacts what
    # goes back to the browser, it's not encryption at rest.
    assert json.loads(open(path, encoding="utf-8").read())["password"] == "s3cret"


def test_leaving_change_password_false_keeps_the_existing_password(tmp_path):
    path = str(tmp_path / "mqtt_broker.json")
    set_broker(path, "192.168.1.21", 1883, "mqtt_user", "s3cret", change_password=True)

    # Editing host only, password field left blank in the form.
    set_broker(path, "192.168.1.22", 1883, "mqtt_user", None, change_password=False)

    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["host"] == "192.168.1.22"
    assert raw["password"] == "s3cret"


def test_change_password_true_with_a_blank_value_clears_it(tmp_path):
    path = str(tmp_path / "mqtt_broker.json")
    set_broker(path, "192.168.1.21", 1883, "mqtt_user", "s3cret", change_password=True)

    set_broker(path, "192.168.1.21", 1883, "mqtt_user", "", change_password=True)

    assert get_broker(path)["has_password"] is False


def test_rejects_a_missing_host(tmp_path):
    with pytest.raises(ValueError, match="host"):
        set_broker(str(tmp_path / "mqtt_broker.json"), "", 1883, None, None, change_password=False)


@pytest.mark.parametrize("port", [0, -1, 70000, "1883", True])
def test_rejects_an_invalid_port(tmp_path, port):
    with pytest.raises(ValueError, match="port"):
        set_broker(str(tmp_path / "mqtt_broker.json"), "192.168.1.21", port, None, None, change_password=False)


def test_an_invalid_save_never_writes_a_file(tmp_path):
    path = tmp_path / "mqtt_broker.json"
    with pytest.raises(ValueError):
        set_broker(str(path), "", 1883, None, None, change_password=False)
    assert not path.exists()
