import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import db, tasks
from agent.core.consolidation import ConsolidationService
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent.tasks import Status
from agent_fixtures import add_conversation

OUT = {"episode": {"summary": "Added copy button", "body": "Opened PR #7; merged."},
       "upserts": [{"scope": "projects/sample", "kind": "fact", "summary": "Tests need NODE_ENV=test", "body": "b"}]}
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


class FakePayloads:
    def chore(self, prompt, schema):
        return {"text": prompt, "structured": schema}


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    memory = MemoryStore(tmp_path / "memory.db")
    queue = WorkQueue(conn)
    svc = ConsolidationService(conn, memory, queue, FakePayloads(), secret_values=[])
    return conn, memory, queue, svc


def merged_task(conn, memory, source="slack"):
    t = tasks.create_task(conn, project="sample", title="Add copy button", description="Copy citations",
                          kind="feature", source=source, source_ref="C1/1", requester="alice", owner="alice")
    for s in (Status.working, Status.ready_for_review, Status.merged):
        tasks.transition(conn, t.id, s, actor="x")
    cid = add_conversation(conn, task_id=t.id)
    item = WorkQueue(conn).enqueue(cid, "turn", {"text": "add a copy button"})
    memory.add_inbox(item, "alice", "project", "Citations live in src/cite.ts")
    return tasks.get_task(conn, t.id)


def chores(conn):
    return conn.execute("SELECT * FROM work_items WHERE kind = 'chore' ORDER BY rowid").fetchall()


async def finish(svc, queue, item_id, result):
    queue.complete(item_id, result)
    await svc.on_result(queue.get(item_id), result)


async def test_terminal_task_enqueues_one_chore(env):
    conn, memory, queue, svc = env
    task = merged_task(conn, memory)
    assert svc.sweep() == [task.id]
    assert svc.sweep() == []
    [chore] = chores(conn)
    payload = json.loads(chore["payload"])
    assert chore["conversation_id"] is None and "Citations live in src/cite.ts" in payload["text"]
    assert payload["consolidate"] == {"task_id": task.id}


async def test_result_applies_notes_once(env):
    conn, memory, queue, svc = env
    task = merged_task(conn, memory)
    svc.sweep()
    await finish(svc, queue, chores(conn)[0]["id"], {"structured": OUT})
    assert sorted(n.kind for n in memory.candidates(["projects/sample"])) == ["episode", "fact"]
    assert svc.sweep() == []
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'consolidated' AND task_id = ?",
                        (task.id,)).fetchone()[0] == 1


async def test_json_in_text_is_accepted(env):
    conn, memory, queue, svc = env
    merged_task(conn, memory)
    svc.sweep()
    await finish(svc, queue, chores(conn)[0]["id"], {"text": "```json\n" + json.dumps(OUT) + "\n```"})
    assert len(memory.candidates(["projects/sample"])) == 2


async def test_linear_task_notes_are_external(env):
    conn, memory, queue, svc = env
    merged_task(conn, memory, source="linear")
    svc.sweep()
    await finish(svc, queue, chores(conn)[0]["id"], {"structured": OUT})
    assert {n.trust for n in memory.candidates(["projects/sample"])} == {"external"}


async def test_failed_chore_is_retried_next_sweep(env):
    conn, memory, queue, svc = env
    task = merged_task(conn, memory)
    svc.sweep()
    await finish(svc, queue, chores(conn)[0]["id"], {"text": "not json", "outcome": "error"})
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'consolidation_failed'").fetchone()[0] == 1
    assert svc.sweep() == [task.id]


async def test_gives_up_after_three_failures(env):
    conn, memory, queue, svc = env
    merged_task(conn, memory)
    for _ in range(3):
        svc.sweep()
        await finish(svc, queue, chores(conn)[-1]["id"], {"text": "nope"})
    assert svc.sweep() == []


async def test_open_tasks_not_consolidated(env):
    conn, _, _, svc = env
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="a", owner="a")
    tasks.transition(conn, t.id, Status.working, actor="x")
    assert svc.sweep() == []


def idle_thread(conn, memory, last_active, with_note=True):
    cid = add_conversation(conn, channel="C1", thread_ts="1.1")
    conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (last_active, cid))
    item = WorkQueue(conn).enqueue(cid, "turn", {"text": "x"})
    if with_note:
        memory.add_inbox(item, "alice", "channel", "We agreed to drop the bulk upload API")
    return cid


async def test_idle_thread_with_notes_consolidated_once(env):
    conn, memory, queue, svc = env
    cid = idle_thread(conn, memory, "2026-11-12T08:00:00Z")
    assert svc.sweep_threads(NOW) == 1
    assert svc.sweep_threads(NOW) == 0
    out = {"episode": {"summary": "Upload API discussion", "body": "Agreed to drop the bulk upload API."}, "upserts": [
        {"scope": "channels/C1", "kind": "fact", "summary": "The bulk upload API is being dropped", "body": "b"}]}
    await finish(svc, queue, chores(conn)[0]["id"], {"structured": out})
    assert sorted(n.kind for n in memory.candidates(["channels/C1"])) == ["episode", "fact"]
    assert conn.execute("SELECT consolidated_at FROM conversations WHERE id = ?", (cid,)).fetchone()[0] is not None


async def test_idle_thread_without_notes_marked_without_chore(env):
    conn, memory, _, svc = env
    cid = idle_thread(conn, memory, "2026-11-12T08:00:00Z", with_note=False)
    assert svc.sweep_threads(NOW) == 0 and chores(conn) == []
    assert conn.execute("SELECT consolidated_at FROM conversations WHERE id = ?", (cid,)).fetchone()[0] is not None


async def test_active_thread_not_consolidated(env):
    conn, memory, _, svc = env
    idle_thread(conn, memory, (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert svc.sweep_threads(NOW) == 0 and chores(conn) == []
