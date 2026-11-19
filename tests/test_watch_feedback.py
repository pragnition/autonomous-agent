import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.watch import runner_feedback

SYSTEM_MD = Path(__file__).resolve().parent.parent / "src" / "agent" / "defaults" / "system.md"

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(value: datetime) -> str:
    return value.strftime(_FMT)


def _touch(path, text: str, when: datetime) -> None:
    path.write_text(text)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


# -- collect --


def test_runner_feedback_collect_window_and_skips_changes(tmp_path, capsys):
    _touch(tmp_path / "2026-11-19-inside.md", "note inside the window", T0 - timedelta(minutes=30))
    _touch(tmp_path / "2026-11-19-outside.md", "note outside the window", T0 - timedelta(hours=2))
    _touch(tmp_path / "CHANGES.md", "# What changed for Jensen\n\n- 2026-11-19: something\n", T0)

    since, until = T0 - timedelta(hours=1), T0
    rc = runner_feedback.main(
        ["collect", "--since", _iso(since), "--until", _iso(until), "--root", str(tmp_path)])

    assert rc == 0
    notes = json.loads(capsys.readouterr().out)
    assert {n["name"] for n in notes} == {"2026-11-19-inside.md"}
    [note] = notes
    assert note["text"] == "note inside the window"
    assert note["modified"] == _iso(T0 - timedelta(minutes=30))


def test_runner_feedback_collect_missing_folder_gives_empty_list(tmp_path):
    notes = runner_feedback.collect(tmp_path / "missing", _iso(T0 - timedelta(hours=1)), _iso(T0))
    assert notes == []


# -- reply --


def test_reply_appends_status_and_rejects_bad_names(tmp_path):
    note = tmp_path / "2026-11-19-thing.md"
    note.write_text("what happened and what would have helped")

    runner_feedback.reply(tmp_path, "2026-11-19-thing.md", "fixed in the next deploy", T0)

    assert note.read_text() == (
        "what happened and what would have helped\nStatus (2026-11-19T10:00:00Z): fixed in the next deploy\n")

    for bad_name in ("../x.md", "a/b.md", "x.txt"):
        with pytest.raises(ValueError):
            runner_feedback.reply(tmp_path, bad_name, "status", T0)


# -- add_change --


def test_add_change_prepends_and_keeps_30(tmp_path):
    for n in range(31):
        runner_feedback.add_change(tmp_path, f"change {n}", T0 + timedelta(days=n))

    text = (tmp_path / "CHANGES.md").read_text()
    assert text.startswith(runner_feedback.CHANGES_HEADING + "\n\n")

    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 30
    # Newest first: change 30 was added last, with the latest date.
    assert lines[0].endswith("change 30")
    assert lines[-1].endswith("change 1")
    assert "change 0" not in text


# -- the prompt line (system.md) --


def test_system_prompt_asks_for_feedback_notes():
    text = SYSTEM_MD.read_text()
    assert "/var/agent/feedback/" in text
    assert "CHANGES.md" in text
    assert len(SYSTEM_MD.read_bytes()) < 12_288
