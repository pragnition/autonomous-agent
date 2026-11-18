from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import db
from agent.core.agent_tools import AgentToolRouter
from agent.core.conversations import Conversations
from agent.core.parking import Parking, parse_until, register_parking_tools
from agent.core.sessions import WorkQueue
from agent.proxy import AccountUsage, ProxyUnavailable, SoftCap, Usage
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack
from test_conversations import FakePayloads, reply

SGT = ZoneInfo("Asia/Singapore")
# Thu 2026-11-19 10:00 +08; lynus's 5h window resets at 16:00 +08, so its cap lifts at 14:30
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=SGT).timestamp()
RESET = datetime(2026, 11, 19, 16, 0, tzinfo=SGT).timestamp()


class FakeProxy:
    def __init__(self, accounts: dict[str, AccountUsage]):
        self.accounts, self.fail = accounts, False

    async def usage(self):
        if self.fail:
            raise ProxyUnavailable("down")
        return Usage(self.accounts, soft_cap=SoftCap(), now=clock[0])


clock = [NOW]


@pytest.fixture(autouse=True)
def _reset_clock():
    clock[0] = NOW


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def setup(conn, tmp_path):
    queue, slack = WorkQueue(conn), FakeSlack()
    config = make_config(tmp_path)
    router = AgentToolRouter(conn, config=config, packs={"sample": make_pack()}, queue=queue, slack=slack,
                             notifier=FakeNotifier(), secret_values=[])
    convos = Conversations(conn, config=config, packs={"sample": make_pack()}, queue=queue, slack=slack,
                           payloads=FakePayloads(), notifier=FakeNotifier(), bot_user_id="UBOT")
    proxy = FakeProxy({"lynus": AccountUsage("allowed", 1, True, used_5h=0.41, reset_5h=RESET),
                       "badger": AccountUsage("allowed", 2, True, used_5h=0.70, reset_5h=RESET + 3600)})
    parking = Parking(conn, proxy, resume=convos.resume_parked, timezone="Asia/Singapore", clock=lambda: clock[0])
    register_parking_tools(router, parking)
    return router, convos, parking, proxy, queue, slack


def turn_in(conn, queue, *, account="lynus", channel="C-SAMPLE", thread_ts="t1", **payload):
    cid = add_conversation(conn, channel=channel, thread_ts=thread_ts)
    conn.execute("UPDATE conversations SET session_id = 's-1', session_account = ? WHERE id = ?", (account, cid))
    item = add_item(queue, cid, origin="[from ann · #sample]", source="slack", requester="ann",
                    message_ts="t1", **payload)
    queue.claim()
    return cid, item


def test_parse_until():
    assert parse_until("14:30", now=NOW, tz=SGT) == datetime(2026, 11, 19, 14, 30, tzinfo=SGT).timestamp()
    assert parse_until("09:00", now=NOW, tz=SGT) == datetime(2026, 11, 20, 9, 0, tzinfo=SGT).timestamp()
    assert parse_until("2026-11-19T15:00", now=NOW, tz=SGT) == datetime(2026, 11, 19, 15, tzinfo=SGT).timestamp()
    assert parse_until("2026-11-19T07:00:00Z", now=NOW, tz=SGT) == datetime(2026, 11, 19, 15, tzinfo=SGT).timestamp()
    assert parse_until("25:00", now=NOW, tz=SGT) is None
    assert parse_until("soon", now=NOW, tz=SGT) is None


async def test_status_shows_the_accounts_window(setup, conn):
    router, _, _, proxy, queue, _ = setup
    _, item = turn_in(conn, queue)
    text = (await router.call(router.context_for(item), "status", {})).text
    assert ("account: lynus · 5h window 41% used of its 65% cap · resets 16:00 +08 (in 6h00m) · "
            "cap lifts to 100% at 14:30 +08 (in 4h30m)") in text
    assert "accounts under their cap: 1 of 2" in text
    assert "running: 1" in text   # the built-in lines are still there

    proxy.accounts["lynus"] = AccountUsage("allowed", 1, True, used_5h=0.66, reset_5h=RESET)
    assert "over its cap: park until it lifts" in (await router.call(router.context_for(item), "status", {})).text
    clock[0] = RESET - 3600
    text = (await router.call(router.context_for(item), "status", {})).text
    assert "of its 100% cap" in text and "cap lifted" in text and "over its cap" not in text

    proxy.fail = True
    text = (await router.call(router.context_for(item), "status", {})).text
    assert "usage not known" in text and "running: 1" in text


async def test_status_before_the_session_has_an_account(setup, conn):
    router, _, _, _, queue, _ = setup
    _, item = turn_in(conn, queue, account=None)
    assert "account: not known yet" in (await router.call(router.context_for(item), "status", {})).text


async def test_park_until_the_cap_lifts_then_carry_on(setup, conn):
    router, _, parking, _, queue, slack = setup
    cid, item = turn_in(conn, queue)
    reply_text = (await router.call(router.context_for(item), "park", {"note": "polish the roster page"})).text
    assert reply_text.startswith("Parked until 14:30 +08 (in 4h30m). End this turn now")
    assert slack.posts[-1]["text"] == "Parked until 14:30 +08; reply here to resume earlier."
    assert slack.posts[-1]["thread_ts"] == "t1"
    queue.complete(item, {"text": "ok"})

    clock[0] = RESET - 5400 - 60
    await parking.tick()
    assert queue.claim() is None   # not yet

    clock[0] = RESET - 5400
    await parking.tick()
    nxt = queue.claim()
    assert nxt.conversation_id == cid and nxt.payload["follows"] == item
    assert nxt.payload["text"] == ("(You parked this conversation at 10:00 +08 until 14:30 +08. It's time: carry on "
                                   "from where you stopped. Your note: polish the roster page)")
    # the parking turn's message was already answered: this turn neither reacts to it nor counts as a continuation
    assert "message_ts" not in nxt.payload and "continuation" not in nxt.payload
    assert nxt.payload["requester"] == "ann" and nxt.payload["origin"] == "[from ann · #sample]"
    row = conn.execute("SELECT ended_how, resumed_as FROM parks").fetchone()
    assert tuple(row) == ("resumed", nxt.id)
    await parking.tick()   # once
    assert conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'queued'").fetchone()[0] == 0


async def test_park_until_a_named_time(setup, conn):
    router, _, parking, _, queue, _ = setup
    _, item = turn_in(conn, queue)
    assert "Parked until 11:15 +08" in (await router.call(router.context_for(item), "park", {"until": "11:15"})).text
    assert "can't read until" in (await router.call(router.context_for(item), "park", {"until": "later"})).text
    assert "more than 7 days" in (await router.call(router.context_for(item), "park",
                                                    {"until": "2026-12-30T10:00"})).text
    # parking again replaces the open park
    await router.call(router.context_for(item), "park", {"until": "12:00"})
    assert [tuple(r) for r in conn.execute("SELECT ended_how FROM parks ORDER BY id")] == [("replaced",), (None,)]


async def test_park_with_no_cap_to_wait_out_needs_a_time(setup, conn):
    router, _, _, _, queue, _ = setup
    clock[0] = RESET - 3600   # lifted already
    _, item = turn_in(conn, queue)
    text = (await router.call(router.context_for(item), "park", {})).text
    assert text.startswith("there is no cap to wait out on account lynus")
    assert conn.execute("SELECT COUNT(*) FROM parks").fetchone()[0] == 0


async def test_park_on_an_account_without_capacity_waits_for_the_reset(setup, conn):
    router, _, _, proxy, queue, _ = setup
    clock[0] = RESET - 3600
    proxy.accounts["lynus"] = AccountUsage("rejected", None, True, used_5h=1.0, reset_5h=RESET)
    _, item = turn_in(conn, queue)
    assert "Parked until 16:00 +08" in (await router.call(router.context_for(item), "park", {})).text


async def test_a_reply_in_the_thread_resumes_earlier(setup, conn):
    router, convos, parking, _, queue, _ = setup
    cid, item = turn_in(conn, queue)
    await router.call(router.context_for(item), "park", {})
    queue.complete(item, {"text": "ok"})

    await convos.handle_slack(reply("t1", "carry on now please"))
    nxt = queue.claim()
    assert nxt.conversation_id == cid and nxt.payload["text"] == "carry on now please"
    assert conn.execute("SELECT ended_how FROM parks").fetchone()[0] == "replied"
    clock[0] = RESET
    await parking.tick()   # nothing left to carry on
    assert conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'queued'").fetchone()[0] == 0
