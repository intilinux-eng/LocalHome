"""drivers/mqtt/state_cache.py: the on-disk persistence that lets a
driver survive a restart with its last known MQTT payload intact - see
that module's docstring for why this matters beyond cosmetics (the
thermostat leaving a valve alone during what would otherwise be a
several-hour "no message yet" blackout after every restart).
"""
import json

from localhome.drivers.mqtt.state_cache import load_cached, save_cached

BROKER = {"host": "192.168.1.21", "port": 1883}


def test_a_topic_with_no_cached_value_returns_none(tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")

    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") is None


def test_a_saved_value_round_trips(tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")

    save_cached(cache_path, BROKER, "shelly/status/temperature:0", {"tC": 21.5}, 1000.0)

    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") == ({"tC": 21.5}, 1000.0)


def test_different_topics_on_the_same_broker_do_not_collide(tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")

    save_cached(cache_path, BROKER, "shelly/status/temperature:0", {"tC": 21.5}, 1000.0)
    save_cached(cache_path, BROKER, "shelly/status/humidity:0", {"rh": 47.0}, 1001.0)

    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") == ({"tC": 21.5}, 1000.0)
    assert load_cached(cache_path, BROKER, "shelly/status/humidity:0") == ({"rh": 47.0}, 1001.0)


def test_the_same_topic_on_two_different_brokers_does_not_collide(tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    other_broker = {"host": "192.168.1.99", "port": 1883}

    save_cached(cache_path, BROKER, "shellies/relay0/status", "on", 1000.0)
    save_cached(cache_path, other_broker, "shellies/relay0/status", "off", 1001.0)

    assert load_cached(cache_path, BROKER, "shellies/relay0/status") == ("on", 1000.0)
    assert load_cached(cache_path, other_broker, "shellies/relay0/status") == ("off", 1001.0)


def test_saving_again_overwrites_rather_than_growing_the_file(tmp_path):
    cache_path = str(tmp_path / "mqtt_state_cache.json")

    save_cached(cache_path, BROKER, "shelly/status/temperature:0", {"tC": 20.0}, 1000.0)
    save_cached(cache_path, BROKER, "shelly/status/temperature:0", {"tC": 21.0}, 2000.0)

    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") == ({"tC": 21.0}, 2000.0)
    with open(cache_path, encoding="utf-8") as f:
        assert len(json.load(f)) == 1


def test_a_corrupt_cache_file_is_treated_as_empty_rather_than_raising(tmp_path):
    cache_path = tmp_path / "mqtt_state_cache.json"
    cache_path.write_text("not valid json", encoding="utf-8")

    assert load_cached(str(cache_path), BROKER, "shelly/status/temperature:0") is None
    # Must not raise, and must still be able to save going forward.
    save_cached(str(cache_path), BROKER, "shelly/status/temperature:0", {"tC": 21.5}, 1000.0)
    assert load_cached(str(cache_path), BROKER, "shelly/status/temperature:0") == ({"tC": 21.5}, 1000.0)


def test_a_missing_directory_is_created_on_save(tmp_path):
    cache_path = str(tmp_path / "nested" / "mqtt_state_cache.json")

    save_cached(cache_path, BROKER, "shelly/status/temperature:0", {"tC": 21.5}, 1000.0)

    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") == ({"tC": 21.5}, 1000.0)
