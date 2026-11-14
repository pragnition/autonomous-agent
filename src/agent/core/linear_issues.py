"""Linear issues handed to the agent, for the KPIs: recorded when the webhook arrives, then kept current by asking
Linear for each open issue's state. Every time comes from Linear, never from when core happened to look."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(value: str | None) -> str | None:
    """Linear's and GitHub's ISO times (milliseconds, Z or an offset) in the database's format."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).strftime(FMT)


def record_issue(conn: sqlite3.Connection, session: dict, conversation_id: str) -> None:
    """One row per issue however many agent sessions it gets; the first hand-over is when the ticket started."""
    issue = session.get("issue") or {}
    if not issue.get("id") or not issue.get("identifier"):
        return
    handed = _iso(session.get("createdAt")) or datetime.now(UTC).strftime(FMT)
    conn.execute("INSERT INTO linear_issues(issue_id, identifier, title, url, handed_at) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(issue_id) DO UPDATE SET identifier = excluded.identifier, title = excluded.title, "
                 "url = excluded.url, handed_at = MIN(handed_at, excluded.handed_at)",
                 (issue["id"], issue["identifier"], issue.get("title"), issue.get("url"), handed))
    conn.execute("INSERT OR IGNORE INTO linear_issue_conversations(issue_id, conversation_id) VALUES (?, ?)",
                 (issue["id"], conversation_id))
