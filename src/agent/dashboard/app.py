"""The dashboard's HTTP app: JSON read models, a live stream per session, the kill switch, and the built
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
from agent.dashboard.names import AccountNames
from agent.dashboard.transcript import TranscriptCache, lines_before, read_from

log = logging.getLogger(__name__)
ACTIVE = ("queued", "claimed")
RECENT = 5
MAX_ROWS = 200
PROXY_TIMEOUT_S = 8
LOOKUP_TIMEOUT_S = 2   # a session's account: worth a short wait, never a slow page
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
                       poll_s: float = 1.0, account_names: AccountNames | None = None,
                       person_name: Callable[[str], Awaitable[str | None]] | None = None,
                       llm_view: Callable[[], dict] | None = None,
                       llm_set_caps: Callable[..., object] | None = None) -> web.Application:
    queue = WorkQueue(conn)
    origins = config.dashboard.agent_origins
    history_cache: dict[int, tuple[float, dict]] = {}
    transcripts = TranscriptCache(scrub, recent=RECENT)
    pinned: dict[str, str] = {}  # session id -> account; a session never moves to another account

    def holder(code: str | None) -> str | None:
        return account_names.name(code) if account_names else None

    def transcript(item_id: str) -> Path:
        return runs_dir / f"{item_id}.jsonl"

    async def account_of(session_id: str | None) -> str | None:
        if not session_id:
            return None
        if session_id not in pinned:
            try:
                account = await asyncio.wait_for(proxy.whoami(session_id), LOOKUP_TIMEOUT_S)
            except Exception:
                return None
            if account:
                pinned[session_id] = account
        return pinned.get(session_id)

    async def fill(row: dict, *, links: bool = False, recent: bool = False) -> dict:
        """Add what needs I/O: the Slack permalink (detail page only), and the numbers a running session has so far
        (or a finished one that never reported), read incrementally from its transcript."""
        if links and permalink and row.get("_channel") and row.get("_thread_ts"):
            try:
                row["links"]["slack"] = await permalink(row["_channel"], row["_thread_ts"])
            except Exception:
                log.debug("no permalink for %s", row["id"], exc_info=True)
        last: list[dict] = []
        running = row["state"] == "running"
        if running or (row["tokens"] is None and row["state"] != "queued" and transcript(row["id"]).exists()):
            stats, last = await asyncio.to_thread(transcripts.follow, transcript(row["id"]))
            row.update(tokens=stats.tokens, turns=stats.turns)
            if running:
                row.update(live=True, account=await account_of(stats.session_id))
        row["account_name"] = holder(row["account"])
        public = _public(row)
        if recent:
            public["recent"] = last
        return public

    async def me(request: web.Request) -> web.Response:
        v = viewer(request)
        return web.json_response({"login": v.login, "name": v.name, "person": v.person.key if v.person else None,
                                  "role": v.person.role if v.person else None, "can_resume": v.can_resume,
                                  "agent": config.name})

    async def overview(request: web.Request) -> web.Response:
        running, _ = list_sessions(conn, SessionFilter(state="running", limit=MAX_ROWS))
        queued, _ = list_sessions(conn, SessionFilter(state="queued", limit=MAX_ROWS))
        return web.json_response({
            "paused": db.get_setting(conn, "paused", "0") == "1", "paused_by": paused_by(conn),
            "running": await asyncio.gather(*(fill(r, recent=True) for r in running)),
            "queued": await asyncio.gather(*(fill(r) for r in queued)),
            "counts": overview_counts(conn, clock(), config.timezone)})

    async def sessions(request: web.Request) -> web.Response:
        q = request.query
        f = SessionFilter(state=q.get("state") or None, project=q.get("project") or None,
                          source=q.get("source") or None, account=q.get("account") or None, q=q.get("q") or None,
                          limit=min(_int(request, "limit", 50, 1, 100_000), MAX_ROWS),
                          offset=_int(request, "offset", 0, 0, 10**9))
        rows, total = list_sessions(conn, f)
        return web.json_response({"rows": await asyncio.gather(*(fill(r) for r in rows)), "total": total})

    async def session(request: web.Request) -> web.Response:
        row = get_session(conn, request.match_info["id"])
        if row is None:
            return web.json_response({"error": "no such session"}, status=404)
        if request.query.get("entries") == "0":  # the page polls the header; the timeline comes from the stream
            return web.json_response(await fill(row, links=True))
        entries, offset, line = await asyncio.to_thread(read_from, transcript(row["id"]), 0, 0, scrub)
        return web.json_response({**await fill(row, links=True), "entries": entries, "offset": offset, "line": line})

    async def stream(request: web.Request) -> web.StreamResponse:
        item_id = request.match_info["id"]
        if queue.get(item_id) is None:
            return web.json_response({"error": "no such session"}, status=404)
        offset = _int(request, "offset", 0, 0, 10**12)
        line = _int(request, "line", None, 0, 10**9)
        if line is None:  # no cursor: count the lines before `offset` once, then follow with the cursor
            line = await asyncio.to_thread(lines_before, transcript(item_id), offset)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)

        async def send(kind: str, data: dict) -> None:
            await resp.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        pinged = time.monotonic()
        try:
            while True:
                entries, offset, line = await asyncio.to_thread(read_from, transcript(item_id), offset, line, scrub)
                if entries:
                    await send("entries", {"entries": entries, "offset": offset, "line": line})
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
        view = accounts_view(raw, origins)
        known = account_names.all() if account_names else {}
        for account in view["accounts"]:
            rec = known.get(account["code"]) or {}
            account.update(name=rec.get("name"), email=rec.get("email"))
        return web.json_response(view)

    async def history(request: web.Request) -> web.Response:
        hours = _int(request, "hours", 24, 1, max(HOURS))
        if hours not in HOURS:
            raise BadRequest(f"hours must be one of {', '.join(map(str, HOURS))}")
        cached = history_cache.get(hours)
        if cached and time.monotonic() - cached[0] < HISTORY_TTL_S:
            return web.json_response({**cached[1], "names": names_for(cached[1]["accounts"])})
        try:
            rows = await asyncio.wait_for(proxy.perf(hours), PROXY_TIMEOUT_S * 3)
        except Exception as e:
            return web.json_response({"error": str(e) or type(e).__name__, "hours": hours,
                                      "bucket_s": bucket_seconds(hours), "accounts": [], "origins": [],
                                      "buckets": []})
        view = await asyncio.to_thread(history_view, rows, hours, origins)
        history_cache[hours] = (time.monotonic(), view)
        return web.json_response({**view, "names": names_for(view["accounts"])})

    def names_for(codes: list[str]) -> dict[str, str]:
        return {code: name for code in codes if (name := holder(code))}

    async def kpis(request: web.Request) -> web.Response:
        days = _int(request, "days", 7, 1, 365)
        return web.json_response(kpi_view(conn, days, request.query.get("project") or None, clock(), config.timezone))

    async def event_log(request: web.Request) -> web.Response:
        page = list_events(conn, request.query.get("type") or None, _int(request, "limit", 100, 1, 500),
                           _int(request, "before", None, 0, 2**62))
        senders = sorted({r["sender"] for r in page["rows"] if r["sender"]})

        async def display(sender: str) -> str:
            if sender.startswith("tailscale:"):
                return sender.removeprefix("tailscale:")
            try:
                return (await person_name(sender) if person_name else None) or sender
            except Exception:
                return sender

        shown = dict(zip(senders, await asyncio.gather(*(display(x) for x in senders))))
        for r in page["rows"]:
            r["sender"] = shown.get(r["sender"], r["sender"])
        return web.json_response(page)

    async def llm(request: web.Request) -> web.Response:
        """What the apps under test spent on their model provider (Azure AI Foundry) through core's meter."""
        if llm_view is None:
            return web.json_response({"installed": False})
        return web.json_response({"installed": True, **llm_view()})

    async def llm_caps(request: web.Request) -> web.Response:
        if llm_view is None or llm_set_caps is None:
            return web.json_response({"error": "the model-call meter isn't installed"}, status=404)
        v = viewer(request)
        if not v.can_resume:
            return web.json_response({"error": "only an admin can change the caps"}, status=403)
        try:
            body = await request.json()
            values = {k: body.get(k) for k in ("daily_usd", "monthly_usd")}
            if not isinstance(body, dict) or all(x is None for x in values.values()):
                raise ValueError("give daily_usd, monthly_usd or both")
            for key, value in values.items():
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f"{key} must be a number of US dollars")
            llm_set_caps(v.actor, **values)
        except (ValueError, AttributeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"installed": True, **llm_view()})

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
        web.get("/api/llm", llm), web.post("/api/llm/caps", llm_caps),
        web.get("/{tail:.*}", web_app)])
    return app
