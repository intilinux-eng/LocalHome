"""Refusals carry a code the pages translate, and every code has a text.

The pages show `errors.<code>` from the locale files, with the refusal's
parameters filled in - see localhome/errors.py. A code without a text
would fall back to the English message; this makes that a failing test
instead of a surprise on an Italian page.
"""
import json
import re
from pathlib import Path

import pytest

from localhome.services.climate_map_store import ClimateMapStore
from localhome.web.app import create_app

SRC = Path(__file__).resolve().parents[1] / "src" / "localhome"
LOCALES = SRC / "web" / "locales"


def _codes_in_source() -> set[str]:
    codes = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        codes |= set(re.findall(r'InputError\(\s*"(\w+)"', text))
        codes |= set(re.findall(r'_fail\(\s*"(\w+)"', text))
        codes |= set(re.findall(r'code="(\w+)"', text))
    return codes


@pytest.mark.parametrize("lang", ["it", "en"])
def test_every_refusal_code_has_a_text(lang):
    texts = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))["errors"]
    codes = _codes_in_source()
    assert len(codes) > 20
    assert sorted(codes - set(texts)) == []


@pytest.mark.parametrize("lang", ["it", "en"])
def test_every_parameter_a_text_uses_is_one_the_code_can_send(lang):
    """A text naming {zone} for a code that never sends it would show the
    placeholder itself."""
    texts = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))["errors"]
    allowed = {"zone", "count", "max", "value", "key", "label", "choices", "min", "length", "period", "name"}
    for code, text in texts.items():
        assert set(re.findall(r"\{(\w+)\}", text)) <= allowed, code


def test_the_api_sends_the_code_and_its_parameters(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web:\n  port: 5000\nintegrations: []\n", encoding="utf-8")
    app = create_app(str(path))
    try:
        zones = [
            {"name": "Stanze", "sensor_mode": "shelly", "sensor_device_id": None,
             "valve_mode": "simulated", "cool_enabled": True},
            {"name": "Bagni", "sensor_mode": "simulated", "valve_mode": "simulated", "cool_enabled": True},
        ]
        res = app.test_client().post("/api/climate-admin/map", json={"zones": zones})
        body = res.get_json()
        assert res.status_code == 400
        assert body["code"] == "sensor_id_required" and body["params"] == {"zone": "Stanze"}
        assert "sensor_device_id" in body["error"], "the English message stays for logs and API callers"

        res = app.test_client().post("/api/climate-admin/settings", json={"values": {"hysteresis_c": 9}})
        assert res.get_json()["code"] == "setting_range"
        assert res.get_json()["params"] == {"key": "hysteresis_c", "min": 0.1, "max": 2.0}
    finally:
        app.config["HISTORY"].close()


def test_a_duplicate_names_the_value(tmp_path):
    store = ClimateMapStore(str(tmp_path / "climate_map.json"))
    zones = [
        {"name": "Stanze", "sensor_mode": "simulated", "valve_mode": "simulated", "cool_enabled": True},
        {"name": "Stanze", "sensor_mode": "simulated", "valve_mode": "simulated", "cool_enabled": True},
    ]
    with pytest.raises(ValueError) as exc:
        store.set_zones(zones)
    assert (exc.value.code, exc.value.params) == ("duplicate_zone_name", {"value": "Stanze"})
