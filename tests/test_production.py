import asyncio

import pytest

from agent import db
from agent.core.agent_tools import AgentToolRouter
from agent.core.production import register_production_tools, scrub_customer
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack


def test_scrub_keeps_debugging_detail():
    text = "2026-11-13T10:00:00Z GET /api/citations/42 500 user=jane@x.com ip=10.1.2.3 at export.py:88 (17 times)"
    out, hits = scrub_customer(text, [])
    assert "jane@x.com" not in out and "10.1.2.3" not in out and hits == 2
    assert "2026-11-13T10:00:00Z" in out and "/api/citations/42" in out and "export.py:88" in out and "17 times" in out


def test_scrub_tokens_phones_and_ipv6():
    text = ("auth Bearer abc123def456ghi tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part phone +65 9123 4567 "
            "v6 2001:db8:85a3:0:0:8a2e:370:7334 date 2026-11-13 took 1234 ms")
    out, hits = scrub_customer(text, [])
    for secret in ("abc123def456ghi", "eyJhbGciOiJIUzI1NiJ9", "9123 4567", "2001:db8"):
        assert secret not in out
    assert "2026-11-13" in out and "1234 ms" in out and hits == 4


def test_pack_pattern():
    assert scrub_customer("cust C-12345 failed", [r"C-\d{5}"])[0] == "cust [customer-data] failed"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def router(conn, tmp_path):
    r = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack(scrub_patterns=[r"C-\d{5}"])},
                        queue=WorkQueue(conn), memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(),
                        notifier=FakeNotifier(), secret_values=["SUPERSECRETVALUE"])
    register_production_tools(r, timeout_s=2, poll_s=0.05)
    return r


def ctx_in(router, sandbox):
    cid = add_conversation(router.conn, sandbox=sandbox)
    return router.context_for(add_item(router.queue, cid, sandbox, actor="bob"))


@pytest.fixture
def ctx_code(router):
    return ctx_in(router, "code-sample")


@pytest.fixture
def ctx_debug(router):
    return ctx_in(router, "debug-sample")


@pytest.fixture
def complete_next_item_with(router):
    tasks = []

    def start(text):
        async def worker():
            for _ in range(100):
                item = router.queue.claim("debug-sample")
                if item is not None:
                    router.queue.complete(item.id, {"text": text})
                    return item
                await asyncio.sleep(0.02)
        tasks.append(asyncio.ensure_future(worker()))
    yield start
    for t in tasks:
        t.cancel()


async def test_not_available_in_debug(router, ctx_debug):
    assert (await router.call(ctx_debug, "ask_production", {"request": "x"})).text == "not available here"


async def test_answer_is_scrubbed(router, ctx_code, complete_next_item_with, conn):
    complete_next_item_with("crash for bob@y.com customer C-12345 token SUPERSECRETVALUE at export.py:88")
    text = (await router.call(ctx_code, "ask_production", {"request": "why"})).text
    assert "bob@y.com" not in text and "C-12345" not in text and "SUPERSECRETVALUE" not in text
    assert "export.py:88" in text
    assert conn.execute("SELECT data FROM events WHERE type = 'production_scrub'").fetchone()[0] == '{"hits": 2}'


async def test_request_goes_to_debug_sandbox_with_caller(router, ctx_code, complete_next_item_with, conn):
    complete_next_item_with("ok")
    await router.call(ctx_code, "ask_production", {"request": "what fails in export"})
    row = conn.execute("SELECT * FROM work_items WHERE kind = 'production'").fetchone()
    assert row["sandbox"] == "debug-sample" and '"caller_conversation_id": "%s"' % ctx_code.conversation_id in row["payload"]
    debug_convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (row["conversation_id"],)).fetchone()
    assert debug_convo["sandbox"] == "debug-sample" and debug_convo["thread_ts"] is None


async def test_follow_up_reuses_debug_conversation(router, ctx_code, complete_next_item_with, conn):
    complete_next_item_with("one")
    await router.call(ctx_code, "ask_production", {"request": "a"})
    complete_next_item_with("two")
    await router.call(ctx_code, "ask_production", {"request": "b"})
    convos = {r[0] for r in conn.execute("SELECT conversation_id FROM work_items WHERE kind = 'production'")}
    assert len(convos) == 1


async def test_timeout(router, ctx_code, conn):
    assert (await router.call(ctx_code, "ask_production", {"request": "x"})).text == "The production check timed out."
    assert conn.execute("SELECT status FROM work_items WHERE kind = 'production'").fetchone()[0] == "cancelled"


async def test_needs_a_project(router, conn):
    cid = add_conversation(conn, sandbox="general", project=None, channel="D-ann")
    ctx = router.context_for(add_item(router.queue, cid, "general", actor="ann"))
    assert "which project" in (await router.call(ctx, "ask_production", {"request": "x"})).text


async def test_debug_conversation_has_no_slack_thread(router, ctx_code, complete_next_item_with, conn):
    complete_next_item_with("ok")
    await router.call(ctx_code, "ask_production", {"request": "x"})
    row = conn.execute("SELECT c.channel, c.thread_ts FROM work_items w JOIN conversations c "
                       "ON c.id = w.conversation_id WHERE w.kind = 'production'").fetchone()
    assert tuple(row) == (None, None)
