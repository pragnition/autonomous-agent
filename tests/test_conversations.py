import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import db, tasks
from agent.core.conversations import (DM_IDLE, FAILED_REPLY, NOT_AUTHORISED, BindRefused, Conversations,
                                      bind_project, create_conversation, record_message)
from agent.core.sessions import AGENT_QUEUE, WorkQueue
from agent.gitutil import branch_name
from agent.tasks import Status
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, make_config, make_pack

_ts = itertools.count(1)


class FakePayloads:
    def turn(self, convo, text, actor, *, unattended=False):
        return {"text": text, "actor": actor, "unattended": unattended, "model": convo["model"]}


class FakeLinearPoller:
    def __init__(self, conn):
        self.conn, self.new = conn, []

    async def poll(self):
        created = []
        for title in self.new:
            t = tasks.create_task(self.conn, project="sample", title=title, description="Steps to reproduce",
                                  kind="bug", source="linear", source_ref="SAM-7", requester=None, owner="ann",
                                  slack_channel="C-SAMPLE", linear_issue_id="iss-7")
            created.append(t.id)
        self.new = []
        return created


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def queue(conn):
    return WorkQueue(conn)


@pytest.fixture
def slack():
    return FakeSlack()


@pytest.fixture
def fake_linear(conn):
    return FakeLinearPoller(conn)


@pytest.fixture
def convos(conn, queue, slack, tmp_path, fake_linear):
    return Conversations(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=queue, slack=slack,
                         payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT",
                         linear_poller=fake_linear)


def mention(thread, text, user="U1", channel="C-SAMPLE"):
    return {"type": "app_mention", "user": user, "channel": channel, "ts": thread, "text": f"<@UBOT> {text}"}


def reply(thread, text, user="U1", channel="C-SAMPLE"):
    return {"type": "message", "user": user, "channel": channel, "ts": f"{thread}{next(_ts)}", "thread_ts": thread,
            "text": text}


def dm(text, user="U1"):
    return {"type": "message", "channel_type": "im", "channel": "D-1", "user": user, "ts": f"5.{next(_ts)}",
            "text": text}


def convo(conn, thread):
    return conn.execute("SELECT * FROM conversations WHERE thread_ts = ? ORDER BY created_at LIMIT 1",
                        (thread,)).fetchone()


def last_item(queue):
    item_id = queue.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    return queue.get(item_id)


def dm_reply(thread, text, user="U1"):
    """A reply in a DM thread: under one of the person's messages or one of the bot's."""
    return {**reply(thread, text, user=user, channel="D-1"), "channel_type": "im"}


def hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sent_ts(slack) -> list[str]:
    """The ts of every message the bot posts from here on, in order."""
    sent, original = [], slack.chat_postMessage

    async def post(**kw):
        resp = await original(**kw)
        sent.append(resp["ts"])
        return resp

    slack.chat_postMessage = post
    return sent


async def finish_turn(convos, queue, text):
    item = queue.claim(AGENT_QUEUE)
    queue.complete(item.id, {"text": text})
    await convos.on_result(queue.get(item.id), {"text": text, "outcome": "done"})
    return item


async def test_second_message_waits_for_first(convos, queue):
    await convos.handle_slack(mention("t1", "do a"))
    await convos.handle_slack(reply("t1", "also b"))
    first = queue.claim(AGENT_QUEUE)
    assert first.payload["text"] == "do a" and queue.claim(AGENT_QUEUE) is None
    queue.complete(first.id, {"text": "ok"})
    assert queue.claim(AGENT_QUEUE).payload["text"].endswith("also b")


async def test_dm_goes_to_the_agent_queue(convos, queue):
    await convos.handle_slack(dm("hi"))
    payload = queue.claim(AGENT_QUEUE).payload
    assert {k: payload[k] for k in ("text", "actor", "unattended", "model")} == \
        {"text": "hi", "actor": "ann", "unattended": False, "model": None}


async def test_mention_outside_pack_channel_goes_to_the_agent_queue(convos, queue):
    await convos.handle_slack(mention("t9", "hello", channel="C-OTHER"))
    assert queue.claim(AGENT_QUEUE) is not None


async def test_dm_and_project_channel_use_one_queue(convos, conn, queue):
    """Nothing about where a message came from routes it to a different queue: a DM, a mention in a channel with
    no project pack, and a mention in a project's channel all land on AGENT_QUEUE, and only the project one
    carries a project."""
    await convos.handle_slack(dm("hi"))
    await convos.handle_slack(mention("t9", "hello", channel="C-OTHER"))
    await convos.handle_slack(mention("t1", "do a"))

    rows = conn.execute("SELECT * FROM conversations").fetchall()
    assert len(rows) == 3 and all(r["sandbox"] == AGENT_QUEUE for r in rows)
    dm_row = next(r for r in rows if r["channel"] == "D-1")
    other_row = next(r for r in rows if r["thread_ts"] == "t9")
    sample_row = next(r for r in rows if r["thread_ts"] == "t1")
    assert dm_row["project"] is None and other_row["project"] is None and sample_row["project"] == "sample"
    assert (dm_row["branch"], dm_row["workdir"]) == (None, "session")
    assert (other_row["branch"], other_row["workdir"]) == (None, "session")

    assert {queue.claim(AGENT_QUEUE).payload["text"] for _ in range(3)} == {"hi", "hello", "do a"}


async def test_project_channel_conversation_gets_branch_at_creation(convos, conn):
    await convos.handle_slack(mention("t30", "fix login"))
    row = convo(conn, "t30")
    assert row["branch"] == branch_name(convos.config.name, row["id"], "fix login")
    assert row["workdir"] == "checkout"


async def test_branch_is_named_from_the_first_line_and_kept(convos, conn):
    await convos.handle_slack(mention("t31", "fix login\n\nIt fails after the password reset."))
    await convos.handle_slack(reply("t31", "and the signup page too"))
    row = convo(conn, "t31")
    assert row["branch"] == branch_name(convos.config.name, row["id"], "fix login")


async def test_reply_goes_to_waiting_split_session(convos, conn, queue):
    await convos.handle_slack(mention("t1", "fix 2 bugs"))
    child = add_conversation(conn, sandbox=AGENT_QUEUE, channel="C-SAMPLE", thread_ts="t1", is_primary=0)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (child,))
    await convos.handle_slack(reply("t1", "use staging"))
    assert last_item(queue).conversation_id == child
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (child,)).fetchone()[0] == 0


async def test_answer_moves_waiting_task_back_to_working(convos, conn, queue):
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    cid = add_conversation(conn, sandbox=AGENT_QUEUE, channel="C-SAMPLE", thread_ts="t2", task_id=t.id)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (cid,))
    await convos.handle_slack(reply("t2", "prod"))
    assert tasks.get_task(conn, t.id).status == Status.working


async def test_unmentioned_message_in_unknown_thread_is_ignored(convos, queue):
    await convos.handle_slack(reply("t3", "chatter"))
    await convos.handle_slack({"type": "message", "user": "U1", "channel": "C-SAMPLE", "ts": "7.0", "text": "hi"})
    assert queue.claim(AGENT_QUEUE) is None


async def test_unknown_person_gets_ephemeral(convos, slack, queue):
    await convos.handle_slack(mention("t1", "hi", user="U999"))
    assert len(slack.ephemerals) == 1 and slack.ephemerals[0]["text"] == NOT_AUTHORISED
    assert queue.claim(AGENT_QUEUE) is None


async def test_duplicate_events_handled_once(convos, queue):
    event = mention("t4", "once")
    await convos.handle_slack(event)
    await convos.handle_slack({**event, "type": "message"})
    queue.claim(AGENT_QUEUE)
    assert queue.claim(AGENT_QUEUE) is None
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1


async def test_own_and_bot_messages_ignored(convos, queue):
    await convos.handle_slack({**mention("t5", "x"), "user": "UBOT"})
    await convos.handle_slack({**mention("t6", "x"), "bot_id": "B1"})
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


async def test_use_fable_sticks(convos, conn, queue):
    await convos.handle_slack(mention("t1", "use fable and fix x"))
    assert convo(conn, "t1")["model"] == "claude-fable-5-1"
    await convos.handle_slack(reply("t1", "and y"))
    assert last_item(queue).payload["model"] == "claude-fable-5-1"


async def test_message_ts_recorded_for_reactions(convos, queue):
    await convos.handle_slack(mention("t7", "react please"))
    assert last_item(queue).payload["message_ts"] == "t7"


async def test_accepted_message_gets_eyes(convos, slack):
    await convos.handle_slack(mention("t8", "look at this"))
    event = reply("t8", "and this")
    await convos.handle_slack(event)
    assert [(r["channel"], r["timestamp"], r["name"]) for r in slack.reactions] == [
        ("C-SAMPLE", "t8", "eyes"), ("C-SAMPLE", event["ts"], "eyes")]


async def test_ignored_and_refused_messages_get_no_eyes(convos, slack):
    await convos.handle_slack(mention("t1", "hi", user="U999"))
    await convos.handle_slack(reply("t9", "chatter"))
    assert slack.reactions == []


async def test_failed_eyes_still_queues_the_turn(convos, slack, queue):
    async def down(**kw):
        raise RuntimeError("missing_scope")

    slack.reactions_add = down
    await convos.handle_slack(mention("t10", "hi"))
    assert queue.claim(AGENT_QUEUE).payload["text"] == "hi"


async def test_linear_issue_is_unattended(convos, fake_linear, queue, slack, conn):
    fake_linear.new = ["Export broken"]
    created = await convos.intake_linear()
    item = queue.claim(AGENT_QUEUE)
    assert item.payload["unattended"] is True and item.payload["text"].startswith("Linear issue SAM-7: Export broken")
    assert slack.posts[0]["channel"] == "C-SAMPLE"
    task = tasks.get_task(conn, created[0])
    assert task.slack_thread_ts == slack.posts[0].get("ts", task.slack_thread_ts) and task.status == Status.working
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert row["task_id"] == task.id and row["workdir"] == "checkout"
    assert row["branch"] == branch_name(convos.config.name, task.id, "Export broken")


async def test_on_result_posts_reply_and_session(convos, queue, slack, conn):
    await convos.handle_slack(mention("t8", "question"))
    item = queue.claim(AGENT_QUEUE)
    queue.complete(item.id, {"text": "the answer", "session_id": "sess-1"})
    await convos.on_result(queue.get(item.id), {"text": "the answer", "session_id": "sess-1", "outcome": "done"})
    assert slack.posts[-1] == {"channel": "C-SAMPLE", "thread_ts": "t8", "text": "the answer",
                               "blocks": [{"type": "markdown", "text": "the answer"}]}
    assert convo(conn, "t8")["session_id"] == "sess-1"


async def test_on_result_failure_posts_failed_reply(convos, queue, slack):
    await convos.handle_slack(mention("t10", "question"))
    item = queue.claim(AGENT_QUEUE)
    queue.complete(item.id, {"outcome": "error"})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "error": "boom"})
    assert slack.posts[-1]["text"] == FAILED_REPLY


async def test_on_result_for_cancelled_item_posts_nothing(convos, queue, slack):
    await convos.handle_slack(mention("t11", "question"))
    item = queue.claim(AGENT_QUEUE)
    queue.cancel(item.id)
    before = len(slack.posts)
    await convos.on_result(queue.get(item.id), {"outcome": "interrupted"})
    assert len(slack.posts) == before


async def test_failed_turn_forgets_the_session(convos, queue, conn):
    await convos.handle_slack(mention("t20", "question"))
    item = queue.claim(AGENT_QUEUE)
    conn.execute("UPDATE conversations SET session_id = 'sess-old' WHERE id = ?", (item.conversation_id,))
    queue.complete(item.id, {"outcome": "error"})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "error": "boom"})
    assert conn.execute("SELECT session_id FROM conversations WHERE id = ?",
                        (item.conversation_id,)).fetchone()[0] is None


async def test_linear_task_is_reattached_after_a_slack_failure(convos, fake_linear, queue, slack, conn):
    fake_linear.new = ["Export broken"]
    original = slack.chat_postMessage

    async def down(**kw):
        raise RuntimeError("slack down")

    slack.chat_postMessage = down
    await convos.intake_linear()
    assert queue.claim(AGENT_QUEUE) is None
    slack.chat_postMessage = original
    await convos.intake_linear()
    item = queue.claim(AGENT_QUEUE)
    assert item is not None and item.payload["text"].startswith("Linear issue SAM-7")


async def test_two_top_level_dms_share_conversation(convos, conn, queue):
    await convos.handle_slack(dm("login yourself to azure cli"))
    first = last_item(queue)
    await convos.handle_slack(dm("done"))
    assert last_item(queue).conversation_id == first.conversation_id
    assert conn.execute("SELECT thread_ts FROM conversations WHERE id = ?",
                        (first.conversation_id,)).fetchone()[0] is None


async def test_dm_after_idle_starts_new_conversation(convos, conn, queue):
    assert DM_IDLE == timedelta(hours=12)
    await convos.handle_slack(dm("first"))
    first = last_item(queue).conversation_id
    conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (hours_ago(11), first))
    await convos.handle_slack(dm("still the same chat"))
    assert last_item(queue).conversation_id == first
    conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (hours_ago(13), first))
    await convos.handle_slack(dm("a new day"))
    second = last_item(queue).conversation_id
    assert second != first
    assert conn.execute("SELECT thread_ts FROM conversations WHERE id = ?", (second,)).fetchone()[0] is None


async def test_dm_message_during_running_turn_queues_on_same_conversation(convos, queue):
    await convos.handle_slack(dm("run the tests"))
    first = queue.claim(AGENT_QUEUE)
    await convos.handle_slack(dm("and the linter"))
    assert last_item(queue).conversation_id == first.conversation_id
    assert queue.claim(AGENT_QUEUE) is None
    queue.complete(first.id, {"text": "ok"})
    assert queue.claim(AGENT_QUEUE).payload["text"] == "and the linter"


async def test_dm_reply_is_posted_top_level(convos, queue, slack):
    await convos.handle_slack(dm("hi"))
    await finish_turn(convos, queue, "hello")
    assert (slack.posts[-1]["channel"], slack.posts[-1]["thread_ts"], slack.posts[-1]["text"]) == \
        ("D-1", None, "hello")


async def test_thread_reply_under_bot_message_routes_back(convos, conn, queue, slack):
    sent = sent_ts(slack)
    await convos.handle_slack(dm("what's failing?"))
    item = await finish_turn(convos, queue, "the login test")
    assert conn.execute("SELECT conversation_id FROM slack_messages WHERE channel = 'D-1' AND ts = ?",
                        (sent[0],)).fetchone()[0] == item.conversation_id
    await convos.handle_slack(dm_reply(sent[0], "why?"))
    assert last_item(queue).conversation_id == item.conversation_id


async def test_thread_reply_under_person_message_routes_back(convos, queue):
    event = dm("deploy the branch")
    await convos.handle_slack(event)
    first = last_item(queue).conversation_id
    await convos.handle_slack(dm_reply(event["ts"], "to staging"))
    assert last_item(queue).conversation_id == first


async def test_thread_reply_under_second_chunk_routes_back(convos, queue, slack):
    sent = sent_ts(slack)
    await convos.handle_slack(dm("write a long report"))
    item = await finish_turn(convos, queue, "x" * 13_000)
    assert len(sent) == 2
    await convos.handle_slack(dm_reply(sent[1], "about the second part"))
    assert last_item(queue).conversation_id == item.conversation_id


async def test_channel_thread_reply_without_mention_continues(convos, queue, slack):
    await convos.handle_slack(mention("t40", "fix login"))
    item = await finish_turn(convos, queue, "on it")
    assert slack.posts[-1]["thread_ts"] == "t40"
    await convos.handle_slack(reply("t40", "the signup page too"))
    assert last_item(queue).conversation_id == item.conversation_id


async def test_first_dm_after_upgrade_starts_rolling_chat(convos, conn, queue):
    old = add_conversation(conn, sandbox=AGENT_QUEUE, project=None, channel="D-1", thread_ts="5.1")
    await convos.handle_slack(dm("hello again"))
    new = last_item(queue).conversation_id
    assert new != old
    assert conn.execute("SELECT thread_ts FROM conversations WHERE id = ?", (new,)).fetchone()[0] is None
    await convos.handle_slack(dm_reply("5.1", "about that old thread"))
    assert last_item(queue).conversation_id == old


async def test_dm_answer_clears_waiting(convos, conn, queue):
    """A question asked in the DM chat is answered at the top level or in a thread under the question."""
    await convos.handle_slack(dm("deploy it"))
    cid = last_item(queue).conversation_id
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (cid,))
    await convos.handle_slack(dm("staging"))
    assert last_item(queue).conversation_id == cid
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == 0

    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    conn.execute("UPDATE conversations SET waiting = 1, task_id = ? WHERE id = ?", (t.id, cid))
    record_message(conn, "D-1", "900.0", cid)  # the bot's question
    await convos.handle_slack(dm_reply("900.0", "prod, actually"))
    assert last_item(queue).conversation_id == cid
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == 0
    assert tasks.get_task(conn, t.id).status == Status.working


PACKS = {"sample": make_pack(), "other": make_pack("other", repo="o/other")}


def unbound(conn) -> str:
    """A DM's conversation: no project, so the session picks one with work_on."""
    return create_conversation(conn, agent_name="agent", channel="D-1", thread_ts="5.1", project=None, title="hi")


def test_bind_sets_project_and_branch_once(conn):
    cid = unbound(conn)
    row = bind_project(conn, cid, "sample", packs=PACKS, agent_name="agent", title="run sample locally")
    assert row["project"] == "sample" and row["branch"] == branch_name("agent", cid, "run sample locally")
    assert row["workdir"] == "session"   # the working folder is fixed by the first turn
    again = bind_project(conn, cid, "sample", packs=PACKS, agent_name="agent", title="something else")
    assert (again["project"], again["branch"], again["workdir"]) == ("sample", row["branch"], "session")
    logged = conn.execute("SELECT data FROM events WHERE type = 'project_bound'").fetchall()
    assert [json.loads(r["data"]) for r in logged] == [{"conversation_id": cid, "project": "sample",
                                                         "branch": row["branch"]}]
    tracked = add_conversation(conn, sandbox=AGENT_QUEUE, project="sample")  # track_task's binding: no branch yet
    assert bind_project(conn, tracked, "sample", packs=PACKS, agent_name="agent", title="t")["branch"] == \
        branch_name("agent", tracked, "t")
    upfront = create_conversation(conn, agent_name="agent", channel="C-SAMPLE", thread_ts="9.1", project="sample",
                                  title="fix login")
    kept = bind_project(conn, upfront, "sample", packs=PACKS, agent_name="agent", title="other title")
    assert (kept["branch"], kept["workdir"]) == (branch_name("agent", upfront, "fix login"), "checkout")


def test_bind_refuses_unknown_and_second_project(conn):
    cid = unbound(conn)
    with pytest.raises(BindRefused, match=r"unknown project 'nope' \(known: other, sample\)"):
        bind_project(conn, cid, "nope", packs=PACKS, agent_name="agent", title="t")
    assert conn.execute("SELECT project, branch FROM conversations WHERE id = ?", (cid,)).fetchone()[:] == (None, None)
    bind_project(conn, cid, "sample", packs=PACKS, agent_name="agent", title="t")
    with pytest.raises(BindRefused, match="bound to sample"):
        bind_project(conn, cid, "other", packs=PACKS, agent_name="agent", title="t")
    assert conn.execute("SELECT project FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == "sample"
