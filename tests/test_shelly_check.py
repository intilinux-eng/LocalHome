"""The admin panel's check of a Shelly the moment its id is typed - see
services/shelly_check.py. A fake RPC: nothing on the network."""
import json

from localhome.services.shelly_check import ShellyCheck
from localhome.web.app import create_app

RELAY = "shellyplus1-c0ffee000001"
INFO = {"id": RELAY, "model": "SNSW-001X16EU", "app": "Plus1", "ver": "1.7.5"}
# The production board's Plus 1 after a first try in the app.
CONFIG = {
    "switch:0": {"id": 0, "in_mode": "activate", "initial_state": "off", "auto_on": False, "auto_on_delay": 60.0,
                 "auto_off": False, "auto_off_delay": 60.0},
    "mqtt": {"enable": True, "status_ntf": True, "enable_rpc": True, "enable_control": True},
}


class FakeRpc:
    def __init__(self, info=INFO, config=CONFIG, silent=False):
        self.info, self.config, self.silent = info, config, silent
        self.calls = []

    def call(self, method, params=None):
        self.calls.append(method)
        if self.silent:
            raise RuntimeError(f"no RPC reply from '{RELAY}' for {method} within 5s - device is probably offline")
        return json.loads(json.dumps(self.info if method == "Shelly.GetDeviceInfo" else self.config))


def _check(tmp_path, rpc):
    broker_file = tmp_path / "mqtt_broker.json"
    broker_file.write_text(json.dumps({"host": "localhost", "port": 1883}), encoding="utf-8")
    made = []

    def rpc_for(broker, device_id):
        made.append(device_id)
        return rpc
    return ShellyCheck(str(broker_file), rpc_for=rpc_for), made


def test_a_valve_relay_is_found_and_its_wrong_settings_named(tmp_path):
    check, _ = _check(tmp_path, FakeRpc())
    result = check.check(RELAY, "valve")
    assert (result["answered"], result["model"], result["firmware"]) == (True, "Plus1", "1.7.5")
    assert result["settings_wrong"] == [{"key": "in_mode", "value": "activate", "expected": "detached"},
                                        {"key": "auto_off", "value": 0, "expected": 900}]


def test_the_bypass_is_held_to_fewer_settings(tmp_path):
    """Its safe state isn't decided: only Detached and MQTT are its own."""
    check, _ = _check(tmp_path, FakeRpc())
    assert [s["key"] for s in check.check(RELAY, "bypass")["settings_wrong"]] == ["in_mode"]


def test_a_silent_device_says_so(tmp_path):
    check, _ = _check(tmp_path, FakeRpc(silent=True))
    result = check.check(RELAY, "dehumidifier")
    assert result["answered"] is False and result["reason"] == "silent" and "probably offline" in result["error"]


def test_a_battery_sensor_isnt_asked(tmp_path):
    rpc = FakeRpc()
    check, made = _check(tmp_path, rpc)
    assert check.check("shellyhtg3-c0ffee000002", "sensor") == {"ok": True, "answered": None, "battery": True}
    assert made == [] and rpc.calls == []


def test_a_typo_isnt_sent_anywhere(tmp_path):
    check, made = _check(tmp_path, FakeRpc())
    assert check.check("shellyplus1-fce8c0", "valve") == {"ok": False, "reason": "id"}
    assert check.check(RELAY, "toaster") == {"ok": False, "reason": "role"}
    assert made == []


def test_no_broker_no_question(tmp_path):
    check = ShellyCheck(str(tmp_path / "mqtt_broker.json"), rpc_for=lambda broker, device_id: FakeRpc())
    assert check.check(RELAY, "valve") == {"ok": True, "answered": False, "reason": "no_broker"}


def test_one_client_per_device_however_often_asked(tmp_path):
    """Each client subscribes to the replies once: asking again mustn't add more."""
    check, made = _check(tmp_path, FakeRpc())
    for _ in range(3):
        check.check(RELAY, "valve")
    assert made == [RELAY]


def test_the_admin_api_answers_with_the_check(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(config))
    check, _ = _check(tmp_path, FakeRpc())
    app.config["SHELLY_CHECK"] = check
    body = app.test_client().get(f"/api/climate-admin/shelly-check?id={RELAY}&role=valve").get_json()
    assert body["answered"] is True and [s["key"] for s in body["settings_wrong"]] == ["in_mode", "auto_off"]


def test_the_installers_guide_is_served_and_linked_from_the_admin(tmp_path):
    """The Plus 1 guide, HTML and PDF, from the board itself - always there,
    for whoever installs a valve."""
    config = tmp_path / "config.yaml"
    config.write_text("web:\n  port: 5000\n  language: it\nintegrations: []\n", encoding="utf-8")
    client = create_app(str(config)).test_client()
    page = client.get("/climate-admin").get_data(as_text=True)
    for path in ("/static/guide/shelly-plus1-valvole.html", "/static/guide/shelly-plus1-valvole.pdf"):
        assert path in page
        res = client.get(path)
        assert res.status_code == 200 and len(res.data) > 2000, path
    html = client.get("/static/guide/shelly-plus1-valvole.html").get_data(as_text=True)
    for setting in ("Detached", "spegnersi quando è alimentato", "900 secondi", "ponte"):
        assert setting in html
    assert client.get("/static/guide/shelly-plus1-valvole.pdf").data.startswith(b"%PDF")
