"""The append-only event log: the source of truth for metrics."""

from __future__ import annotations

import json
import sqlite3

from agent.db import utcnow


def append(conn: sqlite3.Connection, type: str, *, task_id: str | None, actor: str | None,
           data: dict | None = None) -> int:
    cur = conn.execute("INSERT INTO events(ts, task_id, type, actor, data) VALUES (?, ?, ?, ?, ?)",
                       (utcnow(), task_id, type, actor, json.dumps(data) if data is not None else None))
    return cur.lastrowid
