"""The watch CLI: `python scripts/jensen_watch.py <subcommand>`, the thin command-line surface the GitHub Actions
workflow steps and improve-jensen itself call. Each subcommand just wires `state.py`/`measure.py`/`host.py`
together; none of them measure or decide anything on their own.

`gh ARGS...` is handled before argparse ever sees it (not with `nargs=argparse.REMAINDER` on a subparser, which
mishandles a leading flag such as `--json` right after the subcommand name): everything after `gh` goes straight
to `host.jensen_gh`, untouched.

`measure` and `plan` write their outputs as `key=value` lines, GitHub Actions' own `$GITHUB_OUTPUT` format, via
`--github-output FILE` (appended, never truncated); without that flag the same lines print to stdout instead, for
a person running this by hand.

`plan` never puts a PR URL into its own output: it validates `plan.json` (see `state.plan()`, which also drops a PR
an earlier run already reviewed) and writes the review list to `<state>/runs/<run id>-reviews.json`, emitting only
`review_indices` (`[0, 1, ...]`) -- a scheduled review job's prompt can then name an index instead of a URL, and
`review-target --run <run id> --index <i>` (called by Claude itself, inside that job, not by a workflow step whose
output a public Actions log would show) looks the URL and conversation back up.

`seed` and the open-`fix-deployed`-issues lookup inside `measure` always go through the plain `gh` the job itself
is logged in as (`host.run`), never through `host.jensen_gh` -- that call is reading the record of Jensen's work
from the outside, not acting as Jensen.

`gate start`/`gate status`/`gate end` wrap `state.py`'s own gate (held while the yardstick measures one change at a
time). `gate status` prints the active gate, or with `--last` the one that ended last (with its `result`), or
`none`; with `--github-output` it only writes `active=true|false` there, so a workflow log never shows a gate's
handle. `gate end --result passed|failed` archives the gate with its result. `yardstick run --gate` measures the
active gate's change and says what it found in `gate_result`: `none` (no active gate, or no tasks installed:
nothing to measure), `retry` (it couldn't be measured this time -- the pool held or an error -- and the gate stays
active for the next gate run), or `passed`/`failed`, compared only with runs that started before the gate did and
never with a gate run that failed; with no such run to compare with, it passes and becomes the baseline. It
records its verdict, and never ends the gate itself.

`assess-check`/`assess-plan`/`assess-commit` drive the assess job the same way `measure`/`plan`/`commit` drive the
watch; `project-mark` is the deterministic record of which `project` issue, if any, is currently being worked
(`$STATE/project-active.json`), read back by `assess-check`'s own "is anything waiting" check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.watch import feedback, yardstick
from agent.watch.host import Host, pool_is_holding
from agent.watch.measure import measure
from agent.watch.state import FMT, State

# Labels every install needs regardless of what the seed file's own entries use, so a fresh repo always has
# somewhere for the fix job and the record to put these states.
_STATE_LABELS = ("fixing", "fix-deployed", "needs-person", "stuck", "project", "gating", "priority",
                 "jensen-assessment")


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _emit(lines: list[str], github_output: str | None) -> None:
    """Append `lines` to `github_output` in `key=value` form, or print them for a person running this by hand."""
    if github_output:
        with open(github_output, "a") as fh:
            for line in lines:
                fh.write(line + "\n")
    else:
        for line in lines:
            print(line)


# -- yardstick --


def _add_yardstick_parser(sub) -> None:
    yardstick_p = sub.add_parser("yardstick")
    yardstick_sub = yardstick_p.add_subparsers(dest="yardstick_command", required=True)

    install_p = yardstick_sub.add_parser("install")
    install_p.add_argument("dir")

    add_p = yardstick_sub.add_parser("add")
    add_p.add_argument("dir")

    run_p = yardstick_sub.add_parser("run")
    run_p.add_argument("--tasks", help="comma-separated task ids; every installed task when not given")
    run_p.add_argument("--gate", action="store_true")
    run_p.add_argument("--github-output")

    yardstick_sub.add_parser("score")


def _yardstick_install(state: State, folder: str) -> int:
    if not Path(folder).is_dir():
        print("yardstick install: the folder to install from does not exist", file=sys.stderr)
        return 2
    try:
        added = yardstick.install(Path(folder), yardstick.tasks_root(state))
    except OSError as exc:
        print(f"yardstick install failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"installed={len(added)}")
    return 0


def _yardstick_add(state: State, folder: str) -> int:
    """Never overwrites: an id already there (or retired) exits 1. The messages name no task id or path -- an
    assessment-written task's id can come from a real product."""
    try:
        yardstick.add(Path(folder), yardstick.tasks_root(state))
    except FileExistsError as exc:
        print(f"yardstick add: {exc}; tasks are never overwritten", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"yardstick add: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"yardstick add failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("added=1")
    return 0


def _pool_holding(host: Host, now: datetime) -> bool:
    """core's usage-watch pool (`pool_is_holding`, as `measure` reads it). Unreadable counts as not holding, as it
    does there: a change that broke core must fail a gate run, not slip through it unmeasured."""
    holding = pool_is_holding(host, now)
    if holding is None:
        print("warning: reading core's pool failed; running anyway", file=sys.stderr)
    return bool(holding)


def _yardstick_run(host: Host, state: State, now: datetime, tasks_arg: str | None, gate: bool,
                   github_output: str | None) -> int:
    """Outputs `score`, `counted`, `skip_reason`, `run_id`, `gate_result` and `gate_failed` (`true` only when
    `gate_result` is `failed`), then prints `score=<passed>/<counted>`. `gate_result` is always `none` without
    `--gate` (a nightly run never judges a gate); with it, see the module docstring."""

    def emit(passed: int, counted: int, skip_reason: str, run_id: str, gate_result: str) -> None:
        _emit([f"score={passed}", f"counted={counted}", f"skip_reason={skip_reason}", f"run_id={run_id}",
               f"gate_result={gate_result}", f"gate_failed={_bool_str(gate_result == 'failed')}"], github_output)
        print(f"score={passed}/{counted}")

    active = state.gate(now) if gate else None
    if gate and active is None:
        emit(0, 0, "no_gate", "", "none")
        return 0
    cannot_measure = "retry" if gate else "none"

    try:
        tasks = yardstick.load_tasks(yardstick.tasks_root(state))
    except OSError as exc:
        print(f"yardstick run: reading the tasks failed ({type(exc).__name__})", file=sys.stderr)
        emit(0, 0, "error", "", cannot_measure)
        return 1
    if tasks_arg is not None:
        wanted = {task_id for task_id in tasks_arg.split(",") if task_id}
        unknown = wanted - {task.id for task in tasks}
        if unknown or not wanted:
            problem = f"{len(unknown)} of its ids are not installed tasks" if unknown else "it names no task"
            print(f"yardstick run: --tasks {problem}", file=sys.stderr)
            return 2
        tasks = [task for task in tasks if task.id in wanted]

    if not tasks:  # nothing to measure, and a retry wouldn't change that
        emit(0, 0, "no_tasks", "", "none")
        return 0
    if _pool_holding(host, now):
        emit(0, 0, "pool_hold", "", cannot_measure)
        return 0

    run_id = yardstick.new_run_id(now)
    try:
        results = yardstick.run(host, state, tasks, run_id=run_id, gate=active["started"] if active else None)
        gate_result = "none"
        if active:
            # Only runs that started before the gate can be its baseline (`state.gate` has already parsed
            # `started`), and the verdict is recorded: a failed gate run is never anyone's baseline.
            gate_result = yardstick.gate_verdict(state, run_id,
                                                 datetime.strptime(active["started"], FMT).replace(tzinfo=UTC))
            yardstick.record_gate_run(state, run_id, active["started"], gate_result)
    except Exception as exc:
        print(f"yardstick run failed ({type(exc).__name__})", file=sys.stderr)
        emit(0, 0, "error", run_id, cannot_measure)
        return 1
    passed, counted = yardstick.score(results)
    emit(passed, counted, "", run_id, gate_result)
    return 0


def _yardstick_score(state: State) -> int:
    """The latest run's score line, then how it compares -- counts only, never a task id."""
    results = yardstick.read_results(state)
    if not results:
        print("no yardstick runs yet")
        return 0
    latest = results[-1]["run"]
    rows = [r for r in results if r["run"] == latest]
    passed, counted = yardstick.score(rows)
    judged = sum(1 for r in rows if r.get("judged"))
    comparison = yardstick.compare(state, latest)
    print(f"run={latest} sha={rows[-1].get('sha') or 'unknown'} score={passed}/{counted} judged={judged}")
    print(f"previous={comparison['previous'] or 'none'} drop={comparison['drop']} "
          f"regressions={len(comparison['regressions'])} gate_failed={_bool_str(comparison['gate_failed'])}")
    return 0


def _cmd_yardstick(host: Host, state: State, now: datetime, args: argparse.Namespace) -> int:
    if args.yardstick_command == "install":
        return _yardstick_install(state, args.dir)
    if args.yardstick_command == "add":
        return _yardstick_add(state, args.dir)
    if args.yardstick_command == "run":
        return _yardstick_run(host, state, now, args.tasks, args.gate, args.github_output)
    return _yardstick_score(state)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jensen_watch")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_yardstick_parser(sub)

    measure_p = sub.add_parser("measure")
    measure_p.add_argument("--github-output")

    sub.add_parser("commit")

    count_p = sub.add_parser("count")
    count_p.add_argument("signal")
    count_p.add_argument("--since", required=True)

    note_p = sub.add_parser("note")
    note_p.add_argument("issue", type=int)
    note_p.add_argument("ref")
    note_p.add_argument("line")

    plan_p = sub.add_parser("plan")
    plan_p.add_argument("--github-output")
    plan_p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID") or "local")

    review_target_p = sub.add_parser("review-target")
    review_target_p.add_argument("--run", required=True)
    review_target_p.add_argument("--index", type=int, required=True)

    seed_p = sub.add_parser("seed")
    seed_p.add_argument("file")
    seed_p.add_argument("--repo")

    feedback_reply_p = sub.add_parser("feedback-reply")
    feedback_reply_p.add_argument("name")
    feedback_reply_p.add_argument("status")

    changes_add_p = sub.add_parser("changes-add")
    changes_add_p.add_argument("text")

    gate_p = sub.add_parser("gate")
    gate_sub = gate_p.add_subparsers(dest="gate_command", required=True)
    gate_start_p = gate_sub.add_parser("start")
    gate_start_p.add_argument("--issue", type=int, required=True)
    gate_start_p.add_argument("--kind", choices=["repo", "host"], required=True)
    gate_start_p.add_argument("--handle", required=True)
    gate_status_p = gate_sub.add_parser("status")
    gate_status_p.add_argument("--last", action="store_true", help="the gate that ended last, with its result")
    gate_status_p.add_argument("--github-output")
    gate_end_p = gate_sub.add_parser("end")
    gate_end_p.add_argument("--result", choices=["passed", "failed"],
                            help="what the yardstick found; left out when a person ends a gate by hand")

    assess_check_p = sub.add_parser("assess-check")
    assess_check_p.add_argument("--github-output")

    assess_plan_p = sub.add_parser("assess-plan")
    assess_plan_p.add_argument("--github-output")

    sub.add_parser("assess-commit")

    project_mark_p = sub.add_parser("project-mark")
    project_mark_p.add_argument("--issue", type=int, required=True)
    project_mark_p.add_argument("--status", choices=["active", "done"], required=True)

    return parser


# -- measure --


def _fetch_open_deployed(host: Host) -> list[dict]:
    """The open `fix-deployed` issues, with the job's own plain `gh` -- never Jensen's token. An unset
    `GITHUB_REPOSITORY`, a non-zero exit or unparseable JSON all fall back to `[]`, with a warning on stderr;
    one hour's `measure` is never worth losing over this lookup alone. The warning names only the exit code or the
    exception's type: stderr is the public workflow log, and `gh`'s own message can carry a path or a token error."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("warning: GITHUB_REPOSITORY is not set; treating open fix-deployed issues as none", file=sys.stderr)
        return []
    try:
        result = host.run(["gh", "issue", "list", "-R", repo, "--label", "fix-deployed", "--state", "open",
                           "--json", "number,title"], capture_output=True, text=True)
    except OSError as exc:
        print(f"warning: gh issue list failed ({type(exc).__name__})", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"warning: gh issue list failed (exit {result.returncode})", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        print("warning: gh issue list returned unparseable output", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def _cmd_measure(host: Host, state: State, now: datetime, github_output: str | None) -> int:
    open_deployed = _fetch_open_deployed(host)
    result = measure(state, host, now, open_deployed)
    _emit([
        f"has_work={_bool_str(result.has_work)}",
        f"skip_reason={result.skip_reason or ''}",
        f"digest={result.digest or ''}",
    ], github_output)
    return 0


# -- commit --


def _cmd_commit(state: State) -> int:
    state.commit()
    return 0


# -- count --


def _cmd_count(state: State, signal: str, since: str) -> int:
    try:
        since_dt = datetime.strptime(since, FMT).replace(tzinfo=UTC)
    except ValueError:
        print(f"invalid --since value: {since!r}, want {FMT!r}", file=sys.stderr)
        return 2
    try:
        line = state.count(signal, since_dt)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(line)
    return 0


# -- note --


def _cmd_note(state: State, issue: int, ref: str, line: str, now: datetime) -> int:
    print(state.note(issue, ref, line, now))
    return 0


# -- plan --


def _cmd_plan(state: State, github_output: str | None, run_id: str) -> int:
    plan = state.plan(run_id)
    reviews, fix = plan["reviews"], plan["fix"]
    state.write_run_reviews(run_id, reviews)
    _emit([
        f"review_indices={json.dumps(list(range(len(reviews))))}",
        f"fix={fix if fix is not None else ''}",
        f"has_reviews={_bool_str(bool(reviews))}",
        f"has_fix={_bool_str(fix is not None)}",
    ], github_output)
    return 0


# -- review-target --


def _cmd_review_target(state: State, run_id: str, index: int) -> int:
    review = state.read_run_review(run_id, index)
    if review is None:
        print(f"no review at run {run_id!r} index {index}", file=sys.stderr)
        return 2
    conversation = review.get("conversation")
    print(f"url={review['url']}")
    print(f"conversation={conversation if conversation else 'none'}")
    return 0


# -- seed --


def _cmd_seed(host: Host, file: str, repo: str | None) -> int:
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("seed needs --repo or GITHUB_REPOSITORY", file=sys.stderr)
        return 2

    entries = json.loads(Path(file).read_text())

    labels: dict[str, None] = {}
    for entry in entries:
        for label in entry.get("labels", []):
            labels.setdefault(label, None)
    for label in _STATE_LABELS:
        labels.setdefault(label, None)
    for label in labels:
        host.run(["gh", "label", "create", label, "--force", "-R", repo], capture_output=True, text=True)

    for entry in entries:
        title = entry["title"]
        existing = host.run(
            ["gh", "issue", "list", "-R", repo, "--state", "open", "--search", f"{title} in:title",
             "--json", "title"], capture_output=True, text=True)
        try:
            found = json.loads(existing.stdout or "[]")
        except json.JSONDecodeError:
            found = []
        if any(isinstance(item, dict) and item.get("title") == title for item in found):
            continue

        create_args = ["gh", "issue", "create", "-R", repo, "--title", title, "--body", entry.get("body", "")]
        for label in entry.get("labels", []):
            create_args += ["--label", label]
        host.run(create_args, capture_output=True, text=True)

    return 0


# -- feedback-reply / changes-add --


def _cmd_feedback_reply(host: Host, name: str, status: str, now: datetime) -> int:
    """Answer one of Jensen's notes in place. A bad name (`/`, `..`, not `.md`) is a usage error, not a crash:
    print it and exit 2, the same as `count`'s unknown-signal case. A `docker exec` failure (the runner down, a
    missing note raising inside it) is reported by exception type only, never its message -- which can carry the
    container's own stderr, a traceback, a host path -- and exits 1, the same as `_cmd_gh`'s own RuntimeError."""
    try:
        feedback.reply(host, name, status, now)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"feedback-reply failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


def _cmd_changes_add(host: Host, text: str, now: datetime) -> int:
    try:
        feedback.add_change(host, text, now)
    except RuntimeError as exc:
        print(f"changes-add failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


# -- gate --


def _cmd_gate_start(state: State, issue: int, kind: str, handle: str, now: datetime) -> int:
    try:
        state.gate_start(issue, kind, handle, now)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_gate_status(state: State, now: datetime, last: bool, github_output: str | None) -> int:
    """The active gate's JSON (or, with `last`, the one that ended last), or `none`. With `github_output`, only
    `active=true|false` is written there and nothing is printed: a gate's handle can be a host path."""
    active = state.gate(now)
    if github_output:
        _emit([f"active={_bool_str(active is not None)}"], github_output)
        return 0
    shown = state.last_gate() if last else active
    print(json.dumps(shown) if shown else "none")
    return 0


def _cmd_gate_end(state: State, result: str | None, now: datetime) -> int:
    state.gate_end(result, now)
    return 0


# -- assess-check / assess-plan / assess-commit --


def _has_new_notes(host: Host, since: datetime, until: datetime) -> bool:
    """Whether Jensen wrote a note in `(since, until]` (`feedback.collect`). A failed read (the runner
    unreachable, anything else) counts as nothing new -- logged by exception type only, the same as every other
    optional source in this codebase, never its message."""
    try:
        notes = feedback.collect(host, since, until)
    except Exception as exc:
        print(f"warning: reading Jensen's notes failed ({type(exc).__name__})", file=sys.stderr)
        return False
    return bool(notes)


def _cmd_assess_check(host: Host, state: State, now: datetime, github_output: str | None) -> int:
    """`has_work=false`/`skip_reason=pool_hold` while core's usage watch holds (checked first, the same way
    `measure` sits a whole hour out on it, regardless of anything else waiting); otherwise `has_work=true` when
    the assessment has never run, or there's a new digest, a new note from Jensen, a new yardstick result or an
    active project since the last one; `skip_reason=quiet` when none of those are true. A pool that can't be read
    is not a hold: the check falls through to its other signals rather than holding the assessment hostage to
    one failed probe."""
    if pool_is_holding(host, now):
        _emit(["has_work=false", "skip_reason=pool_hold"], github_output)
        return 0

    last_assess = state.last_assess()
    if last_assess is None:
        has_work = True
    else:
        has_work = (state.digest_newer_than(last_assess) or _has_new_notes(host, last_assess, now)
                    or state.yardstick_result_newer_than(last_assess) or state.project_active() is not None)

    _emit([f"has_work={_bool_str(has_work)}", f"skip_reason={'' if has_work else 'quiet'}"], github_output)
    return 0


def _cmd_assess_plan(state: State, github_output: str | None) -> int:
    plan = state.assess_plan()
    project = plan["project"]
    state.clear_assess_plan()
    _emit([f"project={project if project is not None else ''}",
           f"has_project={_bool_str(project is not None)}"], github_output)
    return 0


def _cmd_assess_commit(state: State, now: datetime) -> int:
    state.set_last_assess(now)
    return 0


# -- project-mark --


def _cmd_project_mark(state: State, issue: int, status: str, now: datetime) -> int:
    state.project_mark(issue, status, now)
    return 0


# -- gh passthrough --


def _cmd_gh(host: Host, args: list[str]) -> int:
    """`ARGS...` go straight to `host.jensen_gh`, untouched -- including a leading flag like `--json`, which
    `argparse`'s own `nargs=REMAINDER` would otherwise choke on right after the subcommand name."""
    try:
        result = host.jensen_gh(args, check=False, capture=False)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return result.returncode


def main(argv: list[str] | None = None, *, host: Host | None = None, state: State | None = None,
        now: datetime | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    host = Host.default() if host is None else host
    state = State.default() if state is None else state
    now = datetime.now(UTC) if now is None else now

    if argv and argv[0] == "gh":
        return _cmd_gh(host, argv[1:])

    args = _build_parser().parse_args(argv)

    if args.command == "yardstick":
        return _cmd_yardstick(host, state, now, args)
    if args.command == "measure":
        return _cmd_measure(host, state, now, args.github_output)
    if args.command == "commit":
        return _cmd_commit(state)
    if args.command == "count":
        return _cmd_count(state, args.signal, args.since)
    if args.command == "note":
        return _cmd_note(state, args.issue, args.ref, args.line, now)
    if args.command == "plan":
        return _cmd_plan(state, args.github_output, args.run_id)
    if args.command == "review-target":
        return _cmd_review_target(state, args.run, args.index)
    if args.command == "seed":
        return _cmd_seed(host, args.file, args.repo)
    if args.command == "feedback-reply":
        return _cmd_feedback_reply(host, args.name, args.status, now)
    if args.command == "changes-add":
        return _cmd_changes_add(host, args.text, now)
    if args.command == "gate":
        if args.gate_command == "start":
            return _cmd_gate_start(state, args.issue, args.kind, args.handle, now)
        if args.gate_command == "status":
            return _cmd_gate_status(state, now, args.last, args.github_output)
        return _cmd_gate_end(state, args.result, now)
    if args.command == "assess-check":
        return _cmd_assess_check(host, state, now, args.github_output)
    if args.command == "assess-plan":
        return _cmd_assess_plan(state, args.github_output)
    if args.command == "assess-commit":
        return _cmd_assess_commit(state, now)
    if args.command == "project-mark":
        return _cmd_project_mark(state, args.issue, args.status, now)
    raise AssertionError(f"unhandled command: {args.command!r}")  # pragma: no cover - argparse already validated
