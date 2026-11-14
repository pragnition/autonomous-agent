import json
from datetime import UTC, datetime

import pytest

from agent import db, events, tasks
from agent.core.sessions import WorkQueue
from agent.dashboard.queries import (SessionFilter, get_session, kpi_view, linear_issue, list_events, list_sessions,
                                     overview_counts, paused_by)
from agent_fixtures import add_conversation, add_item

LINEAR_TEXT = "Linear agent session event:\n" + json.dumps(
    {"type": "AgentSessionEvent", "action": "created",
     "agentSession": {"id": "s1", "issue": {"identifier": "ENG-12", "title": "Login times out",
                                            "url": "https://linear.app/x/issue/ENG-12"}}})


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def queue(conn):
    return WorkQueue(conn)


def set_created(conn, item_id, ts):
    conn.execute("UPDATE work_items SET created_at = ?, updated_at = ? WHERE id = ?", (ts, ts, item_id))


def add_run(conn, item_id, *, task_id=None, account="badger", cost=0.5, ended="2026-11-13T10:05:00Z"):
    conn.execute("INSERT INTO runs(id, task_id, kind, status, account_code, outcome, turns, input_tokens, "
                 "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd_est, error, started_at, ended_at) "
                 "VALUES (?, ?, 'code', 'finished', ?, 'answered', 7, 10, 20, 30, 40, ?, NULL, ?, ?)",
                 (item_id, task_id, account, cost, ended, ended))


def started(conn, item_id, ts, type="session_started"):
    conn.execute("INSERT INTO events(ts, task_id, type, actor, data) VALUES (?, NULL, ?, NULL, ?)",
                 (ts, type, json.dumps({"work_item": item_id})))


def make_task(conn, **over):
    fields = dict(project="sample", title="Fix the login", description="", kind="bug", source="slack",
                  source_ref=None, requester="ann", owner="ann")
    fields.update(over)
    return tasks.create_task(conn, **fields)


def test_rows_newest_first_with_total(conn, queue):
    cid = add_conversation(conn, project=None)
    a = add_item(queue, cid, text="first")
    b = add_item(queue, cid, text="second")
    set_created(conn, a, "2026-11-13T09:00:00Z")
    set_created(conn, b, "2026-11-13T10:00:00Z")
    rows, total = list_sessions(conn, SessionFilter(limit=1))
    assert total == 2 and [r["id"] for r in rows] == [b]
    rows, _ = list_sessions(conn, SessionFilter(limit=1, offset=1))
    assert [r["id"] for r in rows] == [a]


def test_state_maps_claimed_to_running(conn, queue):
    cid = add_conversation(conn, project=None)
    item = add_item(queue, cid)
    queue.claim()
    assert get_session(conn, item)["state"] == "running"


def test_filter_by_state_project_source_account_q(conn, queue):
    slack = add_conversation(conn, project="sample")
    other = add_conversation(conn, project=None, channel=None, thread_ts=None)
    a = add_item(queue, slack, text="Fix the flaky login test")
    b = add_item(queue, other, text="Summarise yesterday")
    queue.claim()
    add_run(conn, b, account="onyx")
    conn.execute("UPDATE work_items SET status = 'done' WHERE id = ?", (b,))

    def ids(**f):
        return [r["id"] for r in list_sessions(conn, SessionFilter(**f))[0]]

    assert ids(state="running") == [a]
    assert ids(state="done") == [b]
    assert ids(project="sample") == [a]
    assert ids(source="slack") == [a]
    assert ids(source="other") == [b]
    assert ids(account="onyx") == [b]
    assert ids(q="FLAKY") == [a]


def test_title_from_task_then_payload_then_linear_issue(conn, queue):
    task = make_task(conn)
    with_task = add_conversation(conn, task_id=task.id)
    plain = add_conversation(conn, project=None)
    linear = add_conversation(conn, project=None, channel=None, thread_ts=None)
    a = add_item(queue, with_task, text="whatever")
    b = add_item(queue, plain, text="\n  why is staging slow?\nmore detail " + "x" * 300)
    c = add_item(queue, linear, text=LINEAR_TEXT)
    assert get_session(conn, a)["title"] == "Fix the login"
    assert get_session(conn, b)["title"] == "why is staging slow?"
    assert get_session(conn, c)["title"] == "ENG-12 — Login times out"
    assert get_session(conn, c)["links"]["linear"] == "https://linear.app/x/issue/ENG-12"


def test_long_first_line_cut(conn, queue):
    cid = add_conversation(conn, project=None)
    item = add_item(queue, cid, text="y" * 500)
    assert len(get_session(conn, item)["title"]) <= 120


def test_linear_issue_parses_or_none():
    assert linear_issue(LINEAR_TEXT)["identifier"] == "ENG-12"
    assert linear_issue("hello") is None
    assert linear_issue("Linear agent session event:\n{broken") is None


def test_source_slack_linear_handover_other(conn, queue):
    slack = add_conversation(conn, project=None)
    lin = add_conversation(conn, project=None, channel=None, thread_ts=None)
    db.set_setting(conn, "linear_session:abc", lin)
    hand = add_conversation(conn, is_primary=0, channel=None, thread_ts=None)
    items = {"slack": add_item(queue, slack), "linear": add_item(queue, lin),
             "handover": add_item(queue, hand), "other": queue.enqueue(None, "turn", {"text": "pong?"})}
    for source, item in items.items():
        assert get_session(conn, item)["source"] == source


def test_task_from_linear_is_linear(conn, queue):
    task = make_task(conn, source="linear")
    cid = add_conversation(conn, task_id=task.id, channel=None, thread_ts=None)
    assert get_session(conn, add_item(queue, cid))["source"] == "linear"


def test_started_at_from_events_and_null_without(conn, queue):
    cid = add_conversation(conn, project=None)
    a = add_item(queue, cid)
    b = add_item(queue, cid)
    started(conn, a, "2026-11-13T10:00:00Z")
    started(conn, a, "2026-11-13T11:00:00Z")
    assert get_session(conn, a)["started_at"] == "2026-11-13T10:00:00Z"
    assert get_session(conn, b)["started_at"] is None


def test_finished_row_has_run_numbers(conn, queue):
    task = make_task(conn)
    tasks.set_fields(conn, task.id, pr_url="https://github.com/o/r/pull/3")
    cid = add_conversation(conn, task_id=task.id)
    item = add_item(queue, cid)
    add_run(conn, item, task_id=task.id)
    conn.execute("UPDATE work_items SET status = 'done' WHERE id = ?", (item,))
    row = get_session(conn, item)
    assert row["account"] == "badger" and row["turns"] == 7 and "cost_usd" not in row
    assert row["tokens"] == {"in": 10, "out": 20, "cache_read": 30, "cache_write": 40}
    assert row["outcome"] == "answered" and row["ended_at"] == "2026-11-13T10:05:00Z"
    assert row["links"]["pr"] == "https://github.com/o/r/pull/3" and row["requester"] == "ann"
    assert row["project"] == "sample" and row["task_id"] == task.id


def test_row_without_conversation_or_run(conn, queue):
    item = queue.enqueue(None, "turn", {})
    row = get_session(conn, item)
    assert row["conversation_id"] is None and row["tokens"] is None and row["account"] is None
    assert row["title"] == "turn" and row["live"] is False
    assert row["links"] == {"slack": None, "linear": None, "pr": None}


def test_unknown_session_is_none(conn):
    assert get_session(conn, "W-nope") is None


def test_payload_never_in_row(conn, queue):
    cid = add_conversation(conn)
    item = add_item(queue, cid, text="go", test_secrets={"K": "s3cr3t-value"},
                    system_parts=["SYSTEM PROMPT"])
    text = json.dumps(get_session(conn, item)) + json.dumps(list_sessions(conn, SessionFilter())[0])
    assert "s3cr3t-value" not in text and "SYSTEM PROMPT" not in text and "payload" not in text


def test_paused_by_latest_stop(conn):
    assert paused_by(conn) is None
    db.set_setting(conn, "paused", "1")
    events.append(conn, "stop_all", task_id=None, actor="ann", data={})
    events.append(conn, "stop_all", task_id=None, actor="tailscale:bob@x.com", data={})
    assert paused_by(conn)["actor"] == "tailscale:bob@x.com"
    db.set_setting(conn, "paused", "0")
    assert paused_by(conn) is None


def test_events_paging_and_types(conn):
    for i in range(5):
        events.append(conn, "stop_all" if i % 2 else "resume_all", task_id=None, actor=f"p{i}", data={"i": i})
    page = list_events(conn, None, 2, None)
    assert [r["sender"] for r in page["rows"]] == ["p4", "p3"] and "data" not in page["rows"][0]
    older = list_events(conn, None, 10, page["rows"][-1]["id"])
    assert [r["sender"] for r in older["rows"]] == ["p2", "p1", "p0"]
    assert [r["sender"] for r in list_events(conn, "stop_all", 10, None)["rows"]] == ["p3", "p1"]
    assert page["types"] == ["resume_all", "stop_all"]


def test_kpi_daily_zero_filled_without_cost(conn, queue):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    task = make_task(conn)
    conn.execute("UPDATE tasks SET created_at = '2026-11-12T03:00:00Z' WHERE id = ?", (task.id,))
    conn.execute("INSERT INTO pr_stats(task_id, repo, pr_number, opened_at, merged_at, updated_at) "
                 "VALUES (?, 'o/r', 1, '2026-11-12T04:00:00Z', '2026-11-13T02:00:00Z', '2026-11-13T02:00:00Z')",
                 (task.id,))
    cid = add_conversation(conn, task_id=task.id)
    add_run(conn, add_item(queue, cid), task_id=task.id, cost=1.25, ended="2026-11-12T05:00:00Z")
    add_run(conn, queue.enqueue(None, "turn", {}), cost=0.25, ended="2026-11-13T01:00:00Z")
    view = kpi_view(conn, 3, None, now, "Asia/Singapore")
    assert [d["date"] for d in view["daily"]] == ["2026-11-11", "2026-11-12", "2026-11-13"]
    by_date = {d["date"]: d for d in view["daily"]}
    assert by_date["2026-11-11"] == {"date": "2026-11-11", "tasks": 0, "prs": 0, "merged": 0}
    assert by_date["2026-11-12"]["tasks"] == 1 and by_date["2026-11-12"]["prs"] == 1
    assert by_date["2026-11-13"]["merged"] == 1
    assert "cost_by_project" not in view and view["projects"] == ["sample"]
    assert not {"cost_usd", "cost_per_task", "cost_per_merged"} & set(view["kpis"])
    assert view["kpis"]["merged"] == 1 and view["kpis"]["merge_rate"] == 1.0
    assert view["kpis"]["to_merge"]["n"] == 1


def test_kpi_view_project_filter(conn, queue):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    make_task(conn)
    make_task(conn, project="other")
    conn.execute("UPDATE tasks SET created_at = '2026-11-13T01:00:00Z'")
    view = kpi_view(conn, 7, "other", now, "UTC")
    assert view["kpis"]["tasks"] == 1 and sum(d["tasks"] for d in view["daily"]) == 1


def test_overview_counts_local_day(conn, queue):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)   # 20:00 in Singapore; the local day began 2026-11-12T16:00Z
    cid = add_conversation(conn, project=None)
    done_today, done_before, failed, running, queued = (add_item(queue, cid) for _ in range(5))
    for item, status, ts in ((done_today, "done", "2026-11-12T17:00:00Z"), (done_before, "done", "2026-11-12T15:00:00Z"),
                             (failed, "failed", "2026-11-13T01:00:00Z"), (running, "claimed", "2026-11-13T11:00:00Z")):
        conn.execute("UPDATE work_items SET status = ?, updated_at = ? WHERE id = ?", (status, ts, item))
    assert overview_counts(conn, now, "Asia/Singapore") == {"running": 1, "queued": 1, "done_today": 1,
                                                           "failed_today": 1}


def test_event_sources(conn, queue):
    slack = add_conversation(conn, project=None)
    item = add_item(queue, slack, actor="ann")
    lin = add_conversation(conn, project=None, channel=None, thread_ts=None)
    db.set_setting(conn, "linear_session:x", lin)
    lin_item = add_item(queue, lin, actor=None)
    task = make_task(conn, source="linear", requester=None)
    cases = [
        ("session_started", None, {"work_item": item}, None, ("slack", "ann")),
        ("session_started", None, {"work_item": lin_item}, None, ("linear", None)),
        ("stop_all", "tailscale:x@y", {"via": "dashboard"}, None, ("dashboard", "tailscale:x@y")),
        ("stop_all", "local", {"cancelled": []}, None, ("command line", None)),
        ("status_changed", "agent", {}, task.id, ("linear", None)),
        ("session_started", "ann", {"args": "x"}, None, ("slack", "ann")),
        ("mystery", None, None, None, ("other", None)),
    ]
    conn.execute("DELETE FROM events")
    for type, actor, data, task_id, _ in cases:
        events.append(conn, type, task_id=task_id, actor=actor, data=data)
    rows = list_events(conn, None, 100, None)["rows"][::-1]
    assert [(r["source"], r["sender"]) for r in rows] == [c[-1] for c in cases]
