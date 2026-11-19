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


# -- gate: State --


def test_gate_start_twice_raises(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 10, 0, 0)

    state.gate_start(7, "repo", "abc123", now)
    assert state.gate(now) == {"issue": 7, "kind": "repo", "handle": "abc123", "started": "2026-11-19T10:00:00Z"}

    with pytest.raises(RuntimeError):
        state.gate_start(8, "host", "backup/path", now)
    # The first gate is still the one on record -- the failed second call changed nothing.
    assert state.gate(now)["issue"] == 7


def test_gate_expires_after_12_hours(tmp_path):
    state = State(tmp_path)
    start = _dt(2026, 11, 19, 0, 0, 0)
    state.gate_start(1, "repo", "sha1", start)

    almost = start + timedelta(hours=11, minutes=59)
    assert state.gate(almost) is not None

    expired = start + timedelta(hours=12, minutes=1)
    assert state.gate(expired) is None

    # An expired gate never blocks a fresh gate_start -- it's as if there were no gate at all.
    state.gate_start(2, "host", "backup", expired)
    assert state.gate(expired)["issue"] == 2


def test_gate_end_removes_it(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 0, 0, 0)
    state.gate_start(1, "repo", "sha1", now)
    assert state.gate(now) is not None

    state.gate_end()

    assert state.gate(now) is None
    # Ending an already-absent gate is not an error.
    state.gate_end()


# -- gate: CLI --


def test_gate_cli_start_status_end(tmp_path, capsys):
    state = State(tmp_path / "state")

    rc = main(["gate", "start", "--issue", "5", "--kind", "repo", "--handle", "abc123"],
              host=object(), state=state, now=T0)
    assert rc == 0
    capsys.readouterr()

    rc_status = main(["gate", "status"], host=object(), state=state, now=T0)
    assert rc_status == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"issue": 5, "kind": "repo", "handle": "abc123", "started": "2026-11-19T10:00:00Z"}

    rc_dup = main(["gate", "start", "--issue", "6", "--kind", "host", "--handle", "xyz"],
                  host=object(), state=state, now=T0)
    assert rc_dup == 1
    assert "already active" in capsys.readouterr().err.lower()

    rc_end = main(["gate", "end"], host=object(), state=state, now=T0)
    assert rc_end == 0
    main(["gate", "status"], host=object(), state=state, now=T0)
    assert capsys.readouterr().out.strip() == "none"

    # Now that the gate is gone, starting a new one works.
    rc_fresh = main(["gate", "start", "--issue", "6", "--kind", "host", "--handle", "xyz"],
                    host=object(), state=state, now=T0)
    assert rc_fresh == 0


def test_gate_start_rejects_bad_kind(tmp_path):
    state = State(tmp_path / "state")
    with pytest.raises(SystemExit):
        main(["gate", "start", "--issue", "1", "--kind", "bogus", "--handle", "x"],
             host=object(), state=state, now=T0)


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

    # Mark it assessed; nothing has happened since -- quiet.
    state.set_last_assess(T0)
    rc2 = main(["assess-check"], host=host, state=state, now=T0 + timedelta(hours=1))
    assert rc2 == 0
    kv2 = _kv(capsys.readouterr().out)
    assert kv2["has_work"] == "false"
    assert kv2["skip_reason"] == "quiet"


def test_assess_check_new_feedback_or_yardstick_result(tmp_path, capsys):
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

    # A new yardstick result counts too, even with no notes and an otherwise quiet hour.
    state2_dir = tmp_path / "yardstick-state"
    state2 = State(state2_dir)
    state2.set_last_assess(T0)
    yardstick_dir = state2_dir / "yardstick"
    yardstick_dir.mkdir()
    result_line = json.dumps({"run": "1", "ts": "2026-11-19T10:30:00Z", "sha": "abc", "task": "t1",
                              "passed": True, "minutes": 2.0, "judged": False})
    (yardstick_dir / "results.jsonl").write_text(result_line + "\n")
    host2 = _AssessHost()

    rc2 = main(["assess-check"], host=host2, state=state2, now=T0 + timedelta(hours=1))
    assert rc2 == 0
    kv2 = _kv(capsys.readouterr().out)
    assert kv2["has_work"] == "true"

    # An old yardstick result (before last_assess) is not new.
    state3_dir = tmp_path / "old-yardstick-state"
    state3 = State(state3_dir)
    state3.set_last_assess(T0 + timedelta(hours=2))
    (state3_dir / "yardstick").mkdir()
    (state3_dir / "yardstick" / "results.jsonl").write_text(result_line + "\n")
    host3 = _AssessHost()

    rc3 = main(["assess-check"], host=host3, state=state3, now=T0 + timedelta(hours=3))
    assert rc3 == 0
    kv3 = _kv(capsys.readouterr().out)
    assert kv3["has_work"] == "false"
    assert kv3["skip_reason"] == "quiet"


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


# -- assess-plan --


def test_assess_plan_outputs_and_clears(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    plan_path = state_dir / "assess-plan.json"
    plan_path.write_text(json.dumps({"project": 123}))

    rc = main(["assess-plan"], host=object(), state=state, now=T0)

    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["project"] == "123"
    assert kv["has_project"] == "true"
    assert not plan_path.exists()

    # A missing or invalid project falls back cleanly, and the file is still removed once read.
    plan_path.write_text(json.dumps({"project": True}))  # a bool doesn't count as a positive int
    rc2 = main(["assess-plan"], host=object(), state=state, now=T0)
    assert rc2 == 0
    kv2 = _kv(capsys.readouterr().out)
    assert kv2["project"] == ""
    assert kv2["has_project"] == "false"
    assert not plan_path.exists()

    # No file at all: same clean fallback, no crash.
    rc3 = main(["assess-plan"], host=object(), state=state, now=T0)
    assert rc3 == 0
    assert _kv(capsys.readouterr().out)["has_project"] == "false"


def test_assess_plan_writes_to_github_output(tmp_path):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "assess-plan.json").write_text(json.dumps({"project": 7}))
    output_file = tmp_path / "out.txt"

    rc = main(["assess-plan", "--github-output", str(output_file)], host=object(), state=state, now=T0)

    assert rc == 0
    kv = _kv(output_file.read_text())
    assert kv["project"] == "7"
    assert kv["has_project"] == "true"


# -- assess-commit --


def test_assess_commit_sets_last_assess(tmp_path):
    state = State(tmp_path / "state")
    assert state.last_assess() is None

    rc = main(["assess-commit"], host=object(), state=state, now=T0)

    assert rc == 0
    assert state.last_assess() == T0


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
    assert {"gating", "priority", "jensen-assessment"} <= created
