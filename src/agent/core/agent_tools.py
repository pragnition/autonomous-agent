"""core's tools for the general agent (spec v2 §7): where the guarantees live.

Every call arrives with a session token; core resolves it to a ToolContext (conversation, project, sandbox
profile and the actor whose message started the turn). Tiers come from the actor, never from the model's
arguments, and a tool is offered only in the profiles it belongs to. Every reply is stripped of secrets.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from agent import db, events, tasks
from agent.memory.scopes import SessionContext, scopes_for
from agent.memory.scrub import scrub
from agent.memory.store import MemoryStore
from agent.memory.tools import MemoryToolbox
from agent.tasks import IllegalTransition, Status

log = logging.getLogger(__name__)

ALL_PROFILES = ("code", "debug", "general")
TIER_ORDER = {"read": 0, "act": 1, "admin": 2}
KINDS = ("feature", "bug", "chore")
NOT_AVAILABLE = "not available here"


@dataclass
class ToolContext:
    work_item_id: str
    conversation_id: str | None
    sandbox: str
    profile: Literal["code", "debug", "general"]
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
    file: tuple[str, bytes] | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    tier: str
    profiles: tuple[str, ...] = ALL_PROFILES


Handler = Callable[[ToolContext, dict], Awaitable[ToolReply]]


def schema(required: dict[str, str] | None = None, optional: dict[str, str] | None = None) -> dict:
    props = {k: {"type": "array", "items": {"type": "string"}} if v == "array" else {"type": v}
             for k, v in {**(required or {}), **(optional or {})}.items()}
    return {"type": "object", "properties": props, "required": list(required or {})}


def conversation_scopes(channel: str | None, project: str | None, actor: str | None, packs: dict) -> list[str]:
    """Memory scopes for a conversation (v1 §6.15): a DM sees the person's notes, a channel its own."""
    if channel and channel.startswith("D") and actor:
        sctx = SessionContext(role="session", dm_person=actor, person_projects=list(packs))
    elif channel:
        sctx = SessionContext(role="session", project=project, channel_id=channel,
                              participants=[actor] if actor else [])
    else:
        sctx = SessionContext(role="session", project=project, requester=actor)
    return scopes_for(sctx)


def profile_of(sandbox: str) -> str:
    return "general" if sandbox == "general" else sandbox.split("-", 1)[0]


class AgentToolRouter:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, queue, memory: MemoryStore, slack, notifier,
                 secret_values: list[str],
                 turn_payload: Callable[[str, str, str | None, bool], dict] | None = None):
        self.conn, self.config, self.packs, self.queue = conn, config, packs, queue
        self.memory, self.slack, self.notifier = memory, slack, notifier
        self.secret_values = list(secret_values)
        self.turn_payload = turn_payload or (lambda convo_id, text, actor, unattended:
                                             {"text": text, "actor": actor, "unattended": unattended})
        self._tools: dict[str, tuple[ToolSpec, Handler]] = {}
        self._boxes: dict[str, MemoryToolbox] = {}
        self._register_builtins()

    # registry

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        self._tools[spec.name] = (spec, handler)

    def _allowed(self, spec: ToolSpec, ctx: ToolContext) -> bool:
        return ctx.profile in spec.profiles and TIER_ORDER[spec.tier] <= TIER_ORDER[ctx.tier]

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
        return ToolReply(scrub(reply.text, self.secret_values), reply.file)

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
        return ToolContext(work_item_id=work_item_id, conversation_id=row["conversation_id"], sandbox=row["sandbox"],
                           profile=profile_of(row["sandbox"]),
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
        await self.slack.chat_postMessage(channel=ctx.channel, thread_ts=ctx.thread_ts,
                                          text=scrub(text, self.secret_values))
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

    def _create_task(self, ctx: ToolContext, project: str, title: str, kind: str, description: str):
        return tasks.create_task(self.conn, project=project, title=title, description=description, kind=kind,
                                 source="linear" if ctx.unattended else "slack",
                                 source_ref=f"{ctx.channel}/{ctx.thread_ts}" if ctx.channel else None,
                                 requester=ctx.actor, owner=self.packs[project].project.owner,
                                 slack_channel=ctx.channel, slack_thread_ts=ctx.thread_ts)

    def ensure_task(self, ctx: ToolContext, title: str, kind: str = "feature"):
        """The conversation's task, created (and linked) when the agent didn't call track_task first."""
        if ctx.task_id:
            return tasks.get_task(self.conn, ctx.task_id)
        task = self._create_task(ctx, ctx.project, title or "Untitled", kind, self._payload(ctx).get("text", ""))
        task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        if ctx.conversation_id:
            self.conn.execute("UPDATE conversations SET task_id = ? WHERE id = ?", (task.id, ctx.conversation_id))
        ctx.task_id = task.id
        return task

    def _box(self, ctx: ToolContext) -> MemoryToolbox:
        if ctx.work_item_id not in self._boxes:
            kind = "production" if ctx.profile == "debug" else "session"
            self._boxes[ctx.work_item_id] = MemoryToolbox(
                self.memory, conversation_scopes(ctx.channel, ctx.project, ctx.actor, self.packs),
                self.config.memory_budget.lookups.get(kind, 3000),
                run_id=ctx.work_item_id, actor=ctx.actor, secret_values=self.secret_values)
        return self._boxes[ctx.work_item_id]

    # built-in tools

    def _register_builtins(self) -> None:
        reg, s = self.register, schema
        code_general = ("code", "general")
        # Debug sessions never talk to Slack: their only output is the scrubbed answer ask_production returns
        reg(ToolSpec("post_update", "Post a short progress update in this conversation's Slack thread.",
                     s({"text": "string"}), "act", code_general), self._post_update)
        reg(ToolSpec("ask", "Ask the person a question when the request itself is unclear. Their reply arrives as "
                            "your next message, so end your turn after asking.", s({"question": "string"}), "act",
                     code_general), self._ask)
        reg(ToolSpec("react", "Add an emoji reaction to the message you are answering.", s({"emoji": "string"}),
                     "act", code_general), self._react)
        reg(ToolSpec("post_files", "Post files from your worktree (screenshots, videos, reports) to this "
                                   "conversation's Slack thread, with one line of text.",
                     s({"paths": "array", "text": "string"}), "act", code_general), self._post_files)
        reg(ToolSpec("track_task", "Record that this conversation is now a piece of work (for status, Linear and "
                                   "metrics). kind: feature, bug or chore.",
                     s({"project": "string", "title": "string", "kind": "string"}), "act", code_general),
            self._track_task)
        reg(ToolSpec("finish_task", "Mark this conversation's task done when it ends without a PR.",
                     s({"outcome": "string"}), "act", code_general), self._finish_task)
        reg(ToolSpec("start_task", "Start a separate session in a project's coding sandbox for a piece of work, "
                                   "with a self-contained brief. Use it to hand work from a DM to a project, or to "
                                   "split one request into parallel tasks.",
                     s({"project": "string", "title": "string", "kind": "string", "brief": "string"}), "act",
                     code_general), self._start_task)
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
        reg(ToolSpec("read_thread", "Read this conversation's Slack thread (oldest first).",
                     s(optional={"limit": "integer"}), "read"), self._read_thread)
        reg(ToolSpec("recall", "Search your memory; returns ids and one-line summaries.",
                     s({"query": "string"}, {"limit": "integer"}), "read"), self._recall)
        reg(ToolSpec("read_memory", "Read one memory note by id.", s({"id": "string"}), "read"), self._read_memory)
        reg(ToolSpec("remember", "Note something worth remembering for later. about: me, channel or project.",
                     s({"text": "string"}, {"about": "string"}), "act"), self._remember)

    async def _post_files(self, ctx, args) -> ToolReply:
        return ToolReply("post_files is handled by the sandbox runner, which reads the files from your worktree")

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
                                 self._payload(ctx).get("text", ""))
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
        task = self._create_task(ctx, project, args.get("title", "").strip() or "Untitled", kind, brief)
        task = tasks.transition(self.conn, task.id, Status.working, actor=ctx.actor or "agent")
        convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
        self.conn.execute("INSERT INTO conversations(id, channel, thread_ts, sandbox, project, task_id, is_primary, "
                          "created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                          (convo_id, ctx.channel, ctx.thread_ts, f"code-{project}", project, task.id, now, now))
        self.queue.enqueue(convo_id, f"code-{project}", "turn",
                           self.turn_payload(convo_id, brief, ctx.actor, ctx.unattended))
        await self.notifier.task_changed(task)
        return ToolReply(f"Started {task.id} ({task.title}) in a new session in code-{project}.")

    async def _status(self, ctx, args) -> ToolReply:
        paused = db.get_setting(self.conn, "paused", "0") == "1"
        running = self.conn.execute("SELECT sandbox, kind FROM work_items WHERE status = 'claimed' "
                                    "ORDER BY sandbox").fetchall()
        queued = self.conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'queued'").fetchone()[0]
        open_tasks = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'working', "
                                       "'needs_input', 'ready_for_review')").fetchone()[0]
        lines = [f"paused: {'yes' if paused else 'no'}",
                 f"running: {len(running)}" + (f" ({', '.join(f'{r[0]}/{r[1]}' for r in running)})" if running else ""),
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
                                              text=f"Stopped by {ctx.actor or 'an admin'}. I'll pick this up again "
                                                   "when an admin resumes.")
        return ToolReply(f"Stopped. Interrupted {len(cancelled)} running sessions; queued messages wait, and nothing "
                         "new starts until an admin resumes.")

    async def _resume_all(self, ctx, args) -> ToolReply:
        db.set_setting(self.conn, "paused", "0")
        events.append(self.conn, "resume_all", task_id=None, actor=ctx.actor, data=None)
        return ToolReply("Resumed.")

    async def _read_thread(self, ctx, args) -> ToolReply:
        if not ctx.channel or not ctx.thread_ts:
            return ToolReply("this conversation has no Slack thread")
        resp = await self.slack.conversations_replies(channel=ctx.channel, ts=ctx.thread_ts,
                                                      limit=min(int(args.get("limit") or 50), 200))
        lines = []
        for m in resp.get("messages", []):
            person = self.config.person_by_slack(m.get("user", ""))
            who = person.key if person else ("agent" if m.get("bot_id") else m.get("user", "someone"))
            lines.append(f"{who}: {m.get('text', '')}")
        return ToolReply("\n".join(lines) or "the thread is empty")

    async def _recall(self, ctx, args) -> ToolReply:
        hits = self._box(ctx).recall(args.get("query", ""), int(args.get("limit") or 5))
        return ToolReply(hits if isinstance(hits, str) else json.dumps(hits))

    async def _read_memory(self, ctx, args) -> ToolReply:
        return ToolReply(self._box(ctx).read_memory(args.get("id", "")))

    async def _remember(self, ctx, args) -> ToolReply:
        return ToolReply(self._box(ctx).remember(args.get("text", ""), args.get("about")))
