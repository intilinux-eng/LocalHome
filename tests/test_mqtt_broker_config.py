"""Unit tests for services/mqtt_broker_config.py - what the admin panel
shows of config/mqtt_broker.json, a file written by hand (see
docs/integrations/mqtt.md).
"""
import json

from localhome.services.mqtt_broker_config import get_broker


def test_get_broker_defaults_when_no_file_exists_yet(tmp_path):
    result = get_broker(str(tmp_path / "mqtt_broker.json"))
    assert result == {"host": None, "port": 1883, "username": None, "has_password": False}


def test_get_broker_reads_a_file_without_a_password(tmp_path):
    path = tmp_path / "mqtt_broker.json"
    path.write_text(json.dumps({"host": "localhost", "port": 1883}), encoding="utf-8")
    assert get_broker(str(path)) == {"host": "localhost", "port": 1883, "username": None, "has_password": False}


def test_a_password_is_never_read_back(tmp_path):
    path = tmp_path / "mqtt_broker.json"
    path.write_text(json.dumps({"host": "localhost", "username": "mqtt_user", "password": "s3cret"}), encoding="utf-8")
    result = get_broker(str(path))
    assert result["has_password"] is True and result["username"] == "mqtt_user"
    assert "s3cret" not in json.dumps(result)


def test_a_broken_file_reads_as_none(tmp_path):
    path = tmp_path / "mqtt_broker.json"
    path.write_text("{not json", encoding="utf-8")
    assert get_broker(str(path))["host"] is None
