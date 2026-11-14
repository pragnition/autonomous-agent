import json

import pytest

from agent import db, tasks
from agent.tasks import IllegalTransition, Status


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def mk(conn, **kw):
    args = dict(project="demo", title="Add copy button", description="Copy citations", kind="feature",
                source="slack", source_ref="C1/1.0", requester="ann", owner="ann")
    args.update(kw)
    return tasks.create_task(conn, **args)


def events(conn):
    return [(r["type"], json.loads(r["data"]) if r["data"] else None)
            for r in conn.execute("SELECT type, data FROM events ORDER BY id")]


def test_create_task_assigns_sequential_id_and_event(conn):
    t = mk(conn)
    assert t.id == "T-0001"
    assert t.status == Status.queued
    assert [e[0] for e in events(conn)] == ["task_created"]
    assert mk(conn).id == "T-0002"


def test_legal_transition_records_event(conn):
    t = mk(conn)
    t = tasks.transition(conn, t.id, Status.working, actor="dispatcher")
    assert t.status == Status.working
    assert events(conn)[-1] == ("status_changed", {"from": "queued", "to": "working"})


def test_illegal_transition_raises_and_records_nothing(conn):
    t = mk(conn)
    before = len(events(conn))
    with pytest.raises(IllegalTransition):
        tasks.transition(conn, t.id, Status.merged, actor="x")
    assert len(events(conn)) == before
    assert tasks.get_task(conn, t.id).status == Status.queued


def test_terminal_cannot_be_cancelled(conn):
    t = mk(conn)
    for s in (Status.working, Status.ready_for_review, Status.merged):
        tasks.transition(conn, t.id, s, actor="x")
    with pytest.raises(IllegalTransition):
        tasks.transition(conn, t.id, Status.cancelled, actor="x")


def test_failed_can_be_requeued(conn):
    t = mk(conn)
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.failed, actor="x")
    assert tasks.transition(conn, t.id, Status.queued, actor="ann").status == Status.queued


def test_needs_input_can_be_requeued(conn):
    t = mk(conn)
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    assert tasks.transition(conn, t.id, Status.queued, actor="ann").status == Status.queued


def test_transition_extra_data_merged_into_event(conn):
    t = mk(conn)
    tasks.transition(conn, t.id, Status.working, actor="x", data={"run_id": "R1"})
    assert events(conn)[-1][1] == {"from": "queued", "to": "working", "run_id": "R1"}


def test_list_tasks_filters(conn):
    a = mk(conn)
    mk(conn, project="other")
    tasks.transition(conn, a.id, Status.working, actor="x")
    assert [t.id for t in tasks.list_tasks(conn, status=Status.working)] == [a.id]
    assert [t.project for t in tasks.list_tasks(conn, project="other")] == ["other"]
    assert len(tasks.list_tasks(conn)) == 2


def test_connection_sets_wal_and_busy_timeout(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_settings_roundtrip(conn):
    assert db.get_setting(conn, "paused", "0") == "0"
    db.set_setting(conn, "paused", "1")
    assert db.get_setting(conn, "paused") == "1"


def test_set_fields_updates_and_rejects_protected(conn):
    t = mk(conn)
    t2 = tasks.set_fields(conn, t.id, pr_number=7, branch="jensen/T-0001-x")
    assert (t2.pr_number, t2.branch) == (7, "jensen/T-0001-x")
    for bad in ({"status": "merged"}, {"id": "T-9"}, {"nope": 1}):
        with pytest.raises(ValueError):
            tasks.set_fields(conn, t.id, **bad)


def test_status_edges(conn):
    t = mk(conn)
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.done, actor="x")
    with pytest.raises(IllegalTransition):
        tasks.transition(conn, t.id, Status.working, actor="x")


def test_cancel_from_any_open_state(conn):
    t = mk(conn)
    tasks.transition(conn, t.id, Status.working, actor="x")
    assert tasks.transition(conn, t.id, Status.cancelled, actor="x").status == Status.cancelled


def test_review_can_go_back_to_working(conn):
    t = mk(conn)
    for s in (Status.working, Status.ready_for_review, Status.working, Status.ready_for_review, Status.closed):
        tasks.transition(conn, t.id, s, actor="x")
    assert tasks.get_task(conn, t.id).status == Status.closed

