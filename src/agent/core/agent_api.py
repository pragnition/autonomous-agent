"""The API the agent container's runner talks to. The runner pulls; core never connects into the container.

Two kinds of token: the runner token (pull work, fetch bundles) and a per-item session token (everything a
running session does). Finishing, cancelling or losing an item revokes its session token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from agent import db
from agent import events as event_log
from agent.core.sessions import TokenInfo, WorkItem, WorkQueue, mint_token, renew_token, resolve_token

log = logging.getLogger(__name__)

MAX_FILES = 10
CONN_KEY = web.AppKey("conn", sqlite3.Connection)
MAX_FILE_BYTES = 25 * 1024 * 1024


RUNNER_STALE_S = 120  # the runner pulls at least every 25 s, and heartbeats each running item every 15 s


async def health(request: web.Request) -> web.Response:
    """The supervisors' health check."""
    return web.json_response({"ok": True})


def _token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


def make_agent_app(*, conn: sqlite3.Connection, queue: WorkQueue, gate, tools,
                   context_for: Callable[[str], object],
                   bundles: Callable[[], tuple[str, bytes]],
                   on_result: Callable[[WorkItem, dict], Awaitable[None]],
                   on_files: Callable[[str, list[tuple[str, bytes]], str], Awaitable[bool | None]],
                   refresh: Callable[[str, dict], dict] | None = None,
                   on_start: Callable[[WorkItem], Awaitable[None]] | None = None,
                   on_beat: Callable[[WorkItem, dict], Awaitable[dict]] | None = None,
                   pull_wait_s: float = 25.0, session_ttl_s: int = 100 * 60,
                   runs_dir: Path | None = None,
                   slack_file: Callable[[str], Awaitable[bytes]] | None = None,
                   runner_stale_s: float = RUNNER_STALE_S,
                   on_host: Callable[[dict], None] | None = None,
                   clock: Callable[[], float] = time.monotonic) -> web.Application:
    """`refresh(conversation_id, payload)` re-reads a turn's conversation when the turn is claimed (see
    Payloads.refresh_turn); what it returns is handed out and kept as the item's payload. `slack_file(id)`
    downloads a Slack file with core's bot token, for a session whose conversation was sent it.
    `/health/runner` is the runner's heartbeat for the supervisors: 503 once the runner hasn't pulled or
    heartbeated for runner_stale_s. A fresh core gives the runner that long to show up. `on_host(snapshot)` takes the
    runner's samples of what the machine is doing (agent.runner.host), for the dashboard."""
    runner_seen = [clock()]

    async def runner_health(request: web.Request) -> web.Response:
        age = clock() - runner_seen[0]
        ok = age <= runner_stale_s
        return web.json_response({"ok": ok, "seen_s_ago": round(age)}, status=200 if ok else 503)

    def resolved(request: web.Request, kind: str) -> TokenInfo:
        tok = _token(request)
        info = resolve_token(conn, tok) if tok else None
        if info is None or info.kind != kind:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
        return info

    async def pull(request: web.Request) -> web.Response:
        resolved(request, "runner")
        runner_seen[0] = clock()
        deadline = time.monotonic() + pull_wait_s
        while True:
            item = None
            # nothing claimable means nothing worth asking the Gate about
            if queue.has_claimable() and (await gate.can_start())[0]:
                item = queue.claim()
            if item is not None:
                if refresh is not None and item.conversation_id:
                    # no await since the claim: nothing can change the conversation between the two
                    item.payload = refresh(item.conversation_id, item.payload)
                    queue.set_payload(item.id, item.payload)
                session = mint_token(conn, "session", work_item_id=item.id, ttl_s=session_ttl_s)
                ctx = context_for(item.id)
                # a turn's start is where the KPIs' pickup and queue-wait times begin
                event_log.append(conn, "session_started",
                                 task_id=getattr(ctx, "task_id", None), actor=None,
                                 data={"work_item": item.id, "queued_at": item.created_at, "project": ctx.project})
                if on_start is not None:
                    try:
                        await on_start(item)
                    except Exception:  # the runner gets the item either way; a Slack hiccup must not lose it
                        log.exception("handling the start of %s failed", item.id)
                return web.json_response({
                    "work_item": {"id": item.id, "conversation_id": item.conversation_id, "kind": item.kind,
                                  "payload": item.payload, "attempts": item.attempts},
                    "session_token": session,
                    "tools": tools.manifest(ctx)})
            if time.monotonic() >= deadline:
                return web.Response(status=204)
            await asyncio.sleep(0.5)

    async def heartbeat(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        runner_seen[0] = clock()
        body = await request.json()
        alive = queue.heartbeat(info.work_item_id)
        if alive:  # a turn can outlive the token it was claimed with: an account fork restarts its timer
            renew_token(conn, _token(request), ttl_s=session_ttl_s)
        messages: list[dict] = []
        if body and on_beat is not None:  # an empty body means no beat: an older runner just checking in
            beat = {**body, "accepts_messages": bool(body.get("accepts_messages")) and alive}
            try:
                out = await on_beat(queue.get(info.work_item_id), beat)
                messages = out.get("messages", [])
            except Exception:  # the runner still needs its cancel flag; a Slack hiccup must not break that
                log.exception("handling the heartbeat of %s failed", info.work_item_id)
        return web.json_response({"cancel": not alive, "messages": messages})

    async def events(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        event = (await request.json()).get("event") or {}
        if isinstance(event, dict):
            event["_ts"] = db.utcnow()  # when it arrived, for the dashboard's timeline
        if runs_dir is not None:
            runs_dir.mkdir(parents=True, exist_ok=True)
            with (runs_dir / f"{info.work_item_id}.jsonl").open("a") as f:
                f.write(json.dumps(event) + "\n")
        return web.json_response({"cancel": not queue.heartbeat(info.work_item_id)})

    async def tool(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        body = await request.json()
        reply = await tools.call(context_for(info.work_item_id), body.get("name", ""), body.get("args") or {})
        return web.json_response({"text": reply.text})

    async def files(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        text, items = "", []
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if part.name == "text":
                text = (await part.read()).decode()
                continue
            if len(items) >= MAX_FILES:
                return web.json_response({"error": f"at most {MAX_FILES} files"}, status=413)
            data = await part.read()
            if len(data) > MAX_FILE_BYTES:
                return web.json_response({"error": f"{part.filename} is over 25 MB"}, status=413)
            items.append((part.filename or "file", bytes(data)))
        posted = await on_files(info.work_item_id, items, text)
        return web.json_response({"ok": True, "posted": posted is not False})

    def _sent_to(work_item_id: str, file_id: str) -> dict | None:
        """The file, if a message in this item's conversation carried it: a session reads only what it was sent."""
        item = queue.get(work_item_id)
        if item is None:
            return None
        rows = conn.execute("SELECT payload FROM work_items WHERE (id = ? OR conversation_id IS ?) AND payload LIKE ?",
                            (item.id, item.conversation_id or item.id, f'%"{file_id}"%')).fetchall()
        for row in rows:
            for f in json.loads(row["payload"]).get("files") or []:
                if isinstance(f, dict) and f.get("id") == file_id:
                    return f
        return None

    async def fetch_slack_file(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        file_id = request.match_info["file_id"]
        f = _sent_to(info.work_item_id, file_id) if slack_file is not None else None
        if f is None:
            return web.json_response({"error": "no such file in this conversation"}, status=404)
        if int(f.get("size") or 0) > MAX_FILE_BYTES:
            return web.json_response({"error": "over 25 MB"}, status=413)
        try:
            data = await slack_file(file_id)
        except Exception as e:
            log.warning("couldn't fetch Slack file %s", file_id, exc_info=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        if len(data) > MAX_FILE_BYTES:
            return web.json_response({"error": "over 25 MB"}, status=413)
        return web.Response(body=data, content_type="application/octet-stream")

    async def result(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        outcome = (await request.json()).get("result") or {}
        if "read" in outcome and on_beat is not None:
            try:
                await on_beat(queue.get(info.work_item_id), {"read": outcome["read"]})
            except Exception:  # the item completes either way; a Slack hiccup must not lose the result
                log.exception("marking %s's read messages failed", info.work_item_id)
        queue.complete(info.work_item_id, outcome)
        try:
            await on_result(queue.get(info.work_item_id), outcome)
        except Exception:  # the item is done either way; a Slack hiccup must not turn into a retry loop
            log.exception("handling the result of %s failed", info.work_item_id)
        return web.json_response({"ok": True})

    async def host(request: web.Request) -> web.Response:
        resolved(request, "runner")
        try:
            snap = await request.json()
        except ValueError:
            snap = None
        if not isinstance(snap, dict):
            return web.json_response({"error": "a JSON object is expected"}, status=400)
        if on_host is not None:
            on_host(snap)
        return web.json_response({"ok": True})

    async def bundle(request: web.Request) -> web.Response:
        resolved(request, "runner")
        version, data = bundles()
        return web.Response(body=data, content_type="application/gzip", headers={"X-Bundle-Version": version})

    app = web.Application(client_max_size=MAX_FILES * MAX_FILE_BYTES + 1024 * 1024)
    app[CONN_KEY] = conn
    app.add_routes([web.post("/agent/pull", pull), web.post("/agent/heartbeat", heartbeat),
                    web.post("/agent/events", events), web.post("/agent/tool", tool),
                    web.post("/agent/files", files), web.post("/agent/result", result),
                    web.get("/agent/slack_file/{file_id}", fetch_slack_file),
                    web.get("/agent/bundle", bundle), web.post("/agent/host", host), web.get("/health", health),
                    web.get("/health/runner", runner_health)])
    return app
