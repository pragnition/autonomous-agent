"""`just deploy`: fetch `origin/main` into the live checkout, work out which Compose services the changed paths
touch, rebuild only those (a change to the stack's definition swapped gracefully: pause, swap, resume), and check the
stack is actually healthy live before calling the deploy done.

`services_for` maps changed paths to service names (the rules in `_services_for_one`). `deploy` runs the whole
pipeline under a host-wide `fcntl.flock` on `<state>/deploy.lock`, so two jobs never deploy at once; `check` is
the live probe after a rebuild that decides whether a deploy counts as `ok` or `check_failed`. Every wait here --
the lock poll, the idle-runner grace period, the resume retries, the container-stability wait inside `check` --
takes `sleep`/`clock` arguments (defaulting to `time.sleep`/`time.monotonic`), so a test can inject a fake pair and
run the whole thing
without a real delay; `deploy` forwards its own `sleep` into `check` for the same reason.

Git state is read and changed with `host.run(["git", ...], cwd=host.live)` -- never a bare `subprocess` call --
so a test's fake `Host` can tell a `git` command from a `docker`/`docker compose` one apart and let the former
run for real against a tmp checkout while only the latter gets faked.

**The fetch target is pinned.** Right after `git fetch origin main` succeeds, `target = git rev-parse
origin/main` is read exactly once and used for everything downstream (every diff, every ancestor check, `merge
--ff-only <target>`, the log's `to`) -- never the symbolic `origin/main` again. A concurrent `git fetch` from
another process (a person's `just deploy --dry-run`, say) can move that ref mid-run; pinning means this run
only ever lands the commit it actually planned around.

A checkout that is dirty or has drifted from `target` (an uncommitted change, a local commit never pushed, a
failed fetch, a non-fast-forward) fails loudly with exit 2 before anything is rebuilt, and every such failure is
logged to `deploys.jsonl` as `not_deployable: <reason>`: a drifted live install must never get silently papered
over by a deploy. The drift check runs right after the fetch, before anything is merged or built.

**What gets rebuilt is not a single `HEAD`-vs-`target` diff.** It is the union, over every *base* in a small set
of candidate commits, of `services_for(git diff --name-only --no-renames <base> target)`. The candidates are
`HEAD`; the `to` sha of the most recent `"result": "ok"` deploy record (if any); and the `from`/`to` shas of
every `pending` or `check_failed` record logged since that last `"ok"` one (or, if there has never been one, of
every such record ever) -- each kept only if it still exists and is an ancestor of `target`. A single `HEAD`- or
last-ok-only diff misses real cases: a deploy that fast-forwarded `HEAD` onto a commit whose build or check then
failed leaves `HEAD` sitting past that commit, so a later commit that reverts the change back to the last-ok
content diffs as empty against the last-ok sha even though `HEAD` (and the services it actually needs) moved in
between -- the `HEAD` base in the union catches exactly that. Symmetrically, a deploy whose fast-forward
succeeded but whose *second*, post-build idle wait then timed out (pending) also leaves `HEAD` at the new
commit; with no `"ok"` record yet to anchor to, a plain `HEAD`-vs-`target` diff on the retry would see nothing
and the pending rebuild would be lost -- the pending record's own `from` sha (the pre-merge commit) is in the
union specifically to keep that change visible as something still to rebuild. Diffing with `--no-renames`
matters for the same "don't miss real work" reason from another angle: a file moved from one service's
directory to another's must show up as both its old and new path (a plain rename line would carry only the new
path, hiding whichever side the old path's own rule would have triggered). If the union ends up empty but `HEAD`
is still behind `target` (a docs-only change, or nothing outstanding needs rebuilding), the checkout still
fast-forwards and logs `"ok"` -- `HEAD` and `target` stay level either way.

The rebuild itself is two steps, not one: `docker compose build <services>` first (builds images while Jensen keeps
working, touches no running container), then `docker compose up -d --no-build <services>` to recreate containers.
Turns run in the conversations' own containers, so recreating agent-control or agent-services interrupts none of
them. When `*` (a `compose.yaml` change) is among them, the network container the conversations share may be
recreated, so that swap is graceful: the queue is paused (only if nobody had paused it on purpose), a running turn
gets up to `wait_idle_s` to finish, the stack is swapped, the conversations' containers are restarted onto the new
network if it changed, and the control plane resumes -- in a `finally`, retried while it comes back, so a deploy
never leaves Jensen paused. Every Compose step's exit code is checked; a non-zero one is `check_failed: compose
<step> exited N`, never silently treated as `ok`.

A rebuild is "pending" (exit 3, retried by the next hourly run) only when the host-wide lock is held by another
deploy for the whole `wait_idle_s` wait: `state.set_deploy_pending(True)` and a `"pending"` log record are both
written. The flag converges back to `False` as soon as a run commits to actually landing the pending change --
right after the pre-merge drift check passes, not at the very end -- and also on the "nothing outstanding" path,
so a pending flag never outlives whatever it was flagging.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from agent.watch.host import CORE_CONTAINER, Host
from agent.watch.state import FMT, State

# -- services_for --

_SUPERVISOR_PATH = "src/agent/supervisor.py"
# Used only from the agent image, which agent-control (agentd, the dashboard's web app, the defaults and plugins it
# hands the sessions, the Python it keeps for a rollback) and agent-services (the agent's own services) both run
_AGENT_IMAGE_PREFIXES = ("src/agent/runner/", "src/agent/core/", "src/agent/dashboard/", "src/agent/defaults/",
                         "src/agent/plugins/", "src/agent/connectors/", "rust/", "dashboard/")
_NOTHING_PREFIXES = ("src/agent/watch/", "docs/", "tests/", ".github/", "deploy/", "examples/", "scripts/", "spike/")
_NOTHING_EXTRA_PATHS = {"README.md", "justfile"}
_DOCKERFILE_PATH = "Dockerfile"
_PYPROJECT_PATH = "pyproject.toml"
_COMPOSE_PATH = "compose.yaml"

_AGENT_IMAGE_SERVICES = frozenset({"control", "services"})
_SUPERVISOR_SERVICES = frozenset({"supervisor", "supervisor-standby"})
# the Python base: the agent image and the supervisors; relay has its own Dockerfile stage
_SHARED_SERVICES = _AGENT_IMAGE_SERVICES | _SUPERVISOR_SERVICES
_DOCKERFILE_SERVICES = _SHARED_SERVICES | {"relay"}

# Compose service name -> the container Compose gives it, for `check` and the health/smoke probes.
CONTAINER_NAMES = {
    "control": "agent-control",
    "services": "agent-services",
    "supervisor": "agent-supervisor",
    "supervisor-standby": "agent-supervisor-standby",
    "relay": "agent-relay",
    "docker": "agent-docker",
}
NETWORK_CONTAINER = "agent-docker"  # the conversations' containers share its network

_LOCK_POLL_S = 5
_IDLE_POLL_S = 30
# A probe that raises for an entire idle wait only counts as "core is down" (proceed) rather than "still busy"
# (pending) once the wait has run at least this long -- one flaky reading under a short `--wait-idle-s` (the
# hourly watch calls with 0) must never be read as "core is down for good."
_CORE_DOWN_MIN_WAIT_S = 300


def _services_for_one(path: str) -> frozenset[str]:
    if path == _SUPERVISOR_PATH:
        return _SUPERVISOR_SERVICES
    if path.startswith(_AGENT_IMAGE_PREFIXES):
        return _AGENT_IMAGE_SERVICES
    if path == _COMPOSE_PATH:
        return frozenset({"*"})
    if path.startswith(_NOTHING_PREFIXES) or path in _NOTHING_EXTRA_PATHS:
        return frozenset()
    if path == _DOCKERFILE_PATH:
        return _DOCKERFILE_SERVICES
    if path.startswith("src/agent/") or path == _PYPROJECT_PATH:
        return _SHARED_SERVICES
    return frozenset({"control"})


def services_for(paths: Iterable[str]) -> set[str]:
    """Which Compose services a deploy needs to rebuild for `paths` (a `--no-renames` `git diff --name-only`),
    unioned across all of them. See `_services_for_one` for the mapping; `"*"` (a `compose.yaml`
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


def _drift_reason(host: Host, target: str) -> str | None:
    """`None` if the checkout is clean and `HEAD` is an ancestor of `target`; otherwise a short reason."""
    status = _git(host, ["status", "--porcelain"])
    if status.stdout.strip():
        return "the checkout has uncommitted changes"
    ancestor = _git(host, ["merge-base", "--is-ancestor", "HEAD", target])
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


def _non_ok_shas_since_last_ok(state: State) -> set[str]:
    """`from`/`to` shas of every `"pending"` or `"check_failed: ..."` deploy record logged after the most
    recent `"ok"` one -- or, if there has never been one, of every such record in the whole log. These mark
    git states an earlier, not-fully-successful attempt already moved through, so a later run still needs to
    diff against them even when `HEAD` already sits at `target` and a last-ok/HEAD-only diff would show
    nothing left to do."""
    path = state.root / "deploys.jsonl"
    if not path.exists():
        return set()
    records: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)

    last_ok_index = -1
    for i, record in enumerate(records):
        if record.get("result") == "ok":
            last_ok_index = i

    shas: set[str] = set()
    for record in records[last_ok_index + 1:]:
        result = record.get("result")
        if result == "pending" or (isinstance(result, str) and result.startswith("check_failed")):
            for key in ("from", "to"):
                value = record.get(key)
                if isinstance(value, str):
                    shas.add(value)
    return shas


def _change_bases(host: Host, state: State, target: str, head: str) -> set[str]:
    """Every candidate base (see the module docstring) that still exists and is an ancestor of `target`."""
    candidates = {head, *_non_ok_shas_since_last_ok(state)}
    last_ok = _last_ok_sha(state)
    if last_ok:
        candidates.add(last_ok)

    valid: set[str] = set()
    for sha in candidates:
        if not sha:
            continue
        ancestor = _git(host, ["merge-base", "--is-ancestor", sha, target])
        if ancestor.returncode == 0:
            valid.add(sha)
    return valid


def _changed_paths(host: Host, state: State, target: str, head: str) -> set[str]:
    paths: set[str] = set()
    for base in _change_bases(host, state, target, head):
        diff = _git(host, ["diff", "--name-only", "--no-renames", base, target])
        paths.update(line for line in diff.stdout.splitlines() if line)
    return paths


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
    *unless every single poll in this call raised, and the wait ran for at least `_CORE_DOWN_MIN_WAIT_S`* --
    meaning core was genuinely unreachable, not just one flaky reading under a short wait: nothing could have
    been dispatched in that case, so this returns `True` (proceed) rather than `False` (pending)."""
    start = clock()
    deadline = start + wait_idle_s
    saw_a_real_reading = False
    while True:
        claimed = _claimed(host)
        if claimed is not None:
            saw_a_real_reading = True
            if claimed == 0:
                return True
        if clock() >= deadline:
            if saw_a_real_reading:
                return False
            return (clock() - start) >= _CORE_DOWN_MIN_WAIT_S
        sleep(_IDLE_POLL_S)


# -- the graceful swap: pause core's queue, swap, resume --

_PAUSE_PY = ("from agent import db; c = db.connect('/data/db/agent.db'); was = db.get_setting(c, 'paused', '0'); "
             "print(was); was == '1' or db.set_setting(c, 'paused', '1'); c.commit()")
_RESUME_TRIES = 12  # 10 s apart: core may still be starting after its own swap


_IS_PAUSED_PY = ("from agent import db; c = db.connect('/data/db/agent.db'); "
                 "print(db.get_setting(c, 'paused', '0'))")


def _is_paused(host: Host) -> bool:
    """Whether core is paused right now (unreadable counts as not paused)."""
    try:
        result = host.run(["docker", "exec", CORE_CONTAINER, "python", "-c", _IS_PAUSED_PY],
                          capture_output=True, text=True)
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def _pause(host: Host) -> bool:
    """Stop core from starting turns. True when this call paused it, False when it was already paused (someone
    did that on purpose, so the deploy leaves it paused) or core couldn't be reached (nothing can start then)."""
    try:
        result = host.run(["docker", "exec", CORE_CONTAINER, "python", "-c", _PAUSE_PY],
                          capture_output=True, text=True)
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() != "1"


def _resume(host: Host, sleep, tries: int = _RESUME_TRIES) -> bool:
    """Let the control plane start turns again, retrying while it comes back up. If it never answers, says so on
    stderr and returns False: the paused marker stays, and the host listener resumes it once it answers."""
    for attempt in range(tries):
        try:
            result = host.run(["docker", "exec", CORE_CONTAINER, "agentd", "resume"], capture_output=True, text=True)
            if result.returncode == 0:
                return True
        except Exception:
            pass
        if attempt < tries - 1:
            sleep(10)
    print("deploy: couldn't resume the control plane after the swap; the host listener keeps trying", file=sys.stderr)
    return False


# A deploy that paused core writes this file and removes it once core resumes. A deploy that died in between (killed,
# or the host lost power) leaves it behind with the lock free: `resume_if_left_paused` (the host listener calls it
# every loop) resumes core then, so Jensen is never left paused by a deploy.
_PAUSED_MARKER = "deploy-paused"


def resume_if_left_paused(host: Host, state: State) -> bool:
    """Resume core when a deploy paused it and is no longer running (its marker is there, the deploy lock is free).
    True when it resumed core."""
    marker = state.root / _PAUSED_MARKER
    if not marker.exists():
        return False
    with open(state.root / "deploy.lock", "a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # a deploy is running: its own `finally` resumes core
        try:
            if not _resume(host, lambda s: None, tries=1):
                return False
            marker.unlink(missing_ok=True)
            return True
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _stop_cleanly(signum, frame):
    """SIGTERM or SIGHUP mid-deploy: unwind through the `finally` blocks, so a paused core is resumed."""
    raise SystemExit(128 + signum)


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
    result = host.run(["docker", "exec", CORE_CONTAINER, "python", "-c", code], capture_output=True, text=True)
    return result.returncode == 0


def _container_id(host: Host, container: str) -> str | None:
    result = host.run(["docker", "inspect", container, "--format", "{{.Id}}"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _rejoin_network(host: Host, before: str | None) -> None:
    """A recreated network container leaves the conversations' containers on its old, gone network: restart them so
    they join the new one (a turn running in one carries on from its saved session)."""
    if before is None or _container_id(host, NETWORK_CONTAINER) == before:
        return
    listed = host.run(["docker", "ps", "-q", "--filter", "label=agent.conversation"], capture_output=True, text=True)
    ids = (listed.stdout or "").split()
    if ids:
        host.run(["docker", "restart", *ids], capture_output=True, text=True)


def check(host: Host, services: set[str], *, stable_s: int = 120, sleep=time.sleep,
          smoke: bool = True) -> tuple[bool, str]:
    """The live probe after a rebuild: `(ok, reason)`, `reason` naming the first thing that failed (empty on
    success).

    - Every rebuilt container is `Running`, and its `RestartCount` doesn't move across a `stable_s` wait -- a
      container that is crash-looping can still show `Running` on one snapshot.
    - the control plane's `/health` answers 200, read from inside `agent-control` itself (no port is published to
      the host for it).
    - `docker compose exec -T control agentd smoke` prints `smoke ok` -- a real turn end to end.

    `"*"` in `services` (a `compose.yaml` change) checks every container named in `CONTAINER_NAMES`, since the
    stack definition itself changed and no single service name can be trusted to cover what else did.

    The smoke probe runs through `host.run` directly rather than `Host.compose` -- `compose()` never passes
    `capture_output` (it is meant for a build a person watches stream by), so reading its `stdout` here would
    always see `None`; building the identical `docker compose exec -T control ...` argv through `host.run` instead
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

    if not _health_probe(host, "/health"):
        return False, "/health did not return 200"

    if smoke:
        result = host.run(["docker", "compose", "exec", "-T", "control", "agentd", "smoke"],
                          cwd=host.live, capture_output=True, text=True)
        if "smoke ok" not in (result.stdout or ""):
            return False, "smoke test did not print smoke ok"

    return True, ""


# -- deploy --


def _iso_now() -> str:
    return datetime.now(UTC).strftime(FMT)


def _log_not_deployable(host: Host, state: State, reason: str, target: str | None) -> int:
    print(f"deploy: not deployable: {reason}")
    state.log_deploy({"ts": _iso_now(), "from": _rev_parse_or_none(host, "HEAD"), "to": target,
                       "services": [], "result": f"not_deployable: {reason}"})
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


def deploy(host: Host, state: State, *, wait_idle_s: int = 120, if_pending: bool = False,
           sleep=time.sleep, clock=time.monotonic) -> int:
    """Fetch, decide what to rebuild, wait for an idle runner if needed, fast-forward, rebuild and check.

    Exit codes: 0 deployed and checked (or nothing outstanding); 1 the post-deploy check failed; 2 the checkout
    isn't deployable (dirty, diverged, a failed fetch or a failed fast-forward); 3 pending (the
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
        return _log_not_deployable(host, state, "fetch failed", None)

    # Pin the target once, right after the fetch: everything below reads and acts on this sha, never the
    # symbolic `origin/main` again, so a concurrent fetch from another process can't move the goalposts.
    target = _rev_parse_or_none(host, "origin/main")
    if target is None:
        return _log_not_deployable(host, state, "origin/main could not be resolved after fetch", None)

    reason = _drift_reason(host, target)
    if reason:
        return _log_not_deployable(host, state, reason, target)

    head = _rev_parse(host, "HEAD")
    services = services_for(_changed_paths(host, state, target, head))

    if not services:
        if head == target:
            state.set_deploy_pending(False)
            return 0
        # Nothing needs rebuilding, but there's still something to fast-forward onto (a docs-only change, or
        # the union of outstanding bases happened to net out to nothing) -- land it and log `ok` regardless.
        merge = _git(host, ["merge", "--ff-only", target])
        if merge.returncode != 0:
            failure = (merge.stderr or merge.stdout).strip()
            return _log_not_deployable(host, state, f"merge --ff-only failed: {failure}", target)
        state.set_deploy_pending(False)
        state.log_deploy({"ts": _iso_now(), "from": head, "to": target, "services": [], "result": "ok"})
        return 0

    # Past this point the run is committed to landing everything up to `target` -- clear pending here, not at
    # the end, so a pending flag never outlives the attempt it was flagging.
    state.set_deploy_pending(False)

    # Re-read HEAD fresh rather than reusing the pre-wait `head`: the drift check just confirmed it's still an
    # ancestor of `target`, but only a fresh read is guaranteed to be the exact commit this merge starts from.
    old_sha = _rev_parse(host, "HEAD")
    merge = _git(host, ["merge", "--ff-only", target])
    if merge.returncode != 0:
        failure = (merge.stderr or merge.stdout).strip()
        return _log_not_deployable(host, state, f"merge --ff-only failed: {failure}", target)
    new_sha = target

    build_args = ["build"] + ([] if "*" in services else sorted(services))
    build_result = host.compose(build_args)
    if build_result.returncode != 0:
        reason = f"compose build exited {build_result.returncode}"
        return _log_check_failed(state, old_sha, new_sha, services, reason)

    # Turns run in the conversations' own containers, so recreating agent-control or agent-services interrupts
    # none of them. Only a change to the stack's definition can recreate the network container those containers
    # share, which cuts their network: then the swap is graceful -- the control plane stops starting turns, a running
    # one gets up to `wait_idle_s` to finish, the stack is swapped, the conversations' containers rejoin the new
    # network, and the control plane resumes. Jensen is never left paused by a deploy, and a pause someone set on
    # purpose is left as it was.
    graceful = "*" in services
    paused_here = _pause(host) if graceful else False
    marker = state.root / _PAUSED_MARKER
    if paused_here:
        marker.write_text(f"{os.getpid()}\n")
    try:
        network_before = _container_id(host, NETWORK_CONTAINER) if graceful else None
        if graceful:
            _wait_for_idle(host, wait_idle_s, sleep, clock)

        up_args = (["up", "-d", "--no-build", "--remove-orphans"] if "*" in services
                  else ["up", "-d", "--no-build", *sorted(services)])
        up_result = host.compose(up_args)
        if up_result.returncode != 0:
            return _log_check_failed(state, old_sha, new_sha, services, f"compose up exited {up_result.returncode}")
        _rejoin_network(host, network_before)

        # Resume before the check: its smoke test is a real turn through the queue, which a paused control plane never
        # starts.
        if paused_here and _resume(host, sleep):
            marker.unlink(missing_ok=True)
            paused_here = False

        # A control plane someone paused on purpose starts no turn, so the smoke turn is left out; the other probes
        # still run.
        ok, reason = check(host, services, sleep=sleep, smoke=not _is_paused(host))
        result = "ok" if ok else f"check_failed: {reason}"
        state.log_deploy({"ts": _iso_now(), "from": old_sha, "to": new_sha, "services": sorted(services),
                           "result": result})
        return 0 if ok else 1
    finally:
        if paused_here and _resume(host, sleep):
            marker.unlink(missing_ok=True)


# -- CLI --


def _dry_run(host: Host, state: State) -> int:
    fetch = _git(host, ["fetch", "origin", "main"])
    if fetch.returncode != 0:
        print("fetch failed")
        return 0
    target = _rev_parse_or_none(host, "origin/main")
    if target is None:
        print("origin/main could not be resolved after fetch")
        return 0
    head = _rev_parse(host, "HEAD")
    paths = _changed_paths(host, state, target, head)
    services = services_for(paths)
    print(f"services: {sorted(services)}")
    print("paths:")
    for path in sorted(paths):
        print(f"  {path}")
    return 0


def main(argv: list[str] | None = None, *, host: Host | None = None, state: State | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--wait-idle-s", type=int, default=120)
    parser.add_argument("--if-pending", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    host = Host.default() if host is None else host
    state = State.default() if state is None else state
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _stop_cleanly)

    if args.dry_run:
        return _dry_run(host, state)

    return deploy(host, state, wait_idle_s=args.wait_idle_s, if_pending=args.if_pending)


if __name__ == "__main__":
    sys.exit(main())
