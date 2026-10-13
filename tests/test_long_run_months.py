"""Weeks of a board in seconds (tools/accelerated_months.py): the factory
climate, every device simulated, its clock run fast - each loop's cycle,
two pages asking, a resident's days. Nothing grows without end - memory,
threads, files, the history - a day costs the same at the end, every day
has its summary, nothing logged as an error. The months, on a board:
the tool itself (`--days 90 --no-trace`)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "accelerated_months", Path(__file__).resolve().parents[1] / "tools" / "accelerated_months.py")
months = importlib.util.module_from_spec(spec)
spec.loader.exec_module(months)


def test_two_weeks_across_new_year_leave_nothing_growing(tmp_path):
    report = months.run(tmp_path, days=14, step=1800, retention=4, ask_every=3 * 3600, start="2026-12-24")
    assert report["problems"] == [], months.summary(report)
    daily = report["daily"]
    assert (daily[0]["date"], daily[-1]["date"]) == ("2026-12-24", "2027-01-06")
    assert daily[-1]["daily_runs"] >= 3 * 13, "each valve's and the plant's day kept, every day"
    assert report["answers"]["/api/climate/zones"]["count"] >= 14 * 8 * 2, "two pages, all along"
    assert report["refused"] == [] and report["errors"] == []


def test_what_grows_a_little_at_every_cycle_is_found(tmp_path, monkeypatch):
    """The run's own check: 2 KB kept at each thermostat cycle - a list
    nobody empties - is said, and where."""
    from localhome.services.thermostat import ThermostatController

    kept = []
    tick = ThermostatController.tick

    def leaky_tick(self, now=None):
        kept.append(bytearray(2048))
        return tick(self, now)

    monkeypatch.setattr(ThermostatController, "tick", leaky_tick)
    report = months.run(tmp_path, days=8, step=1800, retention=2, ask_every=6 * 3600)
    found = [p for p in report["problems"] if p.startswith("memory:")]
    assert found, months.summary(report)
    assert any("leaky_tick" in where or "test_long_run_months" in where for _, _, where in report["growth"])
