"""Spawned agents: sessions a session starts that outlive its turn and report back to it.

spawn_agent gives the agent its own conversation, with no Slack thread, and queues its brief as that conversation's
first turn. From there it runs like any other conversation: it waits out a usage limit and can park itself. Its turns
don't depend on the spawning session's turn. Once it is finished (its turn ended with nothing queued, parked or
waiting in its conversation), the outcome and final message are queued as a message in the spawning conversation.
If that conversation is idle, the message starts a new turn in its Slack thread. If it is running, the message
reaches it mid-turn, like any message that arrives while a turn runs. One that fails reports the failure and is not
retried: the spawning session decides. A conversation may have MAX_RUNNING running at once, and all conversations
together MAX_RUNNING_ALL.
"""

from __future__ import annotations

import sqlite3
import uuid

from agent import db, events

MAX_RUNNING = 3         # spawned agents one conversation may have running at once
MAX_RUNNING_ALL = 4     # spawned agents running at once across every conversation
REPORT_CHARS = 20_000   # the longest report passed back; the rest is cut with a note
BRIEF_CHARS = 300       # how much of the brief the report repeats, so the spawning session knows which one it is

CHILD_NOTE = ("(You are agent {id}, an agent another session spawned with spawn_agent. You have no Slack thread and "
              "nobody watches you work. When you're done, your final message is delivered to the session that spawned "
              "you, so make it a complete, self-contained report of what you found or did. Don't ask questions: "
              "decide, say what you assumed and finish.)\n\n")


def new_id() -> str:
    return f"A-{uuid.uuid4().hex[:8]}"


# Still to run in a conversation: a turn queued, running or delivered into one, a park not yet carried on
# (core/parking.py), or a turn a usage limit cut off that hasn't been carried on (core/usage_watch.py)
_MORE_TO_COME = ("EXISTS (SELECT 1 FROM work_items w WHERE w.conversation_id = {c} AND "
                 "w.status IN ('queued', 'claimed', 'delivered')) OR "
                 "EXISTS (SELECT 1 FROM parks p WHERE p.conversation_id = {c} AND p.ended_at IS NULL) OR "
                 "EXISTS (SELECT 1 FROM limit_waits l WHERE l.conversation_id = {c} AND l.resumed_at IS NULL)")


def more_to_come(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """Whether anything is still to run in the conversation: then a spawned agent's turn that just ended isn't its
    last."""
    return conn.execute("SELECT " + _MORE_TO_COME.format(c="?"),
                        (conversation_id,) * 3).fetchone()[0] == 1


# A spawned agent still running: not yet reported, with something still to run in its conversation. One whose turn
# was cancelled (stop_all, cancel_task) or gave up after the runner died never reports, and doesn't count.
_RUNNING = "s.status = 'running' AND (" + _MORE_TO_COME.format(c="s.child_conversation_id") + ")"


def running_count(conn: sqlite3.Connection, parent_conversation_id: str) -> int:
    """The agents a conversation spawned that are still running, a parked one or one waiting out a usage limit too."""
    return conn.execute(f"SELECT COUNT(*) FROM spawned_agents s WHERE s.parent_conversation_id = ? AND {_RUNNING}",
                        (parent_conversation_id,)).fetchone()[0]


def running_total(conn: sqlite3.Connection) -> int:
    """Every conversation's spawned agents still running."""
    return conn.execute(f"SELECT COUNT(*) FROM spawned_agents s WHERE {_RUNNING}").fetchone()[0]


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
    how = "finished" if status == "done" else \
        f"failed ({outcome}). It won't be retried: spawn it again if the work still needs it"
    return (f"<agent-notification>\nAgent {row['id']}, which you spawned with spawn_agent, {how}.\n"
            f"Its brief: {brief}\nIts report:\n{report}\n</agent-notification>\n"
            "Act on it: carry on with the work it was part of, and tell the thread what it found when that matters to "
            "them. Agents you spawned that are still running report back the same way.")
