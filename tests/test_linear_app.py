import json
import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agent import db
from agent.core.linear_app import LinearAppAuth

REDIRECT = "https://jensen.example.ts.net/linear/oauth/callback"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


class Linear:
    """Fake token endpoint and GraphQL: hands out numbered tokens and records requests."""

    def __init__(self):
        self.forms, self.graphql, self.n = [], [], 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            self.n += 1
            return httpx.Response(200, json={"access_token": f"acc-{self.n}", "refresh_token": f"ref-{self.n}",
                                             "expires_in": 86399, "token_type": "Bearer"})
        self.graphql.append((request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"data": {"viewer": {"id": "app-user-1"}}})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_auth(conn, tmp_path, linear, clock):
    return LinearAppAuth(conn, client_id="cid", client_secret="csecret", redirect_uri=REDIRECT,
                         token_path=tmp_path / "runner" / "linear.env",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(linear.handler)), clock=clock)


def test_authorize_url_params(conn, tmp_path):
    auth = make_auth(conn, tmp_path, Linear(), Clock())
    url = urlparse(auth.new_authorize_url())
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://linear.app/oauth/authorize"
    assert q["client_id"] == "cid" and q["redirect_uri"] == REDIRECT and q["response_type"] == "code"
    assert q["scope"] == "read,write,app:assignable,app:mentionable" and q["actor"] == "app"
    assert q["prompt"] == "consent" and len(q["state"]) >= 20
    assert json.loads(db.get_setting(conn, "linear_oauth_state"))["state"] == q["state"]


def test_state_is_single_use_and_expires(conn, tmp_path):
    clock = Clock()
    auth = make_auth(conn, tmp_path, Linear(), clock)
    state = parse_qs(urlparse(auth.new_authorize_url()).query)["state"][0]
    assert not auth.check_state("wrong") and not auth.check_state(None)
    assert auth.check_state(state) and not auth.check_state(state)
    state = parse_qs(urlparse(auth.new_authorize_url()).query)["state"][0]
    clock.t += 16 * 60
    assert not auth.check_state(state)


async def test_exchange_stores_tokens_and_app_user(conn, tmp_path):
    linear = Linear()
    auth = make_auth(conn, tmp_path, linear, Clock())
    assert await auth.exchange("the-code") == "app-user-1"
    form = linear.forms[0]
    assert form == {"code": "the-code", "redirect_uri": REDIRECT, "client_id": "cid", "client_secret": "csecret",
                    "grant_type": "authorization_code"}
    assert linear.graphql[0][0] == "Bearer acc-1" and "viewer" in linear.graphql[0][1]["query"]
    assert json.loads(db.get_setting(conn, "linear_app_tokens"))["app_user_id"] == "app-user-1"
    env = tmp_path / "runner" / "linear.env"
    assert env.read_text() == "LINEAR_APP_TOKEN=acc-1\n" and os.stat(env).st_mode & 0o777 == 0o600


async def test_token_refreshes_and_rotates(conn, tmp_path):
    linear, clock = Linear(), Clock()
    auth = make_auth(conn, tmp_path, linear, clock)
    await auth.exchange("c")
    assert await auth.token() == "acc-1" and len(linear.forms) == 1      # plenty of time left: no call
    clock.t += 86399 - 3000                                             # under an hour left
    assert await auth.token() == "acc-2"
    assert linear.forms[1] == {"grant_type": "refresh_token", "refresh_token": "ref-1", "client_id": "cid",
                               "client_secret": "csecret"}
    stored = json.loads(db.get_setting(conn, "linear_app_tokens"))
    assert (stored["access"], stored["refresh"]) == ("acc-2", "ref-2")
    assert (tmp_path / "runner" / "linear.env").read_text() == "LINEAR_APP_TOKEN=acc-2\n"
    again = make_auth(conn, tmp_path, linear, clock)                    # a restarted core keeps the tokens
    assert await again.token() == "acc-2"


async def test_not_installed_returns_none(conn, tmp_path):
    auth = make_auth(conn, tmp_path, Linear(), Clock())
    assert await auth.token() is None
