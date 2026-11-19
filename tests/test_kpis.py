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


def task(conn, n, source, started, secs, finished, project=None):
    record = {"id": f"TK-{n:04d}", "title": f"task {n}", "source": source, "started_at": at(started),
              "project": project}
    conn.execute("INSERT INTO task_records(id, source, project, requested_by, started_at, ended_at, work_time_s, "
                 "finished, record) VALUES (?, ?, ?, 'ann', ?, ?, ?, ?, ?)",
                 (f"TK-{n:04d}", source, project, at(started), at(started), secs,
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
    task(conn, 1, "slack", "10:00", 600, True, "chilliclaw")
    task(conn, 2, "slack", "11:00", 1800, False)
    task(conn, 3, "linear", "12:00", 60, True, "chilliclaw")
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
    assert k.by_project == {"chilliclaw": 2, "general": 1}
    assert k.time == Stat(3, 600, 1800)
    assert (k.finished, k.finished_rate) == (2, 2 / 3)
    assert (k.merged, k.issues_closed) == (1, 1)


def test_format_kpis_exact(seeded):
    assert format_kpis(compute_kpis(seeded, since=SINCE, until=UNTIL), "Jensen") == (
        "Jensen, last 1 day\n"
        "Tasks: 3 (chilliclaw 2 · general 1)\n"
        "Time per task: median 10m · p90 30m\n"
        "Finished: 67% (2 of 3)\n"
        "Shipped: 1 PR merged · 1 Linear issue closed\n"
        "Tokens: 0 in · 0 out · 0 cache read · 0 cache write")


def test_format_kpis_empty(conn):
    text = format_kpis(compute_kpis(conn, since=SINCE, until=UNTIL), "Jensen")
    assert "Tasks: 0\n" in text
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
    assert "Tokens today:" in text and "Tokens last 7 days:" in text and "Tokens this month:" in text


def test_projects_come_first_by_count_and_general_last(conn):
    task(conn, 1, "slack", "10:00", 60, True)
    task(conn, 2, "slack", "10:01", 60, True)
    task(conn, 3, "slack", "10:02", 60, True, "beta")
    task(conn, 4, "slack", "10:03", 60, True, "alpha")
    task(conn, 5, "slack", "10:04", 60, True, "alpha")
    k = compute_kpis(conn, since=SINCE, until=UNTIL)
    assert list(k.by_project.items()) == [("alpha", 2), ("beta", 1), ("general", 2)]
    assert "Tasks: 5 (alpha 2 · beta 1 · general 2)" in format_kpis(k)


def run(conn, rid, project, started, tokens=(100, 10, 1000, 50), day=13):
    conn.execute("INSERT INTO runs(id, kind, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                 "cost_usd_est, started_at, ended_at, project) VALUES (?, 'turn', ?, ?, ?, ?, 9.99, ?, ?, ?)",
                 (rid, *tokens, at(started, day), at(started, day), project))


def test_a_days_projects_add_up_to_its_run_tokens(conn):
    from agent.core.kpis import Usage, tokens_by_project, total

    run(conn, "R1", "alpha", "10:00", (100, 10, 1000, 50))
    run(conn, "R2", "alpha", "11:00", (1, 2, 3, 4))
    run(conn, "R3", "beta", "12:00", (5, 500, 0, 0))
    run(conn, "R4", None, "13:00", (7, 7, 7, 7))
    run(conn, "R5", "alpha", "13:00", (9, 9, 9, 9), day=12)          # the day before: outside the window
    usage = tokens_by_project(conn, since=SINCE, until=UNTIL)
    assert list(usage) == ["beta", "alpha", "general"]               # most output first, general last
    assert usage["alpha"] == Usage(2, 101, 12, 1003, 54)
    day = conn.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
                       "SUM(cache_write_tokens) FROM runs WHERE started_at >= ? AND started_at < ?",
                       (at("00:00"), at("00:00", 14))).fetchone()
    assert total(usage.values()) == Usage(*day)


def test_format_kpis_shows_tokens_by_project_and_no_cost(conn):
    run(conn, "R1", "alpha", "10:00", (1500, 20, 2_000_000, 0))
    run(conn, "R2", None, "11:00", (5, 5, 5, 5))
    text = format_kpis(compute_kpis(conn, since=SINCE, until=UNTIL))
    assert text.endswith("Tokens: 1.5k in · 25 out · 2M cache read · 5 cache write\n"
                         "  alpha: 1.5k in · 20 out · 2M cache read · 0 cache write\n"
                         "  general: 5 in · 5 out · 5 cache read · 5 cache write")
    assert "$" not in text and "cost" not in text.lower()


def test_usage_windows_today_7d_month_and_the_apps_meter(conn):
    from agent.core.kpis import AppUsage, Usage, usage_windows

    run(conn, "R1", "alpha", "02:00", day=13)     # 10:00 on the 13th in Singapore: today
    run(conn, "R2", "alpha", "02:00", day=10)     # this week
    run(conn, "R3", None, "02:00", day=2)         # this month, not this week
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, "
                 "cost_usd) VALUES (?, 'alpha-foundry', 'm', 200, 10, 2, 3, 0.25)", (at("03:00"),))
    w = usage_windows(conn, datetime(2026, 11, 13, 4, 0, tzinfo=UTC), "Asia/Singapore")
    assert w["today"]["since"] == "2026-11-12T16:00:00Z" and w["month"]["since"] == "2026-10-31T16:00:00Z"
    assert {k: v["total"].runs for k, v in w.items()} == {"today": 1, "7d": 2, "month": 3}
    assert w["month"]["by_project"]["general"] == Usage(1, 100, 10, 1000, 50)
    assert w["today"]["apps_by_upstream"] == {"alpha-foundry": AppUsage(1, 10, 3, 2, 0, 0.25)}


def test_blank_project_counts_as_general_and_a_project_named_general_merges(conn):
    from agent.core.kpis import Usage, tokens_by_project

    run(conn, "R1", "", "10:00", (1, 1, 1, 1))
    run(conn, "R2", None, "10:00", (2, 2, 2, 2))
    run(conn, "R3", "general", "10:00", (4, 4, 4, 4))
    assert tokens_by_project(conn, since=SINCE, until=UNTIL) == {"general": Usage(3, 7, 7, 7, 7)}


def test_format_usage_windows_lists_projects_and_the_apps_meter(conn):
    from agent.core.kpis import format_usage_windows, usage_windows

    run(conn, "R1", "alpha", "02:00")
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, "
                 "cost_usd) VALUES (?, 'alpha-foundry', 'm', 200, 10, 2, 3, 0.25)", (at("03:00"),))
    text = format_usage_windows(usage_windows(conn, datetime(2026, 11, 13, 4, 0, tzinfo=UTC), "UTC"))
    assert text.splitlines()[:3] == [
        "Tokens today: 100 in · 10 out · 1k cache read · 50 cache write",
        "  alpha: 100 in · 10 out · 1k cache read · 50 cache write",
        "  app meter, alpha-foundry: 1 call · 10 in · 3 out · 2 cache read · 0 cache write · $0.25 billed"]
