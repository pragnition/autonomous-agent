"""Approval in Slack: improve-jensen proposes changes to Jensen in one Slack message, a person approves or rejects
them in that message's thread, and the approved ones are handed to the `fix` and `project` jobs to ship.

`post_proposals` posts the watch's or the assessment's proposals as numbered bullets and labels each issue
`proposed`; `poll` (the `approvals` job, every ten minutes) reads each open proposal's thread, moves issues
`proposed` -> `approved` or `rejected`, says what changed in the thread, and picks the next approved fix and
project to fire; `fired` (the last step of a fix or project run) says in the thread whether it shipped.

Each posted message is remembered in `<state root>/proposals/<ts>.json`
(`{"channel", "ts", "posted", "items": [{"n", "issue", "change"}], "notified"}`), written atomically. GitHub's
labels stay the record of where each issue is: a poll decides everything again from the thread and the labels,
never from what an earlier poll saw, so a poll that dies halfway is simply finished by the next one.

GitHub goes through a `gh` callable (`args -> stdout`, raising on a non-zero exit) and Slack through a `Slack`,
both passed in so tests can use fakes. Only the people in `approvers` count, and only their replies and
reactions; an empty set approves nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
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
_MAX_PROPOSALS = 5
_MAX_CHANGE = 140

_APPROVE_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark", "+1", "ballot_box_with_check"})
_REJECT_REACTIONS = frozenset({"x", "-1", "no_entry", "negative_squared_cross_mark"})

_APPROVE_WORDS = ("approve", "approved", "yes", "ok", "okay", "lgtm", "ship", "ship it", "go", "go ahead")
_REJECT_WORDS = ("reject", "rejected", "no", "nope", "skip", "drop", "don't", "dont")
# Longest first, so `ship it` wins over `ship`; a word must end there (`nothing` isn't `no`).
_DECISION = re.compile("(" + "|".join(re.escape(word) for word in sorted(_APPROVE_WORDS + _REJECT_WORDS,
                                                                         key=len, reverse=True)) + r")(?![\w'])")
_MENTION = re.compile(r"<@[^>]*>")
_NUMBERS = re.compile(r"(\d+)(?:\s*(?:-|–|—|to)\s*(\d+))?")
# `approve all but 2` must not approve 2: an approval that hedges is left for the person to say again plainly.
_HEDGE = re.compile(r"\b(?:but|except|not|without)\b")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _escape(text: str) -> str:
    """`text` as literal Slack text: `&`, `<` and `>` would otherwise make links, mentions or `<!channel>`."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _validate_proposals(value) -> list[dict]:
    """`value` as a list of `{"issue": int, "change": str}`: entries whose `issue` is a positive int (a bool
    doesn't count) and whose `change` is a non-empty string once its whitespace is collapsed, `change` cut to 140
    characters (ending in `…` when cut), the first entry per issue kept, at most 5. Anything that isn't a list is
    `[]`."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        issue, change = entry.get("issue"), entry.get("change")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0 or issue in seen:
            continue
        change = " ".join(change.split()) if isinstance(change, str) else ""
        if not change:
            continue
        if len(change) > _MAX_CHANGE:
            change = change[:_MAX_CHANGE - 1] + "…"
        seen.add(issue)
        out.append({"issue": issue, "change": change})
        if len(out) == _MAX_PROPOSALS:
            break
    return out


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


def post_proposals(slack, gh: Gh, state_root: Path, channel: str, items: list[dict], repo: str,
                   now: datetime) -> int:
    """Post `items` (validated proposals) to `channel` as one numbered message, leaving out any whose issue is
    closed or already in the flow (`proposed`, `approved`, `rejected`, `fixing`, `stuck`, `needs-person`); then
    record the message and label each issue `proposed` with a `Proposed in Slack: <change>` comment. Returns how
    many were posted -- 0, posting nothing, when none are left. A `SlackError` posting propagates before any
    issue is labelled."""
    labels = _open_issues(gh)
    kept: list[dict] = []
    for item in items:
        issue = item["issue"]
        if issue in labels and not labels[issue] & _IN_FLOW and all(k["issue"] != issue for k in kept):
            kept.append({"n": len(kept) + 1, "issue": issue, "change": item["change"]})
    if not kept:
        return 0

    lines = ["*improve-jensen wants to change Jensen*"]
    lines += [f"{item['n']}. {_escape(item['change'])} "
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
    """What a thread reply decides: `("approve" | "reject", items)`, where `items` are the item numbers named
    after the word (`1 3`, `1, 3`, `1-3`) or `None` for all of them (also `all`). `None` -- no decision -- unless
    the reply starts (mentions, whitespace and case aside) with an approve word (`approve`, `yes`, `ok`, `lgtm`,
    `ship it`, `go ahead`, ...) or a reject word (`reject`, `no`, `nope`, `skip`, `drop`, `don't`, ...). A reply
    with a `?` asks rather than decides, and an approval that hedges (`but`, `except`, `not`, `without`) is
    ambiguous: both are `None`, so nothing is approved by accident."""
    if not isinstance(text, str):
        return None
    text = _MENTION.sub(" ", text).replace("’", "'").lower().lstrip(" \t\r\n,:;")
    if "?" in text:
        return None
    match = _DECISION.match(text)
    if match is None:
        return None
    verdict = "approve" if match.group(1) in _APPROVE_WORDS else "reject"
    rest = text[match.end():]
    if verdict == "approve" and _HEDGE.search(rest):
        return None
    items: set[int] = set()
    for low, high in _NUMBERS.findall(rest):
        low, high = sorted((int(low), int(high or low)))
        items.update(range(low, min(high, low + 99) + 1))
    return verdict, items or None


def _undecided(labels: set[str] | None) -> bool:
    """Whether an item can still change: its issue is open, not yet approved, and `proposed` or `rejected`."""
    return labels is not None and APPROVED not in labels and bool(labels & {PROPOSED, REJECTED})


def _decisions(slack, record: dict, approvers: set[str]) -> dict[int, str]:
    """Each item number's latest decision in `record`'s thread: approvers' reactions on the message first (a
    reject reaction beats an approve one), then their replies in time order, each overriding what came before for
    the items it names. Replies from anyone else, from a bot, or of any subtype but a reply also sent to the
    channel don't count. Reactions that can't be read (the bot without `reactions:read`, say) are logged and
    left out: they only decide items no reply names, so without them those items just wait."""
    numbers = [item["n"] for item in record["items"]]
    decided: dict[int, str] = {}
    try:
        reactions = slack.reactions(record["channel"], record["ts"])
    except SlackError as exc:
        _warn(f"reading the reactions on proposal {record['ts']}", exc)
        reactions = []
    reacted = {reaction["name"].split("::")[0] for reaction in reactions if approvers & set(reaction["users"])}
    if reacted & _APPROVE_REACTIONS:
        decided = dict.fromkeys(numbers, "approve")
    if reacted & _REJECT_REACTIONS:
        decided = dict.fromkeys(numbers, "reject")
    for reply in sorted(slack.replies(record["channel"], record["ts"]), key=lambda m: _ts_key(m.get("ts"))):
        if (reply.get("user") not in approvers or reply.get("bot_id")
                or reply.get("subtype") not in (None, "thread_broadcast")):
            continue
        decision = parse_decision(reply.get("text") or "")
        if decision is None:
            continue
        verdict, which = decision
        for n in numbers:
            if which is None or n in which:
                decided[n] = verdict
    return decided


def _apply(gh: Gh, issue: int, labels: set[str], verdict: str) -> bool:
    """Label `issue` approved or rejected (removing only labels it has, so `gh` never fails on one it lacks)
    and comment why; `labels` is updated to match. False, with a warning, when the label change failed."""
    target = APPROVED if verdict == "approve" else REJECTED
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


def _summary(approved: list[int], dropped: list[int]) -> str:
    parts = []
    if approved:
        parts.append(f"Approved {', '.join(map(str, approved))}: queued to ship.")
    if dropped:
        parts.append(f"Dropped {', '.join(map(str, dropped))}.")
    return " ".join(parts)


def _busy_jobs(gh: Gh) -> set[str]:
    """Which of `fix` and `project` is queued or running in another run of the workflow (any run but
    `$GITHUB_RUN_ID`, this one). Raises when `gh` fails or answers with something unexpected."""
    own = os.environ.get("GITHUB_RUN_ID", "")
    runs = json.loads(gh(["run", "list", "--workflow", _WORKFLOW, "--json", "databaseId,status", "--limit", "30"]))
    if not isinstance(runs, list):
        raise ValueError("gh run list did not return a list")
    busy: set[str] = set()
    for run in runs:
        if not isinstance(run, dict) or run.get("status") == "completed":
            continue
        run_id = run.get("databaseId")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            raise ValueError("gh run list returned a run without an id")
        if str(run_id) == own:
            continue
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


def poll(slack, gh: Gh, state_root: Path, approvers: set[str], repo: str, now: datetime) -> dict:
    """Apply every new decision in the proposals' threads, then pick what to fire: `{"fix": int | None,
    "project": int | None}`.

    Only records with an item still undecided (its issue open and `proposed` or `rejected`) are read. Approved is
    final: an issue already `approved`, or closed, never changes; a rejected one can still be approved. Each
    record whose items changed gets one thread reply saying how. A Slack error on one record is logged (its code
    only) and the others go on; a failing `gh issue list` raises."""
    labels = _open_issues(gh)
    for record in _read_records(state_root):
        if not any(_undecided(labels.get(item["issue"])) for item in record["items"]):
            continue
        try:
            decided = _decisions(slack, record, approvers)
        except SlackError as exc:
            _warn(f"reading proposal {record['ts']} from Slack", exc)
            continue
        approved, dropped = [], []
        for item in record["items"]:
            current = labels.get(item["issue"])
            verdict = decided.get(item["n"])
            if not _undecided(current) or verdict is None or (verdict == "reject" and REJECTED in current):
                continue
            if _apply(gh, item["issue"], current, verdict):
                (approved if verdict == "approve" else dropped).append(item["n"])
        summary = _summary(approved, dropped)
        if summary:
            try:
                slack.post(record["channel"], summary, thread_ts=record["ts"])
            except SlackError as exc:
                _warn(f"replying in proposal {record['ts']}", exc)
    return _pick(gh, labels)


# -- fired --


def fired(slack, gh: Gh, state_root: Path, issue: int, kind: str, repo: str, now: datetime) -> None:
    """Close the loop after a `fix` or `project` run on `issue`, in its proposal's thread when it has one:

    - closed (shipped): `Shipped <n>: <change>.`
    - still open after a fix: labelled `stuck` instead of `approved`, with a comment, and
      `Couldn't ship <n>: <change>. It needs a person (#N).`
    - still open after a project run: nothing while it's going; once it's `stuck` or `needs-person`,
      `<n> needs a person (#N).`, said once (the record's `notified`).

    GitHub is updated before Slack, and with `slack` `None` (no token) only GitHub is. A `SlackError` replying
    propagates, as does a failing `gh`."""
    if kind not in ("fix", "project"):
        raise ValueError(f"bad kind: {kind!r}")
    data = json.loads(gh(["issue", "view", str(issue), "--json", "state,labels"]))
    if not isinstance(data, dict):
        raise ValueError("gh issue view did not return an object")
    labels = {label["name"] for label in data.get("labels") or []
              if isinstance(label, dict) and isinstance(label.get("name"), str)}
    found = _find(state_root, issue)

    def say(text: str) -> None:
        if slack is not None and found is not None:
            slack.post(found[0]["channel"], text, thread_ts=found[0]["ts"])

    change = _escape(found[1]["change"].rstrip(".")) if found else ""
    n = found[1]["n"] if found else 0
    if str(data.get("state", "")).upper() == "CLOSED":
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
    if labels & _NEEDS_PERSON and found is not None and slack is not None and issue not in found[0]["notified"]:
        say(f"{n} needs a person (#{issue}).")
        record = found[0]
        record["notified"] = [*record["notified"], issue]
        _write_record(state_root, record)
