"""The work queue, the tokens the agent container's runner uses, and the gate that decides when work may start.

One queue holds every conversation's turns, and the runner pulls from it. A conversation never
has two items running at once, so a second Slack message in a busy thread becomes the next turn of the same
session. A runner that dies loses its lease; the item is re-queued once, then marked failed.

Two kinds of token: the long-lived runner token (pull work, fetch bundles) and a session token per work item.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from agent import db
from agent.proxy import ProxyUnavailable

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class TokenInfo:
    kind: str
    work_item_id: str | None


def mint_token(conn: sqlite3.Connection, kind: Literal["runner", "session"], *, work_item_id: str | None = None,
               ttl_s: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO tokens(hash, kind, work_item_id, expires_at) VALUES (?, ?, ?, ?)",
                 (_hash(token), kind, work_item_id, _iso(datetime.now(UTC) + timedelta(seconds=ttl_s))))
    return token


def resolve_token(conn: sqlite3.Connection, token: str, now: datetime | None = None) -> TokenInfo | None:
    row = conn.execute("SELECT kind, work_item_id, expires_at, revoked FROM tokens WHERE hash = ?",
                       (_hash(token),)).fetchone()
    if row is None or row["revoked"] or row["expires_at"] <= _iso(_now(now)):
        return None
    return TokenInfo(row["kind"], row["work_item_id"])


def revoke_for_work_item(conn: sqlite3.Connection, work_item_id: str) -> None:
    conn.execute("UPDATE tokens SET revoked = 1 WHERE work_item_id = ?", (work_item_id,))


@dataclass
class WorkItem:
    id: str
    conversation_id: str | None
    kind: str
    payload: dict
    status: str
    attempts: int
    result: dict | None = None
    created_at: str | None = None


def _item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(row["id"], row["conversation_id"], row["kind"], json.loads(row["payload"]), row["status"],
                    row["attempts"], json.loads(row["result"]) if row["result"] else None, row["created_at"])


# The items claim() may hand out: queued ones, plus claimed ones whose lease has run out (claim()'s own reap
# re-queues those first), whose conversation holds no live claim. Only a live claim blocks a conversation, so a
# dead runner's stale claim never hides the turns queued behind it. Both parameters are the current time.
_CLAIMABLE = ("(w.status = 'queued' OR (w.status = 'claimed' AND w.lease_until < ?)) AND "
              "(w.conversation_id IS NULL OR NOT EXISTS (SELECT 1 FROM work_items o WHERE "
              "o.conversation_id = w.conversation_id AND o.status = 'claimed' AND o.lease_until >= ?))")


class WorkQueue:
    def __init__(self, conn: sqlite3.Connection, lease_s: int = 90, max_attempts: int = 2):
        self.conn, self.lease_s, self.max_attempts = conn, lease_s, max_attempts

    def enqueue(self, conversation_id: str | None, kind: str, payload: dict) -> str:
        item_id, now = f"W-{uuid.uuid4().hex[:12]}", db.utcnow()
        self.conn.execute("INSERT INTO work_items(id, conversation_id, kind, payload, created_at, updated_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (item_id, conversation_id, kind, json.dumps(payload), now, now))
        return item_id

    def get(self, item_id: str) -> WorkItem | None:
        row = self.conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        return _item(row) if row else None

    def _reap(self, now: datetime) -> None:
        expired = self.conn.execute("SELECT id, attempts FROM work_items WHERE status = 'claimed' AND lease_until < ?",
                                    (_iso(now),)).fetchall()
        for row in expired:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= self.max_attempts else "queued"
            self.conn.execute("UPDATE work_items SET status = ?, attempts = ?, lease_until = NULL, updated_at = ? "
                              "WHERE id = ?", (status, attempts, db.utcnow(), row["id"]))
            revoke_for_work_item(self.conn, row["id"])

    def reap(self, now: datetime | None = None) -> None:
        """Re-queue (or fail) items whose runner stopped heartbeating, whoever asks first."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._reap(_now(now))

    def has_claimable(self, now: datetime | None = None) -> bool:
        """Whether claim() could hand out an item right now, without claiming or reaping anything. A runner asks
        the Gate only when there is something to start, so an idle queue never has to call it (and, through it,
        the account proxy) on every poll."""
        now_s = _iso(_now(now))
        return self.conn.execute(f"SELECT 1 FROM work_items w WHERE {_CLAIMABLE} LIMIT 1",
                                 (now_s, now_s)).fetchone() is not None

    def claim(self, now: datetime | None = None) -> WorkItem | None:
        """The oldest claimable item: no other item of its conversation is claimed."""
        now = _now(now)
        now_s = _iso(now)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._reap(now)
            row = self.conn.execute(f"SELECT * FROM work_items w WHERE {_CLAIMABLE} "
                                    "ORDER BY w.created_at, w.rowid LIMIT 1", (now_s, now_s)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE work_items SET status = 'claimed', lease_until = ?, updated_at = ? WHERE id = ?",
                              (_iso(now + timedelta(seconds=self.lease_s)), db.utcnow(), row["id"]))
            return self.get(row["id"])

    def set_payload(self, item_id: str, payload: dict) -> None:
        self.conn.execute("UPDATE work_items SET payload = ?, updated_at = ? WHERE id = ?",
                          (json.dumps(payload), db.utcnow(), item_id))

    def heartbeat(self, item_id: str, now: datetime | None = None) -> bool:
        cur = self.conn.execute("UPDATE work_items SET lease_until = ?, updated_at = ? WHERE id = ? "
                                "AND status = 'claimed'",
                                (_iso(_now(now) + timedelta(seconds=self.lease_s)), db.utcnow(), item_id))
        return cur.rowcount == 1

    def complete(self, item_id: str, result: dict) -> None:
        self.conn.execute("UPDATE work_items SET status = 'done', result = ?, lease_until = NULL, updated_at = ? "
                          "WHERE id = ? AND status IN ('queued', 'claimed')", (json.dumps(result), db.utcnow(), item_id))
        revoke_for_work_item(self.conn, item_id)

    def cancel(self, item_id: str) -> None:
        self.conn.execute("UPDATE work_items SET status = 'cancelled', lease_until = NULL, updated_at = ? "
                          "WHERE id = ? AND status IN ('queued', 'claimed')", (db.utcnow(), item_id))
        revoke_for_work_item(self.conn, item_id)

    def cancel_running(self) -> list[str]:
        """Interrupt what is running; queued messages wait (they can't start while paused) and run after resume."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM work_items WHERE status = 'claimed' ORDER BY created_at, rowid")]
        for item_id in ids:
            self.cancel(item_id)
        return ids

    def claimed_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'claimed'").fetchone()[0]


class Gate:
    """Whether the runner may start another item now."""

    def __init__(self, conn: sqlite3.Connection, config, proxy):
        self.conn, self.config, self.proxy = conn, config, proxy

    async def can_start(self) -> tuple[bool, str]:
        if db.get_setting(self.conn, "paused", "0") == "1":
            return False, "paused"
        queue = WorkQueue(self.conn)
        queue.reap()
        try:
            if not (await self.proxy.usage()).has_capacity():
                return False, "waiting for capacity"
        except ProxyUnavailable:
            return False, "waiting for capacity"
        if queue.claimed_count() >= self.config.limits.max_concurrent_sessions:
            return False, "busy"
        return True, ""
