import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.watch.digest import SIGNAL_KEYS, ConvoView, render
from agent.watch.measure import measure
from agent.watch.state import State
from agent.watch.transcript import TurnFacts

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _line(**obj) -> str:
    return json.dumps(obj)


def _assistant(content, parent_tool_use_id=None) -> str:
    return _line(type="AssistantMessage", parent_tool_use_id=parent_tool_use_id, usage={}, content=content)


def _result_line(tool_use_id, content, is_error=None) -> str:
    return _line(type="UserMessage", parent_tool_use_id=None,
                 content=[{"tool_use_id": tool_use_id, "content": content, "is_error": is_error}])


def _bash_calls(n: int) -> list[str]:
    """`n` Bash tool-call/result pairs, enough to trip `no_delegation` at 40+ with no subagent calls."""
    lines = []
    for i in range(n):
        tool_id = f"t{i}"
        lines.append(_assistant([{"id": tool_id, "name": "Bash", "input": {"command": f"echo {i}"}}]))
        lines.append(_result_line(tool_id, f"out {i}"))
    return lines


def _item(item_id, *, conversation_id, status="done", created_at=None, updated_at=None, payload=None,
          result=None, delivered_into=None, kind="turn") -> dict:
    return {
        "id": item_id, "conversation_id": conversation_id, "kind": kind, "status": status,
        "created_at": created_at or "2026-11-19T09:00:00Z", "updated_at": updated_at or "2026-11-19T09:01:00Z",
        "delivered_into": delivered_into, "payload": payload or {}, "result": result or {},
    }


class FakeHost:
    """Stands in for `agent.watch.host.Host`: serves canned `core_collect` JSON, canned transcripts and a small
    `gh` dispatch table, recording every call for tests to inspect."""

    def __init__(self, *, collect, transcripts=None, login=None, search=None, views=None, checks=None,
                 secrets=None, gh_error=None, transcripts_error=None):
        self.collect = collect
        self._transcripts = transcripts or {}
        self._login = login
        self._search = search
        self._views = views or {}
        self._checks = checks or {}
        self._secrets = secrets or []
        self._gh_error = gh_error
        self._transcripts_error = transcripts_error
        self.gh_calls: list[list[str]] = []
        self.transcripts_calls: list[list[str]] = []
        self.core_python_calls: list[tuple] = []

    def core_python(self, module_file, args):
        self.core_python_calls.append((module_file, tuple(args)))
        return json.dumps(self.collect)

    def transcripts(self, ids, dest):
        self.transcripts_calls.append(list(ids))
        if self._transcripts_error:
            raise self._transcripts_error
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = {}
        for item_id in ids:
            if item_id in self._transcripts:
                path = dest / f"{item_id}.jsonl"
                path.write_text("\n".join(self._transcripts[item_id]))
                out[item_id] = path
        return out

    def jensen_gh(self, args, *, check=True, capture=True):
        self.gh_calls.append(list(args))
        if self._gh_error:
            raise self._gh_error
        if args[:2] == ["api", "user"]:
            return self._login
        if args[:2] == ["search", "prs"]:
            return self._search
        if args[:2] == ["pr", "view"]:
            return self._views[args[2]]
        if args[:2] == ["pr", "checks"]:
            return self._checks[args[2]]
        raise AssertionError(f"unexpected gh call: {args}")

    def secret_values(self):
        return self._secrets


_NO_PRS = {"login": _cp(stdout="jensen-bot\n"), "search": _cp(stdout="[]")}


def _since(dt: datetime) -> datetime:
    return dt - timedelta(days=1)


# -- skip rules --


def test_quiet_hour_writes_nothing(tmp_path):
    state = State(tmp_path)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is False
    assert result.skip_reason == "quiet"
    assert result.digest is None
    assert not list(tmp_path.glob("digest-*.md"))
    assert not (tmp_path / "metrics.jsonl").exists()


def test_pool_hold_skips_without_consuming_window(tmp_path):
    state = State(tmp_path)
    baseline = T0
    state.window(baseline)
    state.commit()  # a prior, already-committed hour

    hold_now = baseline + timedelta(hours=1)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "hold_scheduled",
                             "claimed": 0})

    result = measure(state, host, hold_now, open_deployed=[])

    assert result.has_work is False
    assert result.skip_reason == "pool_hold"
    assert result.digest is None
    assert result.window == (baseline, hold_now)
    assert not list(tmp_path.glob("digest-*.md"))

    # The skip never committed: the next window still starts from the prior committed end, not from hold_now.
    start, _ = state.window(hold_now + timedelta(hours=1))
    assert start == baseline


# -- turn records and signals --


def test_turn_records_and_signals(tmp_path):
    state = State(tmp_path)
    failed_item = _item("W-a", conversation_id="C-a",
                        created_at="2026-11-19T09:00:00Z", updated_at="2026-11-19T09:05:00Z",
                        result={"text": "", "outcome": "timeout", "error": "timed out"})
    scheduled_item = _item("W-b", conversation_id="C-b",
                           created_at="2026-11-19T09:10:00Z", updated_at="2026-11-19T09:11:00Z",
                           payload={"source": "schedule"}, result={"text": "", "outcome": "done"})

    host = FakeHost(
        collect={"items": [failed_item, scheduled_item],
                 "conversations": {"C-a": {"channel": "c", "thread_ts": "1", "project": "sample"},
                                   "C-b": {"channel": "c", "thread_ts": "2", "project": "sample"}},
                 "projects": [], "pool": "normal", "claimed": 0},
        transcripts={"W-a": _bash_calls(45), "W-b": []},
        **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    records = {r["id"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"}

    assert records["W-a"]["turn_failed"] == 1
    assert records["W-a"]["no_delegation"] == 1
    assert records["W-a"]["conversation"] == "C-a"

    assert records["W-b"]["turn_failed"] == 0


def test_turn_failed_isolated_cases(tmp_path):
    state = State(tmp_path)
    # status="failed" wins outright, even though the result carries real text.
    failed_status_with_text = _item("W-1", conversation_id="C-1", status="failed",
                                    created_at="2026-11-19T09:00:00Z", updated_at="2026-11-19T09:01:00Z",
                                    result={"text": "some text", "outcome": "done"})
    # Empty text is still a failure signal even when the outcome itself says "done".
    empty_text_done_outcome = _item("W-2", conversation_id="C-2", status="done",
                                    created_at="2026-11-19T09:00:00Z", updated_at="2026-11-19T09:01:00Z",
                                    result={"text": "", "outcome": "done"})
    # Not person-started (it's a continuation), so a failing outcome doesn't count against it.
    continuation_timeout = _item("W-3", conversation_id="C-3", status="done",
                                 created_at="2026-11-19T09:00:00Z", updated_at="2026-11-19T09:01:00Z",
                                 payload={"continuation": 1}, result={"text": "", "outcome": "timeout"})

    host = FakeHost(
        collect={"items": [failed_status_with_text, empty_text_done_outcome, continuation_timeout],
                 "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
        **_NO_PRS)

    measure(state, host, T0, open_deployed=[])

    records = {r["id"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"}
    assert records["W-1"]["turn_failed"] == 1
    assert records["W-2"]["turn_failed"] == 1
    assert records["W-3"]["turn_failed"] == 0


def test_big_start_small_ask_signal(tmp_path):
    state = State(tmp_path)
    # A single huge first call (over the 100k-token threshold), then a result, with no further tool calls.
    big_call = _line(type="AssistantMessage", parent_tool_use_id=None,
                     usage={"input_tokens": 150000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                     content=[{"id": "t1", "name": "Read", "input": {"file_path": "/a"}}])
    item = _item("W-big", conversation_id="C-big")

    host = FakeHost(
        collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
        transcripts={"W-big": [big_call, _result_line("t1", "contents")]},
        **_NO_PRS)

    measure(state, host, T0, open_deployed=[])

    record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-big")
    assert record["big_start_small_ask"] == 1
    assert record["no_delegation"] == 0


def test_midturn_wait_seconds(tmp_path):
    state = State(tmp_path)
    item = _item("W-mid", conversation_id="C-1", created_at="2026-11-19T10:00:00Z",
                 updated_at="2026-11-19T10:04:30Z", delivered_into="W-parent")

    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    midturn = next(r for r in state.read_metrics(_since(T0)) if r["kind"] == "midturn")
    assert midturn["id"] == "W-mid"
    assert midturn["midturn_wait_s"] == 270

    # A delivered item produces only the midturn record, never a turn record.
    assert not [r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"]


# -- PR records --


def test_pr_records_ci_states(tmp_path):
    state = State(tmp_path)

    created_item = _item("W-creator", conversation_id="C-a")
    pr1_created = _line(type="SystemMessage", subtype="code_change_published",
                        data={"url": "https://github.com/org/repo1/pull/1", "action": "created"})

    search_results = [
        {"url": "https://github.com/org/repo1/pull/1", "repository": {"nameWithOwner": "org/repo1"}, "number": 1},
        {"url": "https://github.com/org/repo2/pull/2", "repository": {"nameWithOwner": "org/repo2"}, "number": 2},
        {"url": "https://github.com/org/repo3/pull/3", "repository": {"nameWithOwner": "org/repo3"}, "number": 3},
        {"url": "https://github.com/org/repo4/pull/4", "repository": {"nameWithOwner": "org/repo4"}, "number": 4},
        {"url": "https://github.com/org/repo5/pull/5", "repository": {"nameWithOwner": "org/repo5"}, "number": 5},
    ]

    def _view(url, commits=()):
        return _cp(stdout=json.dumps({"additions": 10, "deletions": 5, "createdAt": "2026-11-19T09:00:00Z",
                                      "mergedAt": "2026-11-19T09:10:00Z", "commits": list(commits)}))

    views = {pr["url"]: _view(pr["url"]) for pr in search_results}
    # repo5's PR carries a co-author trailer, in GitHub's own casing, and specific additions/deletions/dates so
    # `lines`/`open_to_merge_s`/`coauthor_trailers` can all be checked precisely.
    views["https://github.com/org/repo5/pull/5"] = _cp(stdout=json.dumps({
        "additions": 25, "deletions": 15, "createdAt": "2026-11-19T09:00:00Z", "mergedAt": "2026-11-19T09:16:40Z",
        "commits": [{"messageHeadline": "fix: bug", "messageBody": "Co-authored-by: Claude <noreply@x>"}],
    }))
    checks = {
        "https://github.com/org/repo1/pull/1": _cp(returncode=0),
        "https://github.com/org/repo2/pull/2": _cp(returncode=8, stdout="no checks reported on the 'main' ref\n"),
        "https://github.com/org/repo3/pull/3": _cp(returncode=1,
                                                    stderr="HTTP 403: Resource not accessible by integration"),
        "https://github.com/org/repo4/pull/4": _cp(returncode=1, stdout="some other check failure"),
        # A check URL's own job/run id can contain the digits "403" by pure chance; that must not be read as a
        # "resource not accessible" 403 and downgraded to "unknown" -- it's a real, readable check failure.
        "https://github.com/org/repo5/pull/5": _cp(
            returncode=1,
            stdout="X 1 failing check\nSee https://github.com/org/repo5/actions/runs/33178403211/job/92210403211\n"),
    }

    host = FakeHost(
        collect={"items": [created_item],
                 "conversations": {"C-a": {"channel": "c", "thread_ts": "1", "project": "sample"}},
                 "projects": [{"name": "proj2", "repo": "org/repo2", "conversation_id": "C-proj2",
                              "status": "active"}],
                 "pool": "normal", "claimed": 0},
        transcripts={"W-creator": [pr1_created]},
        login=_cp(stdout="jensen-bot\n"), search=_cp(stdout=json.dumps(search_results)),
        views=views, checks=checks)

    measure(state, host, T0, open_deployed=[])

    # `gh search prs` is capped, per the minor fix.
    search_call = next(c for c in host.gh_calls if c[:2] == ["search", "prs"])
    assert "--limit" in search_call and "200" in search_call

    prs = {r["url"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"}
    assert prs["https://github.com/org/repo1/pull/1"]["ci"] == "passed"
    assert prs["https://github.com/org/repo2/pull/2"]["ci"] == "none"
    assert prs["https://github.com/org/repo3/pull/3"]["ci"] == "unknown"
    assert prs["https://github.com/org/repo4/pull/4"]["ci"] == "failed"
    assert prs["https://github.com/org/repo5/pull/5"]["ci"] == "failed"  # not "unknown": 403 here is just an id

    # The transcript that created it wins over the project fallback.
    assert prs["https://github.com/org/repo1/pull/1"]["conversation"] == "C-a"
    # No transcript match: falls back to the active project's conversation for that repo.
    assert prs["https://github.com/org/repo2/pull/2"]["conversation"] == "C-proj2"
    # Neither a transcript match nor an active project: no conversation.
    assert prs["https://github.com/org/repo3/pull/3"]["conversation"] is None
    assert prs["https://github.com/org/repo4/pull/4"]["conversation"] is None

    assert prs["https://github.com/org/repo5/pull/5"]["lines"] == 40  # 25 additions + 15 deletions
    assert prs["https://github.com/org/repo5/pull/5"]["open_to_merge_s"] == 1000.0  # 09:00:00 -> 09:16:40
    assert prs["https://github.com/org/repo5/pull/5"]["coauthor_trailers"] == 1  # GitHub's own casing
    assert prs["https://github.com/org/repo1/pull/1"]["coauthor_trailers"] == 0  # no commits at all


def test_pr_fetch_failure_skips_prs_without_crashing(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    gh_error=RuntimeError("GH_TOKEN missing from /live/data/secrets/agent.env"))

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    assert not [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    text = result.digest.read_text()
    # A generic, host-path-free reason -- never the raw RuntimeError message, which names the agent.env path.
    assert "PRs: unavailable (Jensen's GitHub token is missing)" in text
    assert "/live/data/secrets/agent.env" not in text


def test_pr_fetch_gh_failure_skips_prs_without_crashing(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    gh_error=subprocess.CalledProcessError(2, ["gh", "api", "user"]))

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    assert not [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    assert "PRs: unavailable (gh exited 2)" in result.digest.read_text()


def test_one_failing_pr_view_does_not_drop_other_pr_records(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    search_results = [
        {"url": "https://github.com/org/repo1/pull/1", "repository": {"nameWithOwner": "org/repo1"}, "number": 1},
        {"url": "https://github.com/org/repo2/pull/2", "repository": {"nameWithOwner": "org/repo2"}, "number": 2},
    ]
    # pull/2 has no entry in `views` at all: FakeHost's dispatch raises KeyError for it, standing in for a real
    # `gh pr view` hiccup (a network error, a malformed response) on just that one PR.
    views = {"https://github.com/org/repo1/pull/1": _cp(stdout=json.dumps(
        {"additions": 1, "deletions": 1, "createdAt": "2026-11-19T09:00:00Z", "mergedAt": "2026-11-19T09:05:00Z",
         "commits": []}))}
    checks = {"https://github.com/org/repo1/pull/1": _cp(returncode=0)}

    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    login=_cp(stdout="jensen-bot\n"), search=_cp(stdout=json.dumps(search_results)),
                    views=views, checks=checks)

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    prs = [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    assert {r["url"] for r in prs} == {"https://github.com/org/repo1/pull/1"}


# -- reclone --


def test_reclone_counts_repos_seen_in_earlier_windows(tmp_path):
    state = State(tmp_path)
    clone_lines = [_assistant([{"id": "t1", "name": "Bash", "input": {"command": "gh repo clone org/repo"}}])]

    item1 = _item("W-first", conversation_id="C-1")
    host1 = FakeHost(collect={"items": [item1], "conversations": {}, "projects": [], "pool": "normal",
                              "claimed": 0},
                     transcripts={"W-first": clone_lines}, **_NO_PRS)
    measure(state, host1, T0, open_deployed=[])
    state.commit()  # the first window's Claude run succeeds: its staged clones become permanently known

    first_record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-first")
    assert first_record["reclone"] == 0
    assert state.known_clones() == {"org/repo"}

    item2 = _item("W-second", conversation_id="C-2")
    host2 = FakeHost(collect={"items": [item2], "conversations": {}, "projects": [], "pool": "normal",
                              "claimed": 0},
                     transcripts={"W-second": clone_lines}, **_NO_PRS)
    measure(state, host2, T0 + timedelta(hours=1), open_deployed=[])

    second_record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-second")
    assert second_record["reclone"] == 1


# -- re-measuring the same window (stage/commit) --


def test_remeasure_same_window_has_no_duplicates_or_self_reclone(tmp_path):
    # The watch only commits a window after Claude's own run against its digest succeeds, so a failed run means
    # the same window gets measured again. That must not duplicate the turn's metrics line, and must not count
    # the turn's own clone as a reclone of itself just because the first (abandoned) attempt already staged it.
    state = State(tmp_path)
    clone_lines = [_assistant([{"id": "t1", "name": "Bash", "input": {"command": "gh repo clone org/repo"}}])]
    item = _item("W-a", conversation_id="C-1")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    transcripts={"W-a": clone_lines}, **_NO_PRS)

    first = measure(state, host, T0, open_deployed=[])
    assert first.has_work is True
    first_record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-a")
    assert first_record["reclone"] == 0
    assert state.known_clones() == set()  # staged, not yet committed

    # Claude's run against the first digest fails; nothing was committed. The same window is measured again.
    second = measure(state, host, T0, open_deployed=[])
    assert second.has_work is True

    turn_records = [r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn" and r["id"] == "W-a"]
    assert len(turn_records) == 1  # overwritten by stage(), not duplicated
    assert turn_records[0]["reclone"] == 0  # still not its own reclone

    # Now the second attempt succeeds and gets committed: the staged data becomes permanent.
    state.commit()
    assert state.known_clones() == {"org/repo"}
    assert len([r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn" and r["id"] == "W-a"]) == 1


# -- transcripts unavailable --


def test_transcripts_failure_still_writes_turn_record_from_db_data(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a", result={"text": "ok", "outcome": "done"})
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    transcripts_error=RuntimeError("docker exec agent-runner returned 1: no such container"),
                    **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-a")
    assert record["context_first"] == 0 and record["context_peak"] == 0
    assert record["inline_code_chars"] == 0
    assert record["tool_errors"] == [] and record["missing_tools"] == []
    assert record["turn_failed"] == 0  # status/outcome/text from the DB alone say this turn succeeded
    text = result.digest.read_text()
    assert "Jensen's runner transcripts are unavailable" in text
    assert "no such container" not in text  # the raw RuntimeError message never reaches the digest


# -- digest redaction (Review Focus 2) --


def test_digest_redacts_secrets(tmp_path):
    state = State(tmp_path)
    leaking_lines = [
        _assistant([{"id": "t1", "name": "Bash", "input": {"command": "env | grep TOKEN"}}]),
        # The exact agent.env value (caught by secret_values()) and a regex-pattern secret, both in tool output.
        _result_line("t1", "GH_TOKEN=supersecretvalue SLACK_TOKEN=xoxb-123456789012-abcdefghij", is_error=True),
    ]
    item = _item("W-a", conversation_id="C-a", payload={"text": "the value is supersecretvalue, please use it"},
                result={"text": "ack: supersecretvalue noted"})

    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    transcripts={"W-a": leaking_lines}, secrets=["supersecretvalue"], **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    text = result.digest.read_text()
    assert "xoxb-123456789012-abcdefghij" not in text
    assert "supersecretvalue" not in text
    assert "[redacted]" in text


def test_digest_never_leaks_a_secret_prefix_across_the_truncation_boundary(tmp_path):
    # turn_facts truncates a tool call's output to 300 characters when it builds the tool line. A secret that
    # starts at position 296 of the raw output would, if scrubbing ran only after that cut, show up as just its
    # first 4 characters -- too short to match the exact value or any pattern. Scrubbing the raw line first
    # (before turn_facts ever sees it) must mean not even that short a prefix survives.
    state = State(tmp_path)
    secret = "supersecretvalue"
    padded_output = ("y" * 296) + secret + " and more trailing text"
    leaking_lines = [
        _assistant([{"id": "t1", "name": "Bash", "input": {"command": "env"}}]),
        _result_line("t1", padded_output, is_error=True),
    ]
    item = _item("W-a", conversation_id="C-a")

    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    transcripts={"W-a": leaking_lines}, secrets=[secret], **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[])

    text = result.digest.read_text()
    assert secret not in text
    assert secret[:4] not in text  # not even a short prefix of the secret survives the 300-char cut


# -- digest totals and deployed issues --


def test_digest_lists_deployed_issues_and_totals(tmp_path):
    state = State(tmp_path)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0, open_deployed=[{"number": 7, "title": "Fix the thing"},
                                                      {"number": 9, "title": "Another issue"}])

    assert result.has_work is True
    text = result.digest.read_text()
    assert "#7 Fix the thing" in text
    assert "#9 Another issue" in text
    for signal in ("context_first", "context_peak", "inline_code_chars", "lost_background", "still_running",
                   "turn_failed", "no_delegation", "big_start_small_ask", "reclone", "tool_errors",
                   "missing_tools", "midturn_wait_s", "pr"):
        assert f"{signal} since" in text


def test_digest_names_the_conversation_behind_each_new_pr():
    # The watch copies this into plan.json, so a review's findings reach the session that made the PR.
    window = (T0, T0 + timedelta(hours=1))
    prs = [{"url": "https://github.com/o/r/pull/1", "repo": "o/r", "lines": 10, "ci": "passed",
            "open_to_merge_s": 60.0, "conversation": "C-made-it"},
           {"url": "https://github.com/o/r/pull/2", "repo": "o/r", "lines": 5, "ci": "none",
            "open_to_merge_s": None, "conversation": None}]

    text = render(window, [], {}, prs, [])

    assert ("- https://github.com/o/r/pull/1 (o/r): 10 lines, ci=passed, open_to_merge_s=60.0, "
            "conversation=C-made-it") in text
    assert ("- https://github.com/o/r/pull/2 (o/r): 5 lines, ci=none, open_to_merge_s=None, "
            "conversation=none") in text


# -- digest budget shrinking (Review Focus 3) --


def test_first_render_attempt_never_recuts_a_turn_facts_line():
    # A tool call's own ~300-char input, by itself, must not make render()'s *first* attempt re-cut the whole
    # "name: input -> output" line down to a flat 300 characters -- that would lose the output half entirely.
    # The first attempt must show the line exactly as turn_facts built it (up to 300 + 300, or 500 for an error).
    window = (T0, T0 + timedelta(hours=1))
    marker = "OUTPUT_MARKER_TEXT_THAT_MUST_SURVIVE_IN_FULL"
    tool_line = f"Bash: {'x' * 280} -> {marker}"
    error_line = ("e" * 480) + marker
    facts = TurnFacts(tool_lines=[tool_line], error_lines=[error_line])
    turn = {"id": "W-a", "source": None, "status": "done", "outcome": "done", "minutes": 1.0, "asked": "",
            "replied": "", "facts": facts, "signals": {key: 0 for key in SIGNAL_KEYS} | {"tool_errors": [],
                                                                                          "missing_tools": []}}
    convos = [ConvoView(id="C-a", project=None, turns=[turn])]

    text = render(window, convos, {}, [], [])

    assert tool_line in text
    assert error_line in text
    assert marker in text


def test_digest_shrinks_tool_lines_not_messages():
    window = (T0, T0 + timedelta(hours=1))
    big_ask = "a" * 200_000
    convos = []
    base_signals = {key: 0 for key in SIGNAL_KEYS}
    base_signals.update(tool_errors=[], missing_tools=[])
    long_line = "x" * 5000

    for i in range(300):
        # Deliberately much larger than a real transcript's own 300/300/500 caps would ever let through, so
        # that even the widest tool-line cap (120) still can't fit the budget -- only cap=0 (omitted entirely)
        # does, proving the lines actually got cut rather than this passing by construction either way.
        facts = TurnFacts(tool_lines=[long_line] * 80, error_lines=["e" * 2000] * 3)
        asked = big_ask if i == 0 else f"ask {i}"
        turn = {"id": f"W-{i}", "source": None, "status": "done", "outcome": "done", "minutes": 1.0,
                "asked": asked, "replied": f"reply {i}", "facts": facts, "signals": dict(base_signals)}
        convos.append(ConvoView(id=f"C-{i}", project=None, turns=[turn]))

    text = render(window, convos, {"context_peak": "context_peak since x: n=0 median=0 p90=0 max=0"}, [], [],
                 budget=400_000)

    assert len(text) <= 400_000
    assert big_ask in text
    for i in range(1, 300):
        assert f"ask {i}" in text
    # Meaningful, not vacuous: every cap in the sequence is well below len(long_line), so "long_line not in
    # text" would hold even if nothing shrank at all. What actually proves the lines were cut (down to the
    # narrowest tier) is that the tool/error line markers are gone entirely.
    assert "tool:" not in text
    assert "error:" not in text
