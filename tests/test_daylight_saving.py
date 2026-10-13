"""The two nights a year the clocks change, on the board's clock (Europe/
Rome): on the last Sunday of October the hour from 2 to 3 comes twice -
a day of 25 hours - and on the last Sunday of March it never comes - 23.
The weekly table follows the clock on the wall; a length of time ("22°
for 2 hours") is real time; a day's totals count the whole day it had."""
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from localhome.services.day_range import DayRange
from localhome.services.history import HistoryStore, run_days_of, temp_days_of
from localhome.services.system_clock import local_after, server_time
from localhome.services.thermostat import ScheduleStore
from localhome.services.vmc_schedule import next_change as vmc_next_change
from tests.test_thermostat import _room
from tests.test_vmc_schedule import _controller as _vmc, week_of


def _utc(*fields):
    return datetime(*fields, tzinfo=timezone.utc).timestamp()


def _follows_rome() -> bool:
    """This process's local time keeps Rome's hours: +1 in winter, +2 in
    summer, changing on the same nights."""
    return (datetime(2026, 1, 15, 12).timestamp() == _utc(2026, 1, 15, 11)
            and datetime(2026, 7, 15, 12).timestamp() == _utc(2026, 7, 15, 10)
            and datetime(2026, 10, 26).timestamp() - datetime(2026, 10, 25).timestamp() == 25 * 3600
            and datetime(2026, 3, 30).timestamp() - datetime(2026, 3, 29).timestamp() == 23 * 3600)


@pytest.fixture(autouse=True)
def rome():
    """The board's zone for this process where it can be set (Linux, the
    board); on Windows it can't, and the machine's own has to keep the
    same hours (an Italian PC does) - or these say why they don't run."""
    if hasattr(time, "tzset"):
        before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Rome"
        time.tzset()
        yield
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()
        return
    if not _follows_rome():
        pytest.skip("this machine's time zone doesn't keep Rome's hours, and Windows can't set one per process")
    yield


# 00:30 UTC on 25 October is 02:30 the first time (summer time, +2), 01:30
# UTC is 02:30 again (winter time, +1).
OCT_0230_FIRST = _utc(2026, 10, 25, 0, 30)
OCT_0230_AGAIN = _utc(2026, 10, 25, 1, 30)


def _sunday_table(tmp_path, rows):
    """Rooms' week: 16° every hour, Sundays' `rows` {hour: target} apart."""
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    week = ScheduleStore.empty_week()
    for day in week:
        week[day] = [16.0] * 24
    for hour, target in rows.items():
        week["sun"][hour] = target
    store.set_week("Rooms", week)
    return store


def _every_quarter(start_ts, end_ts):
    return [datetime.fromtimestamp(ts) for ts in range(int(start_ts), int(end_ts), 900)]


# ------------------------------------------------------- the weekly table --

def test_in_october_the_hour_from_2_to_3_keeps_its_target_both_times(tmp_path):
    controller, store, _ = _room(tmp_path, temperature=18.0, scheduled=16.0)
    week = store.get_week("Rooms")
    week["sun"][2] = 22.0
    store.set_week("Rooms", week)
    applied = []
    for now in _every_quarter(_utc(2026, 10, 24, 22), _utc(2026, 10, 25, 4)):   # 00:00 - 05:00
        controller.tick(now=now)
        applied.append(controller.applied_target("Rooms"))
    assert applied.count(22.0) == 8, "two real hours: 02:00-03:00 twice"
    assert applied[8:16] == [22.0] * 8, "from the first 02:00 to the second 03:00"


def test_in_march_the_hour_the_clock_skips_never_applies(tmp_path):
    controller, store, _ = _room(tmp_path, temperature=18.0, scheduled=16.0)
    week = store.get_week("Rooms")
    week["sun"][2] = 22.0
    store.set_week("Rooms", week)
    for now in _every_quarter(_utc(2026, 3, 28, 23), _utc(2026, 3, 29, 3)):     # 00:00 - 05:00
        controller.tick(now=now)
        assert controller.applied_target("Rooms") == 16.0, now


def test_until_the_next_change_skips_the_hour_march_leaves_out(tmp_path):
    store = _sunday_table(tmp_path, {h: 22.0 for h in range(2, 7)})
    assert store.next_change("Rooms", datetime(2026, 3, 29, 1, 30)) == datetime(2026, 3, 29, 3, 0), \
        "02:00 never comes that night: the table changes at 03:00"
    assert store.next_change("Rooms", datetime(2026, 3, 22, 1, 30)) == datetime(2026, 3, 22, 2, 0), "a week before"


def test_until_the_next_change_in_october_ends_at_the_first_2_oclock(tmp_path):
    store = _sunday_table(tmp_path, {h: 22.0 for h in range(2, 7)})
    one_thirty = datetime.fromtimestamp(_utc(2026, 10, 24, 23, 30))
    until = store.next_change("Rooms", one_thirty)
    assert until == datetime(2026, 10, 25, 2, 0)
    store.set_override("Rooms", 20.0, until)
    assert "Rooms" in store.get_overrides(datetime.fromtimestamp(_utc(2026, 10, 24, 23, 59)))
    assert store.get_overrides(datetime.fromtimestamp(_utc(2026, 10, 25, 0, 1))) == {}, \
        "over when the table changes - the first 02:00, not the second"


# ------------------------------------------------- "22° for 2 hours" ----

def test_two_hours_by_hand_last_two_hours_the_night_october_repeats_one(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    one_thirty = datetime.fromtimestamp(_utc(2026, 10, 24, 23, 30))       # 01:30 summer time
    until = local_after(one_thirty, 2 * 3600)
    assert (until.hour, until.minute, until.timestamp()) == (2, 30, OCT_0230_AGAIN), "the second 02:30"
    store.set_override("Rooms", 22.0, until)
    assert "Rooms" in store.get_overrides(datetime.fromtimestamp(OCT_0230_FIRST + 900)), "an hour and a quarter in"
    store.drop_expired_overrides(datetime.fromtimestamp(OCT_0230_FIRST + 900))
    assert "Rooms" in store.get_overrides(datetime.fromtimestamp(OCT_0230_AGAIN - 60))
    assert store.get_overrides(datetime.fromtimestamp(OCT_0230_AGAIN + 60)) == {}, "two hours, not three"
    shown = store.get_overrides(datetime.fromtimestamp(OCT_0230_AGAIN - 60))["Rooms"]["until"]
    assert (shown.hour, shown.minute) == (2, 30), "said as the clock on the wall says it"


def test_two_hours_by_hand_last_two_hours_the_night_march_skips_one():
    one_thirty = datetime.fromtimestamp(_utc(2026, 3, 29, 0, 30))          # 01:30 winter time
    until = local_after(one_thirty, 2 * 3600)
    assert (until.hour, until.minute) == (4, 30), "02:30 never comes: two real hours end at 04:30"


def test_an_end_written_before_it_kept_its_offset_still_counts(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    data = store._load()
    data["overrides"] = {"Rooms": {"target": 22.0, "until": "2026-10-13T18:00:00"}}
    store._save(data)
    assert "Rooms" in store.get_overrides(datetime(2026, 10, 13, 17, 59))
    assert store.get_overrides(datetime(2026, 10, 13, 18, 0)) == {}


# --------------------------------------------------------- the VMC's boost --

def test_an_hours_boost_the_night_october_repeats_one_lasts_an_hour(tmp_path):
    controller, number, _ = _vmc(tmp_path)
    first = datetime.fromtimestamp(OCT_0230_FIRST)
    controller.tick(now=first)
    controller.boost("VMC Speed", 100, 60, now=first)
    assert number.values == [20, 100]
    assert controller.status(now=first)["override"]["until_hhmm"] == "02:30", "the second 02:30"
    controller.tick(now=datetime.fromtimestamp(OCT_0230_AGAIN - 60))
    assert number.values == [20, 100], "still on"
    controller.tick(now=datetime.fromtimestamp(OCT_0230_AGAIN + 60))
    assert number.values == [20, 100, 20], "an hour, not two"


def test_an_hours_boost_the_night_march_skips_one_lasts_an_hour(tmp_path):
    controller, number, _ = _vmc(tmp_path)
    one_thirty = datetime.fromtimestamp(_utc(2026, 3, 29, 0, 30))         # 01:30 winter time
    controller.tick(now=one_thirty)
    controller.boost("VMC Speed", 100, 60, now=one_thirty)
    assert controller.status(now=one_thirty)["override"]["until_hhmm"] == "03:30", "02:30 never comes"
    controller.tick(now=datetime.fromtimestamp(_utc(2026, 3, 29, 1, 20)))   # 03:20 summer time
    assert number.values == [20, 100], "fifty minutes in: still on - it ended after thirty"


def test_the_vmc_programs_next_change_skips_the_hour_march_leaves_out():
    week = week_of(sun=[(2, 7, 50)])
    assert vmc_next_change(week, datetime(2026, 3, 29, 1, 30)) == (datetime(2026, 3, 29, 3, 0), 50)
    assert vmc_next_change(week, datetime(2026, 3, 22, 1, 30)) == (datetime(2026, 3, 22, 2, 0), 50)


# ---------------------------------------------------- a day's own hours --

def _samples(store, start_ts, end_ts, value, sensor="valve", field="is_on", every=300):
    conn = store._connect()
    conn.executemany("INSERT INTO readings (ts, sensor, field, value) VALUES (?, ?, ?, ?)",
                     [(ts, sensor, field, value) for ts in range(int(start_ts), int(end_ts), every)])
    conn.commit()
    conn.close()


@pytest.mark.parametrize("day, hours", [(date(2026, 10, 25), 25), (date(2026, 3, 29), 23), (date(2026, 10, 18), 24)])
def test_a_day_on_all_its_hours_counts_the_hours_it_had(tmp_path, day, hours):
    store = HistoryStore(str(tmp_path / "history.db"))
    start = datetime(day.year, day.month, day.day).timestamp()
    end = (datetime(day.year, day.month, day.day) + timedelta(days=1)).timestamp()
    assert end - start == hours * 3600
    _samples(store, start, end, 1.0)
    runs = run_days_of(store, ["valve"], "is_on", 300, day, now=datetime(day.year, day.month, day.day) + timedelta(days=1, hours=12))
    assert runs["valve"][day.isoformat()] == (hours * 3600, 1)
    store.close()


def test_the_two_2_30s_of_october_are_two_moments_in_the_days_lows_and_highs(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    _samples(store, datetime(2026, 10, 25).timestamp(), datetime(2026, 10, 26).timestamp(), 20.0,
             sensor="room", field="temperature_c")
    conn = store._connect()
    conn.execute("UPDATE readings SET value = 17.0 WHERE ts = ?", (int(OCT_0230_FIRST),))
    conn.execute("UPDATE readings SET value = 23.0 WHERE ts = ?", (int(OCT_0230_AGAIN),))
    conn.commit()
    conn.close()
    days = temp_days_of(store, "room", "temperature_c", 300, date(2026, 10, 25), now=datetime(2026, 10, 26, 12))
    mean, low, high, low_at, high_at = days["2026-10-25"]
    assert (low, high, low_at, high_at) == (17.0, 23.0, int(OCT_0230_FIRST), int(OCT_0230_AGAIN))
    store.close()


def test_today_since_midnight_on_the_long_day_is_thirteen_hours_at_noon(tmp_path):
    store = HistoryStore(str(tmp_path / "history.db"))
    _samples(store, datetime(2026, 10, 25).timestamp() - 3600, datetime(2026, 10, 25, 12).timestamp(), 1.0)
    stats = store.on_stats("valve", "is_on", 300, now=datetime(2026, 10, 25, 12))
    assert stats["today"]["hours"] == 13, "00:00 summer time to 12:00 winter time"
    store.close()


def test_todays_lowest_and_highest_cover_the_whole_long_day():
    clock = {"now": _utc(2026, 10, 25, 22, 45)}                          # 23:45 winter time
    ranges = DayRange([("room", "temperature_c")], clock=lambda: clock["now"])
    ranges.sample(_utc(2026, 10, 24, 22, 30), {"room": {"temperature_c": 17.0}})   # 00:30 summer time
    ranges.sample(OCT_0230_AGAIN, {"room": {"temperature_c": 20.0}})
    ranges.sample(_utc(2026, 10, 25, 22, 30), {"room": {"temperature_c": 23.0}})   # 23:30 winter time
    assert ranges.get("room", "temperature_c") == {"min": 17.0, "max": 23.0, "min_at": int(_utc(2026, 10, 24, 22, 30)),
                                                   "max_at": int(_utc(2026, 10, 25, 22, 30))}
    clock["now"] = _utc(2026, 10, 25, 23, 10)                            # 00:10 the next day
    ranges.sample(clock["now"], {"room": {"temperature_c": 19.0}})
    assert ranges.get("room", "temperature_c") is None, "a new day, one value so far"


def test_the_clock_the_pages_show_changes_its_offset_with_the_hour():
    assert server_time(datetime.fromtimestamp(OCT_0230_FIRST))["utc_offset_minutes"] == 120
    assert server_time(datetime.fromtimestamp(OCT_0230_AGAIN))["utc_offset_minutes"] == 60
    assert server_time(datetime(2026, 3, 29, 1, 30))["utc_offset_minutes"] == 60
    assert server_time(datetime(2026, 3, 29, 3, 30))["utc_offset_minutes"] == 120
