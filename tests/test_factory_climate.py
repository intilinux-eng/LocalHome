"""A new board - the README's empty config.yaml, or one after a factory
reset - is the reference apartment's climate with every Shelly to
configure: Stanze and Bagni, each with its H&T sensor and its valve, the
bathroom bypass valve, the dehumidifier, the outdoor sensor and the VMC.
Nothing simulated, nothing built until its id is saved - one at a time -
and none of it a fault.
"""
import pytest

from localhome.cli import _run_doctor
from localhome.config import load_config
from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import apply_to_config, open_climate_map, setup_todo
from localhome.services.thermostat import ScheduleStore
from localhome.web.app import create_app

# The README's installation, step 5.
EMPTY = 'web:\n  port: 5000\n  language: "it"\n\nintegrations: []\n'
EVERY_SHELLY = ["Stanze Sensor", "Stanze Valve", "Bagni Sensor", "Bagni Valve",
                "Bathroom Bypass Valve", "Dehumidifier", "Outdoor Sensor", "VMC"]


def _config(tmp_path, text=EMPTY):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_a_new_board_is_the_apartments_climate_every_shelly_to_configure(tmp_path):
    config = _config(tmp_path)
    store = open_climate_map(config)

    zones = store.get_zones()
    assert [(z["name"], z["cool_enabled"]) for z in zones] == [("Stanze", True), ("Bagni", False)]
    assert all(z[f"{part}_mode"] == "shelly" and z[f"{part}_device_id"] is None
               for z in zones for part in ("sensor", "valve"))
    assert not store.zones_saved()
    todo = setup_todo(config, store)
    assert [t["name"] for t in todo] == EVERY_SHELLY
    # The bypass and the dehumidifier go with the bathrooms.
    assert [t["zone"] for t in todo] == ["Stanze", "Stanze", "Bagni", "Bagni", "Bagni", "Bagni", None, None]


def test_each_id_saved_comes_off_the_list(tmp_path):
    config = _config(tmp_path)
    store = open_climate_map(config)
    zones = store.get_zones()
    zones[0]["valve_device_id"] = "shelly1g3-AAAA"
    store.set_zones(zones)
    store.set_extra_switches({"bypass_valve_device_id": "shelly1g3-BBBB", "dehumidifier_device_id": None})
    store.set_extra_dimmers({"vmc_mode": "none", "vmc_device_id": None})   # this apartment has none

    assert [t["name"] for t in setup_todo(config, store)] == [
        "Stanze Sensor", "Bagni Sensor", "Bagni Valve", "Dehumidifier", "Outdoor Sensor"]


def test_a_vmc_wired_by_hand_in_config_yaml_isnt_asked_for_again(tmp_path):
    config = _config(tmp_path, EMPTY.replace("integrations: []", """integrations:
  - kind: switch
    type: shelly_light
    name: "VMC Dimmer"
    broker_file: "mqtt_broker.json"
    device_id: "shelly0110dimg3-AAAA"
"""))
    assert "VMC" not in [t["name"] for t in setup_todo(config, open_climate_map(config))]


def test_a_config_yaml_wiring_its_own_zones_keeps_them(tmp_path):
    config = _config(tmp_path, EMPTY + """
thermostat:
  zones:
    - {name: "Rooms", sensor: "Rooms Sensor", valve: "Rooms Valve"}
""")
    store = open_climate_map(config)

    assert store.get_zones() is None
    assert setup_todo(config, store) == []


def test_doctor_lists_the_shellys_to_configure_and_finds_no_problem(tmp_path, capsys):
    config = _config(tmp_path)
    thermostat_cfg = apply_to_config(config)

    assert _run_doctor(config, DeviceManager(config), thermostat_cfg) == 0
    out = capsys.readouterr().out
    assert f"Shellys to configure in the admin panel (8): {', '.join(EVERY_SHELLY)}" in out


# --------------------------------------------------- the whole app ----------

@pytest.fixture
def app(tmp_path):
    _config(tmp_path)
    app = create_app(str(tmp_path / "config.yaml"))
    yield app
    app.config["HISTORY"].close()


def test_the_board_says_what_is_to_configure_and_nothing_is_broken(app):
    client = app.test_client()
    climate = client.get("/api/climate/zones").get_json()

    assert [z["name"] for z in climate["zones"]] == ["Stanze", "Bagni"]
    assert all(not z["sensor_configured"] and not z["valve_configured"] for z in climate["zones"])
    assert [t["name"] for t in climate["to_configure"]] == EVERY_SHELLY
    assert client.get("/api/devices").get_json()["sensors"] == []
    assert client.get("/api/problems").get_json()["problems"] == []
    panel = client.get("/api/climate-admin/map").get_json()
    assert (panel["configured"], panel["restart_pending"]) == (False, False)
    assert [z["name"] for z in panel["zones"]] == ["Stanze", "Bagni"]


def test_saving_the_zones_as_they_are_needs_no_restart(app):
    client = app.test_client()
    zones = client.get("/api/climate-admin/map").get_json()["zones"]
    for zone in zones:
        zone["sensor_device_id"] = zone["valve_device_id"] = ""   # what the panel's empty fields send

    assert client.post("/api/climate-admin/map", json={"zones": zones}).status_code == 200
    panel = client.get("/api/climate-admin/map").get_json()
    assert (panel["configured"], panel["restart_pending"]) == (True, False)


def test_a_valve_wired_before_its_sensor_stays_closed(tmp_path):
    """Nothing to go by: closed, even with a week wanting heat all day and
    the valve opened by hand - and that's not a sensor gone silent."""
    config = _config(tmp_path)
    store = open_climate_map(config)
    zones = store.get_zones()
    zones[0]["valve_mode"] = "simulated"
    store.set_zones(zones)
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        thermostat, manager = app.config["THERMOSTAT"], app.config["MANAGER"]
        week = ScheduleStore.empty_week()
        for day in week:
            week[day] = [22.0] * 24
        thermostat.schedule_store.set_week("Stanze", week, "heat")
        manager.switches["Stanze Valve"].turn_on()

        thermostat.tick()

        assert manager.sensor_reading("Stanze Valve")["is_on"] is False
        stanze = app.test_client().get("/api/climate/zones").get_json()["zones"][0]
        assert (stanze["sensor_configured"], stanze["valve_configured"]) == (False, True)
        assert stanze["sensor_unreachable_hours"] is None and stanze["sensor_failsafe_active"] is False
    finally:
        app.config["HISTORY"].close()
