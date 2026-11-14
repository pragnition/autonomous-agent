import json
from datetime import UTC, datetime

import pytest

from agent import db
from agent.core.kpis import Stat, compute_kpis, format_duration, format_kpis
from agent.core.linear_issues import record_issue

SINCE, UNTIL = datetime(2026, 11, 13, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def at(hhmm):
    return f"2026-11-13T{hhmm}:00Z"


def issue(conn, n, handed, *, completed=None, canceled=None):
    record_issue(conn, {"createdAt": at(handed), "issue": {"id": f"i{n}", "identifier": f"PRA-{n}"}}, f"V-{n}")
    conn.execute("UPDATE linear_issues SET completed_at = ?, canceled_at = ? WHERE issue_id = ?",
                 (at(completed) if completed else None, at(canceled) if canceled else None, f"i{n}"))


def started(conn, convo, hhmm, queued, item):
    conn.execute("INSERT INTO work_items(id, conversation_id, kind, payload, created_at, updated_at) "
                 "VALUES (?, ?, 'turn', '{}', ?, ?)", (item, convo, at(queued), at(queued)))
    conn.execute("INSERT INTO events(ts, type, data) VALUES (?, 'session_started', ?)",
                 (at(hhmm), json.dumps({"work_item": item, "queued_at": at(queued), "project": None})))


def pr(conn, n, opened, merged=None, closed=None, issue=None):
    conn.execute("INSERT INTO agent_prs(repo, number, opened_at, merged_at, closed_at, issue_identifier, updated_at) "
                 "VALUES ('o/r', ?, ?, ?, ?, ?, ?)", (n, at(opened), at(merged) if merged else None,
                                                     at(closed or merged) if (closed or merged) else None, issue,
                                                     at(opened)))


@pytest.fixture
def seeded(conn):
    issue(conn, 1, "10:00", completed="13:30")
    started(conn, "V-1", "10:05", "10:04", "W1")
    started(conn, "V-1", "12:00", "12:00", "W1b")         # a follow-up turn: pickup stays the first
    pr(conn, 1, "11:05", merged="13:05", issue="PRA-1")
    issue(conn, 2, "10:00", completed="11:00")             # a report: no PR
    started(conn, "V-2", "10:15", "10:14", "W2")
    pr(conn, 2, "12:00")                                   # a PR that names no tracked issue
    return conn


def test_kpis_from_observed_history(seeded):
    k = compute_kpis(seeded, since=SINCE, until=UNTIL)
    assert k.pickup == Stat(2, 600, 900)
    assert k.to_pr == Stat(1, 3600, 3600)
    assert k.to_merge == Stat(1, 7200, 7200)
    assert k.to_done_pr == Stat(1, 12600, 12600)
    assert k.to_done_other == Stat(1, 3600, 3600)
    assert k.queue_wait == Stat(3, 60, 60)
    assert (k.issues, k.done, k.canceled, k.prs, k.merged, k.closed, k.unmatched) == (2, 2, 0, 2, 1, 0, 1)
    assert k.merge_rate == 0.5


def test_canceled_and_closed_unmerged(conn):
    issue(conn, 3, "10:00", canceled="10:30")
    pr(conn, 9, "10:10", closed="10:20", issue="PRA-3")
    k = compute_kpis(conn, since=SINCE, until=UNTIL)
    assert (k.issues, k.done, k.canceled, k.closed, k.merged) == (1, 0, 1, 1, 0)
    assert k.to_done_pr == Stat(0, None, None)


def test_empty_window_reports_na(conn):
    k = compute_kpis(conn, since=SINCE, until=UNTIL)
    assert k.pickup == k.to_pr == k.to_merge == k.queue_wait == Stat(0, None, None)
    assert k.merge_rate is None
    assert "n/a" in format_kpis(k)


async def test_no_claude_cost_anywhere_the_kpis_are_shown(seeded, tmp_path):
    """The digest, the kpis tool and `just kpis` all print format_kpis: none of them shows a Claude cost."""
    import dataclasses

    from agent.core.agent_tools import AgentToolRouter
    from agent.core.kpis import Kpis, register_kpi_tools
    from agent.core.sessions import WorkQueue
    from agent_fixtures import FakeNotifier, FakeSlack, make_config, make_pack

    assert not {f.name for f in dataclasses.fields(Kpis)} & {"cost_usd", "cost_per_task", "cost_per_merged"}
    for k in (compute_kpis(seeded, since=SINCE, until=UNTIL), compute_kpis(seeded, since=SINCE, until=SINCE)):
        text = format_kpis(k)
        assert "cost" not in text.lower() and "$" not in text
    router = AgentToolRouter(seeded, config=make_config(tmp_path), packs={"sample": make_pack()},
                             queue=WorkQueue(seeded), slack=FakeSlack(), notifier=FakeNotifier(), secret_values=[])
    register_kpi_tools(router)
    [spec] = [s for s in router.manifest(router_ctx()) if s["name"] == "kpis"]
    assert "cost" not in spec["description"].lower()


def router_ctx():
    from agent.core.agent_tools import ToolContext
    return ToolContext(work_item_id="W", conversation_id=None, project=None, actor="ann", tier="admin",
                       unattended=False, task_id=None, channel=None, thread_ts=None)


def test_format_duration():
    assert [format_duration(s) for s in (600, 5400, 90000, 20)] == ["10m", "1h 30m", "1d 1h", "0m"]


def test_format_kpis(seeded):
    text = format_kpis(compute_kpis(seeded, since=SINCE, until=UNTIL))
    assert "Ticket → pickup: median 10m, p90 15m (n=2)" in text and "Merge rate: 50%" in text
    assert "Ticket → done (with PR): median 3h 30m" in text and "Ticket → done (no PR): median 1h 0m" in text
    assert "Issues: 2 · done: 2 · canceled: 0" in text
    assert "PRs: 2 · merged: 1 · closed unmerged: 0 · not linked to an issue: 1" in text
    assert "last 1 day" in text


async def test_kpis_tool(conn, tmp_path):
    from agent.core.agent_tools import AgentToolRouter
    from agent.core.kpis import register_kpi_tools
    from agent.core.sessions import WorkQueue
    from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

    router = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    register_kpi_tools(router)
    cid = add_conversation(conn, channel="C1")
    ctx = router.context_for(add_item(router.queue, cid, actor="bob"))
    assert "Ticket → pickup" in (await router.call(ctx, "kpis", {"days": 7})).text
    assert "whole number" in (await router.call(ctx, "kpis", {"days": 0})).text
