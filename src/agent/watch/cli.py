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
output a public Actions log would show) looks the URL and conversation back up. `plan.json`'s own `propose` list
(what a person approves in Slack) is read separately, by `propose --from watch`, never by this subcommand.

`seed` always goes through the plain `gh` the job itself is logged in as (`host.run`), never through
`host.jensen_gh` -- that call is reading the record of Jensen's work from the outside, not acting as Jensen.

`assess-check`/`assess-commit` drive the assess job the same way `measure`/`commit` drive the watch (`assess-check`
outputs when it `started`, and `assess-commit --at` records that time); `project-mark` is the deterministic record
of which `project` issue, if any, is currently being worked (`$STATE/project-active.json`), read back by
`assess-check`'s own "is anything waiting" check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.watch import feedback
from agent.watch.host import Host, pool_is_holding
from agent.watch.measure import measure
from agent.watch.state import FMT, State

# Labels every install needs regardless of what the seed file's own entries use, so a fresh repo always has
# somewhere for the fix job and the record to put these states.
_STATE_LABELS = ("fixing", "needs-person", "stuck", "project", "proposed", "approved", "rejected",
                 "jensen-assessment")


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def _emit(lines: list[str], github_output: str | None) -> None:
    """Append `lines` to `github_output` in `key=value` form, or print them for a person running this by hand."""
    if github_output:
        with open(github_output, "a") as fh:
            for line in lines:
                fh.write(line + "\n")
    else:
        for line in lines:
            print(line)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jensen_watch")
    sub = parser.add_subparsers(dest="command", required=True)

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

    assess_check_p = sub.add_parser("assess-check")
    assess_check_p.add_argument("--github-output")

    assess_commit_p = sub.add_parser("assess-commit")
    assess_commit_p.add_argument("--at", help="when the assessment's check started; now when not given")

    project_mark_p = sub.add_parser("project-mark")
    project_mark_p.add_argument("--issue", type=int, required=True)
    project_mark_p.add_argument("--status", choices=["active", "done"], required=True)

    return parser


# -- measure --


def _cmd_measure(host: Host, state: State, now: datetime, github_output: str | None) -> int:
    result = measure(state, host, now)
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
    reviews = plan["reviews"]
    state.write_run_reviews(run_id, reviews)
    _emit([
        f"review_indices={json.dumps(list(range(len(reviews))))}",
        f"has_reviews={_bool_str(bool(reviews))}",
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


# -- assess-check / assess-commit --


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
    the assessment has never run, or there's a new digest, a new note from Jensen, or an active project since the
    last one; `skip_reason=quiet` when none of those are true. A pool that can't be read is not a hold: the check
    falls through to its other signals rather than holding the assessment hostage to one failed probe. Always also
    `started=<now>`: `assess-commit --at` records that, so anything that arrives while the assessment runs is still
    new to the next check."""
    started = f"started={_iso(now)}"
    if pool_is_holding(host, now):
        _emit(["has_work=false", "skip_reason=pool_hold", started], github_output)
        return 0

    last_assess = state.last_assess()
    if last_assess is None:
        has_work = True
    else:
        has_work = (state.digest_newer_than(last_assess) or _has_new_notes(host, last_assess, now)
                    or state.project_active() is not None)

    _emit([f"has_work={_bool_str(has_work)}", f"skip_reason={'' if has_work else 'quiet'}", started],
          github_output)
    return 0


def _cmd_assess_commit(state: State, at: str | None, now: datetime) -> int:
    """Records the assessment as done as of `at` (`assess-check`'s own `started`), or `now` without it."""
    if at is None:
        state.set_last_assess(now)
        return 0
    try:
        when = datetime.strptime(at, FMT).replace(tzinfo=UTC)
    except ValueError:
        print(f"invalid --at value: {at!r}, want {FMT!r}", file=sys.stderr)
        return 2
    state.set_last_assess(when)
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
    if args.command == "assess-check":
        return _cmd_assess_check(host, state, now, args.github_output)
    if args.command == "assess-commit":
        return _cmd_assess_commit(state, args.at, now)
    if args.command == "project-mark":
        return _cmd_project_mark(state, args.issue, args.status, now)
    raise AssertionError(f"unhandled command: {args.command!r}")  # pragma: no cover - argparse already validated
