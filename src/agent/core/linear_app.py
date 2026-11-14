"""The agent's Linear app: installed with actor=app, so people can delegate issues to it and its own
Linear actions show as the agent.

Install: an admin gets the authorize URL (single-use `state`, 15 minutes); Linear redirects to the public callback
with a code, which core exchanges here. Access tokens last a day and each refresh rotates the refresh token, so
core refreshes ahead of time and keeps the current access token in a file the agent container reads for its
Linear MCP server. No person ever handles the token.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx

from agent import db

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"
SCOPES = "read,write,app:assignable,app:mentionable"
STATE_TTL_S = 15 * 60
REFRESH_BEFORE_S = 3600
TOKENS_KEY, STATE_KEY = "linear_app_tokens", "linear_oauth_state"


class LinearAppAuth:
    def __init__(self, conn: sqlite3.Connection, *, client_id: str, client_secret: str, redirect_uri: str,
                 token_path: Path, owner_uid: int | None = None, http: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time):
        self.conn, self.client_id, self.client_secret, self.redirect_uri = conn, client_id, client_secret, redirect_uri
        self.token_path, self.owner_uid, self.clock = Path(token_path), owner_uid, clock
        self.http = http or httpx.AsyncClient(timeout=30)

    # install

    def new_authorize_url(self) -> str:
        state = secrets.token_urlsafe(24)
        db.set_setting(self.conn, STATE_KEY, json.dumps({"state": state, "expires_at": self.clock() + STATE_TTL_S}))
        return f"{AUTHORIZE_URL}?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": SCOPES, "actor": "app", "state": state, "prompt": "consent"})

    def check_state(self, state: str | None) -> bool:
        saved = json.loads(db.get_setting(self.conn, STATE_KEY) or "{}")
        ok = bool(state) and secrets.compare_digest(str(saved.get("state", "")), state) \
            and self.clock() < float(saved.get("expires_at", 0))
        if ok:
            db.set_setting(self.conn, STATE_KEY, "{}")  # single use
        return ok

    async def exchange(self, code: str) -> str:
        tokens = await self._token_request({"code": code, "redirect_uri": self.redirect_uri,
                                            "client_id": self.client_id, "client_secret": self.client_secret,
                                            "grant_type": "authorization_code"})
        resp = await self.http.post(GRAPHQL_URL, json={"query": "query Viewer { viewer { id } }"},
                                    headers={"Authorization": f"Bearer {tokens['access']}"})
        resp.raise_for_status()
        tokens["app_user_id"] = resp.json()["data"]["viewer"]["id"]
        self._save(tokens)
        return tokens["app_user_id"]

    # tokens

    @property
    def _stored(self) -> dict:
        return json.loads(db.get_setting(self.conn, TOKENS_KEY) or "{}")

    async def token(self) -> str | None:
        stored = self._stored
        if not stored.get("access"):
            return None
        if float(stored["expires_at"]) - self.clock() < REFRESH_BEFORE_S:
            fresh = await self._token_request({"grant_type": "refresh_token", "refresh_token": stored["refresh"],
                                               "client_id": self.client_id, "client_secret": self.client_secret})
            stored = {**stored, **fresh}
            self._save(stored)
        return stored["access"]

    async def _token_request(self, form: dict) -> dict:
        resp = await self.http.post(TOKEN_URL, data=form)
        resp.raise_for_status()
        body = resp.json()
        return {"access": body["access_token"], "refresh": body.get("refresh_token", ""),
                "expires_at": self.clock() + float(body.get("expires_in", 86399))}

    def _save(self, tokens: dict) -> None:
        db.set_setting(self.conn, TOKENS_KEY, json.dumps(tokens))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_name(f".{self.token_path.name}.tmp")
        tmp.write_text(f"LINEAR_APP_TOKEN={tokens['access']}\n")
        os.chmod(tmp, 0o600)
        if self.owner_uid is not None:
            os.chown(tmp, self.owner_uid, -1)
        os.replace(tmp, self.token_path)


INSTALL_DM_KEY = "linear_install_dm"


def register_linear_tools(router, auth: LinearAppAuth) -> None:
    from agent.core.agent_tools import ToolContext, ToolReply, ToolSpec, schema

    async def linear_install(ctx: ToolContext, args: dict) -> ToolReply:
        person = router.config.people.get(ctx.actor or "")
        if person is None:
            return ToolReply("only a configured person can install the Linear app")
        dm = (await router.slack.conversations_open(users=person.slack))["channel"]["id"]
        db.set_setting(router.conn, INSTALL_DM_KEY, dm)
        await router.slack.chat_postMessage(
            channel=dm, text=f"Install me in Linear (you need to be a Linear admin; the link works once, for 15 "
                             f"minutes): {auth.new_authorize_url()}")
        return ToolReply(f"Sent the Linear install link to {person.key} by DM.")

    router.register(ToolSpec("linear_install", "Send yourself the link that installs the agent as a Linear app, so "
                                               "people can delegate issues to it.", schema(), "admin"),
                    linear_install)
