"""Long projects: each one builds in a single conversation, which core tracks by name so every session can see it
and pass it changes with tell_project. Core holds only name, repo and which conversation is building it; the
build itself (branches, milestones, memory) is the long-project skill's work, not core's.
"""

from __future__ import annotations

import json
import re
import sqlite3

from agent import db
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def start(conn: sqlite3.Connection, *, name: str, repo: str, conversation_id: str, started_by: str | None,
          thread_link: str | None) -> None:
    """Register the project, or, if it already exists (new or finished), take it over: its build is now this
    conversation, and it's active again."""
    now = db.utcnow()
    conn.execute(
        "INSERT INTO projects(name, repo, conversation_id, thread_link, started_by, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET repo = excluded.repo, conversation_id = excluded.conversation_id, "
        "thread_link = excluded.thread_link, started_by = excluded.started_by, status = 'active', "
        "updated_at = excluded.updated_at", (name, repo, conversation_id, thread_link, started_by, now, now))


def get(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects WHERE status = 'active' ORDER BY name").fetchall()


def finish(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("UPDATE projects SET status = 'done', updated_at = ? WHERE name = ?", (db.utcnow(), name))


def build_state(conn: sqlite3.Connection, conversation_id: str) -> str:
    """The conversation's build state: "running" when it has a claimed turn, "queued" when it has a queued one,
    else "idle"."""
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM work_items WHERE conversation_id = ? AND kind = 'turn'", (conversation_id,))}
    if "claimed" in statuses:
        return "running"
    if "queued" in statuses:
        return "queued"
    return "idle"


def _asked_payload(conn: sqlite3.Connection, ctx: ToolContext) -> dict:
    row = conn.execute("SELECT payload FROM work_items WHERE id = ?", (ctx.work_item_id,)).fetchone()
    return json.loads(row["payload"]) if row else {}


def register_project_tools(router: AgentToolRouter) -> None:
    conn, queue = router.conn, router.queue

    async def start_project(ctx: ToolContext, args: dict) -> ToolReply:
        name, repo = str(args.get("name") or ""), str(args.get("repo") or "")
        if not NAME_RE.match(name):
            return ToolReply("name must be lowercase letters, digits and hyphens, 2 to 41 characters, starting "
                             "with a letter or digit")
        if ctx.conversation_id is None:
            return ToolReply("this conversation can't run a project")
        existing = get(conn, name)
        if existing is not None and existing["status"] == "active" \
                and existing["conversation_id"] != ctx.conversation_id:
            return ToolReply(f"{name} is already being built in {existing['thread_link']}")
        thread_link = None
        if ctx.channel:
            try:
                thread_link = (await router.slack.chat_getPermalink(
                    channel=ctx.channel, message_ts=ctx.thread_ts))["permalink"]
            except Exception:
                thread_link = None
        start(conn, name=name, repo=repo, conversation_id=ctx.conversation_id, started_by=ctx.actor,
             thread_link=thread_link)
        return ToolReply(f"{name} registered; every session now sees it.")

    async def finish_project(ctx: ToolContext, args: dict) -> ToolReply:
        name = str(args.get("name") or "")
        project = get(conn, name)
        if project is None or project["conversation_id"] != ctx.conversation_id:
            return ToolReply(f"only {name}'s build can finish it")
        finish(conn, name)
        return ToolReply(f"{name} marked done.")

    async def tell_project(ctx: ToolContext, args: dict) -> ToolReply:
        name, message = str(args.get("name") or ""), str(args.get("message") or "")
        project = get(conn, name)
        if project is None:
            names = ", ".join(p["name"] for p in active(conn)) or "none"
            return ToolReply(f"no project named {name}; active: {names}")
        if project["conversation_id"] == ctx.conversation_id:
            return ToolReply(f"this is {name}'s build; act on it here.")
        state = build_state(conn, project["conversation_id"])
        if project["status"] != "active":  # a question or a change restarts a finished project
            start(conn, name=name, repo=project["repo"], conversation_id=project["conversation_id"],
                 started_by=project["started_by"], thread_link=project["thread_link"])
        asked = _asked_payload(conn, ctx)
        payload = router.turn_payload(project["conversation_id"], message, ctx.actor, False)
        payload.pop("message_ts", None)  # no reactions: this turn didn't start from a Slack message
        payload.update(origin=asked.get("origin"), source=asked.get("source") or "slack",
                       requester=asked.get("requester") or ctx.actor, follows=ctx.work_item_id)
        queue.enqueue(project["conversation_id"], "turn", payload)
        step = " It's running, so it reads this at its next step." if state == "running" else \
              " It was idle and starts on this now."
        thread = project["thread_link"] or "(none)"
        return ToolReply(f"Passed to {name}'s build.{step} Thread: {thread}")

    reg, s = router.register, schema
    reg(ToolSpec("start_project", "Register this conversation as the build of a long project, so every session "
                                  "sees it and can pass it changes.", s({"name": "string", "repo": "string"}),
                "act"), start_project)
    reg(ToolSpec("finish_project", "Mark a long project done.", s({"name": "string"}), "act"), finish_project)
    reg(ToolSpec("tell_project", "Pass a change or a question to a project's build. It arrives as a message from "
                                 "the person you're talking to.", s({"name": "string", "message": "string"}),
                "act"), tell_project)
