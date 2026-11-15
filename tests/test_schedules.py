from datetime import datetime
from zoneinfo import ZoneInfo

from agent.core.schedules import Schedule, latest_slot, main, next_slots, parse

SGT = ZoneInfo("Asia/Singapore")

EXAMPLE = '''
[project-sweep]
cron = "0 */3 * * *"
channel = "#project-dev"
prompt = """
Dogfood the project on main.
"""
by = "sam"
link = "https://example.slack.com/archives/C1/p1"
paused = true
'''

VALID = '''
[ok]
cron = "0 9 * * *"
channel = "C1"
prompt = "Do it"
by = "ann"
'''


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=SGT)


# parsing

def test_parse_valid_entry():
    schedules, errors = parse(EXAMPLE)
    assert errors == []
    assert schedules == [Schedule(name="project-sweep", cron="0 */3 * * *", channel="#project-dev",
                                  prompt="Dogfood the project on main.\n", by="sam",
                                  link="https://example.slack.com/archives/C1/p1", paused=True)]


def test_parse_reports_each_bad_entry_and_keeps_good_ones():
    bad = {
        "no-prompt": 'cron = "0 9 * * *"\nchannel = "C1"\nby = "ann"',
        "six-fields": 'cron = "0 0 9 * * *"\nchannel = "C1"\nprompt = "p"\nby = "ann"',
        "bad-cron": 'cron = "61 * * * *"\nchannel = "C1"\nprompt = "p"\nby = "ann"',
        "unknown-key": 'cron = "0 9 * * *"\nchannel = "C1"\nprompt = "p"\nby = "ann"\nwhen = 1',
        "Bad_Name": 'cron = "0 9 * * *"\nchannel = "C1"\nprompt = "p"\nby = "ann"',
        "bad-paused": 'cron = "0 9 * * *"\nchannel = "C1"\nprompt = "p"\nby = "ann"\npaused = "yes"',
    }
    text = VALID + "".join(f"\n[{name}]\n{body}\n" for name, body in bad.items())
    schedules, errors = parse(text)
    assert [s.name for s in schedules] == ["ok"]
    assert len(errors) == len(bad)
    for name, error in zip(bad, errors):
        assert error.startswith(f"{name}: "), error


def test_parse_top_level_value_is_an_error():
    schedules, errors = parse('timezone = "UTC"\n' + VALID)
    assert [s.name for s in schedules] == ["ok"]
    assert len(errors) == 1 and "timezone" in errors[0]


def test_parse_invalid_toml_returns_none():
    schedules, errors = parse("[x\n")
    assert schedules is None
    assert len(errors) == 1 and errors[0].startswith("not valid TOML")


def test_parse_empty_text_is_no_schedules():
    assert parse("") == ([], [])


# slots

def test_latest_slot_counts_now_and_previous():
    assert latest_slot("0 9 * * *", at(16, 9, 0, 30)) == at(16, 9)
    assert latest_slot("0 9 * * *", at(16, 8, 59)) == at(15, 9)


def test_next_slots():
    assert next_slots("0 */3 * * *", at(16, 10, 15)) == [at(16, 12), at(16, 15), at(16, 18)]


# the check command

def test_cli_prints_next_runs_and_exits_zero(tmp_path, capsys):
    path = tmp_path / "schedules.toml"
    path.write_text(VALID)
    assert main(["--file", str(path), "--timezone", "Asia/Singapore"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "0 9 * * *" in out and "Asia/Singapore" in out
    assert out.count(" 09:00") == 3


def test_cli_exits_nonzero_on_errors(tmp_path, capsys):
    path = tmp_path / "schedules.toml"
    path.write_text(VALID + '\n[broken]\ncron = "nope"\nchannel = "C1"\nprompt = "p"\nby = "ann"\n')
    assert main(["--file", str(path), "--timezone", "Asia/Singapore"]) == 1
    out = capsys.readouterr().out
    assert "broken:" in out and "ok" in out


def test_cli_missing_file(tmp_path, capsys):
    assert main(["--file", str(tmp_path / "none.toml"), "--timezone", "UTC"]) == 0
    assert "no schedules file" in capsys.readouterr().out


# the scheduler

import pytest

from agent import db
from agent.core.schedules import Scheduler


class Recorder:
    def __init__(self, fail_times: int = 0, error: str = "boom"):
        self.fired, self.reports, self.fail_times, self.error = [], [], fail_times, error

    async def fire(self, schedule):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError(self.error)
        self.fired.append(schedule.name)

    async def report(self, text):
        self.reports.append(text)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def entry(name="daily", cron="0 9 * * *", extra=""):
    return f'[{name}]\ncron = "{cron}"\nchannel = "C1"\nprompt = "p"\nby = "ann"\n{extra}\n'


def make_scheduler(conn, tmp_path, rec):
    return Scheduler(conn, path=tmp_path / "schedules.toml", timezone="Asia/Singapore", fire=rec.fire,
                     report=rec.report)


async def test_first_sight_records_without_firing(conn, tmp_path):
    rec = Recorder()
    (tmp_path / "schedules.toml").write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    assert await s.tick(at(16, 9, 30)) == []
    assert await s.tick(at(17, 9)) == ["daily"]
    assert rec.fired == ["daily"]


async def test_fires_once_per_slot(conn, tmp_path):
    rec = Recorder()
    (tmp_path / "schedules.toml").write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8, 59))
    for t in (at(16, 9), at(16, 9, 1), at(16, 9, 59)):
        await s.tick(t)
    assert rec.fired == ["daily"]


async def test_downtime_fires_once_on_return(conn, tmp_path):
    rec = Recorder()
    (tmp_path / "schedules.toml").write_text(entry())
    await make_scheduler(conn, tmp_path, rec).tick(at(16, 9))
    await make_scheduler(conn, tmp_path, rec).tick(at(19, 10))  # a restarted core
    assert rec.fired == ["daily"]


async def test_changed_cron_waits_for_its_next_slot(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 9))
    path.write_text(entry(cron="0 */3 * * *"))
    await s.tick(at(16, 13))
    assert rec.fired == []
    await s.tick(at(16, 15))
    assert rec.fired == ["daily"]


async def test_paused_and_stopped_slots_are_consumed(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry(extra="paused = true"))
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    await s.tick(at(16, 9))            # due while paused
    path.write_text(entry())
    await s.tick(at(16, 9, 5))         # unpaused just after: no backlog
    assert rec.fired == []
    db.set_setting(conn, "paused", "1")
    await s.tick(at(17, 9))            # due while the agent is stopped
    db.set_setting(conn, "paused", "0")
    await s.tick(at(17, 9, 5))
    assert rec.fired == []
    await s.tick(at(18, 9))
    assert rec.fired == ["daily"]


async def test_broken_file_keeps_last_valid_set_and_reports_once(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    path.write_text("[x\n")
    await s.tick(at(16, 9))
    await s.tick(at(16, 9, 1))
    assert rec.fired == ["daily"]
    assert len(rec.reports) == 1 and "not valid TOML" in rec.reports[0]
    path.write_text(entry())
    await s.tick(at(16, 9, 2))
    assert len(rec.reports) == 1 and db.get_setting(conn, "schedules.errors_hash") == ""


async def test_failed_firing_retries_and_reports_once(conn, tmp_path):
    rec = Recorder(fail_times=2, error="not_in_channel")
    (tmp_path / "schedules.toml").write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    for t in (at(16, 9), at(16, 9, 1), at(16, 9, 2), at(16, 9, 3)):
        await s.tick(t)
    assert rec.fired == ["daily"]
    assert len(rec.reports) == 1 and "not_in_channel" in rec.reports[0]


async def test_missing_file_is_quiet(conn, tmp_path):
    rec = Recorder()
    s = make_scheduler(conn, tmp_path, rec)
    assert await s.tick(at(16, 9)) == []
    assert rec.fired == [] and rec.reports == []


# review fixes

async def test_removed_and_readded_schedule_waits_for_its_next_slot(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    path.write_text("")
    await s.tick(at(16, 10))
    path.write_text(entry())
    await s.tick(at(20, 14))           # back days later: not straight away
    assert rec.fired == []
    await s.tick(at(21, 9))
    assert rec.fired == ["daily"]


async def test_invalid_entry_fixed_later_waits_for_its_next_slot(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    path.write_text(entry(extra="when = 1"))
    await s.tick(at(16, 10))
    path.write_text(entry())
    await s.tick(at(18, 14))
    assert rec.fired == []


def test_parse_reports_a_cron_that_never_runs():
    schedules, errors = parse(entry(name="feb-30", cron="0 9 30 2 *") + VALID)
    assert [x.name for x in schedules] == ["ok"]
    assert len(errors) == 1 and errors[0].startswith("feb-30: ") and "never" in errors[0]


async def test_unreadable_file_keeps_last_valid_set_and_reports_once(conn, tmp_path):
    rec = Recorder()
    path = tmp_path / "schedules.toml"
    path.write_text(entry())
    s = make_scheduler(conn, tmp_path, rec)
    await s.tick(at(16, 8))
    path.write_bytes(entry().encode() + b"# caf\xe9\n")
    await s.tick(at(16, 9))
    await s.tick(at(16, 9, 1))
    assert rec.fired == ["daily"]
    assert len(rec.reports) == 1 and "can't read" in rec.reports[0]


def test_cli_reports_an_unreadable_file(tmp_path, capsys):
    path = tmp_path / "schedules.toml"
    path.write_bytes(b"\xff\xfe")
    assert main(["--file", str(path), "--timezone", "UTC"]) == 1
    assert "can't read" in capsys.readouterr().out
