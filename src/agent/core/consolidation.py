"""Memory consolidation (v1 §6.15), run as a chore on the one agent queue (spec v2 §5).

When a task ends, or a conversation has been quiet for 24 h with notes jotted down, core queues a chore whose
session returns JSON; core validates it and writes the notes. A failed chore leaves the task unconsolidated so
the next sweep tries again, up to three times.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import events, tasks
from agent.core.agent_tools import conversation_scopes
from agent.core.sessions import AGENT_QUEUE, WorkItem, WorkQueue
from agent.memory.consolidate import (CONSOLIDATION_SCHEMA, ConsolidationError, apply_consolidation,
                                      consolidation_prompt)
from agent.memory.store import MemoryStore
from agent.tasks import TERMINAL

THREAD_IDLE = timedelta(hours=24)
MAX_FAILURES = 3
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _json_from(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


class ConsolidationService:
    def __init__(self, conn: sqlite3.Connection, memory: MemoryStore, queue: WorkQueue, payloads, *,
                 secret_values: list[str], mirror_for: Callable[[str], Path | None] = lambda project: None):
        self.conn, self.memory, self.queue, self.payloads = conn, memory, queue, payloads
        self.secret_values, self.mirror_for = secret_values, mirror_for

    # bookkeeping

    def _open_chore(self, key: str, value: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM work_items WHERE kind = 'chore' AND status IN ('queued', 'claimed') "
            f"AND json_extract(payload, '$.consolidate.{key}') = ? LIMIT 1", (value,)).fetchone() is not None

    def _failures(self, key: str, value: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'consolidation_failed' "
                                 f"AND json_extract(data, '$.{key}') = ?", (value,)).fetchone()[0]

    def _inbox_for_items(self, item_ids: list[str]) -> list[str]:
        return [e["text"] for item_id in item_ids for e in self.memory.inbox_for(item_id)]

    def _enqueue(self, prompt: str, ref: dict) -> None:
        payload = self.payloads.chore(prompt, CONSOLIDATION_SCHEMA)
        payload["consolidate"] = ref
        self.queue.enqueue(None, AGENT_QUEUE, "chore", payload)

    # tasks

    def _pending_tasks(self) -> list[str]:
        marks = ",".join("?" * len(TERMINAL))
        rows = self.conn.execute(
            f"SELECT id FROM tasks WHERE status IN ({marks}) AND id NOT IN (SELECT task_id FROM events WHERE "
            "type = 'consolidated' AND task_id IS NOT NULL) ORDER BY id", [s.value for s in TERMINAL]).fetchall()
        return [r["id"] for r in rows if not self._open_chore("task_id", r["id"])
                and self._failures("task_id", r["id"]) < MAX_FAILURES]

    def sweep(self) -> list[str]:
        queued = []
        for task_id in self._pending_tasks():
            task = tasks.get_task(self.conn, task_id)
            items = [r["id"] for r in self.conn.execute(
                "SELECT w.id FROM work_items w JOIN conversations c ON c.id = w.conversation_id WHERE c.task_id = ?",
                (task_id,))]
            scope = f"projects/{task.project}"
            existing = [f"- [{n.id}] {n.summary}" for n in self.memory.candidates([scope])][:60]
            prompt = consolidation_prompt(task_summary=f"{task.id}: {task.title}\n{task.description}",
                                          outcome=task.status.value, human_messages=[], review_comments=[],
                                          inbox=self._inbox_for_items(items), existing_index=existing)
            self._enqueue(prompt, {"task_id": task_id})
            queued.append(task_id)
        return queued

    # quiet conversations

    def sweep_threads(self, now: datetime) -> int:
        cutoff = (now - THREAD_IDLE).strftime(_FMT)
        queued = 0
        for convo in self.conn.execute("SELECT * FROM conversations WHERE consolidated_at IS NULL AND "
                                       "last_active_at < ?", (cutoff,)).fetchall():
            if self._open_chore("conversation_id", convo["id"]):
                continue
            items = [r["id"] for r in self.conn.execute("SELECT id FROM work_items WHERE conversation_id = ?",
                                                        (convo["id"],))]
            inbox = self._inbox_for_items(items)
            if not inbox or self._failures("conversation_id", convo["id"]) >= MAX_FAILURES:
                self.conn.execute("UPDATE conversations SET consolidated_at = ? WHERE id = ?",
                                  (now.strftime(_FMT), convo["id"]))
                continue
            allowed = self._thread_scopes(convo)
            existing = [f"- [{n.id}] {n.summary}" for n in self.memory.candidates(allowed)][:60]
            prompt = consolidation_prompt(task_summary=f"Slack conversation {convo['channel']}/{convo['thread_ts']}",
                                          outcome="conversation ended", human_messages=[], review_comments=[],
                                          inbox=inbox, existing_index=existing)
            self._enqueue(prompt, {"conversation_id": convo["id"]})
            queued += 1
        return queued

    def _thread_scopes(self, convo) -> list[str]:
        own = conversation_scopes(convo["channel"], None, None, {})[0]
        return [own, *([f"projects/{convo['project']}"] if convo["project"] else [])]

    # results

    async def on_result(self, item: WorkItem | None, result: dict) -> None:
        ref = (item.payload.get("consolidate") if item else None) or {}
        if not ref:
            return
        try:
            output = result.get("structured") or _json_from(result.get("text") or "")
            if "task_id" in ref:
                self._apply_task(ref["task_id"], output, item.id)
            else:
                self._apply_thread(ref["conversation_id"], output, item.id)
        except (ConsolidationError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            events.append(self.conn, "consolidation_failed", task_id=ref.get("task_id"), actor="memory",
                          data={**ref, "work_item": item.id, "error": str(e)[:300]})

    def _apply_task(self, task_id: str, output: dict, item_id: str) -> None:
        task = tasks.get_task(self.conn, task_id)
        notes = apply_consolidation(
            self.memory, output, project=task.project, task_id=task.id, run_id=item_id,
            sources=[task.id, *([f"pr:{task.pr_number}"] if task.pr_number else [])],
            untrusted=task.source == "linear", allowed_scopes=[f"projects/{task.project}"],
            secret_values=self.secret_values, repo_root=self.mirror_for(task.project))
        events.append(self.conn, "consolidated", task_id=task.id, actor="memory",
                      data={"work_item": item_id, "notes": [n.id for n in notes]})

    def _apply_thread(self, conversation_id: str, output: dict, item_id: str) -> None:
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        allowed = self._thread_scopes(convo)
        apply_consolidation(self.memory, output, project=convo["project"] or "none", task_id=None, run_id=item_id,
                            sources=[f"slack:{convo['channel']}/{convo['thread_ts']}"], untrusted=False,
                            allowed_scopes=allowed, secret_values=self.secret_values,
                            repo_root=self.mirror_for(convo["project"]) if convo["project"] else None,
                            episode_scope=allowed[0])
        self.conn.execute("UPDATE conversations SET consolidated_at = ? WHERE id = ?",
                          (datetime.now(UTC).strftime(_FMT), conversation_id))
