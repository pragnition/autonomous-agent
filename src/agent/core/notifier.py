"""Task status in Slack: one status message per task, edited in place, plus a status reaction on the thread root."""

from __future__ import annotations

import sqlite3

from agent import db, tasks
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
        await self.slack.chat_postMessage(channel=task.slack_channel, thread_ts=task.slack_thread_ts,
                                          text=f"Question about {task.id}: {question}\n"
                                               "Reply in this thread and I'll pick it back up.")
