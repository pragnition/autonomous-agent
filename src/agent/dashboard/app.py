"""The dashboard's HTTP app (spec §6): JSON read models, the kill switch, and the built web app."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from agent import db, events
from agent.config import AgentConfig
from agent.core.sessions import WorkQueue
from agent.dashboard.access import access_middleware, viewer
from agent.dashboard.queries import get_session

ACTIVE = ("queued", "claimed")


def make_dashboard_app(*, conn: sqlite3.Connection, config: AgentConfig, proxy, runs_dir: Path,
                       static_dir: Path | None = None,
                       permalink: Callable[[str, str], Awaitable[str | None]] | None = None,
                       scrub: Callable[[str], str] = str,
                       clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> web.Application:
    queue = WorkQueue(conn)

    async def me(request: web.Request) -> web.Response:
        v = viewer(request)
        return web.json_response({"login": v.login, "name": v.name, "person": v.person.key if v.person else None,
                                  "role": v.person.role if v.person else None, "can_resume": v.can_resume})

    async def stop(request: web.Request) -> web.Response:
        db.set_setting(conn, "paused", "1")
        cancelled = queue.cancel_running()
        events.append(conn, "stop_all", task_id=None, actor=viewer(request).actor,
                      data={"cancelled": cancelled, "via": "dashboard"})
        return web.json_response({"ok": True, "cancelled": cancelled})

    async def resume(request: web.Request) -> web.Response:
        v = viewer(request)
        if not v.can_resume:
            return web.json_response({"error": "only an admin can resume"}, status=403)
        db.set_setting(conn, "paused", "0")
        events.append(conn, "resume_all", task_id=None, actor=v.actor, data={"via": "dashboard"})
        return web.json_response({"ok": True})

    async def cancel(request: web.Request) -> web.Response:
        item_id = request.match_info["id"]
        item = queue.get(item_id)
        if item is None:
            return web.json_response({"error": "no such session"}, status=404)
        if item.status not in ACTIVE:
            return web.json_response({"error": "already finished", "state": item.status}, status=409)
        queue.cancel(item_id)
        row = get_session(conn, item_id)
        events.append(conn, "session_cancelled", task_id=row["task_id"] if row else None,
                      actor=viewer(request).actor, data={"work_item": item_id, "via": "dashboard"})
        return web.json_response({"ok": True, "state": "cancelled"})

    app = web.Application(middlewares=[access_middleware(config)])
    app.add_routes([web.get("/api/me", me), web.post("/api/stop", stop), web.post("/api/resume", resume),
                    web.post("/api/sessions/{id}/cancel", cancel)])
    return app
