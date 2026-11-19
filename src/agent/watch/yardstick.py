"""The yardstick: a set of real tasks improve-jensen runs Jensen through, to tell whether Jensen as a whole got better
or worse after a change.

A task is a folder `<id>/` under `$STATE/yardstick/tasks/` (a person retires one by moving it to
`$STATE/yardstick/retired/`):
- `task.toml`: `prompt`, `time_limit_min` (default 30) and an optional `rubric`;
- `fixture/`: what Jensen works on, copied into its own folder in `agent-runner`. A `build.sh` there is run in that
  folder before the turn is queued; it makes the git repo and its first commit, then deletes itself;
- `check`: run in that folder after the turn ends; exit 0 is a pass;
- `hidden/`: optional; copied in, with `check`, only once the turn has ended, and both removed again right after
  the check, so Jensen never sees either -- not even from a later turn, or from the retry next door.

An id may hold only plain characters, and never ends in `-r`: that's the name of a retry's folder.

A task is mechanical when it has a `check` and no rubric. Only mechanical tasks count toward the score or the gate,
so improve-jensen is never the judge of its own change; the others are recorded as `judged`.

`run` first removes run folders under `/var/agent/yardstick/` older than 7 days, then copies each task into
`/var/agent/yardstick/<run>/<id>/` (made with a plain `mkdir` in the same command as the copy, so a folder that is
already there is refused, never reused), queues its prompt as a turn in a fresh `V-yardstick-<run>-` conversation
(`core_ask.py`), keeps at most `concurrency` of those turns in flight, polls them (`core_items.py`) every
`POLL_SECONDS` until each finishes or runs out its time limit, then runs its check. The time limit starts when the
turn first leaves `queued` (a turn waiting behind people's work isn't slow), with an overall cap of three times the
limit from when it was queued (a turn that never starts can't hold the run). A failure of a mechanical task is
tried once more in a fresh folder, `<id>-r`. A turn that never finishes is recorded as a timeout and left running,
never cancelled; the run moves on regardless, so a hung Jensen or a runner that is down can't hang the job: every
`docker exec` here has a timeout, and `build.sh` and `check` also run under `timeout` inside the runner. Each task's
result is appended to `$STATE/yardstick/results.jsonl` as soon as it is final, its `ts` in `state.FMT`. When the run
ends, the memory notes it left behind are removed (`runner_memory.py`).

Nothing here prints a task id, a note's name, a host path or anything a container wrote to stderr: task ids written
by the assessment come from real products, and this output ends up in a public workflow log.
"""

from __future__ import annotations

import base64
import io
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent.watch.state import FMT, State

RUNNER_BASE = "/var/agent/yardstick"
MEMORY_ROOT = "/var/agent/memory"
MARKER = "/var/agent/yardstick/"
CONVERSATION_PREFIX = "V-yardstick-"
RUNNER = "agent-runner"

POLL_SECONDS = 30
DEFAULT_TIME_LIMIT_MIN = 30
# A turn that never leaves `queued` times out at this many times its own limit, counted from when it was queued.
QUEUED_CAP_FACTOR = 3
# `check` and `build.sh` run under `timeout -k 10 <limit>` inside the runner; the host's own wait on that
# `docker exec` is a backstop a minute past it, for a runner that stops answering altogether.
CHECK_TIMEOUT_S = 600
BUILD_TIMEOUT_S = 300
_BACKSTOP_S = 60
_COPY_TIMEOUT_S = 120
_EXEC_TIMEOUT_S = 120  # each `core_python`/`runner_python` call: queue, poll, memory
PRUNE_DAYS = 7
# A run is compared with an earlier one only when they share at least this many mechanical tasks.
MIN_SHARED = 2
_FINISHED = ("done", "failed", "cancelled")
_QUEUED = "queued"
# Exit codes of `timeout` when it stopped the command: 124 after SIGTERM, 137 when it had to SIGKILL it too.
_TIMED_OUT = (124, 137)
# The exit code the copy uses when the task's folder is already there (its plain `mkdir` failed).
_FOLDER_EXISTS = 17

HEADER = ("This is a yardstick task in `{dir}`. Work only in that folder. It has no remote: don't push, don't open "
          "PRs, don't message anyone, and don't save memory notes or skills about it. Reply 'done' when finished.")

# A task id becomes a folder name inside agent-runner and part of a shell command: plain characters only.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RETRY_SUFFIX = "-r"


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    time_limit_min: int
    rubric: str
    path: Path
    mechanical: bool


def tasks_root(state: State) -> Path:
    return state.root / "yardstick" / "tasks"


def results_path(state: State) -> Path:
    return state.yardstick_results_path()


def _retired_root(root: Path) -> Path:
    return Path(root).parent / "retired"


def _warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


# -- tasks --


def _read_task(folder: Path) -> Task:
    """The task in `folder`; raises `ValueError` (with a message naming no path) when it isn't a valid one."""
    if not _ID.fullmatch(folder.name):
        raise ValueError("a task id may hold only letters, digits, '.', '_' and '-'")
    if folder.name.endswith(_RETRY_SUFFIX):
        raise ValueError(f"a task id may not end in '{_RETRY_SUFFIX}': that's the name of a retry's folder")
    try:
        data = tomllib.loads((folder / "task.toml").read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"task.toml can't be read ({type(exc).__name__})") from None
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task.toml needs a non-empty prompt")
    limit = data.get("time_limit_min", DEFAULT_TIME_LIMIT_MIN)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("time_limit_min must be a whole number of minutes, 1 or more")
    rubric = data.get("rubric", "")
    if not isinstance(rubric, str):
        raise ValueError("rubric must be text")
    check = (folder / "check").is_file()
    return Task(id=folder.name, prompt=prompt, time_limit_min=limit, rubric=rubric, path=folder,
                mechanical=check and not rubric.strip())


def _task_folders(root: Path) -> list[Path]:
    """Folders under `root` holding a `task.toml`, in id order. A dot folder is an install still in progress."""
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir())
            if p.is_dir() and not p.name.startswith(".") and (p / "task.toml").is_file()]


def load_tasks(root: Path) -> list[Task]:
    """Every task under `root`, in id order. One that can't be read is skipped with a warning that counts them but
    never names them."""
    tasks, skipped = [], 0
    for folder in _task_folders(Path(root)):
        try:
            tasks.append(_read_task(folder))
        except ValueError:
            skipped += 1
    if skipped:
        _warn(f"skipped {skipped} yardstick task(s) that could not be read")
    return tasks


def _taken(task_id: str, root: Path) -> bool:
    return (root / task_id).exists() or (_retired_root(root) / task_id).exists()


def _copy_in(src: Path, root: Path) -> None:
    """Copy the task folder `src` to `root/<id>` through a dot folder renamed into place, so `load_tasks` never sees
    half a task."""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{src.name}.{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(src, tmp, symlinks=True)
        tmp.rename(root / src.name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def install(src: Path, root: Path) -> list[str]:
    """Copy each task folder in `src` that `root` doesn't have yet (a retired id counts as had), never touching one
    that's there. Returns the ids it added. A folder in `src` that isn't a valid task is skipped."""
    added, skipped = [], 0
    for folder in _task_folders(Path(src)):
        if _taken(folder.name, Path(root)):
            continue
        try:
            _read_task(folder)
        except ValueError:
            skipped += 1
            continue
        _copy_in(folder, Path(root))
        added.append(folder.name)
    if skipped:
        _warn(f"skipped {skipped} yardstick task(s) that could not be read")
    return added


def add(src_task: Path, root: Path) -> str:
    """Add the one task folder `src_task`. Raises `ValueError` when it isn't a valid task and `FileExistsError` when
    its id is already taken, by a task or a retired one: a task is never overwritten."""
    src_task, root = Path(src_task), Path(root)
    if not (src_task / "task.toml").is_file():
        raise ValueError("not a yardstick task: there is no task.toml")
    _read_task(src_task)
    if _taken(src_task.name, root):
        raise FileExistsError("a yardstick task with that id already exists")
    _copy_in(src_task, root)
    return src_task.name


# -- results --


def read_results(state: State) -> list[dict]:
    path = results_path(state)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("run") and record.get("task"):
            out.append(record)
    return out


def _append_result(state: State, record: dict) -> None:
    path = results_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def score(results: list[dict]) -> tuple[int, int]:
    """`(passed, counted)` over the mechanical tasks only; judged ones never count."""
    counted = [r for r in results if not r.get("judged")]
    return sum(1 for r in counted if r.get("passed") is True), len(counted)


def _runs(state: State) -> tuple[list[str], dict[str, dict[str, bool]]]:
    """Run ids in the order they first appear in the results file, and each run's mechanical results by task."""
    order: list[str] = []
    by_run: dict[str, dict[str, bool]] = {}
    for record in read_results(state):
        run_id = str(record["run"])
        if run_id not in by_run:
            order.append(run_id)
            by_run[run_id] = {}
        if not record.get("judged"):
            by_run[run_id][str(record["task"])] = record.get("passed") is True
    return order, by_run


def compare(state: State, current_run_id: str) -> dict:
    """The current run against the most recent earlier run that shares at least `MIN_SHARED` mechanical tasks with
    it (`previous`, `None` when no earlier run does; `shared`, how many). `drop` is how many fewer of those shared
    tasks passed now; a task added since can't move it. A regression is a mechanical task that passed each of its
    last 3 earlier runs and fails now (after its retry). The gate fails on a drop of 2 or more, or on any
    regression."""
    order, by_run = _runs(state)
    out = {"previous": None, "shared": 0, "drop": 0, "regressions": [], "gate_failed": False}
    current = by_run.get(current_run_id)
    if not current:
        return out
    earlier = order[:order.index(current_run_id)]
    previous = next((run_id for run_id in reversed(earlier)
                     if len(set(by_run[run_id]) & set(current)) >= MIN_SHARED), None)
    if previous is not None:
        shared = set(by_run[previous]) & set(current)
        out["previous"] = previous
        out["shared"] = len(shared)
        out["drop"] = sum(by_run[previous][t] for t in shared) - sum(current[t] for t in shared)
    for task_id in sorted(current):
        if current[task_id]:
            continue
        history = [by_run[run_id][task_id] for run_id in earlier if task_id in by_run[run_id]]
        if len(history) >= 3 and all(history[-3:]):
            out["regressions"].append(task_id)
    out["gate_failed"] = out["drop"] >= 2 or bool(out["regressions"])
    return out


def gate_verdict(state: State, run_id: str) -> str:
    """What the run `run_id` says about the change a gate is measuring: `passed` or `failed` from `compare`;
    `passed` too for the first run there has ever been, which becomes the baseline; `retry` when it can't say --
    the run recorded no results, or it shares fewer than `MIN_SHARED` mechanical tasks with every earlier run."""
    order, _ = _runs(state)
    if run_id not in order:
        return "retry"
    if order.index(run_id) == 0:
        return "passed"
    comparison = compare(state, run_id)
    if comparison["previous"] is None:
        return "retry"
    return "failed" if comparison["gate_failed"] else "passed"


# -- run --


@dataclass
class _Attempt:
    task: Task
    tries: int
    folder: str
    item: str = ""
    started: float = 0.0  # when the turn was queued
    running: float | None = None  # when a poll first saw it out of `queued`

    def timed_out(self, now: float) -> bool:
        limit = self.task.time_limit_min * 60
        if self.running is not None and now - self.running >= limit:
            return True
        return now - self.started >= QUEUED_CAP_FACTOR * limit


def _normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No host user or group names travel into the runner (they can be a person's name)."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tar(entries: list[tuple[Path, str]], executable: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, arcname in entries:
            if arcname in executable:
                info = _normalized(tar.gettarinfo(str(path), arcname))
                info.mode = 0o755
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.add(str(path), arcname=arcname, filter=_normalized)
    return buf.getvalue()


def _docker_sh(host, script: str, *, timeout: int, data: bytes | None = None) -> int:
    """`sh -c script` inside agent-runner (with `data` on stdin when given); the exit code, or -1 when docker
    itself can't be run or the command outlives `timeout`. Its output is never read or printed."""
    args = ["docker", "exec", "-i", RUNNER, "sh", "-c", script] if data is not None \
        else ["docker", "exec", RUNNER, "sh", "-c", script]
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if data is not None:
        kwargs["input"] = data
    try:
        return host.run(args, **kwargs).returncode
    except Exception:
        return -1


def _prune(host) -> None:
    """Remove run folders under `RUNNER_BASE` older than `PRUNE_DAYS`, in one `find`. A failure only warns: an old
    folder left behind is no reason to skip a run."""
    try:
        host.run(["docker", "exec", RUNNER, "find", RUNNER_BASE, "-mindepth", "1", "-maxdepth", "1",
                  "-mtime", f"+{PRUNE_DAYS}", "-exec", "rm", "-rf", "{}", "+"],
                 capture_output=True, timeout=_COPY_TIMEOUT_S)
    except Exception as exc:
        _warn(f"pruning old yardstick folders failed ({type(exc).__name__})")


def _prepare(host, task: Task, folder: str) -> str:
    """Copy the fixture into `folder` and build it; `""` when done, else the reason it failed. The folder is made
    with a plain `mkdir` in the same command as the copy: one that is already there fails it ("folder exists"),
    with no gap between a check and the copy for another run to slip into."""
    fixture, q = task.path / "fixture", shlex.quote(folder)
    base = shlex.quote(posixpath.dirname(folder))
    try:
        data = _tar([(child, child.name) for child in sorted(fixture.iterdir())] if fixture.is_dir() else [])
    except OSError:
        return "copy failed"
    script = f"mkdir -p {base} && {{ mkdir {q} || exit {_FOLDER_EXISTS}; }} && tar -x -C {q}"
    rc = _docker_sh(host, script, timeout=_COPY_TIMEOUT_S, data=data)
    if rc == _FOLDER_EXISTS:
        return "folder exists"
    if rc != 0:
        return "copy failed"
    if (fixture / "build.sh").is_file() and _docker_sh(
            host, f"cd {q} && timeout -k 10 {BUILD_TIMEOUT_S} sh build.sh", timeout=BUILD_TIMEOUT_S + _BACKSTOP_S):
        return "build failed"
    return ""


def _queue(host, run_id: str, task: Task, folder: str) -> tuple[str, str]:
    """Queue the task's turn: `(work item id, "")`, or `("", reason)` when it couldn't be queued -- "queue error"
    when the exec timed out (the turn may or may not be in core), "queue failed" when it failed outright."""
    text = HEADER.format(dir=folder) + "\n\n" + task.prompt
    args = ["--new", "--new-prefix", f"{CONVERSATION_PREFIX}{run_id}-",
            "--text-b64", base64.b64encode(text.encode()).decode()]
    try:
        item = host.core_python("core_ask.py", args, timeout=_EXEC_TIMEOUT_S).strip()
    except subprocess.TimeoutExpired:
        return "", "queue error"
    except Exception:
        return "", "queue failed"
    return (item, "") if item else ("", "queue failed")


def _poll(host, ids: list[str]) -> dict | None:
    """Each id's status from core, or `None` when it couldn't be read (a timed-out exec included): no news, and
    each turn's own time limit still runs."""
    try:
        statuses = json.loads(host.core_python("core_items.py", ["--ids", ",".join(ids)], timeout=_EXEC_TIMEOUT_S))
    except Exception:
        return None
    return statuses if isinstance(statuses, dict) else None


def _check(host, attempt: _Attempt, status: str) -> tuple[bool, str]:
    """Copy `hidden/` and `check` in (replacing anything by those names the turn left there), run the check, then
    remove both again, whatever the check's outcome."""
    task, q = attempt.task, shlex.quote(attempt.folder)
    turn_failed = f"turn {status}" if status != "done" else ""
    if not (task.path / "check").is_file():  # a judged task with nothing to run: it passes when the turn finished
        return not turn_failed, turn_failed
    entries = [(task.path / "hidden", "hidden")] if (task.path / "hidden").is_dir() else []
    entries.append((task.path / "check", "check"))
    try:
        data = _tar(entries, executable=("check",))
    except OSError:
        return False, "copy failed"
    try:
        script = f"rm -rf {q}/hidden {q}/check && mkdir -p {q} && tar -x -C {q}"
        if _docker_sh(host, script, timeout=_COPY_TIMEOUT_S, data=data) != 0:
            return False, "copy failed"
        return _run_check(host, q, turn_failed)
    finally:
        _docker_sh(host, f"rm -rf {q}/hidden {q}/check", timeout=_COPY_TIMEOUT_S)


def _run_check(host, q: str, turn_failed: str) -> tuple[bool, str]:
    try:
        result = host.run(["docker", "exec", RUNNER, "sh", "-c", f"cd {q} && timeout -k 10 {CHECK_TIMEOUT_S} ./check"],
                          capture_output=True, timeout=CHECK_TIMEOUT_S + _BACKSTOP_S)
    except subprocess.TimeoutExpired:
        return False, "check timeout"
    except Exception:
        return False, "check error"
    if result.returncode == 0:
        return True, ""
    if result.returncode in _TIMED_OUT:
        return False, "check timeout"
    return False, turn_failed or "check failed"


def _sha(host) -> str:
    try:
        result = host.run(["git", "-C", str(host.live), "rev-parse", "HEAD"], capture_output=True, text=True)
    except Exception:
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _memory(host, args: list[str]) -> dict | None:
    try:
        report = json.loads(host.runner_python("runner_memory.py", args, timeout=_EXEC_TIMEOUT_S))
    except Exception:
        return None
    return report if isinstance(report, dict) else None


def run(host, state: State, tasks: list[Task], *, run_id: str, concurrency: int = 4, sleep=time.sleep,
        clock=time.monotonic) -> list[dict]:
    """Run `tasks` (see the module docstring); one result per task, in `tasks`' order."""
    if not tasks:
        return []
    sha = _sha(host)
    since = int(time.time())
    base = f"{RUNNER_BASE}/{run_id}"
    _prune(host)
    before = f"{base}/.memory-before.json"
    if _memory(host, ["--root", MEMORY_ROOT, "--snapshot", before]) is None:
        _warn("could not list Jensen's memory notes before the run; no note will be removed after it")

    final: dict[str, dict] = {}
    pending: deque[tuple[Task, int]] = deque((task, 1) for task in tasks)
    in_flight: list[_Attempt] = []
    failed_polls = 0

    def finish(attempt: _Attempt, passed: bool, reason: str) -> None:
        task = attempt.task
        if not passed and task.mechanical and attempt.tries == 1 and reason != "folder exists":
            pending.append((task, 2))
            return
        minutes = round((clock() - attempt.started) / 60, 1) if attempt.item else 0.0
        record = {"run": run_id, "ts": datetime.now(UTC).strftime(FMT), "sha": sha, "task": task.id,
                  "passed": passed, "minutes": minutes, "judged": not task.mechanical, "reason": reason,
                  "tries": attempt.tries}
        final[task.id] = record
        _append_result(state, record)

    try:
        while pending or in_flight:
            while pending and len(in_flight) < concurrency:
                task, tries = pending.popleft()
                attempt = _Attempt(task, tries, f"{base}/{task.id}" + (_RETRY_SUFFIX if tries > 1 else ""))
                reason = _prepare(host, task, attempt.folder)
                if not reason:
                    attempt.item, reason = _queue(host, run_id, task, attempt.folder)
                if reason:
                    finish(attempt, False, reason)
                    continue
                attempt.started = clock()
                in_flight.append(attempt)
            if not in_flight:
                continue
            sleep(POLL_SECONDS)
            statuses = _poll(host, [attempt.item for attempt in in_flight])
            if statuses is None:
                failed_polls += 1
                statuses = {}
            for attempt in list(in_flight):
                entry = statuses.get(attempt.item)
                status = entry.get("status") if isinstance(entry, dict) else None
                if status is not None and status != _QUEUED and attempt.running is None:
                    attempt.running = clock()
                if status in _FINISHED:
                    in_flight.remove(attempt)
                    finish(attempt, *_check(host, attempt, status))
                elif attempt.timed_out(clock()):
                    in_flight.remove(attempt)  # left running in Jensen: never cancelled
                    finish(attempt, False, "timeout")
    finally:
        report = _memory(host, ["--root", MEMORY_ROOT, "--since", str(since), "--marker", MARKER,
                                "--existing-file", before])
        if report is None:
            _warn("the memory cleanup after the yardstick run failed")
        else:
            removed, kept = len(report.get("removed") or []), len(report.get("kept") or [])
            print(f"memory cleanup: removed {removed} note(s), kept {kept} that existed before the run",
                  file=sys.stderr)
        if failed_polls:
            _warn(f"{failed_polls} status read(s) of the yardstick turns failed")
    return [final[task.id] for task in tasks if task.id in final]


def new_run_id(now: datetime) -> str:
    return f"{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:4]}"
