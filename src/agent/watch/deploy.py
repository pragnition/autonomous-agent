"""`just deploy`: fetch `origin/main` into the live checkout, work out which Compose services the changed paths
touch, rebuild only those, wait for an idle runner before touching `agent`, and check the stack is actually
healthy live before calling the deploy done.

`services_for` maps changed paths to service names (see the table in the plan's Task 6). `deploy` runs the whole
pipeline under a host-wide `fcntl.flock` on `<state>/deploy.lock`, so two jobs never deploy at once; `check` is
the live probe after a rebuild that decides whether a deploy counts as `ok` or `check_failed`. Every wait here --
the lock poll, the two idle-runner polls, the container-stability wait inside `check` -- takes `sleep`/`clock`
arguments (defaulting to `time.sleep`/`time.monotonic`), so a test can inject a fake pair and run the whole thing
without a real delay; `deploy` forwards its own `sleep` into `check` for the same reason.

Git state is read and changed with `host.run(["git", ...], cwd=host.live)` -- never a bare `subprocess` call --
so a test's fake `Host` can tell a `git` command from a `docker`/`docker compose` one apart and let the former
run for real against a tmp checkout while only the latter gets faked.

A checkout that is dirty or has drifted from `origin/main` (an uncommitted change, a local commit never pushed,
a failed fetch, a non-fast-forward) fails loudly with exit 2 before anything is rebuilt, and every such failure
is logged to `deploys.jsonl` as `not_deployable: <reason>` -- this is Review Focus 4 of the plan: a drifted live
install must never get silently papered over by a deploy. Since waiting for an idle runner can take up to
`wait_idle_s` (30 minutes), the drift check runs twice: once right after the fetch, and again right before the
fast-forward merge, in case the checkout itself moved while this run was waiting.

The rebuild itself is two steps, not one, so a multi-minute image build can never recreate a running container
out from under a turn that started mid-build: `docker compose build <services>` first (builds images, touches no
running container), then, only if `agent`'s own queue needs to be idle for this change, a *second* idle check
(the first one, before the merge, is stale by the time a slow build finishes), and only then `docker compose up
-d --no-build <services>` to actually recreate containers. Every Compose step's exit code is checked; a
non-zero one is `check_failed: compose <step> exited N`, never silently treated as `ok`.

What actually gets rebuilt is decided from a different diff than "HEAD vs origin/main": it is `<base> vs
origin/main`, where `<base>` is the `to` sha of the last deploy that logged `"ok"`, if that sha is still an
ancestor of `origin/main` -- not plain `HEAD`. A deploy whose fast-forward succeeded but whose build or check
then failed still leaves `HEAD` sitting at the new commit; diffing from `HEAD` on the next run would see no
difference and quietly never retry the services that never actually came up. Diffing with `--no-renames` matters
for the same reason from the other direction: a file moved from one service's directory to another's must show
up as both its old and new path (a plain rename line would carry only the new path, hiding whichever side the
old path's own rule would have triggered).

A rebuild is "pending" (exit 3, retried by the next hourly run) when the host-wide lock is held by another
deploy for the whole `wait_idle_s` wait, or when `agent`'s queue never goes idle in time, in which case
`state.set_deploy_pending(True)` and a `"pending"` log record are both written. The flag converges back to
`False` as soon as a run commits to actually landing the pending change -- right after the pre-merge drift
check passes, not at the very end -- and also on the "nothing new since the last attempt" path, so a pending
flag never outlives whatever it was flagging. A `claimed` probe that raises counts as "still busy" the way any
other busy reading does -- *unless every single poll in one wait raised*, which means core itself was
unreachable for the entire wait: nothing can have been dispatched to the runner in that case, so proceeding
(rather than going pending) is the safer failure mode -- the deploy might be exactly the revert that brings core
back.
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
_DOCKERFILE_PATH = "Dockerfile"
_PYPROJECT_PATH = "pyproject.toml"
_COMPOSE_PATH = "compose.yaml"

# core/agent/supervisor/supervisor-standby are all built from the same Dockerfile's Python base; relay has its
# own stage in the same Dockerfile (see compose.yaml's comment on the `relay` service), so a Dockerfile change
# touches it too, but a pyproject.toml change (Python dependencies) does not -- relay's stage never installs them.
_SHARED_SERVICES = frozenset({"core", "agent", "supervisor", "supervisor-standby"})
_DOCKERFILE_SERVICES = _SHARED_SERVICES | {"relay"}
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
    if path == _DOCKERFILE_PATH:
        return _DOCKERFILE_SERVICES
    if path.startswith("src/agent/") or path == _PYPROJECT_PATH:
        return _SHARED_SERVICES
    return frozenset({"core"})


def services_for(paths: Iterable[str]) -> set[str]:
    """Which Compose services a deploy needs to rebuild for `paths` (a `--no-renames` `git diff --name-only`),
    unioned across all of them. See the module docstring's table for the mapping; `"*"` (a `compose.yaml`
    change) means every service, since the stack definition itself might have changed in a way no single
    service name covers."""
    services: set[str] = set()
    for path in paths:
        services |= _services_for_one(path)
    return services


# -- git helpers --


def _git(host: Host, args: list[str]):
    return host.run(["git", *args], cwd=host.live, capture_output=True, text=True)


def _rev_parse(host: Host, ref: str) -> str:
    return _git(host, ["rev-parse", ref]).stdout.strip()


def _rev_parse_or_none(host: Host, ref: str) -> str | None:
    result = _git(host, ["rev-parse", ref])
    return result.stdout.strip() if result.returncode == 0 else None


def _drift_reason(host: Host) -> str | None:
    """`None` if the checkout is clean and `HEAD` is an ancestor of `origin/main`; otherwise a short reason."""
    status = _git(host, ["status", "--porcelain"])
    if status.stdout.strip():
        return "the checkout has uncommitted changes"
    ancestor = _git(host, ["merge-base", "--is-ancestor", "HEAD", "origin/main"])
    if ancestor.returncode != 0:
        return "HEAD is not an ancestor of origin/main"
    return None


def _last_ok_sha(state: State) -> str | None:
    """The `to` sha of the most recent `deploys.jsonl` record with `"result": "ok"`, or `None`."""
    path = state.root / "deploys.jsonl"
    if not path.exists():
        return None
    last: str | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("result") == "ok" and isinstance(record.get("to"), str):
            last = record["to"]
    return last


def _base_sha(host: Host, state: State) -> str:
    """What to diff `origin/main` against to decide what needs rebuilding: the last successful deploy's `to`
    sha, if the deploys log has one and it is still an ancestor of `origin/main` (so a build or check that
    failed after an earlier fast-forward gets retried, rather than looking like "nothing changed" because `HEAD`
    already sits at that commit); `HEAD` otherwise."""
    last_ok = _last_ok_sha(state)
    if last_ok:
        ancestor = _git(host, ["merge-base", "--is-ancestor", last_ok, "origin/main"])
        if ancestor.returncode == 0:
            return last_ok
    return _rev_parse(host, "HEAD")


# -- the idle-runner probe --


def _claimed(host: Host) -> int | None:
    """core's current `claimed` count, or `None` if the probe itself raised."""
    now = datetime.now(UTC).strftime(FMT)
    try:
        raw = host.core_python("core_collect.py", ["--since", now, "--until", now])
        return json.loads(raw)["claimed"]
    except Exception:
        return None


def _wait_for_idle(host: Host, wait_idle_s: int, sleep, clock) -> bool:
    """Poll `claimed` every 30s until it is 0, or `wait_idle_s` runs out. A probe that raises counts as busy --
    *unless every single poll in this call raised*, meaning core was unreachable for the entire wait: nothing
    could have been dispatched in that case, so this returns `True` (proceed) rather than `False` (pending)."""
    deadline = clock() + wait_idle_s
    saw_a_real_reading = False
    while True:
        claimed = _claimed(host)
        if claimed is not None:
            saw_a_real_reading = True
            if claimed == 0:
                return True
        if clock() >= deadline:
            return not saw_a_real_reading
        sleep(_IDLE_POLL_S)


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


def _log_not_deployable(host: Host, state: State, reason: str) -> int:
    print(f"deploy: not deployable: {reason}")
    state.log_deploy({"ts": _iso_now(), "from": _rev_parse_or_none(host, "HEAD"),
                       "to": _rev_parse_or_none(host, "origin/main"), "services": [],
                       "result": f"not_deployable: {reason}"})
    return 2


def _log_check_failed(state: State, from_sha: str, to_sha: str, services: set[str], reason: str) -> int:
    state.log_deploy({"ts": _iso_now(), "from": from_sha, "to": to_sha, "services": sorted(services),
                       "result": f"check_failed: {reason}"})
    return 1


def _log_pending(state: State, from_sha: str | None, to_sha: str | None, services: set[str]) -> int:
    state.set_deploy_pending(True)
    state.log_deploy({"ts": _iso_now(), "from": from_sha, "to": to_sha, "services": sorted(services),
                       "result": "pending"})
    return 3


def deploy(host: Host, state: State, *, wait_idle_s: int = 1800, if_pending: bool = False,
           sleep=time.sleep, clock=time.monotonic) -> int:
    """Fetch, decide what to rebuild, wait for an idle runner if needed, fast-forward, rebuild and check.

    Exit codes: 0 deployed and checked (or nothing to deploy); 1 the post-deploy check failed; 2 the checkout
    isn't deployable (dirty, diverged, a failed fetch or a failed fast-forward -- Review Focus 4); 3 pending (the
    lock held by another deploy, or `agent` never went idle within `wait_idle_s`).
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
                    return _log_pending(state, _rev_parse_or_none(host, "HEAD"), None, set())
                sleep(_LOCK_POLL_S)

        return _deploy_locked(host, state, wait_idle_s=wait_idle_s, sleep=sleep, clock=clock)
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def _deploy_locked(host: Host, state: State, *, wait_idle_s: int, sleep, clock) -> int:
    fetch = _git(host, ["fetch", "origin", "main"])
    if fetch.returncode != 0:
        return _log_not_deployable(host, state, "fetch failed")

    reason = _drift_reason(host)
    if reason:
        return _log_not_deployable(host, state, reason)

    base = _base_sha(host, state)
    diff = _git(host, ["diff", "--name-only", "--no-renames", base, "origin/main"])
    paths = [line for line in diff.stdout.splitlines() if line]
    if not paths:
        state.set_deploy_pending(False)
        return 0

    services = services_for(paths)

    if services & {"agent", "*"}:
        if not _wait_for_idle(host, wait_idle_s, sleep, clock):
            return _log_pending(state, _rev_parse_or_none(host, "HEAD"), _rev_parse_or_none(host, "origin/main"),
                                 services)

    # The checkout may have moved while this run was waiting for the runner to go idle; re-check right before
    # committing to the fast-forward (Review Focus 4).
    reason = _drift_reason(host)
    if reason:
        return _log_not_deployable(host, state, reason)

    # Past this point the run is committed to landing everything up to `origin/main` -- clear pending here, not
    # at the end, so a pending flag never outlives the attempt it was flagging.
    state.set_deploy_pending(False)

    old_sha = _rev_parse(host, "HEAD")
    merge = _git(host, ["merge", "--ff-only", "origin/main"])
    if merge.returncode != 0:
        return _log_not_deployable(host, state, f"merge --ff-only failed: {(merge.stderr or merge.stdout).strip()}")
    new_sha = _rev_parse(host, "HEAD")

    if not services:
        state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": new_sha, "services": [], "result": "ok"})
        return 0

    build_args = ["build"] + ([] if "*" in services else sorted(services))
    build_result = host.compose(build_args)
    if build_result.returncode != 0:
        reason = f"compose build exited {build_result.returncode}"
        return _log_check_failed(state, old_sha, new_sha, services, reason)

    # The build can take minutes; a turn could have been claimed in the meantime, so `agent`/`*` needs a second,
    # fresh idle check right before the containers that actually run it get recreated.
    if services & {"agent", "*"}:
        if not _wait_for_idle(host, wait_idle_s, sleep, clock):
            return _log_pending(state, old_sha, new_sha, services)

    up_args = (["up", "-d", "--no-build", "--remove-orphans"] if "*" in services
              else ["up", "-d", "--no-build", *sorted(services)])
    up_result = host.compose(up_args)
    if up_result.returncode != 0:
        return _log_check_failed(state, old_sha, new_sha, services, f"compose up exited {up_result.returncode}")

    ok, reason = check(host, services, sleep=sleep)
    result = "ok" if ok else f"check_failed: {reason}"
    state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": new_sha, "services": sorted(services),
                       "result": result})
    return 0 if ok else 1


# -- CLI --


def _dry_run(host: Host, state: State) -> int:
    _git(host, ["fetch", "origin", "main"])
    base = _base_sha(host, state)
    diff = _git(host, ["diff", "--name-only", "--no-renames", base, "origin/main"])
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
        return _dry_run(host, state)

    return deploy(host, state, wait_idle_s=args.wait_idle_s, if_pending=args.if_pending)


if __name__ == "__main__":
    sys.exit(main())
