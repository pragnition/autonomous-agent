"""The stack's shared health check endpoint."""

from __future__ import annotations

from aiohttp import web


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})
