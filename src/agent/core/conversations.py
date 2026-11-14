"""Conversations: Slack and Linear messages become work items on the work queue.

Code only routes. A Slack thread, a DM or a Linear issue is a conversation mapped to one agent session; each new
message is the next turn of that session (the work queue never runs two turns of one conversation at once). A DM
is one rolling chat: its top-level messages continue the latest conversation until it has been quiet for DM_IDLE.
A reply in a thread goes to a session that asked a question there, otherwise to the thread's primary conversation,
otherwise to the conversation of the message the thread hangs off (every message core receives or posts is
recorded in slack_messages). Each turn carries a line for the top of its prompt: who sent the message, where,
and its link.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

from agent import db, events, tasks
from agent.connectors.linear import issue_link
from agent.core.sessions import WorkItem, WorkQueue
from agent.core.slack_markdown import post_markdown
from agent.gitutil import branch_name
from agent.scrub import scrub
from agent.tasks import Status

log = logging.getLogger(__name__)
NOT_AUTHORISED = "Sorry, I only work with people on this team's list."
FAILED_REPLY = "Something went wrong on my side, so I couldn't finish that. Try again in a moment."
FAILED_OUTCOMES = {"error", "timeout", "max_turns", "interrupted"}
_FABLE = re.compile(r"\buse fable\b", re.I)
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
DM_IDLE = timedelta(hours=12)  # a DM quiet this long starts a fresh conversation on its next top-level message
LOOKUP_S = 3  # the longest a Slack lookup for a turn's first line may take; the line goes without it after that


def origin_line(where: str, *, who: str | None = None, link: str | None = None) -> str:
    """A turn's first line, e.g. [from Sam Taylor (sam) · DM · <permalink>] or [Linear SAM-7 · <url>]."""
    return "[" + " · ".join(p for p in (f"from {who}" if who else None, where, link) if p) + "]"


async def _nothing(*args) -> None:
    return None


async def _answer(lookup: Awaitable):
    """A lookup's answer, or None when it fails or takes longer than LOOKUP_S."""
    try:
        return await asyncio.wait_for(lookup, LOOKUP_S)
    except Exception:
        return None


def record_message(conn: sqlite3.Connection, channel: str, ts: str, conversation_id: str) -> None:
    """Remember which conversation a Slack message belongs to, so a thread started under it finds its way back."""
    conn.execute("INSERT OR IGNORE INTO slack_messages(channel, ts, conversation_id) VALUES (?, ?, ?)",
                 (channel, ts, conversation_id))


def create_conversation(conn: sqlite3.Connection, *, agent_name: str = "", channel: str | None, thread_ts: str | None,
                        project: str | None, title: str, task_id: str | None = None, is_primary: int = 1) -> str:
    """A new conversation. One bound to its project up front gets its branch now, named from `title`, and works in
    its checkout; any other works in its session folder. The working folder never changes, so --resume always finds
    the session, and a branch once set never does either, so every turn and every open_pr use the same one."""
    convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
    branch = branch_name(agent_name, task_id or convo_id, title) if project else None
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, task_id, is_primary, branch, workdir, "
                 "created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (convo_id, channel, thread_ts, project, task_id, is_primary, branch,
                  "checkout" if project else "session", now, now))
    return convo_id


class BindRefused(Exception):
    pass


def bind_project(conn: sqlite3.Connection, convo_id: str, project: str, *, packs: dict, agent_name: str,
                 title: str) -> sqlite3.Row:
    """work_on's binding: the conversation's project, and its branch the first time it needs one (named from
    `title`). One project per conversation; binding the same one again changes nothing. The working folder stays
    as the first turn fixed it."""
    if project not in packs:
        raise BindRefused(f"unknown project '{project}' (known: {', '.join(sorted(packs)) or 'none'})")
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()
    if row["project"] and row["project"] != project:
        raise BindRefused(f"this conversation is bound to {row['project']}; use start_task for work on {project}")
    if row["project"] == project and row["branch"]:
        return row
    branch = row["branch"] or branch_name(agent_name, row["task_id"] or convo_id, title)
    conn.execute("UPDATE conversations SET project = ?, branch = ? WHERE id = ?", (project, branch, convo_id))
    events.append(conn, "project_bound", task_id=row["task_id"], actor=None,
                  data={"conversation_id": convo_id, "project": project, "branch": branch})
    return conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()


class Conversations:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, queue: WorkQueue, slack, payloads,
                 notifier, bot_user_id: str | None, linear_poller=None,
                 person_name: Callable[[str], Awaitable[str | None]] = _nothing,
                 permalink: Callable[[str, str], Awaitable[str | None]] = _nothing,
                 secret_values: Iterable[str] = ()):
        """person_name(key) and permalink(channel, ts) are core's cached Slack lookups, for each turn's first line.
        secret_values are core's own, scrubbed from each turn's final reply before it is posted."""
        self.conn, self.config, self.packs, self.queue = conn, config, packs, queue
        self.secret_values = list(secret_values)
        self.slack, self.payloads, self.notifier = slack, payloads, notifier
        self.bot_user_id, self.linear_poller = bot_user_id, linear_poller
        self.person_name, self.permalink = person_name, permalink
        self._channel_names: dict[str, str] = {}
        self._arrivals: dict[str, asyncio.Future] = {}  # each channel's latest message, until its lookups are done

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
        return self.conn.execute("SELECT 1 FROM conversations WHERE channel = ? AND thread_ts IS ? UNION ALL "
                                 "SELECT 1 FROM slack_messages WHERE channel = ? AND ts = ? LIMIT 1",
                                 (channel, thread_ts, channel, thread_ts)).fetchone() is not None

    def _new(self, channel: str | None, thread_ts: str | None, project: str | None, title: str,
             task_id: str | None = None) -> str:
        return create_conversation(self.conn, agent_name=self.config.name, channel=channel, thread_ts=thread_ts,
                                   project=project, title=title, task_id=task_id)

    async def _channel_name(self, channel: str) -> str:
        """The channel's name, or "" when Slack won't say."""
        if channel not in self._channel_names:
            try:
                info = await self.slack.conversations_info(channel=channel)
                self._channel_names[channel] = info["channel"]["name"]
            except Exception:
                self._channel_names[channel] = ""
        return self._channel_names[channel]

    async def _project_for_channel(self, channel: str) -> str | None:
        for name, pack in self.packs.items():
            if pack.project.slack_channel == channel:
                return name
        named = {p.project.slack_channel.lstrip("#"): n for n, p in self.packs.items()
                 if p.project.slack_channel.startswith("#")}
        if not named:
            return None
        return named.get(await self._channel_name(channel))

    async def _slack_origin(self, person_key: str, event: dict, is_dm: bool) -> str:
        """Who sent the message, where, and its link. A lookup that fails or is slow is left out (a channel
        without a name shows its id); the turn never waits on it for more than LOOKUP_S."""
        channel = event["channel"]
        lookups = [self.person_name(person_key), self.permalink(channel, event["ts"])]
        if not is_dm:
            lookups.append(self._channel_name(channel))
        name, link, *channel_name = await asyncio.gather(*map(_answer, lookups))
        where = "DM" if is_dm else f"#{channel_name[0] or channel}" + (" thread" if event.get("thread_ts") else "")
        return origin_line(where, who=f"{name} ({person_key})" if name else person_key, link=link)

    async def _in_order(self, channel: str, lookup: Awaitable[str]) -> str:
        """`lookup`'s answer, once every message that arrived earlier in this channel is past its own lookups.
        From there routing is settled before anything else is awaited, so the lookups of several messages run side
        by side and a quick one never lets its message overtake an earlier one."""
        earlier = self._arrivals.get(channel)
        mine = self._arrivals[channel] = asyncio.get_running_loop().create_future()
        try:
            answer = await lookup
            if earlier is not None:
                await earlier
            return answer
        finally:
            mine.set_result(None)
            if self._arrivals.get(channel) is mine:
                del self._arrivals[channel]

    def _rolling_dm(self, channel: str) -> sqlite3.Row | None:
        """The DM's current chat: the top-level conversation active most recently, unless it has been quiet for
        DM_IDLE. A conversation in one of the DM's threads (a start_task hand-over's, say) has a thread_ts, so it is
        never picked."""
        since = (datetime.now(UTC) - DM_IDLE).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS NULL AND "
                                 "is_primary = 1 AND last_active_at >= ? "
                                 "ORDER BY last_active_at DESC, created_at DESC, rowid DESC LIMIT 1",
                                 (channel, since)).fetchone()

    def _in_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        """A thread's own primary conversation, otherwise the conversation of the message it hangs off."""
        return (self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND "
                                  "is_primary = 1 ORDER BY created_at LIMIT 1", (channel, thread_ts)).fetchone()
                or self.conn.execute("SELECT c.* FROM slack_messages m JOIN conversations c ON c.id = "
                                     "m.conversation_id WHERE m.channel = ? AND m.ts = ?",
                                     (channel, thread_ts)).fetchone())

    async def _route(self, channel: str, thread_ts: str | None, is_dm: bool, text: str) -> sqlite3.Row:
        """thread_ts is None for a top-level DM message, which goes to the DM's current chat. In a thread, a session
        waiting on an answer there takes it first. The conversation is settled (and marked active) before the
        notifier is awaited, so a message arriving meanwhile routes as it would a moment later."""
        if thread_ts is None:
            convo = self._rolling_dm(channel)
        else:
            convo = self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND "
                                      "waiting = 1 ORDER BY last_active_at DESC LIMIT 1",
                                      (channel, thread_ts)).fetchone() or self._in_thread(channel, thread_ts)
        if convo is None:
            project = None if is_dm else await self._project_for_channel(channel)
            title = text.strip().splitlines()[0] if text.strip() else ""  # the branch is named from the first line
            return self._row(self._new(channel, thread_ts, project, title))
        self.conn.execute("UPDATE conversations SET waiting = 0, last_active_at = ? WHERE id = ?",
                          (db.utcnow(), convo["id"]))
        if convo["waiting"]:  # the message answers its question
            task = tasks.get_task(self.conn, convo["task_id"]) if convo["task_id"] else None
            if task is not None and task.status == Status.needs_input:
                await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.working, actor="human"))
        return self._row(convo["id"])

    def _enqueue_turn(self, convo: sqlite3.Row, text: str, actor: str | None, *, origin: str, unattended: bool = False,
                      message_ts: str | None = None, reply_thread_ts: str | None = None) -> str:
        payload = self.payloads.turn(convo, text, actor, unattended=unattended)
        payload["origin"] = origin  # the runner puts it at the top of the prompt
        if message_ts:
            payload["message_ts"] = message_ts
        if reply_thread_ts:  # the thread the message came from, when it isn't the conversation's own
            payload["reply_thread_ts"] = reply_thread_ts
        self.conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (db.utcnow(), convo["id"]))
        return self.queue.enqueue(convo["id"], "turn", payload)

    # intake

    async def handle_slack(self, event: dict) -> None:
        user, channel = event.get("user"), event.get("channel")
        if not user or not channel or event.get("bot_id") or event.get("subtype") or user == self.bot_user_id:
            return
        is_dm = event.get("channel_type") == "im"
        thread_ts = event.get("thread_ts") or (None if is_dm else event["ts"])  # a DM's top level is one chat
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
        origin = await self._in_order(channel, self._slack_origin(person.key, event, is_dm))
        convo = await self._route(channel, thread_ts, is_dm, text)
        record_message(self.conn, channel, event["ts"], convo["id"])
        if _FABLE.search(text):
            self.conn.execute("UPDATE conversations SET model = ? WHERE id = ?", (self.config.models.hard, convo["id"]))
            convo = self._row(convo["id"])
        came_from = event.get("thread_ts")
        self._enqueue_turn(convo, text, person.key, origin=origin, message_ts=event["ts"],
                           reply_thread_ts=came_from if came_from != convo["thread_ts"] else None)
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
        convo = self._row(self._new(resp["channel"], resp["ts"], task.project, task.title, task.id))
        task = tasks.transition(self.conn, task.id, Status.working, actor="linear")
        self._enqueue_turn(convo, f"Linear issue {task.source_ref}: {task.title}\n\n{task.description}",
                           task.requester, unattended=True,
                           origin=origin_line(f"Linear {task.source_ref}", link=issue_link(task.description)))
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
        text = FAILED_REPLY if failed else scrub(text, self.secret_values)
        # Saved before anything is awaited: the next turn can be claimed as soon as this one completed, and it
        # resumes whatever session the conversation has at that moment
        if outcome == "error":  # a broken or missing session must not be resumed again; the next turn starts fresh
            self.conn.execute("UPDATE conversations SET session_id = NULL, last_active_at = ? WHERE id = ?",
                              (db.utcnow(), convo["id"]))
        else:
            self.conn.execute("UPDATE conversations SET session_id = COALESCE(?, session_id), last_active_at = ? "
                              "WHERE id = ?", (result.get("session_id"), db.utcnow(), convo["id"]))
        if text and convo["channel"]:  # answered in the thread the message came from, else the conversation's own
            reply_to = item.payload.get("reply_thread_ts") or convo["thread_ts"]
            for ts in await post_markdown(self.slack, channel=convo["channel"], thread_ts=reply_to, text=text):
                record_message(self.conn, convo["channel"], ts, convo["id"])
        task = tasks.get_task(self.conn, convo["task_id"]) if convo["task_id"] else None
        if failed and task is not None and task.status == Status.working:
            await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.failed, actor="agent",
                                                              data={"outcome": outcome}))
