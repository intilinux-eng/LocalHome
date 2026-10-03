"""Unit tests for services/mqtt_broker_status.py's raw TCP reachability
probe - a real socket connect, not a fake, so these use real loopback
sockets rather than tests/mqtt_fakes.py (which only fakes the MQTT
protocol layer, not TCP itself). See web/app.py's
/api/climate-admin/broker-status for how the climate admin panel uses
this.
"""
import json
import socket

from localhome.services.mqtt_broker_status import PROBE_EVERY_SECONDS, BrokerStatus, check_broker


def _unused_port() -> int:
    # Bind-then-close to get a real ephemeral port guaranteed free right
    # before the check, without hardcoding one that might be in use.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    _, port = probe.getsockname()
    probe.close()
    return port


def test_reachable_when_something_is_actually_listening(tmp_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    broker_file = tmp_path / "mqtt_broker.json"
    broker_file.write_text(json.dumps({"host": host, "port": port}), encoding="utf-8")

    try:
        result = check_broker(str(broker_file))
    finally:
        server.close()

    assert result == {"reachable": True, "host": host, "port": port, "error": None}


def test_not_reachable_when_nothing_is_listening(tmp_path):
    port = _unused_port()
    broker_file = tmp_path / "mqtt_broker.json"
    broker_file.write_text(json.dumps({"host": "127.0.0.1", "port": port}), encoding="utf-8")

    result = check_broker(str(broker_file), timeout=0.5)

    assert result["reachable"] is False
    assert result["host"] == "127.0.0.1"
    assert result["port"] == port
    assert result["error"]


def test_missing_broker_file_reports_unreachable_not_a_crash(tmp_path):
    result = check_broker(str(tmp_path / "does_not_exist.json"))

    assert result["reachable"] is False
    assert "does_not_exist.json" in result["error"]


def _broker_file(tmp_path):
    path = tmp_path / "mqtt_broker.json"
    path.write_text(json.dumps({"host": "localhost", "port": 1883}), encoding="utf-8")
    return str(path)


def test_localhomes_own_connection_answers_without_a_probe(tmp_path):
    """Up: no connection to the broker at all - every probe is two lines
    in its log, and an open admin page asks every few seconds."""
    probes = []
    status = BrokerStatus(_broker_file(tmp_path), connection_up=lambda host, port: True,
                          probe=lambda path: probes.append(path) or {"reachable": False})
    for _ in range(5):
        assert status.get()["reachable"] is True
    assert probes == []


def test_without_a_connection_up_it_probes_at_most_once_a_minute(tmp_path):
    now = [1000.0]
    probes = []
    status = BrokerStatus(_broker_file(tmp_path), connection_up=lambda host, port: None,
                          probe=lambda path: probes.append(now[0]) or {"reachable": True, "host": "localhost"},
                          clock=lambda: now[0])
    for step in range(20):      # one ask every 3.5 s for 70 s
        now[0] = 1000.0 + step * 3.5
        assert status.get()["reachable"] is True
    assert probes == [1000.0, 1000.0 + 18 * 3.5], "the first ask, then the first one a minute later"


def test_a_connection_down_falls_back_to_the_probe(tmp_path):
    status = BrokerStatus(_broker_file(tmp_path), connection_up=lambda host, port: False,
                          probe=lambda path: {"reachable": False, "error": "refused"})
    assert status.get() == {"reachable": False, "error": "refused"}
