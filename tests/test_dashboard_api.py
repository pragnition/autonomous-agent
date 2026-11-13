import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import db
from agent.config import load_agent_config
from agent.core.sessions import WorkQueue
from agent.dashboard.app import make_dashboard_app
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
    assert body == {"login": "ann@x.com", "name": "Ann A", "person": "ann", "role": "admin", "can_resume": True}


async def test_me_unknown_login_member_view(api):
    body = await (await api.get("/api/me", headers=EVE)).json()
    assert body == {"login": "eve@x.com", "name": "eve@x.com", "person": None, "role": None, "can_resume": False}


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
