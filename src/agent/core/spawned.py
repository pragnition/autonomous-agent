"""Spawned agents: sessions a session starts that outlive its turn and report back to it.

spawn_agent gives the agent its own conversation, with no Slack thread, and queues its brief as that conversation's
first turn. From there it runs like any other conversation: it carries on after a time or step limit and waits out a
usage limit. Its turns don't depend on the spawning session's turn. When its last turn ends, the outcome and final
message are queued as a message in the spawning conversation. If that conversation is idle, the message starts a
new turn in its Slack thread. If it is running, the message reaches it mid-turn, like any message that arrives
while a turn runs.
"""

from __future__ import annotations

import sqlite3
import uuid

from agent import db, events

MAX_RUNNING = 10        # spawned agents one conversation may have running at once
REPORT_CHARS = 20_000   # the longest report passed back; the rest is cut with a note
BRIEF_CHARS = 300       # how much of the brief the report repeats, so the spawning session knows which one it is

CHILD_NOTE = ("(You are agent {id}, a background agent another session spawned. You have no Slack thread and nobody "
              "watches you work. When you're done, your final message is delivered to the session that spawned you, "
              "so make it a complete, self-contained report of what you found or did. Don't ask questions: decide, "
              "say what you assumed and finish.)\n\n")


def new_id() -> str:
    return f"A-{uuid.uuid4().hex[:8]}"


def running_count(conn: sqlite3.Connection, parent_conversation_id: str) -> int:
    """Agents still running for a conversation: not yet reported, with a turn queued or running. One whose turn was
    cancelled (stop_all, cancel_task) or gave up after the runner died never reports, and doesn't count."""
    return conn.execute(
        "SELECT COUNT(*) FROM spawned_agents s WHERE s.parent_conversation_id = ? AND s.status = 'running' AND EXISTS "
        "(SELECT 1 FROM work_items w WHERE w.conversation_id = s.child_conversation_id AND "
        "w.status IN ('queued', 'claimed', 'delivered'))", (parent_conversation_id,)).fetchone()[0]


def is_spawned(conn: sqlite3.Connection, conversation_id: str | None) -> bool:
    return conversation_id is not None and conn.execute(
        "SELECT 1 FROM spawned_agents WHERE child_conversation_id = ?", (conversation_id,)).fetchone() is not None


def record(conn: sqlite3.Connection, *, agent_id: str, parent_conversation_id: str, child_conversation_id: str,
           spawned_by: str, prompt: str, cwd: str | None, parent: dict) -> None:
    """parent is what the report-back turn needs from the spawning turn: actor, unattended, origin, source and
    requester."""
    conn.execute("INSERT INTO spawned_agents(id, parent_conversation_id, child_conversation_id, spawned_by, actor, "
                 "unattended, origin, source, requester, prompt, cwd, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (agent_id, parent_conversation_id, child_conversation_id, spawned_by, parent.get("actor"),
                  int(bool(parent.get("unattended"))), parent.get("origin") or "", parent.get("source") or "slack",
                  parent.get("requester"), prompt, cwd, db.utcnow()))
    events.append(conn, "agent_spawned", task_id=None, actor=parent.get("actor"),
                  data={"agent": agent_id, "parent": parent_conversation_id, "child": child_conversation_id,
                        "cwd": cwd})


def running_for_child(conn: sqlite3.Connection, child_conversation_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM spawned_agents WHERE child_conversation_id = ? AND status = 'running'",
                        (child_conversation_id,)).fetchone()


def finish(conn: sqlite3.Connection, agent_id: str, status: str, outcome: str | None, text: str) -> bool:
    """Mark a running agent finished; False when it already was (so it reports back once)."""
    cur = conn.execute("UPDATE spawned_agents SET status = ?, outcome = ?, result = ?, finished_at = ? "
                       "WHERE id = ? AND status = 'running'", (status, outcome, text, db.utcnow(), agent_id))
    return cur.rowcount == 1


def report_text(row: sqlite3.Row, status: str, outcome: str, text: str) -> str:
    """The message the spawning session gets when one of its agents finishes."""
    brief = " ".join(row["prompt"].split())
    if len(brief) > BRIEF_CHARS:
        brief = brief[:BRIEF_CHARS] + "…"
    report = text.strip() or "(it ended without a final message)"
    if len(report) > REPORT_CHARS:
        report = report[:REPORT_CHARS] + f"\n[cut: {len(text)} characters in all]"
    how = "finished" if status == "done" else f"failed ({outcome})"
    return (f"<agent-notification>\nAgent {row['id']}, which you spawned with spawn_agent, {how}.\n"
            f"Its brief: {brief}\nIts report:\n{report}\n</agent-notification>\n"
            "Act on it: carry on with the work it was part of, and tell the thread what it found when that matters to "
            "them. Agents you spawned that are still running report back the same way.")
