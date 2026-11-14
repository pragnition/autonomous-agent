import json
from datetime import UTC, datetime

import pytest

from agent import db
from agent.core.kpis import Stat, compute_kpis, format_duration, format_kpis

SINCE, UNTIL = datetime(2026, 11, 13, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def at(hhmm):
    return f"2026-11-13T{hhmm}:00Z"


def task(conn, tid, *, source="linear", project="sample", status="working", created="10:00"):
    conn.execute("INSERT INTO tasks(id, project, title, kind, source, status, created_at, updated_at) "
                 "VALUES (?, ?, 't', 'bug', ?, ?, ?, ?)", (tid, project, source, status, at(created), at(created)))


def started(conn, tid, hhmm, queued="09:59", project="sample"):
    conn.execute("INSERT INTO events(ts, task_id, type, data) VALUES (?, ?, 'session_started', ?)",
                 (at(hhmm), tid, json.dumps({"work_item": "W", "queued_at": at(queued), "project": project})))


def pr(conn, tid, opened, merged=None):
    conn.execute("INSERT INTO pr_stats(task_id, repo, pr_number, opened_at, merged_at, updated_at) "
                 "VALUES (?, 'o/r', 1, ?, ?, ?)", (tid, at(opened), at(merged) if merged else None, at(opened)))


def run(conn, rid, tid, cost):
    conn.execute("INSERT INTO runs(id, task_id, kind, status, cost_usd_est, started_at, ended_at) "
                 "VALUES (?, ?, 'code', 'finished', ?, ?, ?)", (rid, tid, cost, at("10:00"), at("14:00")))


@pytest.fixture
def seeded(conn):
    task(conn, "T1"), started(conn, "T1", "10:05", queued="10:04"), pr(conn, "T1", "11:05", merged="13:05")
    run(conn, "R1", "T1", 1.5)
    task(conn, "T2"), started(conn, "T2", "10:15", queued="10:14"), pr(conn, "T2", "12:15")
    run(conn, "R2", "T2", 0.5)
    task(conn, "T3", source="slack", status="failed"), started(conn, "T3", "10:01", queued="10:00")
    return conn


def test_kpis_from_seeded_history(seeded):
    k = compute_kpis(seeded, since=SINCE, until=UNTIL)
    assert k.pickup == Stat(2, 600, 900)
    assert k.to_pr == Stat(2, 5400, 7200)
    assert k.to_merge == Stat(1, 7200, 7200)
    assert k.queue_wait == Stat(3, 60, 60)
    assert (k.tasks, k.prs, k.merged, k.closed, k.reverted, k.failed, k.cancelled) == (3, 2, 1, 0, 0, 1, 0)
    assert k.merge_rate == 0.5 and k.cost_usd == 2.0 and k.cost_per_merged == 1.5


def test_project_filter(seeded):
    seeded.execute("UPDATE tasks SET project = 'other' WHERE id = 'T2'")
    k = compute_kpis(seeded, since=SINCE, until=UNTIL, project="sample")
    assert k.prs == 1 and k.tasks == 2 and k.cost_usd == 1.5


def test_empty_window_reports_na(conn):
    k = compute_kpis(conn, since=SINCE, until=UNTIL)
    assert k.pickup == k.to_pr == k.to_merge == k.queue_wait == Stat(0, None, None)
    assert k.cost_per_task is None and k.cost_per_merged is None and k.merge_rate is None
    assert "n/a" in format_kpis(k)


def test_runs_without_a_task_count_only_in_total(conn):
    run(conn, "R9", None, 1.0)
    assert compute_kpis(conn, since=SINCE, until=UNTIL).cost_usd == 1.0
    assert compute_kpis(conn, since=SINCE, until=UNTIL, project="sample").cost_usd == 0


def test_format_duration():
    assert [format_duration(s) for s in (600, 5400, 90000, 20)] == ["10m", "1h 30m", "1d 1h", "0m"]


def test_format_kpis(seeded):
    text = format_kpis(compute_kpis(seeded, since=SINCE, until=UNTIL))
    assert "Ticket → pickup: median 10m, p90 15m (n=2)" in text and "Merge rate: 50%" in text
    assert "last 1 days" in text or "last 1 day" in text


async def test_kpis_tool(conn, tmp_path):
    from agent.core.agent_tools import AgentToolRouter
    from agent.core.kpis import register_kpi_tools
    from agent.core.sessions import WorkQueue
    from agent.memory.store import MemoryStore
    from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

    router = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    register_kpi_tools(router)
    cid = add_conversation(conn, channel="C1")
    ctx = router.context_for(add_item(router.queue, cid, actor="bob"))
    assert "Ticket → pickup" in (await router.call(ctx, "kpis", {"days": 7})).text
    assert "sample" in (await router.call(ctx, "kpis", {"project": "nope"})).text


def test_kpi_queue_wait_filters_by_project(conn):
    """One queue serves every project, so the queue-wait filter goes by the event's own "project" field."""
    task(conn, "T1", project="sample")
    started(conn, "T1", "10:05", queued="10:04", project="sample")
    task(conn, "T2", project="other")
    started(conn, "T2", "10:15", queued="10:00", project="other")
    k = compute_kpis(conn, since=SINCE, until=UNTIL, project="sample")
    assert k.queue_wait == Stat(1, 60, 60)


def test_pickup_to_pr_for_slack_tasks_starts_at_creation(conn):
    task(conn, "T5", source="slack", created="10:00")
    pr(conn, "T5", "11:00")
    started(conn, "T5", "14:00", queued="13:59")          # a follow-up turn in the thread, after the PR
    assert compute_kpis(conn, since=SINCE, until=UNTIL).to_pr == Stat(1, 3600, 3600)
