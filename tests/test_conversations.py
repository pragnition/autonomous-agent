import asyncio
import itertools
from datetime import UTC, datetime

import pytest

from agent import db, tasks
from agent.core import projects
from agent.core.conversations import NOT_AUTHORISED, Conversations, cut_by_limit, record_message
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


def own_post(ts, channel="C-SAMPLE", **extra):
    """The agent's own message, as Slack echoes it back when the agent posts with its bot token."""
    return {"type": "message", "user": "UBOT", "bot_id": "BSELF", "channel": channel, "ts": ts, "text": "ok?",
            "channel_type": "channel", **extra}


async def test_replies_in_a_thread_the_agent_started_itself_reach_it(convos, queue, conn, lookups):
    await convos.handle_slack(own_post("8.1"))
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    first, second = reply("8.1", "ok"), reply("8.1", "show me the before and after")
    await convos.handle_slack(first)
    await convos.handle_slack(second)
    items = [r["id"] for r in conn.execute("SELECT id FROM work_items ORDER BY rowid")]
    assert len(items) == 2
    one, two = queue.get(items[0]), queue.get(items[1])
    assert one.conversation_id == two.conversation_id == convo(conn, "8.1")["id"]
    assert one.payload["text"] == "ok"
    assert one.payload["origin"] == (f"[from Ann Lee (ann, GitHub @ann-gh) · #chilliclaw thread you started · "
                                     f"{permalink_of('C-SAMPLE', first['ts'])}]")


async def test_only_the_agents_own_top_level_channel_posts_open_a_thread(convos, queue):
    await convos.handle_slack(own_post("8.2", thread_ts="8.0"))  # a reply of its own in some thread
    await convos.handle_slack(own_post("8.3", subtype="message_changed"))
    await convos.handle_slack({**own_post("8.4"), "user": "UOTHER", "bot_id": "B1"})  # another bot
    for thread in ("8.0", "8.2", "8.3", "8.4"):
        await convos.handle_slack(reply(thread, "hello?"))
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


async def test_a_file_the_agent_posts_itself_opens_a_thread_too(convos, queue):
    await convos.handle_slack(own_post("8.5", subtype="file_share"))
    await convos.handle_slack(reply("8.5", "nice"))
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1


async def test_a_thread_core_recorded_still_routes_to_its_conversation(convos, queue, conn):
    await convos.handle_slack(mention("8.6", "fix it"))
    first = queue.claim()
    queue.complete(first.id, {})
    await convos.handle_slack(own_post("8.6"))  # no-op: core's own posts are already known
    await convos.handle_slack(reply("8.6", "and the other one"))
    assert last_item(queue).conversation_id == first.conversation_id


async def test_replies_to_improve_jensens_proposals_are_left_to_it(convos, queue, tmp_path):
    from agent.watch.approvals import post_proposals

    class Posts:
        def __init__(self):
            self.texts = []

        def post(self, channel, text, thread_ts=None):
            self.texts.append(text)
            return "8.7"

    def gh(args):  # every issue open, unlabelled and just opened; edits and comments succeed
        if args[:2] == ["issue", "list"]:
            return '[{"number": 31, "labels": []}]'
        if args[:2] == ["issue", "view"]:
            return '{"createdAt": "%s"}' % datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ""

    posts = Posts()
    post_proposals(posts, gh, tmp_path, "C-SAMPLE", [{"issue": 31, "change": "x"}], "acme/widgets",
                   datetime.now(UTC))
    assert posts.texts, "post_proposals posted nothing"
    await convos.handle_slack(own_post("8.7", text=posts.texts[0]))
    await convos.handle_slack(reply("8.7", "approve"))
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


SHOT = {"id": "F1", "name": "shot.png", "mimetype": "image/png", "size": 2048}


async def test_untagged_reply_with_a_file_in_a_known_thread_carries_the_file(convos, queue):
    await convos.handle_slack(mention("t8", "the export is broken"))
    await convos.handle_slack({**reply("t8", "like this"), "subtype": "file_share",
                               "files": [SHOT, {"id": "F2", "mode": "tombstone"}]})
    item = last_item(queue)
    assert item.payload["text"] == "like this" and item.payload["files"] == [SHOT]


async def test_a_file_without_text_still_makes_a_turn(convos, queue):
    await convos.handle_slack({**dm(""), "subtype": "file_share", "files": [SHOT]})
    assert last_item(queue).payload["files"] == [SHOT]


async def test_a_message_without_files_carries_none(convos, queue):
    await convos.handle_slack(dm("hi"))
    assert "files" not in last_item(queue).payload


async def test_other_subtypes_still_ignored(convos, queue):
    await convos.handle_slack(mention("t9", "x"))
    queue.claim()
    for subtype in ("message_changed", "channel_join", "bot_message", "message_deleted"):
        await convos.handle_slack({**reply("t9", "x"), "subtype": subtype})
    assert queue.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1


async def test_use_fable_sticks(convos, conn, queue):
    await convos.handle_slack(mention("t1", "use fable and fix x"))
    assert convo(conn, "t1")["model"] == "claude-fable-5-1"
    await convos.handle_slack(reply("t1", "and y"))
    assert last_item(queue).payload["model"] == "claude-fable-5-1"


ALLOWED = ["claude-opus-5-5", "claude-fable-5-1", "claude-opus-4-6", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]


def aliases(model):
    """The ways a person may name an allowed model: its id, and its family and version as words."""
    family, *version = model.removeprefix("claude-").split("-")
    words = [model, model.upper(), f"claude {family} {'.'.join(version)}", f"{family} {'.'.join(version)}"]
    if version and len(version[-1]) == 8:  # a dated id: the version without its date
        words.append(f"{family} {'.'.join(version[:-1])}")
    return words


@pytest.fixture
def model_convos(conn, queue, slack, tmp_path):
    config = make_config(tmp_path, f"\n[claude.models]\nallowed = {ALLOWED!r}\n".replace("'", '"'))
    return Conversations(conn, config=config, packs={"sample": make_pack()}, queue=queue, slack=slack,
                         payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT")


async def test_every_allowed_model_switches_by_id_or_alias(model_convos, conn, queue, slack):
    asked = [(model, alias) for model in ALLOWED for alias in aliases(model)]
    for i, (model, alias) in enumerate(asked):
        lead = ["", "please ", "Can you "][i % 3]
        await model_convos.handle_slack(mention(f"m{i}", f"{lead}use {alias} for this, please"))
        assert convo(conn, f"m{i}")["model"] == model, alias
        assert last_item(queue).payload["model"] == model, alias
    assert slack.posts == []


@pytest.mark.parametrize("asked", ["claude-opus-9-9", "sonnet 4.6", "opus", "fable 5.2", "claude-opus 4.7"])
async def test_a_model_not_allowed_changes_nothing_and_replies(model_convos, conn, queue, slack, asked):
    await model_convos.handle_slack(mention("t1", "use fable"))
    await model_convos.handle_slack(reply("t1", f"now use {asked}"))
    assert convo(conn, "t1")["model"] == "claude-fable-5-1"
    assert last_item(queue).payload["model"] == "claude-fable-5-1"  # the message is still a turn
    [post] = slack.posts
    assert post["thread_ts"] == "t1" and asked in post["text"]
    assert all(f"`{m}`" in post["text"] for m in ALLOWED)
    recorded = conn.execute("SELECT conversation_id FROM slack_messages WHERE ts = ?", (f"{slack._ts}.0",)).fetchone()
    assert recorded[0] == convo(conn, "t1")["id"]  # a thread under the reply finds its way back


async def test_a_spaced_version_after_a_dashed_family_is_not_dropped(conn, queue, slack, tmp_path):
    convos = Conversations(conn, config=make_config(tmp_path), packs={}, queue=queue, slack=slack,
                           payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT")
    await convos.handle_slack(mention("t1", "use claude-opus 4.6"))  # not allowed by default: not opus 5.5
    assert convo(conn, "t1")["model"] is None and len(slack.posts) == 1


async def test_the_reply_failing_still_queues_the_turn(model_convos, conn, queue, slack):
    async def down(**kw):
        raise RuntimeError("slack down")
    slack.chat_postMessage = down
    await model_convos.handle_slack(mention("t1", "use sonnet 4.6"))
    assert last_item(queue).payload["text"] == "use sonnet 4.6" and convo(conn, "t1")["model"] is None


async def test_use_default_goes_back_to_the_default(model_convos, conn, queue):
    await model_convos.handle_slack(mention("t1", "use opus 4.6"))
    await model_convos.handle_slack(reply("t1", "use default."))
    assert convo(conn, "t1")["model"] is None  # payloads send [claude.models] default
    await model_convos.handle_slack(reply("t1", "use opus 4.6"))
    await model_convos.handle_slack(reply("t1", "use default\nand fix x"))
    assert convo(conn, "t1")["model"] is None
    await model_convos.handle_slack(reply("t1", "use sonnet 5.5"))
    await model_convos.handle_slack(reply("t1", "Use the default model"))
    assert convo(conn, "t1")["model"] is None


@pytest.mark.parametrize("text", ["use the API for this", "use default settings", "we use pytest", "used opus 4.6",
                                  "use claude-code to run it", "don't use opus here",
                                  "make the labeler use haiku 4.5", "we use sonnet 4.6 in the app"])
async def test_ordinary_words_after_use_switch_nothing(model_convos, conn, slack, text):
    await model_convos.handle_slack(mention("t1", text))
    assert convo(conn, "t1")["model"] is None and slack.posts == []


async def test_use_fable_is_hard_whatever_the_allowed_list(model_convos, convos, conn):
    await model_convos.handle_slack(mention("t1", "Use Fable and fix x"))
    await convos.handle_slack(mention("t2", "use fable"))  # no allowed list: just default and hard
    await convos.handle_slack(mention("t3", "fix x, and use fable for it"))  # anywhere in the message, as before
    assert convo(conn, "t1")["model"] == convo(conn, "t2")["model"] == convo(conn, "t3")["model"] == "claude-fable-5-1"


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


async def test_reaction_moves_from_seen_to_working_to_done(convos, queue, slack):
    await convos.handle_slack(mention("t12", "question"))
    item = queue.claim()
    await convos.on_start(item)
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"text": "answer", "outcome": "done"})
    assert [r["name"] for r in slack.reactions] == ["eyes", "hourglass_flowing_sand", "white_check_mark"]
    assert [r["name"] for r in slack.unreacted] == ["eyes", "hourglass_flowing_sand"]
    assert {r["timestamp"] for r in slack.reactions + slack.unreacted} == {"t12"}


async def test_failed_turn_ends_on_a_warning(convos, queue, slack):
    await convos.handle_slack(mention("t13", "question"))
    item = queue.claim()
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"outcome": "error"})
    assert slack.reactions[-1]["name"] == "warning"


async def test_a_continued_turn_stays_working_until_it_finishes(convos, queue, slack, conn):
    first = await start_build(convos, queue, conn, "t14")
    await convos.on_start(first)
    queue.complete(first.id, {})
    await convos.on_result(queue.get(first.id), {"outcome": "stalled", "error": "no progress for 1800 s"})
    assert slack.reactions[-1]["name"] == "hourglass_flowing_sand"
    nxt = queue.claim()
    await convos.on_start(nxt)
    queue.complete(nxt.id, {})
    await convos.on_result(queue.get(nxt.id), {"text": "done now", "outcome": "done"})
    assert [r["name"] for r in slack.reactions] == ["eyes", "hourglass_flowing_sand", "white_check_mark"]
    assert slack.reactions[-1]["timestamp"] == "t14"


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


async def test_the_session_is_saved_with_the_account_it_ran_on(convos, queue, conn):
    await convos.handle_slack(mention("t9", "question"))
    item = queue.claim()
    await convos.on_beat(item, {"session_id": "live-1", "account": "maple", "read": [], "accepts_messages": False})
    assert (convo(conn, "t9")["session_id"], convo(conn, "t9")["session_account"]) == ("live-1", "maple")
    # the run moved to a fresh session on another account: the next turn may only resume it there
    result = {"outcome": "done", "text": "ok", "session_id": "sess-2", "account_code": "cedar"}
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)
    assert (convo(conn, "t9")["session_id"], convo(conn, "t9")["session_account"]) == ("sess-2", "cedar")


async def test_a_session_whose_account_is_not_known_yet_is_saved_without_one(convos, queue, conn):
    await convos.handle_slack(mention("t10", "question"))
    item = queue.claim()
    conn.execute("UPDATE conversations SET session_id = 'old', session_account = 'maple' WHERE id = ?",
                 (item.conversation_id,))
    await convos.on_beat(item, {"session_id": "live-2", "read": [], "accepts_messages": False})
    assert (convo(conn, "t10")["session_id"], convo(conn, "t10")["session_account"]) == ("live-2", None)


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


async def test_timed_out_turn_says_it_ran_out_of_time_and_stops(convos, queue, slack):
    await failed_turn(convos, queue, "t10t", {"outcome": "timeout", "error": "no result within 21600 s"})
    assert slack.posts[-1]["text"] == "I ran out of time before finishing that (no result within 21600 s)."
    assert queue.claim() is None    # a time budget is the end of the run: it is never carried on


async def test_a_stalled_turn_says_it_got_stuck_and_keeps_its_session(convos, queue, slack, conn):
    await convos.handle_slack(mention("t10i", "question"))
    item = queue.claim()
    result = {"outcome": "stalled", "error": "no progress for 1800 s", "session_id": "sess-1"}
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)
    assert slack.posts[-1]["text"] == "I got stuck before finishing that (no progress for 1800 s)."
    assert slack.reactions[-1]["name"] == "warning" and queue.claim() is None
    assert convo(conn, "t10i")["session_id"] == "sess-1"    # a reply picks the session up where it stopped


async def start_build(convos, queue, conn, thread):
    """A conversation that is a long project's build, with its first turn claimed."""
    await convos.handle_slack(mention(thread, "build the spec"))
    item = queue.claim()
    projects.start(conn, name="exams", repo="org/exams", conversation_id=item.conversation_id, started_by="ann",
                   thread_link=None)
    return item


async def fail(convos, queue, item, result):
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)


async def test_a_build_turn_that_fails_says_so_and_carries_on(convos, queue, slack, conn):
    first = await start_build(convos, queue, conn, "t60")
    reactions = len(slack.reactions)
    await fail(convos, queue, first, {"outcome": "error", "error": "500 API Error: overloaded"})
    assert "500 API Error: overloaded" in slack.posts[-1]["text"]  # the real error still reaches the thread
    assert len(slack.reactions) == reactions                        # the message keeps its ⏳
    nxt = queue.claim()
    assert nxt.conversation_id == first.conversation_id and nxt.payload["continuation"] == 1
    assert "500 API Error: overloaded" in nxt.payload["text"] and "memory note" in nxt.payload["text"]


async def test_a_build_stops_after_three_failed_turns_in_a_row(convos, queue, slack, conn):
    item = await start_build(convos, queue, conn, "t61")
    for _ in range(3):
        await fail(convos, queue, item, {"outcome": "error", "error": "boom"})
        item = queue.claim()
    await fail(convos, queue, item, {"outcome": "error", "error": "boom"})
    assert queue.claim() is None
    assert "boom" in slack.posts[-1]["text"]


async def test_an_interrupted_build_turn_stops(convos, queue, slack, conn):
    # a person's stop never posts a result; one that does is a stale claim, and retrying it would run two at once
    item = await start_build(convos, queue, conn, "t64")
    await fail(convos, queue, item, {"outcome": "interrupted", "error": "interrupted"})
    assert queue.claim() is None


async def test_a_stalled_build_turn_says_so_and_carries_on_in_its_session(convos, queue, slack, conn):
    first = await start_build(convos, queue, conn, "t62")
    await fail(convos, queue, first, {"outcome": "stalled", "error": "no progress for 1800 s", "session_id": "s-1"})
    assert slack.posts[-1]["text"] == "I got stuck before finishing that (no progress for 1800 s)."
    nxt = queue.claim()
    assert nxt.conversation_id == first.conversation_id and nxt.payload["continuation"] == 1
    assert nxt.payload["text"].startswith("(Your last turn got stuck: no progress for 1800 s. Carry on")
    assert convo(conn, "t62")["session_id"] == "s-1"


async def test_stalled_and_failed_build_turns_count_together_toward_the_three_in_a_row(convos, queue, slack, conn):
    item = await start_build(convos, queue, conn, "t65")
    for outcome in ("error", "stalled", "error"):
        await fail(convos, queue, item, {"outcome": outcome, "error": "x"})
        item = queue.claim()
    await fail(convos, queue, item, {"outcome": "stalled", "error": "x"})
    assert queue.claim() is None


async def test_a_failed_turn_outside_a_build_still_stops(convos, queue, slack, conn):
    await failed_turn(convos, queue, "t63", {"outcome": "error", "error": "boom"})
    assert queue.claim() is None


def test_what_counts_as_cut_by_a_usage_limit():
    assert cut_by_limit("error", "429 [{'type': 'rate_limit_error'}]")
    assert cut_by_limit("error", "account badger is rejected")
    assert cut_by_limit("error", "account badger is disabled")
    assert not cut_by_limit("error", "500 API Error: overloaded")
    assert not cut_by_limit("error", None)
    assert not cut_by_limit("timeout", "429 later")


async def test_a_turn_cut_by_a_limit_waits_and_is_carried_on_once(convos, queue, slack, conn):
    await convos.handle_slack(mention("t70", "question"))
    item = queue.claim()
    reactions = len(slack.reactions)
    await fail(convos, queue, item, {"outcome": "error", "error": "account badger is rejected", "session_id": "s-1",
                                     "account_code": "badger"})
    assert slack.posts[-1]["text"].startswith("A usage limit cut me off (account badger is rejected).")
    assert len(slack.reactions) == reactions          # the message keeps its ⏳
    assert queue.claim() is None                      # nothing runs until the usage watch says so
    # its session and the account it ran on are kept: the carry-on turn resumes it only if it stays on that account
    assert tuple(conn.execute("SELECT session_id, session_account FROM conversations WHERE id = ?",
                              (item.conversation_id,)).fetchone()) == ("s-1", "badger")
    assert conn.execute("SELECT type FROM events WHERE type = 'usage_limit_cut'").fetchone() is not None

    new_id = convos.resume_after_limit(item.id)
    assert convos.resume_after_limit(item.id) is None  # once
    nxt = queue.claim()
    assert nxt.id == new_id and nxt.conversation_id == item.conversation_id and nxt.payload["limit_resume"] == 1
    assert nxt.payload["text"].startswith("(A usage limit cut your last turn off: account badger is rejected.")

    # cut off again, it fails like any other turn: never a second resume
    await fail(convos, queue, nxt, {"outcome": "error", "error": "429 rate_limit_error"})
    assert slack.posts[-1]["text"].startswith("That failed before I could finish it:")
    assert conn.execute("SELECT COUNT(*) FROM limit_waits").fetchone()[0] == 1
    assert queue.claim() is None


async def test_a_build_turn_cut_by_a_limit_retries_as_before(convos, queue, slack, conn):
    item = await start_build(convos, queue, conn, "t71")
    await fail(convos, queue, item, {"outcome": "error", "error": "429 rate_limit_error"})
    assert queue.claim().payload["failures"] == 1     # the build's own retry, which the Gate holds until capacity
    assert conn.execute("SELECT COUNT(*) FROM limit_waits").fetchone()[0] == 0


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
    assert tuple(conn.execute("SELECT session_id, session_account FROM conversations WHERE id = ?",
                              (item.conversation_id,)).fetchone()) == (None, None)


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


# schedules

from agent.core.schedules import Schedule


def scheduled(**over):
    fields = dict(name="nightly-sweep", cron="0 22 * * *", channel="C-SAMPLE", prompt="Sweep it", by="ann",
                  link="https://l")
    fields.update(over)
    return Schedule(**fields)


def only_item(queue, conn):
    rows = conn.execute("SELECT id FROM work_items").fetchall()
    assert len(rows) == 1
    return queue.get(rows[0]["id"])


async def test_scheduled_run_posts_nothing_and_queues_an_unattended_turn(convos, slack, queue, conn):
    await convos.start_scheduled(scheduled())
    assert slack.posts == [] and slack.opened == []
    item = only_item(queue, conn)
    convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert (convo["channel"], convo["thread_ts"], convo["project"]) == (None, None, "sample")
    p = item.payload
    assert p["text"] == "Sweep it" and p["actor"] == "ann" and p["unattended"] is True
    assert p["origin"].startswith("[schedule nightly-sweep · 0 22 * * * · set up by ")
    assert p["origin"].endswith(" · https://l]") and "GitHub @ann-gh" in p["origin"]


async def test_scheduled_run_for_a_person_posts_nothing(convos, slack, queue, conn):
    await convos.start_scheduled(scheduled(channel="bob", link=None))
    assert slack.posts == [] and slack.opened == []
    convo = conn.execute("SELECT * FROM conversations").fetchone()
    assert (convo["channel"], convo["project"]) == (None, None)


async def test_scheduled_run_by_someone_unknown_has_no_actor(convos, queue, conn):
    await convos.start_scheduled(scheduled(by="carol", link=None))
    p = only_item(queue, conn).payload
    assert p["actor"] is None and p["origin"] == "[schedule nightly-sweep · 0 22 * * * · set up by carol]"


async def test_scheduled_runs_final_message_goes_nowhere(convos, slack, queue):
    await convos.start_scheduled(scheduled(channel="bob"))
    await finish_turn(convos, queue, "nothing new")
    assert slack.posts == [] and slack.reactions == []


async def test_a_scheduled_run_out_of_time_stops_and_says_so(convos, queue, slack):
    await convos.start_scheduled(scheduled())
    item = queue.claim()
    result = {"outcome": "timeout", "error": "no result within 21600 s"}
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)
    assert queue.claim() is None
    assert "didn't finish" in slack.posts[-1]["text"] and "21600 s" in slack.posts[-1]["text"]


async def test_failed_scheduled_run_says_so_and_replies_continue_it(convos, slack, queue, conn):
    await convos.start_scheduled(scheduled(channel="bob"))
    item = queue.claim()
    result = {"outcome": "error", "error": "gh: not logged in"}
    queue.complete(item.id, result)
    await convos.on_result(queue.get(item.id), result)
    assert slack.opened == [{"users": "U2"}] and len(slack.posts) == 1
    post = slack.posts[0]
    assert post["channel"] == "D-U2" and "thread_ts" not in post
    assert post["text"].startswith("My nightly-sweep run didn't finish.") and "gh: not logged in" in post["text"]
    convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (item.conversation_id,)).fetchone()
    assert (convo["channel"], convo["thread_ts"]) == ("D-U2", "101.0")
    await convos.handle_slack({**reply("101.0", "try again", user="U2", channel="D-U2"), "channel_type": "im"})
    assert last_item(queue).conversation_id == item.conversation_id


async def test_scheduled_run_that_cannot_be_queued_posts_nothing(convos, slack, conn):
    def broken(*a, **k):
        raise RuntimeError("database is locked")
    convos.payloads.turn = broken
    with pytest.raises(RuntimeError):  # the scheduler reports it to the admins and retries on its next tick
        await convos.start_scheduled(scheduled())
    assert slack.posts == []
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


# where each turn came from and who asked (the task record)

async def test_slack_turn_records_source_and_requester(convos, queue):
    await convos.handle_slack(dm("study the codebase"))
    p = queue.claim().payload
    assert (p["source"], p["requester"], p.get("follows")) == ("slack", "ann", None)


async def test_scheduled_turn_records_schedule_and_by(convos, queue, conn):
    await convos.start_scheduled(scheduled())
    p = only_item(queue, conn).payload
    assert (p["source"], p["requester"]) == ("schedule", "ann")


async def test_scheduled_turn_by_someone_unknown_keeps_their_name(convos, queue, conn):
    await convos.start_scheduled(scheduled(by="carol", link=None))
    assert only_item(queue, conn).payload["requester"] == "carol"


async def test_continuation_follows_the_turn_it_continues(convos, queue, conn):
    first = await start_build(convos, queue, conn, "t41")
    result = {"outcome": "stalled", "error": "no progress for 1800 s"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    nxt = queue.claim()
    assert nxt.payload["follows"] == first.id
    assert (nxt.payload["source"], nxt.payload["requester"]) == ("slack", "ann")


# heartbeats: a running turn's live session and the messages that arrive while it runs

async def test_a_message_to_a_busy_thread_is_handed_to_the_running_turn(convos, queue, slack, conn):
    await convos.handle_slack(mention("t50", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t50", "how's it going?"))
    out = await convos.on_beat(first, {"session_id": "live-1", "read": [], "accepts_messages": True})
    [msg] = out["messages"]
    assert msg["payload"]["text"] == "how's it going?" and queue.get(msg["id"]).status == "delivered"
    assert slack.reactions[-1]["name"] == "hourglass_flowing_sand"
    assert convo(conn, "t50")["session_id"] == "live-1"     # a restarted turn resumes this session


async def test_a_read_message_is_done_and_ticked(convos, queue, slack):
    await convos.handle_slack(mention("t51", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t51", "status?"))
    [msg] = (await convos.on_beat(first, {"accepts_messages": True}))["messages"]
    await convos.on_beat(first, {"read": [msg["id"]], "accepts_messages": True})
    assert queue.get(msg["id"]).status == "done"
    assert slack.reactions[-1]["name"] == "white_check_mark"


async def test_an_unread_message_runs_before_the_carry_on(convos, queue, conn):
    first = await start_build(convos, queue, conn, "t52")
    await convos.handle_slack(reply("t52", "status?"))
    [msg] = (await convos.on_beat(first, {"accepts_messages": True}))["messages"]
    result = {"outcome": "stalled", "error": "no progress for 1800 s", "session_id": "live-1"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    assert queue.claim().id == msg["id"]


def last_reaction_for(slack, ts):
    return [r for r in slack.reactions if r["timestamp"] == ts][-1]


async def test_a_carry_on_never_lands_in_the_next_turn_that_runs_the_unread_message(convos, queue, slack, conn):
    """Build turn A stalls with message M delivered but unread. complete(A) releases M back to queued, and
    on_result(A) enqueues carry-on C. M is older than C, so it is claimed and run first — but its own heartbeat
    must not deliver C into it: C runs as its own turn, after M, not folded into M as a mid-turn message. Until
    C actually runs, the original request (t52) keeps its hourglass, not a checkmark."""
    first = await start_build(convos, queue, conn, "t52")
    await convos.on_start(first)
    await convos.handle_slack(reply("t52", "status?"))
    [msg] = (await convos.on_beat(first, {"accepts_messages": True}))["messages"]
    result = {"outcome": "stalled", "error": "no progress for 1800 s", "session_id": "live-1"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    assert last_reaction_for(slack, "t52")["name"] == "hourglass_flowing_sand"

    nxt = queue.claim()
    assert nxt.id == msg["id"]                      # M, not the carry-on, runs next
    out = await convos.on_beat(nxt, {"accepts_messages": True})
    assert out["messages"] == []                    # the carry-on is never handed into M's turn

    msg_result = {"text": "still going", "outcome": "done"}
    queue.complete(nxt.id, msg_result)
    await convos.on_result(queue.get(nxt.id), msg_result)
    assert last_reaction_for(slack, msg["payload"]["message_ts"])["name"] == "white_check_mark"
    assert last_reaction_for(slack, "t52")["name"] == "hourglass_flowing_sand"  # still no checkmark on the request

    carry_on = queue.claim()
    assert carry_on.payload.get("continuation") == 1
    assert carry_on.id != msg["id"]


async def test_a_runner_that_cannot_take_messages_gets_none(convos, queue, slack):
    await convos.handle_slack(mention("t53", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t53", "status?"))
    reactions = len(slack.reactions)
    assert (await convos.on_beat(first, {"accepts_messages": False}))["messages"] == []
    assert len(slack.reactions) == reactions
    assert (await convos.on_beat(first, {"accepts_messages": True}))["messages"] != []


def spawn(conn, queue, parent_cid, agent_id="A-1"):
    """A spawned agent of parent_cid, with its first turn queued."""
    from agent.core import spawned
    from agent.core.conversations import create_conversation
    child = create_conversation(conn, channel=None, thread_ts=None, project="sample", is_primary=0)
    spawned.record(conn, agent_id=agent_id, parent_conversation_id=parent_cid, child_conversation_id=child,
                   spawned_by="W-x", prompt="compare A and B", cwd=None,
                   parent={"actor": "ann", "origin": "[from ann · DM]", "source": "slack", "requester": "ann"})
    queue.enqueue(child, "turn", {"text": "compare A and B", "actor": "ann"})
    return child


async def test_a_finished_spawned_agent_starts_a_turn_in_its_parents_thread(convos, conn, queue, slack):
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    child = spawn(conn, queue, parent)
    await finish_turn(convos, queue, "B is 12% faster")
    assert slack.posts == []  # the agent has no thread of its own; nothing is posted for it
    turn = last_item(queue)
    assert turn.conversation_id == parent and turn.status == "queued"
    assert "<agent-notification>" in turn.payload["text"] and "B is 12% faster" in turn.payload["text"]
    assert "A-1" in turn.payload["text"] and "finished" in turn.payload["text"]
    assert turn.payload["origin"] == "[from ann · DM] (report from agent A-1)"
    assert (turn.payload["actor"], turn.payload["source"], turn.payload["requester"]) == ("ann", "slack", "ann")
    row = conn.execute("SELECT status, result FROM spawned_agents WHERE child_conversation_id = ?", (child,)).fetchone()
    assert (row["status"], row["result"]) == ("done", "B is 12% faster")
    # the parent's turn answers in the parent's thread
    await finish_turn(convos, queue, "Done: B wins")
    assert [(p["channel"], p["thread_ts"], p["text"]) for p in slack.posts] == [("C-SAMPLE", "7.0", "Done: B wins")]


async def test_a_spawned_agent_reports_once_after_it_is_carried_on(convos, conn, queue):
    # A turn has no time limit to carry on from any more (#24); a usage limit still cuts one off and carries it on
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    spawn(conn, queue, parent)
    item = queue.claim()
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "text": "", "error": "429 rate limit"})
    assert conn.execute("SELECT COUNT(*) FROM work_items WHERE conversation_id = ?", (parent,)).fetchone()[0] == 0
    carry_on = queue.get(convos.resume_after_limit(item.id))
    assert carry_on.conversation_id != parent
    await finish_turn(convos, queue, "done now")
    reports = conn.execute("SELECT payload FROM work_items WHERE conversation_id = ?", (parent,)).fetchall()
    assert len(reports) == 1 and "done now" in reports[0]["payload"]


async def test_a_failed_spawned_agent_reports_the_failure(convos, conn, queue):
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    spawn(conn, queue, parent)
    item = queue.claim()
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "text": "", "error": "boom"})
    turn = last_item(queue)
    assert turn.conversation_id == parent and "failed (error)" in turn.payload["text"] and "boom" in turn.payload["text"]


async def test_a_report_reaches_a_running_parent_mid_turn(convos, conn, queue):
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    queue.enqueue(parent, "turn", {"text": "spawn some agents", "actor": "ann"})
    running = queue.claim()
    spawn(conn, queue, parent)
    await finish_turn(convos, queue, "found it")
    got = await convos.on_beat(running, {"accepts_messages": True})
    assert len(got["messages"]) == 1 and "found it" in got["messages"][0]["payload"]["text"]


def reports_to(conn, parent):
    return [r["payload"] for r in conn.execute("SELECT payload FROM work_items WHERE conversation_id = ? "
                                               "ORDER BY rowid", (parent,))]


async def test_a_spawned_agent_that_parked_reports_only_once_the_park_is_carried_on_and_done(convos, conn, queue):
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    child = spawn(conn, queue, parent)
    item = queue.claim()
    conn.execute("INSERT INTO parks(conversation_id, work_item_id, until_at, note, created_at) VALUES (?, ?, ?, ?, ?)",
                 (child, item.id, "2099-01-01T00:00:00Z", "rerun the B side", db.utcnow()))
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"outcome": "done", "text": "Parked until the cap lifts."})
    assert reports_to(conn, parent) == []
    # the park is due: core carries the agent on, and its turn ends with nothing more to come
    convos.resume_parked(child, item.id, "(It's time: carry on.)")
    conn.execute("UPDATE parks SET ended_at = ?, ended_how = 'resumed' WHERE conversation_id = ?", (db.utcnow(), child))
    await finish_turn(convos, queue, "B is 12% faster")
    (report,) = reports_to(conn, parent)
    assert "B is 12% faster" in report and "Parked until" not in report


async def test_a_spawned_agent_with_another_turn_queued_reports_after_that_one(convos, conn, queue):
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    child = spawn(conn, queue, parent)
    queue.enqueue(child, "turn", {"text": "and the C side too", "actor": "ann"})
    await finish_turn(convos, queue, "A and B done")
    assert reports_to(conn, parent) == []
    await finish_turn(convos, queue, "A, B and C done")
    (report,) = reports_to(conn, parent)
    assert "A, B and C done" in report


async def test_a_failed_spawned_agent_is_not_retried_the_parent_decides(convos, conn, queue):
    # even when the agent's conversation became a long project's build, whose failed turns otherwise retry
    parent = add_conversation(conn, channel="C-SAMPLE", thread_ts="7.0")
    child = spawn(conn, queue, parent)
    projects.start(conn, name="ab", repo="acme/ab", conversation_id=child, thread_link=None, started_by="ann")
    item = queue.claim()
    queue.complete(item.id, {})
    await convos.on_result(queue.get(item.id), {"outcome": "error", "text": "", "error": "boom"})
    assert conn.execute("SELECT COUNT(*) FROM work_items WHERE conversation_id = ? AND status = 'queued'",
                        (child,)).fetchone()[0] == 0
    (report,) = reports_to(conn, parent)
    assert "failed (error)" in report and "won't be retried" in report
