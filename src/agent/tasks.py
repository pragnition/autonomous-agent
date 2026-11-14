"""Tasks and their lifecycle. Every change is one transaction that also appends an event."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from enum import StrEnum

from agent import events
from agent.db import utcnow


class Status(StrEnum):
    queued = "queued"
    working = "working"
    needs_input = "needs_input"
    ready_for_review = "ready_for_review"
    done = "done"
    merged = "merged"
    closed = "closed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL = {Status.done, Status.merged, Status.closed, Status.cancelled}

# The agent decides the work; these transitions only record where a task stands
_LEGAL: dict[Status, set[Status]] = {
    Status.queued: {Status.working},
    Status.working: {Status.needs_input, Status.ready_for_review, Status.done, Status.failed, Status.queued},
    Status.needs_input: {Status.working, Status.queued},
    Status.ready_for_review: {Status.merged, Status.closed, Status.working},
    Status.failed: {Status.queued},
}


class IllegalTransition(Exception):
    pass


@dataclass
class Task:
    id: str
    project: str
    title: str
    description: str
    kind: str
    source: str
    source_ref: str | None
    requester: str | None
    owner: str | None
    status: Status
    branch: str | None
    pr_number: int | None
    pr_url: str | None
    linear_issue_id: str | None
    slack_channel: str | None
    slack_thread_ts: str | None
    status_message_ts: str | None
    created_at: str
    updated_at: str


_COLUMNS = [f.name for f in fields(Task)]


def _row_to_task(row: sqlite3.Row) -> Task:
    data = {k: row[k] for k in _COLUMNS}
    data["status"] = Status(data["status"])
    return Task(**data)


def next_task_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) AS n FROM tasks").fetchone()
    return f"T-{(row['n'] or 0) + 1:04d}"


def create_task(conn: sqlite3.Connection, *, project: str, title: str, description: str, kind: str,
                source: str, source_ref: str | None, requester: str | None, owner: str | None,
                slack_channel: str | None = None, slack_thread_ts: str | None = None,
                linear_issue_id: str | None = None) -> Task:
    now = utcnow()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        task_id = next_task_id(conn)
        conn.execute(
            "INSERT INTO tasks(id, project, title, description, kind, source, source_ref, requester, owner, "
            "status, linear_issue_id, slack_channel, slack_thread_ts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project, title, description, kind, source, source_ref, requester, owner,
             Status.queued.value, linear_issue_id, slack_channel, slack_thread_ts, now, now))
        events.append(conn, "task_created", task_id=task_id, actor=requester,
                      data={"project": project, "kind": kind, "source": source})
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def transition(conn: sqlite3.Connection, task_id: str, to: Status, *, actor: str,
               data: dict | None = None) -> Task:
    to = Status(to)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        task = get_task(conn, task_id)
        if task is None:
            raise IllegalTransition(f"{task_id}: no such task")
        allowed = _LEGAL.get(task.status, set())
        if to == Status.cancelled and task.status not in TERMINAL:
            allowed = allowed | {Status.cancelled}
        if to not in allowed:
            raise IllegalTransition(f"{task_id}: {task.status} -> {to} is not allowed")
        conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (to.value, utcnow(), task_id))
        events.append(conn, "status_changed", task_id=task_id, actor=actor,
                      data={"from": task.status.value, "to": to.value, **(data or {})})
    return get_task(conn, task_id)


_PROTECTED = {"id", "status", "created_at", "updated_at"}


def set_fields(conn: sqlite3.Connection, task_id: str, **values) -> Task:
    """Update plain fields. Status only ever changes through transition()."""
    bad = sorted(k for k in values if k not in _COLUMNS or k in _PROTECTED)
    if bad:
        raise ValueError(f"cannot set {bad}")
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", (*values.values(), utcnow(), task_id))
    return get_task(conn, task_id)


def list_tasks(conn: sqlite3.Connection, *, status: Status | None = None, project: str | None = None) -> list[Task]:
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if status is not None:
        sql, args = sql + " AND status = ?", [*args, Status(status).value]
    if project is not None:
        sql, args = sql + " AND project = ?", [*args, project]
    return [_row_to_task(r) for r in conn.execute(sql + " ORDER BY id", args)]
