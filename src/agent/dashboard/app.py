"""The dashboard's HTTP app (spec §6): JSON read models, a live stream per session, the kill switch, and the built
web app with an index.html fallback for its client-side routes."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from agent import db, events
from agent.config import AgentConfig
from agent.core.sessions import WorkQueue
from agent.dashboard.access import access_middleware, viewer
from agent.dashboard.accounts import HOURS, accounts_view, bucket_seconds, history_view
from agent.dashboard.queries import (SessionFilter, get_session, kpi_view, list_events, list_sessions,
                                     overview_counts, paused_by)
from agent.dashboard.transcript import live_stats, read_entries

log = logging.getLogger(__name__)
ACTIVE = ("queued", "claimed")
RECENT = 5
MAX_ROWS = 200
PROXY_TIMEOUT_S = 8
HISTORY_TTL_S = 60
PING_S = 15
NOT_BUILT = "The dashboard's web app isn't built into this image: run `npm run build` in dashboard/ or rebuild core."


class BadRequest(Exception):
    pass


def _int(request: web.Request, key: str, default: int | None, lo: int, hi: int) -> int | None:
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{key} must be a whole number") from None
    if not lo <= value <= hi:
        raise BadRequest(f"{key} must be from {lo} to {hi}")
    return value


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def make_dashboard_app(*, conn: sqlite3.Connection, config: AgentConfig, proxy, runs_dir: Path,
                       static_dir: Path | None = None,
                       permalink: Callable[[str, str], Awaitable[str | None]] | None = None,
                       scrub: Callable[[str], str] = str,
                       clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                       poll_s: float = 1.0) -> web.Application:
    queue = WorkQueue(conn)
    origins = config.dashboard.agent_origins
    history_cache: dict[int, tuple[float, dict]] = {}

    def transcript(item_id: str) -> Path:
        return runs_dir / f"{item_id}.jsonl"

    async def account_of(session_id: str | None) -> str | None:
        if not session_id:
            return None
        try:
            return await asyncio.wait_for(proxy.whoami(session_id), PROXY_TIMEOUT_S)
        except Exception:
            return None

    async def fill(row: dict) -> dict:
        """Add what needs I/O: the Slack permalink and, while it runs, the session's live numbers."""
        if permalink and row.get("_channel") and row.get("_thread_ts"):
            try:
                row["links"]["slack"] = await permalink(row["_channel"], row["_thread_ts"])
            except Exception:
                log.debug("no permalink for %s", row["id"], exc_info=True)
        if row["state"] == "running":
            stats = await asyncio.to_thread(live_stats, transcript(row["id"]))
            row.update(live=True, tokens=stats.tokens, turns=stats.turns, cost_usd=stats.cost_usd,
                       account=await account_of(stats.session_id))
        elif row["tokens"] is None and row["state"] != "queued" and transcript(row["id"]).exists():
            # finished without a run row (cancelled before the runner reported): what the transcript shows
            stats = await asyncio.to_thread(live_stats, transcript(row["id"]))
            row.update(tokens=stats.tokens, turns=stats.turns, cost_usd=stats.cost_usd)
        return _public(row)

    async def me(request: web.Request) -> web.Response:
        v = viewer(request)
        return web.json_response({"login": v.login, "name": v.name, "person": v.person.key if v.person else None,
                                  "role": v.person.role if v.person else None, "can_resume": v.can_resume,
                                  "agent": config.name})

    async def overview(request: web.Request) -> web.Response:
        running, _ = list_sessions(conn, SessionFilter(state="running", limit=MAX_ROWS))
        queued, _ = list_sessions(conn, SessionFilter(state="queued", limit=MAX_ROWS))
        rows = []
        for row in running:
            entries, _ = await asyncio.to_thread(read_entries, transcript(row["id"]), 0, scrub)
            rows.append({**await fill(row), "recent": entries[-RECENT:]})
        return web.json_response({
            "paused": db.get_setting(conn, "paused", "0") == "1", "paused_by": paused_by(conn),
            "running": rows, "queued": [await fill(r) for r in queued],
            "counts": overview_counts(conn, clock(), config.timezone)})

    async def sessions(request: web.Request) -> web.Response:
        q = request.query
        f = SessionFilter(state=q.get("state") or None, project=q.get("project") or None,
                          source=q.get("source") or None, account=q.get("account") or None, q=q.get("q") or None,
                          limit=min(_int(request, "limit", 50, 1, 100_000), MAX_ROWS),
                          offset=_int(request, "offset", 0, 0, 10**9))
        rows, total = list_sessions(conn, f)
        return web.json_response({"rows": [await fill(r) for r in rows], "total": total})

    async def session(request: web.Request) -> web.Response:
        row = get_session(conn, request.match_info["id"])
        if row is None:
            return web.json_response({"error": "no such session"}, status=404)
        if request.query.get("entries") == "0":  # the page polls the header; the timeline comes from the stream
            return web.json_response(await fill(row))
        entries, offset = await asyncio.to_thread(read_entries, transcript(row["id"]), 0, scrub)
        return web.json_response({**await fill(row), "entries": entries, "offset": offset})

    async def stream(request: web.Request) -> web.StreamResponse:
        item_id = request.match_info["id"]
        if queue.get(item_id) is None:
            return web.json_response({"error": "no such session"}, status=404)
        offset = _int(request, "offset", 0, 0, 10**12)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)

        async def send(kind: str, data: dict) -> None:
            await resp.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        pinged = time.monotonic()
        try:
            while True:
                entries, offset = await asyncio.to_thread(read_entries, transcript(item_id), offset, scrub)
                if entries:
                    await send("entries", {"entries": entries, "offset": offset})
                item = queue.get(item_id)
                if item is None or item.status not in ACTIVE:
                    if not entries:  # drained: nothing more will be written for it
                        await send("end", {"state": item.status if item else "gone"})
                        break
                    continue
                if time.monotonic() - pinged >= PING_S:
                    await resp.write(b": ping\n\n")
                    pinged = time.monotonic()
                await asyncio.sleep(poll_s)
        except ConnectionResetError:  # the browser went away
            pass
        return resp

    async def accounts(request: web.Request) -> web.Response:
        try:
            raw = await asyncio.wait_for(proxy.raw_usage(), PROXY_TIMEOUT_S)
        except Exception as e:
            return web.json_response(accounts_view(None, origins, error=str(e) or type(e).__name__))
        return web.json_response(accounts_view(raw, origins))

    async def history(request: web.Request) -> web.Response:
        hours = _int(request, "hours", 24, 1, max(HOURS))
        if hours not in HOURS:
            raise BadRequest(f"hours must be one of {', '.join(map(str, HOURS))}")
        cached = history_cache.get(hours)
        if cached and time.monotonic() - cached[0] < HISTORY_TTL_S:
            return web.json_response(cached[1])
        try:
            rows = await asyncio.wait_for(proxy.perf(hours), PROXY_TIMEOUT_S * 3)
        except Exception as e:
            return web.json_response({"error": str(e) or type(e).__name__, "hours": hours,
                                      "bucket_s": bucket_seconds(hours), "accounts": [], "origins": [],
                                      "buckets": []})
        view = await asyncio.to_thread(history_view, rows, hours, origins)
        history_cache[hours] = (time.monotonic(), view)
        return web.json_response(view)

    async def kpis(request: web.Request) -> web.Response:
        days = _int(request, "days", 7, 1, 365)
        return web.json_response(kpi_view(conn, days, request.query.get("project") or None, clock(), config.timezone))

    async def event_log(request: web.Request) -> web.Response:
        return web.json_response(list_events(conn, request.query.get("type") or None,
                                             _int(request, "limit", 100, 1, 500), _int(request, "before", None, 0,
                                                                                       2**62)))

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

    root = static_dir.resolve() if static_dir is not None else None

    async def web_app(request: web.Request) -> web.StreamResponse:
        if request.path.startswith("/api/") or request.path == "/api":
            return web.json_response({"error": "no such route"}, status=404)
        if root is None or not (root / "index.html").is_file():
            return web.Response(text=NOT_BUILT)
        wanted = (root / request.match_info["tail"]).resolve()
        if wanted != root and wanted.is_relative_to(root) and wanted.is_file():
            cache = "public, max-age=31536000, immutable" if wanted.parent.name == "assets" else "no-cache"
            return web.FileResponse(wanted, headers={"Cache-Control": cache})
        return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    @web.middleware
    async def bad_requests(request: web.Request, handler):
        try:
            return await handler(request)
        except BadRequest as e:
            return web.json_response({"error": str(e)}, status=400)

    app = web.Application(middlewares=[access_middleware(config), bad_requests])
    app.add_routes([
        web.get("/api/me", me), web.get("/api/overview", overview), web.get("/api/sessions", sessions),
        web.get("/api/sessions/{id}", session), web.get("/api/sessions/{id}/stream", stream),
        web.post("/api/sessions/{id}/cancel", cancel), web.get("/api/accounts", accounts),
        web.get("/api/accounts/history", history), web.get("/api/kpis", kpis), web.get("/api/events", event_log),
        web.post("/api/stop", stop), web.post("/api/resume", resume),
        web.get("/{tail:.*}", web_app)])
    return app
