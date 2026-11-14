"""Linear issues handed to the agent, for the KPIs: recorded when the webhook arrives, then kept current by asking
Linear for each open issue's state. Every time comes from Linear, never from when core happened to look."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

FMT = "%Y-%m-%dT%H:%M:%SZ"
REOPEN_WINDOW = timedelta(days=7)   # closed issues are still checked this long, so a reopen is seen


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


class LinearIssuePoller:
    def __init__(self, conn: sqlite3.Connection, client, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.conn, self.client, self.clock = conn, client, clock

    async def poll(self) -> int:
        recent = (self.clock() - REOPEN_WINDOW).strftime(FMT)
        rows = self.conn.execute(
            "SELECT issue_id FROM linear_issues WHERE (completed_at IS NULL AND canceled_at IS NULL) "
            "OR completed_at >= ? OR canceled_at >= ? ORDER BY handed_at", (recent, recent)).fetchall()
        for r in rows:
            try:
                state = await self.client.issue_state(r["issue_id"])
            except Exception:
                log.exception("couldn't read Linear issue %s", r["issue_id"])
                continue
            kind = state.get("state_type")
            self.conn.execute(
                "UPDATE linear_issues SET state_type = ?, completed_at = ?, canceled_at = ?, checked_at = ? "
                "WHERE issue_id = ?",
                (kind, _iso(state.get("completed_at")) if kind == "completed" else None,
                 _iso(state.get("canceled_at")) if kind == "canceled" else None,
                 self.clock().strftime(FMT), r["issue_id"]))
        return len(rows)
