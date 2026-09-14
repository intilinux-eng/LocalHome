from localhome.cli import _run_doctor
from localhome.config import load_config


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

    exit_code = _run_doctor(config, manager)

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

    exit_code = _run_doctor(config, manager)

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

    exit_code = _run_doctor(config, manager)

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

    exit_code = _run_doctor(config, manager)

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

    exit_code = _run_doctor(config, manager)

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

    exit_code = _run_doctor(config, manager)

    assert exit_code == 1
    assert "energy.sensor 'Meter - typo' matches no configured integration" in capsys.readouterr().out
