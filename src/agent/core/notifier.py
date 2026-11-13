"""Task status in Slack: one status message per task, edited in place, plus a status reaction on the thread root.
Every message posted for a task is recorded against the task's conversation, so a reply in a thread under one (a
DM chat's status message, a question) reaches that conversation."""

from __future__ import annotations

import sqlite3

from agent import db, tasks
from agent.core.conversations import record_message
from agent.core.slack_markdown import post_markdown
from agent.tasks import Status, Task

REACTIONS = {Status.queued: "eyes", Status.working: "hammer", Status.needs_input: "question",
             Status.ready_for_review: "white_check_mark", Status.done: "white_check_mark", Status.failed: "x"}
WORDS = {Status.queued: "queued", Status.working: "working on it", Status.needs_input: "waiting for an answer",
         Status.ready_for_review: "draft PR ready for review", Status.done: "done", Status.merged: "merged",
         Status.closed: "PR closed", Status.failed: "failed", Status.cancelled: "cancelled"}


def status_text(task: Task) -> str:
    text = f"{task.id} · {task.title} — {WORDS.get(task.status, task.status.value)}"
    return f"{text}\n{task.pr_url}" if task.pr_url else text


class SlackNotifier:
    def __init__(self, slack, conn: sqlite3.Connection):
        self.slack, self.conn = slack, conn

    def _record(self, task: Task, *ts: str) -> None:
        """Record posted messages against the task's conversation, preferring its primary one. A task with no
        conversation records nothing."""
        row = self.conn.execute("SELECT id FROM conversations WHERE task_id = ? ORDER BY is_primary DESC, created_at "
                                "LIMIT 1", (task.id,)).fetchone()
        if row is None:
            return
        for one in ts:
            record_message(self.conn, task.slack_channel, one, row["id"])

    async def task_changed(self, task: Task) -> None:
        if not task.slack_channel:
            return
        if task.status_message_ts:
            await self.slack.chat_update(channel=task.slack_channel, ts=task.status_message_ts, text=status_text(task))
        else:
            kwargs = {"channel": task.slack_channel, "text": status_text(task)}
            if task.slack_thread_ts:
                kwargs["thread_ts"] = task.slack_thread_ts
            resp = await self.slack.chat_postMessage(**kwargs)
            task = tasks.set_fields(self.conn, task.id, slack_channel=resp["channel"],
                                    slack_thread_ts=task.slack_thread_ts or resp["ts"], status_message_ts=resp["ts"])
        self._record(task, task.status_message_ts)
        await self._react(task)

    async def _react(self, task: Task) -> None:
        name = REACTIONS.get(task.status)
        key = f"reaction:{task.id}"
        previous = db.get_setting(self.conn, key)
        if not name or name == previous or not task.slack_thread_ts:
            return
        if previous:
            try:
                await self.slack.reactions_remove(channel=task.slack_channel, timestamp=task.slack_thread_ts,
                                                  name=previous)
            except Exception:
                pass  # already removed by someone; not worth failing over
        await self.slack.reactions_add(channel=task.slack_channel, timestamp=task.slack_thread_ts, name=name)
        db.set_setting(self.conn, key, name)

    async def ask(self, task: Task, question: str) -> None:
        if not task.slack_channel:
            return
        posted = await post_markdown(self.slack, channel=task.slack_channel, thread_ts=task.slack_thread_ts,
                                     text=f"Question about {task.id}: {question}\n"
                                          "Reply in this thread and I'll pick it back up.")
        self._record(task, *posted)
