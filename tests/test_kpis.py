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


def at(hhmm, day=13):
    return f"2026-11-{day:02d}T{hhmm}:00Z"


def task(conn, n, source, started, secs, finished):
    record = {"id": f"TK-{n:04d}", "title": f"task {n}", "source": source, "started_at": at(started)}
    conn.execute("INSERT INTO task_records(id, source, requested_by, started_at, ended_at, work_time_s, finished, "
                 "record) VALUES (?, ?, 'ann', ?, ?, ?, ?, ?)",
                 (f"TK-{n:04d}", source, at(started), at(started), secs,
                  None if finished is None else int(finished), json.dumps(record)))


def pr(conn, n, opened, merged=None):
    conn.execute("INSERT INTO agent_prs(repo, number, opened_at, merged_at, closed_at, updated_at) "
                 "VALUES ('o/r', ?, ?, ?, ?, ?)", (n, at(opened), merged and at(merged), merged and at(merged),
                                                   at(opened)))


def issue(conn, n, completed):
    conn.execute("INSERT INTO linear_issues(issue_id, identifier, handed_at, completed_at) VALUES (?, ?, ?, ?)",
                 (f"i{n}", f"PRA-{n}", at("09:00"), completed and at(completed)))


@pytest.fixture
def seeded(conn):
    task(conn, 1, "slack", "10:00", 600, True)
    task(conn, 2, "slack", "11:00", 1800, False)
    task(conn, 3, "linear", "12:00", 60, True)
    task(conn, 4, "slack", "10:00", 99, True)       # started the day before: outside the window
    conn.execute("UPDATE task_records SET started_at = ? WHERE id = 'TK-0004'", (at("10:00", day=12),))
    pr(conn, 1, "11:00", merged="13:00")
    pr(conn, 2, "11:30")                            # still open
    pr(conn, 3, "09:00", merged="10:00")
    conn.execute("UPDATE agent_prs SET merged_at = ? WHERE number = 3", (at("10:00", day=12),))
    issue(conn, 1, "14:00")
    issue(conn, 2, None)
    return conn


def test_kpis_from_task_records(seeded):
    k = compute_kpis(seeded, since=SINCE, until=UNTIL)
    assert (k.tasks, k.by_source) == (3, {"slack": 2, "linear": 1, "schedule": 0})
    assert k.time == Stat(3, 600, 1800)
    assert (k.finished, k.finished_rate) == (2, 2 / 3)
    assert (k.merged, k.issues_closed) == (1, 1)


def test_format_kpis_exact(seeded):
    assert format_kpis(compute_kpis(seeded, since=SINCE, until=UNTIL), "Jensen") == (
        "Jensen, last 1 day\n"
        "Tasks: 3 (Slack 2 · Linear 1 · schedules 0)\n"
        "Time per task: median 10m · p90 30m\n"
        "Finished: 67% (2 of 3)\n"
        "Shipped: 1 PR merged · 1 Linear issue closed")


def test_format_kpis_empty(conn):
    text = format_kpis(compute_kpis(conn, since=SINCE, until=UNTIL), "Jensen")
    assert "Tasks: 0 (Slack 0 · Linear 0 · schedules 0)" in text
    assert "Time per task: n/a" in text and "Finished: n/a (0 of 0)" in text
    assert "Shipped: 0 PRs merged · 0 Linear issues closed" in text


def test_format_kpis_names_the_days(seeded):
    k = compute_kpis(seeded, since=datetime(2026, 11, 7, tzinfo=UTC), until=UNTIL)
    assert format_kpis(k).startswith("Agent, last 7 days\n")


def test_format_duration():
    assert [format_duration(s) for s in (600, 5400, 90000, 20)] == ["10m", "1h 30m", "1d 1h", "20s"]


async def test_no_claude_cost_anywhere_the_kpis_are_shown(seeded, tmp_path):
    """The digest, the kpis tool and `agent kpis` all print format_kpis: none of them shows a Claude cost."""
    import dataclasses

    from agent.core.kpis import Kpis

    assert not {f.name for f in dataclasses.fields(Kpis)} & {"cost_usd", "cost_per_task", "cost_per_merged"}
    for k in (compute_kpis(seeded, since=SINCE, until=UNTIL), compute_kpis(seeded, since=SINCE, until=SINCE)):
        text = format_kpis(k)
        assert "cost" not in text.lower() and "$" not in text


async def test_kpis_tool(seeded, tmp_path):
    from agent.core.agent_tools import AgentToolRouter, ToolContext
    from agent.core.kpis import register_kpi_tools
    from agent.core.sessions import WorkQueue
    from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

    router = AgentToolRouter(seeded, config=make_config(tmp_path), packs={"sample": make_pack()},
                             queue=WorkQueue(seeded), slack=FakeSlack(), notifier=FakeNotifier(), secret_values=[])
    register_kpi_tools(router)
    cid = add_conversation(seeded, channel="C1")
    ctx = router.context_for(add_item(router.queue, cid, actor="bob"))
    text = (await router.call(ctx, "kpis", {"days": 7})).text
    assert text.startswith("agent, last 7 days\n") and "Tasks:" in text
    assert "whole number" in (await router.call(ctx, "kpis", {"days": 0})).text
    admin = ToolContext(work_item_id="W", conversation_id=None, project=None, actor="ann", tier="admin",
                        unattended=False, task_id=None, channel=None, thread_ts=None)
    [spec] = [s for s in router.manifest(admin) if s["name"] == "kpis"]
    assert "cost" not in spec["description"].lower() and "tasks" in spec["description"]
