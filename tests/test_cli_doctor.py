from localhome.cli import _run_doctor
from localhome.config import load_config
from localhome.core.manager import DeviceManager
from localhome.services.climate_map_store import ClimateMapStore, apply_to_config
from tests.mqtt_fakes import FakeMqttConnection


def _config(tmp_path, yaml_text: str):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_config(path)


class FakeManager:
    def __init__(self, pollers, switches, numbers, sensor_kinds, paired_switch, notifier=None):
        self.pollers = pollers
        self.switches = switches
        self.numbers = numbers
        self.sensor_kinds = sensor_kinds
        self.paired_switch = paired_switch
        self.notifier = notifier
        self.covers = {}
        self.failed_integrations = []


def test_doctor_reports_no_problems_for_a_consistent_setup(tmp_path, capsys):
    config = _config(
        tmp_path,
        """
integrations: []
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Temp"
      valve: "Rooms Valve"
""",
    )
    manager = FakeManager(
        pollers={"Rooms Temp": object(), "Rooms Valve": object()},
        switches={"Rooms Valve": object()},
        numbers={},
        sensor_kinds={"Rooms Temp": "climate", "Rooms Valve": "switch"},
        paired_switch={},
    )

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    assert exit_code == 0
    assert "No configuration problems found" in capsys.readouterr().out


def test_doctor_catches_a_zone_sensor_typo(tmp_path, capsys):
    config = _config(
        tmp_path,
        """
integrations: []
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Temp - typo"
      valve: "Rooms Valve"
""",
    )
    manager = FakeManager(
        pollers={"Rooms Temp": object(), "Rooms Valve": object()},
        switches={"Rooms Valve": object()},
        numbers={},
        sensor_kinds={"Rooms Temp": "climate", "Rooms Valve": "switch"},
        paired_switch={},
    )

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "Rooms Temp - typo" in out
    assert "matches no configured integration" in out


def test_doctor_catches_a_sensor_of_the_wrong_kind(tmp_path, capsys):
    config = _config(
        tmp_path,
        """
integrations: []
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Power Meter"
      valve: "Rooms Valve"
""",
    )
    manager = FakeManager(
        pollers={"Rooms Power Meter": object(), "Rooms Valve": object()},
        switches={"Rooms Valve": object()},
        numbers={},
        sensor_kinds={"Rooms Power Meter": "power_meter", "Rooms Valve": "switch"},
        paired_switch={},
    )

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "expected 'climate'" in out


def test_doctor_catches_a_missing_valve(tmp_path, capsys):
    config = _config(
        tmp_path,
        """
integrations: []
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Temp"
      valve: "Rooms Valve - typo"
""",
    )
    manager = FakeManager(
        pollers={"Rooms Temp": object()},
        switches={},
        numbers={},
        sensor_kinds={"Rooms Temp": "climate"},
        paired_switch={},
    )

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    assert exit_code == 1
    assert "valve 'Rooms Valve - typo' matches no configured switch" in capsys.readouterr().out


def test_doctor_catches_a_dangling_paired_switch(tmp_path, capsys):
    config = _config(tmp_path, "integrations: []\n")
    manager = FakeManager(
        pollers={"Brightness": object()},
        switches={},
        numbers={"Brightness": object()},
        sensor_kinds={"Brightness": "number"},
        paired_switch={"Brightness": "Lamp - typo"},
    )

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    assert exit_code == 1
    assert "paired_switch 'Lamp - typo' matches no configured switch" in capsys.readouterr().out


def test_doctor_catches_a_dangling_energy_sensor(tmp_path, capsys):
    config = _config(
        tmp_path,
        """
integrations: []
energy:
  sensor: "Meter - typo"
  contract_limit_w: 3000
""",
    )
    manager = FakeManager(pollers={}, switches={}, numbers={}, sensor_kinds={}, paired_switch={})

    exit_code = _run_doctor(config, manager, apply_to_config(config))

    assert exit_code == 1
    assert "energy.sensor 'Meter - typo' matches no configured integration" in capsys.readouterr().out


def test_doctor_cross_checks_a_climate_admin_panel_managed_zone(tmp_path, capsys):
    # No thermostat: key in config.yaml at all - the from-scratch,
    # entirely panel-managed apartment. Before apply_to_config() was
    # wired into main(), doctor would report "Thermostat zones: 0" and
    # never check these at all - see cli.py's main().
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_zones([
        {"name": "Rooms", "sensor_mode": "simulated", "sensor_device_id": None,
         "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": True},
        {"name": "Bathrooms", "sensor_mode": "simulated", "sensor_device_id": None,
         "valve_mode": "simulated", "valve_device_id": None, "cool_enabled": False},
    ])
    config = _config(tmp_path, "integrations: []\n")

    thermostat_cfg = apply_to_config(config)
    manager = DeviceManager(config)
    exit_code = _run_doctor(config, manager, thermostat_cfg)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Thermostat zones: 2" in out


def test_doctor_sees_a_climate_admin_panel_managed_extra_switch(tmp_path, capsys, monkeypatch):
    # The panel's fixed-name extra switch (services/climate_map_store.py's
    # EXTRA_SWITCH_FIELDS) needs a real broker file to actually build -
    # same requirement any mqtt_json driver has. Faked, like every other
    # MQTT-touching test in this project - no real network.
    monkeypatch.setattr("localhome.drivers.mqtt.base.get_connection", lambda broker: FakeMqttConnection())
    ClimateMapStore(str(tmp_path / "climate_map.json")).set_extra_switches(
        {"bypass_valve_device_id": None, "dehumidifier_device_id": "shellyplus1-AAAA"}
    )
    (tmp_path / "mqtt_broker.json").write_text('{"host": "test-broker", "port": 1883}', encoding="utf-8")
    config = _config(
        tmp_path,
        """
integrations: []
energy:
  sensor: "Dehumidifier"
""",
    )

    thermostat_cfg = apply_to_config(config)
    manager = DeviceManager(config)
    exit_code = _run_doctor(config, manager, thermostat_cfg)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "No configuration problems found" in out
