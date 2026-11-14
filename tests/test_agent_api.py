import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import db
from agent.config import Limits
from agent.core.agent_api import CONN_KEY, make_agent_app
from agent.core.sessions import Gate, WorkQueue, mint_token
from agent_fixtures import add_conversation


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


class FakeGate:
    def __init__(self, ok=True):
        self.ok = ok

    async def can_start(self):
        return (True, "") if self.ok else (False, "paused")


class FakeRouter:
    def __init__(self):
        self.calls = []

    def manifest(self, ctx):
        return [{"name": "status", "description": "d", "input_schema": {}}]

    async def call(self, ctx, name, args):
        self.calls.append((ctx, name, args))
        return SimpleNamespace(text=f"ran {name}")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def queue(conn):
    return WorkQueue(conn)


@pytest.fixture
def parts(tmp_path):
    return SimpleNamespace(router=FakeRouter(), results=[], files=[], runs=tmp_path / "runs")


async def _client(conn, queue, parts, gate_ok=True, **kw):
    async def on_result(item, result):
        parts.results.append((item.id, result))

    async def on_files(item_id, files, text):
        parts.files.append((item_id, files, text))

    app = make_agent_app(conn=conn, queue=queue, gate=FakeGate(gate_ok), tools=parts.router,
                         context_for=lambda item_id: SimpleNamespace(work_item_id=item_id, task_id=None, project=None),
                         bundles=lambda: ("b1", b"bundle-bytes"),
                         on_result=on_result, on_files=on_files, pull_wait_s=0.2,
                         runs_dir=parts.runs, **kw)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def api(conn, queue, parts):
    client = await _client(conn, queue, parts)
    yield client
    await client.close()


@pytest.fixture
async def api_paused(conn, queue, parts):
    client = await _client(conn, queue, parts, gate_ok=False)
    yield client
    await client.close()


async def pull_one(api, payload=None):
    conn = api.server.app[CONN_KEY]
    WorkQueue(conn).enqueue("c1", "turn", payload or {"text": "hi"})
    tok = mint_token(conn, "runner", ttl_s=3600)
    r = await api.post("/agent/pull", headers=bearer(tok))
    assert r.status == 200
    body = await r.json()
    return body["work_item"], body["session_token"]


async def pull_status(api):
    conn = api.server.app[CONN_KEY]
    WorkQueue(conn).enqueue("c1", "turn", {})
    tok = mint_token(conn, "runner", ttl_s=3600)
    return (await api.post("/agent/pull", headers=bearer(tok))).status


async def test_pull_hands_out_item_token_and_tools(api):
    item, tok = await pull_one(api, {"text": "fix it"})
    assert item["payload"] == {"text": "fix it"} and item["kind"] == "turn" and tok
    conn = api.server.app[CONN_KEY]
    WorkQueue(conn).enqueue("c2", "turn", {})
    stok = mint_token(conn, "runner", ttl_s=3600)
    body = await (await api.post("/agent/pull", headers=bearer(stok))).json()
    assert body["tools"][0]["name"] == "status"


async def test_pull_resolves_a_turn_from_its_conversation_when_claimed(conn, queue, parts):
    """A turn's conversation-derived fields are re-read when it is claimed, not when it was queued, and the item
    keeps what was handed out. Only items of a conversation are refreshed."""
    seen = []

    def refresh(conversation_id, payload):
        seen.append(conversation_id)
        return {**payload, "resume_session_id": "S1"}

    client = await _client(conn, queue, parts, refresh=refresh)
    try:
        queue.enqueue(None, "turn", {"text": "no conversation"})
        tok = mint_token(conn, "runner", ttl_s=3600)
        alone = (await (await client.post("/agent/pull", headers=bearer(tok))).json())
        assert alone["work_item"]["payload"] == {"text": "no conversation"} and seen == []
        item, _ = await pull_one(client, {"text": "hi", "resume_session_id": None})
        assert item["payload"] == {"text": "hi", "resume_session_id": "S1"} and seen == ["c1"]
        assert queue.get(item["id"]).payload == item["payload"]
    finally:
        await client.close()


async def test_pull_returns_204_when_nothing_queued(api, conn):
    tok = mint_token(conn, "runner", ttl_s=3600)
    assert (await api.post("/agent/pull", headers=bearer(tok))).status == 204


async def test_revoked_session_token_is_401(api, conn, queue):
    item, tok = await pull_one(api)
    await api.post("/agent/result", json={"result": {"text": "done"}}, headers=bearer(tok))
    r = await api.post("/agent/tool", json={"name": "status", "args": {}}, headers=bearer(tok))
    assert r.status == 401


async def test_session_token_cannot_pull(api):
    _, tok = await pull_one(api)
    assert (await api.post("/agent/pull", headers=bearer(tok))).status == 401


async def test_missing_token_is_401(api):
    assert (await api.post("/agent/pull")).status == 401


async def test_heartbeat_reports_cancel(api, queue):
    item, tok = await pull_one(api)
    assert (await (await api.post("/agent/heartbeat", json={}, headers=bearer(tok))).json())["cancel"] is False
    queue.cancel(item["id"])
    assert (await api.post("/agent/heartbeat", json={}, headers=bearer(tok))).status == 401


async def test_pull_respects_gate(api_paused):
    assert (await pull_status(api_paused)) == 204


async def test_tool_call_resolves_context(api, parts):
    item, tok = await pull_one(api)
    r = await (await api.post("/agent/tool", json={"name": "status", "args": {"x": 1}}, headers=bearer(tok))).json()
    assert parts.router.calls[-1] == (SimpleNamespace(work_item_id=item["id"], task_id=None, project=None),
                                      "status", {"x": 1})
    assert r == {"text": "ran status"}


async def test_result_completes_and_calls_back(api, parts, queue):
    item, tok = await pull_one(api)
    await api.post("/agent/result", json={"result": {"text": "all done"}}, headers=bearer(tok))
    assert parts.results == [(item["id"], {"text": "all done"})]
    assert queue.get(item["id"]).status == "done"


async def test_events_append_to_transcript(api, parts):
    item, tok = await pull_one(api)
    body = await (await api.post("/agent/events", json={"event": {"type": "assistant", "n": 1}},
                                 headers=bearer(tok))).json()
    assert body == {"cancel": False}
    lines = (parts.runs / f"{item['id']}.jsonl").read_text().splitlines()
    stored = json.loads(lines[0])
    assert {k: v for k, v in stored.items() if k != "_ts"} == {"type": "assistant", "n": 1}


async def test_events_are_stamped(api, parts):
    item, tok = await pull_one(api)
    await api.post("/agent/events", json={"event": {"type": "AssistantMessage"}}, headers=bearer(tok))
    stored = json.loads((parts.runs / f"{item['id']}.jsonl").read_text())
    assert stored["_ts"].endswith("Z") and stored["type"] == "AssistantMessage"


async def test_files_upload_and_limit(api, parts):
    item, tok = await pull_one(api)
    form = aiohttp.FormData()
    form.add_field("text", "screenshots")
    form.add_field("file", b"png-bytes", filename="a.png")
    assert (await api.post("/agent/files", data=form, headers=bearer(tok))).status == 200
    assert parts.files == [(item["id"], [("a.png", b"png-bytes")], "screenshots")]
    many = aiohttp.FormData()
    for i in range(11):
        many.add_field("file", b"x", filename=f"{i}.png")
    assert (await api.post("/agent/files", data=many, headers=bearer(tok))).status == 413


async def test_bundle_needs_runner_token(api, conn):
    tok = mint_token(conn, "runner", ttl_s=3600)
    r = await api.get("/agent/bundle", headers=bearer(tok))
    assert r.status == 200 and r.headers["X-Bundle-Version"] == "b1" and await r.read() == b"bundle-bytes"
    _, stok = await pull_one(api)
    assert (await api.get("/agent/bundle", headers=bearer(stok))).status == 401


async def test_health_is_open(api):
    assert (await api.get("/health")).status == 200


class CountingGate(FakeGate):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def can_start(self):
        self.calls.append("asked")
        return True, ""


async def test_idle_pull_never_asks_the_gate(conn, queue, parts):
    """pull() asks queue.has_claimable() first — a plain read, no transaction — and only calls the Gate (and
    through it the account proxy) when something is claimable. An idle queue must not pay for an HTTP round trip
    to the proxy on every 0.5 s poll."""
    gate = CountingGate()

    async def noop(*a):
        pass

    app = make_agent_app(conn=conn, queue=queue, gate=gate, tools=parts.router,
                         context_for=lambda i: SimpleNamespace(task_id=None, project=None),
                         bundles=lambda: ("v", b""), on_result=noop, on_files=noop, pull_wait_s=0.2)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        tok = mint_token(conn, "runner", ttl_s=60)
        assert (await client.post("/agent/pull", headers=bearer(tok))).status == 204
        assert gate.calls == []
        queue.enqueue("c1", "turn", {})
        assert (await client.post("/agent/pull", headers=bearer(tok))).status == 200
        assert gate.calls == ["asked"]
    finally:
        await client.close()


async def test_result_survives_a_failing_callback(conn, queue, parts):
    async def boom(item, result):
        raise RuntimeError("slack down")

    async def noop(*a):
        pass

    app = make_agent_app(conn=conn, queue=queue, gate=FakeGate(), tools=parts.router,
                         context_for=lambda i: SimpleNamespace(task_id=None, project=None),
                         bundles=lambda: ("v", b""), on_result=boom, on_files=noop, pull_wait_s=0.2)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        queue.enqueue("c1", "turn", {})
        tok = mint_token(conn, "runner", ttl_s=60)
        session = (await (await client.post("/agent/pull", headers=bearer(tok))).json())
        r = await client.post("/agent/result", json={"result": {"text": "x"}}, headers=bearer(session["session_token"]))
        assert r.status == 200
    finally:
        await client.close()


async def test_pull_records_session_started(conn):
    queue = WorkQueue(conn)

    async def noop(*a):
        return None

    app = make_agent_app(conn=conn, queue=queue, gate=FakeGate(), tools=FakeRouter(),
                         context_for=lambda i: SimpleNamespace(task_id="T-1", project="sample"),
                         bundles=lambda: ("v", b""), on_result=noop, on_files=noop, pull_wait_s=0.1)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        turn = queue.enqueue("c1", "turn", {})
        tok = mint_token(conn, "runner", ttl_s=60)
        assert (await client.post("/agent/pull", headers=bearer(tok))).status == 200
        rows = conn.execute("SELECT task_id, data FROM events WHERE type = 'session_started'").fetchall()
        assert len(rows) == 1 and rows[0]["task_id"] == "T-1"
        data = json.loads(rows[0]["data"])
        assert data["work_item"] == turn and data["queued_at"] and data["project"] == "sample"
        assert set(data) == {"work_item", "queued_at", "project"}
    finally:
        await client.close()


class AlwaysCapacity:
    async def usage(self):
        return SimpleNamespace(has_capacity=lambda: True)


def cfg(sessions):
    return SimpleNamespace(limits=Limits(max_concurrent_sessions=sessions))


async def test_ten_conversations_run_at_once_eleventh_waits(conn, parts):
    """One queue is not one at a time: with room for 10 turns and 11 separate conversations queued (3 of them
    bound to the 'sample' project, the rest to none), 10 pulls succeed and the 11th finds nothing to claim —
    through the real make_agent_app pull path and the real Gate, not a stand-in for either."""
    queue = WorkQueue(conn)
    gate = Gate(conn, cfg(10), AlwaysCapacity())

    async def noop(*a):
        return None

    app = make_agent_app(conn=conn, queue=queue, gate=gate, tools=parts.router,
                         context_for=lambda i: SimpleNamespace(task_id=None, project=None),
                         bundles=lambda: ("v", b""), on_result=noop, on_files=noop, pull_wait_s=0.2)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for i in range(11):
            cid = add_conversation(conn, project="sample" if i < 3 else None, conversation_id=f"c{i}")
            queue.enqueue(cid, "turn", {"text": f"t{i}"})
        tok = mint_token(conn, "runner", ttl_s=3600)
        statuses = [(await client.post("/agent/pull", headers=bearer(tok))).status for _ in range(11)]
        assert statuses.count(200) == 10 and statuses.count(204) == 1
    finally:
        await client.close()


async def test_second_turn_of_running_conversation_waits(conn, parts):
    """Only turns of the same conversation wait for each other: with plenty of gate capacity, a conversation's
    second queued turn still isn't handed out while its first is claimed."""
    queue = WorkQueue(conn)
    gate = Gate(conn, cfg(10), AlwaysCapacity())

    async def noop(*a):
        return None

    app = make_agent_app(conn=conn, queue=queue, gate=gate, tools=parts.router,
                         context_for=lambda i: SimpleNamespace(task_id=None, project=None),
                         bundles=lambda: ("v", b""), on_result=noop, on_files=noop, pull_wait_s=0.1)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        queue.enqueue("c1", "turn", {"text": "first"})
        queue.enqueue("c1", "turn", {"text": "second"})
        tok = mint_token(conn, "runner", ttl_s=3600)
        first = await client.post("/agent/pull", headers=bearer(tok))
        assert first.status == 200
        second = await client.post("/agent/pull", headers=bearer(tok))
        assert second.status == 204
    finally:
        await client.close()


async def test_pull_reaps_an_expired_claim_blocking_its_conversation(conn, parts):
    """A dead runner's stale claim must not leave its conversation invisible to pull() forever: once its lease
    is in the past, has_claimable() surfaces the conversation again, so claim() gets the chance to reap it (or
    the follow-up queued behind it) and hand something out — the agent container restarting mid-session, and
    the person replying "hello?", must not stall there until unrelated work arrives."""
    queue = WorkQueue(conn)
    gate = Gate(conn, cfg(10), AlwaysCapacity())

    async def noop(*a):
        return None

    app = make_agent_app(conn=conn, queue=queue, gate=gate, tools=parts.router,
                         context_for=lambda i: SimpleNamespace(task_id=None, project=None),
                         bundles=lambda: ("v", b""), on_result=noop, on_files=noop, pull_wait_s=0.2)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        stale = queue.enqueue("c1", "turn", {"text": "first"})
        queue.claim()    # a runner claimed it, then died before finishing or heartbeating
        conn.execute("UPDATE work_items SET lease_until = '2000-01-01T00:00:00Z' WHERE id = ?", (stale,))
        followup = queue.enqueue("c1", "turn", {"text": "hello?"})

        tok = mint_token(conn, "runner", ttl_s=3600)
        r = await client.post("/agent/pull", headers=bearer(tok))
        assert r.status == 200
        item = (await r.json())["work_item"]
        assert item["id"] in (stale, followup) and item["conversation_id"] == "c1"
    finally:
        await client.close()
