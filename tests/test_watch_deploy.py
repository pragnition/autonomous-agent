import json
import subprocess
from pathlib import Path

import pytest

from agent.watch.deploy import check, deploy, main, services_for
from agent.watch.host import Host
from agent.watch.state import State


@pytest.fixture(autouse=True)
def _use_git_env(git_env):
    """Every test here touches a real tmp git checkout; `git_env` (conftest.py) gives it a throwaway identity
    and `main` as the default branch, so these tests never read or write the host's own git config."""


# -- a real tmp git repo pair (origin + a clone standing in for the live checkout) --


def _run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result


def _commit(repo: Path, files: dict, message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _run(["git", "add", rel], cwd=repo)
    _run(["git", "commit", "-m", message], cwd=repo)
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """`origin` and a `live` clone of it, both on `main`, one initial commit already in both."""
    origin = tmp_path / "origin"
    origin.mkdir(parents=True)
    _run(["git", "init"], cwd=origin)
    _commit(origin, {"README.md": "hello\n"}, "initial")

    live = tmp_path / "live"
    _run(["git", "clone", str(origin), str(live)], cwd=tmp_path)
    return origin, live


# -- a fake that delegates `git` to the real subprocess.run and fakes only docker/docker compose --


class FakeDocker:
    """Stands in for `Host.run`. Any `git ...` call goes straight to the real `subprocess.run` against the tmp
    checkout; every `docker`/`docker compose` call is faked here instead, since there is no real stack in tests.

    Knobs tests set before calling `deploy`/`check`:
    - `claimed`: what a `core_collect` probe reports (the queue depth `deploy` waits to drain).
    - `unhealthy`: container names whose `docker inspect` fails outright (not running).
    - `restarted`: container names whose `RestartCount` bumps between the two stability-check rounds.
    - `health_fails`: of `/health`, `/health/runner`, which probe returns non-200.
    - `smoke_ok`: whether the smoke test prints `smoke ok`.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.claimed = 0
        self.unhealthy: set[str] = set()
        self.restarted: set[str] = set()
        self.health_fails: set[str] = set()
        self.smoke_ok = True
        self._inspect_rounds: dict[str, int] = {}

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] != "docker":
            return subprocess.run(args, **kwargs)
        if args[1] == "inspect":
            return self._inspect(args[2])
        if args[1] == "compose":
            return self._compose(args[2:])
        if args[1] == "exec" and args[2] == "-i":
            return self._core_collect()
        if args[1] == "exec":
            return self._health(args)
        raise AssertionError(f"unexpected docker call: {args}")

    def _inspect(self, container):
        if container in self.unhealthy:
            return subprocess.CompletedProcess([], 1, "", "Error: No such container: " + container)
        round_n = self._inspect_rounds.get(container, 0)
        self._inspect_rounds[container] = round_n + 1
        count = 1 if (container in self.restarted and round_n >= 1) else 0
        return subprocess.CompletedProcess([], 0, f"true {count}", "")

    def _compose(self, rest):
        if rest[0] == "up":
            return subprocess.CompletedProcess([], 0, "", "")
        if rest[0] == "exec":
            stdout = "smoke ok\n" if self.smoke_ok else "smoke FAILED: timed out\n"
            return subprocess.CompletedProcess([], 0 if self.smoke_ok else 1, stdout, "")
        raise AssertionError(f"unexpected compose call: {rest}")

    def _core_collect(self):
        return subprocess.CompletedProcess([], 0, json.dumps({"claimed": self.claimed}), "")

    def _health(self, args):
        probe = args[-1]
        path = "/health/runner" if "/health/runner" in probe else "/health"
        ok = path not in self.health_fails
        return subprocess.CompletedProcess([], 0 if ok else 1, "", "")


class FakeClock:
    """`sleep`/`clock` for `deploy`/`check`: `sleep` advances a virtual clock instead of actually waiting."""

    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def time(self):
        return self.t


# -- services_for --


def test_services_for_table():
    assert services_for(["src/agent/runner/foo.py"]) == {"agent"}
    assert services_for(["src/agent/supervisor.py"]) == {"supervisor", "supervisor-standby"}
    assert services_for(["src/agent/core/app.py"]) == {"core"}
    assert services_for(["src/agent/dashboard/app.py"]) == {"core"}
    assert services_for(["src/agent/defaults/agent.toml"]) == {"core"}
    assert services_for(["src/agent/plugins/x.py"]) == {"core"}
    assert services_for(["src/agent/connectors/x.py"]) == {"core"}
    assert services_for(["dashboard/src/App.tsx"]) == {"core"}
    assert services_for(["scripts/smoke.py"]) == {"core"}
    assert services_for(["src/agent/watch/host.py"]) == set()
    assert services_for(["docs/plan.md"]) == set()
    assert services_for(["tests/test_x.py"]) == set()
    assert services_for([".github/workflows/ci.yml"]) == set()
    assert services_for(["deploy/improve-jensen-seed.json"]) == set()
    assert services_for(["examples/foo/bar.py"]) == set()
    assert services_for(["scripts/ask_jensen.py"]) == set()
    assert services_for(["README.md"]) == set()
    assert services_for(["justfile"]) == set()
    assert services_for(["src/agent/config.py"]) == {"core", "agent", "supervisor", "supervisor-standby"}
    assert services_for(["src/agent/sandbox/run.py"]) == {"core", "agent", "supervisor", "supervisor-standby"}
    assert services_for(["Dockerfile"]) == {"core", "agent", "supervisor", "supervisor-standby"}
    assert services_for(["pyproject.toml"]) == {"core", "agent", "supervisor", "supervisor-standby"}
    assert services_for(["compose.yaml"]) == {"*"}
    assert services_for(["LICENSE"]) == {"core"}

    # A mix of rows unions the services.
    mixed = services_for(["src/agent/runner/foo.py", "src/agent/supervisor.py", "docs/plan.md"])
    assert mixed == {"agent", "supervisor", "supervisor-standby"}


# -- deploy --


def test_nothing_to_deploy_returns_0(tmp_path):
    origin, live = _repo_pair(tmp_path)
    host = Host(run=FakeDocker(), live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    fake = host.run
    assert not any(c[:2] == ["git", "merge"] for c in fake.calls)
    assert not any(c[0] == "docker" for c in fake.calls)
    assert state.deploy_pending() is False


def test_dirty_or_diverged_checkout_returns_2_and_builds_nothing(tmp_path, capsys):
    # Dirty: an uncommitted change in the live checkout.
    origin, live = _repo_pair(tmp_path)
    (live / "README.md").write_text("dirty, never committed\n")
    host = Host(run=FakeDocker(), live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 2
    assert not any(c[0] == "docker" for c in host.run.calls)
    out = capsys.readouterr().out
    assert out.strip() != ""

    # Diverged: a local commit on `live` that never made it to `origin`.
    origin2, live2 = _repo_pair(tmp_path / "second")
    _commit(live2, {"local-only.txt": "x\n"}, "local commit never pushed")
    host2 = Host(run=FakeDocker(), live=live2)
    state2 = State(tmp_path / "second-state")

    result2 = deploy(host2, state2, sleep=lambda s: None, clock=lambda: 0.0)

    assert result2 == 2
    assert not any(c[0] == "docker" for c in host2.run.calls)


def test_runner_change_waits_for_idle_then_pending(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/runner/foo.py": "x\n"}, "runner change")
    head_before = _run(["git", "rev-parse", "HEAD"], cwd=live).stdout.strip()

    fake = FakeDocker()
    fake.claimed = 1  # never drops to 0
    clock = FakeClock()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, wait_idle_s=1800, sleep=clock.sleep, clock=clock.time)

    assert result == 3
    assert state.deploy_pending() is True
    head_after = _run(["git", "rev-parse", "HEAD"], cwd=live).stdout.strip()
    assert head_after == head_before  # never merged
    assert not any(c[0] == "docker" and c[1] == "compose" for c in fake.calls)
    # Polled every 30s, never slept past the 1800s budget.
    assert all(s == 30 for s in clock.slept)
    assert sum(clock.slept) >= 1800


def test_core_change_deploys_without_waiting(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/core/app.py": "x\n"}, "core change")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    assert ["docker", "compose", "up", "-d", "--build", "core"] in fake.calls
    deploys = (tmp_path / "state" / "deploys.jsonl").read_text().splitlines()
    assert len(deploys) == 1
    record = json.loads(deploys[0])
    assert record["services"] == ["core"]
    assert record["result"] == "ok"
    assert state.deploy_pending() is False


def test_failed_check_returns_1_and_is_logged(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/core/app.py": "x\n"}, "core change")

    fake = FakeDocker()
    fake.smoke_ok = False
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 1
    deploys = (tmp_path / "state" / "deploys.jsonl").read_text().splitlines()
    assert len(deploys) == 1
    record = json.loads(deploys[0])
    assert record["result"].startswith("check_failed")
    # Even a failed check clears the flag -- there is nothing left "pending" about this attempt.
    assert state.deploy_pending() is False


def test_if_pending_without_flag_does_nothing(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/runner/foo.py": "x\n"}, "runner change")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")
    assert state.deploy_pending() is False

    result = deploy(host, state, if_pending=True, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    assert fake.calls == []  # never even fetched


def test_lock_held_returns_pending(tmp_path):
    import fcntl

    origin, live = _repo_pair(tmp_path)
    state = State(tmp_path / "state")
    lock_path = state.root / "deploy.lock"
    lock_path.touch()
    held = open(lock_path, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        fake = FakeDocker()
        host = Host(run=fake, live=live)
        clock = FakeClock()

        result = deploy(host, state, wait_idle_s=0, sleep=clock.sleep, clock=clock.time)

        assert result == 3
        assert fake.calls == []  # never got past the lock
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# -- check --


def test_check_detects_a_not_running_container():
    fake = FakeDocker()
    fake.unhealthy = {"agent-core"}
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"core"}, sleep=lambda s: None)

    assert ok is False
    assert "agent-core" in reason


def test_check_passes_when_everything_is_healthy():
    fake = FakeDocker()
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"core"}, sleep=lambda s: None)

    assert ok is True
    assert reason == ""


def test_check_star_covers_every_container_in_the_map():
    fake = FakeDocker()
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"*"}, sleep=lambda s: None)

    assert ok is True
    inspected = {c[2] for c in fake.calls if c[:2] == ["docker", "inspect"]}
    assert inspected == {"agent-core", "agent-runner", "agent-supervisor", "agent-supervisor-standby",
                          "agent-relay", "agent-docker"}


# -- CLI --


def test_dry_run_prints_services_and_changes_nothing(tmp_path, capsys):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/core/app.py": "x\n"}, "core change")
    head_before = _run(["git", "rev-parse", "HEAD"], cwd=live).stdout.strip()

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = main(["--dry-run"], host=host, state=state)

    assert result == 0
    head_after = _run(["git", "rev-parse", "HEAD"], cwd=live).stdout.strip()
    assert head_after == head_before
    assert not any(c[0] == "docker" for c in fake.calls)
    assert state.deploy_pending() is False
    out = capsys.readouterr().out
    assert "core" in out
    assert "src/agent/core/app.py" in out


def test_cli_wires_if_pending(tmp_path):
    origin, live = _repo_pair(tmp_path)
    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = main(["--if-pending"], host=host, state=state)

    assert result == 0
    assert fake.calls == []
