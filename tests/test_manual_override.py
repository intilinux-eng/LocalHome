from localhome.drivers.manual.climate import ManualOverrideDriver
from localhome.services.manual_override import ManualOverrideStore


def test_get_returns_none_before_any_value_is_set(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    assert store.get("Rooms Sensor") is None


def test_set_and_get_round_trip(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    store.set("Rooms Sensor", 21.5, 47.0)
    assert store.get("Rooms Sensor") == {"temperature_c": 21.5, "humidity_pct": 47.0}


def test_humidity_is_optional(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    store.set("Rooms Sensor", 21.5)
    assert store.get("Rooms Sensor") == {"temperature_c": 21.5, "humidity_pct": None}


def test_a_second_store_instance_sees_a_saved_value(tmp_path):
    path = str(tmp_path / "manual_override.json")
    ManualOverrideStore(path).set("Rooms Sensor", 21.5)
    assert ManualOverrideStore(path).get("Rooms Sensor") == {"temperature_c": 21.5, "humidity_pct": None}


def test_different_zones_do_not_collide(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    store.set("Rooms Sensor", 21.5)
    store.set("Bathrooms Sensor", 26.0)
    assert store.get("Rooms Sensor")["temperature_c"] == 21.5
    assert store.get("Bathrooms Sensor")["temperature_c"] == 26.0


def test_a_corrupt_file_is_treated_as_empty_rather_than_raising(tmp_path):
    path = tmp_path / "manual_override.json"
    path.write_text("not valid json", encoding="utf-8")
    store = ManualOverrideStore(str(path))

    assert store.get("Rooms Sensor") is None
    store.set("Rooms Sensor", 21.5)  # must not raise, must still be able to save
    assert store.get("Rooms Sensor")["temperature_c"] == 21.5


# --------------------------------------------------------- driver ----

def test_driver_reports_not_ok_before_any_value_is_set(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    driver = ManualOverrideDriver(name="Rooms Sensor", store=store)

    reading = driver.read()

    assert reading["ok"] is False
    assert "no manual value set yet" in reading["error"]


def test_driver_reports_the_set_value(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    store.set("Rooms Sensor", 21.5, 47.0)
    driver = ManualOverrideDriver(name="Rooms Sensor", store=store)

    reading = driver.read()

    assert reading == {
        "ok": True, "name": "Rooms Sensor", "temperature_c": 21.5, "humidity_pct": 47.0, "manual_override": True,
    }


def test_driver_omits_humidity_when_not_set(tmp_path):
    store = ManualOverrideStore(str(tmp_path / "manual_override.json"))
    store.set("Rooms Sensor", 21.5)
    driver = ManualOverrideDriver(name="Rooms Sensor", store=store)

    reading = driver.read()

    assert "humidity_pct" not in reading
