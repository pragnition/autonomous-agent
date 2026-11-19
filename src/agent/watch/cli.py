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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.watch import feedback
from agent.watch.host import Host
from agent.watch.measure import measure
from agent.watch.state import FMT, State

# Labels every install needs regardless of what the seed file's own entries use, so a fresh repo always has
# somewhere for the fix job and the record to put these states.
_STATE_LABELS = ("fixing", "fix-deployed", "needs-person", "stuck", "project")


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
    print it and exit 2, the same as `count`'s unknown-signal case."""
    try:
        feedback.reply(host, name, status, now)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _cmd_changes_add(host: Host, text: str, now: datetime) -> int:
    feedback.add_change(host, text, now)
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
    raise AssertionError(f"unhandled command: {args.command!r}")  # pragma: no cover - argparse already validated
