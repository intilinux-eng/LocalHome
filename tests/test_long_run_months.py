"""Weeks of a board in seconds (tools/accelerated_months.py): the factory
climate, every device simulated, its clock run fast - each loop's cycle,
two pages asking, a resident's days. Nothing grows without end - memory,
threads, files, the history - a day costs the same at the end, every day
has its summary, nothing logged as an error. The months, on a board:
the tool itself (`--days 90 --no-trace`).

Each run goes in a process of its own: it moves its clock with every
time.sleep in the process, and a thread another test left running (CI's
Python 3.10 had hundreds) pushed it on by days, slowed every answer and
filled what the run measures with its own errors."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "accelerated_months.py"

# The factory climate with 2 KB kept at each thermostat cycle - a list
# nobody empties: argv[1] the tool, argv[2] the run's folder.
LEAKY = """\
import importlib.util
import json
import sys
from pathlib import Path

from localhome.services.thermostat import ThermostatController

spec = importlib.util.spec_from_file_location("accelerated_months", sys.argv[1])
months = importlib.util.module_from_spec(spec)
spec.loader.exec_module(months)
kept, tick = [], ThermostatController.tick


def leaky_tick(self, now=None):
    kept.append(bytearray(2048))
    return tick(self, now)


ThermostatController.tick = leaky_tick
report = months.run(Path(sys.argv[2]), days=8, step=1800, retention=2, ask_every=6 * 3600)
print(json.dumps({"problems": report["problems"], "growth": [where for _, _, where in report["growth"]],
                  "summary": months.summary(report)}))
"""


def _python(*args):
    return subprocess.run([sys.executable, "-X", "utf8", *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", timeout=900)


def test_two_weeks_across_new_year_leave_nothing_growing(tmp_path):
    done = _python(TOOL, "--days", 14, "--step", 1800, "--retention", 4, "--ask-every", 3 * 3600,
                   "--start", "2026-12-24", "--out", tmp_path)
    assert (tmp_path / "report.json").exists(), done.stderr[-3000:]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["problems"] == [], done.stdout
    daily = report["daily"]
    assert (daily[0]["date"], daily[-1]["date"]) == ("2026-12-24", "2027-01-06")
    assert daily[-1]["daily_runs"] >= 3 * 13, "each valve's and the plant's day kept, every day"
    assert report["answers"]["/api/climate/zones"]["count"] >= 14 * 8 * 2, "two pages, all along"
    assert report["refused"] == [] and report["errors"] == []


def test_what_grows_a_little_at_every_cycle_is_found(tmp_path):
    """The run's own check: 2 KB kept at each thermostat cycle - a list
    nobody empties - is said, and where."""
    script = tmp_path / "leaky_tick.py"
    script.write_text(LEAKY, encoding="utf-8")
    done = _python(script, TOOL, tmp_path / "run")
    assert done.returncode == 0, done.stderr[-3000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert any(p.startswith("memory:") for p in report["problems"]), report["summary"]
    assert any("leaky_tick.py" in where for where in report["growth"]), report["summary"]


def _months():
    spec = importlib.util.spec_from_file_location("accelerated_months", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(real, cpu):
    """Nine quiet days, watched from the fourth: only a day's time changes."""
    day = lambda d: {"day": d, "date": f"2026-12-{24 + d}", "real_seconds": real(d), "cpu_seconds": cpu(d),
                     "rss_mb": 80.0, "rows": 1000, "files": {}, "oldest": None, "daily_runs": 3, "daily_temps": 3}
    return {"daily": [day(d) for d in range(9)], "watch_from": 2, "threads": {"after_start": 5, "end": 5},
            "growth_total": None, "traced": False, "retention": 4, "answers": {}, "refused": [], "errors": []}


def test_a_day_slower_only_by_the_clock_on_the_wall_is_no_growth():
    """On a shared machine (CI) the wall clock runs on while other tests
    take the processor: only the run's own processor time tells a day
    that costs more."""
    months = _months()
    busy_neighbours = _report(real=lambda d: 0.5 + d * 0.2, cpu=lambda d: 0.8)
    assert months.problems(busy_neighbours) == []
    really_slower = _report(real=lambda d: 1.0, cpu=lambda d: 0.5 + d * 0.2)
    assert any(p.startswith("a day's cost") for p in months.problems(really_slower))
