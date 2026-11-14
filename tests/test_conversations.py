import asyncio
import itertools

import pytest

from agent import db, tasks
from agent.core.conversations import NOT_AUTHORISED, Conversations, record_message
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
            t = tasks.create_task(self.conn, project="sample", title=title,
                                  description="Steps to reproduce\n\nLinear: https://linear.app/x/issue/SAM-7",
                                  kind="bug", source="linear", source_ref="SAM-7", requester=None, owner="ann",
                                  slack_channel="C-SAMPLE", linear_issue_id="iss-7")
            created.append(t.id)
        self.new = []
        return created


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
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


def sent_ts(slack) -> list[str]:
    """The ts of every message the bot posts from here on, in order."""
    sent, original = [], slack.chat_postMessage

    async def post(**kw):
        resp = await original(**kw)
        sent.append(resp["ts"])
        return resp

    slack.chat_postMessage = post
    return sent


def task_waiting_on(conn, cid) -> str:
    """Give the conversation a task that asked a question, and mark it waiting."""
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    conn.execute("UPDATE conversations SET waiting = 1, task_id = ? WHERE id = ?", (t.id, cid))
    return t.id


async def finish_turn(convos, queue, text):
    item = queue.claim()
    queue.complete(item.id, {"text": text})
    await convos.on_result(queue.get(item.id), {"text": text, "outcome": "done"})
    return item


async def test_second_message_waits_for_first(convos, queue):
    await convos.handle_slack(mention("t1", "do a"))
    await convos.handle_slack(reply("t1", "also b"))
    first = queue.claim()
    assert first.payload["text"] == "do a" and queue.claim() is None
    queue.complete(first.id, {"text": "ok"})
    assert queue.claim().payload["text"].endswith("also b")


async def test_dm_queues_a_turn(convos, queue):
    await convos.handle_slack(dm("hi"))
    payload = queue.claim().payload
    assert {k: payload[k] for k in ("text", "actor", "unattended", "model")} == \
        {"text": "hi", "actor": "ann", "unattended": False, "model": None}


async def test_mention_outside_a_project_channel_queues_a_turn(convos, queue):
    await convos.handle_slack(mention("t9", "hello", channel="C-OTHER"))
    assert queue.claim() is not None


async def test_only_a_project_channel_binds_a_project(convos, conn, queue):
    """A DM, a mention in a channel with no project pack and a mention in a project's channel each queue a turn;
    only the project channel's conversation carries a project."""
    await convos.handle_slack(dm("hi"))
    await convos.handle_slack(mention("t9", "hello", channel="C-OTHER"))
    await convos.handle_slack(mention("t1", "do a"))

    rows = conn.execute("SELECT * FROM conversations").fetchall()
    assert len(rows) == 3
    dm_row = next(r for r in rows if r["channel"] == "D-1")
    other_row = next(r for r in rows if r["thread_ts"] == "t9")
    sample_row = next(r for r in rows if r["thread_ts"] == "t1")
    assert dm_row["project"] is None and other_row["project"] is None and sample_row["project"] == "sample"

    assert {queue.claim().payload["text"] for _ in range(3)} == {"hi", "hello", "do a"}


async def test_reply_goes_to_waiting_split_session(convos, conn, queue):
    await convos.handle_slack(mention("t1", "fix 2 bugs"))
    child = add_conversation(conn, channel="C-SAMPLE", thread_ts="t1", is_primary=0)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (child,))
    await convos.handle_slack(reply("t1", "use staging"))
    assert last_item(queue).conversation_id == child
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (child,)).fetchone()[0] == 0


async def test_answer_moves_waiting_task_back_to_working(convos, conn, queue):
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.transition(conn, t.id, Status.needs_input, actor="x")
    cid = add_conversation(conn, channel="C-SAMPLE", thread_ts="t2", task_id=t.id)
    conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (cid,))
    await convos.handle_slack(reply("t2", "prod"))
    assert tasks.get_task(conn, t.id).status == Status.working


async def test_unmentioned_message_in_unknown_thread_is_ignored(convos, queue):
    await convos.handle_slack(reply("t3", "chatter"))
    await convos.handle_slack({"type": "message", "user": "U1", "channel": "C-SAMPLE", "ts": "7.0", "text": "hi"})
    assert queue.claim() is None


async def test_unknown_person_gets_ephemeral(convos, slack, queue):
    await convos.handle_slack(mention("t1", "hi", user="U999"))
    assert len(slack.ephemerals) == 1 and slack.ephemerals[0]["text"] == NOT_AUTHORISED
    assert queue.claim() is None


async def test_duplicate_events_handled_once(convos, queue):
    event = mention("t4", "once")
    await convos.handle_slack(event)
    await convos.handle_slack({**event, "type": "message"})
    queue.claim()
    assert queue.claim() is None
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
    assert queue.claim().payload["text"] == "hi"


def permalink_of(channel, ts):
    return f"https://team.slack.com/archives/{channel}/p{ts.replace('.', '')}"


class Lookups:
    """What core's Slack lookups answer: people's names, channel names and message permalinks."""

    def __init__(self, slack, names=None, channels=None):
        self.names, self.channels, self.calls = names or {}, channels or {}, []
        slack.conversations_info = self.conversations_info

    async def person_name(self, key):
        return self.names.get(key)

    async def permalink(self, channel, ts):
        self.calls.append(ts)
        return permalink_of(channel, ts)

    async def conversations_info(self, channel):
        return {"channel": {"name": self.channels[channel]}}


@pytest.fixture
def lookups(convos, slack):
    found = Lookups(slack, names={"ann": "Ann Lee"}, channels={"C-SAMPLE": "chilliclaw"})
    convos.person_name, convos.permalink = found.person_name, found.permalink
    return found


async def test_dm_turn_starts_with_who_where_and_link(convos, queue, lookups):
    event = dm("deploy it")
    await convos.handle_slack(event)
    item = queue.claim()
    assert item.payload["origin"] == f"[from Ann Lee (ann, GitHub @ann-gh) · DM · {permalink_of('D-1', event['ts'])}]"
    assert item.payload["text"] == "deploy it"


async def test_channel_mention_and_thread_reply_each_carry_their_own_link(convos, queue, lookups):
    await convos.handle_slack(mention("t1", "fix the export"))
    answer = reply("t1", "the csv one")
    await convos.handle_slack(answer)
    first = queue.claim()
    queue.complete(first.id, {"text": "ok"})
    second = queue.claim()
    assert first.payload["origin"] == f"[from Ann Lee (ann, GitHub @ann-gh) · #chilliclaw · {permalink_of('C-SAMPLE', 't1')}]"
    assert second.payload["origin"] == (f"[from Ann Lee (ann, GitHub @ann-gh) · #chilliclaw thread · "
                                        f"{permalink_of('C-SAMPLE', answer['ts'])}]")


async def test_failed_lookups_leave_out_what_is_unknown(convos, queue, slack):
    async def broken(**kw):
        raise RuntimeError("channel_not_found")
    slack.conversations_info = broken
    await convos.handle_slack(mention("t1", "fix the export", user="U2"))
    assert queue.claim().payload["origin"] == "[from bob (GitHub @bob-gh) · #C-SAMPLE]"


async def test_slow_lookups_never_hold_the_turn(convos, queue, lookups, monkeypatch):
    monkeypatch.setattr("agent.core.conversations.LOOKUP_S", 0.05)

    async def stuck(channel, ts):
        await asyncio.sleep(10)
    convos.permalink = stuck
    await asyncio.wait_for(convos.handle_slack(dm("deploy it")), 2)
    assert queue.claim().payload["origin"] == "[from Ann Lee (ann, GitHub @ann-gh) · DM]"


async def test_messages_keep_their_order_while_lookups_run(convos, queue, lookups):
    """A first message whose lookups are slow is still queued before a second that arrives a moment later."""
    first, second = dm("fix the login"), dm("and the logout")
    release = asyncio.Event()
    fast = lookups.permalink

    async def permalink(channel, ts):
        if ts == first["ts"]:
            await release.wait()
        return await fast(channel, ts)
    convos.permalink = permalink
    a = asyncio.create_task(convos.handle_slack(first))
    b = asyncio.create_task(convos.handle_slack(second))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(a, b)
    texts = [r[0] for r in queue.conn.execute("SELECT json_extract(payload, '$.text') FROM work_items ORDER BY rowid")]
    assert texts == ["fix the login", "and the logout"]


async def test_linear_issue_turn_starts_with_the_issue_and_its_link(convos, fake_linear, queue):
    fake_linear.new = ["Export broken"]
    await convos.intake_linear()
    assert queue.claim().payload["origin"] == "[Linear SAM-7 · https://linear.app/x/issue/SAM-7]"


async def test_linear_issue_is_unattended(convos, fake_linear, queue, slack, conn):
    fake_linear.new = ["Export broken"]
    created = await convos.intake_linear()
    item = queue.claim()
    assert item.payload["unattended"] is True and item.payload["text"].startswith("Linear issue SAM-7: Export broken")
    assert slack.posts[0]["channel"] == "C-SAMPLE"
    task = tasks.get_task(conn, created[0])
    assert task.slack_thread_ts == slack.posts[0].get("ts", task.slack_thread_ts) and task.status == Status.working
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert row["task_id"] == task.id and row["project"] == "sample"


async def test_on_result_posts_reply_and_session(convos, queue, slack, conn):
    await convos.handle_slack(mention("t8", "question"))
    item = queue.claim()
    queue.complete(item.id, {"text": "the answer", "session_id": "sess-1"})
    await convos.on_result(queue.get(item.id), {"text": "the answer", "session_id": "sess-1", "outcome": "done"})
    assert slack.posts[-1] == {"channel": "C-SAMPLE", "thread_ts": "t8", "text": "the answer",
                               "blocks": [{"type": "markdown", "text": "the answer"}]}
    assert convo(conn, "t8")["session_id"] == "sess-1"


async def test_on_result_reply_is_scrubbed(conn, queue, slack, tmp_path):
    """The session's final reply goes through the same scrub as every tool reply: configured secret values and
    token patterns."""
    convos = Conversations(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=queue,
                           slack=slack, payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT",
                           secret_values=["SUPERSECRETVALUE"])
    await convos.handle_slack(mention("t9", "what are the tokens?"))
    item = queue.claim()
    text = "the bot token is xoxb-1234567890-abcdefghij and the key is SUPERSECRETVALUE"
    queue.complete(item.id, {"text": text})
    await convos.on_result(queue.get(item.id), {"text": text, "outcome": "done"})
    posted = slack.posts[-1]
    assert posted["text"] == "the bot token is [redacted] and the key is [redacted]"
    assert posted["blocks"] == [{"type": "markdown", "text": posted["text"]}]


async def test_on_result_reply_without_configured_secrets_still_drops_token_patterns(convos, queue, slack):
    await convos.handle_slack(mention("t9b", "and the GitHub one?"))
    item = queue.claim()
    text = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    queue.complete(item.id, {"text": text})
    await convos.on_result(queue.get(item.id), {"text": text, "outcome": "done"})
    assert slack.posts[-1]["text"] == "[redacted]"


async def failed_turn(convos, queue, thread, result):
    await convos.handle_slack(mention(thread, "question"))
    item = queue.claim()
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)


async def test_failed_turn_says_what_the_error_was(convos, queue, slack):
    error = "preparing the session failed: GitError: git clone -q failed: fatal: the remote end hung up unexpectedly"
    await failed_turn(convos, queue, "t10", {"outcome": "error", "error": error})
    text = slack.posts[-1]["text"]
    assert text.startswith("That failed before I could finish it:")
    assert error in text


async def test_failed_turn_error_is_scrubbed(conn, queue, slack, tmp_path):
    convos = Conversations(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=queue,
                           slack=slack, payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT",
                           secret_values=["SUPERSECRETVALUE"])
    await failed_turn(convos, queue, "t10s", {"outcome": "error", "error": "auth failed for SUPERSECRETVALUE"})
    assert "SUPERSECRETVALUE" not in slack.posts[-1]["text"]
    assert "auth failed for [redacted]" in slack.posts[-1]["text"]


async def test_failed_turn_without_an_error_says_so(convos, queue, slack):
    await failed_turn(convos, queue, "t10n", {"outcome": "error"})
    assert slack.posts[-1]["text"] == "That failed before I could finish it, and the runner didn't say why."


async def test_timed_out_turn_says_it_ran_out_of_time(convos, queue, slack):
    convos.config.limits.continuations = 0
    await failed_turn(convos, queue, "t10t", {"outcome": "timeout", "error": "no result within 3600 s"})
    assert slack.posts[-1]["text"] == "I ran out of time before finishing that (no result within 3600 s)."


async def test_turn_at_the_step_limit_says_so(convos, queue, slack):
    convos.config.limits.continuations = 0
    await failed_turn(convos, queue, "t10m", {"outcome": "max_turns"})
    assert slack.posts[-1]["text"] == "I hit my step limit before finishing that."


async def test_turn_that_hits_a_limit_carries_on_by_itself(convos, queue, slack, conn):
    await convos.handle_slack(mention("t40", "build it"))
    first = queue.claim()
    result = {"outcome": "timeout", "error": "no result within 5400 s", "session_id": "sess-1"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    assert slack.posts[-1]["text"] == ("Still on it: that turn hit its 90-minute limit, so I'm carrying on from where "
                                       "I stopped.")
    nxt = queue.claim()
    assert nxt.conversation_id == first.conversation_id and nxt.payload["continuation"] == 1
    assert nxt.payload["text"] == ("(Your last turn hit its 90-minute limit before you finished. Carry on from where "
                                   "you stopped.)")
    assert (nxt.payload["actor"], nxt.payload["origin"]) == (first.payload["actor"], first.payload["origin"])
    assert conn.execute("SELECT session_id FROM conversations WHERE id = ?",
                        (first.conversation_id,)).fetchone()[0] == "sess-1"   # the next turn resumes it


async def test_carrying_on_stops_after_the_configured_number_of_times(convos, fake_linear, queue, slack, conn):
    fake_linear.new = ["Export broken"]
    task_id = (await convos.intake_linear())[0]
    for n in range(4):
        item = queue.claim()
        assert item.payload.get("continuation", 0) == n and item.payload["unattended"] is True
        queue.complete(item.id, {"outcome": "max_turns"})
        await convos.on_result(queue.get(item.id), {"outcome": "max_turns"})
        if n < 3:
            assert slack.posts[-1]["text"].startswith("Still on it: that turn hit its 400-step limit")
            assert tasks.get_task(conn, task_id).status == Status.working
    assert slack.posts[-1]["text"] == "I hit my step limit before finishing that."
    assert queue.claim() is None
    assert tasks.get_task(conn, task_id).status == Status.failed


async def test_interrupted_turn_says_it_was_stopped(convos, queue, slack):
    await failed_turn(convos, queue, "t10i", {"outcome": "interrupted", "error": "interrupted"})
    assert slack.posts[-1]["text"] == "That was stopped before I finished it."


async def test_on_result_for_cancelled_item_posts_nothing(convos, queue, slack):
    await convos.handle_slack(mention("t11", "question"))
    item = queue.claim()
    queue.cancel(item.id)
    before = len(slack.posts)
    await convos.on_result(queue.get(item.id), {"outcome": "interrupted"})
    assert len(slack.posts) == before


async def test_failed_turn_forgets_the_session(convos, queue, conn):
    await convos.handle_slack(mention("t20", "question"))
    item = queue.claim()
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
    assert queue.claim() is None
    slack.chat_postMessage = original
    await convos.intake_linear()
    item = queue.claim()
    assert item is not None and item.payload["text"].startswith("Linear issue SAM-7")


async def test_each_top_level_dm_starts_its_own_thread(convos, conn, queue):
    first_event, second_event = dm("login yourself to azure cli"), dm("what's on staging?")
    await convos.handle_slack(first_event)
    first = last_item(queue)
    await convos.handle_slack(second_event)
    second = last_item(queue)
    assert first.conversation_id != second.conversation_id
    threads = dict(conn.execute("SELECT id, thread_ts FROM conversations").fetchall())
    assert (threads[first.conversation_id], threads[second.conversation_id]) == (first_event["ts"], second_event["ts"])


async def test_dm_thread_reply_during_running_turn_queues_on_same_conversation(convos, queue):
    event = dm("run the tests")
    await convos.handle_slack(event)
    first = queue.claim()
    await convos.handle_slack(dm_reply(event["ts"], "and the linter"))
    assert last_item(queue).conversation_id == first.conversation_id
    assert queue.claim() is None
    queue.complete(first.id, {"text": "ok"})
    assert queue.claim().payload["text"].endswith("and the linter")


async def test_session_is_saved_before_the_reply_is_posted(convos, conn, queue, slack):
    """The next turn can be claimed the moment this one completes, while its reply is still going to Slack: by then
    the conversation already has the session to resume."""
    await convos.handle_slack(dm("run the tests"))
    item = queue.claim()
    queue.complete(item.id, {})
    seen, original = [], slack.chat_postMessage

    async def post(**kw):
        seen.append(conn.execute("SELECT session_id FROM conversations WHERE id = ?",
                                 (item.conversation_id,)).fetchone()[0])
        return await original(**kw)

    slack.chat_postMessage = post
    await convos.on_result(queue.get(item.id), {"outcome": "done", "text": "all green", "session_id": "S1"})
    assert seen == ["S1"]


async def test_dm_reply_is_posted_in_its_thread(convos, queue, slack):
    event = dm("hi")
    await convos.handle_slack(event)
    await finish_turn(convos, queue, "hello")
    assert (slack.posts[-1]["channel"], slack.posts[-1]["thread_ts"], slack.posts[-1]["text"]) == \
        ("D-1", event["ts"], "hello")


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
    assert "reply_thread_ts" not in last_item(queue).payload  # its own thread: answered there anyway


async def test_top_level_dm_does_not_continue_a_threaded_dm_conversation(convos, conn, queue):
    old = add_conversation(conn, project=None, channel="D-1", thread_ts="5.1")
    event = dm("hello again")
    await convos.handle_slack(event)
    new = last_item(queue).conversation_id
    assert new != old
    assert conn.execute("SELECT thread_ts FROM conversations WHERE id = ?", (new,)).fetchone()[0] == event["ts"]
    await convos.handle_slack(dm_reply("5.1", "about that thread"))
    assert last_item(queue).conversation_id == old


async def test_dm_answer_clears_waiting(convos, conn, queue):
    """A question asked in a DM thread is answered in that thread."""
    await convos.handle_slack(dm("deploy it"))
    cid = last_item(queue).conversation_id
    task_id = task_waiting_on(conn, cid)
    record_message(conn, "D-1", "900.0", cid)  # the bot's question
    await convos.handle_slack(dm_reply("900.0", "prod, actually"))
    assert last_item(queue).conversation_id == cid
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == 0
    assert tasks.get_task(conn, task_id).status == Status.working


async def test_top_level_dm_leaves_an_open_question_open(convos, conn, queue):
    """A new top-level DM is a new request, not the answer to a question waiting in another thread."""
    await convos.handle_slack(dm("deploy it"))
    old = last_item(queue).conversation_id
    task_id = task_waiting_on(conn, old)
    await convos.handle_slack(dm("something new"))
    assert last_item(queue).conversation_id != old
    assert conn.execute("SELECT waiting FROM conversations WHERE id = ?", (old,)).fetchone()[0] == 1
    assert tasks.get_task(conn, task_id).status == Status.needs_input


class SlowNotifier(FakeNotifier):
    """Holds task_changed until released, so another message can arrive meanwhile."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def task_changed(self, task):
        self.entered.set()
        await self.release.wait()
        await super().task_changed(task)


async def test_messages_during_the_notifier_call_stay_together(convos, conn, queue):
    """Routing settles before the notifier is awaited: a second reply that arrives while the thread's answer is
    still being handled lands where it would have landed a moment later."""
    await convos.handle_slack(dm("deploy it"))
    old = last_item(queue).conversation_id
    task_waiting_on(conn, old)
    record_message(conn, "D-1", "900.0", old)  # the bot's question
    convos.notifier = SlowNotifier()
    answer = asyncio.create_task(convos.handle_slack(dm_reply("900.0", "staging")))
    await convos.notifier.entered.wait()
    await convos.handle_slack(dm_reply("900.0", "and run the smoke tests"))
    convos.notifier.release.set()
    await answer
    items = conn.execute("SELECT conversation_id FROM work_items ORDER BY rowid").fetchall()
    assert [r[0] for r in items] == [old, old, old]


async def test_thread_reply_to_an_old_dm_chat_is_answered_in_its_thread(convos, conn, queue, slack):
    sent = sent_ts(slack)
    await convos.handle_slack(dm("first chat"))
    old = (await finish_turn(convos, queue, "answer one")).conversation_id
    await convos.handle_slack(dm_reply(sent[0], "one more thing about that"))
    item = last_item(queue)
    assert item.conversation_id == old and item.payload["reply_thread_ts"] == sent[0]
    await finish_turn(convos, queue, "answer two")
    assert (slack.posts[-1]["channel"], slack.posts[-1]["thread_ts"]) == ("D-1", sent[0])
    assert conn.execute("SELECT conversation_id FROM slack_messages WHERE ts = ?", (sent[1],)).fetchone()[0] == old
