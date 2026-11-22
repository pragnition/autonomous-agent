"""Conversations: Slack and Linear messages become work items on the work queue.

Code only routes. A Slack thread, a DM or a Linear issue is a conversation mapped to one agent session; each new
message is the next turn of that session (the work queue never runs two turns of one conversation at once). Every
top-level message, in a DM or a channel, starts its own thread and its own conversation. A reply in a thread goes to a session that asked a question there, otherwise to the thread's primary conversation,
otherwise to the conversation of the message the thread hangs off (every message core receives or posts is
recorded in slack_messages). A thread the agent started itself, posting with its own bot token rather than through
core, is noted in own_threads: a reply there starts a new conversation, since which session posted it isn't known.
Each turn carries a line for the top of its prompt: who sent the message, where,
and its link.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterable

from agent import db, events, tasks
from agent.connectors.linear import issue_link
from agent.core import spawned
from agent.core.sessions import WorkItem, WorkQueue
from agent.core.slack_markdown import post_markdown
from agent.scrub import scrub
from agent.tasks import Status

log = logging.getLogger(__name__)
NOT_AUTHORISED = "Sorry, I only work with people on this team's list."
FAILED_OUTCOMES = {"error", "timeout", "stalled", "interrupted"}
# A long project's build carries on after a turn that ended in an error (an API error, a lost session) or got stuck
# (no progress for [limits] idle_minutes), so one bad turn doesn't end days of work; after this many failures in a
# row it stops like any other turn. Not "interrupted": a real stop never posts a result. Not "timeout": that is a
# scheduled run's time budget running out, which ends the run.
RETRY_OUTCOMES, BUILD_RETRIES = {"error", "stalled"}, 3
# A turn a usage limit cut off: the API's 429, or the runner giving up on an account the proxy reports rejected or
# disabled. It waits for capacity (limit_waits) and is carried on once, by the usage watch (core/usage_watch.py).
_LIMIT_ERROR = re.compile(r"^429\b|\baccount \S+ is (rejected|disabled)\b|rate.?limit", re.I)
# A message's progress: seen -> working -> done or failed
SEEN, WORKING, DONE, FAILED = "eyes", "hourglass_flowing_sand", "white_check_mark", "warning"
# "use <model>" in a message switches its conversation's model (see model_named): a full id, a family with or without
# a version ("opus 4.6", "sonnet"), or "default". Only model-shaped names count, so "use the API" switches nothing;
# "default" only at the end of a sentence, so "use default settings" doesn't either.
_USE_MODEL = re.compile(r"\buse\s+(?:the\s+)?(claude-[a-z0-9][a-z0-9.-]*[a-z0-9]"
                        r"|(?:claude\s+)?(?:opus|sonnet|haiku|fable)(?:[\s-]*\d+(?:[.-]\d+)*)?\b"
                        r"|default(?:\s+model)?(?=\s*(?:[.,!?;:]|$)))", re.I)
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
LOOKUP_S = 3  # the longest a Slack lookup for a turn's first line may take; the line goes without it after that
# A person's message with files attached arrives with this subtype; every other subtype (edits, joins, bot
# messages, deletions) is not something a person said to the agent
FILE_SHARE = "file_share"
# How improve-jensen's proposals start (agent/watch/approvals.py). It posts them with the agent's bot token and
# answers the replies in their threads itself, so those threads are not the agent's own.
IMPROVE_JENSEN_PROPOSALS = "*improve-jensen wants to change Jensen*"


def message_files(event: dict) -> list[dict]:
    """The files attached to a Slack message, as the turn carries them: the runner fetches each one through core
    (which holds the bot token) into the session folder. Files Slack won't serve (deleted, past the free plan's
    limit) are left out."""
    return [{"id": f["id"], "name": f.get("name") or f["id"], "mimetype": f.get("mimetype") or "",
             "size": int(f.get("size") or 0)}
            for f in event.get("files") or []
            if f.get("id") and f.get("mode") not in ("tombstone", "hidden_by_limit")]


def origin_line(where: str, *, who: str | None = None, link: str | None = None) -> str:
    """A turn's first line, e.g. [from Sam Taylor (sam) · DM · <permalink>] or [Linear SAM-7 · <url>]."""
    return "[" + " · ".join(p for p in (f"from {who}" if who else None, where, link) if p) + "]"


def _model_key(name: str) -> str:
    """A model name compared without "claude", case or separators: "Opus 4.6" and "claude-opus-4-6" are both
    "opus-4-6"."""
    return re.sub(r"^claude-", "", re.sub(r"[\s.-]+", "-", name.strip().lower()))


def model_named(models, name: str) -> tuple[bool, str | None]:
    """The model "use <name>" asks for: (True, the id) for an allowed model, (True, None) for "default", which puts
    the conversation back on [claude.models] default, and (False, None) for a model that isn't allowed or a name that
    fits more than one. "fable" alone is [claude.models] hard, as it always was. An exact id wins; otherwise the name
    may leave out the rest of an id ("opus 4" for claude-opus-4-6, "haiku 4.5" for claude-haiku-4-5-20251001)."""
    key = _model_key(name)
    if key in ("default", "default-model"):
        return True, None
    if key == "fable":
        return True, models.hard
    choices = models.choices()
    exact = [m for m in choices if _model_key(m) == key]
    fits = exact or [m for m in choices if _model_key(m).startswith(key + "-")]
    return (True, fits[0]) if len(fits) == 1 else (False, None)


def unknown_model_reply(models, name: str) -> str:
    listed = ", ".join(f"`{m}`" + (" (default)" if m == models.default else "") for m in models.choices())
    return (f"I can't switch this conversation to {name.strip()}, so it stays on the model it has. Models I can "
            f"switch to: {listed}. Say \"use <model>\" with one of them, or \"use default\".")


def cut_by_limit(outcome: str, error: str | None) -> bool:
    return outcome == "error" and bool(error) and bool(_LIMIT_ERROR.search(error))


def limit_wait_reply(error: str | None) -> str:
    return (f"A usage limit cut me off ({(error or '').strip()}). I'll carry on from where I stopped once an account "
            "has capacity again.")


def failure_reply(outcome: str, error: str | None) -> str:
    """What the thread is told when a turn fails: what happened, with the runner's own error text."""
    if outcome == "timeout":
        return "I ran out of time before finishing that" + (f" ({error})." if error else ".")
    if outcome == "stalled":
        return "I got stuck before finishing that" + (f" ({error})." if error else ".")
    if outcome == "interrupted":
        return "That was stopped before I finished it."
    if not error:
        return "That failed before I could finish it, and the runner didn't say why."
    return f"That failed before I could finish it:\n```\n{error.strip()}\n```"


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


def create_conversation(conn: sqlite3.Connection, *, channel: str | None, thread_ts: str | None,
                        project: str | None, task_id: str | None = None, is_primary: int = 1,
                        cwd: str | None = None) -> str:
    convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, task_id, is_primary, cwd, created_at, "
                 "last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (convo_id, channel, thread_ts, project, task_id, is_primary, cwd, now, now))
    return convo_id


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

    def _builds(self, convo_id: str) -> bool:
        """Whether the conversation is an active long project's build."""
        return self.conn.execute("SELECT 1 FROM projects WHERE conversation_id = ? AND status = 'active'",
                                 (convo_id,)).fetchone() is not None

    def _known_thread(self, channel: str, thread_ts: str) -> bool:
        return self.conn.execute("SELECT 1 FROM conversations WHERE channel = ? AND thread_ts IS ? UNION ALL "
                                 "SELECT 1 FROM slack_messages WHERE channel = ? AND ts = ? UNION ALL "
                                 "SELECT 1 FROM own_threads WHERE channel = ? AND ts = ? LIMIT 1",
                                 (channel, thread_ts, channel, thread_ts, channel, thread_ts)).fetchone() is not None

    def _own_thread(self, channel: str, thread_ts: str) -> bool:
        return self.conn.execute("SELECT 1 FROM own_threads WHERE channel = ? AND ts = ?",
                                 (channel, thread_ts)).fetchone() is not None

    def _note_own_post(self, event: dict) -> None:
        """The agent's own top-level message in a channel opens a thread whose replies should reach it, even when it
        was posted straight through Slack's Web API and core never recorded it. A DM needs nothing: every DM message
        reaches the agent. improve-jensen's proposals are left out: it answers the replies in those itself."""
        if event.get("subtype") not in (None, FILE_SHARE) or event.get("thread_ts") or not event.get("ts") \
                or event.get("channel_type") == "im" or (event.get("text") or "").startswith(IMPROVE_JENSEN_PROPOSALS):
            return
        self.conn.execute("INSERT OR IGNORE INTO own_threads(channel, ts, at) VALUES (?, ?, ?)",
                          (event["channel"], event["ts"], db.utcnow()))

    def _new(self, channel: str | None, thread_ts: str | None, project: str | None, task_id: str | None = None) -> str:
        return create_conversation(self.conn, channel=channel, thread_ts=thread_ts, project=project, task_id=task_id)

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

    async def _slack_origin(self, person_key: str, event: dict, is_dm: bool, own: bool = False) -> str:
        """Who sent the message, where, and its link. A lookup that fails or is slow is left out (a channel
        without a name shows its id); the turn never waits on it for more than LOOKUP_S. `own`: the message is a
        reply in a thread the agent started itself."""
        channel = event["channel"]
        lookups = [self.person_name(person_key), self.permalink(channel, event["ts"])]
        if not is_dm:
            lookups.append(self._channel_name(channel))
        name, link, *channel_name = await asyncio.gather(*map(_answer, lookups))
        where = "DM" if is_dm else f"#{channel_name[0] or channel}" + (" thread" if event.get("thread_ts") else "") \
            + (" you started" if own else "")
        github = getattr(self.config.people.get(person_key), "github", "")  # so PRs can be assigned to them
        tags = ", ".join(t for t in (person_key if name else "", f"GitHub @{github}" if github else "") if t)
        return origin_line(where, who=f"{name or person_key} ({tags})" if tags else person_key, link=link)

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

    def _in_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        """A thread's own primary conversation, otherwise the conversation of the message it hangs off."""
        return (self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND "
                                  "is_primary = 1 ORDER BY created_at LIMIT 1", (channel, thread_ts)).fetchone()
                or self.conn.execute("SELECT c.* FROM slack_messages m JOIN conversations c ON c.id = "
                                     "m.conversation_id WHERE m.channel = ? AND m.ts = ?",
                                     (channel, thread_ts)).fetchone())

    async def _route(self, channel: str, thread_ts: str, is_dm: bool) -> sqlite3.Row:
        """A session waiting on an answer in the thread takes the message first. The conversation is settled (and
        marked active) before the notifier is awaited, so a message arriving meanwhile routes as it would a moment
        later."""
        convo = self.conn.execute("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND "
                                  "waiting = 1 ORDER BY last_active_at DESC LIMIT 1",
                                  (channel, thread_ts)).fetchone() or self._in_thread(channel, thread_ts)
        if convo is None:
            project = None if is_dm else await self._project_for_channel(channel)
            return self._row(self._new(channel, thread_ts, project))
        self.conn.execute("UPDATE conversations SET waiting = 0, last_active_at = ? WHERE id = ?",
                          (db.utcnow(), convo["id"]))
        if convo["waiting"]:  # the message answers its question
            task = tasks.get_task(self.conn, convo["task_id"]) if convo["task_id"] else None
            if task is not None and task.status == Status.needs_input:
                await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.working, actor="human"))
        return self._row(convo["id"])

    def _unpark(self, convo_id: str, by: str | None) -> None:
        """End the conversation's open park (core/parking.py) early: a person's message resumes it now."""
        cur = self.conn.execute("UPDATE parks SET ended_at = ?, ended_how = 'replied' WHERE conversation_id = ? AND "
                                "ended_at IS NULL", (db.utcnow(), convo_id))
        if cur.rowcount:
            events.append(self.conn, "park_ended", task_id=None, actor=by,
                          data={"conversation_id": convo_id, "how": "replied"})

    def _enqueue_turn(self, convo: sqlite3.Row, text: str, actor: str | None, *, origin: str, source: str,
                      requester: str | None, follows: str | None = None, unattended: bool = False,
                      message_ts: str | None = None, reply_thread_ts: str | None = None, continuation: int = 0,
                      report_to: dict | None = None, files: list[dict] | None = None, failures: int = 0,
                      limit_resume: bool = False) -> str:
        payload = self.payloads.turn(convo, text, actor, unattended=unattended)
        if files:  # the message's attachments; the runner saves them into the session folder
            payload["files"] = files
        payload["origin"] = origin  # the runner puts it at the top of the prompt
        # where the turn came from, who asked and which turn it carries on: the task record's labeler reads them
        payload.update(source=source, requester=requester, follows=follows)
        if continuation:
            payload["continuation"] = continuation
        if failures:  # the failed turns in a row this one carries on from
            payload["failures"] = failures
        if limit_resume:  # carries on a turn a usage limit cut off; cut off again, it fails like any other turn
            payload["limit_resume"] = 1
        if message_ts:
            payload["message_ts"] = message_ts
        if reply_thread_ts:  # the thread the message came from, when it isn't the conversation's own
            payload["reply_thread_ts"] = reply_thread_ts
        if report_to:  # a scheduled run: where it says it failed
            payload["report_to"] = report_to
        self.conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (db.utcnow(), convo["id"]))
        return self.queue.enqueue(convo["id"], "turn", payload)

    # intake

    async def handle_slack(self, event: dict) -> None:
        user, channel = event.get("user"), event.get("channel")
        subtype = event.get("subtype")
        if channel and self.bot_user_id and user == self.bot_user_id:
            self._note_own_post(event)
            return
        if not user or not channel or event.get("bot_id") or (subtype and subtype != FILE_SHARE):
            return
        is_dm = event.get("channel_type") == "im"
        thread_ts = event.get("thread_ts") or event["ts"]  # a top-level message starts its own thread
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
        own = bool(event.get("thread_ts")) and self._own_thread(channel, thread_ts)
        origin = await self._in_order(channel, self._slack_origin(person.key, event, is_dm, own))
        convo = await self._route(channel, thread_ts, is_dm)
        record_message(self.conn, channel, event["ts"], convo["id"])
        self._unpark(convo["id"], person.key)  # their message carries the conversation on now
        not_allowed = self._switch_model(convo["id"], text)
        convo = self._row(convo["id"])
        came_from = event.get("thread_ts")
        self._enqueue_turn(convo, text, person.key, origin=origin, source="slack", requester=person.key,
                           message_ts=event["ts"], files=message_files(event),
                           reply_thread_ts=came_from if came_from != convo["thread_ts"] else None)
        await self._react(channel, event["ts"], SEEN)  # the person sees it landed long before the session replies
        if not_allowed:
            try:
                resp = await self.slack.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                                         text=unknown_model_reply(self.config.models, not_allowed))
                record_message(self.conn, resp["channel"], resp["ts"], convo["id"])
            except Exception:
                log.warning("couldn't say that %r isn't an allowed model", not_allowed, exc_info=True)

    def _switch_model(self, convo_id: str, text: str) -> str | None:
        """A message saying "use <model>" switches its conversation to that model from this message's turn on (a
        turn already running keeps its own). Returns the name asked for when that model isn't allowed: nothing
        changes, and the caller tells the person."""
        asked = _USE_MODEL.search(text)
        if asked is None:
            return None
        ok, model = model_named(self.config.models, asked.group(1))
        if not ok:
            return asked.group(1)
        self.conn.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, convo_id))
        return None

    async def _react(self, channel: str, ts: str, name: str, replaces: str | None = None) -> None:
        if replaces:
            try:
                await self.slack.reactions_remove(channel=channel, timestamp=ts, name=replaces)
            except Exception:
                pass  # already gone; not worth skipping the new one over
        try:
            await self.slack.reactions_add(channel=channel, timestamp=ts, name=name)
        except Exception:
            log.warning("couldn't add the %s reaction to %s:%s", name, channel, ts, exc_info=True)

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
        convo = self._row(self._new(resp["channel"], resp["ts"], task.project, task.id))
        task = tasks.transition(self.conn, task.id, Status.working, actor="linear")
        self._enqueue_turn(convo, f"Linear issue {task.source_ref}: {task.title}\n\n{task.description}",
                           task.requester, unattended=True, source="linear", requester=task.requester,
                           origin=origin_line(f"Linear {task.source_ref}", link=issue_link(task.description)))
        await self.notifier.task_changed(task)

    async def start_scheduled(self, s) -> str:
        """A due schedule: an unattended turn with its prompt in a new conversation. Nothing is posted: like a
        teammate, the agent messages whoever it needs to itself, and the run's final message goes nowhere. A run that
        fails says so in the schedule's channel (a person key means their DM); see on_result."""
        project = None if s.channel in self.config.people else await self._project_for_channel(s.channel)
        convo = self._row(self._new(None, None, project))
        by = self.config.people.get(s.by)
        if by is None:
            who = s.by
        else:
            name = await _answer(self.person_name(by.key))
            tags = ", ".join(t for t in (by.key if name else "", f"GitHub @{by.github}" if by.github else "") if t)
            who = f"{name or by.key} ({tags})" if tags else by.key
        try:
            return self._enqueue_turn(convo, s.prompt, by.key if by else None, unattended=True, source="schedule",
                                      requester=by.key if by else s.by,
                                      origin=origin_line(f"schedule {s.name} · {s.cron} · set up by {who}",
                                                         link=s.link),
                                      report_to={"channel": s.channel, "schedule": s.name})
        except Exception:  # the scheduler reports it and fires again next minute, with a new conversation
            self.conn.execute("DELETE FROM conversations WHERE id = ?", (convo["id"],))
            raise

    # results

    async def on_start(self, item: WorkItem) -> None:
        if item.kind != "turn" or item.payload.get("continuation"):  # a continuation's message is already working
            return
        convo = self._row(item.conversation_id) if item.conversation_id else None
        if convo is not None and convo["channel"] and item.payload.get("message_ts"):
            await self._react(convo["channel"], item.payload["message_ts"], WORKING, replaces=SEEN)

    async def on_beat(self, item: WorkItem, beat: dict) -> dict:
        """A running turn's heartbeat: save its live session, mark whatever it already answered inline as done,
        and hand it whatever else arrived in its conversation meanwhile."""
        convo = self._row(item.conversation_id) if item.conversation_id else None
        if beat.get("session_id") and convo is not None:
            # the account goes with the session: NULL until the proxy has said, so it's never resumed elsewhere
            self.conn.execute("UPDATE conversations SET session_id = ?, session_account = ? WHERE id = ?",
                              (beat["session_id"], beat.get("account"), convo["id"]))
        for read_item in self.queue.mark_read(beat.get("read") or [], item.id):
            ts = read_item.payload.get("message_ts")
            if ts and convo is not None and convo["channel"]:
                await self._react(convo["channel"], ts, DONE, replaces=WORKING)
        delivered = []
        if beat.get("accepts_messages") and item.conversation_id:
            delivered = self.queue.deliver(item.conversation_id, item.id)
            for d in delivered:
                ts = d.payload.get("message_ts")
                if ts and convo is not None and convo["channel"]:
                    await self._react(convo["channel"], ts, WORKING, replaces=SEEN)
        return {"messages": [{"id": d.id, "payload": d.payload} for d in delivered]}

    async def _report_scheduled_failure(self, convo: sqlite3.Row, report_to: dict, text: str) -> None:
        """A scheduled run that failed says so in its schedule's channel, and that message's thread becomes the run's
        conversation, so replies there continue it."""
        person = self.config.people.get(report_to["channel"])
        channel = (await self.slack.conversations_open(users=person.slack))["channel"]["id"] if person \
            else report_to["channel"]
        resp = await self.slack.chat_postMessage(channel=channel,
                                                 text=f"My {report_to['schedule']} run didn't finish. {text}")
        self.conn.execute("UPDATE conversations SET channel = ?, thread_ts = ? WHERE id = ?",
                          (resp["channel"], resp["ts"], convo["id"]))
        record_message(self.conn, resp["channel"], resp["ts"], convo["id"])

    async def on_result(self, item: WorkItem | None, result: dict) -> None:
        if item is None or item.kind != "turn" or item.status == "cancelled":
            return
        convo = self._row(item.conversation_id) if item.conversation_id else None
        if convo is None:
            return
        outcome, step = result.get("outcome", "done"), int(item.payload.get("continuation") or 0)
        failures = int(item.payload.get("failures") or 0)
        # a spawned agent that fails is never retried by itself: it reports the failure and its parent decides
        retrying = outcome in RETRY_OUTCOMES and failures < BUILD_RETRIES and self._builds(convo["id"]) and \
            not spawned.is_spawned(self.conn, convo["id"])
        # cut off by a usage limit: waits for capacity and is carried on once (see resume_after_limit)
        waiting = not retrying and not item.payload.get("limit_resume") and \
            cut_by_limit(outcome, result.get("error"))
        failed = outcome in FAILED_OUTCOMES and not retrying and not waiting
        if waiting:
            text = limit_wait_reply(result.get("error"))  # the ⏳ stays
        elif retrying:
            text = failure_reply(outcome, result.get("error"))  # the thread sees the real error; the ⏳ stays
        elif failed:
            text = failure_reply(outcome, result.get("error"))
        else:
            text = (result.get("text") or "").strip()
        text = scrub(text, self.secret_values)
        # Saved before anything is awaited: the next turn can be claimed as soon as this one completed, and it
        # resumes whatever session the conversation has at that moment
        # A turn a usage limit cut off keeps its session and account: resume_after_limit carries it on from there
        if outcome == "error" and not waiting:  # a broken or missing session must not be resumed; the next starts fresh
            self.conn.execute("UPDATE conversations SET session_id = NULL, session_account = NULL, last_active_at = ? "
                              "WHERE id = ?", (db.utcnow(), convo["id"]))
        elif result.get("session_id"):  # the session and the account it ran on, which the next turn checks
            self.conn.execute("UPDATE conversations SET session_id = ?, session_account = ?, last_active_at = ? "
                              "WHERE id = ?", (result["session_id"], result.get("account_code"), db.utcnow(),
                                               convo["id"]))
        else:
            self.conn.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", (db.utcnow(), convo["id"]))
        if retrying:  # resumes the session this turn ended with (a fresh one after an error), like any next turn
            p = item.payload
            what = "got stuck" if outcome == "stalled" else "ended with an error"
            note = (f"(Your last turn {what}: {result.get('error') or outcome}. Carry on from where you stopped. If "
                    "you don't remember the work, read your project's memory note and the thread (read_thread) "
                    "first.)")
            self._enqueue_turn(self._row(convo["id"]), note, p.get("actor"), origin=p.get("origin") or "",
                               source=p.get("source") or "slack", requester=p.get("requester"), follows=item.id,
                               unattended=bool(p.get("unattended")), message_ts=p.get("message_ts"),
                               reply_thread_ts=p.get("reply_thread_ts"), continuation=step + 1,
                               report_to=p.get("report_to"), failures=failures + 1)
        if waiting:
            self.conn.execute("INSERT OR IGNORE INTO limit_waits(work_item_id, conversation_id, error, created_at) "
                              "VALUES (?, ?, ?, ?)", (item.id, convo["id"], result.get("error"), db.utcnow()))
            events.append(self.conn, "usage_limit_cut", task_id=convo["task_id"], actor=None,
                          data={"work_item": item.id, "conversation_id": convo["id"], "error": result.get("error")})
        if not retrying and not waiting:  # a spawned agent's last turn reports to the session that spawned it
            self._report_to_spawner(convo, item, outcome, failed, text)
        if failed and not convo["channel"] and item.payload.get("report_to"):
            await self._report_scheduled_failure(convo, item.payload["report_to"], text)
        elif text and convo["channel"]:  # answered in the thread the message came from, else the conversation's own
            reply_to = item.payload.get("reply_thread_ts") or convo["thread_ts"]
            for ts in await post_markdown(self.slack, channel=convo["channel"], thread_ts=reply_to, text=text):
                record_message(self.conn, convo["channel"], ts, convo["id"])
        if not retrying and not waiting and convo["channel"] and item.payload.get("message_ts"):
            await self._react(convo["channel"], item.payload["message_ts"], FAILED if failed else DONE,
                              replaces=WORKING)
        task = tasks.get_task(self.conn, convo["task_id"]) if convo["task_id"] else None
        if failed and task is not None and task.status == Status.working:
            await self.notifier.task_changed(tasks.transition(self.conn, task.id, Status.failed, actor="agent",
                                                              data={"outcome": outcome}))

    def _report_to_spawner(self, convo: sqlite3.Row, item: WorkItem, outcome: str, failed: bool,
                           text: str) -> str | None:
        """Queue a finished spawned agent's report as a message in the conversation that spawned it, with the
        spawning turn's actor and origin, so the next turn there (or the running one, mid-turn) acts on it and
        answers in its Slack thread. Only once the agent is really finished: not while it is parked or has another
        turn queued (the caller already holds back a retry or a usage-limit wait). Returns the new item's id, or None
        when the conversation isn't a running spawned agent's or there is more to come in it."""
        row = spawned.running_for_child(self.conn, convo["id"])
        status = "failed" if failed else "done"
        if row is None or spawned.more_to_come(self.conn, convo["id"]) or \
                not spawned.finish(self.conn, row["id"], status, outcome, text):
            return None
        parent = self._row(row["parent_conversation_id"])
        if parent is None:
            return None
        origin = (row["origin"] or origin_line("no Slack thread", who=row["actor"])) + \
            f" (report from agent {row['id']})"
        new_id = self._enqueue_turn(parent, spawned.report_text(row, status, outcome, text), row["actor"],
                                    origin=origin, source=row["source"] or "slack", requester=row["requester"],
                                    follows=item.id, unattended=bool(row["unattended"]))
        events.append(self.conn, "agent_reported", task_id=parent["task_id"], actor=row["actor"],
                      data={"agent": row["id"], "parent": parent["id"], "status": status, "outcome": outcome,
                            "work_item": new_id})
        return new_id

    def resume_after_limit(self, work_item_id: str) -> str | None:
        """Carry on a turn a usage limit cut off, once: a new turn in its conversation. Like any next turn, the
        runner resumes its session only if the proxy keeps it on the account it last ran on; otherwise (the usual
        case, since the limit was hit on that account) a fresh session starts with a digest of the earlier one.
        Returns the new item's id, or None when there is nothing to carry on (the conversation is gone)."""
        wait = self.conn.execute("SELECT * FROM limit_waits WHERE work_item_id = ? AND resumed_at IS NULL",
                                 (work_item_id,)).fetchone()
        if wait is None:
            return None
        item, convo, new_id = self.queue.get(work_item_id), self._row(wait["conversation_id"]), None
        if item is not None and convo is not None:
            p = item.payload
            note = (f"(A usage limit cut your last turn off: {wait['error'] or 'no error text'}. An account has "
                    "capacity again; carry on from where you stopped.)")
            new_id = self._enqueue_turn(convo, note, p.get("actor"), origin=p.get("origin") or "",
                                        source=p.get("source") or "slack", requester=p.get("requester"),
                                        follows=item.id, unattended=bool(p.get("unattended")),
                                        message_ts=p.get("message_ts"), reply_thread_ts=p.get("reply_thread_ts"),
                                        continuation=int(p.get("continuation") or 0) + 1,
                                        report_to=p.get("report_to"), limit_resume=True)
        self.conn.execute("UPDATE limit_waits SET resumed_at = ?, resumed_as = ? WHERE work_item_id = ?",
                          (db.utcnow(), new_id, work_item_id))
        return new_id

    def resume_parked(self, conversation_id: str, work_item_id: str, note: str) -> str | None:
        """Carry on a conversation the agent parked (core/parking.py): a new turn in it, as whoever the parking turn
        was for, with `note` as its text. Like any next turn, the runner resumes the session when the proxy keeps it
        on its account, else starts a fresh one with a digest. The parking turn's own message was already answered,
        so this turn neither reacts to it nor counts as a continuation. None when the conversation is gone."""
        convo, item = self._row(conversation_id), self.queue.get(work_item_id)
        if convo is None:
            return None
        p = item.payload if item is not None else {}
        return self._enqueue_turn(convo, note, p.get("actor"), origin=p.get("origin") or "",
                                  source=p.get("source") or "slack", requester=p.get("requester"),
                                  follows=work_item_id, unattended=bool(p.get("unattended")),
                                  reply_thread_ts=p.get("reply_thread_ts"), report_to=p.get("report_to"))
