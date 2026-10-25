import pytest

from localhome.config import load_config


def test_load_example_config_end_to_end(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
web:
  host: "127.0.0.1"
  port: 9000
  language: "it"

integrations:
  - kind: cover
    type: tuya_curtain_switch
    devices_file: "devices.json"

notifications:
  type: telegram
  secrets_file: "secrets_telegram.json"

energy:
  sensor: "Home Power Meter"
  contract_limit_w: 3000
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.web_host == "127.0.0.1"
    assert config.web_port == 9000
    assert config.web_language == "it"
    assert len(config.integrations) == 1
    assert config.integrations[0].kind == "cover"
    assert config.integrations[0].options["devices_file"] == "devices.json"
    assert config.notifier.type == "telegram"
    assert config.raw["energy"]["contract_limit_w"] == 3000


def test_relative_paths_resolve_against_the_config_files_own_directory(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("integrations: []\n", encoding="utf-8")

    config = load_config(config_path)

    assert config.resolve("devices.json") == str(tmp_path / "devices.json")


def test_absolute_paths_are_returned_unchanged(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("integrations: []\n", encoding="utf-8")
    config = load_config(config_path)

    absolute = str(tmp_path / "elsewhere" / "devices.json")
    assert config.resolve(absolute) == absolute


def test_missing_config_file_raises_with_a_helpful_hint(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_the_example_as_it_comes_is_the_climate_with_nothing_else(tmp_path):
    """config.example.yaml copied as it is: `integrations:` with every
    entry commented out is no device, not an error."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    path = tmp_path / "config.yaml"
    path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    config = load_config(path)

    assert config.integrations == [] and config.notifier is None
    assert config.web_language == "it" and config.raw["thermostat"]["mode"] == "heat"


def test_an_entry_with_enabled_false_is_left_out(tmp_path):
    """A device not installed yet stays in the file, but isn't loaded -
    so it raises no error in the admin panel or on the LED matrix."""
    path = tmp_path / "config.yaml"
    path.write_text("""
integrations:
  - kind: switch
    type: unconfigured_switch
    name: "Divano"
    enabled: false
  - kind: switch
    type: simulated
    name: "Lamp"
  - kind: switch
    type: simulated
    name: "Explicitly on"
    enabled: true
""", encoding="utf-8")

    config = load_config(path)

    assert [i.options["name"] for i in config.integrations] == ["Lamp", "Explicitly on"]
    assert all("enabled" not in i.options for i in config.integrations)
