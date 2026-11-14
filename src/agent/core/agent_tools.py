"""core's tools for the agent: where the guarantees live.

Every call arrives with a session token; core resolves it to a ToolContext (conversation, project and the actor
whose message started the turn). Tiers come from the actor, never from the model's arguments,
and a tool is offered only by tier. Every reply is stripped of secrets.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from agent import db, events, tasks
from agent.core.conversations import create_conversation, origin_line, record_message
from agent.core.slack_markdown import post_markdown
from agent.scrub import scrub
from agent.tasks import IllegalTransition, Status

log = logging.getLogger(__name__)

TIER_ORDER = {"read": 0, "act": 1, "admin": 2}
KINDS = ("feature", "bug", "chore")
NOT_AVAILABLE = "not available here"
SLACK_READ_METHODS = ("conversations.history", "conversations.replies", "conversations.list", "conversations.info",
                      "users.info", "users.list", "pins.list", "bookmarks.list")
SLACK_READ_CHARS = 20_000


@dataclass
class ToolContext:
    work_item_id: str
    conversation_id: str | None
    project: str | None
    actor: str | None
    tier: Literal["read", "act", "admin"]
    unattended: bool
    task_id: str | None
    channel: str | None
    thread_ts: str | None


@dataclass
class ToolReply:
    text: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    tier: str


Handler = Callable[[ToolContext, dict], Awaitable[ToolReply]]


def schema(required: dict[str, str] | None = None, optional: dict[str, str] | None = None) -> dict:
    props = {k: {"type": "array", "items": {"type": "string"}} if v == "array" else {"type": v}
             for k, v in {**(required or {}), **(optional or {})}.items()}
    return {"type": "object", "properties": props, "required": list(required or {})}


def _running(project: str, minutes: int | None) -> str:
    return project if minutes is None else f"{project} {minutes}m"


class AgentToolRouter:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, queue, slack, notifier,
                 secret_values: list[str],
                 turn_payload: Callable[[str, str, str | None, bool], dict] | None = None):
        self.conn, self.config, self.packs, self.queue = conn, config, packs, queue
        self.slack, self.notifier = slack, notifier
        self.secret_values = list(secret_values)
        self.turn_payload = turn_payload or (lambda convo_id, text, actor, unattended:
                                             {"text": text, "actor": actor, "unattended": unattended})
        self._tools: dict[str, tuple[ToolSpec, Handler]] = {}
        self._register_builtins()

    # registry

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        self._tools[spec.name] = (spec, handler)

    def _allowed(self, spec: ToolSpec, ctx: ToolContext) -> bool:
        return TIER_ORDER[spec.tier] <= TIER_ORDER[ctx.tier]

    def manifest(self, ctx: ToolContext) -> list[dict]:
        return [{"name": s.name, "description": s.description, "input_schema": s.input_schema}
                for s, _ in self._tools.values() if self._allowed(s, ctx)]

    async def call(self, ctx: ToolContext, name: str, args: dict) -> ToolReply:
        entry = self._tools.get(name)
        if entry is None or not self._allowed(entry[0], ctx):
            return ToolReply(NOT_AVAILABLE)
        try:
            reply = await entry[1](ctx, args or {})
        except Exception as e:  # a tool failure is the agent's information, never a crash of core
            log.exception("tool %s failed", name)
            reply = ToolReply(f"{name} failed: {type(e).__name__}: {e}")
        return ToolReply(scrub(reply.text, self.secret_values))

    def context_for(self, work_item_id: str) -> ToolContext:
        row = self.conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        payload = json.loads(row["payload"])
        convo = (self.conn.execute("SELECT * FROM conversations WHERE id = ?", (row["conversation_id"],)).fetchone()
                 if row["conversation_id"] else None)
        actor, unattended = payload.get("actor"), bool(payload.get("unattended"))
        person = self.config.people.get(actor) if actor else None
        if person is not None:
            tier = "admin" if person.role == "admin" else "act"
        else:
            tier = "act" if unattended and actor is None else "read"
        return ToolContext(work_item_id=work_item_id, conversation_id=row["conversation_id"],
                           project=(convo["project"] if convo else None) or payload.get("project"),
                           actor=actor, tier=tier, unattended=unattended,
                           task_id=convo["task_id"] if convo else payload.get("task_id"),
                           channel=convo["channel"] if convo else None,
                           thread_ts=convo["thread_ts"] if convo else None)

    # helpers

    def _payload(self, ctx: ToolContext) -> dict:
        row = self.conn.execute("SELECT payload FROM work_items WHERE id = ?", (ctx.work_item_id,)).fetchone()
        return json.loads(row["payload"]) if row else {}

    async def _post(self, ctx: ToolContext, text: str) -> ToolReply:
        if not ctx.channel:
            return ToolReply("this conversation has no Slack thread")
        posted = await post_markdown(self.slack, channel=ctx.channel, thread_ts=ctx.thread_ts,
                                     text=scrub(text, self.secret_values))
        if ctx.conversation_id:
            for ts in posted:
                record_message(self.conn, ctx.channel, ts, ctx.conversation_id)
        return ToolReply("posted")

    def _tasks_today(self) -> int:
        today = db.utcnow()[:10]
        return self.conn.execute("SELECT COUNT(*) FROM tasks WHERE created_at >= ?", (today,)).fetchone()[0]

    def _new_task_problem(self, project: str, kind: str) -> str | None:
        if project not in self.packs:
            return f"unknown project '{project}' (known: {', '.join(sorted(self.packs)) or 'none'})"
        if kind not in KINDS:
            return f"kind must be one of {', '.join(KINDS)}"
        if self._tasks_today() >= self.config.limits.max_tasks_per_day:
            return f"daily task limit reached ({self.config.limits.max_tasks_per_day}); try again tomorrow"
        return None

    def _create_task(self, ctx: ToolContext, project: str, title: str, kind: str, description: str,
                     thread_ts: str | None):
        """thread_ts is the task's Slack thread: ctx.thread_ts, except in start_task's DM case, which gives the task
        the child's own thread before the task exists, so the status notifier never has to guess one."""
        return tasks.create_task(self.conn, project=project, title=title, description=description, kind=kind,
                                 source="linear" if ctx.unattended else "slack",
                                 source_ref=f"{ctx.channel}/{thread_ts}" if ctx.channel else None,
                                 requester=ctx.actor, owner=self.packs[project].project.owner,
                                 slack_channel=ctx.channel, slack_thread_ts=thread_ts)

    def ensure_task(self, ctx: ToolContext, title: str, kind: str = "feature"):
        """The conversation's task, created (and linked) when the agent didn't call track_task first."""
        if ctx.task_id:
            return tasks.get_task(self.conn, ctx.task_id)
        task = self._create_task(ctx, ctx.project, title or "Untitled", kind, self._payload(ctx).get("text", ""),
                                 ctx.thread_ts)
        task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        if ctx.conversation_id:
            self.conn.execute("UPDATE conversations SET task_id = ? WHERE id = ?", (task.id, ctx.conversation_id))
        ctx.task_id = task.id
        return task

    # built-in tools

    def _register_builtins(self) -> None:
        reg, s = self.register, schema
        reg(ToolSpec("post_update", "Post a short progress update in this conversation's Slack thread.",
                     s({"text": "string"}), "act"), self._post_update)
        reg(ToolSpec("ask", "Ask the person a question when the request itself is unclear. Their reply arrives as "
                            "your next message, so end your turn after asking.", s({"question": "string"}), "act"),
            self._ask)
        reg(ToolSpec("react", "Add an emoji reaction to the message you are answering.", s({"emoji": "string"}),
                     "act"), self._react)
        reg(ToolSpec("post_files", "Post files from your session folder (screenshots, videos, reports) "
                                   "to this conversation's Slack thread, with one line of text.",
                     s({"paths": "array", "text": "string"}), "act"), self._post_files)
        reg(ToolSpec("track_task", "Record that this conversation is now a piece of work (for status, Linear and "
                                   "metrics). kind: feature, bug or chore.",
                     s({"project": "string", "title": "string", "kind": "string"}), "act"), self._track_task)
        reg(ToolSpec("finish_task", "Mark this conversation's task done, with its outcome (a PR link, say).",
                     s({"outcome": "string"}), "act"), self._finish_task)
        reg(ToolSpec("start_task", "Start a separate session for a piece of work, with a self-contained brief, to "
                                   "split one request into parallel tasks.",
                     s({"project": "string", "title": "string", "kind": "string", "brief": "string"}), "act"),
            self._start_task)
        reg(ToolSpec("status", "What is running and queued, and whether the agent is paused.", s(), "read"),
            self._status)
        reg(ToolSpec("list_tasks", "List tasks, optionally by status or project.",
                     s(optional={"status": "string", "project": "string"}), "read"), self._list_tasks)
        reg(ToolSpec("get_task", "Show one task.", s({"task_id": "string"}), "read"), self._get_task)
        reg(ToolSpec("cancel_task", "Cancel a task and anything running for it.", s({"task_id": "string"}), "act"),
            self._cancel_task)
        reg(ToolSpec("stop_all", "Stop everything: pause new work and cancel what is running. Only an admin can "
                                 "resume.", s(), "act"), self._stop_all)
        reg(ToolSpec("resume_all", "Resume after stop_all.", s(), "admin"), self._resume_all)
        reg(ToolSpec("read_thread", "Read this conversation's Slack thread, or in a DM chat its messages so far "
                                    "(oldest first).",
                     s(optional={"limit": "integer"}), "read"), self._read_thread)
        reg(ToolSpec("slack_read", "Read from Slack with core's bot token: one Web API call, with params as Slack "
                                   f"documents them. Methods: {', '.join(SLACK_READ_METHODS)}. Replies over "
                                   f"{SLACK_READ_CHARS} characters are cut; page with limit and cursor.",
                     s({"method": "string"}, {"params": "object"}), "read"), self._slack_read)

    async def _post_files(self, ctx, args) -> ToolReply:
        return ToolReply("post_files is handled by the runner, which reads the files from your session folder")

    async def _post_update(self, ctx, args) -> ToolReply:
        return await self._post(ctx, args.get("text", ""))

    async def _ask(self, ctx, args) -> ToolReply:
        question = args.get("question", "").strip()
        if ctx.conversation_id:
            self.conn.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", (ctx.conversation_id,))
        task = tasks.get_task(self.conn, ctx.task_id) if ctx.task_id else None
        if task is not None and task.status == Status.working:
            task = tasks.transition(self.conn, task.id, Status.needs_input, actor=ctx.actor or "agent",
                                    data={"question": question})
        if task is not None and task.slack_channel:
            await self.notifier.ask(task, question)
        else:
            await self._post(ctx, question)
        return ToolReply("Asked. Their reply arrives as your next message; end this turn now.")

    async def _react(self, ctx, args) -> ToolReply:
        if not ctx.channel:
            return ToolReply("this conversation has no Slack thread")
        ts = self._payload(ctx).get("message_ts") or ctx.thread_ts
        await self.slack.reactions_add(channel=ctx.channel, timestamp=ts, name=args.get("emoji", "").strip(":"))
        return ToolReply("reacted")

    async def _track_task(self, ctx, args) -> ToolReply:
        if ctx.task_id:
            return ToolReply(f"Already tracking {ctx.task_id}.")
        project, kind = args.get("project", ""), args.get("kind", "")
        if problem := self._new_task_problem(project, kind):
            return ToolReply(problem)
        task = self._create_task(ctx, project, args.get("title", "").strip() or "Untitled", kind,
                                 self._payload(ctx).get("text", ""), ctx.thread_ts)
        task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        if ctx.conversation_id:
            self.conn.execute("UPDATE conversations SET task_id = ?, project = ? WHERE id = ?",
                              (task.id, project, ctx.conversation_id))
        await self.notifier.task_changed(task)
        return ToolReply(f"Tracking {task.id}: {task.title}")

    async def _finish_task(self, ctx, args) -> ToolReply:
        task = tasks.get_task(self.conn, ctx.task_id) if ctx.task_id else None
        if task is None:
            return ToolReply("there is no task to finish in this conversation")
        if task.status == Status.needs_input:
            task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        try:
            task = tasks.transition(self.conn, task.id, Status.done, actor=ctx.actor or "agent",
                                    data={"outcome": args.get("outcome", "")})
        except IllegalTransition as e:
            return ToolReply(f"can't finish {task.id}: {e}")
        await self.notifier.task_changed(task)
        return ToolReply(f"{task.id} is done.")

    async def _start_task(self, ctx, args) -> ToolReply:
        project, kind = args.get("project", ""), args.get("kind", "")
        if problem := self._new_task_problem(project, kind):
            return ToolReply(problem)
        brief = args.get("brief", "").strip()
        title = args.get("title", "").strip() or "Untitled"
        # a DM has no thread of its own: post the child's thread-opening line before anything is created, so a
        # failed post (caught by the router and reported as a tool error) leaves no task, conversation or work item
        if ctx.channel and ctx.thread_ts is None:
            posted = await self.slack.chat_postMessage(channel=ctx.channel,
                                                       text=scrub(f"Working on {title}", self.secret_values))
            thread_ts, is_primary = posted["ts"], 1
        else:
            thread_ts, is_primary = ctx.thread_ts, 0
        task = self._create_task(ctx, project, title, kind, brief, thread_ts)
        task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        convo_id = create_conversation(self.conn, channel=ctx.channel, thread_ts=thread_ts, project=project,
                                       task_id=task.id, is_primary=is_primary)
        if is_primary:
            record_message(self.conn, ctx.channel, thread_ts, convo_id)
        payload = self.turn_payload(convo_id, brief, ctx.actor, ctx.unattended)
        payload["origin"] = self._handover_origin(ctx)
        self.queue.enqueue(convo_id, "turn", payload)
        await self.notifier.task_changed(task)
        return ToolReply(f"Started {task.id} ({task.title}) in a new session for {project}.")

    def _handover_origin(self, ctx: ToolContext) -> str:
        """A hand-over turn's first line: the calling turn's own (who sent its message, where and the link), marked
        as handed over once however often it is handed on. A calling turn without one gives its actor and channel."""
        asked = self._payload(ctx).get("origin") or origin_line(ctx.channel or "no Slack thread", who=ctx.actor)
        mark = f" (handed over by {self.config.name})"
        return asked if asked.endswith(mark) else asked + mark

    async def _status(self, ctx, args) -> ToolReply:
        paused = db.get_setting(self.conn, "paused", "0") == "1"
        # each running item is named by its conversation's project ("-" when unbound), with how long it has run
        # since its runner claimed it (the item's latest session_started event)
        running = self.conn.execute(
            "SELECT COALESCE(c.project, '-'), (SELECT CAST((julianday('now') - julianday(MAX(e.ts))) * 1440 AS "
            "INTEGER) FROM events e WHERE e.type = 'session_started' AND json_extract(e.data, '$.work_item') = w.id) "
            "FROM work_items w LEFT JOIN conversations c ON c.id = w.conversation_id WHERE w.status = 'claimed' "
            "ORDER BY w.created_at, w.rowid").fetchall()
        queued = self.conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'queued'").fetchone()[0]
        open_tasks = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'working', "
                                       "'needs_input', 'ready_for_review')").fetchone()[0]
        lines = [f"paused: {'yes' if paused else 'no'}",
                 f"running: {len(running)}" + (f" ({', '.join(_running(*r) for r in running)})" if running else ""),
                 f"queued: {queued}", f"open tasks: {open_tasks}"]
        return ToolReply("\n".join(lines))

    async def _list_tasks(self, ctx, args) -> ToolReply:
        status = args.get("status")
        try:
            found = tasks.list_tasks(self.conn, status=Status(status) if status else None,
                                     project=args.get("project"))
        except ValueError:
            return ToolReply(f"unknown status '{status}'")
        if not found:
            return ToolReply("no tasks")
        return ToolReply("\n".join(f"{t.id} [{t.status.value}] {t.project}: {t.title}" for t in found[-50:]))

    async def _get_task(self, ctx, args) -> ToolReply:
        task = tasks.get_task(self.conn, args.get("task_id", ""))
        if task is None:
            return ToolReply("no such task")
        lines = [f"{task.id} · {task.title} — {task.status.value}", f"project: {task.project} · kind: {task.kind}",
                 f"requester: {task.requester or '-'}"]
        if task.pr_url:
            lines.append(f"PR: {task.pr_url}")
        return ToolReply("\n".join(lines))

    async def _cancel_task(self, ctx, args) -> ToolReply:
        task_id = args.get("task_id", "")
        try:
            task = tasks.transition(self.conn, task_id, Status.cancelled, actor=ctx.actor or "agent")
        except IllegalTransition as e:
            return ToolReply(f"can't cancel: {e}")
        rows = self.conn.execute("SELECT w.id FROM work_items w JOIN conversations c ON c.id = w.conversation_id "
                                 "WHERE c.task_id = ? AND w.status IN ('queued', 'claimed')", (task_id,)).fetchall()
        for row in rows:
            self.queue.cancel(row["id"])
        await self.notifier.task_changed(task)
        return ToolReply(f"Cancelled {task_id}.")

    async def _stop_all(self, ctx, args) -> ToolReply:
        db.set_setting(self.conn, "paused", "1")
        threads = {(r["channel"], r["thread_ts"]) for r in self.conn.execute(
            "SELECT c.channel, c.thread_ts FROM work_items w JOIN conversations c ON c.id = w.conversation_id "
            "WHERE w.status = 'claimed' AND c.channel IS NOT NULL")}
        cancelled = self.queue.cancel_running()
        events.append(self.conn, "stop_all", task_id=None, actor=ctx.actor, data={"cancelled": cancelled})
        for channel, thread_ts in sorted(threads):
            await self.slack.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                              text=scrub(f"Stopped by {ctx.actor or 'an admin'}. I'll pick this up "
                                                         "again when an admin resumes.", self.secret_values))
        return ToolReply(f"Stopped. Interrupted {len(cancelled)} running sessions; queued messages wait, and nothing "
                         "new starts until an admin resumes.")

    async def _resume_all(self, ctx, args) -> ToolReply:
        db.set_setting(self.conn, "paused", "0")
        events.append(self.conn, "resume_all", task_id=None, actor=ctx.actor, data=None)
        return ToolReply("Resumed.")

    async def _read_thread(self, ctx, args) -> ToolReply:
        if not ctx.channel:
            return ToolReply("this conversation has no Slack thread")
        limit = min(int(args.get("limit") or 50), 200)
        if ctx.thread_ts:
            messages = (await self.slack.conversations_replies(channel=ctx.channel, ts=ctx.thread_ts,
                                                               limit=limit)).get("messages", [])
        else:  # a DM chat: its top-level messages from the first one recorded for this conversation
            first = self.conn.execute("SELECT ts FROM slack_messages WHERE channel = ? AND conversation_id = ? "
                                      "ORDER BY CAST(ts AS REAL) LIMIT 1",
                                      (ctx.channel, ctx.conversation_id)).fetchone()
            if first is None:
                return ToolReply("no Slack messages in this conversation yet")
            resp = await self.slack.conversations_history(channel=ctx.channel, oldest=first["ts"], inclusive=True,
                                                          limit=limit)
            messages = list(reversed(resp.get("messages", [])))  # Slack gives newest first
        lines = []
        for m in messages:
            person = self.config.person_by_slack(m.get("user", ""))
            who = person.key if person else ("agent" if m.get("bot_id") else m.get("user", "someone"))
            lines.append(f"{who}: {m.get('text', '')}")
        return ToolReply("\n".join(lines) or "the thread is empty")

    async def _slack_read(self, ctx, args) -> ToolReply:
        method = str(args.get("method") or "")
        if method not in SLACK_READ_METHODS:
            return ToolReply(f"slack_read can't call '{method}'. Methods: {', '.join(SLACK_READ_METHODS)}")
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return ToolReply("params must be an object")
        # a query string takes strings and numbers; Slack reads 1/0 as true/false and comma lists as lists
        query = {k: ("1" if v else "0") if isinstance(v, bool) else ",".join(map(str, v)) if isinstance(v, list)
                 else v if isinstance(v, (str, int, float)) else json.dumps(v)
                 for k, v in params.items() if v is not None}
        resp = await self.slack.api_call(method, http_verb="GET", params=query)
        # scrubbed before the cut, so a cut never lands inside a secret
        text = scrub(json.dumps(getattr(resp, "data", resp)), self.secret_values)
        if len(text) > SLACK_READ_CHARS:
            text = (text[:SLACK_READ_CHARS] + f"\n[truncated: {len(text)} characters in all; ask for less with "
                    "limit, or page with cursor]")
        return ToolReply(text)
