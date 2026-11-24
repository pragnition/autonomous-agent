import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.watch.digest import SIGNAL_KEYS, ConvoView, render
from agent.watch.host import Host
from agent.watch.measure import measure
from agent.watch.state import State
from agent.watch.transcript import TurnFacts

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
                 runs=None, statuses=None, secrets=None, gh_error=None, transcripts_error=None, core_error=None,
                 notes=None, notes_error=None):
        self.collect = collect
        self._runs = runs or {}
        self._statuses = statuses or {}
        self._core_error = core_error
        self._transcripts = transcripts or {}
        self._login = login
        self._search = search
        self._views = views or {}
        self._checks = checks or {}
        self._secrets = secrets or []
        self._gh_error = gh_error
        self._transcripts_error = transcripts_error
        self._notes = notes or []
        self._notes_error = notes_error
        self.gh_calls: list[list[str]] = []
        self.transcripts_calls: list[list[str]] = []
        self.core_python_calls: list[tuple] = []
        self.runner_python_calls: list[tuple] = []

    def core_python(self, module_file, args):
        self.core_python_calls.append((module_file, tuple(args)))
        if self._core_error:
            raise self._core_error
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
            view = self._views[args[2]]
            if isinstance(view, BaseException):
                raise view
            return view
        if args[:2] == ["pr", "checks"]:
            return self._checks[args[2]]
        if args[0] == "api" and "/actions/runs?" in args[1]:
            return self._runs[args[1]]
        if args[0] == "api" and "/commits/" in args[1] and args[1].endswith("/status"):
            return self._statuses[args[1]]
        raise AssertionError(f"unexpected gh call: {args}")

    def secret_values(self):
        return self._secrets

    def runner_python(self, module_file, args):
        self.runner_python_calls.append((module_file, tuple(args)))
        if self._notes_error:
            raise self._notes_error
        return json.dumps(self._notes)


_NO_PRS = {"login": _cp(stdout="jensen-bot\n"), "search": _cp(stdout="[]")}
_EMPTY = {"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0, "running": []}


def _pr_view(created_at="2026-11-19T09:00:00Z", merged_at="2026-11-19T09:10:00Z", **extra):
    return _cp(stdout=json.dumps({"additions": 1, "deletions": 1, "createdAt": created_at, "mergedAt": merged_at,
                                  "commits": [], **extra}))


def _search(*urls):
    return _cp(stdout=json.dumps([{"url": url, "repository": {"nameWithOwner": "/".join(url.split("/")[3:5])},
                                   "number": int(url.rsplit("/", 1)[1])} for url in urls]))


def _since(dt: datetime) -> datetime:
    return dt - timedelta(days=1)


# -- skip rules --


def test_quiet_hour_writes_nothing(tmp_path):
    state = State(tmp_path)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is False
    assert result.skip_reason == "quiet"
    assert result.digest is None
    assert not list(tmp_path.glob("digest-*.md"))
    assert not (tmp_path / "metrics.jsonl").exists()


def test_quiet_hour_with_a_successful_empty_notes_read_still_stages_the_notes_cursor(tmp_path):
    # A notes read that succeeds but finds nothing must still advance the staged cursor, even on an otherwise
    # quiet hour -- staging happens before the quiet-check precisely so this isn't skipped. Left unstaged, a
    # long run of quiet-but-notes-successful hours would never move the cursor forward at all.
    state = State(tmp_path)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is False  # genuinely quiet: no items, no notes, no error
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["notes_pending_end"] == _iso(T0)

    state.commit()
    assert state.notes_committed_end() == T0


def test_pool_hold_skips_without_consuming_window(tmp_path):
    state = State(tmp_path)
    baseline = T0
    state.window(baseline)
    state.commit()  # a prior, already-committed hour

    hold_now = baseline + timedelta(hours=1)
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "hold_scheduled",
                             "claimed": 0})

    result = measure(state, host, hold_now)

    assert result.has_work is False
    assert result.skip_reason == "pool_hold"
    assert result.digest is None
    assert result.window == (baseline, hold_now)
    assert not list(tmp_path.glob("digest-*.md"))

    # The skip never committed: the next window still starts from the prior committed end, not from hold_now.
    start, _ = state.window(hold_now + timedelta(hours=1))
    assert start == baseline


def test_pool_hold_comes_from_the_shared_pool_helper(tmp_path, monkeypatch):
    # measure and `assess-check` share one reading of core's pool (`host.pool_is_holding`).
    from agent.watch import measure as measure_module

    seen = []

    def holding(host, now=None):
        seen.append(now)
        return True

    monkeypatch.setattr(measure_module, "pool_is_holding", holding)
    host = FakeHost(collect=_EMPTY, **_NO_PRS)  # core itself says "normal": only the helper says hold

    result = measure(State(tmp_path), host, T0)

    assert result.skip_reason == "pool_hold" and seen == [T0]

    # Unreadable (None) is not a hold: the hour goes on, and core's own reply decides the rest.
    monkeypatch.setattr(measure_module, "pool_is_holding", lambda host, now=None: None)
    assert measure(State(tmp_path / "unread"), FakeHost(collect=_EMPTY, **_NO_PRS), T0).skip_reason == "quiet"


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

    result = measure(state, host, T0)

    assert result.has_work is True
    records = {r["id"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"}

    assert records["W-a"]["turn_failed"] == 1
    assert records["W-a"]["no_delegation"] == 1
    assert records["W-a"]["conversation"] == "C-a"

    assert records["W-b"]["turn_failed"] == 0


def test_session_restarts_and_account_moves_from_agentd(tmp_path):
    # agentd's record of the turn: a session that crashed or was lost and had to start again, and the times the turn
    # moved to another account. A turn agentd has no record of counts neither.
    state = State(tmp_path)
    restarted = _item("W-r", conversation_id="C-a")
    restarted["agentd"] = {"attempts": 2, "forks": 0, "crash_resumed": True, "fresh_retried": False}
    moved = _item("W-m", conversation_id="C-a")
    moved["agentd"] = {"attempts": 3, "forks": 2, "crash_resumed": False, "fresh_retried": False}
    bare = _item("W-b", conversation_id="C-a")
    host = FakeHost(collect={"items": [restarted, moved, bare], "conversations": {}, "projects": [],
                             "pool": "normal", "claimed": 0}, **_NO_PRS)

    measure(state, host, T0)

    records = {r["id"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"}
    assert (records["W-r"]["session_restarted"], records["W-r"]["account_moved"]) == (1, 0)
    assert (records["W-m"]["session_restarted"], records["W-m"]["account_moved"]) == (0, 2)
    assert (records["W-b"]["session_restarted"], records["W-b"]["account_moved"]) == (0, 0)
    assert state.count("account_moved", _since(T0)).endswith("total=2 turns=1")


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

    measure(state, host, T0)

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

    measure(state, host, T0)

    record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-big")
    assert record["big_start_small_ask"] == 1
    assert record["no_delegation"] == 0


def test_midturn_wait_seconds(tmp_path):
    state = State(tmp_path)
    item = _item("W-mid", conversation_id="C-1", created_at="2026-11-19T10:00:00Z",
                 updated_at="2026-11-19T10:04:30Z", delivered_into="W-parent")

    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0)

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

    measure(state, host, T0)

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


def test_ci_falls_back_to_the_actions_runs_of_the_head_commit_when_checks_are_forbidden(tmp_path):
    # A fine-grained token has no "Checks" permission, so `gh pr checks` can 403 while Actions: read still works.
    state = State(tmp_path)
    forbidden = _cp(returncode=1, stderr="HTTP 403: Resource not accessible by personal access token")

    def _runs(*runs):
        return _cp(stdout=json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}))

    cases = {
        # repo: (headRefOid, runs API response, expected ci)
        "repo1": ("sha1", _runs({"status": "completed", "conclusion": "success"},
                                {"status": "completed", "conclusion": "skipped"},
                                {"status": "completed", "conclusion": "neutral"}), "passed"),
        "repo2": ("sha2", _runs(), "none"),
        "repo3": ("sha3", _runs({"status": "completed", "conclusion": "success"},
                                {"status": "in_progress", "conclusion": None}), "failed"),
        "repo4": ("sha4", _runs({"status": "completed", "conclusion": "success"},
                                {"status": "completed", "conclusion": "failure"}), "failed"),
        "repo5": ("sha5", forbidden, "unknown"),
        "repo6": (None, None, "unknown"),  # no head commit to look up: no runs call at all
    }
    search_results, views, checks, runs = [], {}, {}, {}
    for n, (repo, (sha, response, _)) in enumerate(cases.items(), start=1):
        url = f"https://github.com/org/{repo}/pull/{n}"
        search_results.append({"url": url, "repository": {"nameWithOwner": f"org/{repo}"}, "number": n})
        view = {"additions": 1, "deletions": 1, "createdAt": "2026-11-19T09:00:00Z",
                "mergedAt": "2026-11-19T09:05:00Z", "commits": []}
        if sha:
            view["headRefOid"] = sha
            runs[f"repos/org/{repo}/actions/runs?head_sha={sha}&per_page=100"] = response
        views[url] = _cp(stdout=json.dumps(view))
        checks[url] = forbidden
    # repo2's head commit has no Actions runs and no commit statuses either: `none`.
    statuses = {"repos/org/repo2/commits/sha2/status": _cp(stdout=json.dumps(
        {"state": "pending", "total_count": 0, "statuses": []}))}

    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    login=_cp(stdout="jensen-bot\n"), search=_cp(stdout=json.dumps(search_results)),
                    views=views, checks=checks, runs=runs, statuses=statuses)

    measure(state, host, T0)

    prs = {r["repo"]: r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"}
    assert {repo: prs[f"org/{repo}"]["ci"] for repo in cases} == {repo: want for repo, (_, _, want) in cases.items()}
    runs_calls = [c for c in host.gh_calls if c[0] == "api" and "/actions/runs?" in c[1]]
    assert sorted(c[1] for c in runs_calls) == sorted(runs)


def test_pr_fetch_failure_skips_prs_without_crashing(tmp_path):
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    gh_error=RuntimeError("GH_TOKEN missing from /live/data/secrets/agent.env"))

    result = measure(state, host, T0)

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

    result = measure(state, host, T0)

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

    result = measure(state, host, T0)

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
    measure(state, host1, T0)
    state.commit()  # the first window's Claude run succeeds: its staged clones become permanently known

    first_record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-first")
    assert first_record["reclone"] == 0
    assert state.known_clones() == {"org/repo"}

    item2 = _item("W-second", conversation_id="C-2")
    host2 = FakeHost(collect={"items": [item2], "conversations": {}, "projects": [], "pool": "normal",
                              "claimed": 0},
                     transcripts={"W-second": clone_lines}, **_NO_PRS)
    measure(state, host2, T0 + timedelta(hours=1))

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

    first = measure(state, host, T0)
    assert first.has_work is True
    first_record = next(r for r in state.read_metrics(_since(T0)) if r["id"] == "W-a")
    assert first_record["reclone"] == 0
    assert state.known_clones() == set()  # staged, not yet committed

    # Claude's run against the first digest fails; nothing was committed. The same window is measured again.
    second = measure(state, host, T0)
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

    result = measure(state, host, T0)

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

    result = measure(state, host, T0)

    text = result.digest.read_text()
    assert "xoxb-123456789012-abcdefghij" not in text
    assert "supersecretvalue" not in text
    assert "[redacted]" in text


def test_measure_digest_has_jensen_reports_scrubbed(tmp_path):
    # A note Jensen wrote itself can carry a secret or a product name just as easily as a transcript can
    # (Review Focus 3): it must only ever reach the digest through the scrub, same as everything else.
    state = State(tmp_path)
    note = {"name": "2026-11-19-blocked.md",
            "text": "hit a limit: supersecretvalue and a token xoxb-1234567890-abcdefghij got in the way",
            "modified": "2026-11-19T09:30:00Z"}
    host = FakeHost(collect=_EMPTY, notes=[note], secrets=["supersecretvalue"], **_NO_PRS)

    result = measure(state, host, T0)

    # Even though no turns finished this hour, a new note alone is still work.
    assert result.has_work is True
    text = result.digest.read_text()
    assert "## Jensen reports" in text
    assert "2026-11-19-blocked.md" in text
    assert "supersecretvalue" not in text
    assert "xoxb-1234567890-abcdefghij" not in text
    assert "[redacted]" in text


def test_measure_marks_the_digest_when_notes_cannot_be_read_but_still_advances_the_window(tmp_path):
    # A failed note read must not look exactly like "no notes this hour" -- it gets named in the digest, by
    # exception type only. Unlike core being unreachable, it must NOT hold the main window back: notes have
    # their own, separate cursor for that (see the next two tests), since the main window may well have real
    # turns/PRs to commit this same hour.
    state = State(tmp_path)
    state.window(T0 - timedelta(hours=1))
    state.commit()  # an earlier, committed hour

    host = FakeHost(collect=_EMPTY, notes_error=RuntimeError("docker exec returned 1: /secret/host/path"),
                    **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is True
    text = result.digest.read_text()
    assert "Jensen reports: unavailable (RuntimeError)" in text
    assert "/secret/host/path" not in text  # the raw error message never reaches the digest

    # The main window still advances normally: a notes failure alone no longer holds it open.
    state.commit()
    start, _ = state.window(T0 + timedelta(hours=1))
    assert start == T0


def test_notes_error_with_core_items_present_does_not_re_fetch_them_next_window(tmp_path):
    # The regression this guards against: holding the *main* window open on a notes failure, while core's own
    # turns for this hour were staged and committed anyway, would mean the next window re-fetches -- and, since
    # metrics.jsonl only ever appends, re-commits -- those same turns, compounding every hour notes stay down.
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    notes_error=RuntimeError("docker exec returned 1: boom"), **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is True
    assert any(r["id"] == "W-a" for r in state.read_metrics(_since(T0)))  # core's turn is staged despite the error

    state.commit()  # Claude's action on this digest succeeded

    # The next window starts at this hour's own end -- not held back by the notes failure -- so core's own
    # filter by `updated_at` would never hand this same turn back again.
    start, _ = state.window(T0 + timedelta(hours=1))
    assert start == T0


def test_notes_gap_is_covered_after_an_error_then_a_successful_collect(tmp_path):
    # Notes' own cursor must lag, not reset, across a failure: the next successful read has to start from the
    # last point notes were actually read, not from wherever the main window happens to be that hour -- or the
    # notes sitting there during the failed hour would never be looked at again.
    state = State(tmp_path)
    item = _item("W-a", conversation_id="C-a")  # keeps every hour non-quiet, so each one writes a digest
    host = FakeHost(collect={"items": [item], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    hour_a = T0
    measure(state, host, hour_a)  # notes succeed
    state.commit()  # notes_committed_end -> hour_a

    hour_b = T0 + timedelta(hours=1)
    host._notes_error = RuntimeError("docker exec returned 1: boom")
    measure(state, host, hour_b)  # notes fail
    state.commit()  # main window advances to hour_b; notes' own cursor stays at hour_a

    hour_c = T0 + timedelta(hours=2)
    host._notes_error = None
    host.runner_python_calls.clear()
    measure(state, host, hour_c)

    [(_, args)] = [call for call in host.runner_python_calls if call[1][0] == "collect"]
    assert args[args.index("--since") + 1] == _iso(hour_a)  # covers the whole gap since the last good read
    assert args[args.index("--until") + 1] == _iso(hour_c)


def test_a_notes_read_that_keeps_failing_is_work_only_in_the_first_hour_it_fails(tmp_path):
    # The runner staying unreachable for a day must not hand Claude a near-empty digest every hour.
    state = State(tmp_path)
    host = FakeHost(collect=_EMPTY, notes_error=RuntimeError("docker exec returned 1: boom"), **_NO_PRS)

    first = measure(state, host, T0)
    assert first.has_work is True
    # Claude's run on it failed: the hour is measured again, and is still the first of the streak.
    again = measure(state, host, T0 + timedelta(minutes=5))
    assert again.has_work is True
    state.commit()

    second = measure(state, host, T0 + timedelta(hours=1))
    assert second.has_work is False and second.skip_reason == "quiet"
    state.commit()

    # With other work in a later hour of the streak, the digest still says the notes are unreadable.
    busy = FakeHost(collect={**_EMPTY, "items": [_item("W-a", conversation_id="C-a")]},
                    notes_error=RuntimeError("docker exec returned 1: boom"), **_NO_PRS)
    third = measure(state, busy, T0 + timedelta(hours=2))
    assert third.has_work is True
    assert "Jensen reports: unavailable (RuntimeError)" in third.digest.read_text()
    state.commit()

    # A good read ends the streak; the next failure is news again.
    host._notes_error = None
    assert measure(state, host, T0 + timedelta(hours=3)).has_work is False
    state.commit()
    host._notes_error = RuntimeError("docker exec returned 1: boom")
    assert measure(state, host, T0 + timedelta(hours=4)).has_work is True


def test_improve_jensens_own_turns_alone_are_no_work(tmp_path):
    # The reviews' own turns are improve-jensen's traffic, not Jensen's work.
    state = State(tmp_path)
    own = [_item("W-v", conversation_id="V-review-0a1b2c3d", payload={"continuation": 1}),
           _item("W-r", conversation_id="C-p", payload={"source": "improve-jensen"})]
    host = FakeHost(collect={**_EMPTY, "items": own}, **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is False and result.skip_reason == "quiet" and result.digest is None
    # Still kept in the metrics, as on any other hour.
    state.commit()
    assert {r["id"] for r in state.read_metrics(_since(T0))} == {"W-v", "W-r"}

    person = _item("W-p", conversation_id="C-p", payload={"source": "slack"}, updated_at="2026-11-19T10:30:00Z")
    host2 = FakeHost(collect={**_EMPTY, "items": own + [person]}, **_NO_PRS)
    assert measure(state, host2, T0 + timedelta(hours=1)).has_work is True


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

    result = measure(state, host, T0)

    text = result.digest.read_text()
    assert secret not in text
    assert secret[:4] not in text  # not even a short prefix of the secret survives the 300-char cut


def test_digest_never_leaks_a_product_secret_or_one_with_json_escapes_across_the_cut(tmp_path):
    # A product's own secret (data/secrets/projects/*.env) must be scrubbed like Jensen's; and a secret holding a
    # `"` or `\\` is escaped in the raw JSON line, so only a scrub of the decoded text, before the 300-char cut,
    # keeps even its first few characters out.
    live = tmp_path / "live"
    (live / "data" / "secrets" / "projects").mkdir(parents=True)
    (live / "data" / "secrets" / "agent.env").write_text("GH_TOKEN=agent-token-0001\n")
    product = "Qz7productdbpw-0042"
    escaped_in, escaped_out = 'in"put\\secret-0123', 'ou"tput\\secret-4567'
    (live / "data" / "secrets" / "projects" / "sample.env").write_text(
        f"DB_PASSWORD={product}\nAPI_KEY={escaped_in}\nOTHER_KEY={escaped_out}\n")
    secrets = Host(run=lambda *a, **k: None, live=live).secret_values()
    leaking_lines = [
        _assistant([{"id": "t1", "name": "Bash", "input": {"command": "x" * 296 + escaped_in}}]),
        _result_line("t1", "y" * 296 + escaped_out + " trailing", is_error=True),
        _assistant([{"id": "t2", "name": "Bash", "input": {"command": "cat .env"}}]),
        _result_line("t2", "z" * 297 + product),
    ]
    host = FakeHost(collect={**_EMPTY, "items": [_item("W-a", conversation_id="C-a")]},
                    transcripts={"W-a": leaking_lines}, secrets=secrets, **_NO_PRS)

    result = measure(state := State(tmp_path / "state"), host, T0)

    text = result.digest.read_text()
    for secret in (product, escaped_in, escaped_out):
        assert secret not in text
        assert secret[:3] not in text, secret[:3]  # not even a prefix across the 300-char cut
    assert state.read_metrics(_since(T0))  # and the turn was still measured


# -- digest totals --


def test_digest_lists_totals(tmp_path):
    state = State(tmp_path)
    # A newest failed-deploy record is a simple way to force `has_work` with an otherwise empty collection.
    state.log_deploy({"ts": "2026-11-19T09:00:00Z", "from": "a" * 40, "to": "b" * 40, "services": [],
                      "result": "check_failed: compose up exited 1"})
    host = FakeHost(collect={"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0},
                    **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is True
    text = result.digest.read_text()
    for signal in ("context_first", "context_peak", "inline_code_chars", "lost_background", "still_running",
                   "turn_failed", "no_delegation", "big_start_small_ask", "reclone", "tool_errors",
                   "missing_tools", "session_restarted", "account_moved", "midturn_wait_s", "pr"):
        assert f"{signal} since" in text


def test_digest_names_the_conversation_behind_each_new_pr():
    # The watch copies this into plan.json, so a review's findings reach the session that made the PR.
    window = (T0, T0 + timedelta(hours=1))
    prs = [{"url": "https://github.com/o/r/pull/1", "repo": "o/r", "lines": 10, "ci": "passed",
            "open_to_merge_s": 60.0, "conversation": "C-made-it"},
           {"url": "https://github.com/o/r/pull/2", "repo": "o/r", "lines": 5, "ci": "none",
            "open_to_merge_s": None, "conversation": None}]

    text = render(window, [], {}, prs)

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

    text = render(window, convos, {}, [])

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

    text = render(window, convos, {"context_peak": "context_peak since x: n=0 median=0 p90=0 max=0"}, [],
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


# -- what Jensen added --


def _added_turn(item_id, *, added=(), skills=(), own=False) -> dict:
    turn = {"id": item_id, "source": None, "status": "done", "outcome": "done", "minutes": 1.0, "asked": "",
            "replied": "", "facts": TurnFacts(skills=list(skills), added=list(added)), "added": list(added),
            "signals": {}}
    return {**turn, "own": True} if own else turn


def _section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [line for line in lines[start:end] if line]


def test_what_jensen_added_sits_between_reports_and_conversations_and_says_none_when_empty():
    text = render((T0, T0 + timedelta(hours=1)), [], {}, [])

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings[headings.index("## Jensen reports") + 1] == "## What Jensen added"
    assert headings[headings.index("## What Jensen added") + 1] == "## Conversations"
    assert _section(text, "## What Jensen added") == ["- none"]


def test_what_jensen_added_lists_each_turns_lines_and_the_skills_used_leaving_out_improve_jensens_own():
    convos = [
        ConvoView(id="C-a", project="sample", turns=[
            _added_turn("W-a", added=["install: pipx install some-tool", "skill: /work/r/.claude/skills/x/SKILL.md"],
                        skills=["superpowers:writing-plans", "deploy"]),
            _added_turn("W-own", added=["install: npm i -g own-tool"], skills=["deploy"] * 5, own=True),
        ]),
        ConvoView(id="C-b", project=None, turns=[
            _added_turn("W-b", added=["memory: /var/agent/memory/people.md"],
                        skills=["deploy", "deploy", "superpowers:writing-plans", "review"]),
        ]),
    ]

    text = render((T0, T0 + timedelta(hours=1)), convos, {}, [])

    assert _section(text, "## What Jensen added") == [
        "- C-a W-a: install: pipx install some-tool",
        "- C-a W-a: skill: /work/r/.claude/skills/x/SKILL.md",
        "- C-b W-b: memory: /var/agent/memory/people.md",
        "- skills used this hour: deploy (3), superpowers:writing-plans (2), review (1)",
    ]


def test_what_jensen_added_says_none_but_still_lists_the_skills_used():
    convos = [ConvoView(id="C-a", project=None, turns=[_added_turn("W-a", skills=["review"])]),
              ConvoView(id="C-own", project=None, turns=[_added_turn("W-own", added=["install: pipx install x"],
                                                                     own=True)])]

    text = render((T0, T0 + timedelta(hours=1)), convos, {}, [])

    assert _section(text, "## What Jensen added") == ["- none", "- skills used this hour: review (1)"]


def test_what_jensen_added_lines_shrink_with_the_tool_lines():
    added = "install: " + "x" * 1000
    convos = [ConvoView(id="C-a", project=None, turns=[_added_turn("W-a", added=[added], skills=["review"])])]
    window = (T0, T0 + timedelta(hours=1))
    full = render(window, convos, {}, [])
    assert f"- C-a W-a: {added}" in full

    cut = render(window, convos, {}, [], budget=len(full) - 1)
    assert f"- C-a W-a: {added[:120]}" in cut and added[:121] not in cut

    narrower = render(window, convos, {}, [], budget=len(cut) - 1)
    assert f"- C-a W-a: {added[:40]}" in narrower and added[:41] not in narrower

    omitted = render(window, convos, {}, [], budget=len(narrower) - 1)
    assert "W-a: install" not in omitted
    assert "- skills used this hour: review (1)" in omitted


def test_measure_digest_lists_what_jensen_added_scrubbed_and_not_improve_jensens_own(tmp_path):
    state = State(tmp_path)
    secret = "supersecretvalue"
    person = [
        _assistant([{"id": "t1", "name": "Bash",
                     "input": {"command": f"pipx install some-tool --index-url https://{secret}@example.com"}}]),
        _result_line("t1", "installed"),
        _assistant([{"id": "t2", "name": "Write",
                     "input": {"file_path": "/var/agent/claude/skills/x/SKILL.md", "content": "x"}}]),
        _assistant([{"id": "t3", "name": "Skill", "input": {"skill": "review"}}]),
    ]
    review = [_assistant([{"id": "t1", "name": "Bash", "input": {"command": "npm i -g own-tool"}}]),
              _assistant([{"id": "t2", "name": "Skill", "input": {"skill": "own-skill"}}])]
    items = [_item("W-p", conversation_id="C-p", payload={"source": "slack"}),
             _item("W-r", conversation_id="C-p", payload={"source": "improve-jensen"})]
    host = FakeHost(collect={**_EMPTY, "items": items}, transcripts={"W-p": person, "W-r": review},
                    secrets=[secret], **_NO_PRS)

    text = measure(state, host, T0).digest.read_text()

    assert _section(text, "## What Jensen added") == [
        "- C-p W-p: install: pipx install some-tool --index-url https://[redacted]@example.com",
        "- C-p W-p: skill: /var/agent/claude/skills/x/SKILL.md",
        "- skills used this hour: review (1)",
    ]
    assert secret not in text


# -- a failed deploy or a down core never wedges the watch (C1) --


def test_core_unreachable_still_reports_work_and_keeps_the_window_open(tmp_path):
    state = State(tmp_path)
    state.window(T0 - timedelta(hours=1))
    state.commit()  # an earlier, committed hour
    host = FakeHost(collect=None, core_error=RuntimeError("docker exec returned 1: Error: No such container: x"),
                    **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is True
    text = result.digest.read_text()
    assert "Jensen's control plane is unreachable" in text
    assert "No such container" not in text  # the raw error never reaches the digest
    # Committing this hour must not skip the turns core couldn't hand over: the next window starts where this one did.
    state.commit()
    start, _ = state.window(T0 + timedelta(hours=1))
    assert start == T0 - timedelta(hours=1)


def test_core_answering_garbage_counts_as_unreachable(tmp_path):
    state = State(tmp_path)
    host = FakeHost(collect=None, **_NO_PRS)
    host.core_python = lambda module_file, args: "Traceback (most recent call last): not json"

    result = measure(state, host, T0)

    assert result.has_work is True
    assert "Jensen's control plane is unreachable" in result.digest.read_text()


def _deploys(state, *results):
    for n, result in enumerate(results):
        state.log_deploy({"ts": f"2026-11-19T0{n + 7}:00:00Z", "from": f"{n}" * 40, "to": f"{n + 1}" * 40,
                          "services": ["agent"], "result": result})


def test_newest_failed_deploy_record_forces_work_on_a_quiet_hour(tmp_path):
    for n, failure in enumerate(("check_failed: /health/runner did not return 200",
                                 "not_deployable: the checkout has uncommitted changes")):
        state = State(tmp_path / str(n))
        _deploys(state, "ok", failure)

        result = measure(state, FakeHost(collect=_EMPTY, **_NO_PRS), T0)

        assert result.has_work is True, failure
        text = result.digest.read_text()
        assert failure in text
        assert "2" * 40 in text  # the failed record's `to`


def test_an_ok_deploy_after_a_failed_one_leaves_a_quiet_hour_quiet(tmp_path):
    state = State(tmp_path)
    _deploys(state, "check_failed: smoke test did not print smoke ok", "ok")

    result = measure(state, FakeHost(collect=_EMPTY, **_NO_PRS), T0)

    assert result.has_work is False
    assert result.skip_reason == "quiet"


def _attention(digest: Path) -> list[str]:
    text = digest.read_text()
    return text.split("## Needs attention\n", 1)[1].split("\n\n", 1)[0].splitlines()


def test_a_behaviour_changing_deploy_that_landed_this_window_is_work(tmp_path):
    # A deploy that was pending (Jensen busy) can land in an otherwise quiet hour: the watch must still see it.
    state = State(tmp_path)
    state.log_deploy({"ts": "2026-11-19T09:30:00Z", "from": "a" * 40, "to": "b" * 40,
                      "services": ["agent", "core"], "result": "ok"})

    result = measure(state, FakeHost(collect=_EMPTY, **_NO_PRS), T0)

    assert result.has_work is True
    assert _attention(result.digest) == [
        f"- A behaviour-changing deploy landed (from {'a' * 40} to {'b' * 40}, at 2026-11-19T09:30:00Z): "
        "Jensen's running services changed."]

    # Only shas go into the line: anything else in the record's sha fields reads as unknown.
    other = State(tmp_path / "other")
    other.log_deploy({"ts": "2026-11-19T09:45:00Z", "from": None, "to": "acme-widgets main",
                      "services": ["agent"], "result": "ok"})
    attention = _attention(measure(other, FakeHost(collect=_EMPTY, **_NO_PRS), T0).digest)
    assert attention == ["- A behaviour-changing deploy landed (from unknown to unknown, at 2026-11-19T09:45:00Z): "
                         "Jensen's running services changed."]


def test_deploys_that_change_no_service_or_land_outside_the_window_leave_a_quiet_hour_quiet(tmp_path):
    # The window is (09:00, 10:00].
    for n, record in enumerate((
            {"ts": "2026-11-19T09:30:00Z", "services": [], "result": "ok"},  # docs only: nothing restarted
            {"ts": "2026-11-19T08:59:59Z", "services": ["agent"], "result": "ok"},  # an earlier window's
            {"ts": "2026-11-19T09:00:00Z", "services": ["agent"], "result": "ok"},  # on the boundary: earlier
            {"ts": "2026-11-19T10:00:01Z", "services": ["agent"], "result": "ok"},  # after the window
            {"ts": "2026-11-19T09:30:00Z", "services": ["agent"], "result": "pending"},  # not landed yet
            {"ts": "not a time", "services": ["agent"], "result": "ok"})):
        state = State(tmp_path / str(n))
        state.log_deploy({"from": "a" * 40, "to": "b" * 40, **record})

        result = measure(state, FakeHost(collect=_EMPTY, **_NO_PRS), T0)

        assert result.has_work is False and result.skip_reason == "quiet", record


def test_a_pr_with_no_created_at_still_gets_a_record(tmp_path):
    state = State(tmp_path)
    url = "https://github.com/org/repo1/pull/1"
    host = FakeHost(collect=_EMPTY, login=_cp(stdout="jensen-bot\n"), search=_search(url),
                    views={url: _pr_view(created_at=None)}, checks={url: _cp(returncode=0)})

    result = measure(state, host, T0)

    assert result.has_work is True
    [record] = [r for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"]
    assert record["url"] == url
    assert record["open_to_merge_s"] is None


def test_an_unexpected_error_on_one_pr_names_only_its_type(tmp_path, capsys):
    state = State(tmp_path)
    bad, good = "https://github.com/org/repo1/pull/1", "https://github.com/org/repo2/pull/2"
    host = FakeHost(collect=_EMPTY, login=_cp(stdout="jensen-bot\n"), search=_search(bad, good),
                    views={bad: TypeError("a detail naming /srv/private/path"), good: _pr_view()},
                    checks={good: _cp(returncode=0)})

    result = measure(state, host, T0)

    assert result.has_work is True
    assert {r["url"] for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"} == {good}
    err = capsys.readouterr().err
    assert "TypeError" in err
    assert "/srv/private/path" not in err


# -- event time (I2) --


def test_records_are_stamped_with_the_event_time_not_the_measurement_time(tmp_path):
    state = State(tmp_path)
    turn = _item("W-t", conversation_id="C-1", created_at="2026-11-19T09:00:00Z", updated_at="2026-11-19T09:20:00Z")
    mid = _item("W-m", conversation_id="C-1", created_at="2026-11-19T09:30:00Z", updated_at="2026-11-19T09:31:00Z",
                delivered_into="W-t")
    merged, still_open = "https://github.com/org/repo1/pull/1", "https://github.com/org/repo2/pull/2"
    host = FakeHost(collect={**_EMPTY, "items": [turn, mid]}, login=_cp(stdout="jensen-bot\n"),
                    search=_search(merged, still_open),
                    views={merged: _pr_view(created_at="2026-11-19T09:00:00Z", merged_at="2026-11-19T09:40:00Z"),
                           still_open: _pr_view(created_at="2026-11-19T09:45:00Z", merged_at=None)},
                    checks={merged: _cp(returncode=0), still_open: _cp(returncode=0)})

    measure(state, host, T0)

    stamps = {r.get("id") or r.get("url"): r["ts"] for r in state.read_metrics(_since(T0))}
    assert stamps == {"W-t": "2026-11-19T09:20:00Z", "W-m": "2026-11-19T09:31:00Z",
                      merged: "2026-11-19T09:40:00Z", still_open: "2026-11-19T09:45:00Z"}


# -- a PR the turn announced --


def test_a_pr_the_turn_announced_maps_to_its_conversation(tmp_path):
    # `gh pr create ... | tail -1` hides the URL from the transcript; Jensen still says it in a progress post or its
    # reply. A PR the turn made itself (created) wins over one another turn only mentioned.
    state = State(tmp_path)
    announced, made = "https://github.com/org/repo1/pull/1", "https://github.com/org/repo1/pull/2"
    talker = _item("W-a", conversation_id="C-said", updated_at="2026-11-19T09:01:00Z",
                   result={"text": f"Merged {made}.", "outcome": "done"})
    poster = [_assistant([{"id": "p1", "name": "mcp__agent__post_update",
                           "input": {"text": f"PR is up: {announced}. Checks next."}}]),
              _result_line("p1", "posted")]
    maker = _item("W-m", conversation_id="C-made", updated_at="2026-11-19T09:02:00Z")
    host = FakeHost(collect={**_EMPTY, "items": [talker, maker]},
                    transcripts={"W-a": poster, "W-m": [_line(type="SystemMessage", subtype="code_change_published",
                                                              data={"url": made, "action": "created"})]},
                    login=_cp(stdout="jensen-bot\n"), search=_search(announced, made),
                    views={announced: _pr_view(merged_at=None), made: _pr_view(merged_at=None)},
                    checks={announced: _cp(returncode=0), made: _cp(returncode=0)})

    measure(state, host, T0)

    prs = {r["url"]: r["conversation"] for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"}
    assert prs == {announced: "C-said", made: "C-made"}


# -- a PR made by a turn still running (I7) --


def test_a_pr_opened_by_a_turn_still_running_maps_to_that_turns_conversation(tmp_path):
    state = State(tmp_path)
    url = "https://github.com/org/repo1/pull/1"
    still_writing = [
        _line(type="SystemMessage", subtype="code_change_published", data={"url": url, "action": "created"}),
        '{"type": "AssistantMessage", "parent_tool_use_id": nu',  # the runner is still writing this line
    ]
    host = FakeHost(collect={**_EMPTY, "running": [{"id": "W-run", "conversation_id": "C-run"}]},
                    transcripts={"W-run": still_writing}, login=_cp(stdout="jensen-bot\n"), search=_search(url),
                    views={url: _pr_view(merged_at=None)}, checks={url: _cp(returncode=0)})

    result = measure(state, host, T0)

    records = state.read_metrics(_since(T0))
    assert [r["conversation"] for r in records if r["kind"] == "pr"] == ["C-run"]
    assert not [r for r in records if r["kind"] == "turn"]  # not finished: no turn record yet
    assert f"{url} (org/repo1): 2 lines, ci=passed, open_to_merge_s=None, conversation=C-run" in \
        result.digest.read_text()


def test_a_running_turn_whose_transcript_breaks_turn_facts_costs_nothing_else(tmp_path, capsys):
    # A usage count that isn't a number makes turn_facts raise; for a running turn that must be caught the same way
    # a finished turn's is, never crash the hour.
    state = State(tmp_path)
    broken = [_line(type="AssistantMessage", parent_tool_use_id=None, usage={"input_tokens": "12k"}, content=[])]
    host = FakeHost(collect={**_EMPTY, "items": [_item("W-done", conversation_id="C-1")],
                             "running": [{"id": "W-run", "conversation_id": "C-run"}]},
                    transcripts={"W-run": broken, "W-done": _bash_calls(1)}, **_NO_PRS)

    result = measure(state, host, T0)

    assert result.has_work is True
    assert [r["id"] for r in state.read_metrics(_since(T0)) if r["kind"] == "turn"] == ["W-done"]
    err = capsys.readouterr().err
    assert "TypeError" in err and "12k" not in err


# -- improve-jensen's own traffic (m1) --


def test_improve_jensens_own_turns_show_in_the_digest_but_not_in_the_totals(tmp_path):
    state = State(tmp_path)
    person = _item("W-p", conversation_id="C-p", payload={"source": "slack"})
    review = _item("W-r", conversation_id="C-p", payload={"source": "improve-jensen"})
    check = _item("W-v", conversation_id="V-review-0a1b2c3d", payload={"continuation": 1})
    person_mid = _item("W-pm", conversation_id="C-p", created_at="2026-11-19T09:00:00Z",
                       updated_at="2026-11-19T09:00:50Z", delivered_into="W-p", payload={"source": "slack"})
    review_mid = _item("W-rm", conversation_id="C-p", created_at="2026-11-19T09:00:00Z",
                       updated_at="2026-11-19T09:10:00Z", delivered_into="W-p", payload={"source": "improve-jensen"})
    host = FakeHost(collect={**_EMPTY, "items": [person, review, check, person_mid, review_mid]},
                    transcripts={"W-p": _bash_calls(45), "W-r": _bash_calls(45), "W-v": _bash_calls(45)}, **_NO_PRS)

    result = measure(state, host, T0)

    records = {r["id"]: r for r in state.read_metrics(_since(T0))}
    assert records["W-r"]["source"] == "improve-jensen"
    assert records["W-rm"]["source"] == "improve-jensen" and records["W-rm"]["conversation"] == "C-p"
    since = _since(T0)
    assert state.count("no_delegation", since).endswith("total=1 turns=1")
    assert state.count("midturn_wait_s", since).endswith("n=1 median=50.0 p90=50.0 max=50.0")
    text = result.digest.read_text()
    assert "no_delegation since 2026-11-19T09:00:00Z: total=1 turns=1" in text
    for item_id in ("W-r", "W-v"):
        [line] = [line for line in text.splitlines() if line.startswith(f"- turn {item_id} ")]
        assert "not in the totals" in line
    [person_line] = [line for line in text.splitlines() if line.startswith("- turn W-p ")]
    assert "not in the totals" not in person_line


# -- commit statuses when a head commit has no Actions runs (m6) --


def test_ci_reads_commit_statuses_when_the_head_commit_has_no_actions_runs(tmp_path):
    state = State(tmp_path)
    forbidden = _cp(returncode=1, stderr="HTTP 403: Resource not accessible by personal access token")
    no_runs = _cp(stdout=json.dumps({"total_count": 0, "workflow_runs": []}))

    def _status(state_value, n=1):
        return _cp(stdout=json.dumps({"state": state_value, "total_count": n,
                                      "statuses": [{"state": state_value, "context": "ci"}] * n}))

    cases = {"repo1": (_status("success"), "passed"), "repo2": (_status("pending"), "failed"),
             "repo3": (_status("failure"), "failed"), "repo4": (_status("error"), "failed"),
             "repo5": (_status("pending", n=0), "none"), "repo6": (forbidden, "unknown")}
    urls, views, checks, runs, statuses = [], {}, {}, {}, {}
    for n, (repo, (response, _)) in enumerate(cases.items(), start=1):
        url = f"https://github.com/org/{repo}/pull/{n}"
        urls.append(url)
        views[url] = _pr_view(headRefOid=f"sha{n}")
        checks[url] = forbidden
        runs[f"repos/org/{repo}/actions/runs?head_sha=sha{n}&per_page=100"] = no_runs
        statuses[f"repos/org/{repo}/commits/sha{n}/status"] = response
    host = FakeHost(collect=_EMPTY, login=_cp(stdout="jensen-bot\n"), search=_search(*urls), views=views,
                    checks=checks, runs=runs, statuses=statuses)

    measure(state, host, T0)

    prs = {r["repo"]: r["ci"] for r in state.read_metrics(_since(T0)) if r["kind"] == "pr"}
    assert prs == {f"org/{repo}": want for repo, (_, want) in cases.items()}
