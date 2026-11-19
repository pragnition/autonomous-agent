import dataclasses
import json
import re

import pytest

from agent import db, tasks
from agent.core.agent_tools import AgentToolRouter, ToolReply, ToolSpec
from agent.core.conversations import Conversations, record_message
from agent.core.notifier import SlackNotifier
from agent.core.sessions import WorkQueue
from agent.tasks import Status
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack
from test_conversations import reply


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def router(conn, tmp_path):
    return AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                           slack=FakeSlack(), notifier=FakeNotifier(),
                           secret_values=["SUPERSECRETVALUE"])


def ctx_for(router, *, actor="ann", task_id=None, unattended=False, channel="C1"):
    cid = add_conversation(router.conn, task_id=task_id, channel=channel)
    return router.context_for(add_item(router.queue, cid, actor=actor, unattended=unattended))


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


class _FakePayloads:
    def turn(self, convo, text, actor, *, unattended=False):
        return {"text": text, "actor": actor, "unattended": unattended, "model": convo["model"]}


async def test_member_cannot_resume_all(router, ctx_member):
    assert (await router.call(ctx_member, "resume_all", {})).text == "not available here"


async def test_tiers_from_actor(router):
    assert ctx_for(router, actor="ann").tier == "admin"
    assert ctx_for(router, actor="bob").tier == "act"
    assert ctx_for(router, actor=None, unattended=True).tier == "act"
    assert ctx_for(router, actor="stranger").tier == "read"


async def test_context_fields(router, ctx_member):
    assert ctx_member.project == "sample"
    assert (ctx_member.channel, ctx_member.thread_ts) == ("C1", "1.0")


async def test_manifest_offers_tools_by_tier_only(router):
    """Every tool is offered by tier only: the session gets the full tool set for its tier, track_task included,
    and each entry has a name, a description and an input schema."""
    ctx = ctx_for(router)
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


async def test_post_update_returns_its_ts(router, ctx_member):
    assert (await router.call(ctx_member, "post_update", {"text": "status"})).text == "posted (ts 101.0)"


async def test_pin_defaults_to_the_message_being_answered(router):
    ctx = router.context_for(add_item(router.queue, add_conversation(router.conn), actor="bob", message_ts="5.0"))
    assert (await router.call(ctx, "pin", {})).text == "pinned 5.0 in C1"
    assert router.slack.pins == [("add", {"channel": "C1", "timestamp": "5.0"})]


async def test_pin_falls_back_to_the_thread(router, ctx_member):
    assert (await router.call(ctx_member, "pin", {})).text == "pinned 1.0 in C1"


async def test_pin_without_a_channel(router):
    ctx = ctx_for(router, actor="bob", channel=None)
    assert "channel" in (await router.call(ctx, "pin", {"ts": "7.0"})).text
    assert router.slack.pins == []


async def test_pin_and_unpin_a_given_message(router, ctx_member):
    await router.call(ctx_member, "pin", {"ts": "7.0", "channel": "C9"})
    await router.call(ctx_member, "pin", {"ts": "7.0", "channel": "C9", "unpin": True})
    assert router.slack.pins == [("add", {"channel": "C9", "timestamp": "7.0"}),
                                 ("remove", {"channel": "C9", "timestamp": "7.0"})]


async def test_pin_in_another_channel_needs_a_ts(router, ctx_member):
    assert "ts" in (await router.call(ctx_member, "pin", {"channel": "C9"})).text
    assert router.slack.pins == []


async def test_pin_is_not_offered_at_read_tier(router):
    ctx = ctx_for(router, actor="stranger")
    assert "pin" not in {t["name"] for t in router.manifest(ctx)}
    assert (await router.call(ctx, "pin", {"ts": "7.0"})).text == "not available here"


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


async def test_track_task_may_name_another_project_than_the_channels(router, ctx_member, conn):
    """A conversation isn't tied to one project: one in #sample can track work on another repo."""
    router.packs["other"] = make_pack("other", repo="o/other")
    reply = await router.call(ctx_member, "track_task", {"project": "other", "title": "t", "kind": "bug"})
    assert reply.text.startswith("Tracking ")
    row = conn.execute("SELECT project, task_id FROM conversations WHERE id = ?",
                       (ctx_member.conversation_id,)).fetchone()
    assert row["project"] == "other" and tasks.get_task(conn, row["task_id"]).project == "other"


async def test_track_task_unknown_project(router):
    ctx = router.context_for(add_item(router.queue, add_conversation(router.conn, project=None), actor="bob"))
    assert "unknown project" in (await router.call(ctx, "track_task", {"project": "nope", "title": "t",
                                                                       "kind": "bug"})).text


async def test_finish_task(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "finish_task", {"outcome": "answered the question"})
    assert tasks.get_task(conn, ctx_with_task.task_id).status == Status.done


async def test_start_task_queues_code_session(router, ctx_general, conn):
    await router.call(ctx_general, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    item = latest_item(router)
    assert item.payload["text"] == "b"
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert (row["channel"], row["thread_ts"], row["is_primary"]) == ("D-ann", "1.0", 0)
    assert tasks.get_task(conn, row["task_id"]).title == "fix x" and row["project"] == "sample"


async def test_start_task_from_dm_posts_its_own_thread(router, conn):
    """From a DM (thread_ts is None), start_task posts one top-level line and the child lives in its thread."""
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    assert router.slack.posts == [{"channel": "D-ann", "text": "Working on fix x"}]
    child = conn.execute("SELECT * FROM conversations WHERE id = ?", (latest_item(router).conversation_id,)).fetchone()
    assert (child["channel"], child["thread_ts"], child["is_primary"]) == ("D-ann", "101.0", 1)
    assert conn.execute("SELECT conversation_id FROM slack_messages WHERE channel = ? AND ts = ?",
                        ("D-ann", "101.0")).fetchone()[0] == child["id"]


async def test_reply_in_child_thread_reaches_child(router, conn, tmp_path):
    """Routing finds the child by its own thread_ts, the way any primary conversation's thread does."""
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    child = latest_item(router).conversation_id
    thread_ts = conn.execute("SELECT thread_ts FROM conversations WHERE id = ?", (child,)).fetchone()[0]

    convos = Conversations(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=router.queue,
                           slack=router.slack, payloads=_FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT")
    before = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    await convos.handle_slack(reply(thread_ts, "more work", channel="D-ann"))
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == before + 1
    assert latest_item(router).conversation_id == child


async def test_start_task_from_dm_status_stays_in_its_thread(router, conn):
    """With the real SlackNotifier: the task's status message threads under the child's own opening line, never
    a second top-level DM message."""
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))
    router.notifier = SlackNotifier(router.slack, conn)
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})

    child = conn.execute("SELECT * FROM conversations WHERE id = ?", (latest_item(router).conversation_id,)).fetchone()
    task_id = conn.execute("SELECT id FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert tasks.get_task(conn, task_id).slack_thread_ts == child["thread_ts"]

    posts = router.slack.posts
    top_level = [p for p in posts if not p.get("thread_ts")]
    threaded = [p for p in posts if p.get("thread_ts")]
    assert top_level == [{"channel": "D-ann", "text": "Working on fix x"}]
    assert threaded and all(p["thread_ts"] == child["thread_ts"] for p in threaded)
    assert any(task_id in p["text"] for p in threaded)


async def test_start_task_turn_starts_with_who_asked_and_where(router, conn):
    """A hand-over turn's first line is the calling turn's: who asked, where and the link, marked as handed over."""
    asked = "[from Ann Lee (ann) · #sample thread · https://team.slack.com/archives/C1/p11]"
    ctx = router.context_for(add_item(router.queue, add_conversation(conn), origin=asked))
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    handed = latest_item(router).payload["origin"]
    assert handed == f"{asked} (handed over by agent)"

    child = router.context_for(latest_item(router).id)    # the child hands over again: marked once
    await router.call(child, "start_task", {"project": "sample", "title": "fix y", "kind": "bug", "brief": "c"})
    assert latest_item(router).payload["origin"] == handed


async def test_start_task_turn_from_a_turn_without_a_line(router, conn):
    ctx = router.context_for(add_item(router.queue, add_conversation(conn, channel=None, thread_ts=None),
                                      actor=None, unattended=True))
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    assert latest_item(router).payload["origin"] == "[no Slack thread] (handed over by agent)"


async def test_start_task_failed_post_creates_nothing(router, conn):
    """A chat_postMessage that raises (posting the thread-opening line) leaves no task, conversation or work item,
    and the router reports the failure instead of crashing."""
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))

    async def boom(**kw):
        raise RuntimeError("slack is down")
    router.slack.chat_postMessage = boom

    before = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
             for name in ("tasks", "conversations", "work_items")}
    result = await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})

    assert "failed" in result.text.lower()
    after = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("tasks", "conversations", "work_items")}
    assert after == before


async def test_daily_task_limit(router, ctx_general, conn):
    for _ in range(20):
        new_task(conn, status=Status.queued)
    assert "daily task limit" in (await router.call(ctx_general, "track_task",
                                                    {"project": "sample", "title": "t", "kind": "bug"})).text


async def test_stop_all_and_resume(router, ctx_member, ctx_with_task, conn):
    other = add_item(router.queue, "V-x")
    for _ in range(3):  # the two contexts' items, then this one
        router.queue.claim()
    r = await router.call(ctx_member, "stop_all", {})
    assert db.get_setting(conn, "paused") == "1" and router.queue.get(other).status == "cancelled"
    assert "admin" in r.text
    await router.call(ctx_with_task, "resume_all", {})
    assert db.get_setting(conn, "paused") == "0"


async def test_start_task_and_stop_all_posts_are_scrubbed(router, conn):
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))
    await router.call(ctx, "start_task", {"project": "sample", "title": "rotate SUPERSECRETVALUE", "kind": "chore",
                                          "brief": "b"})
    router.queue.claim()
    await router.call(dataclasses.replace(ctx, actor="SUPERSECRETVALUE"), "stop_all", {})
    texts = [p["text"] for p in router.slack.posts]
    assert any(t.startswith("Working on rotate") for t in texts) and any(t.startswith("Stopped by") for t in texts)
    assert not any("SUPERSECRETVALUE" in t for t in texts)


async def test_cancel_task_cancels_its_items(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "cancel_task", {"task_id": ctx_with_task.task_id})
    assert tasks.get_task(conn, ctx_with_task.task_id).status == Status.cancelled
    assert router.queue.get(ctx_with_task.work_item_id).status == "cancelled"


async def test_status_and_tasks(router, ctx_with_task):
    assert "running" in (await router.call(ctx_with_task, "status", {})).text
    assert ctx_with_task.task_id in (await router.call(ctx_with_task, "list_tasks", {})).text
    assert "working" in (await router.call(ctx_with_task, "get_task", {"task_id": ctx_with_task.task_id})).text


async def test_status_names_each_running_items_project_and_how_long_it_has_run(router, conn):
    from datetime import UTC, datetime, timedelta

    bound = add_item(router.queue, add_conversation(conn, project="sample"))
    add_item(router.queue, add_conversation(conn, project=None))
    for _ in range(2):
        router.queue.claim()
    text = (await router.call(router.context_for(bound), "status", {})).text
    assert "running: 2 (sample, -)" in text
    started = (datetime.now(UTC) - timedelta(minutes=12, seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO events(ts, type, data) VALUES (?, 'session_started', ?)",
                 (started, json.dumps({"work_item": bound})))
    text = (await router.call(router.context_for(bound), "status", {})).text
    assert "running: 2 (sample 12m, -)" in text


async def test_read_thread(router, ctx_member):
    router.slack.replies = [{"user": "U1", "text": "the export crashes"}, {"user": "U2", "text": "since monday"}]
    text = (await router.call(ctx_member, "read_thread", {})).text
    assert "ann: the export crashes" in text and "bob: since monday" in text


async def test_read_thread_in_a_dm_reads_the_chat_since_it_started(router, conn):
    """A DM chat has no thread: read_thread gives its top-level messages from its first recorded one on."""
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts=None)
    ctx = router.context_for(add_item(router.queue, cid))
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


class SlackApi(FakeSlack):
    """Answers slack_read's calls: each method's canned response, and every call it was asked for."""

    def __init__(self, answers):
        super().__init__()
        self.answers, self.calls = answers, []

    async def api_call(self, method, *, http_verb="POST", params=None):
        self.calls.append((method, http_verb, params))
        return self.answers[method]


async def test_slack_read_calls_an_allowed_method(router, conn):
    router.slack = SlackApi({"conversations.history": {"ok": True, "messages": [{"ts": "1.0", "text": "hi"}]}})
    ctx = ctx_for(router, actor=None)  # a read-tier session gets it too
    reply = await router.call(ctx, "slack_read", {"method": "conversations.history",
                                                  "params": {"channel": "C9", "limit": 5, "inclusive": True}})
    assert json.loads(reply.text) == {"ok": True, "messages": [{"ts": "1.0", "text": "hi"}]}
    assert router.slack.calls == [("conversations.history", "GET", {"channel": "C9", "limit": 5, "inclusive": "1"})]
    assert "slack_read" in {t["name"] for t in router.manifest(ctx)}


async def test_slack_read_refuses_other_methods(router, ctx_member):
    router.slack = SlackApi({})
    text = (await router.call(ctx_member, "slack_read", {"method": "chat.postMessage",
                                                         "params": {"channel": "C9", "text": "hi"}})).text
    assert "chat.postMessage" in text and "conversations.history" in text and "bookmarks.list" in text
    assert router.slack.calls == []


async def test_slack_read_truncates_big_replies_after_scrubbing(router, ctx_member):
    messages = [{"ts": f"{i}.0", "text": "x" * 100} for i in range(400)]
    messages.insert(0, {"ts": "0.5", "text": "the key is SUPERSECRETVALUE"})
    router.slack = SlackApi({"conversations.replies": {"ok": True, "messages": messages}})
    text = (await router.call(ctx_member, "slack_read", {"method": "conversations.replies",
                                                         "params": {"channel": "C9", "ts": "0.5"}})).text
    assert len(text) < 20_500 and "[truncated" in text
    assert "SUPERSECRETVALUE" not in text and "[redacted]" in text


async def test_post_files_is_in_the_manifest(router, ctx_member):
    assert "post_files" in {t["name"] for t in router.manifest(ctx_member)}
    assert "runner" in (await router.call(ctx_member, "post_files", {"paths": [], "text": ""})).text


async def test_stop_all_keeps_queued_turns_and_tells_the_thread(router, ctx_member, conn):
    running = add_item(router.queue, add_conversation(conn, thread_ts="8.0"))
    for _ in range(2):  # ctx_member's item, then this one
        router.queue.claim()
    queued = add_item(router.queue, add_conversation(conn, thread_ts="9.0"))
    await router.call(ctx_member, "stop_all", {})
    assert router.queue.get(queued).status == "queued" and router.queue.get(running).status == "cancelled"
    stopped = [p for p in router.slack.posts if "Stopped" in p["text"]]
    assert sorted(p["thread_ts"] for p in stopped) == ["1.0", "8.0"]


async def test_start_task_follows_the_calling_turn(router, conn):
    cid = add_conversation(conn, project=None, channel="D-ann", thread_ts="9.0")
    ctx = router.context_for(add_item(router.queue, cid, source="slack", requester="ann"))
    await router.call(ctx, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    child = latest_item(router).payload
    assert (child["follows"], child["source"], child["requester"]) == (ctx.work_item_id, "slack", "ann")


async def test_spawn_agent_queues_a_threadless_session_that_reports_back(router, conn):
    cid = add_conversation(conn, project="sample", channel="C1", thread_ts="9.0")
    ctx = router.context_for(add_item(router.queue, cid, source="slack", requester="ann", origin="[from ann · DM]"))
    reply_ = await router.call(ctx, "spawn_agent", {"prompt": "compare A and B", "cwd": "/var/agent/scratch/x"})
    assert "Spawned agent A-" in reply_.text
    child = latest_item(router)
    row = conn.execute("SELECT * FROM spawned_agents").fetchone()
    convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (child.conversation_id,)).fetchone()
    assert (row["parent_conversation_id"], row["child_conversation_id"], row["status"]) == \
        (cid, child.conversation_id, "running")
    assert (convo["channel"], convo["thread_ts"], convo["cwd"], convo["project"]) == \
        (None, None, "/var/agent/scratch/x", "sample")
    assert child.payload["text"].endswith("compare A and B") and row["id"] in child.payload["text"]
    assert child.payload["origin"] == f"[from ann · DM] (spawned by {router.config.name} as agent {row['id']})"
    assert (child.payload["follows"], child.payload["actor"]) == (ctx.work_item_id, "ann")


async def test_spawn_agent_needs_a_prompt(router, ctx_member):
    assert "needs a prompt" in (await router.call(ctx_member, "spawn_agent", {"prompt": " "})).text
    assert router.conn.execute("SELECT COUNT(*) FROM spawned_agents").fetchone()[0] == 0


async def test_a_spawned_agent_cannot_spawn(router, ctx_member):
    await router.call(ctx_member, "spawn_agent", {"prompt": "p"})
    child = router.context_for(latest_item(router).id)
    assert "can't spawn" in (await router.call(child, "spawn_agent", {"prompt": "q"})).text


async def test_spawn_agent_caps_running_agents_per_conversation(router, ctx_member):
    for _ in range(3):
        assert "Spawned" in (await router.call(ctx_member, "spawn_agent", {"prompt": "p"})).text
    refused = (await router.call(ctx_member, "spawn_agent", {"prompt": "p"})).text
    assert refused.startswith("3 agents you spawned are still running, the most one conversation may have")
    assert router.conn.execute("SELECT COUNT(*) FROM spawned_agents").fetchone()[0] == 3
    # one whose turn was cancelled never reports back, and stops counting
    router.queue.cancel(latest_item(router).id)
    assert "Spawned" in (await router.call(ctx_member, "spawn_agent", {"prompt": "p"})).text


async def test_spawn_agent_caps_running_agents_overall(router):
    first, second = ctx_for(router, actor="bob"), ctx_for(router, actor="bob", channel="C2")
    for _ in range(3):
        await router.call(first, "spawn_agent", {"prompt": "p"})
    assert "Spawned" in (await router.call(second, "spawn_agent", {"prompt": "p"})).text
    refused = (await router.call(second, "spawn_agent", {"prompt": "p"})).text
    assert refused.startswith("4 spawned agents are running already, the most at once")
    assert router.conn.execute("SELECT COUNT(*) FROM spawned_agents").fetchone()[0] == 4


async def test_a_parked_spawned_agent_still_counts_as_running(router, ctx_member):
    for _ in range(3):
        await router.call(ctx_member, "spawn_agent", {"prompt": "p"})
    child = latest_item(router)
    router.queue.complete(child.id, {})   # its turn ended parked: nothing queued, but it carries on later
    router.conn.execute("INSERT INTO parks(conversation_id, work_item_id, until_at, created_at) VALUES (?, ?, ?, ?)",
                        (child.conversation_id, child.id, "2099-01-01T00:00:00Z", db.utcnow()))
    assert "still running" in (await router.call(ctx_member, "spawn_agent", {"prompt": "p"})).text


async def test_spawn_agent_works_in_its_own_folder_unless_told_and_sharing_a_checkout_is_the_callers_call(
        router, ctx_member):
    await router.call(ctx_member, "spawn_agent", {"prompt": "p"})
    child = latest_item(router)
    # no cwd: the runner gives the agent its conversation's own new folder, never the caller's
    assert router.conn.execute("SELECT cwd FROM conversations WHERE id = ?",
                               (child.conversation_id,)).fetchone()[0] is None
    description = next(t["description"] for t in router.manifest(ctx_member) if t["name"] == "spawn_agent")
    assert "its own new folder" in description and "shares that checkout with you" in description


async def test_the_spawn_agent_description_says_background_agents_keep_the_session_open(router, ctx_member):
    description = next(t["description"] for t in router.manifest(ctx_member) if t["name"] == "spawn_agent")
    assert "Background agents (the Agent tool with run_in_background) keep your session open" in description
    assert not re.search(r"(stopped|die|end|killed)\b[^.]*when your turn ends", description)
