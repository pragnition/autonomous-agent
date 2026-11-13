import itertools

import pytest

from agent import db, tasks
from agent.core.conversations import FAILED_REPLY, NOT_AUTHORISED, Conversations
from agent.core.sessions import WorkQueue
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


async def test_second_message_waits_for_first(convos, queue):
    await convos.handle_slack(mention("t1", "do a"))
    await convos.handle_slack(reply("t1", "also b"))
    first = queue.claim("code-sample")
    assert first.payload["text"] == "do a" and queue.claim("code-sample") is None
    queue.complete(first.id, {"text": "ok"})
    assert queue.claim("code-sample").payload["text"].endswith("also b")


async def test_dm_goes_to_general(convos, queue):
    await convos.handle_slack(dm("hi"))
    payload = queue.claim("general").payload
    assert {k: payload[k] for k in ("text", "actor", "unattended", "model")} == \
        {"text": "hi", "actor": "ann", "unattended": False, "model": None}


async def test_mention_outside_pack_channel_goes_to_general(convos, queue):
    await convos.handle_slack(mention("t9", "hello", channel="C-OTHER"))
    assert queue.claim("general") is not None


async def test_reply_goes_to_waiting_split_session(convos, conn, queue):
    await convos.handle_slack(mention("t1", "fix 2 bugs"))
    child = add_conversation(conn, sandbox="code-sample", channel="C-SAMPLE", thread_ts="t1", is_primary=0)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (child,))
    await convos.handle_slack(reply("t1", "use staging"))
    assert last_item(queue).conversation_id == child
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (child,)).fetchone()[0] == 0


async def test_answer_moves_waiting_task_back_to_working(convos, conn, queue):
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    cid = add_conversation(conn, sandbox="code-sample", channel="C-SAMPLE", thread_ts="t2", task_id=t.id)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (cid,))
    await convos.handle_slack(reply("t2", "prod"))
    assert tasks.get_task(conn, t.id).status == Status.working


async def test_unmentioned_message_in_unknown_thread_is_ignored(convos, queue):
    await convos.handle_slack(reply("t3", "chatter"))
    await convos.handle_slack({"type": "message", "user": "U1", "channel": "C-SAMPLE", "ts": "7.0", "text": "hi"})
    assert queue.claim("code-sample") is None and queue.claim("general") is None


async def test_unknown_person_gets_ephemeral(convos, slack, queue):
    await convos.handle_slack(mention("t1", "hi", user="U999"))
    assert len(slack.ephemerals) == 1 and slack.ephemerals[0]["text"] == NOT_AUTHORISED
    assert queue.claim("code-sample") is None


async def test_duplicate_events_handled_once(convos, queue):
    event = mention("t4", "once")
    await convos.handle_slack(event)
    await convos.handle_slack({**event, "type": "message"})
    queue.claim("code-sample")
    assert queue.claim("code-sample") is None
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
    assert queue.claim("code-sample").payload["text"] == "hi"


async def test_linear_issue_is_unattended(convos, fake_linear, queue, slack, conn):
    fake_linear.new = ["Export broken"]
    created = await convos.intake_linear()
    item = queue.claim("code-sample")
    assert item.payload["unattended"] is True and item.payload["text"].startswith("Linear issue SAM-7: Export broken")
    assert slack.posts[0]["channel"] == "C-SAMPLE"
    task = tasks.get_task(conn, created[0])
    assert task.slack_thread_ts == slack.posts[0].get("ts", task.slack_thread_ts) and task.status == Status.working
    assert conn.execute("SELECT task_id FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()[0] == task.id


async def test_on_result_posts_reply_and_session(convos, queue, slack, conn):
    await convos.handle_slack(mention("t8", "question"))
    item = queue.claim("code-sample")
    queue.complete(item.id, {"text": "the answer", "session_id": "sess-1"})
    await convos.on_result(queue.get(item.id), {"text": "the answer", "session_id": "sess-1", "outcome": "done"})
    assert slack.posts[-1] == {"channel": "C-SAMPLE", "thread_ts": "t8", "text": "the answer"}
    assert convo(conn, "t8")["session_id"] == "sess-1"


async def test_on_result_failure_posts_failed_reply(convos, queue, slack):
    await convos.handle_slack(mention("t10", "question"))
    item = queue.claim("code-sample")
    queue.complete(item.id, {"outcome": "error"})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "error": "boom"})
    assert slack.posts[-1]["text"] == FAILED_REPLY


async def test_on_result_for_cancelled_item_posts_nothing(convos, queue, slack):
    await convos.handle_slack(mention("t11", "question"))
    item = queue.claim("code-sample")
    queue.cancel(item.id)
    before = len(slack.posts)
    await convos.on_result(queue.get(item.id), {"outcome": "interrupted"})
    assert len(slack.posts) == before


async def test_failed_turn_forgets_the_session(convos, queue, conn):
    await convos.handle_slack(mention("t20", "question"))
    item = queue.claim("code-sample")
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
    assert queue.claim("code-sample") is None
    slack.chat_postMessage = original
    await convos.intake_linear()
    item = queue.claim("code-sample")
    assert item is not None and item.payload["text"].startswith("Linear issue SAM-7")
