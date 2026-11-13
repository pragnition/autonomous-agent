"""ask_production (spec v2 G9, §7): production data lands only in the project's locked-down debug sandbox.

A coding (or general) session asks a question; core runs it as a session in `debug-<project>`, which has the
Azure and tracing tools, waits for the answer, and strips customer data before handing it back. Errors, stack
traces, timings, request paths and counts survive; emails, phone numbers, IPs, tokens and the pack's own
patterns don't.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable

from agent import db, events
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema
from agent.memory.scrub import scrub

MARK = "[customer-data]"
_CUSTOMER = [re.compile(p) for p in (
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",                       # email
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",                                 # bearer token
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+",            # JWT
    r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b",                        # IPv6 (full form)
    r"\b(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4})?\b",                 # IPv6 (compressed)
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                                           # IPv4
    r"\+\d[\d \-]{6,}\d",                                                      # phone with a country code
    r"(?<![\w.:/-])\d{8,15}(?![\w.:/-])",                                      # long bare digit run (phone, ID)
)]


def scrub_customer(text: str, patterns: list[str]) -> tuple[str, int]:
    hits = 0
    for pattern in [*_CUSTOMER, *(re.compile(p) for p in patterns)]:
        text, n = pattern.subn(MARK, text)
        hits += n
    return text, hits


ProductionPayload = Callable[[str, str, str | None, bool, str | None], dict]


def _default_payload(project: str, request: str, actor: str | None, unattended: bool, caller: str | None) -> dict:
    return {"text": request, "actor": actor, "unattended": unattended, "caller_conversation_id": caller,
            "project": project}


def register_production_tools(router: AgentToolRouter, *, production_payload: ProductionPayload | None = None,
                              timeout_s: float = 15 * 60, poll_s: float = 1.0) -> None:
    make_payload = production_payload or _default_payload
    conn, queue = router.conn, router.queue

    def debug_conversation(ctx: ToolContext, project: str) -> str:
        """One debug conversation per asking conversation, so follow-up questions resume the same session."""
        key = f"debug_conversation:{ctx.conversation_id}:{project}"
        existing = db.get_setting(conn, key)
        if existing:
            return existing
        convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
        conn.execute("INSERT INTO conversations(id, channel, thread_ts, sandbox, project, task_id, is_primary, "
                     "created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                     (convo_id, None, None, f"debug-{project}", project, ctx.task_id, now, now))
        db.set_setting(conn, key, convo_id)
        return convo_id

    async def ask_production(ctx: ToolContext, args: dict) -> ToolReply:
        project = ctx.project or args.get("project")
        if project not in router.packs:
            return ToolReply(f"which project? Pass project (one of {', '.join(sorted(router.packs))}).")
        convo = debug_conversation(ctx, project)
        item_id = queue.enqueue(convo, f"debug-{project}", "production",
                                make_payload(project, args.get("request", ""), ctx.actor, ctx.unattended,
                                             ctx.conversation_id))
        deadline = time.monotonic() + timeout_s
        while True:
            item = queue.get(item_id)
            if item.status in ("done", "failed", "cancelled"):
                break
            if time.monotonic() >= deadline:
                queue.cancel(item_id)
                return ToolReply("The production check timed out.")
            await asyncio.sleep(poll_s)
        if item.status != "done":
            return ToolReply(f"The production check didn't finish ({item.status}).")
        text, hits = scrub_customer(scrub((item.result or {}).get("text", ""), router.secret_values),
                                    router.packs[project].project.scrub_patterns)
        events.append(conn, "production_scrub", task_id=ctx.task_id, actor=ctx.actor, data={"hits": hits})
        return ToolReply(text or "The production check returned nothing.")

    router.register(ToolSpec(
        "ask_production", "Ask about production (logs, metrics, traces, recent deploys) or request a routine "
                          "production action. A separate locked-down session investigates and answers; customer "
                          "data is removed from the answer. Ask follow-ups as often as you need.",
        schema({"request": "string"}, {"project": "string"}), "act", ("code", "general")), ask_production)
