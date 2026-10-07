"""drivers/mqtt/state_cache.py: the on-disk persistence that lets a
driver survive a restart with its last known MQTT payload intact - see
that module's docstring for why this matters beyond cosmetics (the
thermostat leaving a valve alone during what would otherwise be a
several-hour "no message yet" blackout after every restart).
"""
import json
import time

from localhome.drivers.mqtt import state_cache
from localhome.drivers.mqtt.state_cache import flush, load_cached, save_cached

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
    flush()
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
    flush()

    assert (tmp_path / "nested" / "mqtt_state_cache.json").exists()
    assert load_cached(cache_path, BROKER, "shelly/status/temperature:0") == ({"tC": 21.5}, 1000.0)


def test_a_re_addressed_broker_keeps_the_last_reading(tmp_path):
    # The broker moved from its IP to "localhost" - same broker, same topic.
    path = str(tmp_path / "mqtt_state_cache.json")
    save_cached(path, {"host": "192.168.1.28", "port": 1883}, "sensor/status/temperature:0", {"tC": 21.5}, 1000.0)

    assert load_cached(path, {"host": "localhost", "port": 1883}, "sensor/status/temperature:0") == ({"tC": 21.5}, 1000.0)
    assert load_cached(path, {"host": "localhost", "port": 1883}, "other/topic") is None


def test_the_newest_reading_wins_across_addresses(tmp_path):
    path = str(tmp_path / "mqtt_state_cache.json")
    save_cached(path, {"host": "a"}, "t", {"v": 1}, 100.0)
    save_cached(path, {"host": "b"}, "t", {"v": 2}, 200.0)

    assert load_cached(path, {"host": "c"}, "t") == ({"v": 2}, 200.0)


# ------------------------------------------------ off the MQTT thread ----
# save_cached() runs on every MQTT message, in the MQTT client's network
# thread. It used to read, write and fsync the whole file each time: a
# slow disk held that thread - on the production board, right after a
# boot, LocalHome's connection went 90 s without a keepalive and the
# broker dropped it.

def test_a_message_never_waits_for_the_disk(tmp_path, monkeypatch):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    monkeypatch.setattr(state_cache, "save_json_atomic", lambda path, data: time.sleep(1.5))
    started = time.monotonic()
    save_cached(cache_path, BROKER, "shelly/status/switch:0", {"output": True}, 1000.0)
    assert time.monotonic() - started < 0.2
    assert load_cached(cache_path, BROKER, "shelly/status/switch:0") == ({"output": True}, 1000.0),         "read back at once, before it's on disk"


def test_the_file_follows_by_itself(tmp_path):
    cache_path = tmp_path / "mqtt_state_cache.json"
    save_cached(str(cache_path), BROKER, "shelly/status/switch:0", {"output": True}, 1000.0)
    deadline = time.monotonic() + state_cache.FLUSH_SECONDS * 2 + 1
    while not cache_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(cache_path.read_text(encoding="utf-8"))["192.168.1.21:1883/shelly/status/switch:0"]["payload"] == {"output": True}


def test_many_messages_are_one_write(tmp_path, monkeypatch):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    flush()
    writes = []
    real = state_cache.save_json_atomic
    monkeypatch.setattr(state_cache, "save_json_atomic", lambda path, data: (writes.append(path), real(path, data)))
    for i in range(50):
        save_cached(cache_path, BROKER, f"shelly/status/{i % 5}", {"n": i}, 1000.0 + i)
    flush()
    assert writes == [cache_path]
    assert load_cached(cache_path, BROKER, "shelly/status/4") == ({"n": 49}, 1049.0), "the newest of each topic"


def _five_minutes_on(path):
    for (p, key), at in list(state_cache._written_at.items()):
        if p == path:
            state_cache._written_at[(p, key)] = at - state_cache.REWRITE_SECONDS


def _counting_writes(monkeypatch):
    writes = []
    real = state_cache.save_json_atomic
    monkeypatch.setattr(state_cache, "save_json_atomic", lambda path, data: (writes.append(path), real(path, data)))
    return writes


def test_a_chatty_topic_is_written_once_every_few_minutes(tmp_path, monkeypatch):
    # A meter reporting every few seconds had the whole file rewritten and
    # fsynced every 5 s - ~17,000 times a day on the production board.
    path = str(tmp_path / "mqtt_state_cache.json")
    writes = _counting_writes(monkeypatch)
    for i in range(30):                                  # one every 2 s
        save_cached(path, BROKER, "meter/status/em:0", {"w": 300 + i}, 1000.0 + 2 * i)
        flush(due_only=True)
    assert len(writes) == 2, "its first two messages, then it's known to be chatty"
    _five_minutes_on(path)
    flush(due_only=True)
    assert len(writes) == 3
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["192.168.1.21:1883/meter/status/em:0"]["payload"] == {"w": 329}


def test_news_from_a_quiet_topic_is_written_at_once(tmp_path, monkeypatch):
    path = str(tmp_path / "mqtt_state_cache.json")
    for i in range(5):
        save_cached(path, BROKER, "meter/status/em:0", {"w": i}, 1000.0 + i)
    save_cached(path, BROKER, "valve/status/switch:0", {"output": False}, 1000.0)
    flush(due_only=True)
    writes = _counting_writes(monkeypatch)
    save_cached(path, BROKER, "meter/status/em:0", {"w": 99}, 1006.0)
    assert flush(due_only=True) > 0 and writes == [], "the meter waits"
    save_cached(path, BROKER, "valve/status/switch:0", {"output": True}, 1300.0)   # after five quiet minutes
    assert flush(due_only=True) is None and writes == [path]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["192.168.1.21:1883/valve/status/switch:0"]["payload"] == {"output": True}
    assert data["192.168.1.21:1883/meter/status/em:0"]["payload"] == {"w": 99}, "the meter's newest went with it"


def test_a_change_right_after_another_is_still_news(tmp_path, monkeypatch):
    path = str(tmp_path / "mqtt_state_cache.json")
    save_cached(path, BROKER, "valve/status/switch:0", {"output": True}, 1000.0)
    flush(due_only=True)
    writes = _counting_writes(monkeypatch)
    save_cached(path, BROKER, "valve/status/switch:0", {"output": False}, 1010.0)   # off 10 s later
    flush(due_only=True)
    assert writes == [path]
    assert load_cached(path, BROKER, "valve/status/switch:0") == ({"output": False}, 1010.0)


def test_stopping_writes_even_a_chatty_topic(tmp_path):
    path = str(tmp_path / "mqtt_state_cache.json")
    for i in range(5):
        save_cached(path, BROKER, "meter/status/em:0", {"w": i}, 1000.0 + i)
        flush(due_only=True)
    flush()                                              # what runs at exit
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["192.168.1.21:1883/meter/status/em:0"]["payload"] == {"w": 4}


def test_a_write_that_failed_is_tried_again(tmp_path, monkeypatch):
    cache_path = str(tmp_path / "mqtt_state_cache.json")
    real = state_cache.save_json_atomic

    def full_disk(path, data):
        raise OSError("No space left on device")
    monkeypatch.setattr(state_cache, "save_json_atomic", full_disk)
    save_cached(cache_path, BROKER, "shelly/status/switch:0", {"output": True}, 1000.0)
    flush()
    monkeypatch.setattr(state_cache, "save_json_atomic", real)
    flush()
    with open(cache_path, encoding="utf-8") as f:
        assert "192.168.1.21:1883/shelly/status/switch:0" in json.load(f)
