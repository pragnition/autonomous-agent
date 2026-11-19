"""`measure`: one hourly pass over core's finished work, Jensen's PRs and Jensen's own notes, boiled down to a
digest.

Pulls the window's finished work items out of core (`core_collect.py`, run inside `agent-core` through
`Host.core_python`), fetches each undelivered turn's own runner transcript to size it up with
`agent.watch.transcript.turn_facts`, finds Jensen's PRs for the window with `gh search prs` and nails down each
one's size, CI result and co-author trail with `gh pr view`/`gh pr checks` (or, when the token can't read checks,
the Actions runs on the PR's head commit, then its commit statuses), reads whatever notes Jensen wrote to
`/var/agent/feedback/` this window (`agent.watch.feedback.collect`, run inside `agent-runner` through
`Host.runner_python`), then stages what `agent.watch.state.State` keeps (`State.stage`) and writes a digest file
for improve-jensen to read next. Every piece of free text that lands in the digest -- a person's ask, Jensen's
reply, a tool or error line, a deployed issue's title, a note Jensen wrote itself -- is scrubbed of secrets
first, since it might end up quoted back out in a public issue; so is every tool input, tool output and error
text *before* `turn_facts` truncates it (its `clean` hook, on the decoded strings), since a secret that straddles
that cut would otherwise leak a prefix no exact-value or pattern match could catch after the fact.

Every record is stamped with when its event happened, not when it was measured: a turn or midturn with its
`updated_at`, a PR with its `mergedAt`, else its `createdAt`. So `count --since <deploy time>` reads only what
happened after a deploy, even from a window that straddled it.

Measuring is staged, not committed: the digest this produces is meant to be acted on before the window is
final, and that action can fail. `State.stage` records this window's metrics and newly-seen clones without
touching the permanent `metrics.jsonl` or `known_clones()` -- only `State.commit()`, called once that action
succeeds, folds them in. So a window re-measured after a failed action (the same items, reprocessed) overwrites
its own prior staging rather than duplicating metrics, and a turn's own clone is never mistaken for a reclone of
itself just because an earlier, abandoned attempt at the same window already staged it as "seen."

Nothing that goes wrong here may stop the hour from reaching Claude, because the hour that most needs Claude is
the one after a deploy broke Jensen -- and only Claude reverts it. So:
- core unreachable (or answering garbage) still gives `has_work`, with a digest that says so, and the window is
  held open (`State.hold_window`) so the turns core couldn't hand over are read once it answers again;
- a newest `deploys.jsonl` record of `check_failed` or `not_deployable` gives `has_work` on an otherwise quiet hour,
  with the record named in the digest;
- `gh` failing outright, one PR's own calls failing in any way, the runner's transcripts being unavailable, or
  one malformed turn are each caught around just the work that depends on them; the rest of the window is still
  measured and written. A caught error's message never reaches the digest or the log -- only its type, since a
  message can carry a host path or worse.
- a note Jensen wrote this window, with nothing else to show for the hour, still gives `has_work`; the runner
  being unreachable when reading notes is caught the same way, by type only, and costs just the notes section.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from agent.scrub import scrub
from agent.watch import feedback
from agent.watch.digest import SIGNAL_KEYS, ConvoView, render
from agent.watch.host import Host
from agent.watch.state import FMT, State, is_improve_jensens_own, summarize
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

# A newest deploy record starting with one of these means Jensen may be broken by the last deploy.
_DEPLOY_FAILURES = ("check_failed", "not_deployable")

_CORE_DOWN = ("Jensen's core is unreachable: this hour's finished turns could not be read, and the window stays "
              "open until core answers again.")

# A head commit's combined status (`gh api repos/<repo>/commits/<sha>/status`), read as a CI result.
_STATUS_CI = {"success": "passed", "pending": "failed", "failure": "failed", "error": "failed"}

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


def _event_time(*values: object, fallback: datetime) -> str:
    """The first of `values` that parses as a timestamp, re-formatted; `fallback` when none does."""
    for value in values:
        if isinstance(value, str):
            try:
                return _iso(_parse(value))
            except ValueError:
                continue
    return _iso(fallback)


def _warn(what: str, exc: BaseException) -> None:
    """One line on stderr naming what failed and the exception's type -- never its message, which can carry a
    host path, a token's error text or a piece of a transcript into the public workflow log."""
    print(f"warning: {what} failed ({type(exc).__name__})", file=sys.stderr)


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


# A completed Actions run with one of these conclusions doesn't fail the commit.
_RUN_OK = {"success", "skipped", "neutral"}


def _ci_from_statuses(host: Host, repo: str, head_sha: str) -> str:
    """CI from the head commit's combined status, for a repo whose CI reports through commit statuses (an outside
    CI service, an app) rather than Actions: `success` gives `passed`, `pending`, `failure` or `error` gives
    `failed`, no statuses at all gives `none`. A refused or failed call, or output that doesn't parse, gives
    `unknown`."""
    result = host.jensen_gh(["api", f"repos/{repo}/commits/{head_sha}/status"], check=False)
    if result.returncode != 0:
        return "unknown"
    try:
        data = json.loads(result.stdout or "{}")
        if not data.get("statuses"):
            return "none"
        return _STATUS_CI.get(data.get("state"), "unknown")
    except (ValueError, AttributeError, TypeError):
        return "unknown"


def _ci_from_runs(host: Host, repo: str, head_sha: str | None) -> str:
    """CI from the Actions runs on the PR's head commit, for when `gh pr checks` is forbidden: a fine-grained token
    has no "Checks" permission, but Actions: read still lists the runs. No runs falls back to the commit's
    statuses (`_ci_from_statuses`), which give `none` when there are none either; a run not yet completed gives
    `failed`, the same as `gh pr checks`' pending; all completed with a conclusion in `_RUN_OK` gives `passed`,
    anything else `failed`. A missing head sha, a refused or failed call, or output that doesn't parse gives
    `unknown`, never a guess."""
    if not head_sha:
        return "unknown"
    result = host.jensen_gh(["api", f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"], check=False)
    if result.returncode != 0:
        return "unknown"
    try:
        runs = json.loads(result.stdout or "{}").get("workflow_runs") or []
        if runs:
            if any(run.get("status") != "completed" for run in runs):
                return "failed"
            return "passed" if all(run.get("conclusion") in _RUN_OK for run in runs) else "failed"
    except (ValueError, AttributeError, TypeError):
        return "unknown"
    return _ci_from_statuses(host, repo, head_sha)


def _pr_record(host: Host, found: dict, now: datetime, pr_to_conversation: dict[str, str],
               projects: list[dict]) -> dict:
    url = found.get("url")
    repo = (found.get("repository") or {}).get("nameWithOwner", "")

    view = json.loads(host.jensen_gh(
        ["pr", "view", url, "--json", "additions,deletions,createdAt,mergedAt,commits,headRefOid"]).stdout)
    lines = (view.get("additions") or 0) + (view.get("deletions") or 0)
    created_at, merged_at = view.get("createdAt"), view.get("mergedAt")
    open_to_merge_s = (_parse(merged_at) - _parse(created_at)).total_seconds() if merged_at and created_at else None

    ci = _ci_state(host.jensen_gh(["pr", "checks", url], check=False))
    if ci == "unknown":
        ci = _ci_from_runs(host, repo, view.get("headRefOid"))

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

    return {"ts": _event_time(merged_at, created_at, fallback=now), "kind": "pr", "url": url, "repo": repo,
            "lines": lines, "open_to_merge_s": open_to_merge_s, "ci": ci, "coauthor_trailers": coauthor_trailers,
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
    except Exception as exc:
        _warn("searching Jensen's PRs", exc)
        return [], f"gh failed ({type(exc).__name__})"
    if not isinstance(found, list):
        return [], "gh returned unparseable output"

    records = []
    for pr in found:
        try:
            records.append(_pr_record(host, pr, now, pr_to_conversation, projects))
        except Exception as exc:  # one PR failing in any way (a gh hiccup, a null field) must not drop the rest
            _warn("one PR's record", exc)
    return records, None


def _collect(host: Host, start: datetime, end: datetime) -> dict | None:
    """core's snapshot of the window (`core_collect.py`), or `None` when core can't be read: the container is
    down, `docker exec` fails, or what comes back isn't a JSON object."""
    try:
        collected = json.loads(host.core_python("core_collect.py", ["--since", _iso(start), "--until", _iso(end)]))
    except Exception as exc:
        _warn("reading core's work queue", exc)
        return None
    return collected if isinstance(collected, dict) else None


def _collect_notes(host: Host, start: datetime, end: datetime, secrets: list[str]) -> list[dict]:
    """This window's Jensen notes (`feedback.collect`), each scrubbed the same way a tool line is -- a note can
    carry a secret or a product name just as easily as a transcript can. The runner being unreachable, or
    anything else going wrong reading notes, is caught and logged by type only, the same as every other
    optional source here: a note improve-jensen can't read yet must never cost the rest of the hour."""
    try:
        notes = feedback.collect(host, start, end)
    except Exception as exc:
        _warn("reading Jensen's notes", exc)
        return []
    return [{**note, "text": scrub(note.get("text") or "", secrets)} for note in notes]


def _fetch_transcripts(host: Host, ids: list[str]) -> tuple[dict[str, bytes], str | None]:
    """Each id's runner transcript as raw bytes, plus a host-path-free reason when none could be read at all."""
    if not ids:
        return {}, None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fetched = host.transcripts(ids, Path(tmp))
            return {item_id: path.read_bytes() for item_id, path in fetched.items()}, None
        except Exception as exc:
            _warn("reading the runner's transcripts", exc)
            return {}, "Jensen's runner transcripts are unavailable"


def _facts(raw_bytes: bytes | None, secrets: list[str]) -> TurnFacts:
    if raw_bytes is None:
        return TurnFacts()
    # Scrub *before* turn_facts truncates its tool/error lines to 300/300/500: a secret that straddles that cut would
    # otherwise leak a prefix too short for any exact-value or pattern match to catch once it's already been cut.
    # The decoded strings are what matter (`clean`): a secret holding a `"` or `\` is escaped in the raw JSON line,
    # where an exact match can't see it. The raw lines are scrubbed too, which costs nothing.
    return turn_facts([scrub(line, secrets) for line in raw_bytes.decode(errors="replace").splitlines()],
                      clean=lambda text: scrub(text, secrets))


def _deploy_alert(state: State) -> str | None:
    """A "Needs attention" line when the newest `deploys.jsonl` record is a failed or impossible deploy."""
    last = state.last_deploy()
    result = last.get("result") if last else None
    if not isinstance(result, str) or not result.startswith(_DEPLOY_FAILURES):
        return None
    return (f"The newest deploy record is `{result}` (from {last.get('from')} to {last.get('to')}, at "
            f"{last.get('ts')}): Jensen may be broken. The record is the last line of deploys.jsonl in the state "
            f"folder.")


def _midturn_record(item: dict) -> dict:
    created, updated = _parse(item["created_at"]), _parse(item["updated_at"])
    return {"ts": _iso(updated), "kind": "midturn", "id": item["id"], "conversation": item.get("conversation_id"),
            "source": (item.get("payload") or {}).get("source"),
            "midturn_wait_s": (updated - created).total_seconds()}


def _turn_record_and_view(item: dict, facts: TurnFacts, known_clones: set[str],
                          secrets: list[str]) -> tuple[dict, dict]:
    created, updated = _parse(item["created_at"]), _parse(item["updated_at"])
    payload = item.get("payload") or {}
    result = item.get("result") or {}
    source = payload.get("source")
    record = {
        "ts": _iso(updated), "kind": "turn", "id": item["id"], "conversation": item.get("conversation_id"),
        "source": source, "context_first": facts.context_first, "context_peak": facts.context_peak,
        "big_start_small_ask": _big_start_small_ask(facts), "no_delegation": _no_delegation(facts),
        "inline_code_chars": facts.inline_code_chars, "lost_background": facts.lost_background,
        "still_running": facts.still_running, "turn_failed": _turn_failed(_person_started(payload), item, result),
        "reclone": sum(1 for repo in facts.clones if repo in known_clones), "tool_errors": facts.tool_errors,
        "missing_tools": facts.missing_tools,
    }
    # Belt and suspenders: the lines fed into turn_facts were already scrubbed, but scrub again here in case a line
    # was skipped (bad JSON) and its text still ended up somewhere -- scrubbing already-safe text is a no-op.
    scrubbed_facts = replace(facts, tool_lines=[scrub(line, secrets) for line in facts.tool_lines],
                              error_lines=[scrub(line, secrets) for line in facts.error_lines])
    view = {
        "id": item["id"], "source": source, "status": item.get("status"), "outcome": result.get("outcome"),
        "minutes": round((updated - created).total_seconds() / 60, 1),
        "asked": scrub(payload.get("text") or "", secrets), "replied": scrub(result.get("text") or "", secrets),
        "facts": scrubbed_facts, "signals": {key: record[key] for key in SIGNAL_KEYS},
        "own": is_improve_jensens_own(record),
    }
    return record, view


def measure(state: State, host: Host, now: datetime, open_deployed: list[dict]) -> MeasureResult:
    start, end = state.window(now)
    window = (start, end)

    collected = _collect(host, start, end)
    core_down = collected is None
    if core_down:
        collected = {}
        state.hold_window()
    elif collected.get("pool", "normal") in _POOL_HOLD:
        return MeasureResult(has_work=False, skip_reason="pool_hold", digest=None, window=window)

    secrets = host.secret_values()
    # Created-at order, not whatever order core returned them in: a repo cloned by two turns in the same window
    # must count as a reclone for the second one, which only holds if "second" is well defined.
    items = sorted((item for item in collected.get("items") or [] if isinstance(item, dict)),
                   key=lambda item: str(item.get("created_at") or ""))
    conversations = collected.get("conversations") or {}
    projects = collected.get("projects") or []
    running = [turn for turn in collected.get("running") or [] if isinstance(turn, dict) and turn.get("id")]

    turn_records: list[dict] = []
    midturn_records: list[dict] = []
    convo_views: dict[str | None, ConvoView] = {}
    pr_to_conversation: dict[str, str] = {}
    known_clones = set(state.known_clones())
    new_clones: set[str] = set()
    skipped = 0

    finished_ids = [item.get("id") for item in items
                    if item.get("kind") == "turn" and item.get("delivered_into") is None]
    wanted_ids = list(dict.fromkeys(i for i in finished_ids + [turn["id"] for turn in running] if i))
    transcripts, transcripts_error = _fetch_transcripts(host, wanted_ids)

    # A turn still running at the window's end may already have opened a PR this window's search finds; read its
    # transcript so far (a half-written last line is skipped), so that PR names the conversation that made it now,
    # rather than none -- by the time the turn finishes, the PR has already been planned for review.
    for turn in running:
        try:
            prs_created = _facts(transcripts.get(turn["id"]), secrets).prs_created
        except Exception as exc:  # the same guard a finished turn gets below: one bad transcript costs only itself
            _warn("reading a running turn's transcript", exc)
            continue
        for url in prs_created:
            pr_to_conversation[url] = turn.get("conversation_id")

    for item in items:
        if item.get("kind") != "turn":
            continue
        try:
            if item.get("delivered_into") is not None:
                midturn_records.append(_midturn_record(item))
                continue
            facts = _facts(transcripts.get(item["id"]), secrets)
            record, view = _turn_record_and_view(item, facts, known_clones, secrets)
        except Exception as exc:  # one malformed item must not cost the rest of the hour
            _warn("measuring one turn", exc)
            skipped += 1
            continue

        conversation_id = item.get("conversation_id")
        for url in facts.prs_created:
            pr_to_conversation[url] = conversation_id
        known_clones.update(facts.clones)
        new_clones.update(facts.clones)
        turn_records.append(record)
        convo = convo_views.setdefault(
            conversation_id,
            ConvoView(id=conversation_id, project=(conversations.get(conversation_id) or {}).get("project"),
                      turns=[]))
        convo.turns.append(view)

    pr_records, prs_error = _fetch_prs(host, now, start, pr_to_conversation, projects)
    deploy_alert = _deploy_alert(state)
    notes = _collect_notes(host, start, end, secrets)

    if (not items and not pr_records and not open_deployed and not deploy_alert and not core_down
            and not notes):
        return MeasureResult(has_work=False, skip_reason="quiet", digest=None, window=window)

    # Claude gets a new hour: the last hour's plan is spent. Nothing else consumes plan.json, so a run that
    # succeeds without writing one would otherwise have `plan` dispatch last hour's fix and reviews again.
    state.clear_plan()

    produced = turn_records + midturn_records + pr_records
    # Staged, not appended: committing this window (merging into metrics.jsonl/known_clones for good) is a
    # separate step, taken only once Claude's own run against this digest succeeds. A re-measure of the same
    # window before that overwrites this same staging rather than piling another copy on top.
    state.stage(produced, new_clones)

    # Totals summarize exactly what this run produced, never a disk read "since start": a PR committed by an earlier
    # window keeps the ts of its own creation or merge, which can fall inside this window too, and this window's
    # own PRs would then be counted next to their earlier records.
    totals = {signal: summarize(produced, signal, _iso(start)) for signal in _SIGNALS}
    totals["pr"] = f"PRs: unavailable ({prs_error})" if prs_error else summarize(produced, "pr", _iso(start))
    if transcripts_error:
        totals["transcripts"] = transcripts_error
    if skipped:
        totals["skipped"] = f"turns that could not be measured: {skipped}"

    alerts = ([_CORE_DOWN] if core_down else []) + ([deploy_alert] if deploy_alert else [])
    deployed = [{"number": issue.get("number"), "title": scrub(issue.get("title") or "", secrets)}
                for issue in open_deployed]

    digest_text = render(window, list(convo_views.values()), totals, pr_records, deployed, notes=notes,
                         alerts=alerts)
    digest_text = scrub(digest_text, secrets)
    digest_path = state.write_digest(end, digest_text)
    state.prune(now, days=_RETENTION_DAYS)

    return MeasureResult(has_work=True, skip_reason=None, digest=digest_path, window=window)
