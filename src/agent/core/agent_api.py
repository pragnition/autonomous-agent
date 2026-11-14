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
from agent.core.sessions import TokenInfo, WorkItem, WorkQueue, mint_token, resolve_token

log = logging.getLogger(__name__)

MAX_FILES = 10
CONN_KEY = web.AppKey("conn", sqlite3.Connection)
MAX_FILE_BYTES = 25 * 1024 * 1024


async def health(request: web.Request) -> web.Response:
    """The supervisor's health check."""
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
                   pull_wait_s: float = 25.0, session_ttl_s: int = 100 * 60,
                   runs_dir: Path | None = None) -> web.Application:
    """`refresh(conversation_id, payload)` re-reads a turn's conversation when the turn is claimed (see
    Payloads.refresh_turn); what it returns is handed out and kept as the item's payload."""
    def resolved(request: web.Request, kind: str) -> TokenInfo:
        tok = _token(request)
        info = resolve_token(conn, tok) if tok else None
        if info is None or info.kind != kind:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
        return info

    async def pull(request: web.Request) -> web.Response:
        resolved(request, "runner")
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
        return web.json_response({"cancel": not queue.heartbeat(info.work_item_id)})

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

    async def result(request: web.Request) -> web.Response:
        info = resolved(request, "session")
        outcome = (await request.json()).get("result") or {}
        queue.complete(info.work_item_id, outcome)
        try:
            await on_result(queue.get(info.work_item_id), outcome)
        except Exception:  # the item is done either way; a Slack hiccup must not turn into a retry loop
            log.exception("handling the result of %s failed", info.work_item_id)
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
                    web.get("/agent/bundle", bundle), web.get("/health", health)])
    return app
