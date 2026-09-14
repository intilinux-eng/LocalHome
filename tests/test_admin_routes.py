from localhome.services.device_overrides import DeviceOverrideStore
from localhome.web.app import create_app

CONFIG_YAML = """
web:
  port: 5000
integrations:
  - kind: climate
    type: simulated
    name: Test Sensor
  - kind: switch
    type: simulated
    name: Test Switch
"""


def _app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")
    return create_app(str(config_path)), tmp_path


def test_admin_page_loads(tmp_path):
    app, _ = _app(tmp_path)
    res = app.test_client().get("/admin")
    assert res.status_code == 200
    assert b"device-list" in res.data


def test_api_admin_devices_lists_every_poller_with_default_order(tmp_path):
    app, _ = _app(tmp_path)
    res = app.test_client().get("/api/admin/devices")
    data = res.get_json()

    assert data["ok"] is True
    names = [d["name"] for d in data["devices"]]
    assert set(names) == {"Test Sensor", "Test Switch"}
    assert "Test Switch" in data["switch_names"]
    for device in data["devices"]:
        assert device["hidden"] is False
        assert device["tab"] == "home"


def test_override_persists_across_a_fresh_store_instance(tmp_path):
    app, tmp_path = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/admin/devices/Test Sensor/override", json={"hidden": True})
    assert res.get_json() == {"ok": True}

    # Read back through a brand-new DeviceOverrideStore pointed at the
    # same file, not the one create_app() built - proves this actually
    # round-trips through disk, not just an in-memory object.
    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    assert store.get("Test Sensor") == {"hidden": True}

    res = client.get("/api/admin/devices")
    device = next(d for d in res.get_json()["devices"] if d["name"] == "Test Sensor")
    assert device["hidden"] is True


def test_override_rejects_an_unknown_device_name(tmp_path):
    app, _ = _app(tmp_path)
    res = app.test_client().post("/api/admin/devices/Does Not Exist/override", json={"hidden": True})
    assert res.status_code == 404


def test_override_rejects_an_invalid_tab_value(tmp_path):
    app, _ = _app(tmp_path)
    res = app.test_client().post("/api/admin/devices/Test Sensor/override", json={"tab": "not-a-real-tab"})
    assert res.status_code == 400


def test_override_rejects_pairing_with_an_unknown_switch(tmp_path):
    app, _ = _app(tmp_path)
    res = app.test_client().post("/api/admin/devices/Test Sensor/override", json={"paired_switch": "Ghost Switch"})
    assert res.status_code == 400


def test_set_order_applies_and_persists(tmp_path):
    app, tmp_path = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/admin/devices/order", json={"order": ["Test Switch", "Test Sensor"]})
    assert res.get_json() == {"ok": True}

    devices = client.get("/api/admin/devices").get_json()["devices"]
    assert [d["name"] for d in devices] == ["Test Switch", "Test Sensor"]


def test_set_order_rejects_an_unknown_name_and_writes_nothing(tmp_path):
    app, tmp_path = _app(tmp_path)
    client = app.test_client()

    res = client.post("/api/admin/devices/order", json={"order": ["Test Sensor", "Ghost Device"]})
    assert res.status_code == 400

    store = DeviceOverrideStore(str(tmp_path / "device_overrides.json"))
    assert store.get_all() == {}
