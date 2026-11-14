"""core's public listener: the only thing Tailscale Funnel exposes, with exactly two routes.

`POST /linear/webhook` accepts a request only when its `Linear-Signature` is the HMAC-SHA256 of the raw body with
the webhook secret and its `webhookTimestamp` is within a minute, answers at once (Linear waits 5 s at most) and
handles the event in the background. `GET /linear/oauth/callback` finishes the app install. Everything else is 404.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable

from aiohttp import web

from agent.core.linear_app import LinearAppAuth

log = logging.getLogger(__name__)
MAX_SKEW_MS = 60_000

PAGE = "<!doctype html><title>{title}</title><p>{text}</p>"


def _page(status: int, title: str, text: str) -> web.Response:
    return web.Response(status=status, text=PAGE.format(title=title, text=text), content_type="text/html")


def make_public_app(*, webhook_secret: str, on_event: Callable[[dict], Awaitable[None]], auth: LinearAppAuth,
                    on_installed: Callable[[str], Awaitable[None]],
                    clock: Callable[[], float] = time.time) -> web.Application:
    pending: set[asyncio.Task] = set()

    async def webhook(request: web.Request) -> web.Response:
        raw = await request.read()
        expected = hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not webhook_secret or not hmac.compare_digest(request.headers.get("Linear-Signature", ""), expected):
            return web.json_response({"error": "bad signature"}, status=401)
        try:
            body = json.loads(raw)
            fresh = abs(clock() * 1000 - float(body["webhookTimestamp"])) <= MAX_SKEW_MS
        except (ValueError, KeyError, TypeError):
            fresh = False
        if not fresh:
            return web.json_response({"error": "stale or malformed"}, status=401)

        async def handle() -> None:
            try:
                await on_event(body)
            except Exception:
                log.exception("handling a Linear %s/%s event failed", body.get("type"), body.get("action"))

        task = asyncio.create_task(handle())
        pending.add(task)
        task.add_done_callback(pending.discard)
        return web.json_response({"ok": True})

    async def callback(request: web.Request) -> web.Response:
        q = request.query
        if q.get("error") or not q.get("code") or not auth.check_state(q.get("state")):
            return _page(400, "Not installed", "This install link is invalid or expired. Ask the agent for a new one.")
        try:
            app_user_id = await auth.exchange(q["code"])
        except Exception:
            log.exception("the Linear OAuth exchange failed")
            return _page(502, "Not installed", "Linear didn't accept the install. Ask the agent for a new link.")
        await on_installed(app_user_id)
        return _page(200, "Installed", "The agent is installed in Linear. You can close this tab.")

    app = web.Application()
    app.router.add_post("/linear/webhook", webhook)
    app.router.add_get("/linear/oauth/callback", callback)
    return app
