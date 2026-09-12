"""get_connection() pooling logic - no real socket, MqttConnection itself
is replaced with a lightweight fake so this only tests the dict-keyed
reuse/dedup behavior."""
from __future__ import annotations

import localhome.drivers.mqtt.client as client_module


class _FakeConnectionHandle:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fresh_pool(monkeypatch):
    monkeypatch.setattr(client_module, "MqttConnection", _FakeConnectionHandle)
    monkeypatch.setattr(client_module, "_pool", {})


def test_identical_broker_details_share_one_connection(monkeypatch):
    _fresh_pool(monkeypatch)
    broker = {"host": "10.0.0.5", "port": 1883}

    a = client_module.get_connection(broker)
    b = client_module.get_connection(dict(broker))  # a different dict, same values

    assert a is b


def test_different_host_gets_a_different_connection(monkeypatch):
    _fresh_pool(monkeypatch)

    a = client_module.get_connection({"host": "10.0.0.5"})
    b = client_module.get_connection({"host": "10.0.0.6"})

    assert a is not b


def test_different_username_gets_a_different_connection(monkeypatch):
    _fresh_pool(monkeypatch)

    a = client_module.get_connection({"host": "10.0.0.5", "username": "alice"})
    b = client_module.get_connection({"host": "10.0.0.5", "username": "bob"})

    assert a is not b


def test_port_defaults_to_1883(monkeypatch):
    _fresh_pool(monkeypatch)

    a = client_module.get_connection({"host": "10.0.0.5"})
    b = client_module.get_connection({"host": "10.0.0.5", "port": 1883})

    assert a is b
