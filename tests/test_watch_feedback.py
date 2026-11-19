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


def test_collect_truncates_a_long_note_and_marks_it(tmp_path):
    long_text = "x" * (runner_feedback.MAX_NOTE_CHARS + 500)
    _touch(tmp_path / "2026-11-19-long.md", long_text, T0 - timedelta(minutes=10))

    [note] = runner_feedback.collect(tmp_path, _iso(T0 - timedelta(hours=1)), _iso(T0))

    assert note["text"].startswith("x" * runner_feedback.MAX_NOTE_CHARS)
    assert "[truncated]" in note["text"]
    assert len(note["text"]) < len(long_text)


def test_collect_survives_a_badly_encoded_note(tmp_path, capsys):
    # A single note with invalid UTF-8 bytes must not crash the whole window's collection (it used to: an
    # uncaught UnicodeDecodeError inside the container meant `docker exec` failed and every note in the window
    # -- not just the bad one -- was lost).
    bad = tmp_path / "2026-11-19-bad-encoding.md"
    bad.write_bytes(b"what happened: \xff\xfe garbled bytes")
    os.utime(bad, ((T0 - timedelta(minutes=10)).timestamp(),) * 2)
    _touch(tmp_path / "2026-11-19-fine.md", "a normal note", T0 - timedelta(minutes=5))

    notes = runner_feedback.collect(tmp_path, _iso(T0 - timedelta(hours=1)), _iso(T0))

    assert {n["name"] for n in notes} == {"2026-11-19-bad-encoding.md", "2026-11-19-fine.md"}


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


def test_reply_restores_mtime_so_the_note_is_not_recollected_next_window(tmp_path):
    # Appending a Status line naturally bumps a file's own mtime; left alone, that would push the note's mtime
    # into the *next* window, so the next hourly `collect` hands the already-answered note back as if it were
    # new, forever. `reply` must restore the original mtime so this doesn't happen.
    note_time = T0 - timedelta(minutes=30)
    _touch(tmp_path / "2026-11-19-thing.md", "what happened", note_time)

    window1 = runner_feedback.collect(tmp_path, _iso(T0 - timedelta(hours=1)), _iso(T0))
    assert [n["name"] for n in window1] == ["2026-11-19-thing.md"]

    runner_feedback.reply(tmp_path, "2026-11-19-thing.md", "fixed in the next deploy", T0)

    window2 = runner_feedback.collect(tmp_path, _iso(T0), _iso(T0 + timedelta(hours=1)))
    assert window2 == []


def test_reply_on_a_missing_note_fails_without_creating_it(tmp_path):
    with pytest.raises(FileNotFoundError):
        runner_feedback.reply(tmp_path, "2026-11-19-missing.md", "status", T0)

    assert not (tmp_path / "2026-11-19-missing.md").exists()


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


def test_add_change_flattens_embedded_whitespace(tmp_path):
    runner_feedback.add_change(tmp_path, "line one\nline two   with   extra   spaces\t\ttabs", T0)

    text = (tmp_path / "CHANGES.md").read_text()
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert lines == ["- 2026-11-19: line one line two with extra spaces tabs"]


# -- the prompt line (system.md) --


def test_system_prompt_asks_for_feedback_notes():
    text = SYSTEM_MD.read_text()
    assert "/var/agent/feedback/" in text
    assert "CHANGES.md" in text
    assert len(SYSTEM_MD.read_bytes()) < 12_288
