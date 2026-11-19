import asyncio
import itertools
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.runner import services as services_mod
from agent.runner.main import RunnerKeeper, serve
from agent.runner.services import (Backoff, Output, Service, ServiceSupervisor, find_strays, read_services,
                                   supervise, take_lock)

PY = sys.executable
SLEEP = "import time; time.sleep(60)"


def toml_value(v):
    return json.dumps(str(v) if isinstance(v, Path) else v)  # JSON strings, numbers and arrays are valid TOML


def write_services(path: Path, *entries: dict) -> None:
    lines = []
    for entry in entries:
        lines.append("[[services]]")
        lines += [f"{k} = {toml_value(v)}" for k, v in entry.items()]
    path.write_text("\n".join(lines) + "\n")


def running(pid: int) -> bool:
    """Whether the process is running (not gone, not a zombie waiting to be reaped)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


async def until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


def pid_of(run: Path, name: str) -> int | None:
    try:
        return int((run / f"{name}.pid").read_text())
    except (OSError, ValueError):
        return None


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


@pytest.fixture
async def make_supervisor(tmp_path):
    made = []

    def make(**options):
        options.setdefault("grace_s", 2.0)
        sup = ServiceSupervisor(tmp_path / "services.toml", tmp_path / "run", **options)
        made.append(sup)
        return sup

    yield make
    for sup in made:
        await sup.stop_all()


class FakeSleep:
    """Records each restart wait and returns at once, until `limit` waits; then it waits forever."""

    def __init__(self, limit: int):
        self.delays: list[float] = []
        self.limit = limit
        self.enough = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if len(self.delays) >= self.limit:
            self.enough.set()
            await asyncio.Event().wait()


# -- the services file --


def test_reads_the_services(tmp_path, work):
    other = tmp_path / "other"
    other.mkdir()
    write_services(tmp_path / "s.toml", {"name": "outlook-bridge", "cmd": ["python3", "-I", "bridge.py"], "cwd": work},
                   {"name": "poller.v2_x", "cmd": ["./poll"], "cwd": other})
    services, problems = read_services(tmp_path / "s.toml")
    assert problems == []
    assert services == [Service("outlook-bridge", ("python3", "-I", "bridge.py"), str(work)),
                        Service("poller.v2_x", ("./poll",), str(other))]


def test_a_missing_file_lists_no_services(tmp_path):
    assert read_services(tmp_path / "nope.toml") == ([], [])


@pytest.mark.parametrize("content", [
    b"\xff\xfe\x00garbage",
    b"[[services]\nname = ",
    b"services = 3",
    b"services = [1, 2]",
    b'services = "outlook-bridge"',
])
def test_an_invalid_file_lists_no_services_and_says_why(tmp_path, content):
    (tmp_path / "s.toml").write_bytes(content)
    services, problems = read_services(tmp_path / "s.toml")
    assert services == [] and len(problems) == 1


def test_a_directory_where_the_file_should_be_lists_no_services(tmp_path):
    (tmp_path / "s.toml").mkdir()
    services, problems = read_services(tmp_path / "s.toml")
    assert services == [] and problems


@pytest.mark.parametrize("change, problem", [
    ({"name": "bad name"}, "`name`"),
    ({"name": "a/b"}, "`name`"),
    ({"name": ""}, "`name`"),
    ({"name": 5}, "`name`"),
    ({"name": None}, "`name`"),
    ({"cmd": []}, "`cmd`"),
    ({"cmd": "python3 bridge.py"}, "`cmd`"),
    ({"cmd": ["python3", 1]}, "`cmd`"),
    ({"cmd": None}, "`cmd`"),
    ({"cwd": "relative/dir"}, "`cwd`"),
    ({"cwd": "/no/such/folder"}, "`cwd`"),
    ({"cwd": "FILE"}, "`cwd`"),
    ({"cwd": None}, "`cwd`"),
])
def test_an_invalid_entry_is_left_out_and_the_rest_run(tmp_path, work, change, problem):
    (work / "file").write_text("")
    entry = {"name": "broken", "cmd": ["python3", "x.py"], "cwd": str(work)}
    entry.update({k: str(work / "file") if v == "FILE" else v for k, v in change.items()})
    entry = {k: v for k, v in entry.items() if v is not None}
    write_services(tmp_path / "s.toml", entry, {"name": "good", "cmd": ["python3", "y.py"], "cwd": work})
    services, problems = read_services(tmp_path / "s.toml")
    assert [s.name for s in services] == ["good"]
    assert len(problems) == 1 and problem in problems[0]
    assert "x.py" not in problems[0]  # never a service's arguments


def test_a_name_listed_twice_runs_once(tmp_path, work):
    write_services(tmp_path / "s.toml", {"name": "a", "cmd": ["one"], "cwd": work},
                   {"name": "a", "cmd": ["two"], "cwd": work})
    services, problems = read_services(tmp_path / "s.toml")
    assert [s.cmd for s in services] == [("one",)] and "more than once" in problems[0]


# -- backoff --


def test_backoff_doubles_from_5_s_to_5_minutes():
    backoff = Backoff()
    assert [backoff.after(1) for _ in range(9)] == [5, 10, 20, 40, 80, 160, 300, 300, 300]


def test_backoff_resets_after_10_minutes_up():
    backoff = Backoff()
    assert [backoff.after(1) for _ in range(4)] == [5, 10, 20, 40]
    assert backoff.after(599) == 80
    assert backoff.after(600) == 5
    assert backoff.after(1) == 10


# -- reconciling --


async def test_starts_a_service_in_its_folder_with_the_runners_environment(tmp_path, work, make_supervisor,
                                                                           monkeypatch):
    monkeypatch.setenv("SERVICE_TEST_VALUE", "from-the-runner")
    code = ("import os, time; open('env.txt', 'w').write(os.environ['SERVICE_TEST_VALUE']); "
            "print('hello from the bridge', flush=True); time.sleep(60)")
    write_services(tmp_path / "services.toml", {"name": "bridge", "cmd": [PY, "-c", code], "cwd": work})
    sup = make_supervisor()
    await sup.reconcile()
    run = tmp_path / "run"
    await until(lambda: (work / "env.txt").exists() and pid_of(run, "bridge"))
    pid = pid_of(run, "bridge")
    assert (work / "env.txt").read_text() == "from-the-runner"
    assert os.getpgid(pid) == pid and os.getsid(pid) == pid  # its own session and process group
    assert os.readlink(f"/proc/{pid}/cwd") == str(work.resolve())
    await until(lambda: "hello from the bridge" in (run / "bridge.out").read_text())
    assert f"started (pid {pid})" in (run / "bridge.out").read_text()
    assert set(sup.running()) == {"bridge"}
    await sup.reconcile()  # nothing changed: the same process keeps running
    assert pid_of(run, "bridge") == pid and running(pid)


async def test_stops_a_removed_service_and_everything_in_its_group(tmp_path, work, make_supervisor, caplog):
    code = ("import subprocess, time; p = subprocess.Popen(['sleep', '60']); "
            "open('child.pid', 'w').write(str(p.pid)); time.sleep(60)")
    write_services(tmp_path / "services.toml", {"name": "bridge", "cmd": [PY, "-c", code], "cwd": work})
    sup = make_supervisor()
    run = tmp_path / "run"
    await sup.reconcile()
    await until(lambda: (work / "child.pid").exists() and pid_of(run, "bridge"))
    pid, child = pid_of(run, "bridge"), int((work / "child.pid").read_text())
    (tmp_path / "services.toml").write_text("")
    with caplog.at_level(logging.INFO, logger="agent.runner.services"):
        await sup.reconcile()
    assert not running(pid)
    await until(lambda: not running(child))
    assert not (run / "bridge.pid").exists() and sup.running() == {}
    assert "stopped: it's no longer in" in (run / "bridge.out").read_text()
    assert "stopping service bridge" in caplog.text
    fd = take_lock(run / "bridge.lock")  # the lock is released
    assert fd is not None
    os.close(fd)


async def test_a_service_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, work, make_supervisor):
    code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open('ready', 'w').close(); time.sleep(60)")
    write_services(tmp_path / "services.toml", {"name": "stubborn", "cmd": [PY, "-c", code], "cwd": work})
    sup = make_supervisor(grace_s=0.5)
    await sup.reconcile()
    await until(lambda: (work / "ready").exists())
    pid = pid_of(tmp_path / "run", "stubborn")
    (tmp_path / "services.toml").unlink()
    started = time.monotonic()
    await sup.reconcile()
    assert time.monotonic() - started >= 0.5
    assert not running(pid)


async def test_restarts_a_service_whose_command_changed(tmp_path, work, make_supervisor):
    run = tmp_path / "run"
    write_services(tmp_path / "services.toml",
                   {"name": "bridge", "cmd": [PY, "-c", "open('a', 'w').close(); " + SLEEP], "cwd": work})
    sup = make_supervisor()
    await sup.reconcile()
    await until(lambda: (work / "a").exists() and pid_of(run, "bridge"))
    old = pid_of(run, "bridge")
    write_services(tmp_path / "services.toml",
                   {"name": "bridge", "cmd": [PY, "-c", "open('b', 'w').close(); " + SLEEP], "cwd": work})
    await sup.reconcile()
    await until(lambda: (work / "b").exists() and pid_of(run, "bridge"))
    assert not running(old) and pid_of(run, "bridge") != old
    fd = take_lock(run / "bridge.lock")  # still held across the restart
    assert fd is None


async def test_restarts_an_exited_service_with_backoff(tmp_path, work, make_supervisor, caplog):
    code = "import sys; print('boom', flush=True); sys.exit(3)"
    write_services(tmp_path / "services.toml", {"name": "flaky", "cmd": [PY, "-c", code], "cwd": work})
    sleep = FakeSleep(limit=8)
    sup = make_supervisor(sleep=sleep)
    with caplog.at_level(logging.INFO, logger="agent.runner.services"):
        await sup.reconcile()
        await asyncio.wait_for(sleep.enough.wait(), 30)
    assert sleep.delays == [5, 10, 20, 40, 80, 160, 300, 300]
    assert "service flaky started" in caplog.text and "service flaky restarted" in caplog.text
    assert "service flaky exited with code 3" in caplog.text and "restarting it in 5 s" in caplog.text
    out = (tmp_path / "run" / "flaky.out").read_text()
    assert out.count("boom") == 8 and "exited with code 3" in out and "restarting in 300 s" in out


async def test_backoff_resets_after_a_long_run(tmp_path, work, make_supervisor):
    write_services(tmp_path / "services.toml", {"name": "flaky", "cmd": [PY, "-c", "pass"], "cwd": work})
    ticks = itertools.count(0, 700)  # every run lasts 700 s: over the 10 minutes that resets the backoff
    sleep = FakeSleep(limit=3)
    sup = make_supervisor(sleep=sleep, clock=lambda: next(ticks))
    await sup.reconcile()
    await asyncio.wait_for(sleep.enough.wait(), 30)
    assert sleep.delays == [5, 5, 5]


async def test_a_command_that_cant_start_backs_off_and_never_raises(tmp_path, work, make_supervisor, caplog):
    write_services(tmp_path / "services.toml", {"name": "missing", "cmd": ["/no/such/binary"], "cwd": work})
    sleep = FakeSleep(limit=2)
    sup = make_supervisor(sleep=sleep)
    with caplog.at_level(logging.INFO, logger="agent.runner.services"):
        await sup.reconcile()
        await asyncio.wait_for(sleep.enough.wait(), 10)
    assert sleep.delays == [5, 10]
    assert "service missing couldn't start" in caplog.text
    assert "couldn't start" in (tmp_path / "run" / "missing.out").read_text()


async def test_an_invalid_file_never_raises_and_stops_what_ran(tmp_path, work, make_supervisor, caplog):
    run = tmp_path / "run"
    write_services(tmp_path / "services.toml", {"name": "bridge", "cmd": [PY, "-c", SLEEP], "cwd": work})
    sup = make_supervisor()
    await sup.reconcile()
    await until(lambda: pid_of(run, "bridge"))
    pid = pid_of(run, "bridge")
    (tmp_path / "services.toml").write_bytes(b"[[services]\nname = \xff")
    with caplog.at_level(logging.INFO, logger="agent.runner.services"):
        await sup.reconcile()
        await sup.reconcile()  # the same problem is logged once
    assert sup.running() == {} and not running(pid)
    assert caplog.text.count("services file:") == 1
    (tmp_path / "services.toml").unlink()
    (tmp_path / "services.toml").mkdir()
    await sup.reconcile()
    assert sup.running() == {}


async def test_only_one_copy_runs(tmp_path, work, make_supervisor):
    run = tmp_path / "run"
    code = "import os, time; open('starts', 'a').write(f'{os.getpid()}\\n'); time.sleep(60)"
    write_services(tmp_path / "services.toml", {"name": "one", "cmd": [PY, "-c", code], "cwd": work})
    first, second = make_supervisor(), make_supervisor()
    starts = lambda: (work / "starts").read_text().split() if (work / "starts").exists() else []  # noqa: E731
    await first.reconcile()
    await until(lambda: len(starts()) == 1)
    await second.reconcile()  # can't take the lock
    await asyncio.sleep(0.5)
    assert len(starts()) == 1 and second.running() == {}
    assert take_lock(run / "one.lock") is None
    pid = int(starts()[0])
    await first.stop_all()
    await second.reconcile()  # the lock is free now
    await until(lambda: len(starts()) == 2)
    assert not running(pid) and running(int(starts()[1]))


async def test_runner_shutdown_stops_every_service(tmp_path, work, make_supervisor):
    run = tmp_path / "run"
    write_services(tmp_path / "services.toml", {"name": "a", "cmd": [PY, "-c", SLEEP], "cwd": work},
                   {"name": "b", "cmd": [PY, "-c", SLEEP], "cwd": work})
    sup = make_supervisor(check_s=0.05)
    task = asyncio.create_task(sup.run_forever())
    await until(lambda: pid_of(run, "a") and pid_of(run, "b"))
    pids = [pid_of(run, "a"), pid_of(run, "b")]
    task.cancel()
    await asyncio.wait([task])
    assert not any(running(p) for p in pids)
    assert not (run / "a.pid").exists() and not (run / "b.pid").exists()
    for name in ("a", "b"):
        fd = take_lock(run / f"{name}.lock")
        assert fd is not None
        os.close(fd)
        assert "stopped: the runner is stopping" in (run / f"{name}.out").read_text()


# -- taking over a copy started by hand --


def fake_proc_entry(proc: Path, pid: int | str, argv: list[str] | None, cwd: Path | None,
                    real: bool = False) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    if argv is not None:
        (entry / "cmdline").write_bytes(b"".join(os.fsencode(a) + b"\0" for a in argv))
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)
    if real:
        (entry / "stat").symlink_to(f"/proc/{pid}/stat")


def test_find_strays_matches_the_whole_argv_in_the_same_folder(tmp_path, work):
    other = tmp_path / "other"
    other.mkdir()
    proc = tmp_path / "proc"
    cmd = ["python3", "-I", "bridge.py"]
    fake_proc_entry(proc, 100, cmd, work)                                    # the copy
    fake_proc_entry(proc, 101, cmd + ["--verbose"], work)                    # more arguments
    fake_proc_entry(proc, 102, ["python3", "-I", "bridge.py.bak"], work)     # an argument that contains it
    fake_proc_entry(proc, 103, ["python3 -I bridge.py"], work)               # one argument with the same text
    fake_proc_entry(proc, 104, ["sudo"] + cmd, work)                         # contains the argv
    fake_proc_entry(proc, 105, cmd, other)                                   # another folder
    fake_proc_entry(proc, 106, [], work)                                     # a zombie or kernel thread
    fake_proc_entry(proc, 107, cmd, None)                                    # cwd unreadable
    fake_proc_entry(proc, os.getpid(), cmd, work)                            # this process
    fake_proc_entry(proc, "self", cmd, work)                                 # not a pid
    assert find_strays(proc, cmd, str(work)) == [100]
    assert find_strays(proc, cmd, str(work) + "/") == [100]
    assert find_strays(tmp_path / "no-proc", cmd, str(work)) == []


async def test_takes_over_a_copy_started_by_hand(tmp_path, work, make_supervisor, caplog):
    run, proc = tmp_path / "run", tmp_path / "proc"
    cmd = [PY, "-c", SLEEP, "bridge"]
    stray = subprocess.Popen(cmd, cwd=work)
    longer = subprocess.Popen(cmd + ["--other"], cwd=work)
    elsewhere = subprocess.Popen(cmd, cwd=tmp_path)
    try:
        fake_proc_entry(proc, stray.pid, cmd, work.resolve(), real=True)
        fake_proc_entry(proc, longer.pid, cmd + ["--other"], work.resolve(), real=True)
        fake_proc_entry(proc, elsewhere.pid, cmd, tmp_path.resolve(), real=True)
        write_services(tmp_path / "services.toml", {"name": "bridge", "cmd": cmd, "cwd": work})
        sup = make_supervisor(proc_root=proc)
        with caplog.at_level(logging.INFO, logger="agent.runner.services"):
            await sup.reconcile()
            await until(lambda: stray.poll() is not None)
            await until(lambda: pid_of(run, "bridge"))
        assert stray.returncode == -signal.SIGTERM
        assert pid_of(run, "bridge") != stray.pid and running(pid_of(run, "bridge"))
        assert longer.poll() is None and elsewhere.poll() is None  # neither is the same argv in the same folder
        assert f"stopped a copy started outside the runner (pid {stray.pid})" in caplog.text
        assert f"stopped a copy started outside the runner (pid {stray.pid})" in (run / "bridge.out").read_text()
    finally:
        for p in (stray, longer, elsewhere):
            p.kill()
            p.wait()


async def test_looks_for_a_hand_started_copy_only_before_the_first_start(tmp_path, work, make_supervisor,
                                                                        monkeypatch):
    looked = []
    monkeypatch.setattr(services_mod, "find_strays", lambda *a: looked.append(a) or [])
    write_services(tmp_path / "services.toml", {"name": "flaky", "cmd": [PY, "-c", "pass"], "cwd": work})
    sleep = FakeSleep(limit=3)
    sup = make_supervisor(sleep=sleep)
    await sup.reconcile()
    await asyncio.wait_for(sleep.enough.wait(), 10)
    assert len(looked) == 1


# -- output --


def test_output_rotates_at_the_cap(tmp_path):
    out = Output(tmp_path / "x.out", cap=100)
    out.write(b"a" * 60)
    out.write(b"b" * 30)
    assert not (tmp_path / "x.out.1").exists()
    out.write(b"c" * 30)  # would take it past 100
    assert (tmp_path / "x.out.1").read_bytes() == b"a" * 60 + b"b" * 30
    assert (tmp_path / "x.out").read_bytes() == b"c" * 30
    out.write(b"d" * 90)
    assert (tmp_path / "x.out.1").read_bytes() == b"c" * 30 and (tmp_path / "x.out").read_bytes() == b"d" * 90
    out.close()


def test_output_appends_to_what_an_earlier_runner_wrote(tmp_path):
    (tmp_path / "x.out").write_bytes(b"earlier\n")
    out = Output(tmp_path / "x.out", cap=1000)
    out.write(b"partial")
    out.note("exited with code 1")
    out.close()
    text = (tmp_path / "x.out").read_text().splitlines()
    assert text[:2] == ["earlier", "partial"] and text[2].endswith("runner: exited with code 1")


async def test_a_chatty_service_rotates_its_output(tmp_path, work, make_supervisor):
    code = ("import time\nfor i in range(100):\n    print(f'line {i:04d} ' + 'x' * 40, flush=True)\n"
            "    time.sleep(0.01)\ntime.sleep(60)")
    write_services(tmp_path / "services.toml", {"name": "chatty", "cmd": [PY, "-c", code], "cwd": work})
    sup = make_supervisor(out_cap=2000)
    await sup.reconcile()
    run = tmp_path / "run"
    await until(lambda: (run / "chatty.out").exists() and "line 0099" in (run / "chatty.out").read_text())
    assert "line 00" in (run / "chatty.out.1").read_text()
    # each file stays near the cap: only a single write bigger than the room left can take it past
    assert (run / "chatty.out").stat().st_size <= 4000 and (run / "chatty.out.1").stat().st_size <= 4000


# -- in the runner --


async def test_supervise_starts_again_after_a_failure(tmp_path, monkeypatch):
    calls = []

    async def run_forever(self):
        calls.append(self)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await asyncio.Event().wait()

    monkeypatch.setattr(ServiceSupervisor, "run_forever", run_forever)
    task = asyncio.create_task(supervise(tmp_path / "s.toml", tmp_path / "run", retry_s=0.01))
    await until(lambda: len(calls) == 2)
    task.cancel()
    await asyncio.wait([task])


async def test_services_start_without_a_runner_token_and_never_stop_the_runner():
    started, reads, stopping = asyncio.Event(), [], asyncio.Event()

    async def services():
        started.set()
        raise RuntimeError("boom")

    def read_token():
        reads.append(1)
        if len(reads) >= 5:
            stopping.set()
        return ""  # no token yet

    keeper = RunnerKeeper(lambda token: pytest.fail("no runner without a token"))
    await asyncio.wait_for(serve(keeper, read_token, 0.01, services(), stopping), 5)
    assert started.is_set() and len(reads) == 5


async def test_stopping_the_runner_stops_the_services():
    stopped, running_ = asyncio.Event(), asyncio.Event()

    async def services():
        running_.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stopping = asyncio.Event()
    keeper = RunnerKeeper(lambda token: pytest.fail("no runner without a token"))
    task = asyncio.create_task(serve(keeper, lambda: "", 0.01, services(), stopping))
    await asyncio.wait_for(running_.wait(), 5)
    stopping.set()
    await asyncio.wait_for(task, 5)
    assert stopped.is_set()
