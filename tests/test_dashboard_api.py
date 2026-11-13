import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import db
from agent.config import load_agent_config
from agent.core.sessions import WorkQueue
from agent.dashboard.app import make_dashboard_app
from agent.dashboard.transcript import make_scrubber
from agent_fixtures import AGENT_TOML, add_conversation, add_item

ANN = {"Tailscale-User-Login": "ann@x.com", "Tailscale-User-Name": "Ann A"}
BOB = {"Tailscale-User-Login": "bob@x.com", "Tailscale-User-Name": "Bob B"}
EVE = {"Tailscale-User-Login": "eve@x.com"}
POST = {"X-Dashboard": "1"}


def config(tmp_path: Path, peers=("127.0.0.1",)):
    text = (AGENT_TOML.replace('role = "admin"', 'role = "admin"\ntailscale = "ann@x.com"')
            .replace('role = "member"', 'role = "member"\ntailscale = "bob@x.com"'))
    text += f"\n[dashboard]\ntrusted_peers = {json.dumps(list(peers))}\n"
    path = tmp_path / "agent.toml"
    path.write_text(text)
    return load_agent_config(path)


class FakeProxy:
    def __init__(self):
        self.usage, self.rows, self.down, self.pins = {}, [], False, {}

    def _check(self):
        from agent.proxy import ProxyUnavailable
        if self.down:
            raise ProxyUnavailable("proxy unreachable: down")

    async def raw_usage(self):
        self._check()
        return self.usage

    async def perf(self, hours):
        self._check()
        return self.rows

    async def whoami(self, sid):
        self._check()
        return self.pins.get(sid)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def proxy():
    return FakeProxy()


async def make_client(tmp_path, conn, proxy, peers=("127.0.0.1",), **kw):
    app = make_dashboard_app(conn=conn, config=config(tmp_path, peers), proxy=proxy, runs_dir=tmp_path / "runs",
                             **kw)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def api(tmp_path, conn, proxy):
    client = await make_client(tmp_path, conn, proxy)
    yield client
    await client.close()


def running_item(conn):
    queue = WorkQueue(conn)
    cid = add_conversation(conn, sandbox="general", project=None)
    item = add_item(queue, cid, "general", text="fix the build")
    queue.claim("general")
    return item


async def test_untrusted_peer_403(tmp_path, conn, proxy):
    client = await make_client(tmp_path, conn, proxy, peers=("172.30.0.1",))
    try:
        assert (await client.get("/api/me", headers=ANN)).status == 403
        assert (await client.post("/api/resume", headers={**ANN, **POST})).status == 403
        assert (await client.get("/")).status == 403
    finally:
        await client.close()


async def test_missing_identity_403(api):
    assert (await api.get("/api/me")).status == 403
    assert (await api.get("/api/me", headers={"Tailscale-User-Login": "  "})).status == 403


async def test_me_matches_person(api):
    body = await (await api.get("/api/me", headers=ANN)).json()
    assert body == {"login": "ann@x.com", "name": "Ann A", "person": "ann", "role": "admin", "can_resume": True,
                    "agent": "agent"}


async def test_me_unknown_login_member_view(api):
    body = await (await api.get("/api/me", headers=EVE)).json()
    assert body == {"login": "eve@x.com", "name": "eve@x.com", "person": None, "role": None, "can_resume": False,
                    "agent": "agent"}


async def test_post_without_header_403(api, conn):
    assert (await api.post("/api/stop", headers=ANN)).status == 403
    assert db.get_setting(conn, "paused", "0") == "0"


async def test_post_cross_origin_403(api, conn):
    r = await api.post("/api/stop", headers={**ANN, **POST, "Origin": "https://evil.example"})
    assert r.status == 403 and db.get_setting(conn, "paused", "0") == "0"


async def test_post_same_origin_ok(api):
    host = f"{api.host}:{api.port}"
    r = await api.post("/api/stop", headers={**ANN, **POST, "Origin": f"http://{host}"})
    assert r.status == 200


async def test_post_origin_matching_forwarded_host_ok(api):
    r = await api.post("/api/stop", headers={**ANN, **POST, "Origin": "https://node.tail.ts.net:9443",
                                             "X-Forwarded-Host": "node.tail.ts.net:9443"})
    assert r.status == 200


def last_event(conn, type):
    row = conn.execute("SELECT actor, data FROM events WHERE type = ? ORDER BY id DESC LIMIT 1", (type,)).fetchone()
    return row["actor"], json.loads(row["data"]) if row["data"] else None


async def test_stop_pauses_cancels_and_logs_actor(api, conn):
    item = running_item(conn)
    body = await (await api.post("/api/stop", headers={**BOB, **POST})).json()
    assert body == {"ok": True, "cancelled": [item]}
    assert db.get_setting(conn, "paused") == "1"
    assert WorkQueue(conn).get(item).status == "cancelled"
    actor, data = last_event(conn, "stop_all")
    assert actor == "bob" and data == {"cancelled": [item], "via": "dashboard"}


async def test_stop_actor_for_unknown_login(api, conn):
    await api.post("/api/stop", headers={**EVE, **POST})
    assert last_event(conn, "stop_all")[0] == "tailscale:eve@x.com"


async def test_resume_admin_only(api, conn):
    db.set_setting(conn, "paused", "1")
    r = await api.post("/api/resume", headers={**BOB, **POST})
    assert r.status == 403 and db.get_setting(conn, "paused") == "1"
    r = await api.post("/api/resume", headers={**EVE, **POST})
    assert r.status == 403
    r = await api.post("/api/resume", headers={**ANN, **POST})
    assert r.status == 200 and await r.json() == {"ok": True}
    assert db.get_setting(conn, "paused") == "0"
    assert last_event(conn, "resume_all") == ("ann", {"via": "dashboard"})


async def test_cancel_running_item(api, conn):
    item = running_item(conn)
    r = await api.post(f"/api/sessions/{item}/cancel", headers={**EVE, **POST})
    assert r.status == 200 and await r.json() == {"ok": True, "state": "cancelled"}
    assert WorkQueue(conn).get(item).status == "cancelled"
    actor, data = last_event(conn, "session_cancelled")
    assert actor == "tailscale:eve@x.com" and data == {"work_item": item, "via": "dashboard"}


async def test_cancel_terminal_409(api, conn):
    item = running_item(conn)
    WorkQueue(conn).complete(item, {})
    r = await api.post(f"/api/sessions/{item}/cancel", headers={**ANN, **POST})
    assert r.status == 409 and (await r.json())["state"] == "done"


async def test_cancel_unknown_404(api):
    assert (await api.post("/api/sessions/W-nope/cancel", headers={**ANN, **POST})).status == 404


# read routes, the live stream and static files

INIT = {"type": "SystemMessage", "subtype": "init", "data": {"session_id": "sid-1"}}
USAGE = {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}


def say(text, message_id="m1"):
    return {"type": "AssistantMessage", "content": [{"text": text}], "message_id": message_id, "usage": USAGE}


def write_transcript(tmp_path, item, records):
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    with (runs / f"{item}.jsonl").open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


async def test_overview_running_has_recent_and_live_tokens(tmp_path, api, conn, proxy):
    item = running_item(conn)
    queued = add_item(WorkQueue(conn), add_conversation(conn, sandbox="general", project=None), "general")
    write_transcript(tmp_path, item, [INIT] + [say(f"step {i}", f"m{i}") for i in range(7)])
    proxy.pins["sid-1"] = "badger"
    body = await (await api.get("/api/overview", headers=ANN)).json()
    assert body["paused"] is False and body["paused_by"] is None
    [run] = body["running"]
    assert run["id"] == item and run["live"] is True and run["account"] == "badger"
    assert run["tokens"] == {"in": 21, "out": 28, "cache_read": 35, "cache_write": 42} and run["turns"] == 7
    assert [e["text"] for e in run["recent"]] == ["step 2", "step 3", "step 4", "step 5", "step 6"]
    assert [q["id"] for q in body["queued"]] == [queued]
    assert body["counts"]["running"] == 1 and body["counts"]["queued"] == 1
    assert not any(k.startswith("_") for k in run)


async def test_overview_survives_proxy_down(tmp_path, api, conn, proxy):
    item = running_item(conn)
    write_transcript(tmp_path, item, [INIT])
    proxy.down = True
    r = await api.get("/api/overview", headers=ANN)
    assert r.status == 200 and (await r.json())["running"][0]["account"] is None


async def test_sessions_route_filters_and_limit_cap(api, conn):
    queue = WorkQueue(conn)
    cid = add_conversation(conn, sandbox="general", project=None)
    for i in range(3):
        add_item(queue, cid, "general", text=f"task {i}")
    body = await (await api.get("/api/sessions?limit=1000&q=task", headers=ANN)).json()
    assert body["total"] == 3 and len(body["rows"]) == 3
    body = await (await api.get("/api/sessions?limit=2&offset=2", headers=ANN)).json()
    assert len(body["rows"]) == 1
    assert (await api.get("/api/sessions?limit=x", headers=ANN)).status == 400


async def test_session_detail_entries_and_offset(tmp_path, api, conn):
    item = running_item(conn)
    write_transcript(tmp_path, item, [INIT, say("hello")])
    body = await (await api.get(f"/api/sessions/{item}", headers=ANN)).json()
    assert body["id"] == item and [e["text"] for e in body["entries"]] == ["hello"]
    assert body["offset"] == (tmp_path / "runs" / f"{item}.jsonl").stat().st_size


async def test_session_detail_404(api):
    assert (await api.get("/api/sessions/W-nope", headers=ANN)).status == 404


async def read_sse(resp, until="end", limit=10):
    events, kind = [], None
    async for raw in resp.content:
        line = raw.decode().rstrip("\n")
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            events.append((kind, json.loads(line[6:])))
            if kind == until or len(events) >= limit:
                break
    return events


async def test_stream_sends_appended_lines_then_end(tmp_path, conn, proxy):
    client = await make_client(tmp_path, conn, proxy, poll_s=0.05)
    try:
        item = running_item(conn)
        write_transcript(tmp_path, item, [INIT])
        resp = await client.get(f"/api/sessions/{item}/stream?offset=0", headers=ANN)
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        async def later():
            await asyncio.sleep(0.2)
            write_transcript(tmp_path, item, [say("live line")])
            await asyncio.sleep(0.2)
            WorkQueue(conn).complete(item, {})

        task = asyncio.create_task(later())
        events = await asyncio.wait_for(read_sse(resp), 5)
        await task
        kinds = [k for k, _ in events]
        assert kinds[-1] == "end" and "entries" in kinds
        texts = [e["text"] for k, d in events if k == "entries" for e in d["entries"]]
        assert texts == ["live line"]
        assert events[-1][1] == {"state": "done"}
    finally:
        await client.close()


async def test_stream_ends_for_cancelled_item(tmp_path, conn, proxy):
    client = await make_client(tmp_path, conn, proxy, poll_s=0.05)
    try:
        item = running_item(conn)
        WorkQueue(conn).cancel(item)
        resp = await client.get(f"/api/sessions/{item}/stream", headers=ANN)
        events = await asyncio.wait_for(read_sse(resp), 5)
        assert events == [("end", {"state": "cancelled"})]
    finally:
        await client.close()


async def test_stream_404(api):
    assert (await api.get("/api/sessions/W-nope/stream", headers=ANN)).status == 404


async def test_accounts_proxy_down_200_unavailable(api, proxy):
    proxy.down = True
    r = await api.get("/api/accounts", headers=ANN)
    body = await r.json()
    assert r.status == 200 and body["available"] is False and "unreachable" in body["error"]
    r = await api.get("/api/accounts/history?hours=24", headers=ANN)
    body = await r.json()
    assert r.status == 200 and body["buckets"] == [] and body["error"]


async def test_accounts_route(api, proxy):
    proxy.usage = {"badger": {"status": "allowed", "rank": 1, "5h": [0.1, 5], "7d": [0.2, 6]}, "_all": {}}
    body = await (await api.get("/api/accounts", headers=ANN)).json()
    assert body["available"] is True and body["accounts"][0]["code"] == "badger"


async def test_history_rejects_bad_hours(api):
    assert (await api.get("/api/accounts/history?hours=5", headers=ANN)).status == 400
    assert (await api.get("/api/accounts/history?hours=x", headers=ANN)).status == 400


async def test_history_is_cached(api, proxy):
    calls = []
    original = proxy.perf

    async def counted(hours):
        calls.append(hours)
        return await original(hours)

    proxy.perf = counted
    for _ in range(2):
        assert (await api.get("/api/accounts/history?hours=6", headers=ANN)).status == 200
    assert calls == [6]


async def test_kpis_route(api):
    body = await (await api.get("/api/kpis?days=7", headers=ANN)).json()
    assert len(body["daily"]) == 7 and "merge_rate" in body["kpis"]
    assert (await api.get("/api/kpis?days=0", headers=ANN)).status == 400


async def test_events_route(api, conn):
    from agent import events as event_log
    event_log.append(conn, "stop_all", task_id=None, actor="ann", data={"x": 1})
    body = await (await api.get("/api/events?type=stop_all", headers=ANN)).json()
    assert body["rows"][0]["actor"] == "ann" and "stop_all" in body["types"]
    assert (await api.get("/api/events?before=x", headers=ANN)).status == 400


async def test_unknown_api_route_404_json(api):
    r = await api.get("/api/nope", headers=ANN)
    assert r.status == 404 and "error" in await r.json()


async def test_static_fallback_serves_index(tmp_path, conn, proxy):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dash</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    client = await make_client(tmp_path, conn, proxy, static_dir=dist)
    try:
        assert "dash" in await (await client.get("/")).text()
        assert "dash" in await (await client.get("/sessions/W-1")).text()
        assert "console.log" in await (await client.get("/assets/app.js")).text()
        assert "nope" not in await (await client.get("/../secret.txt")).text()
    finally:
        await client.close()


async def test_no_static_dir_says_not_built(api):
    r = await api.get("/")
    assert r.status == 200 and "isn't built" in await r.text()


async def test_payload_secrets_never_in_any_response(tmp_path, conn, proxy):
    client = await make_client(tmp_path, conn, proxy, scrub=make_scrubber(["s3cr3t-value"]))
    try:
        queue = WorkQueue(conn)
        cid = add_conversation(conn, sandbox="code-sample")
        item = add_item(queue, cid, "code-sample", text="go", test_secrets={"K": "s3cr3t-value"})
        queue.claim("code-sample")
        write_transcript(tmp_path, item, [INIT, say("the key is s3cr3t-value")])
        bodies = [await (await client.get(path, headers=ANN)).text()
                  for path in ("/api/overview", "/api/sessions", f"/api/sessions/{item}")]
        assert all("s3cr3t-value" not in b for b in bodies)
        assert "[secret]" in bodies[2]
    finally:
        await client.close()
