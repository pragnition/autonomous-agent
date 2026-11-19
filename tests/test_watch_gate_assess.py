import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from agent.watch.cli import main
from agent.watch.state import State

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if line)


# -- assess-check --


class _AssessHost:
    """A host for `assess-check` wiring: `core_python` answers with a canned `pool`, `runner_python` answers
    with canned notes (`feedback.collect` parses its stdout as a JSON list of `{name, text, modified}`)."""

    def __init__(self, pool: str = "normal", notes: list | None = None, core_raises: bool = False):
        self.pool = pool
        self.notes = notes if notes is not None else []
        self.core_raises = core_raises
        self.core_calls: list[list[str]] = []
        self.runner_calls: list[list[str]] = []

    def core_python(self, module_file, args):
        self.core_calls.append(list(args))
        if self.core_raises:
            raise RuntimeError("core is down")
        return json.dumps({"pool": self.pool})

    def runner_python(self, module_file, args):
        self.runner_calls.append(list(args))
        return json.dumps(self.notes)


def test_assess_check_first_time_and_nothing_new(tmp_path, capsys):
    state = State(tmp_path / "state")
    host = _AssessHost()

    # Never assessed: has_work is true no matter what else is (or isn't) going on.
    rc = main(["assess-check"], host=host, state=state, now=T0)
    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["has_work"] == "true"
    assert kv["skip_reason"] == ""
    assert kv["started"] == "2026-11-19T10:00:00Z"  # what assess-commit --at records once the run is done

    # Mark it assessed; nothing has happened since -- quiet.
    state.set_last_assess(T0)
    rc2 = main(["assess-check"], host=host, state=state, now=T0 + timedelta(hours=1))
    assert rc2 == 0
    kv2 = _kv(capsys.readouterr().out)
    assert kv2["has_work"] == "false"
    assert kv2["skip_reason"] == "quiet"
    assert kv2["started"] == "2026-11-19T11:00:00Z"


def test_assess_check_new_feedback_counts_as_work(tmp_path, capsys):
    # A new note from Jensen counts as new work.
    state_dir = tmp_path / "notes-state"
    state = State(state_dir)
    state.set_last_assess(T0)
    host = _AssessHost(notes=[{"name": "2026-11-19-thing.md", "text": "stuck", "modified": "x"}])

    rc = main(["assess-check"], host=host, state=state, now=T0 + timedelta(hours=1))
    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["has_work"] == "true"
    assert kv["skip_reason"] == ""


def test_assess_check_new_digest_counts_as_work(tmp_path, capsys):
    state = State(tmp_path / "state")
    # Set last_assess well in the past, so a digest written *right now* by the test unambiguously postdates it
    # regardless of the sandbox's own wall clock.
    state.set_last_assess(datetime(2000, 1, 1, tzinfo=UTC))
    state.write_digest(_dt(2026, 11, 19, 9, 0, 0), "a digest")
    host = _AssessHost()

    rc = main(["assess-check"], host=host, state=state, now=T0)

    assert rc == 0
    assert _kv(capsys.readouterr().out)["has_work"] == "true"


def test_assess_check_active_project_counts_as_work(tmp_path, capsys):
    state = State(tmp_path / "state")
    state.set_last_assess(T0)
    state.project_mark(42, "active", T0)
    host = _AssessHost()

    rc = main(["assess-check"], host=host, state=state, now=T0 + timedelta(hours=1))

    assert rc == 0
    assert _kv(capsys.readouterr().out)["has_work"] == "true"


def test_assess_check_pool_hold(tmp_path, capsys):
    state = State(tmp_path / "state")
    host = _AssessHost(pool="hold_scheduled")

    # Pool hold wins even on a never-assessed install -- improve-jensen sits the hour out entirely.
    rc = main(["assess-check"], host=host, state=state, now=T0)

    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["has_work"] == "false"
    assert kv["skip_reason"] == "pool_hold"
    assert kv["started"] == "2026-11-19T10:00:00Z"

    host_empty = _AssessHost(pool="empty")
    rc2 = main(["assess-check"], host=host_empty, state=state, now=T0)
    assert _kv(capsys.readouterr().out)["skip_reason"] == "pool_hold"


def test_assess_check_pool_probe_failure_falls_through(tmp_path, capsys):
    # A failed pool read must not be mistaken for a hold -- it falls through to the other signals instead.
    state = State(tmp_path / "state")
    host = _AssessHost(core_raises=True)

    rc = main(["assess-check"], host=host, state=state, now=T0)

    assert rc == 0
    assert _kv(capsys.readouterr().out)["has_work"] == "true"  # never assessed


def test_assess_check_failed_notes_read_counts_as_nothing_new(tmp_path, capsys):
    state = State(tmp_path / "state")
    state.set_last_assess(T0)

    class _FailingNotesHost(_AssessHost):
        def runner_python(self, module_file, args):
            raise RuntimeError("docker exec returned 1: /home/someone/secret")

    host = _FailingNotesHost()

    rc = main(["assess-check"], host=host, state=state, now=T0 + timedelta(hours=1))

    assert rc == 0
    captured = capsys.readouterr()
    kv = _kv(captured.out)
    assert kv["has_work"] == "false"
    assert kv["skip_reason"] == "quiet"
    assert "RuntimeError" in captured.err
    assert "/home/someone/secret" not in captured.err


def test_assess_check_writes_to_github_output(tmp_path):
    state = State(tmp_path / "state")
    host = _AssessHost()
    output_file = tmp_path / "out.txt"

    rc = main(["assess-check", "--github-output", str(output_file)], host=host, state=state, now=T0)

    assert rc == 0
    kv = _kv(output_file.read_text())
    assert kv["has_work"] == "true"
    assert kv["skip_reason"] == ""
    assert kv["started"] == "2026-11-19T10:00:00Z"


def test_assess_check_reads_the_pool_through_the_shared_helper(tmp_path, monkeypatch, capsys):
    from agent.watch import cli

    monkeypatch.setattr(cli, "pool_is_holding", lambda host, now=None: True)
    assert main(["assess-check"], host=_AssessHost(pool="normal"), state=State(tmp_path / "s"), now=T0) == 0
    assert _kv(capsys.readouterr().out)["skip_reason"] == "pool_hold"


def test_cli_imports_no_private_names_from_other_watch_modules():
    import ast
    from pathlib import Path

    import agent.watch.cli as cli

    tree = ast.parse(Path(cli.__file__).read_text())
    private = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and (node.module or "").startswith("agent.watch") for alias in node.names
               if alias.name.startswith("_")]
    assert private == []


def test_state_digest_newer_than(tmp_path):
    import os

    state = State(tmp_path)
    assert state.digest_newer_than(T0) is False  # no digest at all

    path = state.write_digest(_dt(2026, 11, 19, 9, 0, 0), "a digest")
    # Its own mtime decides, not the hour in its name: a window measured late writes its digest late.
    written = T0 + timedelta(minutes=30)
    os.utime(path, (written.timestamp(), written.timestamp()))
    assert state.digest_newer_than(T0) is True
    assert state.digest_newer_than(written) is False
    (tmp_path / "not-a-digest.md").write_text("x")
    assert state.digest_newer_than(written) is False


# -- assess-commit --


def test_assess_commit_sets_last_assess(tmp_path):
    state = State(tmp_path / "state")
    assert state.last_assess() is None

    rc = main(["assess-commit"], host=object(), state=state, now=T0)

    assert rc == 0
    assert state.last_assess() == T0


def test_assess_commit_at_records_when_the_check_started(tmp_path, capsys):
    # Anything that arrives while the assessment runs is after `--at`: the next check still sees it as new.
    state = State(tmp_path / "state")

    assert main(["assess-commit", "--at", "2026-11-19T09:13:00Z"], host=object(), state=state, now=T0) == 0
    assert state.last_assess() == _dt(2026, 11, 19, 9, 13, 0)

    assert main(["assess-commit", "--at", "yesterday"], host=object(), state=state, now=T0) == 2
    assert "--at" in capsys.readouterr().err
    assert state.last_assess() == _dt(2026, 11, 19, 9, 13, 0)


# -- project-mark / State.project_active --


def test_project_mark_round_trip(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 9, 0, 0)

    assert state.project_active() is None

    state.project_mark(42, "active", now)
    assert state.project_active() == {"issue": 42, "started": "2026-11-19T09:00:00Z"}

    # Marking a different issue active replaces the old one outright.
    later = now + timedelta(hours=1)
    state.project_mark(43, "active", later)
    assert state.project_active() == {"issue": 43, "started": "2026-11-19T10:00:00Z"}

    state.project_mark(43, "done", later)
    assert state.project_active() is None


def test_project_mark_bad_status_raises(tmp_path):
    state = State(tmp_path)
    with pytest.raises(ValueError):
        state.project_mark(1, "bogus", _dt(2026, 11, 19, 0, 0, 0))


def test_project_mark_cli(tmp_path):
    state = State(tmp_path / "state")

    rc = main(["project-mark", "--issue", "9", "--status", "active"], host=object(), state=state, now=T0)
    assert rc == 0
    assert state.project_active() == {"issue": 9, "started": "2026-11-19T10:00:00Z"}

    rc2 = main(["project-mark", "--issue", "9", "--status", "done"], host=object(), state=state, now=T0)
    assert rc2 == 0
    assert state.project_active() is None


def test_project_mark_cli_rejects_bad_status(tmp_path):
    state = State(tmp_path / "state")
    with pytest.raises(SystemExit):
        main(["project-mark", "--issue", "9", "--status", "bogus"], host=object(), state=state, now=T0)


# -- seed: the new state labels --


def test_seed_creates_the_new_state_labels(tmp_path):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([{"title": "Something", "labels": ["jensen-watch", "works"], "body": "b"}]))

    class _SeedHost:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, args, **kwargs):
            self.calls.append(list(args))
            if args[1:3] == ["label", "create"]:
                return subprocess.CompletedProcess(args, 0)
            if args[1:3] == ["issue", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout="[]")
            if args[1:3] == ["issue", "create"]:
                return subprocess.CompletedProcess(args, 0, stdout="https://github.com/acme/widgets/issues/1\n")
            raise AssertionError(f"unexpected call: {args}")

    host = _SeedHost()

    rc = main(["seed", str(seed_file), "--repo", "acme/widgets"], host=host, state=State(tmp_path / "state"))

    assert rc == 0
    created = {c[3] for c in host.calls if c[1:3] == ["label", "create"]}
    assert {"proposed", "approved", "rejected", "jensen-assessment"} <= created
