"""`just deploy`: fetch `origin/main` into the live checkout, work out which Compose services the changed paths
touch, rebuild only those, wait for an idle runner before touching `agent`, and check the stack is actually
healthy live before calling the deploy done.

`services_for` maps changed paths to service names (see the table in the plan's Task 6). `deploy` runs the whole
pipeline under a host-wide `fcntl.flock` on `<state>/deploy.lock`, so two jobs (a person and a workflow run, or
two workflow runs) never deploy at once; `check` is the live probe after a rebuild that decides whether a deploy
counts as `ok` or `check_failed`. Every wait here -- the lock poll, the idle-runner poll, the container-stability
wait inside `check` -- takes `sleep`/`clock` arguments (defaulting to `time.sleep`/`time.monotonic`), so a test
can inject a fake pair and run the whole thing without a real delay; `deploy` forwards its own `sleep` into
`check` for the same reason, rather than letting `check`'s own default take over partway through.

Git state is read and changed with `host.run(["git", ...], cwd=host.live)` -- never a bare `subprocess` call --
so a test's fake `Host` can tell a `git` command from a `docker`/`docker compose` one apart and let the former
run for real against a tmp checkout while only the latter gets faked.

A checkout that is dirty or has drifted from `origin/main` (an uncommitted change, a local commit never pushed,
any other non-fast-forward) fails loudly with exit 2 before anything is fetched into place or rebuilt -- this is
Review Focus 4 of the plan: a drifted live install must never get silently papered over by a deploy.

Whether `agent`'s own queue is idle is read from core with `core_collect.py` (see `Host.core_python`), the same
script the hourly watch itself uses; a failed probe (core down mid-deploy, a stale lock inside the container)
counts as "still busy," never as "idle," so a deploy never rebuilds the runner out from under a turn it merely
failed to see.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from agent.watch.host import Host
from agent.watch.state import FMT, State

# -- services_for --

_RUNNER_PREFIX = "src/agent/runner/"
_SUPERVISOR_PATH = "src/agent/supervisor.py"
_CORE_PREFIXES = ("src/agent/core/", "src/agent/dashboard/", "src/agent/defaults/", "src/agent/plugins/",
                   "src/agent/connectors/", "dashboard/")
_CORE_EXTRA_PATHS = {"scripts/smoke.py"}
_NOTHING_PREFIXES = ("src/agent/watch/", "docs/", "tests/", ".github/", "deploy/", "examples/", "scripts/")
_NOTHING_EXTRA_PATHS = {"README.md", "justfile"}
_SHARED_EXTRA_PATHS = {"Dockerfile", "pyproject.toml"}
_COMPOSE_PATH = "compose.yaml"

_SHARED_SERVICES = frozenset({"core", "agent", "supervisor", "supervisor-standby"})
_SUPERVISOR_SERVICES = frozenset({"supervisor", "supervisor-standby"})

# Compose service name -> the container Compose gives it, for `check` and the health/smoke probes.
CONTAINER_NAMES = {
    "core": "agent-core",
    "agent": "agent-runner",
    "supervisor": "agent-supervisor",
    "supervisor-standby": "agent-supervisor-standby",
    "relay": "agent-relay",
    "docker": "agent-docker",
}

_LOCK_POLL_S = 5
_IDLE_POLL_S = 30


def _services_for_one(path: str) -> frozenset[str]:
    if path.startswith(_RUNNER_PREFIX):
        return frozenset({"agent"})
    if path == _SUPERVISOR_PATH:
        return _SUPERVISOR_SERVICES
    if path.startswith(_CORE_PREFIXES) or path in _CORE_EXTRA_PATHS:
        return frozenset({"core"})
    if path == _COMPOSE_PATH:
        return frozenset({"*"})
    if path.startswith(_NOTHING_PREFIXES) or path in _NOTHING_EXTRA_PATHS:
        return frozenset()
    if path.startswith("src/agent/") or path in _SHARED_EXTRA_PATHS:
        return _SHARED_SERVICES
    return frozenset({"core"})


def services_for(paths: Iterable[str]) -> set[str]:
    """Which Compose services a deploy needs to rebuild for `paths` (`git diff --name-only`), unioned across all
    of them. See the module docstring's table for the mapping; `"*"` (a `compose.yaml` change) means every
    service, since the stack definition itself might have changed in a way no single service name covers."""
    services: set[str] = set()
    for path in paths:
        services |= _services_for_one(path)
    return services


# -- git helpers --


def _git(host: Host, args: list[str]):
    return host.run(["git", *args], cwd=host.live, capture_output=True, text=True)


def _rev_parse(host: Host, ref: str) -> str:
    return _git(host, ["rev-parse", ref]).stdout.strip()


# -- the idle-runner probe --


def _claimed(host: Host) -> int | None:
    """core's current `claimed` count, or `None` if the probe itself raised -- treated as "still busy," never
    as idle, so a core that is unreachable mid-deploy never looks like a good time to rebuild the runner."""
    now = datetime.now(UTC).strftime(FMT)
    try:
        raw = host.core_python("core_collect.py", ["--since", now, "--until", now])
        return json.loads(raw)["claimed"]
    except Exception:
        return None


# -- check --


def _inspect(host: Host, container: str) -> tuple[bool, int] | None:
    """`(running, restart_count)` for `container`, or `None` if `docker inspect` itself failed -- a missing or
    stopped container looks the same as "not running" to a deploy check."""
    result = host.run(["docker", "inspect", container, "--format", "{{.State.Running}} {{.RestartCount}}"],
                       capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        running, restart_count = result.stdout.split()
        return running == "true", int(restart_count)
    except (ValueError, AttributeError):
        return None


def _health_probe(host: Host, path: str) -> bool:
    code = (f"import sys,urllib.request; sys.exit(0 if urllib.request.urlopen("
            f"'http://localhost:8080{path}', timeout=10).status == 200 else 1)")
    result = host.run(["docker", "exec", "agent-core", "python", "-c", code], capture_output=True, text=True)
    return result.returncode == 0


def check(host: Host, services: set[str], *, stable_s: int = 120, sleep=time.sleep) -> tuple[bool, str]:
    """The live probe after a rebuild: `(ok, reason)`, `reason` naming the first thing that failed (empty on
    success).

    - Every rebuilt container is `Running`, and its `RestartCount` doesn't move across a `stable_s` wait -- a
      container that is crash-looping can still show `Running` on one snapshot.
    - core's `/health` and `/health/runner` both answer 200, read from inside `agent-core` itself (no port is
      published to the host for either).
    - `docker compose exec -T core python /app/scripts/smoke.py` prints `smoke ok` -- a real turn end to end.

    `"*"` in `services` (a `compose.yaml` change) checks every container named in `CONTAINER_NAMES`, since the
    stack definition itself changed and no single service name can be trusted to cover what else did.

    The smoke probe runs through `host.run` directly rather than `Host.compose` -- `compose()` never passes
    `capture_output` (it is meant for a build a person watches stream by), so reading its `stdout` here would
    always see `None`; building the identical `docker compose exec -T core ...` argv through `host.run` instead
    gets the captured text this probe actually needs.
    """
    containers = (list(CONTAINER_NAMES.values()) if "*" in services
                 else [CONTAINER_NAMES[s] for s in sorted(services) if s in CONTAINER_NAMES])

    before: dict[str, int] = {}
    for container in containers:
        info = _inspect(host, container)
        if info is None or not info[0]:
            return False, f"{container} is not running"
        before[container] = info[1]

    sleep(stable_s)

    for container in containers:
        info = _inspect(host, container)
        if info is None or not info[0]:
            return False, f"{container} is not running"
        if info[1] != before[container]:
            return False, f"{container} restarted"

    for path in ("/health", "/health/runner"):
        if not _health_probe(host, path):
            return False, f"{path} did not return 200"

    smoke = host.run(["docker", "compose", "exec", "-T", "core", "python", "/app/scripts/smoke.py"],
                      cwd=host.live, capture_output=True, text=True)
    if "smoke ok" not in (smoke.stdout or ""):
        return False, "smoke test did not print smoke ok"

    return True, ""


# -- deploy --


def _iso_now() -> str:
    return datetime.now(UTC).strftime(FMT)


def deploy(host: Host, state: State, *, wait_idle_s: int = 1800, if_pending: bool = False,
           sleep=time.sleep, clock=time.monotonic) -> int:
    """Fetch, decide what to rebuild, wait for an idle runner if needed, fast-forward, rebuild and check.

    Exit codes: 0 deployed and checked (or nothing to deploy); 1 the post-deploy check failed; 2 the checkout
    isn't deployable (dirty or diverged -- Review Focus 4); 3 pending (a lock held by another deploy, or `agent`
    never went idle within `wait_idle_s`).
    """
    if if_pending and not state.deploy_pending():
        return 0

    lock_path = state.root / "deploy.lock"
    lock_file = open(lock_path, "a+")
    try:
        deadline = clock() + wait_idle_s
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if clock() >= deadline:
                    return 3
                sleep(_LOCK_POLL_S)

        return _deploy_locked(host, state, wait_idle_s=wait_idle_s, sleep=sleep, clock=clock)
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def _deploy_locked(host: Host, state: State, *, wait_idle_s: int, sleep, clock) -> int:
    _git(host, ["fetch", "origin", "main"])

    status = _git(host, ["status", "--porcelain"])
    ancestor = _git(host, ["merge-base", "--is-ancestor", "HEAD", "origin/main"])
    if status.stdout.strip() or ancestor.returncode != 0:
        reason = ("the checkout has uncommitted changes" if status.stdout.strip()
                  else "HEAD is not an ancestor of origin/main")
        print(f"deploy: not deployable: {reason}")
        return 2

    diff = _git(host, ["diff", "--name-only", "HEAD", "origin/main"])
    paths = [line for line in diff.stdout.splitlines() if line]
    if not paths:
        return 0

    old_sha = _rev_parse(host, "HEAD")
    target_sha = _rev_parse(host, "origin/main")
    services = services_for(paths)

    if services & {"agent", "*"}:
        deadline = clock() + wait_idle_s
        while True:
            claimed = _claimed(host)
            if claimed == 0:
                break
            if clock() >= deadline:
                state.set_deploy_pending(True)
                state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": target_sha,
                                  "services": sorted(services), "result": "pending"})
                return 3
            sleep(_IDLE_POLL_S)

    _git(host, ["merge", "--ff-only", "origin/main"])
    new_sha = _rev_parse(host, "HEAD")

    if not services:
        state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": new_sha, "services": [], "result": "ok"})
        state.set_deploy_pending(False)
        return 0

    build_args = ["up", "-d", "--build"] if "*" in services else ["up", "-d", "--build", *sorted(services)]
    host.compose(build_args)

    ok, reason = check(host, services, sleep=sleep)
    result = "ok" if ok else f"check_failed: {reason}"
    state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": new_sha, "services": sorted(services),
                       "result": result})
    state.set_deploy_pending(False)
    return 0 if ok else 1


# -- CLI --


def _dry_run(host: Host) -> int:
    _git(host, ["fetch", "origin", "main"])
    diff = _git(host, ["diff", "--name-only", "HEAD", "origin/main"])
    paths = [line for line in diff.stdout.splitlines() if line]
    services = services_for(paths)
    print(f"services: {sorted(services)}")
    print("paths:")
    for path in paths:
        print(f"  {path}")
    return 0


def main(argv: list[str] | None = None, *, host: Host | None = None, state: State | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--wait-idle-s", type=int, default=1800)
    parser.add_argument("--if-pending", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    host = Host.default() if host is None else host
    state = State.default() if state is None else state

    if args.dry_run:
        return _dry_run(host)

    return deploy(host, state, wait_idle_s=args.wait_idle_s, if_pending=args.if_pending)


if __name__ == "__main__":
    sys.exit(main())
