"""Approval in Slack: improve-jensen proposes changes to Jensen in one Slack message, a person approves or rejects
them in that message's thread, and the approved ones are handed to the `fix` and `project` jobs to ship.

`post_proposals` posts the watch's or the assessment's proposals as numbered bullets and labels each issue
`proposed`; `poll` (the `approvals` job, every ten minutes) reads each open proposal's thread, moves issues
`proposed` -> `approved` or `rejected`, says what changed in the thread, and picks the next approved fix and
project to fire; `ready` (the first step of a fix or project run) checks the issue is still approved; `fired` (the
last step) says in the thread whether it shipped.

Each posted message is remembered in `<state root>/proposals/<ts>.json`
(`{"channel", "ts", "posted", "items": [{"n", "issue", "change"}], "notified"}`, plus `"released": true` once
it has expired), written atomically; an item from the assessment's "what worked" carries `"kind": "learned"`
and maybe `"credit"` (whose session it came from), which go into Slack and this record but never into GitHub. An
issue belongs to the newest record that names it. GitHub's labels stay the record of where each issue is: a poll
decides everything again from the thread and the labels, never from what an earlier poll saw, so a poll that dies
halfway is simply finished by the next one.

GitHub goes through a `gh` callable (`args -> stdout`, raising on a non-zero exit) and Slack through a `Slack`,
both passed in so tests can use fakes. Only the people in `approvers` count, and only their replies and
reactions; an empty set approves nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from agent.watch.slack import SlackError
from agent.watch.state import FMT

Gh = Callable[[list[str]], str]

PROPOSED, APPROVED, REJECTED = "proposed", "approved", "rejected"
# An issue carrying any of these is already somewhere in the flow, so it's never proposed again.
_IN_FLOW = frozenset({PROPOSED, APPROVED, REJECTED, "fixing", "stuck", "needs-person"})
_NEEDS_PERSON = frozenset({"stuck", "needs-person"})

_PROPOSALS_DIR = "proposals"
_WORKFLOW = "improve-jensen.yml"
_DECIDE_DAYS = 14  # a proposal nobody decided on by then is released
_RELEASED = f"No decision in Slack within {_DECIDE_DAYS} days; the watch can propose it again."
# A run that isn't finished has one of these statuses; `gh run list --status` asks for each. (`pending` is a status
# the API reports but `gh run list --status` rejects, and a rejected flag would fire nothing ever again.)
_UNFINISHED = ("in_progress", "queued", "waiting", "requested")
# Consecutive project runs that didn't succeed, per issue, and how many of them make a project `stuck`.
_FAILURES_FILE = "project-failures.json"
_FAILURES_BEFORE_STUCK = 2

# A thumbs-up often only means "seen", so it approves nothing.
_APPROVE_REACTIONS = frozenset({"white_check_mark", "ballot_box_with_check"})
_REJECT_REACTIONS = frozenset({"x", "-1", "no_entry", "negative_squared_cross_mark"})

_APPROVE_WORDS = ("approve", "approved", "yes", "ok", "okay", "lgtm", "ship", "ship it", "go", "go ahead")
_REJECT_WORDS = ("reject", "rejected", "no", "nope", "skip", "drop", "don't", "dont")
# Longest first, so `ship it` wins over `ship`; a word must end there (`nothing` isn't `no`).
_DECISION = re.compile("(" + "|".join(re.escape(word) for word in sorted(_APPROVE_WORDS + _REJECT_WORDS,
                                                                         key=len, reverse=True)) + r")(?![\w'])")
_MENTION = re.compile(r"<@[^>]*>")
# What may follow the word, one token at a time; anything else (`bad`) means the reply isn't a plain decision.
_TOKEN = re.compile(r"\s*(?:(?P<num>#?\d+)|(?P<range>[-–—]|to\b)|(?P<sep>[,&]|and\b)|(?P<all>all\b)"
                    r"|(?P<bad>\S+))")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _escape(text: str) -> str:
    """`text` as literal Slack text: `&`, `<` and `>` would otherwise make links, mentions or `<!channel>`."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -- GitHub --


def _open_issues(gh: Gh) -> dict[int, set[str]]:
    """Every open issue's labels, from one `gh issue list`. Raises when `gh` fails or answers with something
    that isn't a list."""
    data = json.loads(gh(["issue", "list", "--state", "open", "--limit", "1000", "--json", "number,labels"]))
    if not isinstance(data, list):
        raise ValueError("gh issue list did not return a list")
    return {issue["number"]: {label["name"] for label in issue.get("labels") or []
                              if isinstance(label, dict) and isinstance(label.get("name"), str)}
            for issue in data if isinstance(issue, dict) and isinstance(issue.get("number"), int)}


def _issue(gh: Gh, issue: int) -> tuple[bool, set[str]]:
    """Whether `issue` is open, and its labels (`gh issue view`). Raises when `gh` fails."""
    data = json.loads(gh(["issue", "view", str(issue), "--json", "state,labels"]))
    if not isinstance(data, dict):
        raise ValueError("gh issue view did not return an object")
    labels = {label["name"] for label in data.get("labels") or []
              if isinstance(label, dict) and isinstance(label.get("name"), str)}
    return str(data.get("state", "")).upper() != "CLOSED", labels


def _warn(what: str, exc: BaseException) -> None:
    """A warning on stderr (the public workflow log) naming only what failed and the exception's type -- or, for
    a `SlackError`, Slack's code."""
    detail = str(exc) if isinstance(exc, SlackError) else type(exc).__name__
    print(f"warning: {what} failed ({detail})", file=sys.stderr)


# -- records --


def _record_path(root: Path, ts: str) -> Path:
    if not re.fullmatch(r"\d+(?:\.\d+)?", ts):
        raise ValueError("a Slack ts is digits and a dot")
    return Path(root) / _PROPOSALS_DIR / f"{ts}.json"


def _write_record(root: Path, record: dict) -> None:
    path = _record_path(root, record["ts"])
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(record))


def _ts_key(ts) -> tuple[int, int]:
    """A Slack `ts` (`seconds.micros`) as something that sorts in time order; an unreadable one sorts first."""
    seconds, _, fraction = str(ts).partition(".")
    try:
        return int(seconds), int((fraction + "000000")[:6])
    except ValueError:
        return 0, 0


def _valid_record(data) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("channel"), str) or not isinstance(data.get("ts"), str):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    items = [item for item in items if isinstance(item, dict) and isinstance(item.get("n"), int)
             and isinstance(item.get("issue"), int) and isinstance(item.get("change"), str)]
    notified = [n for n in data.get("notified") or [] if isinstance(n, int)]
    return {**data, "items": items, "notified": notified}


def _read_records(root: Path) -> list[dict]:
    """Every readable proposal record, oldest first; a broken file is skipped."""
    folder = Path(root) / _PROPOSALS_DIR
    if not folder.is_dir():
        return []
    records = []
    for path in folder.glob("*.json"):
        try:
            record = _valid_record(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda record: _ts_key(record["ts"]))


def _find(root: Path, issue: int) -> tuple[dict, dict] | None:
    """The newest record proposing `issue`, with that issue's item."""
    for record in reversed(_read_records(root)):
        for item in record["items"]:
            if item["issue"] == issue:
                return record, item
    return None


# -- propose --


def _slack_change(item: dict) -> str:
    """An item's change as its Slack line shows it: a learned one marked so and crediting whose session it came
    from, when that's known."""
    change = _escape(item["change"])
    if item.get("kind") != "learned":
        return change
    credit = f" (from {_escape(item['credit'])})" if item.get("credit") else ""
    return f"Learned: {change}{credit}"


def post_proposals(slack, gh: Gh, state_root: Path, channel: str, items: list[dict], repo: str,
                   now: datetime) -> int:
    """Post `items` (validated proposals) to `channel` as one numbered message, leaving out any whose issue is
    closed or already in the flow (`proposed`, `approved`, `rejected`, `fixing`, `stuck`, `needs-person`); then
    record the message and label each issue `proposed` with a `Proposed in Slack: <change>` comment. A learned
    item's line reads `Learned: <change> (from <credit>)`; its credit stays out of GitHub. Returns how many were
    posted -- 0, posting nothing, when none are left. A `SlackError` posting propagates before any issue is
    labelled."""
    labels = _open_issues(gh)
    kept: list[dict] = []
    for item in items:
        issue = item["issue"]
        if issue in labels and not labels[issue] & _IN_FLOW and all(k["issue"] != issue for k in kept):
            kept.append({"n": len(kept) + 1, "issue": issue, "change": item["change"],
                         **{key: item[key] for key in ("kind", "credit") if key in item}})
    if not kept:
        return 0

    lines = ["*improve-jensen wants to change Jensen*"]
    lines += [f"{item['n']}. {_slack_change(item)} "
              f"(<https://github.com/{repo}/issues/{item['issue']}|#{item['issue']}>)" for item in kept]
    lines.append("Reply here: `approve` ships them all, `approve 1 3` ships some, `reject 2` drops one.")
    ts = slack.post(channel, "\n".join(lines))
    _write_record(state_root, {"channel": channel, "ts": ts, "posted": _iso(now), "items": kept, "notified": []})

    for item in kept:
        try:
            gh(["issue", "edit", str(item["issue"]), "--add-label", PROPOSED])
            gh(["issue", "comment", str(item["issue"]), "--body", f"Proposed in Slack: {item['change']}"])
        except Exception as exc:
            _warn(f"marking #{item['issue']} proposed", exc)
    return len(kept)


# -- approve --


def parse_decision(text: str) -> tuple[str, set[int] | None] | None:
    """What a thread reply decides: `("approve" | "reject", items)`, where `items` are the item numbers it names,
    or `None` when it names none (a bare word, or `all`).

    Strict, so chatter never decides anything: once mentions are stripped, the whole reply must be an approve word
    (`approve`, `yes`, `ok`, `lgtm`, `ship it`, `go ahead`, ...) or a reject word (`reject`, `no`, `nope`, `skip`,
    `drop`, `don't`, ...) followed by nothing but a plain item list -- numbers or `#N`, separated by spaces,
    commas, `and` or `&`, ranges (`1-3`, with an en or em dash too, or `1 to 3`), or `all` on its own -- and a
    trailing `.` or `!`. Anything else is `None`: `approve 1, 3; reject 2`, `ok, I will check #2 tomorrow`."""
    if not isinstance(text, str):
        return None
    text = _MENTION.sub(" ", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("’", "'")
    text = text.lower().strip().lstrip(",:").rstrip(".! \t\r\n").strip()
    match = _DECISION.match(text)
    if match is None:
        return None
    verdict = "approve" if match.group(1) in _APPROVE_WORDS else "reject"

    rest = text[match.end():].strip()
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(rest):
        token = _TOKEN.match(rest, pos)
        kind = token.lastgroup
        if kind == "bad":
            return None
        tokens.append((kind, token.group(kind)))
        pos = token.end()

    items: set[int] = set()
    every = False
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "sep":
            i += 1
        elif kind == "all":
            every = True
            i += 1
        elif kind == "num":
            low = high = int(value.lstrip("#"))
            if i + 2 < len(tokens) and tokens[i + 1][0] == "range" and tokens[i + 2][0] == "num":
                high = int(tokens[i + 2][1].lstrip("#"))
                i += 3
            else:
                i += 1
            low, high = sorted((low, high))
            items.update(range(low, min(high, low + 99) + 1))
        else:   # a range with no number on one side
            return None
    if every and items:
        return None
    return verdict, items or None


def _undecided(labels: set[str] | None) -> bool:
    """Whether an item can still change: its issue is open, not yet approved, and `proposed` or `rejected`."""
    return labels is not None and APPROVED not in labels and bool(labels & {PROPOSED, REJECTED})


def _decisions(slack, record: dict, approvers: set[str]) -> list[tuple[str, set[int] | None]]:
    """The decisions in `record`'s thread, in the order they apply: approvers' reactions on the message first (a
    reject reaction before an approve one), then their replies in time order (`parse_decision`). Replies from
    anyone else, from a bot, or of any subtype but a reply also sent to the channel don't count. Reactions that
    can't be read (the bot without `reactions:read`, say) are logged and left out: they only decide items no reply
    names, so without them those items just wait."""
    decisions: list[tuple[str, set[int] | None]] = []
    try:
        reactions = slack.reactions(record["channel"], record["ts"])
    except SlackError as exc:
        _warn(f"reading the reactions on proposal {record['ts']}", exc)
        reactions = []
    reacted = {reaction["name"].split("::")[0] for reaction in reactions if approvers & set(reaction["users"])}
    if reacted & _REJECT_REACTIONS:
        decisions.append(("reject", None))
    if reacted & _APPROVE_REACTIONS:
        decisions.append(("approve", None))
    for reply in sorted(slack.replies(record["channel"], record["ts"]), key=lambda m: _ts_key(m.get("ts"))):
        if (reply.get("user") not in approvers or reply.get("bot_id")
                or reply.get("subtype") not in (None, "thread_broadcast")):
            continue
        decision = parse_decision(reply.get("text") or "")
        if decision is not None:
            decisions.append(decision)
    return decisions


def _settle(state: str, n: int, decisions: list[tuple[str, set[int] | None]]) -> str:
    """Item `n`'s state (`proposed` or `rejected` to start with) after `decisions` in order. A decision that names
    `n` sets it either way; one that names no numbers moves it only while it's still `proposed`, so re-approving
    a rejected item takes its number."""
    for verdict, which in decisions:
        target = APPROVED if verdict == "approve" else REJECTED
        if (which is None and state == PROPOSED) or (which is not None and n in which):
            state = target
    return state


def _apply(gh: Gh, issue: int, labels: set[str], target: str) -> bool:
    """Label `issue` `target` (approved or rejected; removing only labels it has, so `gh` never fails on one it
    lacks) and comment why; `labels` is updated to match. False, with a warning, when the label change failed."""
    remove = [label for label in ((PROPOSED, REJECTED) if target == APPROVED else (PROPOSED,)) if label in labels]
    args = ["issue", "edit", str(issue), "--add-label", target]
    for label in remove:
        args += ["--remove-label", label]
    try:
        gh(args)
    except Exception as exc:
        _warn(f"labelling #{issue} {target}", exc)
        return False
    labels.difference_update(remove)
    labels.add(target)
    try:
        gh(["issue", "comment", str(issue), "--body",
            "Approved in Slack." if target == APPROVED else "Not approved in Slack."])
    except Exception as exc:
        _warn(f"commenting on #{issue}", exc)
    return True


def _expired(record: dict, now: datetime) -> bool:
    """Whether `record` was posted more than `_DECIDE_DAYS` ago. One whose `posted` can't be read never expires."""
    try:
        posted = datetime.strptime(str(record.get("posted")), FMT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return now - posted > timedelta(days=_DECIDE_DAYS)


def _unpropose(gh: Gh, issue: int, labels: set[str]) -> bool:
    """Take `proposed` off an undecided `issue`, with a comment, so a later watch can propose it again. True when
    there was nothing to take off or it came off; False, with a warning, when `gh` failed."""
    if PROPOSED not in labels or labels & {APPROVED, REJECTED}:
        return True
    try:
        gh(["issue", "edit", str(issue), "--remove-label", PROPOSED])
    except Exception as exc:
        _warn(f"releasing #{issue}", exc)
        return False
    labels.discard(PROPOSED)
    try:
        gh(["issue", "comment", str(issue), "--body", _RELEASED])
    except Exception as exc:
        _warn(f"commenting on #{issue}", exc)
    return True


def _summary(approved: list[int], dropped: list[int]) -> str:
    parts = []
    if approved:
        parts.append(f"Approved {', '.join(map(str, approved))}: queued to ship.")
    if dropped:
        parts.append(f"Dropped {', '.join(map(str, dropped))}.")
    return " ".join(parts)


def _busy_jobs(gh: Gh) -> set[str]:
    """Which of `fix` and `project` is queued or running in another run of the workflow (any unfinished run but
    `$GITHUB_RUN_ID`, this one), however long ago it started: unfinished runs are asked for by status, never
    read off the newest few. Raises when `gh` fails or answers with something unexpected."""
    own = os.environ.get("GITHUB_RUN_ID", "")
    run_ids: list[int] = []
    for status in _UNFINISHED:
        runs = json.loads(gh(["run", "list", "--workflow", _WORKFLOW, "--status", status,
                              "--json", "databaseId,status", "--limit", "100"]))
        if not isinstance(runs, list):
            raise ValueError("gh run list did not return a list")
        for run in runs:
            run_id = run.get("databaseId") if isinstance(run, dict) else None
            if isinstance(run_id, bool) or not isinstance(run_id, int):
                raise ValueError("gh run list returned a run without an id")
            if str(run_id) != own and run_id not in run_ids:
                run_ids.append(run_id)
    busy: set[str] = set()
    for run_id in run_ids:
        data = json.loads(gh(["run", "view", str(run_id), "--json", "jobs"]))
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("gh run view did not return jobs")
        busy.update(job["name"] for job in jobs if isinstance(job, dict) and job.get("name") in ("fix", "project")
                    and job.get("status") != "completed")
    return busy


def _pick(gh: Gh, labels: dict[int, set[str]]) -> dict:
    """The next approved fix (no `project`, `fixing`, `stuck` or `needs-person`) and project (no `stuck` or
    `needs-person`), lowest number first, each only while no job of its kind is queued or running elsewhere.
    Nothing at all when that can't be checked."""
    fix = min((n for n, ls in labels.items()
               if APPROVED in ls and not ls & {"project", "fixing", *_NEEDS_PERSON}), default=None)
    project = min((n for n, ls in labels.items()
                   if APPROVED in ls and "project" in ls and not ls & _NEEDS_PERSON), default=None)
    if fix is None and project is None:
        return {"fix": None, "project": None}
    try:
        busy = _busy_jobs(gh)
    except Exception as exc:
        _warn("checking for a running fix or project job (nothing fires this time)", exc)
        return {"fix": None, "project": None}
    return {"fix": None if "fix" in busy else fix, "project": None if "project" in busy else project}


def poll(slack, gh: Gh, state_root: Path, approvers: set[str], repo: str, now: datetime, *,
         fire: bool = True) -> dict:
    """Apply every new decision in the proposals' threads, then pick what to fire: `{"fix": int | None,
    "project": int | None}` (both `None` without `fire`, which still applies the decisions).

    Each issue belongs to the newest record naming it, and only that record decides or releases it. A record older
    than `_DECIDE_DAYS` is no longer read: its undecided items lose `proposed` (with a comment) and the record is
    marked `released`, after which it's skipped. A `proposed` issue no live record owns (the state folder lost it,
    or its file is broken) is released the same way, since nothing could ever decide it.

    Of the rest, only records with an item still undecided (its issue open and `proposed` or `rejected`) are read.
    Approved is final: an issue already `approved`, or closed, never changes. Each record whose items changed gets
    one thread reply saying how. A Slack error on one record is logged (its code only) and the others go on; a
    failing `gh issue list` raises."""
    labels = _open_issues(gh)
    # Read after the labels: `post_proposals` writes a record before it labels anything, so every `proposed` issue
    # just listed already has its record.
    records = _read_records(state_root)
    owner = {item["issue"]: record for record in records for item in record["items"]}
    for issue in sorted(labels):
        record = owner.get(issue)
        if record is None or record.get("released"):
            _unpropose(gh, issue, labels[issue])

    for record in records:
        if record.get("released"):
            continue
        mine = [item for item in record["items"] if owner[item["issue"]] is record]
        if _expired(record, now):
            released = [_unpropose(gh, item["issue"], labels[item["issue"]]) for item in mine
                        if item["issue"] in labels]
            if all(released):
                _write_record(state_root, {**record, "released": True})
            continue
        if not any(_undecided(labels.get(item["issue"])) for item in mine):
            continue
        try:
            decisions = _decisions(slack, record, approvers)
        except SlackError as exc:
            _warn(f"reading proposal {record['ts']} from Slack", exc)
            continue
        approved, dropped = [], []
        for item in mine:
            current = labels.get(item["issue"])
            if not _undecided(current):
                continue
            start = PROPOSED if PROPOSED in current else REJECTED
            end = _settle(start, item["n"], decisions)
            if end != start and _apply(gh, item["issue"], current, end):
                (approved if end == APPROVED else dropped).append(item["n"])
        summary = _summary(approved, dropped)
        if summary:
            try:
                slack.post(record["channel"], summary, thread_ts=record["ts"])
            except SlackError as exc:
                _warn(f"replying in proposal {record['ts']}", exc)
    return _pick(gh, labels) if fire else {"fix": None, "project": None}


# -- ready --


def ready(gh: Gh, issue: int, kind: str) -> bool:
    """Whether a `fix` or `project` run should go ahead on `issue` now: it's open and `approved`, with no `stuck`
    or `needs-person` (and, for a fix, not a `project`). The approvals run that named it can be minutes old."""
    is_open, labels = _issue(gh, issue)
    return (is_open and APPROVED in labels and not labels & _NEEDS_PERSON
            and (kind == "project" or "project" not in labels))


# -- fired --


def _read_failures(root: Path) -> dict[str, int]:
    try:
        data = json.loads((Path(root) / _FAILURES_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def _set_failures(root: Path, issue: int, count: int) -> None:
    """Record `count` consecutive failed runs for project `issue` (0 forgets it)."""
    failures = _read_failures(root)
    if count == failures.get(str(issue), 0):
        return
    failures.pop(str(issue), None)
    if count:
        failures[str(issue)] = count
    _atomic_write(Path(root) / _FAILURES_FILE, json.dumps(failures))


def fired(slack, gh: Gh, state_root: Path, issue: int, kind: str, repo: str, now: datetime, *,
          outcome: str | None = None) -> None:
    """Close the loop after a `fix` or `project` run on `issue`, in its proposal's thread when it has one:

    - closed (shipped): `Shipped <n>: <change>.`
    - still open after a fix: labelled `stuck` instead of `approved`, with a comment, and
      `Couldn't ship <n>: <change>. It needs a person (#N).`
    - still open after a project run: nothing while it's going. `outcome` is how the run's Claude step ended; a
      project whose last 2 runs didn't succeed is labelled `stuck` instead of `approved`, with a comment (a
      success starts the count again). Once it's `stuck` or `needs-person`, `<n> needs a person (#N).`, said once
      (the record's `notified`).

    GitHub is updated before Slack, and with `slack` `None` (no token) only GitHub is. A `SlackError` replying
    propagates, as does a failing `gh`."""
    if kind not in ("fix", "project"):
        raise ValueError(f"bad kind: {kind!r}")
    is_open, labels = _issue(gh, issue)
    found = _find(state_root, issue)

    def say(text: str) -> None:
        if slack is not None and found is not None:
            slack.post(found[0]["channel"], text, thread_ts=found[0]["ts"])

    change = _escape(found[1]["change"].rstrip(".")) if found else ""
    n = found[1]["n"] if found else 0
    if not is_open:
        if kind == "project":
            _set_failures(state_root, issue, 0)
        say(f"Shipped {n}: {change}.")
        return
    if kind == "fix":
        args = ["issue", "edit", str(issue)]
        if APPROVED in labels:
            args += ["--remove-label", APPROVED]
        gh(args + ["--add-label", "stuck"])
        gh(["issue", "comment", str(issue), "--body", "The approved change didn't ship; a person needs to look."])
        say(f"Couldn't ship {n}: {change}. It needs a person (#{issue}).")
        return

    if outcome == "success":
        _set_failures(state_root, issue, 0)
    elif outcome is not None and not labels & _NEEDS_PERSON:
        failures = _read_failures(state_root).get(str(issue), 0) + 1
        if failures < _FAILURES_BEFORE_STUCK:
            _set_failures(state_root, issue, failures)
        else:
            args = ["issue", "edit", str(issue)]
            if APPROVED in labels:
                args += ["--remove-label", APPROVED]
            gh(args + ["--add-label", "stuck"])
            gh(["issue", "comment", str(issue), "--body",
                f"The approved project's last {failures} runs failed; a person needs to look."])
            labels = (labels - {APPROVED}) | {"stuck"}
            _set_failures(state_root, issue, 0)
    if labels & _NEEDS_PERSON and found is not None and slack is not None and issue not in found[0]["notified"]:
        say(f"{n} needs a person (#{issue}).")
        record = found[0]
        record["notified"] = [*record["notified"], issue]
        _write_record(state_root, record)
