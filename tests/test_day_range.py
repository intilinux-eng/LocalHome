"""Today's outdoor minimum and maximum (services/day_range.py): kept in
memory from the recorder's cycles, the history read once at start-up -
never at a page's refresh, which would write the rows waiting in memory
out early (HistoryStore._connect())."""
import threading
from datetime import datetime

from localhome.services.day_range import DayRange
from localhome.services.history import HistoryStore
from localhome.web.app import create_app

OUT = ("Outdoor Sensor", "temperature_c")


def _ts(hour, minute=0, day=8):
    return datetime(2026, 10, day, hour, minute).timestamp()


def test_the_lowest_and_highest_of_today_from_each_cycle():
    clock = {"now": _ts(14)}
    days = DayRange([OUT], clock=lambda: clock["now"])
    assert days.get(*OUT) is None
    days.sample(_ts(6), {"Outdoor Sensor": {"temperature_c": 12.04, "humidity_pct": 80}})
    assert days.get(*OUT) is None, "one value says nothing the reading doesn't"
    days.sample(_ts(14), {"Outdoor Sensor": {"temperature_c": 21.66}, "Other": {"temperature_c": 40.0}})
    days.sample(_ts(9), {"Outdoor Sensor": {"temperature_c": 15.0}})
    assert days.get(*OUT) == {"min": 12.0, "max": 21.7}


def test_the_first_value_after_midnight_starts_the_new_day():
    clock = {"now": _ts(23, 50)}
    days = DayRange([OUT], clock=lambda: clock["now"])
    days.sample(_ts(10), {"Outdoor Sensor": {"temperature_c": 25.0}})
    days.sample(_ts(23, 50), {"Outdoor Sensor": {"temperature_c": 14.0}})
    clock["now"] = _ts(0, 5, day=9)
    assert days.get(*OUT) is None, "yesterday's isn't today's"
    days.sample(_ts(0, 5, day=9), {"Outdoor Sensor": {"temperature_c": 13.5}})
    days.sample(_ts(0, 35, day=9), {"Outdoor Sensor": {"temperature_c": 13.1}})
    assert days.get(*OUT) == {"min": 13.1, "max": 13.5}


def test_start_up_reads_what_today_already_had_from_the_history(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"), write_every_seconds=0)
    store.record("Outdoor Sensor", int(_ts(7, day=7)), {"temperature_c": 2.0})   # yesterday
    store.record("Outdoor Sensor", int(_ts(7)), {"temperature_c": 11.0})
    store.record("Outdoor Sensor", int(_ts(13)), {"temperature_c": 19.5})
    days = DayRange([OUT], clock=lambda: _ts(15))
    days.seed(store)
    assert days.get(*OUT) == {"min": 11.0, "max": 19.5}
    store.close()


def test_the_home_page_gets_it_with_the_readings_and_the_history_isnt_read(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("web:\n  port: 5000\n", encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    client = app.test_client()
    assert client.get("/api/readings").get_json()["day_ranges"] == {}
    now = datetime.now().timestamp()
    app.config["DAY_RANGE"].sample(now - 60, {"Outdoor Sensor": {"temperature_c": 10.0}})
    app.config["DAY_RANGE"].sample(now, {"Outdoor Sensor": {"temperature_c": 18.0}})
    # The request runs on this thread (Flask's test client); the app's own
    # loops may read the history meanwhile, as they always do.
    history_connect = app.config["HISTORY"]._connect
    here, reads = threading.current_thread(), []

    def counted():
        if threading.current_thread() is here:
            reads.append(1)
        return history_connect()
    monkeypatch.setattr(app.config["HISTORY"], "_connect", counted)
    body = client.get("/api/readings").get_json()
    assert body["day_ranges"] == {"Outdoor Sensor": {"temperature_c": {"min": 10.0, "max": 18.0}}}
    assert reads == [], "a page's refresh doesn't read the history"
    monkeypatch.undo()
    app.config["HISTORY"].close()


def test_the_recorder_feeds_it_each_cycle(tmp_path):
    from localhome.services.history import HistoryRecorder

    class Poller:
        def __init__(self, value):
            self.driver = type("D", (), {"history_fields": ("temperature_c",)})()
            self.value = value

        def get(self):
            return {"ok": True, "temperature_c": self.value}

    poller = Poller(9.0)
    manager = type("M", (), {"pollers": {"Outdoor Sensor": poller}})()
    store = HistoryStore(str(tmp_path / "history.db"))
    recorder = HistoryRecorder(manager, store)
    recorder.day_range = DayRange([OUT])
    recorder.poll_once()
    poller.value = 16.5
    recorder.poll_once()
    assert recorder.day_range.get(*OUT) == {"min": 9.0, "max": 16.5}
    store.close()
