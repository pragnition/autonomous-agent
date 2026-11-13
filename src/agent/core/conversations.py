"""Conversations (spec v2 §6, §8): Slack and Linear messages become work items for the right sandbox.

Code only routes. A Slack thread, a DM or a Linear issue is a conversation mapped to one agent session; each new
message is the next turn of that session (the work queue never runs two turns of one conversation at once). A
reply goes to a session that asked a question in the thread, otherwise to the thread's primary conversation.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid

from agent import db, tasks
from agent.core.sessions import WorkItem, WorkQueue
from agent.core.slack_markdown import post_markdown
from agent.tasks import Status

log = logging.getLogger(__name__)
NOT_AUTHORISED = "Sorry, I only work with people on this team's list."
FAILED_REPLY = "Something went wrong on my side, so I couldn't finish that. Try again in a moment."
FAILED_OUTCOMES = {"error", "timeout", "max_turns", "max_budget", "interrupted", "blocked"}
_FABLE = re.compile(r"\buse fable\b", re.I)
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


class Conversations:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, queue: WorkQueue, slack, payloads,
                 notifier, bot_user_id: str | None, linear_poller=None):
        self.conn, self.config, self.packs, self.queue = conn, config, packs, queue
        self.slack, self.payloads, self.notifier = slack, payloads, notifier
        self.bot_user_id, self.linear_poller = bot_user_id, linear_poller
        self._channel_names: dict[str, str] = {}

    # helpers

    def _claim(self, key: str) -> bool:
        """True the first time this Slack message is seen (Bolt retries, mention + message duplicates)."""
        cur = self.conn.execute("INSERT OR IGNORE INTO slack_events(key, at) VALUES (?, ?)", (key, db.utcnow()))
        return cur.rowcount == 1

    def _mentioned(self, event: dict) -> bool:
        return event.get("type") == "app_mention" or bool(
            self.bot_user_id and f"<@{self.bot_user_id}>" in (event.get("text") or ""))

    def _row(self, convo_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()

    def _known_thread(self, channel: str, thread_ts: str) -> bool:
        return self.conn.execute("SELECT 1 FROM conversations WHERE channel = ? AND thread_ts = ? LIMIT 1",
                                 (channel, thread_ts)).fetchone() is not None

    def _new(self, channel: str | None, thread_ts: str | None, sandbox: str, project: str | None,
             task_id: str | None = None) -> str:
        convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
        self.conn.execute("INSERT INTO conversations(id, channel, thread_ts, sandbox, project, task_id, is_primary, "
                          "created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                          (convo_id, channel, thread_ts, sandbox, project, task_id, now, now))
        return convo_id

    async def _project_for_channel(self, channel: str) -> str | None:
        for name, pack in self.packs.items():
            if pack.project.slack_channel == channel:
                return name
        named = {p.project.slack_channel.lstrip("#"): n for n, p in self.packs.items()
                 if p.project.slack_channel.startswith("#")}
        if not named:
            return None
        if channel not in self._channel_names:
            try:
                info = await self.slack.conversations_info(channel=channel)
                self._channel_names[channel] = info["channel"]["name"]
            except Exception:
                self._channel_names[channel] = ""
        return named.get(self._channel_names[channel])

    async def _route(self, channel: str, thread_ts: str, is_dm: bool) -> sqlite3.Row:
        waiting = self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts = ? AND waiting = 1 "
                                    "ORDER BY last_active_at DESC LIMIT 1", (channel, thread_ts)).fetchone()
        if waiting is not None:
            self.conn.execute("UPDATE conversations SET waiting = 0 WHERE id = ?", (waiting["id"],))
            task = tasks.get_task(self.conn, waiting["task_id"]) if waiting["task_id"] else None
            if task is not None and task.status == Status.needs_input:
                await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.working, actor="human"))
            return self._row(waiting["id"])
        primary = self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts = ? AND is_primary = 1 "
                                    "ORDER BY created_at LIMIT 1", (channel, thread_ts)).fetchone()
        if primary is not None:
            return primary
        project = None if is_dm else await self._project_for_channel(channel)
        sandbox = f"code-{project}" if project else "general"
        return self._row(self._new(channel, thread_ts, sandbox, project))

    def _enqueue_turn(self, convo: sqlite3.Row, text: str, actor: str | None, *, unattended: bool = False,
                      message_ts: str | None = None) -> str:
        payload = self.payloads.turn(convo, text, actor, unattended=unattended)
        if message_ts:
            payload["message_ts"] = message_ts
        self.conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (db.utcnow(), convo["id"]))
        return self.queue.enqueue(convo["id"], convo["sandbox"], "turn", payload)

    # intake

    async def handle_slack(self, event: dict) -> None:
        user, channel = event.get("user"), event.get("channel")
        if not user or not channel or event.get("bot_id") or event.get("subtype") or user == self.bot_user_id:
            return
        is_dm = event.get("channel_type") == "im"
        thread_ts = event.get("thread_ts") or event["ts"]
        mentioned = self._mentioned(event)
        if not (is_dm or mentioned or (event.get("thread_ts") and self._known_thread(channel, thread_ts))):
            return
        if not self._claim(f"{channel}:{event['ts']}"):
            return
        person = self.config.person_by_slack(user)
        if person is None:
            if is_dm or mentioned:
                await self.slack.chat_postEphemeral(channel=channel, user=user, text=NOT_AUTHORISED)
            return
        text = _MENTION.sub("", event.get("text") or "").strip()
        convo = await self._route(channel, thread_ts, is_dm)
        if _FABLE.search(text):
            self.conn.execute("UPDATE conversations SET model = ? WHERE id = ?", (self.config.models.hard, convo["id"]))
            convo = self._row(convo["id"])
        self._enqueue_turn(convo, text, person.key, message_ts=event["ts"])
        try:  # the acknowledgement: the person sees it landed long before the session replies
            await self.slack.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
        except Exception:
            log.warning("couldn't add the eyes reaction to %s:%s", channel, event["ts"], exc_info=True)

    async def intake_linear(self) -> list[str]:
        if self.linear_poller is None:
            return []
        created = await self.linear_poller.poll()
        # Tasks from an earlier poll whose Slack thread couldn't be created yet (one task per issue, ever)
        orphans = [r["id"] for r in self.conn.execute(
            "SELECT id FROM tasks WHERE source = 'linear' AND status = 'queued' AND id NOT IN "
            "(SELECT task_id FROM conversations WHERE task_id IS NOT NULL) ORDER BY id")]
        attached = []
        for task_id in dict.fromkeys([*orphans, *created]):
            try:
                await self._attach_linear(task_id)
                attached.append(task_id)
            except Exception:
                log.exception("couldn't start %s; will retry on the next poll", task_id)
        return attached

    async def _attach_linear(self, task_id: str) -> None:
        task = tasks.get_task(self.conn, task_id)
        pack = self.packs[task.project].project
        resp = await self.slack.chat_postMessage(channel=pack.slack_channel,
                                                 text=f"Linear {task.source_ref}: {task.title}")
        task = tasks.set_fields(self.conn, task.id, slack_channel=resp["channel"], slack_thread_ts=resp["ts"])
        convo = self._row(self._new(resp["channel"], resp["ts"], f"code-{task.project}", task.project, task.id))
        task = tasks.transition(self.conn, task.id, Status.working, actor="linear")
        self._enqueue_turn(convo, f"Linear issue {task.source_ref}: {task.title}\n\n{task.description}",
                           task.requester, unattended=True)
        await self.notifier.task_changed(task)

    # results

    async def on_result(self, item: WorkItem | None, result: dict) -> None:
        if item is None or item.kind != "turn" or item.status == "cancelled":
            return
        convo = self._row(item.conversation_id) if item.conversation_id else None
        if convo is None:
            return
        outcome = result.get("outcome", "done")
        text = (result.get("text") or "").strip()
        failed = outcome in FAILED_OUTCOMES
        if failed:
            text = FAILED_REPLY
        if text and convo["channel"]:
            await post_markdown(self.slack, channel=convo["channel"], thread_ts=convo["thread_ts"], text=text)
        if outcome == "error":  # a broken or missing session must not be resumed again; the next turn starts fresh
            self.conn.execute("UPDATE conversations SET session_id = NULL, last_active_at = ? WHERE id = ?",
                              (db.utcnow(), convo["id"]))
        else:
            self.conn.execute("UPDATE conversations SET session_id = COALESCE(?, session_id), last_active_at = ? "
                              "WHERE id = ?", (result.get("session_id"), db.utcnow(), convo["id"]))
        task = tasks.get_task(self.conn, convo["task_id"]) if convo["task_id"] else None
        if failed and task is not None and task.status == Status.working:
            await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.failed, actor="agent",
                                                              data={"outcome": outcome}))
