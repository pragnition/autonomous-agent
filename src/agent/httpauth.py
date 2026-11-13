"""Shared-token auth for the stack's internal HTTP APIs (compose network only)."""

from __future__ import annotations

import hmac

from aiohttp import web

OPEN_PATHS = {"/health"}


def bearer_auth(token: str):
    expected = f"Bearer {token}"

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path not in OPEN_PATHS and not hmac.compare_digest(
                request.headers.get("Authorization", ""), expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return middleware


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})
