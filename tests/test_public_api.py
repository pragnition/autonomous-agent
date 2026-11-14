import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import db
from agent.core.agent_tools import AgentToolRouter
from agent.core.linear_app import LinearAppAuth, register_linear_tools
from agent.core.public_api import make_public_app
from agent.core.sessions import WorkQueue
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

SECRET = "whsec"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def calls():
    return []


@pytest.fixture
def auth(conn, tmp_path, calls):
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 86399})
        return httpx.Response(200, json={"data": {"viewer": {"id": "app-user-1"}}})

    return LinearAppAuth(conn, client_id="cid", client_secret="cs", redirect_uri="https://x/linear/oauth/callback",
                         token_path=tmp_path / "linear.env",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.fixture
async def client(auth):
    events, installed = [], []
    got = asyncio.Event()

    async def on_event(body):
        events.append(body)
        got.set()

    async def on_installed(app_user_id):
        installed.append(app_user_id)

    app = make_public_app(webhook_secret=SECRET, on_event=on_event, auth=auth, on_installed=on_installed)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.events, c.installed, c.got = events, installed, got
    yield c
    await c.close()


def signed(body: dict, secret=SECRET):
    raw = json.dumps(body).encode()
    return raw, {"Linear-Signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                 "Content-Type": "application/json"}


def event(**over):
    return {"type": "AgentSessionEvent", "action": "created", "webhookTimestamp": int(time.time() * 1000), **over}


async def test_webhook_rejects_bad_signature_and_old_timestamp(client):
    raw, headers = signed(event())
    for bad in ({**headers, "Linear-Signature": "0" * 64}, {"Content-Type": "application/json"}):
        assert (await client.post("/linear/webhook", data=raw, headers=bad)).status == 401
    raw, headers = signed(event(webhookTimestamp=int((time.time() - 90) * 1000)))
    assert (await client.post("/linear/webhook", data=raw, headers=headers)).status == 401
    raw, headers = signed(event(), secret="other")
    assert (await client.post("/linear/webhook", data=raw, headers=headers)).status == 401
    await asyncio.sleep(0.05)
    assert client.events == []


async def test_webhook_answers_then_processes(client):
    raw, headers = signed(event(agentSession={"id": "S1"}))
    assert (await client.post("/linear/webhook", data=raw, headers=headers)).status == 200
    await asyncio.wait_for(client.got.wait(), 1)
    assert client.events[0]["agentSession"]["id"] == "S1"


async def test_callback_rejects_bad_state(client, auth, calls):
    auth.new_authorize_url()
    for query in ("?code=c&state=wrong", "?code=c", "?error=access_denied"):
        assert (await client.get(f"/linear/oauth/callback{query}")).status == 400
    assert calls == [] and client.installed == []


async def test_callback_installs(client, auth):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(auth.new_authorize_url()).query)["state"][0]
    resp = await client.get(f"/linear/oauth/callback?code=c&state={state}")
    assert resp.status == 200 and "installed" in await resp.text()
    assert client.installed == ["app-user-1"] and json.loads(db.get_setting(auth.conn, "linear_app_tokens"))["access"]


async def test_other_paths_404(client):
    for path in ("/", "/agent/pull", "/health", "/linear"):
        assert (await client.get(path)).status == 404


async def test_linear_install_is_admin_only_and_dms(conn, tmp_path, auth):
    slack = FakeSlack()
    router = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             slack=slack, notifier=FakeNotifier(),
                             secret_values=[])
    register_linear_tools(router, auth)
    cid = add_conversation(conn, channel="D-ann")
    admin = router.context_for(add_item(router.queue, cid, actor="ann"))
    assert "DM" in (await router.call(admin, "linear_install", {})).text
    dm = [p for p in slack.posts if p["channel"] == "D-U1"][0]
    assert "https://linear.app/oauth/authorize?" in dm["text"]
    member = router.context_for(add_item(router.queue, cid, actor="bob"))
    assert (await router.call(member, "linear_install", {})).text == "not available here"
