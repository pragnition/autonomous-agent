"""`measure`: one hourly pass over core's finished work and Jensen's PRs, boiled down to a digest.

Pulls the window's finished work items out of core (`core_collect.py`, run inside `agent-core` through
`Host.core_python`), fetches each undelivered turn's own runner transcript to size it up with
`agent.watch.transcript.turn_facts`, finds Jensen's PRs for the window with `gh search prs` and nails down each
one's size, CI result and co-author trail with `gh pr view`/`gh pr checks`, then stages what
`agent.watch.state.State` keeps (`State.stage`) and writes a digest file for improve-jensen to read next. Every
piece of free text that lands in the digest -- a person's ask, Jensen's reply, a tool or error line, a deployed
issue's title -- is scrubbed of secrets first, since it might end up quoted back out in a public issue; so is
every raw transcript line *before* `turn_facts` ever truncates it, since a secret that straddles that cut would
otherwise leak a prefix no exact-value or pattern match could catch after the fact.

Measuring is staged, not committed: the digest this produces is meant to be acted on before the window is
final, and that action can fail. `State.stage` records this window's metrics and newly-seen clones without
touching the permanent `metrics.jsonl` or `known_clones()` -- only `State.commit()`, called once that action
succeeds, folds them in. So a window re-measured after a failed action (the same items, reprocessed) overwrites
its own prior staging rather than duplicating metrics, and a turn's own clone is never mistaken for a reclone of
itself just because an earlier, abandoned attempt at the same window already staged it as "seen."

Two things can go wrong here without the rest of the hour's measurement being worth losing: `gh` (missing
token, a single PR's own `gh pr view`/`gh pr checks` failing, or any other failure) and the runner's transcripts
(the container unreachable). Both are caught around just the work that depends on them; everything else in the
window still gets measured and written, and neither failure's own message carries a host path.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from agent.scrub import scrub
from agent.watch.digest import SIGNAL_KEYS, ConvoView, render
from agent.watch.host import Host
from agent.watch.state import FMT, State, summarize
from agent.watch.transcript import TurnFacts, turn_facts

# `pool` values that mean improve-jensen should sit this hour out entirely.
_POOL_HOLD = {"hold_scheduled", "empty"}

# A person-started turn counts as failed on any of these outcomes.
_FAILED_OUTCOMES = {"error", "timeout", "max_turns", "interrupted"}

# Values from the spec (see global-constraints.md).
_NO_DELEGATION_CALLS = 40
_BIG_START_TOKENS = 100_000
_BIG_START_MAX_CALLS = 5
_RETENTION_DAYS = 30
_PR_SEARCH_LIMIT = 200

# A single failing `gh` call for one PR (a network hiccup, a malformed response) must not drop the rest.
_PR_FAILURE = (RuntimeError, subprocess.CalledProcessError, OSError, ValueError, KeyError)

# The per-turn/midturn signals the digest's totals section shows, in that order. "pr" is handled separately,
# since it needs its own fallback text when `gh` is unavailable.
_SIGNALS = ("context_first", "context_peak", "inline_code_chars", "lost_background", "still_running",
            "turn_failed", "no_delegation", "big_start_small_ask", "reclone", "tool_errors", "missing_tools",
            "midturn_wait_s")


@dataclass
class MeasureResult:
    has_work: bool
    skip_reason: str | None
    digest: Path | None
    window: tuple[datetime, datetime]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def _parse(value: str) -> datetime:
    return datetime.strptime(value, FMT).replace(tzinfo=UTC)


def _person_started(payload: dict) -> bool:
    return payload.get("source") not in ("schedule", "improve-jensen") and not payload.get("continuation")


def _turn_failed(person_started: bool, item: dict, result: dict) -> int:
    if not person_started:
        return 0
    if result.get("outcome") in _FAILED_OUTCOMES or item.get("status") == "failed":
        return 1
    if not result.get("text") and item.get("delivered_into") is None:
        return 1
    return 0


def _big_start_small_ask(facts: TurnFacts) -> int:
    return int(facts.context_first > _BIG_START_TOKENS and facts.tool_calls <= _BIG_START_MAX_CALLS)


def _no_delegation(facts: TurnFacts) -> int:
    return int(facts.tool_calls >= _NO_DELEGATION_CALLS and facts.subagent_calls == 0)


def _ci_state(checks: subprocess.CompletedProcess) -> str:
    if checks.returncode == 0:
        return "passed"
    output = f"{checks.stdout or ''}{checks.stderr or ''}".lower()
    if "no checks reported" in output:
        return "none"
    # The literal phrase, not a bare "403": a check URL's own job/run id can contain those digits by chance
    # (e.g. .../job/33178403211), and that must still read as a real CI failure, not an unreadable token.
    if "http 403" in output or "resource not accessible" in output:
        return "unknown"
    return "failed"


def _pr_record(host: Host, found: dict, now: datetime, pr_to_conversation: dict[str, str],
               projects: list[dict]) -> dict:
    url = found.get("url")
    repo = (found.get("repository") or {}).get("nameWithOwner", "")

    view = json.loads(host.jensen_gh(
        ["pr", "view", url, "--json", "additions,deletions,createdAt,mergedAt,commits"]).stdout)
    lines = (view.get("additions") or 0) + (view.get("deletions") or 0)
    merged_at = view.get("mergedAt")
    open_to_merge_s = (_parse(merged_at) - _parse(view["createdAt"])).total_seconds() if merged_at else None

    ci = _ci_state(host.jensen_gh(["pr", "checks", url], check=False))

    commits = view.get("commits") or []
    coauthor_trailers = sum(
        1 for commit in commits
        if "co-authored-by: claude" in
        f"{commit.get('messageHeadline', '')}\n{commit.get('messageBody', '')}".lower())

    conversation = pr_to_conversation.get(url)
    if conversation is None:
        for project in projects:
            if project.get("repo") == repo and project.get("status") == "active":
                conversation = project.get("conversation_id")
                break

    return {"ts": _iso(now), "kind": "pr", "url": url, "repo": repo, "lines": lines,
            "open_to_merge_s": open_to_merge_s, "ci": ci, "coauthor_trailers": coauthor_trailers,
            "conversation": conversation}


def _fetch_prs(host: Host, now: datetime, start: datetime, pr_to_conversation: dict[str, str],
               projects: list[dict]) -> tuple[list[dict], str | None]:
    try:
        login = host.jensen_gh(["api", "user", "--jq", ".login"]).stdout.strip()
        search_out = host.jensen_gh(
            ["search", "prs", "--author", login, "--updated", f">={_iso(start)}",
             "--json", "url,repository,number", "--limit", str(_PR_SEARCH_LIMIT)]).stdout
        found = json.loads(search_out or "[]")
    except RuntimeError:
        return [], "Jensen's GitHub token is missing"
    except subprocess.CalledProcessError as exc:
        return [], f"gh exited {exc.returncode}"
    except json.JSONDecodeError:
        return [], "gh returned unparseable output"

    records = []
    for pr in found:
        try:
            records.append(_pr_record(host, pr, now, pr_to_conversation, projects))
        except _PR_FAILURE:
            continue  # one PR's own `gh pr view`/`gh pr checks` failing must not drop the rest
    return records, None


def measure(state: State, host: Host, now: datetime, open_deployed: list[dict]) -> MeasureResult:
    start, end = state.window(now)
    window = (start, end)

    collected = json.loads(host.core_python("core_collect.py", ["--since", _iso(start), "--until", _iso(end)]))
    if collected.get("pool", "normal") in _POOL_HOLD:
        return MeasureResult(has_work=False, skip_reason="pool_hold", digest=None, window=window)

    secrets = host.secret_values()
    # Created-at order, not whatever order core returned them in: a repo cloned by two turns in the same window
    # must count as a reclone for the second one, which only holds if "second" is well defined.
    items = sorted(collected.get("items", []), key=lambda item: item.get("created_at") or "")
    conversations = collected.get("conversations", {})
    projects = collected.get("projects", [])

    turn_records: list[dict] = []
    midturn_records: list[dict] = []
    convo_views: dict[str | None, ConvoView] = {}
    pr_to_conversation: dict[str, str] = {}
    known_clones = set(state.known_clones())
    new_clones: set[str] = set()

    wanted_ids = [item["id"] for item in items
                  if item.get("kind") == "turn" and item.get("delivered_into") is None]
    transcripts: dict[str, bytes] = {}
    transcripts_error: str | None = None
    if wanted_ids:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fetched = host.transcripts(wanted_ids, Path(tmp))
                transcripts = {item_id: path.read_bytes() for item_id, path in fetched.items()}
            except RuntimeError:
                transcripts_error = "Jensen's runner transcripts are unavailable"

    for item in items:
        if item.get("kind") != "turn":
            continue
        created = _parse(item["created_at"])
        updated = _parse(item["updated_at"])
        conversation_id = item.get("conversation_id")

        if item.get("delivered_into") is not None:
            midturn_records.append({"ts": _iso(now), "kind": "midturn", "id": item["id"],
                                    "midturn_wait_s": (updated - created).total_seconds()})
            continue

        payload = item.get("payload") or {}
        result = item.get("result") or {}
        source = payload.get("source")
        person_started = _person_started(payload)

        raw_bytes = transcripts.get(item["id"])
        if raw_bytes is not None:
            # Scrub each raw line *before* turn_facts builds (and truncates to 300/300/500) its tool/error
            # lines from it: a secret that straddles that cut would otherwise leak a prefix too short for any
            # exact-value or pattern match to catch once it's already been cut.
            scrubbed_lines = [scrub(line, secrets) for line in raw_bytes.decode(errors="replace").splitlines()]
            facts = turn_facts(scrubbed_lines)
        else:
            facts = TurnFacts()

        for url in facts.prs_created:
            pr_to_conversation[url] = conversation_id

        reclone = sum(1 for repo in facts.clones if repo in known_clones)
        known_clones.update(facts.clones)
        new_clones.update(facts.clones)

        record = {
            "ts": _iso(now), "kind": "turn", "id": item["id"], "conversation": conversation_id, "source": source,
            "context_first": facts.context_first, "context_peak": facts.context_peak,
            "big_start_small_ask": _big_start_small_ask(facts), "no_delegation": _no_delegation(facts),
            "inline_code_chars": facts.inline_code_chars, "lost_background": facts.lost_background,
            "still_running": facts.still_running, "turn_failed": _turn_failed(person_started, item, result),
            "reclone": reclone, "tool_errors": facts.tool_errors, "missing_tools": facts.missing_tools,
        }
        turn_records.append(record)

        # Belt and suspenders: the lines fed into turn_facts were already scrubbed above, but scrub again here
        # in case a line was skipped (bad JSON) and its text still ended up somewhere -- scrubbing already-safe
        # text is a no-op.
        scrubbed_facts = replace(facts, tool_lines=[scrub(line, secrets) for line in facts.tool_lines],
                                  error_lines=[scrub(line, secrets) for line in facts.error_lines])
        turn_view = {
            "id": item["id"], "source": source, "status": item.get("status"), "outcome": result.get("outcome"),
            "minutes": round((updated - created).total_seconds() / 60, 1),
            "asked": scrub(payload.get("text") or "", secrets), "replied": scrub(result.get("text") or "", secrets),
            "facts": scrubbed_facts, "signals": {key: record[key] for key in SIGNAL_KEYS},
        }
        convo = convo_views.setdefault(
            conversation_id,
            ConvoView(id=conversation_id, project=conversations.get(conversation_id, {}).get("project"), turns=[]))
        convo.turns.append(turn_view)

    pr_records, prs_error = _fetch_prs(host, now, start, pr_to_conversation, projects)

    if not items and not pr_records and not open_deployed:
        return MeasureResult(has_work=False, skip_reason="quiet", digest=None, window=window)

    produced = turn_records + midturn_records + pr_records
    # Staged, not appended: committing this window (merging into metrics.jsonl/known_clones for good) is a
    # separate step, taken only once Claude's own run against this digest succeeds. A re-measure of the same
    # window before that overwrites this same staging rather than piling another copy on top.
    state.stage(produced, new_clones)

    # Totals summarize exactly what this run produced, never a disk read "since start": every record's `ts` is
    # this run's own clock, so a read-since-start would also pick up an earlier run's records stamped right at
    # that same boundary, double-counting a re-measured window.
    totals = {signal: summarize(produced, signal, _iso(start)) for signal in _SIGNALS}
    totals["pr"] = f"PRs: unavailable ({prs_error})" if prs_error else summarize(produced, "pr", _iso(start))
    if transcripts_error:
        totals["transcripts"] = transcripts_error

    deployed = [{"number": issue.get("number"), "title": scrub(issue.get("title") or "", secrets)}
                for issue in open_deployed]

    digest_text = render(window, list(convo_views.values()), totals, pr_records, deployed)
    digest_text = scrub(digest_text, secrets)
    digest_path = state.write_digest(end, digest_text)
    state.prune(now, days=_RETENTION_DAYS)

    return MeasureResult(has_work=True, skip_reason=None, digest=digest_path, window=window)
