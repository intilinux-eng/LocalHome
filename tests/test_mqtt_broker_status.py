"""Unit tests for services/mqtt_broker_status.py's raw TCP reachability
probe - a real socket connect, not a fake, so these use real loopback
sockets rather than tests/mqtt_fakes.py (which only fakes the MQTT
protocol layer, not TCP itself). See web/app.py's
/api/climate-admin/broker-status for how the climate admin panel uses
this.
"""
import json
import socket

from localhome.services.mqtt_broker_status import check_broker


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
