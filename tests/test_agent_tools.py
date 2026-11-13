import pytest

from agent import db, tasks
from agent.core.agent_tools import AgentToolRouter, ToolReply, ToolSpec
from agent.core.conversations import record_message
from agent.core.sessions import AGENT_QUEUE, WorkQueue
from agent.gitutil import branch_name
from agent.memory.store import MemoryStore
from agent.tasks import Status
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def router(conn, tmp_path):
    return AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                           memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                           secret_values=["SUPERSECRETVALUE"])


def ctx_for(router, *, sandbox=AGENT_QUEUE, actor="ann", task_id=None, unattended=False, channel="C1"):
    cid = add_conversation(router.conn, sandbox=sandbox, task_id=task_id, channel=channel)
    return router.context_for(add_item(router.queue, cid, sandbox, actor=actor, unattended=unattended))


def new_task(conn, status=Status.working):
    t = tasks.create_task(conn, project="sample", title="t", description="d", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann", slack_channel="C1", slack_thread_ts="1.0")
    if status != Status.queued:
        tasks.transition(conn, t.id, Status.working, actor="x")
    return t.id


@pytest.fixture
def ctx_member(router):
    return ctx_for(router, actor="bob")


@pytest.fixture
def ctx_general(router):
    return ctx_for(router, channel="D-ann")


@pytest.fixture
def ctx_with_task(router):
    return ctx_for(router, task_id=new_task(router.conn))


def convo_waiting(conn):
    return conn.execute("SELECT MAX(waiting) FROM conversations").fetchone()[0]


def latest_item(router):
    item_id = router.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    return router.queue.get(item_id)


async def test_member_cannot_resume_all(router, ctx_member):
    assert (await router.call(ctx_member, "resume_all", {})).text == "not available here"


async def test_tiers_from_actor(router):
    assert ctx_for(router, actor="ann").tier == "admin"
    assert ctx_for(router, actor="bob").tier == "act"
    assert ctx_for(router, actor=None, unattended=True).tier == "act"
    assert ctx_for(router, actor="stranger").tier == "read"


async def test_context_fields(router, ctx_member):
    assert (ctx_member.project, ctx_member.sandbox) == ("sample", AGENT_QUEUE)
    assert (ctx_member.channel, ctx_member.thread_ts) == ("C1", "1.0")


async def test_manifest_has_no_sandbox_filtering(router):
    """Every tool is offered by tier only now (no profiles): an arbitrary sandbox label still gets the full
    act-tier tool set, track_task included."""
    ctx = ctx_for(router, sandbox="anything-goes")
    names = {t["name"] for t in router.manifest(ctx)}
    assert "track_task" in names
    assert all({"name", "description", "input_schema"} <= set(t) for t in router.manifest(ctx))


async def test_manifest_filters_by_tier(router, ctx_member, ctx_with_task):
    assert "resume_all" not in {t["name"] for t in router.manifest(ctx_member)}
    assert "resume_all" in {t["name"] for t in router.manifest(ctx_with_task)}


async def test_unknown_tool(router, ctx_member):
    assert (await router.call(ctx_member, "rm_rf", {})).text == "not available here"


async def test_ask_marks_waiting(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "ask", {"question": "which env?"})
    assert tasks.get_task(conn, ctx_with_task.task_id).status == Status.needs_input
    assert convo_waiting(conn) == 1
    assert router.notifier.asked == [(ctx_with_task.task_id, "which env?")]


async def test_ask_without_task_posts_in_thread(router, ctx_member):
    await router.call(ctx_member, "ask", {"question": "which repo?"})
    assert router.slack.posts[-1]["text"] == "which repo?" and router.slack.posts[-1]["thread_ts"] == "1.0"


async def test_post_update_records_its_message(router, ctx_member, conn):
    await router.call(ctx_member, "post_update", {"text": "halfway there"})
    rows = conn.execute("SELECT channel, ts, conversation_id FROM slack_messages").fetchall()
    assert [tuple(r) for r in rows] == [("C1", "101.0", ctx_member.conversation_id)]


async def test_post_update_is_scrubbed(router, ctx_member):
    await router.call(ctx_member, "post_update", {"text": "token SUPERSECRETVALUE here"})
    assert "SUPERSECRETVALUE" not in router.slack.posts[-1]["text"]
    assert "SUPERSECRETVALUE" not in router.slack.posts[-1]["blocks"][0]["text"]


async def test_replies_are_scrubbed(router, ctx_member):
    async def leaky(ctx, args):
        return ToolReply("the value is SUPERSECRETVALUE")
    router.register(ToolSpec("leaky", "d", {}, "read"), leaky)
    assert "SUPERSECRETVALUE" not in (await router.call(ctx_member, "leaky", {})).text


async def test_track_task_creates_working_task_once(router, ctx_member, conn):
    r = await router.call(ctx_member, "track_task", {"project": "sample", "title": "Fix export", "kind": "bug"})
    task_id = conn.execute("SELECT task_id FROM conversations WHERE id = ?", (ctx_member.conversation_id,)).fetchone()[0]
    assert task_id in r.text and tasks.get_task(conn, task_id).status == Status.working
    assert tasks.get_task(conn, task_id).requester == "bob"
    again = router.context_for(ctx_member.work_item_id)
    assert "already" in (await router.call(again, "track_task", {"project": "sample", "title": "x",
                                                                  "kind": "bug"})).text.lower()


async def test_track_task_refuses_other_project(router, ctx_member, conn):
    """A conversation bound to one project (up front or by work_on) can't be rebound to another by track_task."""
    router.packs["other"] = make_pack("other", repo="o/other")
    reply = await router.call(ctx_member, "track_task", {"project": "other", "title": "t", "kind": "bug"})
    assert reply.text == "this conversation is bound to sample"
    row = conn.execute("SELECT project, task_id FROM conversations WHERE id = ?",
                       (ctx_member.conversation_id,)).fetchone()
    assert tuple(row) == ("sample", None) and tasks.list_tasks(conn) == []


async def test_work_on_lists_the_packs_and_is_handled_by_the_runner(conn, tmp_path):
    router = AgentToolRouter(conn, config=make_config(tmp_path), queue=WorkQueue(conn),
                             packs={"sample": make_pack(), "other": make_pack("other", repo="o/other")},
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    ctx = ctx_for(router, actor="bob")
    spec = next(t for t in router.manifest(ctx) if t["name"] == "work_on")
    assert "sample (o/r)" in spec["description"] and "other (o/other)" in spec["description"]
    assert spec["input_schema"]["required"] == ["project"] and "title" in spec["input_schema"]["properties"]
    assert (await router.call(ctx, "work_on", {"project": "sample"})).text == "work_on is handled by the runner"
    assert "work_on" not in {t["name"] for t in router.manifest(ctx_for(router, actor="stranger"))}


async def test_track_task_unknown_project(router):
    ctx = router.context_for(add_item(router.queue, add_conversation(router.conn, sandbox=AGENT_QUEUE, project=None),
                                      AGENT_QUEUE, actor="bob"))
    assert "unknown project" in (await router.call(ctx, "track_task", {"project": "nope", "title": "t",
                                                                       "kind": "bug"})).text


async def test_finish_task(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "finish_task", {"outcome": "answered the question"})
    assert tasks.get_task(conn, ctx_with_task.task_id).status == Status.done


async def test_start_task_queues_code_session(router, ctx_general, conn):
    await router.call(ctx_general, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    item = latest_item(router)
    assert item.sandbox == AGENT_QUEUE and item.payload["text"] == "b"
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert (row["channel"], row["thread_ts"], row["is_primary"]) == ("D-ann", "1.0", 0)
    assert tasks.get_task(conn, row["task_id"]).title == "fix x"
    assert row["branch"] == branch_name("agent", row["task_id"], "fix x") and row["workdir"] == "checkout"


async def test_daily_task_limit(router, ctx_general, conn):
    for _ in range(20):
        new_task(conn, status=Status.queued)
    assert "daily task limit" in (await router.call(ctx_general, "track_task",
                                                    {"project": "sample", "title": "t", "kind": "bug"})).text


async def test_stop_all_and_resume(router, ctx_member, ctx_with_task, conn):
    other = add_item(router.queue, "V-x", "debug-sample")
    router.queue.claim("debug-sample")
    r = await router.call(ctx_member, "stop_all", {})
    assert db.get_setting(conn, "paused") == "1" and router.queue.get(other).status == "cancelled"
    assert "admin" in r.text
    await router.call(ctx_with_task, "resume_all", {})
    assert db.get_setting(conn, "paused") == "0"


async def test_cancel_task_cancels_its_items(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "cancel_task", {"task_id": ctx_with_task.task_id})
    assert tasks.get_task(conn, ctx_with_task.task_id).status == Status.cancelled
    assert router.queue.get(ctx_with_task.work_item_id).status == "cancelled"


async def test_status_and_tasks(router, ctx_with_task):
    assert "running" in (await router.call(ctx_with_task, "status", {})).text
    assert ctx_with_task.task_id in (await router.call(ctx_with_task, "list_tasks", {})).text
    assert "working" in (await router.call(ctx_with_task, "get_task", {"task_id": ctx_with_task.task_id})).text


async def test_read_thread(router, ctx_member):
    router.slack.replies = [{"user": "U1", "text": "the export crashes"}, {"user": "U2", "text": "since monday"}]
    text = (await router.call(ctx_member, "read_thread", {})).text
    assert "ann: the export crashes" in text and "bob: since monday" in text


async def test_read_thread_in_a_dm_reads_the_chat_since_it_started(router, conn):
    """A DM chat has no thread: read_thread gives its top-level messages from its first recorded one on."""
    cid = add_conversation(conn, sandbox=AGENT_QUEUE, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid, AGENT_QUEUE))
    assert (await router.call(ctx, "read_thread", {})).text == "no Slack messages in this conversation yet"
    for ts in ("10.0", "11.0", "12.0", "13.0"):  # two turns: the person's message and the reply, twice
        record_message(conn, "D-ann", ts, cid)
    router.slack.history = [
        {"ts": "13.0", "user": "UBOT", "bot_id": "B1", "text": "logged in"},
        {"ts": "9.0", "user": "U1", "text": "from an earlier chat"},
        {"ts": "11.0", "user": "UBOT", "bot_id": "B1", "text": "here's the device code"},
        {"ts": "10.0", "user": "U1", "text": "login yourself to azure cli"},
        {"ts": "12.0", "user": "U1", "text": "done"},
    ]
    assert (await router.call(ctx, "read_thread", {})).text == (
        "ann: login yourself to azure cli\nagent: here's the device code\nann: done\nagent: logged in")


async def test_memory_tools(router, ctx_member):
    assert (await router.call(ctx_member, "remember", {"text": "prefers small PRs", "about": "me"})).text == "noted"
    assert router.memory.inbox_for(ctx_member.work_item_id)[0]["text"] == "prefers small PRs"
    assert (await router.call(ctx_member, "read_memory", {"id": "missing"})).text == "not found"


async def test_post_files_is_in_the_manifest(router, ctx_member):
    assert "post_files" in {t["name"] for t in router.manifest(ctx_member)}
    assert "runner" in (await router.call(ctx_member, "post_files", {"paths": [], "text": ""})).text


async def test_stop_all_keeps_queued_turns_and_tells_the_thread(router, ctx_member, conn):
    running = add_item(router.queue, add_conversation(conn, sandbox="code-sample", thread_ts="8.0"), "code-sample")
    router.queue.claim("code-sample")
    queued = add_item(router.queue, add_conversation(conn, sandbox="code-sample", thread_ts="9.0"), "code-sample")
    await router.call(ctx_member, "stop_all", {})
    assert router.queue.get(queued).status == "queued"
    stopped = [p for p in router.slack.posts if "Stopped" in p["text"]]
    assert stopped and stopped[0]["thread_ts"] in ("1.0", "8.0")
    assert running
