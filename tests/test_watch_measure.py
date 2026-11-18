import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.watch.digest import ConvoView, render
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
    ]

    def _view(url):
        return _cp(stdout=json.dumps({"additions": 10, "deletions": 5, "createdAt": "2026-11-19T09:00:00Z",
                                      "mergedAt": "2026-11-19T09:10:00Z", "commits": []}))

    views = {pr["url"]: _view(pr["url"]) for pr in search_results}
    checks = {
        "https://github.com/org/repo1/pull/1": _cp(returncode=0),
        "https://github.com/org/repo2/pull/2": _cp(returncode=8, stdout="no checks reported on the 'main' ref\n"),
        "https://github.com/org/repo3/pull/3": _cp(returncode=1,
                                                    stderr="HTTP 403: Resource not accessible by integration"),
        "https://github.com/org/repo4/pull/4": _cp(returncode=1, stdout="some other check failure"),
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

    prs = {r["url"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"}
    assert prs["https://github.com/org/repo1/pull/1"]["ci"] == "passed"
    assert prs["https://github.com/org/repo2/pull/2"]["ci"] == "none"
    assert prs["https://github.com/org/repo3/pull/3"]["ci"] == "unknown"
    assert prs["https://github.com/org/repo4/pull/4"]["ci"] == "failed"

    # The transcript that created it wins over the project fallback.
    assert prs["https://github.com/org/repo1/pull/1"]["conversation"] == "C-a"
    # No transcript match: falls back to the active project's conversation for that repo.
    assert prs["https://github.com/org/repo2/pull/2"]["conversation"] == "C-proj2"
    # Neither a transcript match nor an active project: no conversation.
    assert prs["https://github.com/org/repo3/pull/3"]["conversation"] is None
    assert prs["https://github.com/org/repo4/pull/4"]["conversation"] is None


def test_pr_fetch_failure_skips_prs_without_crashing(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    gh_error=RuntimeError("GH_TOKEN missing from /live/data/secrets/agent.env"))

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    assert not [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    text = result.digest.read_text()
    assert "PRs: unavailable (GH_TOKEN missing from /live/data/secrets/agent.env)" in text


def test_pr_fetch_gh_failure_skips_prs_without_crashing(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    gh_error=subprocess.CalledProcessError(2, ["gh", "api", "user"]))

    result = measure(state, host, T0, open_deployed=[])

    assert result.has_work is True
    assert not [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    assert "PRs: unavailable (gh exited 2)" in result.digest.read_text()


# -- reclone --


def test_reclone_counts_repos_seen_in_earlier_windows(tmp_path):
    state = State(tmp_path)
    clone_lines = [_assistant([{"id": "t1", "name": "Bash", "input": {"command": "gh repo clone org/repo"}}])]

    item1 = _item("W-first", conversation_id="C-1")
    host1 = FakeHost(collect={"items": [item1], "conversations": {}, "projects": [], "pool": "normal",
                              "claimed": 0},
                     transcripts={"W-first": clone_lines}, **_NO_PRS)
    measure(state, host1, T0, open_deployed=[])

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
    assert "transcripts: unavailable" in result.digest.read_text()


# -- digest redaction (Review Focus 2) --


def test_digest_redacts_secrets(tmp_path):
    state = State(tmp_path)
    leaking_lines = [
        _assistant([{"id": "t1", "name": "Bash", "input": {"command": "env | grep TOKEN"}}]),
        _result_line("t1", "SLACK_TOKEN=xoxb-123456789012-abcdefghij", is_error=True),
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


# -- digest budget shrinking (Review Focus 3) --


def test_digest_shrinks_tool_lines_not_messages():
    window = (T0, T0 + timedelta(hours=1))
    big_ask = "a" * 200_000
    convos = []
    base_signals = {key: 0 for key in ("context_first", "context_peak", "big_start_small_ask", "no_delegation",
                                       "inline_code_chars", "lost_background", "still_running", "turn_failed",
                                       "reclone")}
    base_signals.update(tool_errors=[], missing_tools=[])
    long_line = "x" * 5000

    for i in range(300):
        facts = TurnFacts(tool_lines=[long_line] * 40, error_lines=["e" * 2000])
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
    assert long_line not in text  # the tool lines got cut, not the messages
