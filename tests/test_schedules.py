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
