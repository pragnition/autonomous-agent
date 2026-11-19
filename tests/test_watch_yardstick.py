import base64
import io
import json
import os
import re
import subprocess
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import db
from agent.core.sessions import WorkQueue
from agent.watch import core_ask, core_items, runner_memory, yardstick
from agent.watch.cli import main
from agent.watch.state import FMT, State, is_improve_jensens_own, summarize

T0 = datetime(2026, 11, 19, 19, 7, 0, tzinfo=UTC)
RUN = "20261119T190700Z-ab12"
BASE = f"/var/agent/yardstick/{RUN}"


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _write_task(root: Path, task_id: str, *, prompt: str = "Fix the bug.", time_limit_min: int | None = None,
                rubric: str | None = None, check: bool = True, hidden: bool = False, build: bool = False) -> Path:
    folder = root / task_id
    (folder / "fixture").mkdir(parents=True)
    (folder / "fixture" / "app.py").write_text("print('hi')\n")
    if build:
        (folder / "fixture" / "build.sh").write_text("git init -q && git add -A && git commit -qm start\n")
    lines = [f"prompt = {json.dumps(prompt)}"]
    if time_limit_min is not None:
        lines.append(f"time_limit_min = {time_limit_min}")
    if rubric is not None:
        lines.append(f"rubric = {json.dumps(rubric)}")
    (folder / "task.toml").write_text("\n".join(lines) + "\n")
    if check:
        (folder / "check").write_text("#!/bin/sh\npython3 -m pytest -q hidden\n")
        (folder / "check").chmod(0o755)
    if hidden:
        (folder / "hidden").mkdir()
        (folder / "hidden" / "test_hidden.py").write_text("def test_x():\n    assert True\n")
    return folder


class FakeClock:
    """`clock`/`sleep` for `yardstick.run`: sleeping only moves the fake clock forward, so a test never waits."""

    def __init__(self):
        self.t = 5_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeHost:
    """Stands in for `Host`: records every docker/git call in order in `calls`, as `(kind, detail...)` tuples,
    and serves the turns' statuses from `status(item_id, poll_number)`. `check_rc(folder)` is the check's exit
    code for a task folder (or an exception for the check's `docker exec` to raise), `existing` the folders the
    copy's own `mkdir` finds already there. `exec_timeouts` records the `timeout` each `core_python` and
    `runner_python` call was given."""

    def __init__(self, tmp_path: Path, *, status=lambda item, poll: "done", check_rc=lambda folder: 0,
                 existing=(), pool: str = "normal"):
        self.live = tmp_path / "live"
        self.status = status
        self.check_rc = check_rc
        self.existing = set(existing)
        self.pool = pool
        self.calls: list[tuple] = []
        self.asks: dict[str, dict] = {}  # item id -> {"args", "text", "folder"}
        self.polls = 0
        self.exec_timeouts: list[tuple[str, object]] = []

    def run(self, args, **kwargs):
        args = list(args)
        if args[:2] == ["git", "-C"]:
            assert args[2] == str(self.live) and args[3:] == ["rev-parse", "HEAD"]
            self.calls.append(("sha",))
            return _cp(stdout="abc1234def\n")
        if args[:4] == ["docker", "exec", "agent-runner", "find"]:
            self.calls.append(("prune", args[4:], kwargs.get("timeout")))
            return _cp()
        if args[:5] == ["docker", "exec", "-i", "agent-runner", "sh"]:
            script = args[-1]
            folder = re.search(r"tar -x -C (\S+)$", script).group(1)
            with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
                members = {m.name: m for m in tar.getmembers()}
            self.calls.append(("tar", folder, sorted(members), script))
            if f" mkdir {folder} " in f" {script} " and folder in self.existing:
                return _cp(returncode=17)  # the plain mkdir refuses a folder that is already there
            return _cp()
        if args[:5] == ["docker", "exec", "agent-runner", "sh", "-c"]:
            script = args[-1]
            cleanup = re.fullmatch(r"rm -rf (\S+)/hidden (\S+)/check", script)
            if cleanup:
                assert cleanup.group(1) == cleanup.group(2)
                self.calls.append(("cleanup", cleanup.group(1)))
                return _cp()
            folder = re.match(r"cd (\S+) && ", script).group(1)
            if script.endswith("./check"):
                assert script == f"cd {folder} && timeout -k 10 600 ./check"
                assert kwargs.get("timeout") == 660  # the host's own backstop, past the one inside the runner
                self.calls.append(("check", folder))
                rc = self.check_rc(folder)
                if isinstance(rc, BaseException):
                    raise rc
                return _cp(returncode=rc)
            if script.endswith("sh build.sh"):
                assert script == f"cd {folder} && timeout -k 10 300 sh build.sh"
                assert kwargs.get("timeout") == 360
                self.calls.append(("build", folder))
                return _cp()
        raise AssertionError(f"unexpected run call: {args}")

    def core_python(self, module_file, args, timeout=None):
        self.exec_timeouts.append((module_file, timeout))
        if module_file == "core_ask.py":
            text = base64.b64decode(args[args.index("--text-b64") + 1]).decode()
            item = f"W-{len(self.asks) + 1}"
            folder = re.search(r"`(/var/agent/yardstick/[^`]+)`", text).group(1)
            self.asks[item] = {"args": list(args), "text": text, "folder": folder}
            self.calls.append(("ask", item, folder))
            return item + "\n"
        if module_file == "core_items.py" and "--cancel" in args:
            self.calls.append(("cancel", args[args.index("--cancel") + 1]))
            return json.dumps({"cancelled": True})
        if module_file == "core_items.py":
            ids = args[args.index("--ids") + 1].split(",")
            self.polls += 1
            self.calls.append(("poll", tuple(ids)))
            return json.dumps({i: {"status": self.status(i, self.polls), "outcome": "done"} for i in ids})
        if module_file == "core_collect.py":
            self.calls.append(("pool",))
            return json.dumps({"items": [], "conversations": {}, "projects": [], "pool": self.pool, "claimed": 0,
                               "running": []})
        raise AssertionError(f"unexpected core_python call: {module_file} {args}")

    def runner_python(self, module_file, args, timeout=None):
        self.exec_timeouts.append((module_file, timeout))
        assert module_file == "runner_memory.py"
        mode = "snapshot" if "--snapshot" in args else "clean"
        self.calls.append(("memory", mode, list(args)))
        if mode == "snapshot":
            return json.dumps({"notes": 0})
        return json.dumps({"removed": [], "index_lines": 0, "kept": []})

    def kinds(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]


def _results_file(state: State) -> Path:
    return state.root / "yardstick" / "results.jsonl"


def _read_results(state: State) -> list[dict]:
    path = _results_file(state)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# -- tasks --


def test_load_tasks_and_mechanical_flag(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "plain", prompt="Do the plain thing.")
    _write_task(root, "quick", time_limit_min=5)
    _write_task(root, "rubric", rubric="Is the reply clear?")
    _write_task(root, "no-check", check=False)
    (root / "not-a-task").mkdir()  # no task.toml: not a task
    _write_task(root, ".half-copied")  # a dot folder is an install still in progress, never a task

    tasks = {task.id: task for task in yardstick.load_tasks(root)}

    assert set(tasks) == {"plain", "quick", "rubric", "no-check"}
    plain = tasks["plain"]
    assert plain.prompt == "Do the plain thing."
    assert plain.time_limit_min == 30
    assert plain.rubric == ""
    assert plain.path == root / "plain"
    assert plain.mechanical is True
    assert tasks["quick"].time_limit_min == 5 and tasks["quick"].mechanical is True
    assert tasks["rubric"].rubric == "Is the reply clear?" and tasks["rubric"].mechanical is False
    assert tasks["no-check"].mechanical is False

    assert yardstick.load_tasks(tmp_path / "missing") == []


def test_load_tasks_skips_an_unreadable_task_without_naming_it(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "good")
    bad = _write_task(root, "acme-billing")
    (bad / "task.toml").write_text("prompt = [unclosed\n")

    assert [task.id for task in yardstick.load_tasks(root)] == ["good"]
    err = capsys.readouterr().err
    assert "skipped 1" in err and "acme" not in err


def test_add_refuses_overwrite(tmp_path):
    src, root = tmp_path / "src", tmp_path / "yardstick" / "tasks"
    _write_task(src, "auth-hole", prompt="first version")

    assert yardstick.add(src / "auth-hole", root) == "auth-hole"
    assert (root / "auth-hole" / "task.toml").read_text() == 'prompt = "first version"\n'
    assert os.access(root / "auth-hole" / "check", os.X_OK)

    (src / "auth-hole" / "task.toml").write_text('prompt = "edited version"\n')
    with pytest.raises(FileExistsError):
        yardstick.add(src / "auth-hole", root)
    assert (root / "auth-hole" / "task.toml").read_text() == 'prompt = "first version"\n'

    # A retired task's id is taken too: adding it again would quietly undo a person's retirement.
    _write_task(src, "old-task")
    (root.parent / "retired").mkdir()
    (root.parent / "retired" / "old-task").mkdir()
    with pytest.raises(FileExistsError):
        yardstick.add(src / "old-task", root)
    assert not (root / "old-task").exists()

    # Not a task at all (no task.toml) is refused before anything is copied.
    (src / "junk").mkdir()
    with pytest.raises(ValueError):
        yardstick.add(src / "junk", root)
    assert not (root / "junk").exists()


def test_a_task_id_ending_in_dash_r_is_refused(tmp_path, capsys):
    # `<id>-r` is the folder a failed task's retry runs in: a task by that id would share it.
    root = tmp_path / "tasks"
    _write_task(root, "fix-it")
    _write_task(root, "fix-it-r")
    _write_task(root, "docker")  # ends in "r", not "-r": fine

    assert [task.id for task in yardstick.load_tasks(root)] == ["docker", "fix-it"]
    assert "skipped 1" in capsys.readouterr().err

    src = tmp_path / "src"
    _write_task(src, "auth-hole-r")
    with pytest.raises(ValueError, match="-r"):
        yardstick.add(src / "auth-hole-r", tmp_path / "yardstick" / "tasks")
    assert not (tmp_path / "yardstick" / "tasks" / "auth-hole-r").exists()


def test_install_copies_only_new(tmp_path):
    src, root = tmp_path / "deploy-yardstick", tmp_path / "yardstick" / "tasks"
    _write_task(root, "setup-facts", prompt="already installed")
    _write_task(src, "setup-facts", prompt="the shipped version")
    _write_task(src, "split-change")
    _write_task(src, "background-result")
    _write_task(src, "retired-one")
    (root.parent / "retired" / "retired-one").mkdir(parents=True)
    (src / "README.md").write_text("not a task folder\n")

    assert yardstick.install(src, root) == ["background-result", "split-change"]
    assert (root / "setup-facts" / "task.toml").read_text() == 'prompt = "already installed"\n'
    assert (root / "split-change" / "fixture" / "app.py").exists()
    assert not (root / "retired-one").exists()

    assert yardstick.install(src, root) == []


# -- run --


def test_run_passes_and_records_results(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "fix-it", prompt="Fix the failing test in calc.py.")
    _write_task(root, "explain", rubric="Is the explanation right?")
    tasks = yardstick.load_tasks(root)
    host, clock, state = FakeHost(tmp_path), FakeClock(), State(tmp_path / "state")

    results = yardstick.run(host, state, tasks, run_id=RUN, sleep=clock.sleep, clock=clock.clock)

    by_task = {r["task"]: r for r in results}
    assert [r["task"] for r in results] == ["explain", "fix-it"]  # one result per task, in the tasks' order
    fix = by_task["fix-it"]
    assert fix["run"] == RUN and fix["sha"] == "abc1234def"
    assert fix["passed"] is True and fix["judged"] is False and fix["reason"] == "" and fix["infra"] is False
    assert isinstance(fix["minutes"], float) and fix["minutes"] >= 0
    assert datetime.strptime(fix["ts"], "%Y-%m-%dT%H:%M:%SZ")
    assert set(fix) >= {"run", "ts", "sha", "task", "passed", "minutes", "judged", "reason", "infra"}
    assert by_task["explain"]["judged"] is True

    assert _read_results(state) == results

    # The fixture is copied into its own folder under the run, then the turn is queued as a fresh
    # V-yardstick-<run>- conversation with the header naming that folder.
    [fix_ask] = [a for a in host.asks.values() if a["folder"] == f"{BASE}/fix-it"]
    assert fix_ask["args"][:3] == ["--new", "--new-prefix", f"V-yardstick-{RUN}-"]
    assert fix_ask["text"] == (
        f"This is a yardstick task in `{BASE}/fix-it`. Work only in that folder. It has no remote: don't push, "
        "don't open PRs, don't message anyone, and don't save memory notes or skills about it. Reply 'done' when "
        "finished.\n\nFix the failing test in calc.py.")
    [fixture_tar] = [c for c in host.kinds("tar") if c[1] == f"{BASE}/fix-it" and "app.py" in c[2]]
    assert fixture_tar[2] == ["app.py"]
    # The folder is made with a plain mkdir, which refuses one already there, in the same command as the copy.
    assert fixture_tar[3] == f"mkdir -p {BASE} && {{ mkdir {BASE}/fix-it || exit 17; }} && tar -x -C {BASE}/fix-it"

    # Memory is snapshotted before the first turn, and cleaned once at the very end.
    memory = host.kinds("memory")
    assert [m[1] for m in memory] == ["snapshot", "clean"]
    assert host.calls.index(memory[0]) < host.calls.index(host.kinds("ask")[0])
    assert host.calls[-1] == memory[1]
    snapshot_args, clean_args = memory[0][2], memory[1][2]
    assert snapshot_args == ["--root", "/var/agent/memory", "--snapshot", f"{BASE}/.memory-before.json"]
    assert clean_args[clean_args.index("--root") + 1] == "/var/agent/memory"
    assert clean_args[clean_args.index("--marker") + 1] == "/var/agent/yardstick/"
    assert clean_args[clean_args.index("--existing-file") + 1] == f"{BASE}/.memory-before.json"
    since = int(clean_args[clean_args.index("--since") + 1])
    assert abs(since - time.time()) < 60

    assert yardstick.score(results) == (1, 1)


def test_run_times_out_without_hanging(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "slow", time_limit_min=2, hidden=True)
    host = FakeHost(tmp_path, status=lambda item, poll: "claimed")  # Jensen never finishes
    clock, state = FakeClock(), State(tmp_path / "state")

    [result] = yardstick.run(host, state, yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                             clock=clock.clock)

    assert result["passed"] is False and result["reason"] == "timeout" and result["infra"] is False
    assert result["minutes"] >= 2
    assert host.kinds("cancel") == []  # a turn that started is left running, never cancelled
    # Every wait went through the injected sleep, 30 s at a time, and stopped at the limit (both tries).
    assert set(clock.sleeps) == {30}
    assert len(clock.sleeps) <= 2 * (2 * 60 // 30) + 2
    # The turn was never finished, so neither the hidden tests nor the check were ever copied in or run.
    assert host.kinds("check") == []
    assert all("check" not in c[2] for c in host.kinds("tar"))
    assert host.kinds("memory")[-1][1] == "clean"


def test_run_times_out_when_status_reads_keep_failing(tmp_path, capsys):
    # core goes away after the turn is queued: every status read fails. Still a timeout per task, and the run returns.
    root = tmp_path / "tasks"
    _write_task(root, "slow", time_limit_min=1)

    class StatusDownHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            if module_file == "core_items.py":
                self.calls.append(("poll", ()))
                raise RuntimeError("docker exec returned 1: /home/someone/secret")
            return super().core_python(module_file, args, timeout)

    host, clock = StatusDownHost(tmp_path), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is False and result["reason"] == "timeout" and result["tries"] == 2
    assert host.kinds("check") == [] and host.kinds("cancel") == []  # never known to be queued: nothing to cancel
    printed = capsys.readouterr()
    assert "status read" in printed.err and "secret" not in printed.out + printed.err


def test_judged_task_without_a_check_records_the_turn(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "explain", rubric="Is it right?", check=False)
    host = FakeHost(tmp_path, status=lambda item, poll: "failed")
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["judged"] is True and result["passed"] is False and result["reason"] == "turn failed"
    assert result["tries"] == 1  # judged tasks are never retried
    assert host.kinds("check") == [] and len(host.kinds("tar")) == 1  # nothing to copy in afterwards


def test_hidden_copied_only_after_turn(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "auth-hole", hidden=True)
    host = FakeHost(tmp_path, status=lambda item, poll: "done" if poll >= 3 else "claimed")
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is True
    tars = host.kinds("tar")
    fixture_tar, copy_in = tars
    assert "check" not in fixture_tar[2] and not any(n.startswith("hidden") for n in fixture_tar[2])
    assert copy_in[1] == f"{BASE}/auth-hole"
    assert "check" in copy_in[2] and "hidden/test_hidden.py" in copy_in[2]

    polls = host.kinds("poll")
    assert len(polls) == 3
    done_poll = host.calls.index(polls[-1])
    assert host.calls.index(fixture_tar) < host.calls.index(host.kinds("ask")[0]) < host.calls.index(polls[0])
    assert host.calls.index(copy_in) > done_poll
    assert host.calls.index(host.kinds("check")[0]) > host.calls.index(copy_in)


def test_check_copied_in_executable(tmp_path):
    root = tmp_path / "tasks"
    folder = _write_task(root, "t1")
    (folder / "check").chmod(0o644)  # a check that lost its executable bit on the way still runs
    host = FakeHost(tmp_path)
    captured = {}
    original = host.run

    def run(args, **kwargs):
        if "input" in kwargs and "rm -rf" in args[-1]:
            with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
                captured.update({m.name: m for m in tar.getmembers()})
        return original(args, **kwargs)

    host.run = run
    clock = FakeClock()
    yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                  clock=clock.clock)

    assert captured["check"].mode & 0o111
    assert captured["check"].uname == "" and captured["check"].uid == 0


def test_failed_task_retried_once(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "flaky")
    _write_task(root, "broken")
    _write_task(root, "judged", rubric="Judge the reply.")
    fails = {f"{BASE}/flaky", f"{BASE}/broken", f"{BASE}/broken-r", f"{BASE}/judged"}
    host = FakeHost(tmp_path, check_rc=lambda folder: 1 if folder in fails else 0)
    clock, state = FakeClock(), State(tmp_path / "state")

    results = {r["task"]: r for r in yardstick.run(host, state, yardstick.load_tasks(root), run_id=RUN,
                                                    sleep=clock.sleep, clock=clock.clock)}

    assert results["flaky"]["passed"] is True and results["flaky"]["tries"] == 2
    assert results["broken"]["passed"] is False and results["broken"]["tries"] == 2
    assert results["broken"]["reason"] == "check failed"
    # Judged tasks are never retried.
    assert results["judged"]["passed"] is False and results["judged"]["tries"] == 1

    folders = [ask[2] for ask in host.kinds("ask")]
    assert sorted(folders) == sorted([f"{BASE}/flaky", f"{BASE}/flaky-r", f"{BASE}/broken", f"{BASE}/broken-r",
                                      f"{BASE}/judged"])
    # One line per task in the results file, not one per try.
    assert sorted(r["task"] for r in _read_results(state)) == ["broken", "flaky", "judged"]


def test_run_skips_a_task_whose_folder_exists(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "t1")
    host = FakeHost(tmp_path, existing={f"{BASE}/t1"})
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is False and result["reason"] == "folder exists" and result["tries"] == 1
    assert result["infra"] is True
    assert host.kinds("ask") == []
    # One atomic attempt: no separate existence check that a folder made in between could slip past.
    [attempt] = host.kinds("tar")
    assert attempt[3] == f"mkdir -p {BASE} && {{ mkdir {BASE}/t1 || exit 17; }} && tar -x -C {BASE}/t1"
    assert not any(call[0] == "exists?" for call in host.calls)


def test_infrastructure_failures_are_tagged_and_never_scored(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "copy")
    _write_task(root, "build", build=True)
    _write_task(root, "exists")
    _write_task(root, "real")

    class BrokenSetupHost(FakeHost):
        def run(self, args, **kwargs):
            script = str(args[-1])
            if "/copy" in script and "|| exit 17" in script:  # the fixture's copy, either try
                return _cp(returncode=1)
            if script.endswith("sh build.sh"):
                return _cp(returncode=1)
            return super().run(args, **kwargs)

    host = BrokenSetupHost(tmp_path, existing={f"{BASE}/exists"},
                           check_rc=lambda folder: 1 if "/real" in folder else 0)
    clock = FakeClock()

    results = {r["task"]: r for r in yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root),
                                                    run_id=RUN, sleep=clock.sleep, clock=clock.clock)}

    assert {task: (r["reason"], r["infra"]) for task, r in results.items()} == {
        "copy": ("copy failed", True), "build": ("build failed", True), "exists": ("folder exists", True),
        "real": ("check failed", False)}
    assert results["copy"]["tries"] == 2 and results["build"]["tries"] == 2  # each gets its retry all the same
    assert yardstick.score(list(results.values())) == (0, 1)


def test_time_spent_waiting_on_a_usage_limit_counts_toward_no_limit(tmp_path):
    # A 2-minute task: a minute of work, five minutes waiting for an account with capacity, then done.
    root = tmp_path / "tasks"
    _write_task(root, "t1", time_limit_min=2)
    host = FakeHost(tmp_path, status=lambda item, poll: "claimed" if poll <= 2 or poll == 13
                    else "waiting" if poll <= 12 else "done")
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is True and result["tries"] == 1 and result["reason"] == ""
    assert result["minutes"] == 7.0


def test_run_builds_the_fixture_before_queuing(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "t1", build=True)
    _write_task(root, "t2")
    host = FakeHost(tmp_path)
    clock = FakeClock()

    yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                  clock=clock.clock)

    [build] = host.kinds("build")
    assert build[1] == f"{BASE}/t1"
    t1_ask = next(c for c in host.kinds("ask") if c[2] == f"{BASE}/t1")
    assert host.calls.index(build) < host.calls.index(t1_ask)


def test_run_prunes_run_folders_older_than_a_week_first(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "t1")
    host, clock = FakeHost(tmp_path), FakeClock()

    yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                  clock=clock.clock)

    [prune] = host.kinds("prune")
    assert prune[1] == ["/var/agent/yardstick", "-mindepth", "1", "-maxdepth", "1", "-mtime", "+7",
                        "-exec", "rm", "-rf", "{}", "+"]
    assert prune[2] == 120
    assert host.calls.index(prune) < host.calls.index(host.kinds("memory")[0])

    # A prune that fails is no reason to skip the run.
    class NoPruneHost(FakeHost):
        def run(self, args, **kwargs):
            if list(args[:4]) == ["docker", "exec", "agent-runner", "find"]:
                raise RuntimeError("docker exec returned 1: /home/someone/secret")
            return super().run(args, **kwargs)

    [result] = yardstick.run(NoPruneHost(tmp_path), State(tmp_path / "state2"), yardstick.load_tasks(root),
                             run_id=RUN, sleep=clock.sleep, clock=clock.clock)
    assert result["passed"] is True
    printed = capsys.readouterr()
    assert "prun" in printed.err and "secret" not in printed.out + printed.err


def test_hidden_and_check_are_removed_after_each_check(tmp_path):
    # Left in place, a retry in `<id>-r` (or any later turn) could read the first try's hidden tests.
    root = tmp_path / "tasks"
    _write_task(root, "flaky", hidden=True)
    _write_task(root, "steady", hidden=True)
    host = FakeHost(tmp_path, check_rc=lambda folder: 1 if folder == f"{BASE}/flaky" else 0)
    clock = FakeClock()

    yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                  clock=clock.clock)

    checks = host.kinds("check")
    assert sorted(c[1] for c in checks) == [f"{BASE}/flaky", f"{BASE}/flaky-r", f"{BASE}/steady"]
    # Right after each check, failed or passed, its own folder's hidden/ and check go.
    for check in checks:
        assert host.calls[host.calls.index(check) + 1] == ("cleanup", check[1])
    assert len(host.kinds("cleanup")) == 3


def test_a_check_that_times_out_fails_with_check_timeout(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "slow-check", hidden=True)
    _write_task(root, "slow-inside")

    def check_rc(folder):
        if "slow-check" in folder:  # the host-side backstop fires
            return subprocess.TimeoutExpired(["docker"], 660)
        return 124  # `timeout` inside the runner stopped it

    host, clock = FakeHost(tmp_path, check_rc=check_rc), FakeClock()

    results = {r["task"]: r for r in yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root),
                                                    run_id=RUN, sleep=clock.sleep, clock=clock.clock)}

    for task_id in ("slow-check", "slow-inside"):
        assert results[task_id]["passed"] is False and results[task_id]["reason"] == "check timeout"
        assert results[task_id]["tries"] == 2
    # The hidden tests are removed after a check that timed out too.
    assert sorted(c[1] for c in host.kinds("cleanup")) == [f"{BASE}/slow-check", f"{BASE}/slow-check-r",
                                                           f"{BASE}/slow-inside", f"{BASE}/slow-inside-r"]


def test_time_limit_starts_when_the_turn_leaves_the_queue(tmp_path):
    # Queued behind real work for 4 minutes on a 2-minute task, then done 1 minute after it started: a pass.
    root = tmp_path / "tasks"
    _write_task(root, "t1", time_limit_min=2)
    host = FakeHost(tmp_path, status=lambda item, poll: "queued" if poll <= 8 else "claimed" if poll <= 10
                    else "done")
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is True and result["tries"] == 1 and result["reason"] == ""
    assert result["minutes"] == 5.5  # still the whole time since it was queued


def test_a_turn_that_never_leaves_the_queue_times_out_at_three_times_its_limit(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "stuck", time_limit_min=2, rubric="Judge it.", check=False)  # judged: one try only
    host = FakeHost(tmp_path, status=lambda item, poll: "queued")
    clock = FakeClock()

    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    # Not Jensen's doing: an infrastructure failure, which no score or comparison counts.
    assert result["passed"] is False and result["reason"] == "never left the queue" and result["infra"] is True
    assert result["minutes"] == 6.0
    assert len(host.kinds("poll")) == 12  # never cut off at the 2-minute limit while it was still queued
    # Still queued, it would start long after the run gave up on it: it is cancelled.
    assert host.kinds("cancel") == [("cancel", "W-1")]
    assert host.calls.index(host.kinds("cancel")[0]) > host.calls.index(host.kinds("poll")[-1])


def test_a_cancel_that_fails_is_only_counted(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "stuck", time_limit_min=1, rubric="Judge it.", check=False)

    class NoCancelHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            if "--cancel" in args:
                self.calls.append(("cancel-failed",))
                assert timeout == 120
                raise RuntimeError("docker exec returned 1: /home/someone/secret")
            return super().core_python(module_file, args, timeout)

    host, clock = NoCancelHost(tmp_path, status=lambda item, poll: "queued"), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["reason"] == "never left the queue" and len(host.kinds("cancel-failed")) == 1
    printed = capsys.readouterr()
    assert "1 yardstick turn(s) still queued could not be cancelled" in printed.err
    assert "secret" not in printed.out + printed.err


def test_run_stops_at_its_time_budget(tmp_path):
    # One turn at a time: t1 runs out its 30 minutes, t2 is still queued when the 40-minute budget is spent.
    root = tmp_path / "tasks"
    for task_id in ("t1", "t2", "t3"):
        _write_task(root, task_id, time_limit_min=30)
    host = FakeHost(tmp_path, status=lambda item, poll: "claimed" if item == "W-1" else "queued")
    clock, state = FakeClock(), State(tmp_path / "state")

    results = yardstick.run(host, state, yardstick.load_tasks(root), run_id=RUN, concurrency=1, budget_min=40,
                            sleep=clock.sleep, clock=clock.clock)

    assert clock.t - 5_000 <= 40 * 60
    # t1's retry, t2 in flight and t3 never started: none of them says anything about Jensen.
    assert [(r["task"], r["reason"], r["infra"], r["tries"]) for r in results] == [
        ("t1", "skipped: time budget", True, 2), ("t2", "skipped: time budget", True, 1),
        ("t3", "skipped: time budget", True, 1)]
    assert sorted(_read_results(state), key=lambda r: r["task"]) == results
    assert [ask[2] for ask in host.kinds("ask")] == [f"{BASE}/t1", f"{BASE}/t2"]
    assert host.kinds("cancel") == [("cancel", "W-2")]  # still queued: it would start after the run gave up
    assert host.kinds("memory")[-1][1] == "clean"


def test_run_has_a_200_minute_budget_by_default(tmp_path):
    root = tmp_path / "tasks"
    for n in range(4):
        _write_task(root, f"t{n}", time_limit_min=120)
    host = FakeHost(tmp_path, status=lambda item, poll: "claimed")  # nothing ever finishes
    clock = FakeClock()

    results = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                            sleep=clock.sleep, clock=clock.clock)

    assert clock.t - 5_000 <= 200 * 60
    # Four first tries time out after two hours; their four retries run into the budget.
    assert {(r["reason"], r["tries"]) for r in results} == {("skipped: time budget", 2)}


def test_every_exec_in_the_run_loop_has_a_timeout(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "t1")
    host, clock = FakeHost(tmp_path), FakeClock()

    yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                  clock=clock.clock)

    assert {module for module, _ in host.exec_timeouts} == {"core_ask.py", "core_items.py", "runner_memory.py"}
    assert {timeout for _, timeout in host.exec_timeouts} == {120}


def test_a_status_read_that_times_out_is_no_news(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "t1", time_limit_min=5)

    class SlowStatusHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            if module_file == "core_items.py" and self.polls < 2:
                self.polls += 1
                raise subprocess.TimeoutExpired(["docker"], timeout)
            return super().core_python(module_file, args, timeout)

    host, clock = SlowStatusHost(tmp_path), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is True and result["tries"] == 1
    assert "2 status read(s)" in capsys.readouterr().err


def test_a_queue_that_times_out_fails_the_task(tmp_path):
    root = tmp_path / "tasks"
    _write_task(root, "t1")

    class SlowQueueHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            if module_file == "core_ask.py":
                raise subprocess.TimeoutExpired(["docker"], timeout)
            return super().core_python(module_file, args, timeout)

    host, clock = SlowQueueHost(tmp_path), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is False and result["reason"] == "queue error" and result["tries"] == 2
    assert host.kinds("poll") == []


def test_a_memory_step_that_times_out_is_logged_and_skipped(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "t1")

    class SlowMemoryHost(FakeHost):
        def runner_python(self, module_file, args, timeout=None):
            self.calls.append(("memory-timeout",))
            raise subprocess.TimeoutExpired(["docker"], timeout)

    host, clock = SlowMemoryHost(tmp_path), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is True
    assert len(host.kinds("memory-timeout")) == 2  # the snapshot before and the cleanup after
    err = capsys.readouterr().err
    assert "no note will be removed" in err and "memory cleanup after the yardstick run failed" in err


def test_run_keeps_at_most_concurrency_turns_in_flight(tmp_path):
    root = tmp_path / "tasks"
    for n in range(6):
        _write_task(root, f"t{n}")
    host = FakeHost(tmp_path)
    clock = FakeClock()

    results = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                            concurrency=4, sleep=clock.sleep, clock=clock.clock)

    assert len(results) == 6
    polls = host.kinds("poll")
    assert len(polls[0][1]) == 4
    assert all(len(p[1]) <= 4 for p in polls)
    in_flight = peak = 0
    for call in host.calls:
        if call[0] == "ask":
            in_flight += 1
            peak = max(peak, in_flight)
        elif call[0] == "check":
            in_flight -= 1
    assert peak == 4


def test_run_survives_core_being_unreachable(tmp_path, capsys):
    root = tmp_path / "tasks"
    _write_task(root, "t1", time_limit_min=1)

    class DownHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            raise RuntimeError("docker exec returned 1: secret path /home/someone")

    host, clock = DownHost(tmp_path), FakeClock()
    [result] = yardstick.run(host, State(tmp_path / "state"), yardstick.load_tasks(root), run_id=RUN,
                             sleep=clock.sleep, clock=clock.clock)

    assert result["passed"] is False and result["reason"] == "queue failed" and result["infra"] is True
    assert host.kinds("memory")[-1][1] == "clean"
    printed = capsys.readouterr()
    assert "secret" not in printed.out + printed.err and "/home" not in printed.out + printed.err


def test_results_ts_round_trips_through_assess_checks_reader(tmp_path):
    # `run` writes each result's `ts` in state.FMT; `assess-check` reads them back with
    # `State.yardstick_result_newer_than` -- the two must agree, or a new result never counts as new.
    root = tmp_path / "tasks"
    _write_task(root, "t1")
    state, host, clock = State(tmp_path / "state"), FakeHost(tmp_path), FakeClock()
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)

    [result] = yardstick.run(host, state, yardstick.load_tasks(root), run_id=RUN, sleep=clock.sleep,
                             clock=clock.clock)

    assert result["ts"] == datetime.strptime(result["ts"], FMT).strftime(FMT)
    assert state.yardstick_result_newer_than(before) is True
    assert state.yardstick_result_newer_than(datetime.strptime(result["ts"], FMT).replace(tzinfo=UTC)) is False
    assert yardstick.results_path(state) == state.yardstick_results_path()


# -- score / compare --


def test_judged_tasks_not_counted_in_score():
    results = [
        {"task": "a", "passed": True, "judged": False},
        {"task": "b", "passed": False, "judged": False},
        {"task": "c", "passed": True, "judged": True},
        {"task": "d", "passed": False, "judged": True},
    ]
    assert yardstick.score(results) == (1, 2)
    assert yardstick.score([]) == (0, 0)
    assert yardstick.score(results + [{"task": "e", "passed": False, "judged": False, "infra": True}]) == (1, 2)


def _record(state: State, run: str, outcomes: dict[str, bool], judged: dict[str, bool] | None = None,
            infra: tuple[str, ...] = ()) -> None:
    """Result lines for `run`: mechanical `outcomes`, `judged` ones, and mechanical tasks that failed for an
    infrastructure reason (`infra`)."""
    path = _results_file(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for task, passed in outcomes.items():
            fh.write(json.dumps({"run": run, "ts": "2026-11-19T00:00:00Z", "sha": "x", "task": task,
                                 "passed": passed, "minutes": 1.0, "judged": False, "reason": ""}) + "\n")
        for task, passed in (judged or {}).items():
            fh.write(json.dumps({"run": run, "ts": "2026-11-19T00:00:00Z", "sha": "x", "task": task,
                                 "passed": passed, "minutes": 1.0, "judged": True, "reason": ""}) + "\n")
        for task in infra:
            fh.write(json.dumps({"run": run, "ts": "2026-11-19T00:00:00Z", "sha": "x", "task": task,
                                 "passed": False, "minutes": 0.0, "judged": False, "reason": "copy failed",
                                 "infra": True}) + "\n")


def test_compare_gate_fails_on_drop_of_two_and_on_regression(tmp_path):
    # A drop of 2 against the previous run on the same tasks fails the gate; a drop of 1 alone does not.
    state = State(tmp_path / "drop")
    _record(state, "r1", {"a": True, "b": True, "c": True, "d": False})
    _record(state, "r2", {"a": True, "b": False, "c": False, "d": False}, judged={"j": False})
    assert yardstick.compare(state, "r2") == {"previous": "r1", "shared": 4, "drop": 2, "regressions": [],
                                              "gate_failed": True}
    _record(state, "r3", {"a": True, "b": True, "c": False, "d": False})
    assert yardstick.compare(state, "r3") == {"previous": "r2", "shared": 4, "drop": -1, "regressions": [],
                                              "gate_failed": False}

    # A task that passed its last 3 runs and fails now is a regression, even with a drop of only 1.
    state = State(tmp_path / "regression")
    for run in ("r1", "r2", "r3"):
        _record(state, run, {"a": True, "b": False})
    _record(state, "r4", {"a": False, "b": False})
    assert yardstick.compare(state, "r4") == {"previous": "r3", "shared": 2, "drop": 1, "regressions": ["a"],
                                              "gate_failed": True}

    # Two passes in a row is not yet a streak.
    state = State(tmp_path / "short")
    _record(state, "r1", {"a": False})
    _record(state, "r2", {"a": True})
    _record(state, "r3", {"a": True})
    _record(state, "r4", {"a": False})
    assert yardstick.compare(state, "r4")["regressions"] == []


def test_compare_previous_is_the_last_run_sharing_tasks_and_new_tasks_never_trip_it(tmp_path):
    state = State(tmp_path / "state")
    _record(state, "r1", {"a": True, "b": True})
    _record(state, "r2", {"z": True})  # a --tasks run over other tasks only: not comparable
    # Two tasks added since r1 fail on their first run: they're not shared with r1, so no drop.
    _record(state, "r3", {"a": True, "b": True, "new1": False, "new2": False}, judged={"j": True})

    assert yardstick.compare(state, "r3") == {"previous": "r1", "shared": 2, "drop": 0, "regressions": [],
                                              "gate_failed": False}

    first = State(tmp_path / "first")
    _record(first, "r1", {"a": False, "b": False})
    assert yardstick.compare(first, "r1") == {"previous": None, "shared": 0, "drop": 0, "regressions": [],
                                              "gate_failed": False}
    assert yardstick.compare(first, "missing") == {"previous": None, "shared": 0, "drop": 0, "regressions": [],
                                                   "gate_failed": False}


def test_compare_needs_two_shared_tasks_and_looks_past_a_run_that_shares_fewer(tmp_path):
    state = State(tmp_path / "state")
    _record(state, "r1", {"a": True, "b": True, "c": True})
    _record(state, "r2", {"a": True, "z": True})  # a --tasks run sharing only one task with r3
    _record(state, "r3", {"a": True, "b": False, "c": False})

    assert yardstick.compare(state, "r3") == {"previous": "r1", "shared": 3, "drop": 2, "regressions": [],
                                              "gate_failed": True}

    lone = State(tmp_path / "lone")
    _record(lone, "r1", {"a": True, "b": True})
    _record(lone, "r2", {"a": False, "c": False})
    assert yardstick.compare(lone, "r2")["previous"] is None


def test_compare_leaves_infra_results_out(tmp_path):
    # Two tasks the runner couldn't even set up are no drop.
    state = State(tmp_path / "state")
    _record(state, "r1", {"a": True, "b": True, "c": True, "d": True})
    _record(state, "r2", {"a": True, "b": True}, infra=("c", "d"))
    assert yardstick.compare(state, "r2") == {"previous": "r1", "shared": 2, "drop": 0, "regressions": [],
                                              "gate_failed": False}

    # An earlier infra result is no result either: never a baseline's pass or fail, never a break in a streak.
    streak = State(tmp_path / "streak")
    for run in ("r1", "r2", "r3"):
        _record(streak, run, {"a": True, "b": True})
    _record(streak, "r4", {"b": True}, infra=("a",))
    _record(streak, "r5", {"a": False, "b": True})
    assert yardstick.compare(streak, "r5")["regressions"] == ["a"]
    assert yardstick.compare(streak, "r5")["previous"] == "r3"  # r4 shares only b once its infra result is out


def test_a_gate_run_spoiled_by_infra_failures_is_retried(tmp_path):
    gate_started = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    before, gate_run = "20261118T190700Z-0000", "20261119T130000Z-2222"
    state = State(tmp_path / "state")
    _record(state, before, {"a": True, "b": True, "c": True})
    _record(state, gate_run, {"c": True}, infra=("a", "b"))
    assert yardstick.gate_verdict(state, gate_run, gate_started) == "retry"

    # With no run before the gate, or none that ever shared its tasks, there is nothing a retry could compare it
    # with: it passes as the baseline, as any first run does.
    first = State(tmp_path / "first")
    _record(first, gate_run, {"c": True}, infra=("a", "b"))
    assert yardstick.gate_verdict(first, gate_run, gate_started) == "passed"
    other = State(tmp_path / "other")
    _record(other, before, {"x": True, "y": True})
    _record(other, gate_run, {"c": True}, infra=("a", "b"))
    assert yardstick.gate_verdict(other, gate_run, gate_started) == "passed"


def test_gate_verdict(tmp_path):
    first = State(tmp_path / "first")
    _record(first, "r1", {"a": False, "b": False})
    assert yardstick.gate_verdict(first, "r1") == "passed"  # nothing before it: it is the baseline

    state = State(tmp_path / "state")
    _record(state, "r1", {"a": True, "b": True, "c": True})
    _record(state, "r2", {"a": True, "b": True, "c": False})
    assert yardstick.gate_verdict(state, "r2") == "passed"  # a drop of 1
    _record(state, "r3", {"a": False, "b": False, "c": False})
    assert yardstick.gate_verdict(state, "r3") == "failed"
    # No earlier run it can be compared with: it passes and becomes the baseline.
    _record(state, "r4", {"a": True, "new": True})  # one task in common with every earlier run
    assert yardstick.gate_verdict(state, "r4") == "passed"
    _record(state, "r5", {}, judged={"j": True})  # no mechanical task at all
    assert yardstick.gate_verdict(state, "r5") == "passed"
    assert yardstick.gate_verdict(state, "never-ran") == "retry"


def test_a_gate_compares_against_the_last_run_started_before_the_gate(tmp_path):
    # A nightly run landing between the deploy and the gate run already includes the change: it must not be the
    # baseline the gate measures the change against.
    state = State(tmp_path / "state")
    baseline, nightly, gate_run = "20261118T190700Z-0000", "20261119T120500Z-1111", "20261119T130000Z-2222"
    gate_started = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    _record(state, baseline, {"a": True, "b": True, "c": True})
    _record(state, nightly, {"a": False, "b": False, "c": True})
    _record(state, gate_run, {"a": False, "b": False, "c": True})

    assert yardstick.compare(state, gate_run, before=gate_started) == {
        "previous": baseline, "shared": 3, "drop": 2, "regressions": [], "gate_failed": True}
    assert yardstick.gate_verdict(state, gate_run, gate_started) == "failed"
    # Without a gate, compare is unchanged: the nightly is the previous run.
    assert yardstick.compare(state, gate_run)["previous"] == nightly
    assert yardstick.gate_verdict(state, gate_run) == "passed"

    # The streak behind a regression only counts runs from before the gate, too.
    streak = State(tmp_path / "streak")
    for n in range(3):
        _record(streak, f"2026111{n}T000000Z-000{n}", {"a": True, "b": True})
    _record(streak, nightly, {"a": False, "b": True})
    _record(streak, gate_run, {"a": False, "b": True})
    assert yardstick.compare(streak, gate_run, before=gate_started)["regressions"] == ["a"]
    assert yardstick.compare(streak, gate_run)["regressions"] == []

    # No run started before the gate at all: the gate run is the first baseline there can be.
    fresh = State(tmp_path / "fresh")
    _record(fresh, nightly, {"a": True, "b": True})
    _record(fresh, gate_run, {"a": False, "b": False})
    assert yardstick.gate_verdict(fresh, gate_run, gate_started) == "passed"

    # A run id that doesn't carry its start time falls back to its earliest result's ts.
    loose = State(tmp_path / "loose")
    path = yardstick.results_path(loose)
    path.parent.mkdir(parents=True)
    rows = [("early", "2026-11-19T11:00:00Z", True), ("late", "2026-11-19T12:30:00Z", False),
            (gate_run, "2026-11-19T13:30:00Z", False)]
    path.write_text("".join(json.dumps({"run": run, "ts": ts, "task": task, "passed": passed, "judged": False})
                            + "\n" for run, ts, passed in rows for task in ("a", "b")))
    assert yardstick.compare(loose, gate_run, before=gate_started)["previous"] == "early"


def test_a_failed_gate_run_is_never_a_baseline(tmp_path):
    # The change a failed gate measured was reverted: its run says nothing about Jensen as it is now. Compared with
    # it, the next change scoring just as badly would pass.
    state = State(tmp_path / "state")
    nightlies = [f"2026111{n}T190700Z-000{n}" for n in range(3)]
    for run in nightlies:
        _record(state, run, {task: True for task in "abcde"})
    three_of_five = {"a": True, "b": True, "c": True, "d": False, "e": False}
    gate_a, gate_a_started = "20261119T090000Z-aaaa", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)
    _record(state, gate_a, three_of_five)
    assert yardstick.gate_verdict(state, gate_a, gate_a_started) == "failed"
    yardstick.record_gate_run(state, gate_a, "2026-11-19T08:00:00Z", "failed")

    gate_b, gate_b_started = "20261119T110000Z-bbbb", datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    _record(state, gate_b, three_of_five)
    comparison = yardstick.compare(state, gate_b, before=gate_b_started)
    assert comparison["previous"] == nightlies[-1] and comparison["drop"] == 2
    assert comparison["regressions"] == ["d", "e"]  # the failed run doesn't break their streak either
    assert yardstick.gate_verdict(state, gate_b, gate_b_started) == "failed"

    # A nightly run skips it too.
    yardstick.record_gate_run(state, gate_b, "2026-11-19T10:00:00Z", "failed")
    _record(state, "20261119T190700Z-cccc", {task: True for task in "abcde"})
    assert yardstick.compare(state, "20261119T190700Z-cccc")["previous"] == nightlies[-1]

    # A gate whose only earlier run is a failed gate run has nothing to compare with: it is the baseline.
    lone = State(tmp_path / "lone")
    _record(lone, gate_a, three_of_five)
    yardstick.record_gate_run(lone, gate_a, "2026-11-19T08:00:00Z", "failed")
    _record(lone, gate_b, {"a": False, "b": False, "c": False, "d": False, "e": False})
    assert yardstick.gate_verdict(lone, gate_b, gate_b_started) == "passed"


def test_cli_gate_run_records_its_verdict_so_a_failed_one_is_never_a_baseline(tmp_path, monkeypatch):
    state = State(tmp_path / "state")
    for task_id in "abcde":
        _write_task(state.root / "yardstick" / "tasks", task_id)
    for n in range(3):
        _record(state, f"2026111{n}T190700Z-000{n}", {task: True for task in "abcde"})
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)
    fails_d_and_e = lambda folder: 1 if folder.rsplit("/", 1)[1][0] in "de" else 0  # noqa: E731

    state.gate_start(12, "repo", "abc123", T0 - timedelta(hours=2))
    rc, kv = _gate_run(state, FakeHost(tmp_path, check_rc=fails_d_and_e), tmp_path / "out.txt",
                       now=T0 - timedelta(hours=1))
    assert rc == 0 and kv["gate_result"] == "failed"
    gate_a_lines = [r for r in _read_results(state) if r["run"] == kv["run_id"]]
    assert {r["gate"] for r in gate_a_lines} == {"2026-11-19T17:07:00Z"}
    state.gate_end("failed", T0 - timedelta(minutes=50))

    state.gate_start(13, "repo", "def456", T0 - timedelta(minutes=30))
    rc, kv = _gate_run(state, FakeHost(tmp_path, check_rc=fails_d_and_e), tmp_path / "out.txt")
    assert rc == 0 and kv["gate_result"] == "failed"  # against the nightly 5/5, not gate A's 3/5

    runs = [json.loads(line) for line in yardstick.gate_runs_path(state).read_text().splitlines()]
    assert [(r["gate"], r["result"]) for r in runs] == [("2026-11-19T17:07:00Z", "failed"),
                                                       ("2026-11-19T18:37:00Z", "failed")]
    # A nightly run's lines carry no gate.
    main(["yardstick", "run", "--github-output", str(tmp_path / "out.txt")], host=FakeHost(tmp_path), state=state,
         now=T0 + timedelta(hours=1))
    assert all("gate" not in r for r in _read_results(state) if r["run"].startswith("20261119T200700Z"))


def test_cli_gate_run_ignores_a_nightly_run_after_the_gate_started(tmp_path, monkeypatch):
    state = State(tmp_path / "state")
    for task_id in ("a", "b", "c"):
        _write_task(state.root / "yardstick" / "tasks", task_id)
    _record(state, "20261118T190700Z-0000", {"a": True, "b": True, "c": True})
    state.gate_start(12, "repo", "abc123", datetime(2026, 11, 19, 17, 0, 0, tzinfo=UTC))
    _record(state, "20261119T180000Z-1111", {"a": False, "b": False, "c": True})  # nightly, after the deploy
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)
    host = FakeHost(tmp_path, check_rc=lambda folder: 0 if folder.rsplit("/", 1)[1].startswith("c") else 1)

    rc, kv = _gate_run(state, host, tmp_path / "out.txt")

    assert rc == 0 and kv["gate_result"] == "failed" and kv["gate_failed"] == "true"


# -- memory cleanup --


def _note(path: Path, text: str, mtime: float) -> None:
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def test_memory_cleanup_only_removes_marked_notes_from_this_run(tmp_path, capsys):
    memory = tmp_path / "memory"
    memory.mkdir()
    since = 1_800_000_000
    snapshot = tmp_path / "yardstick" / RUN / ".memory-before.json"

    _note(memory / "old-marked.md", "learned in /var/agent/yardstick/20261001T000000Z-0000/x/\n", since - 3600)
    _note(memory / "project-roster.md", "real work note\n", since - 3600)
    # The snapshot is taken as the run starts: every note above existed before it.
    assert runner_memory.main(["--root", str(memory), "--snapshot", str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out) == {"notes": 2}

    _note(memory / "new-marked.md", f"the answer is in {BASE}/setup-facts/answers.md\n", since + 60)
    _note(memory / "new-plain.md", "a person asked for shorter replies\n", since + 60)
    # A note that was there before the run and got touched during it, mentioning the yardstick: still kept.
    _note(memory / "project-roster.md", f"real work note\nalso see {BASE}/x\n", since + 120)
    (memory / "MEMORY.md").write_text(
        "- [Old marked](old-marked.md) — an old one\n"
        "- [New marked](new-marked.md) — the yardstick answer\n"
        "- [New plain](new-plain.md) — replies\n"
        "- [Roster](project-roster.md) — the roster project\n"
        "- [Not the same](renew-marked.md) — a different file whose name only ends the same\n")

    rc = runner_memory.main(["--root", str(memory), "--since", str(since), "--marker", "/var/agent/yardstick/",
                             "--existing-file", str(snapshot)])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["removed"] == ["new-marked.md"]
    assert report["index_lines"] == 1
    assert report["kept"] == ["project-roster.md"]
    assert sorted(p.name for p in memory.iterdir()) == ["MEMORY.md", "new-plain.md", "old-marked.md",
                                                       "project-roster.md"]
    assert (memory / "MEMORY.md").read_text() == (
        "- [Old marked](old-marked.md) — an old one\n"
        "- [New plain](new-plain.md) — replies\n"
        "- [Roster](project-roster.md) — the roster project\n"
        "- [Not the same](renew-marked.md) — a different file whose name only ends the same\n")


def test_memory_cleanup_without_a_readable_snapshot_removes_nothing(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    since = 1_800_000_000
    _note(memory / "new-marked.md", f"see {BASE}/x\n", since + 60)

    report = runner_memory.clean(memory, since, "/var/agent/yardstick/", existing_file=tmp_path / "missing.json")
    assert report == {"removed": [], "index_lines": 0, "kept": ["new-marked.md"]}
    assert (memory / "new-marked.md").exists()

    # With no snapshot asked for at all, the plain rule applies: new and marked is removed.
    assert runner_memory.clean(memory, since, "/var/agent/yardstick/")["removed"] == ["new-marked.md"]
    assert runner_memory.clean(tmp_path / "missing", since, "/var/agent/yardstick/") == \
        {"removed": [], "index_lines": 0, "kept": []}


# -- improve-jensen's own traffic --


def test_yardstick_conversations_count_as_improve_jensens_own(tmp_path):
    record = {"kind": "turn", "id": "t1", "source": None, "conversation": f"V-yardstick-{RUN}-0a1b2c3d",
              "no_delegation": 1}
    assert is_improve_jensens_own(record)
    assert is_improve_jensens_own({**record, "conversation": "V-review-0a1b2c3d"})
    assert not is_improve_jensens_own({**record, "conversation": "C-123"})
    assert summarize([record], "no_delegation", "x") == "no_delegation since x: total=0 turns=0"

    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)

    class Payloads:
        def turn(self, convo, text, actor, *, unattended=False):
            return {"text": text}

    item = WorkQueue(conn).get(core_ask.enqueue_turn(conn, Payloads(), None, "hi",
                                                     new_prefix=f"V-yardstick-{RUN}-"))
    assert re.fullmatch(rf"V-yardstick-{RUN}-[0-9a-f]{{8}}", item.conversation_id)
    assert item.payload["source"] == "improve-jensen"
    # The default is unchanged.
    default = WorkQueue(conn).get(core_ask.enqueue_turn(conn, Payloads(), None, "hi"))
    assert default.conversation_id.startswith("V-review-")


def test_core_ask_accepts_new_and_new_prefix():
    args = core_ask.build_parser().parse_args(["--new", "--new-prefix", f"V-yardstick-{RUN}-", "--text-b64", "aGk="])
    assert args.new is True and args.new_prefix == f"V-yardstick-{RUN}-" and args.conversation is None
    assert core_ask.build_parser().parse_args(["--text-b64", "aGk="]).new_prefix == "V-review-"
    with pytest.raises(SystemExit):
        core_ask.build_parser().parse_args(["--new", "--conversation", "V-x", "--text-b64", "aGk="])


# -- core_items --


def _item(conn, item_id, conversation_id, status, *, payload=None, result=None):
    conn.execute(
        "INSERT INTO work_items(id, conversation_id, kind, payload, status, result, created_at, updated_at) "
        "VALUES (?, ?, 'turn', ?, ?, ?, ?, ?)",
        (item_id, conversation_id, json.dumps(payload or {}), status,
         json.dumps(result) if result is not None else None, f"2026-11-19T00:00:{len(item_id):02d}Z",
         "2026-11-19T00:00:00Z"))


def test_core_items_reports_status_and_follows_continuations(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    _item(conn, "W-1", "V-a", "claimed")
    _item(conn, "W-2", "V-b", "done", result={"outcome": "done", "text": "secret reply text"})
    # A turn cut off by its step limit is carried on by a new turn in the same conversation.
    _item(conn, "W-3", "V-c", "done", result={"outcome": "max_turns"})
    _item(conn, "W-3b", "V-c", "queued", payload={"follows": "W-3", "continuation": 1})
    # A turn cut off by a usage limit waits for capacity before it is carried on.
    _item(conn, "W-4", "V-d", "done", result={"outcome": "error"})
    conn.execute("INSERT INTO limit_waits(work_item_id, conversation_id, error, created_at) "
                 "VALUES ('W-4', 'V-d', 'limit', '2026-11-19T00:00:00Z')")
    # A handover into another conversation follows the turn, but is not its continuation.
    _item(conn, "W-5", "V-e", "done", result={"outcome": "done"})
    _item(conn, "W-5x", "V-other", "claimed", payload={"follows": "W-5"})

    out = core_items.items(conn, ["W-1", "W-2", "W-3", "W-4", "W-5", "W-missing"])

    assert out["W-1"]["status"] == "claimed"
    assert out["W-2"] == {"status": "done", "outcome": "done", "latest": "W-2"}
    assert out["W-3"]["status"] == "queued" and out["W-3"]["latest"] == "W-3b"
    assert out["W-4"]["status"] == "waiting"
    assert out["W-5"]["status"] == "done"
    assert "W-missing" not in out
    assert "secret reply text" not in json.dumps(out)


def test_core_items_cancels_only_a_queued_yardstick_turn(tmp_path, monkeypatch, capsys):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    _item(conn, "W-q", f"V-yardstick-{RUN}-0a1b2c3d", "queued")
    _item(conn, "W-c", f"V-yardstick-{RUN}-1a1b2c3d", "claimed")
    _item(conn, "W-p", "C-person", "queued")  # a person's turn: never touched
    _item(conn, "W-r", "V-review-0a1b2c3d", "queued")

    assert core_items.cancel(conn, "W-q") is True
    assert [core_items.cancel(conn, item) for item in ("W-c", "W-p", "W-r", "W-missing")] == [False] * 4
    statuses = {row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM work_items")}
    assert statuses == {"W-q": "cancelled", "W-c": "claimed", "W-p": "queued", "W-r": "queued"}

    # On the command line it opens core's DB for writing, and says whether it cancelled.
    _item(conn, "W-q2", f"V-yardstick-{RUN}-2a1b2c3d", "queued")
    monkeypatch.setattr(core_items, "_DB_PATH", str(tmp_path / "agent.db"))
    assert core_items.main(["--cancel", "W-q2"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cancelled": True}
    assert WorkQueue(conn).get("W-q2").status == "cancelled"
    with pytest.raises(SystemExit):
        core_items.main(["--cancel", "W-q2", "--ids", "W-q"])


# -- CLI --


def test_cli_yardstick_run_skips_while_pool_holds(tmp_path):
    state = State(tmp_path / "state")
    _write_task(state.root / "yardstick" / "tasks", "t1")
    host = FakeHost(tmp_path, pool="hold_scheduled")
    out = tmp_path / "out.txt"

    assert main(["yardstick", "run", "--github-output", str(out)], host=host, state=state, now=T0) == 0

    kv = _kv(out.read_text())
    assert kv["skip_reason"] == "pool_hold" and kv["gate_failed"] == "false"
    assert kv["score"] == "0" and kv["counted"] == "0" and kv["run_id"] == ""
    assert host.kinds("ask") == [] and host.kinds("tar") == [] and host.kinds("memory") == []


def test_cli_yardstick_run_goes_ahead_when_core_cannot_be_read(tmp_path, monkeypatch):
    # Like the hourly measure: an unreadable pool is no reason to skip. A change that broke core must fail the gate,
    # not pass it unmeasured.
    state = State(tmp_path / "state")
    _write_task(state.root / "yardstick" / "tasks", "t1")

    class NoPoolHost(FakeHost):
        def core_python(self, module_file, args, timeout=None):
            if module_file == "core_collect.py":
                raise RuntimeError("docker exec returned 1")
            return super().core_python(module_file, args, timeout)

    host = NoPoolHost(tmp_path)
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)
    out = tmp_path / "out.txt"
    state.gate_start(12, "repo", "abc123", T0)

    assert main(["yardstick", "run", "--gate", "--github-output", str(out)], host=host, state=state, now=T0) == 0
    kv = _kv(out.read_text())
    assert kv["skip_reason"] == "" and kv["score"] == "1" and kv["counted"] == "1" and kv["gate_failed"] == "false"
    assert kv["gate_result"] == "passed"  # the first run ever: the baseline


def test_cli_yardstick_run_with_no_tasks_skips(tmp_path):
    out = tmp_path / "out.txt"
    host = FakeHost(tmp_path)
    assert main(["yardstick", "run", "--github-output", str(out)], host=host, state=State(tmp_path / "state"),
                now=T0) == 0
    assert _kv(out.read_text())["skip_reason"] == "no_tasks"
    assert host.kinds("ask") == []


def test_cli_yardstick_run_writes_outputs_and_gate(tmp_path, monkeypatch, capsys):
    state = State(tmp_path / "state")
    tasks_root = state.root / "yardstick" / "tasks"
    _write_task(tasks_root, "acme-a")
    _write_task(tasks_root, "acme-b")
    _write_task(tasks_root, "acme-c")
    _record(state, "20261118T190700Z-0000", {"acme-a": True, "acme-b": True, "acme-c": True})
    host = FakeHost(tmp_path, check_rc=lambda folder: 0 if folder.endswith("acme-a") else 1)
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)  # the CLI waits with the real sleep: make it instant
    out = tmp_path / "out.txt"
    state.gate_start(12, "repo", "abc123", T0)

    rc = main(["yardstick", "run", "--gate", "--github-output", str(out)], host=host, state=state, now=T0)

    assert rc == 0
    kv = _kv(out.read_text())
    assert kv["score"] == "1" and kv["counted"] == "3" and kv["skip_reason"] == ""
    assert re.fullmatch(r"20261119T190700Z-[0-9a-f]{4}", kv["run_id"])
    assert kv["gate_result"] == "failed" and kv["gate_failed"] == "true"
    assert state.gate(T0) is not None  # the run never ends the gate itself: the workflow does, with its result
    printed = capsys.readouterr()
    assert printed.out.strip().splitlines()[-1] == "score=1/3"
    assert "acme" not in printed.out and "acme" not in printed.err


def _gate_run(state, host, out, now=T0):
    rc = main(["yardstick", "run", "--gate", "--github-output", str(out)], host=host, state=state, now=now)
    kv = _kv(out.read_text())
    out.unlink()
    return rc, kv


def test_cli_gate_run_without_an_active_gate_measures_nothing(tmp_path):
    state = State(tmp_path / "state")
    _write_task(state.root / "yardstick" / "tasks", "t1")
    host, out = FakeHost(tmp_path), tmp_path / "out.txt"

    rc, kv = _gate_run(state, host, out)

    assert rc == 0
    assert kv["gate_result"] == "none" and kv["gate_failed"] == "false" and kv["skip_reason"] == "no_gate"
    assert kv["run_id"] == "" and kv["counted"] == "0"
    assert host.calls == [] and not yardstick.results_path(state).exists()


def test_cli_gate_run_retries_when_it_cannot_measure(tmp_path, monkeypatch, capsys):
    state = State(tmp_path / "state")
    state.gate_start(12, "repo", "abc123", T0)
    out = tmp_path / "out.txt"
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)

    # No tasks installed: nothing to measure, now or on a retry.
    rc, kv = _gate_run(state, FakeHost(tmp_path), out)
    assert rc == 0 and kv["gate_result"] == "none" and kv["skip_reason"] == "no_tasks"

    # The pool holds.
    _write_task(state.root / "yardstick" / "tasks", "t1")
    _write_task(state.root / "yardstick" / "tasks", "t2")
    rc, kv = _gate_run(state, FakeHost(tmp_path, pool="hold_scheduled"), out)
    assert rc == 0 and kv["gate_result"] == "retry" and kv["skip_reason"] == "pool_hold"

    # The run itself raises.
    def broken_run(*args, **kwargs):
        raise RuntimeError("boom in /home/someone")

    with monkeypatch.context() as m:
        m.setattr(yardstick, "run", broken_run)
        rc, kv = _gate_run(state, FakeHost(tmp_path), out)
    assert rc == 1 and kv["gate_result"] == "retry" and kv["gate_failed"] == "false"

    # The comparison raises.
    def broken_verdict(*args, **kwargs):
        raise ValueError("bad results")

    with monkeypatch.context() as m:
        m.setattr(yardstick, "gate_verdict", broken_verdict)
        rc, kv = _gate_run(state, FakeHost(tmp_path), out)
    assert rc == 1 and kv["gate_result"] == "retry"

    # Every retry leaves the gate active, for the next gate run.
    assert state.gate(T0) is not None
    printed = capsys.readouterr()
    assert "boom" not in printed.out + printed.err and "/home" not in printed.out + printed.err


def test_cli_gate_run_with_no_comparable_earlier_run_passes_as_the_baseline(tmp_path, monkeypatch):
    # Fewer than 2 mechanical tasks in common with any earlier run: waiting would never change that.
    state = State(tmp_path / "state")
    state.gate_start(12, "repo", "abc123", T0)
    _write_task(state.root / "yardstick" / "tasks", "t1")
    _write_task(state.root / "yardstick" / "tasks", "t2")
    _record(state, "20261118T190700Z-0000", {"t1": True, "old": True})
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)

    rc, kv = _gate_run(state, FakeHost(tmp_path, check_rc=lambda folder: 1), tmp_path / "out.txt")

    assert rc == 0 and kv["gate_result"] == "passed" and kv["skip_reason"] == "" and kv["score"] == "0"


def test_cli_gate_run_passes_without_a_drop(tmp_path, monkeypatch):
    state = State(tmp_path / "state")
    for task_id in ("a", "b", "c"):
        _write_task(state.root / "yardstick" / "tasks", task_id)
    _record(state, "20261118T190700Z-0000", {"a": True, "b": True, "c": False})
    state.gate_start(12, "repo", "abc123", T0)
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)

    rc, kv = _gate_run(state, FakeHost(tmp_path), tmp_path / "out.txt")

    assert rc == 0 and kv["gate_result"] == "passed" and kv["gate_failed"] == "false" and kv["score"] == "3"


def test_cli_nightly_run_never_judges_a_gate(tmp_path, monkeypatch):
    state = State(tmp_path / "state")
    for task_id in ("a", "b"):
        _write_task(state.root / "yardstick" / "tasks", task_id)
    _record(state, "20261118T190700Z-0000", {"a": True, "b": True})
    state.gate_start(12, "repo", "abc123", T0)
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)
    out = tmp_path / "out.txt"

    assert main(["yardstick", "run", "--github-output", str(out)], host=FakeHost(tmp_path, check_rc=lambda f: 1),
                state=state, now=T0) == 0

    kv = _kv(out.read_text())
    assert kv["gate_result"] == "none" and kv["gate_failed"] == "false" and kv["score"] == "0"
    assert state.gate(T0) is not None


def test_cli_yardstick_run_only_named_tasks(tmp_path, monkeypatch, capsys):
    state = State(tmp_path / "state")
    tasks_root = state.root / "yardstick" / "tasks"
    _write_task(tasks_root, "t1")
    _write_task(tasks_root, "t2")
    host = FakeHost(tmp_path)
    monkeypatch.setattr(yardstick, "POLL_SECONDS", 0)

    assert main(["yardstick", "run", "--tasks", "t2"], host=host, state=state, now=T0) == 0
    assert [ask[2].rsplit("/", 1)[1] for ask in host.kinds("ask")] == ["t2"]
    assert capsys.readouterr().out.strip().splitlines()[-1] == "score=1/1"

    assert main(["yardstick", "run", "--tasks", "nope"], host=host, state=state, now=T0) == 2


def test_cli_yardstick_install_add_and_score(tmp_path, capsys):
    state = State(tmp_path / "state")
    src = tmp_path / "seeds"
    _write_task(src, "s1")
    _write_task(src, "s2")

    assert main(["yardstick", "install", str(src)], host=FakeHost(tmp_path), state=state, now=T0) == 0
    assert capsys.readouterr().out.strip() == "installed=2"
    assert sorted(p.name for p in (state.root / "yardstick" / "tasks").iterdir()) == ["s1", "s2"]

    extra = tmp_path / "new"
    _write_task(extra, "n1")
    assert main(["yardstick", "add", str(extra / "n1")], host=FakeHost(tmp_path), state=state, now=T0) == 0
    assert capsys.readouterr().out.strip() == "added=1"
    assert main(["yardstick", "add", str(extra / "n1")], host=FakeHost(tmp_path), state=state, now=T0) == 1
    err = capsys.readouterr().err
    assert "exists" in err and "n1" not in err
    (extra / "junk").mkdir()
    assert main(["yardstick", "add", str(extra / "junk")], host=FakeHost(tmp_path), state=state, now=T0) == 2
    assert main(["yardstick", "install", str(tmp_path / "nowhere")], host=FakeHost(tmp_path), state=state,
                now=T0) == 2
    capsys.readouterr()

    assert main(["yardstick", "score"], host=FakeHost(tmp_path), state=state, now=T0) == 0
    assert "no yardstick runs yet" in capsys.readouterr().out

    _record(state, "r1", {"s1": True, "s2": True})
    _record(state, "r2", {"s1": True, "s2": False}, judged={"n1": True})
    assert main(["yardstick", "score"], host=FakeHost(tmp_path), state=state, now=T0) == 0
    # The latest run's score line, then the comparison -- counts only, never a task id (an assessment-written task
    # can be named after a real product).
    assert capsys.readouterr().out.splitlines() == [
        "run=r2 sha=x score=1/2 judged=1",
        "previous=r1 drop=1 regressions=0 gate_failed=false",
    ]
