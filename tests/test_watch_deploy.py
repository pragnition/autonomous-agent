import fcntl
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.watch.deploy import check, deploy, main, resume_if_left_paused, services_for
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


def _head(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _run(["git", "add", rel], cwd=repo)
    _run(["git", "commit", "-m", message], cwd=repo)
    return _head(repo)


def _repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """`origin` and a `live` clone of it, both on `main`, one initial commit already in both."""
    origin = tmp_path / "origin"
    origin.mkdir(parents=True)
    _run(["git", "init"], cwd=origin)
    _commit(origin, {"README.md": "hello\n"}, "initial")

    live = tmp_path / "live"
    _run(["git", "clone", str(origin), str(live)], cwd=tmp_path)
    return origin, live


def _deploys(tmp_path: Path, state_name: str = "state") -> list[dict]:
    path = tmp_path / state_name / "deploys.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- a fake that delegates `git` to the real subprocess.run and fakes only docker/docker compose --


class FakeDocker:
    """Stands in for `Host.run`. Any `git ...` call goes straight to the real `subprocess.run` against the tmp
    checkout (unless its subcommand is listed in `raise_on_git`, for exercising error paths); every
    `docker`/`docker compose` call is faked here instead, since there is no real stack in tests.

    Knobs tests set before calling `deploy`/`check`:
    - `claimed`: what a `core_collect` probe reports (the queue depth `deploy` waits to drain).
    - `core_unreachable`: makes the `core_collect` probe fail outright (simulating core being down).
    - `unhealthy`: container names whose `docker inspect` fails outright (not running).
    - `restarted`: container names whose `RestartCount` bumps between the two stability-check rounds.
    - `health_fails`: of `/health`, `/health/runner`, which probe returns non-200.
    - `smoke_ok`: whether the smoke test prints `smoke ok`.
    - `build_fails` / `up_fails`: whether `docker compose build`/`up` exits non-zero.
    - `raise_on_git`: git subcommands (e.g. `{"fetch"}`) that raise instead of running for real.
    - `fail_git`: git subcommands (e.g. `{"merge": "stderr text"}`) that return exit code 1 with that stderr,
      instead of running for real -- for a failure the real tmp repo can't easily be made to produce on its own.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.claimed = 0
        self.core_unreachable = False
        self.unhealthy: set[str] = set()
        self.restarted: set[str] = set()
        self.health_fails: set[str] = set()
        self.smoke_ok = True
        self.build_fails = False
        self.up_fails = False
        self.raise_on_git: set[str] = set()
        self.fail_git: dict[str, str] = {}
        self._inspect_rounds: dict[str, int] = {}
        self.paused = "0"            # core's `paused` setting
        self.resume_failures = 0     # how many `agent resume` calls fail first (core still starting)
        self.swaps: list[str] = []   # "pause", "up <services>", "resume", in order

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] != "docker":
            if args[0] == "git" and len(args) > 1:
                if args[1] in self.raise_on_git:
                    raise RuntimeError(f"simulated failure running {args}")
                if args[1] in self.fail_git:
                    return subprocess.CompletedProcess(args, 1, "", self.fail_git[args[1]])
            return subprocess.run(args, **kwargs)
        if args[1] == "inspect":
            return self._inspect(args[2])
        if args[1] == "compose":
            return self._compose(args[2:])
        if args[1] == "exec" and args[2] == "-i":
            return self._core_collect()
        if args[1] == "exec" and args[3:5] == ["python", "-c"] and "set_setting" not in args[5] \
                and "'paused'" in args[5]:
            return subprocess.CompletedProcess([], 0, self.paused + "\n", "")
        if args[1] == "exec" and args[3:5] == ["python", "-c"] and "'paused'" in args[5]:
            was = self.paused
            self.paused = "1"
            self.swaps.append("pause")
            return subprocess.CompletedProcess([], 0, was + "\n", "")
        if args[1] == "exec" and args[3:] == ["agentd", "resume"]:
            if self.resume_failures:
                self.resume_failures -= 1
                return subprocess.CompletedProcess([], 1, "", "Error: container is restarting")
            self.paused = "0"
            self.swaps.append("resume")
            return subprocess.CompletedProcess([], 0, "resume: done\n", "")
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
        if rest[0] == "build":
            return subprocess.CompletedProcess([], 1 if self.build_fails else 0, "", "")
        if rest[0] == "up":
            self.swaps.append("up " + " ".join(rest[3:]))
            return subprocess.CompletedProcess([], 1 if self.up_fails else 0, "", "")
        if rest[0] == "exec":
            self.swaps.append("smoke")
            ok = self.smoke_ok and self.paused != "1"   # a paused core never starts the smoke turn
            stdout = "smoke ok\n" if ok else "smoke FAILED: timed out\n"
            return subprocess.CompletedProcess([], 0 if ok else 1, stdout, "")
        raise AssertionError(f"unexpected compose call: {rest}")

    def _core_collect(self):
        if self.core_unreachable:
            return subprocess.CompletedProcess([], 1, "", "Error: No such container: agent-control")
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
    image = {"control", "services"}  # what runs the agent image
    for path in ("src/agent/services/supervise.py", "src/agent/defaults/agent.toml", "src/agent/plugins/x.py",
                 "dashboard/src/App.tsx", "rust/crates/agentd/src/serve.rs"):
        assert services_for([path]) == image, path
    assert services_for(["src/agent/supervisor.py"]) == {"supervisor", "supervisor-standby"}
    assert services_for(["src/agent/watch/host.py"]) == set()
    assert services_for(["docs/plan.md"]) == set()
    assert services_for(["tests/test_x.py"]) == set()
    assert services_for([".github/workflows/ci.yml"]) == set()
    assert services_for(["deploy/improve-jensen-seed.json"]) == set()
    assert services_for(["examples/foo/bar.py"]) == set()
    assert services_for(["scripts/ask_jensen.py"]) == set()
    assert services_for(["README.md"]) == set()
    assert services_for(["justfile"]) == set()
    assert services_for(["src/agent/db.py"]) == image | {"supervisor", "supervisor-standby"}
    assert services_for(["src/agent/sandbox/run.py"]) == image | {"supervisor", "supervisor-standby"}
    # Dockerfile has its own build stage for `relay` too (see compose.yaml); pyproject.toml (Python deps) does not.
    assert services_for(["Dockerfile"]) == image | {"supervisor", "supervisor-standby", "relay"}
    assert services_for(["pyproject.toml"]) == image | {"supervisor", "supervisor-standby"}
    assert services_for(["compose.yaml"]) == {"*"}
    assert services_for(["LICENSE"]) == {"control"}

    # A mix of rows unions the services.
    mixed = services_for(["src/agent/services/foo.py", "src/agent/supervisor.py", "docs/plan.md"])
    assert mixed == {"control", "services", "supervisor", "supervisor-standby"}


# -- deploy: nothing to deploy / not deployable --


def test_nothing_to_deploy_returns_0(tmp_path):
    origin, live = _repo_pair(tmp_path)
    host = Host(run=FakeDocker(), live=live)
    state = State(tmp_path / "state")
    state.set_deploy_pending(True)  # a stale flag from some earlier attempt must not outlive "nothing to do".

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    fake = host.run
    assert not any(c[:2] == ["git", "merge"] for c in fake.calls)
    assert not any(c[0] == "docker" for c in fake.calls)
    assert state.deploy_pending() is False
    assert _deploys(tmp_path) == []


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
    records = _deploys(tmp_path)
    assert len(records) == 1
    assert records[0]["result"].startswith("not_deployable")
    assert records[0]["services"] == []

    # Diverged: a local commit on `live` that never made it to `origin`.
    origin2, live2 = _repo_pair(tmp_path / "second")
    _commit(live2, {"local-only.txt": "x\n"}, "local commit never pushed")
    host2 = Host(run=FakeDocker(), live=live2)
    state2 = State(tmp_path / "second-state")

    result2 = deploy(host2, state2, sleep=lambda s: None, clock=lambda: 0.0)

    assert result2 == 2
    assert not any(c[0] == "docker" for c in host2.run.calls)
    records2 = _deploys(tmp_path, "second-state")
    assert records2[0]["result"].startswith("not_deployable")


def test_fetch_failure_returns_2_and_is_logged(tmp_path):
    origin, live = _repo_pair(tmp_path)
    shutil.rmtree(origin)  # `git fetch origin main` now genuinely fails -- no fake needed.

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 2
    assert not any(c[0] == "docker" for c in fake.calls)
    records = _deploys(tmp_path)
    assert len(records) == 1
    assert records[0]["result"] == "not_deployable: fetch failed"
    assert records[0]["from"] is not None  # HEAD is always readable even when the fetch itself failed


def test_failed_ff_merge_returns_2_and_is_logged(tmp_path):
    """Defensive: even after both drift checks pass, a non-zero `merge --ff-only` itself must not be treated as
    success -- `fail_git` forces this exit code directly since a real tmp repo won't produce this failure on
    its own once the ancestor check has already passed."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    fake.fail_git = {"merge": "simulated merge failure"}
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 2
    assert not any(c[0] == "docker" for c in fake.calls)
    records = _deploys(tmp_path)
    assert records[-1]["result"].startswith("not_deployable: merge --ff-only failed")


# -- deploy: the graceful swap, for a change to the stack's definition --


def test_an_image_change_never_pauses(tmp_path):
    """Turns run in the conversations' own containers: rebuilding the control plane interrupts none of them."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    fake.claimed = 1  # Jensen busy
    host = Host(run=fake, live=live)

    assert deploy(host, State(tmp_path / "state"), wait_idle_s=1800, sleep=lambda s: None, clock=lambda: 0.0) == 0
    assert fake.swaps == ["up control services", "smoke"]
    assert ["docker", "compose", "build", "control", "services"] in fake.calls


def test_a_busy_runner_change_pauses_core_swaps_and_resumes(tmp_path):
    """Jensen busy the whole time: the deploy still lands -- core stops starting turns, the running turn gets the
    grace period, the container is swapped and core resumes. Nothing goes pending."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.claimed = 1  # never drops to 0
    clock = FakeClock()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, wait_idle_s=120, sleep=clock.sleep, clock=clock.time)

    assert result == 0
    assert fake.swaps == ["pause", "up --remove-orphans", "resume", "smoke"]  # resumed before the smoke turn
    assert fake.paused == "0"
    assert state.deploy_pending() is False
    assert _head(live) == _run(["git", "rev-parse", "origin/main"], cwd=live).stdout.strip()
    assert _deploys(tmp_path)[-1]["result"] == "ok"
    assert 120 <= sum(s for s in clock.slept if s == 30) <= 150  # the grace period, then the swap anyway


def test_an_idle_runner_change_still_pauses_around_the_swap(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    host = Host(run=fake, live=live)

    assert deploy(host, State(tmp_path / "state"), wait_idle_s=120, sleep=lambda s: None, clock=lambda: 0.0) == 0
    assert fake.swaps == ["pause", "up --remove-orphans", "resume", "smoke"]


def test_a_pause_someone_set_on_purpose_is_left_in_place(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.paused = "1"
    host = Host(run=fake, live=live)

    assert deploy(host, State(tmp_path / "state"), wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0) == 0
    assert fake.swaps == ["pause", "up --remove-orphans"]  # no resume, and no smoke turn a paused core would never start
    assert fake.paused == "1"


def test_resume_is_retried_while_core_comes_back(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.resume_failures = 3
    clock = FakeClock()
    host = Host(run=fake, live=live)

    assert deploy(host, State(tmp_path / "state"), wait_idle_s=0, sleep=clock.sleep, clock=clock.time) == 0
    assert fake.swaps[-2:] == ["resume", "smoke"] and fake.paused == "0"
    assert clock.slept.count(10) >= 3


def test_core_that_never_answers_resume_is_reported(tmp_path, capsys):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.resume_failures = 100
    host = Host(run=fake, live=live)

    deploy(host, State(tmp_path / "state"), wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0)

    assert "couldn't resume the control plane" in capsys.readouterr().err


def test_a_failed_swap_still_resumes(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.up_fails = True
    host = Host(run=fake, live=live)

    assert deploy(host, State(tmp_path / "state"), wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0) == 1
    assert fake.swaps == ["pause", "up --remove-orphans", "resume"]
    assert fake.paused == "0"


def test_core_down_for_whole_wait_still_deploys(tmp_path):
    """core being unreachable for the *entire* idle wait must not be treated as "still busy forever" -- nothing
    can have been dispatched with core down, and a revert that fixes core must be able to land."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")

    fake = FakeDocker()
    fake.core_unreachable = True  # every claimed probe raises, for the whole wait
    clock = FakeClock()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, wait_idle_s=1800, sleep=clock.sleep, clock=clock.time)

    assert result == 0
    assert ["docker", "compose", "build"] in fake.calls
    assert ["docker", "compose", "up", "-d", "--no-build", "--remove-orphans"] in fake.calls
    assert state.deploy_pending() is False


def test_a_recreated_network_container_moves_the_conversations_onto_it(tmp_path):
    """A conversation's container is tied to the network container by id: when a stack change recreates that one,
    every conversation's container, stopped ones too, is removed so agentd creates it afresh on the new network (a
    restart can't rejoin a network whose container is gone)."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")
    fake, ids, removed = FakeDocker(), iter(["old-id", "new-id"]), []

    def run(args, **kwargs):
        if args[:2] == ["docker", "inspect"] and args[-1] == "{{.Id}}":
            return subprocess.CompletedProcess(args, 0, next(ids) + "\n", "")
        if args[:4] == ["docker", "ps", "-a", "-q"]:
            assert "label=agent.conversation" in args
            return subprocess.CompletedProcess(args, 0, "c1\nc2\n", "")
        if args[:3] == ["docker", "rm", "-f"]:
            removed.append(args[3:])
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[:2] != ["docker", "restart"], "a restart can't rejoin a gone network"
        return fake(args, **kwargs)

    assert deploy(Host(run=run, live=live), State(tmp_path / "state"), wait_idle_s=0, sleep=lambda s: None,
                  clock=lambda: 0.0) == 0
    assert removed == [["c1", "c2"]]
    assert fake.swaps == ["pause", "up --remove-orphans", "resume", "smoke"]


# -- deploy: the second idle wait, after the build and before the recreate --


def _claim_during_build(fake: FakeDocker):
    """Flips `fake.claimed` to 1 the moment a `compose build` call happens -- simulating a turn getting
    claimed while the (multi-minute, in reality) image build is running. Returns the original `_compose`, so
    a test can restore it once the build-time claim has served its purpose."""
    original_compose = fake._compose

    def hook(rest):
        result = original_compose(rest)
        if rest[0] == "build":
            fake.claimed = 1
        return result

    fake._compose = hook
    return original_compose


# -- deploy: a successful rebuild, and the two-step build/up split --


def test_core_change_deploys_without_waiting(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    assert ["docker", "compose", "build", "control", "services"] in fake.calls
    assert ["docker", "compose", "up", "-d", "--no-build", "control", "services"] in fake.calls
    assert not any(c[:3] == ["docker", "compose", "up"] and "--build" in c for c in fake.calls)
    records = _deploys(tmp_path)
    assert len(records) == 1
    assert records[0]["services"] == ["control", "services"]
    assert records[0]["result"] == "ok"
    assert state.deploy_pending() is False
    assert fake.swaps == ["up control services", "smoke"]  # an image swap never pauses


def test_rename_from_runner_to_watch_still_rebuilds_the_agent_image(tmp_path):
    """`--no-renames` matters: a plain rename line carries only the new path, which alone maps to nothing."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/services/foo.py": "identical content\n"}, "add services file")
    _run(["git", "pull", "origin", "main"], cwd=live)  # `live`'s HEAD (the diff base) now has the old path

    # `git mv` in this git version doesn't auto-create the destination directory; move it by hand instead.
    (origin / "src/agent/services/foo.py").unlink()
    _run(["git", "add", "src/agent/services/foo.py"], cwd=origin)
    _commit(origin, {"src/agent/watch/foo.py": "identical content\n"}, "move runner file to watch")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    assert ["docker", "compose", "build", "control", "services"] in fake.calls
    assert ["docker", "compose", "up", "-d", "--no-build", "control", "services"] in fake.calls


def test_a_failed_build_is_retried_from_the_last_ok_sha_not_head(tmp_path):
    """A deploy whose fast-forward succeeded but whose build then failed must be retried next time, even though
    `HEAD` already sits at the new commit -- the base for "what changed" is the last *successful* deploy's `to`
    sha, not whatever `HEAD` happens to be right now."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "v1\n"}, "control plane change 1")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    # Establish a known-good baseline: this first deploy succeeds and logs "ok".
    baseline = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)
    assert baseline == 0
    assert _deploys(tmp_path)[0]["result"] == "ok"

    # A second core change whose build fails -- HEAD still gets fast-forwarded onto it.
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "v2\n"}, "control plane change 2")
    fake.build_fails = True
    failed = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)
    assert failed == 1
    assert _deploys(tmp_path)[1]["result"].startswith("check_failed: compose build exited")
    assert _head(live) == _run(["git", "rev-parse", "origin/main"], cwd=live).stdout.strip()  # ff'd anyway

    # No further commits on origin; a HEAD-based diff would now see nothing to do, since HEAD already sits at
    # "control plane change 2". The base must still be the last *ok* sha ("control plane change 1"), so the retry rebuilds.
    fake.build_fails = False
    fake.calls.clear()
    retried = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert retried == 0
    assert ["docker", "compose", "build", "control", "services"] in fake.calls
    assert _deploys(tmp_path)[2]["result"] == "ok"


def test_revert_after_check_failed_redeploys_and_logs(tmp_path):
    """The exact Critical repro: an `ok` deploy, then a core change whose build and recreate both succeed but
    whose *check* fails, then a revert back to the working content. With the base pinned to only the last-ok
    sha, `diff(last_ok, revert)` is empty -- the revert's tree matches the last-ok tree exactly -- so the broken
    container would stay up forever. The union must also diff from `HEAD` (the broken commit) to catch it."""
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "good\n"}, "control plane change 1")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    baseline = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)
    assert baseline == 0
    assert _deploys(tmp_path)[0]["result"] == "ok"

    _commit(origin, {"rust/crates/agentd/src/serve.rs": "bad\n"}, "control plane change 2 (breaks smoke)")
    fake.smoke_ok = False
    broken = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)
    assert broken == 1
    assert _deploys(tmp_path)[1]["result"].startswith("check_failed")
    assert ["docker", "compose", "up", "-d", "--no-build", "control", "services"] in fake.calls  # it did recreate, just failed

    # Revert: a new commit whose tree matches "control plane change 1" exactly.
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "good\n"}, "revert to control plane change 1")
    fake.smoke_ok = True
    fake.calls.clear()
    reverted = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert reverted == 0
    assert ["docker", "compose", "build", "control", "services"] in fake.calls
    assert ["docker", "compose", "up", "-d", "--no-build", "control", "services"] in fake.calls
    assert _head(live) == _run(["git", "rev-parse", "origin/main"], cwd=live).stdout.strip()
    records = _deploys(tmp_path)
    assert len(records) == 3
    assert records[2]["result"] == "ok"


# -- deploy: a failed compose step or check --


def test_compose_build_failure_returns_1_and_is_logged(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    fake.build_fails = True
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 1
    assert not any(c[:3] == ["docker", "compose", "up"] for c in fake.calls)  # never reached
    records = _deploys(tmp_path)
    assert len(records) == 1
    assert records[0]["result"] == "check_failed: compose build exited 1"
    assert state.deploy_pending() is False


def test_compose_up_failure_returns_1_and_is_logged(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    fake.up_fails = True
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 1
    records = _deploys(tmp_path)
    assert records[0]["result"] == "check_failed: compose up exited 1"


def test_failed_check_returns_1_and_is_logged(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")

    fake = FakeDocker()
    fake.smoke_ok = False
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 1
    records = _deploys(tmp_path)
    assert len(records) == 1
    assert records[0]["result"].startswith("check_failed")
    # Even a failed check clears the flag -- there is nothing left "pending" about this attempt.
    assert state.deploy_pending() is False


# -- deploy: if-pending, the lock --


def test_if_pending_without_flag_does_nothing(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"src/agent/services/foo.py": "x\n"}, "services change")

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")
    assert state.deploy_pending() is False

    result = deploy(host, state, if_pending=True, sleep=lambda s: None, clock=lambda: 0.0)

    assert result == 0
    assert fake.calls == []  # never even fetched


def test_lock_held_returns_pending(tmp_path):
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
        # Never actually attempted a deploy -- no fetch, no docker/compose -- but reading HEAD for the pending
        # log entry is a safe, read-only git call, allowed even without the lock.
        assert not any(c[0] == "docker" for c in fake.calls)
        assert not any(c[:2] == ["git", "fetch"] for c in fake.calls)
        assert state.deploy_pending() is True
        records = _deploys(tmp_path)
        assert len(records) == 1
        assert records[0]["result"] == "pending"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_lock_released_after_locked_body_raises(tmp_path):
    origin, live = _repo_pair(tmp_path)
    fake = FakeDocker()
    fake.raise_on_git = {"fetch"}
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    with pytest.raises(RuntimeError):
        deploy(host, state, sleep=lambda s: None, clock=lambda: 0.0)

    # A fresh deploy() must be able to acquire the lock right away -- if it were still held, a 0s wait would
    # time out (return 3) instead of actually running (and finding nothing new to do: return 0).
    fake2 = FakeDocker()
    host2 = Host(run=fake2, live=live)
    result = deploy(host2, state, wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0)
    assert result == 0


# -- check --


def test_check_detects_a_not_running_container():
    fake = FakeDocker()
    fake.unhealthy = {"agent-control"}
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"control"}, sleep=lambda s: None)

    assert ok is False
    assert "agent-control" in reason


def test_check_passes_when_everything_is_healthy():
    fake = FakeDocker()
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"control"}, sleep=lambda s: None)

    assert ok is True
    assert reason == ""


def test_check_star_covers_every_container_in_the_map():
    fake = FakeDocker()
    host = Host(run=fake, live=Path("/nonexistent"))

    ok, reason = check(host, {"*"}, sleep=lambda s: None)

    assert ok is True
    inspected = {c[2] for c in fake.calls if c[:2] == ["docker", "inspect"]}
    assert inspected == {"agent-control", "agent-services", "agent-supervisor", "agent-supervisor-standby",
                          "agent-relay", "agent-docker"}


# -- CLI --


def test_dry_run_prints_services_and_changes_nothing(tmp_path, capsys):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"rust/crates/agentd/src/serve.rs": "x\n"}, "control plane change")
    head_before = _head(live)

    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = main(["--dry-run"], host=host, state=state)

    assert result == 0
    assert _head(live) == head_before
    assert not any(c[0] == "docker" for c in fake.calls)
    assert state.deploy_pending() is False
    out = capsys.readouterr().out
    assert "control" in out
    assert "rust/crates/agentd/src/serve.rs" in out


def test_cli_wires_if_pending(tmp_path):
    origin, live = _repo_pair(tmp_path)
    fake = FakeDocker()
    host = Host(run=fake, live=live)
    state = State(tmp_path / "state")

    result = main(["--if-pending"], host=host, state=state)

    assert result == 0
    assert fake.calls == []


# -- a deploy never leaves core paused --


def test_the_paused_marker_is_there_during_the_swap_and_gone_after(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")
    fake = FakeDocker()
    state = State(tmp_path / "state")
    seen = []
    original = fake._compose

    def compose(rest):
        if rest[0] == "up":
            seen.append((state.root / "deploy-paused").exists())
        return original(rest)

    fake._compose = compose
    assert deploy(Host(run=fake, live=live), state, wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0) == 0
    assert seen == [True]
    assert not (state.root / "deploy-paused").exists()


def test_a_resume_that_fails_leaves_the_marker_for_the_listener(tmp_path):
    origin, live = _repo_pair(tmp_path)
    _commit(origin, {"compose.yaml": "x\n"}, "stack change")
    fake = FakeDocker()
    fake.resume_failures = 100
    state = State(tmp_path / "state")
    deploy(Host(run=fake, live=live), state, wait_idle_s=0, sleep=lambda s: None, clock=lambda: 0.0)
    assert (state.root / "deploy-paused").exists() and fake.paused == "1"

    fake.resume_failures = 0
    assert resume_if_left_paused(Host(run=fake, live=live), state) is True
    assert fake.paused == "0" and not (state.root / "deploy-paused").exists()


def test_a_running_deploy_is_never_resumed_from_outside(tmp_path):
    import fcntl as _fcntl
    fake = FakeDocker()
    fake.paused = "1"
    state = State(tmp_path / "state")
    (state.root / "deploy-paused").write_text("1\n")
    with open(state.root / "deploy.lock", "a+") as held:
        _fcntl.flock(held, _fcntl.LOCK_EX)
        assert resume_if_left_paused(Host(run=fake, live=tmp_path), state) is False
    assert fake.paused == "1"


def test_nothing_to_resume_without_a_marker(tmp_path):
    fake = FakeDocker()
    assert resume_if_left_paused(Host(run=fake, live=tmp_path), State(tmp_path / "state")) is False
    assert fake.calls == []


def test_sigterm_unwinds_so_the_finally_resumes():
    import signal as _signal
    import pytest as _pytest
    from agent.watch.deploy import _stop_cleanly
    with _pytest.raises(SystemExit) as exc:
        _stop_cleanly(_signal.SIGTERM, None)
    assert exc.value.code == 128 + _signal.SIGTERM
