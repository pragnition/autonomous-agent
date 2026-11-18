import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.watch.state import State


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_first_window_is_last_hour_then_resumes_from_commit(tmp_path):
    state = State(tmp_path)

    now1 = _dt(2026, 11, 19, 1, 0, 0)
    start1, end1 = state.window(now1)
    assert start1 == now1 - timedelta(hours=1)
    assert end1 == now1
    # window() writes pending_end, ready for a later commit().
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["pending_end"] == "2026-11-19T01:00:00Z"

    state.commit()

    now2 = _dt(2026, 11, 19, 2, 0, 0)
    start2, end2 = state.window(now2)
    assert start2 == now1
    assert end2 == now2

    # Without a commit, the next window still starts at the same place.
    now3 = _dt(2026, 11, 19, 3, 0, 0)
    start3, end3 = state.window(now3)
    assert start3 == now1
    assert end3 == now3


def test_stage_is_visible_to_read_metrics_but_not_known_clones_until_commit(tmp_path):
    state = State(tmp_path)
    since = _dt(2026, 11, 19, 0, 0, 0)

    state.stage([{"ts": "2026-11-19T00:10:00Z", "kind": "turn", "id": "t1", "reclone": 0}], ["org/repo"])

    # Staged metrics show up right away...
    assert [r["id"] for r in state.read_metrics(since)] == ["t1"]
    # ...but staged clones never show up in known_clones() until commit() folds them in.
    assert state.known_clones() == set()

    # A re-measure of the same window stages again: this overwrites, it never appends a second copy.
    state.stage([{"ts": "2026-11-19T00:10:00Z", "kind": "turn", "id": "t1", "reclone": 0}], ["org/repo"])
    assert [r["id"] for r in state.read_metrics(since)] == ["t1"]

    state.commit()
    # Now permanent: committed, not duplicated, and known_clones() finally sees it.
    assert [r["id"] for r in state.read_metrics(since)] == ["t1"]
    assert state.known_clones() == {"org/repo"}

    # commit() cleared the staging files: a later stage()+commit() for a new window doesn't re-merge the old one.
    state.stage([{"ts": "2026-11-19T01:10:00Z", "kind": "turn", "id": "t2", "reclone": 0}], ["org/repo2"])
    assert {r["id"] for r in state.read_metrics(since)} == {"t1", "t2"}
    assert state.known_clones() == {"org/repo"}  # t2's clone isn't merged in yet
    state.commit()
    assert state.known_clones() == {"org/repo", "org/repo2"}
    assert {r["id"] for r in state.read_metrics(since)} == {"t1", "t2"}


def test_commit_without_a_prior_stage_only_moves_the_window(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 0, 0, 0)
    state.window(now)

    state.commit()  # no stage() call ever happened for this window

    assert state.known_clones() == set()
    assert state.read_metrics(_dt(2000, 1, 1, 0, 0, 0)) == []


def test_count_numeric_and_counted_and_pr(tmp_path):
    state = State(tmp_path)
    since = _dt(2026, 11, 19, 0, 0, 0)

    turn_records = [
        {"ts": "2026-11-19T00:10:00Z", "kind": "turn", "id": "t1", "conversation": "c1", "source": "person",
         "context_first": 50000, "context_peak": 100000, "big_start_small_ask": 0, "no_delegation": 0,
         "inline_code_chars": 500, "lost_background": 0, "still_running": 0, "turn_failed": 0,
         "reclone": 0, "tool_errors": [], "missing_tools": []},
        {"ts": "2026-11-19T00:20:00Z", "kind": "turn", "id": "t2", "conversation": "c1", "source": "person",
         "context_first": 60000, "context_peak": 200000, "big_start_small_ask": 1, "no_delegation": 1,
         "inline_code_chars": 1000, "lost_background": 1, "still_running": 0, "turn_failed": 1,
         "reclone": 2, "tool_errors": ["e1", "e2"], "missing_tools": []},
        {"ts": "2026-11-19T00:30:00Z", "kind": "turn", "id": "t3", "conversation": "c1", "source": "schedule",
         "context_first": 70000, "context_peak": 800000, "big_start_small_ask": 0, "no_delegation": 0,
         "inline_code_chars": 1500, "lost_background": 0, "still_running": 1, "turn_failed": 0,
         "reclone": 0, "tool_errors": [], "missing_tools": ["ss"]},
    ]
    midturn_records = [
        {"ts": "2026-11-19T00:15:00Z", "kind": "midturn", "id": "m1", "midturn_wait_s": 120},
        {"ts": "2026-11-19T00:25:00Z", "kind": "midturn", "id": "m2", "midturn_wait_s": 270},
    ]
    pr_records = [
        {"ts": "2026-11-19T00:05:00Z", "kind": "pr", "url": "https://github.com/x/y/pull/1", "repo": "x/y",
         "lines": 40, "open_to_merge_s": 600, "ci": "passed", "coauthor_trailers": 0, "conversation": "c1"},
        {"ts": "2026-11-19T00:35:00Z", "kind": "pr", "url": "https://github.com/x/y/pull/2", "repo": "x/y",
         "lines": 80, "open_to_merge_s": 300, "ci": "failed", "coauthor_trailers": 1, "conversation": None},
    ]
    state.append_metrics(turn_records + midturn_records + pr_records)

    assert state.count("context_peak", since) == \
        "context_peak since 2026-11-19T00:00:00Z: n=3 median=200000 p90=800000 max=800000"
    assert state.count("context_first", since) == \
        "context_first since 2026-11-19T00:00:00Z: n=3 median=60000 p90=70000 max=70000"
    assert state.count("inline_code_chars", since) == \
        "inline_code_chars since 2026-11-19T00:00:00Z: n=3 median=1000 p90=1500 max=1500"
    assert state.count("midturn_wait_s", since) == \
        "midturn_wait_s since 2026-11-19T00:00:00Z: n=2 median=120 p90=270 max=270"

    assert state.count("lost_background", since) == \
        "lost_background since 2026-11-19T00:00:00Z: total=1 turns=1"
    assert state.count("still_running", since) == \
        "still_running since 2026-11-19T00:00:00Z: total=1 turns=1"
    assert state.count("turn_failed", since) == \
        "turn_failed since 2026-11-19T00:00:00Z: total=1 turns=1"
    assert state.count("no_delegation", since) == \
        "no_delegation since 2026-11-19T00:00:00Z: total=1 turns=1"
    assert state.count("big_start_small_ask", since) == \
        "big_start_small_ask since 2026-11-19T00:00:00Z: total=1 turns=1"
    assert state.count("reclone", since) == \
        "reclone since 2026-11-19T00:00:00Z: total=2 turns=1"
    assert state.count("tool_errors", since) == \
        "tool_errors since 2026-11-19T00:00:00Z: total=2 turns=1"
    assert state.count("missing_tools", since) == \
        "missing_tools since 2026-11-19T00:00:00Z: total=1 turns=1"

    assert state.count("pr", since) == \
        ("pr since 2026-11-19T00:00:00Z: prs=2 merged_without_passing_ci=1 "
         "median_lines=40 median_open_to_merge_s=300 coauthor_trailers=1")


def test_count_pr_sums_coauthor_trailers_and_defaults_missing_to_zero(tmp_path):
    state = State(tmp_path)
    since = _dt(2026, 11, 19, 0, 0, 0)
    state.append_metrics([
        {"ts": "2026-11-19T00:05:00Z", "kind": "pr", "url": "https://github.com/x/y/pull/3", "repo": "x/y",
         "lines": 10, "open_to_merge_s": 60, "ci": "passed", "coauthor_trailers": 2, "conversation": None},
        # No `coauthor_trailers` key at all -- missing must count as 0, not crash `_count_pr`.
        {"ts": "2026-11-19T00:10:00Z", "kind": "pr", "url": "https://github.com/x/y/pull/4", "repo": "x/y",
         "lines": 20, "open_to_merge_s": 90, "ci": "passed", "conversation": None},
    ])

    assert state.count("pr", since).endswith("coauthor_trailers=2")


def test_count_unknown_signal_raises(tmp_path):
    state = State(tmp_path)
    with pytest.raises(ValueError):
        state.count("not_a_real_signal", _dt(2026, 11, 19, 0, 0, 0))


def test_note_returns_running_total_per_issue(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 0, 0, 0)

    assert state.note(7, "digest-20261119T0000Z", "lost background 3x", now) == 1
    assert state.note(7, "digest-20261119T0100Z", "lost background 1x", now) == 2
    assert state.note(7, "digest-20261119T0200Z", "lost background 2x", now) == 3
    assert state.note(8, "digest-20261119T0200Z", "no delegation", now) == 1


def test_prune_drops_old_digests_and_metrics(tmp_path):
    state = State(tmp_path)
    now = _dt(2026, 11, 19, 0, 0, 0)

    old_end = now - timedelta(days=31)
    recent_end = now - timedelta(days=1)
    old_digest = state.write_digest(old_end, "old digest")
    recent_digest = state.write_digest(recent_end, "recent digest")

    state.append_metrics([
        {"ts": "2026-10-01T00:00:00Z", "kind": "turn", "id": "old"},
        {"ts": "2026-11-18T00:00:00Z", "kind": "turn", "id": "recent"},
    ])

    state.prune(now, days=30)

    assert not old_digest.exists()
    assert recent_digest.exists()
    remaining = state.read_metrics(_dt(2000, 1, 1, 0, 0, 0))
    assert {r["id"] for r in remaining} == {"recent"}


def test_plan_validation(tmp_path):
    state = State(tmp_path)

    # No plan.json at all.
    assert state.plan() == {"reviews": [], "fix": None}

    # Malformed JSON.
    (tmp_path / "plan.json").write_text("{not valid json")
    assert state.plan() == {"reviews": [], "fix": None}

    # Reviews without a string url are dropped; more than 10 are cut to 10; fix and conversation are kept when valid.
    reviews = [{"url": f"https://github.com/x/y/pull/{i}"} for i in range(12)]
    reviews.append({"notes": "no url here"})
    reviews.append({"url": 123})
    (tmp_path / "plan.json").write_text(json.dumps({"reviews": reviews, "fix": 42, "conversation": "C123"}))
    result = state.plan()
    assert len(result["reviews"]) == 10
    assert all(isinstance(r["url"], str) for r in result["reviews"])
    assert result["reviews"][0]["url"] == "https://github.com/x/y/pull/0"
    assert result["fix"] == 42
    assert result["conversation"] == "C123"

    # fix that isn't a positive int, and a conversation that isn't a string, both fall back to None.
    (tmp_path / "plan.json").write_text(json.dumps({"reviews": [], "fix": -1, "conversation": 5}))
    result = state.plan()
    assert result["fix"] is None
    assert result["conversation"] is None

    # A bool is an int subclass in Python but doesn't count as a positive int here.
    (tmp_path / "plan.json").write_text(json.dumps({"reviews": [], "fix": True}))
    assert state.plan()["fix"] is None

    # A JSON value that parses but isn't an object.
    (tmp_path / "plan.json").write_text(json.dumps([1, 2, 3]))
    assert state.plan() == {"reviews": [], "fix": None}


def test_known_clones_and_deploy_pending_round_trip(tmp_path):
    state = State(tmp_path)

    assert state.known_clones() == set()
    assert state.deploy_pending() is False

    state.add_clones(["org/repo1", "org/repo2"])
    assert state.known_clones() == {"org/repo1", "org/repo2"}

    state.add_clones(["org/repo2", "org/repo3"])
    assert state.known_clones() == {"org/repo1", "org/repo2", "org/repo3"}

    state.set_deploy_pending(True)
    assert state.deploy_pending() is True

    state.set_deploy_pending(False)
    assert state.deploy_pending() is False


def test_write_digest_filename(tmp_path):
    state = State(tmp_path)
    end = _dt(2026, 11, 19, 1, 30, 0)
    path = state.write_digest(end, "hello")
    assert path.name == "digest-20261119T0130Z.md"
    assert path.read_text() == "hello"


def test_log_deploy_appends_jsonl(tmp_path):
    state = State(tmp_path)
    state.log_deploy({"ts": "2026-11-19T00:00:00Z", "from": "aaa", "to": "bbb", "services": ["core"],
                      "result": "ok"})
    state.log_deploy({"ts": "2026-11-19T01:00:00Z", "from": "bbb", "to": "ccc", "services": ["agent"],
                      "result": "failed"})
    lines = (tmp_path / "deploys.jsonl").read_text().splitlines()
    assert [json.loads(line)["to"] for line in lines] == ["bbb", "ccc"]


def test_default_uses_env_or_fallback(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    monkeypatch.delenv("IMPROVE_JENSEN_STATE", raising=False)
    state = State.default()
    assert state.root == fake_home / ".local" / "state" / "improve-jensen"
    assert state.root.exists()

    monkeypatch.setenv("IMPROVE_JENSEN_STATE", str(tmp_path / "custom"))
    state = State.default()
    assert state.root == tmp_path / "custom"
    assert state.root.exists()
