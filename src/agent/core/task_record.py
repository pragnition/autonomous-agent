"""The task record: what the agent worked on, who asked, when, for how long, and how it went.

The labeler (task_labeler.py) gives each request the agent answered a label: chat, a new task, or the same task as an
earlier one. A task is its `new` request plus every `same` request after it; its row in task_records is rebuilt from
those requests whenever one is labeled, and the agent reads a copy of the records, one file per month, in
/data/tasks. Everything here comes from what core recorded itself: the work items, their payloads and results, and the
session_started event a pull appends.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import db
from agent.core.conversations import FAILED_OUTCOMES
from agent.core.linear_sessions import linear_requester

log = logging.getLogger(__name__)

FMT = "%Y-%m-%dT%H:%M:%SZ"
NO_TEXT = "(no text)"
LINEAR_EVENT = "Linear agent session event:"
ASKED_CHARS, REPLY_CHARS, STEP_CHARS = 2000, 3000, 300
EARLIER_MAX, RECENT_MAX, RECENT_HOURS = 5, 10, 24
# The labeler's version. Raising it relabels the whole history, oldest first, the next time the sweep runs: do so
# whenever the prompt or the rules change what a label means.
LABEL_VERSION = 2
_URL = re.compile(r"https?://[^\s\]|>]+")
_LINKS = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|https://linear\.app/[\w.-]+/issue/[\w-]+(?:/[\w-]+)?")


@dataclass(frozen=True)
class Label:
    label: str               # chat | new | same
    task: str | None         # for same: the task it continues
    title: str | None        # for new
    outcome: str | None
    finished: bool | None
    project: str | None = None   # a configured project's name; None for general work


@dataclass(frozen=True)
class RequestContext:
    asked: str
    reply: str
    errored: bool
    earlier: list[dict]      # this conversation's earlier labeled requests, oldest first
    recent: list[dict]       # the requester's other tasks from the 24 hours before this request


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Finished turns with no label yet, or one from an older labeler, oldest first. Smoke-test conversations are
    never labeled."""
    return conn.execute(
        "SELECT w.* FROM work_items w LEFT JOIN request_labels l ON l.work_item_id = w.id "
        "WHERE w.kind = 'turn' AND w.status IN ('done', 'failed', 'cancelled') "
        "AND COALESCE(w.conversation_id, '') NOT LIKE 'V-smoke%' AND (l.label IS NULL OR l.version < ?) "
        "ORDER BY w.created_at, w.rowid", (LABEL_VERSION,)).fetchall()


def _webhook(text: str) -> dict | None:
    if not text.startswith(LINEAR_EVENT):
        return None
    try:
        body = json.loads(text[len(LINEAR_EVENT):])
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def origin_of(conn: sqlite3.Connection, config, row: sqlite3.Row) -> tuple[str, str | None, str | None]:
    """(source, requester, follows). Turns queued since the task record shipped carry them; older ones are inferred
    from what they do carry."""
    p = json.loads(row["payload"])
    if p.get("source"):
        return p["source"], p.get("requester"), p.get("follows")
    text = p.get("text") or ""
    body = _webhook(text)
    linked = conn.execute("SELECT 1 FROM linear_issue_conversations WHERE conversation_id = ?",
                          (row["conversation_id"],)).fetchone()
    if body is not None or linked:
        return "linear", linear_requester(body, config) if body else p.get("actor"), None
    if (p.get("origin") or "").startswith("[schedule"):
        return "schedule", p.get("actor"), None
    follows = None
    if p.get("continuation"):
        prev = conn.execute("SELECT id FROM work_items WHERE conversation_id = ? AND kind = 'turn' AND "
                            "(created_at < ? OR (created_at = ? AND rowid < ?)) ORDER BY created_at DESC, rowid DESC "
                            "LIMIT 1", (row["conversation_id"], row["created_at"], row["created_at"],
                                        _rowid(conn, row["id"]))).fetchone()
        follows = prev["id"] if prev else None
    return "slack", p.get("actor"), follows


def _rowid(conn: sqlite3.Connection, item_id: str) -> int:
    return conn.execute("SELECT rowid FROM work_items WHERE id = ?", (item_id,)).fetchone()[0]


def asked_text(payload: dict) -> str:
    """What was asked, in words: a Linear webhook becomes its issue and the prompt."""
    text = payload.get("text") or ""
    body = _webhook(text)
    if body is not None:
        session = body.get("agentSession") or {}
        issue = session.get("issue") or {}
        prompt = ((body.get("agentActivity") or {}).get("content") or {}).get("body") \
            or (session.get("comment") or {}).get("body") or body.get("promptContext") or ""
        head = " ".join(p for p in (issue.get("identifier"), issue.get("title")) if p)
        text = f"{head}: {prompt}" if head else prompt
    return text.strip() or NO_TEXT


def _result(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["result"]) if row["result"] else {}
    except ValueError:
        return {}


def project_hints(conn: sqlite3.Connection, row: sqlite3.Row, projects: dict) -> list[str]:
    """Projects core can tell a request is about, strongest first: the conversation's bound project, then a Linear key
    of the project's team or its repo in the request or the reply, or its name in the request. The name in the reply
    doesn't count: the agent mentions its main project in most of what it says."""
    hints = []
    convo = conn.execute("SELECT project FROM conversations WHERE id = ?", (row["conversation_id"],)).fetchone()
    if convo and convo["project"] in projects:
        hints.append(convo["project"])
    asked = asked_text(json.loads(row["payload"]))
    text = f"{asked}\n{_result(row).get('text') or ''}"
    for name, project in projects.items():
        team, repo = getattr(project, "linear_team", ""), getattr(project, "repo", "")
        if (team and re.search(rf"\b{re.escape(team)}-\d+\b", text, re.I)) \
                or (repo and re.search(rf"(?<![\w.-]){re.escape(repo)}(?![\w.-])", text, re.I)) \
                or re.search(rf"\b{re.escape(name)}\b", asked, re.I):
            hints.append(name)
    return list(dict.fromkeys(hints))


def context_for(conn: sqlite3.Connection, row: sqlite3.Row, requester: str | None) -> RequestContext:
    result = _result(row)
    errored = row["status"] != "done" or result.get("outcome") in FAILED_OUTCOMES
    earlier_rows = conn.execute(
        "SELECT l.*, (SELECT n.title FROM request_labels n WHERE n.task_id = l.task_id AND n.label = 'new') AS t "
        "FROM request_labels l WHERE l.conversation_id = ? AND l.label IS NOT NULL AND l.arrived_at <= ? "
        "AND l.work_item_id != ? ORDER BY l.arrived_at DESC, l.work_item_id DESC LIMIT ?",
        (row["conversation_id"], row["created_at"], row["id"], EARLIER_MAX)).fetchall()
    earlier = [{"task": r["task_id"], "label": r["label"], "title": r["t"], "asked": r["asked"][:STEP_CHARS],
                "project": r["project"]} for r in reversed(earlier_rows)]
    recent = []
    if requester:
        since = _iso(_ts(row["created_at"]) - timedelta(hours=RECENT_HOURS))
        seen = {e["task"] for e in earlier if e["task"]}
        for r in conn.execute(
                "SELECT task_id, MAX(arrived_at) AS last FROM request_labels WHERE requester = ? AND task_id IS NOT "
                "NULL AND arrived_at >= ? AND arrived_at <= ? AND work_item_id != ? GROUP BY task_id "
                "ORDER BY last DESC", (requester, since, row["created_at"], row["id"])):
            if r["task_id"] in seen:
                continue
            rec = task_record(conn, r["task_id"])
            if rec:
                recent.append({"task": rec["id"], "title": rec["title"], "outcome": rec["outcome"],
                               "project": rec.get("project")})
            if len(recent) >= RECENT_MAX:
                break
    return RequestContext(asked_text(json.loads(row["payload"]))[:ASKED_CHARS],
                          ((result.get("text") or "").strip() or NO_TEXT)[:REPLY_CHARS], errored, earlier, recent)


def _new_task_id(conn: sqlite3.Connection) -> str:
    top = conn.execute("SELECT MAX(CAST(SUBSTR(task_id, 4) AS INTEGER)) FROM request_labels "
                       "WHERE task_id LIKE 'TK-%'").fetchone()[0]
    return f"TK-{(top or 0) + 1:04d}"


def _save(conn: sqlite3.Connection, row: sqlite3.Row, *, source: str, requester: str | None, follows: str | None,
          **fields) -> None:
    payload = json.loads(row["payload"])
    links = _URL.findall(payload.get("origin") or "")
    started = conn.execute("SELECT MAX(ts) FROM events WHERE type = 'session_started' "
                           "AND json_extract(data, '$.work_item') = ?", (row["id"],)).fetchone()[0]
    base = {"conversation_id": row["conversation_id"], "source": source, "requester": requester, "follows": follows,
            "asked": asked_text(payload)[:STEP_CHARS], "link": links[-1] if links else None,
            "arrived_at": row["created_at"], "started_at": started, "answered_at": row["updated_at"], **fields}
    cols = ", ".join(base)
    conn.execute(f"INSERT INTO request_labels(work_item_id, {cols}) VALUES (?, {', '.join('?' * len(base))}) "
                 f"ON CONFLICT(work_item_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in base)}",
                 (row["id"], *base.values()))


def count_try(conn: sqlite3.Connection, row: sqlite3.Row, *, source: str, requester: str | None,
              follows: str | None) -> int:
    """One more unparsable answer for this request; how many it has had."""
    tries = conn.execute("SELECT tries FROM request_labels WHERE work_item_id = ?", (row["id"],)).fetchone()
    tries = (tries[0] if tries else 0) + 1
    _save(conn, row, source=source, requester=requester, follows=follows, tries=tries)
    return tries


def apply_label(conn: sqlite3.Connection, row: sqlite3.Row, label: Label, *, source: str, requester: str | None,
                follows: str | None) -> str | None:
    """Save the request's label and rebuild its task (and the task it was in before, when relabeled); the task's id,
    or None for chat. A request relabeled `new` keeps the task id it had."""
    before = conn.execute("SELECT label, task_id FROM request_labels WHERE work_item_id = ?", (row["id"],)).fetchone()
    if label.label == "new":
        task_id = before["task_id"] if before and before["label"] == "new" and before["task_id"] else _new_task_id(conn)
    else:
        task_id = label.task if label.label == "same" else None
    is_task = task_id is not None
    _save(conn, row, source=source, requester=requester, follows=follows, label=label.label, task_id=task_id,
          title=label.title if label.label == "new" else None, outcome=label.outcome if is_task else None,
          finished=None if not is_task or label.finished is None else int(label.finished),
          project=label.project if is_task else None, labeled_at=db.utcnow(), version=LABEL_VERSION, tries=0)
    if task_id:
        rebuild_task(conn, task_id)
    if before and before["task_id"] and before["task_id"] != task_id:
        rebuild_task(conn, before["task_id"])
    return task_id


def work_seconds(intervals: list[tuple[str, str]]) -> int:
    """The seconds covered by the union of [start, end) intervals: overlapping requests count once."""
    spans = sorted((_ts(a), _ts(b)) for a, b in intervals if a and b and b > a)
    total, cur = 0.0, None
    for start, end in spans:
        if cur and start <= cur[1]:
            cur = (cur[0], max(cur[1], end))
            continue
        if cur:
            total += (cur[1] - cur[0]).total_seconds()
        cur = (start, end)
    if cur:
        total += (cur[1] - cur[0]).total_seconds()
    return int(total)


def _distinct(values) -> list:
    return list(dict.fromkeys(v for v in values if v))


def rebuild_task(conn: sqlite3.Connection, task_id: str) -> None:
    reqs = conn.execute(
        "SELECT l.*, w.result FROM request_labels l LEFT JOIN work_items w ON w.id = l.work_item_id "
        "WHERE l.task_id = ? AND l.label IN ('new', 'same') ORDER BY l.arrived_at, l.work_item_id",
        (task_id,)).fetchall()
    if not reqs:
        conn.execute("DELETE FROM task_records WHERE id = ?", (task_id,))
        return
    first = next((r for r in reqs if r["label"] == "new"), reqs[0])
    # where the task stands: the latest request with a verdict (one labeled without one says nothing new)
    latest = next((r for r in reversed(reqs) if r["finished"] is not None or r["outcome"] is not None), reqs[-1])
    answered = [r["answered_at"] for r in reqs if r["answered_at"]]
    finished = None if latest["finished"] is None else bool(latest["finished"])
    project = next((r["project"] for r in reqs if r["project"]), None)
    record = {
        "id": task_id, "title": first["title"] or "Untitled", "source": first["source"], "project": project,
        "requested_by": first["requester"],
        "followed_up_by": [p for p in _distinct(r["requester"] for r in reqs) if p != first["requester"]],
        "where": _distinct(r["link"] for r in reqs),
        "started_at": reqs[0]["arrived_at"], "ended_at": max(answered) if answered else None,
        # from pickup to answer; a request never picked up (cancelled while queued, so no result) adds nothing
        "work_time_s": work_seconds([(r["started_at"] or (r["arrived_at"] if r["result"] else None), r["answered_at"])
                                     for r in reqs]),
        "requests": len(reqs), "finished": finished, "outcome": latest["outcome"],
        "links": _distinct(link for r in reqs
                           for link in _LINKS.findall(json.loads(r["result"]).get("text") or "" if r["result"] else "")),
        "steps": [{"at": r["arrived_at"], "by": r["requester"], "asked": r["asked"], "outcome": r["outcome"],
                   "finished": None if r["finished"] is None else bool(r["finished"]), "work_item": r["work_item_id"],
                   "conversation": r["conversation_id"]}
                  for r in reqs]}
    conn.execute("INSERT INTO task_records(id, source, project, requested_by, started_at, ended_at, work_time_s, "
                 "finished, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                 "source = excluded.source, project = excluded.project, requested_by = excluded.requested_by, "
                 "started_at = excluded.started_at, ended_at = excluded.ended_at, work_time_s = excluded.work_time_s, "
                 "finished = excluded.finished, record = excluded.record",
                 (task_id, record["source"], project, record["requested_by"], record["started_at"], record["ended_at"],
                  record["work_time_s"], None if finished is None else int(finished), json.dumps(record)))


def task_record(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute("SELECT record FROM task_records WHERE id = ?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def month_of(started_at: str, tz: str) -> str:
    return _ts(started_at).astimezone(ZoneInfo(tz)).strftime("%Y-%m")


def _month_bounds(month: str, tz: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    zone = ZoneInfo(tz)
    start = datetime(year, mon, 1, tzinfo=zone)
    end = datetime(year + mon // 12, mon % 12 + 1, 1, tzinfo=zone)
    return _iso(start), _iso(end)


def export_months(conn: sqlite3.Connection, tasks_dir: Path, months: set[str], tz: str) -> None:
    """Rewrite each month's file: its tasks by when they started (in the team's timezone), newest first. A reader
    never sees half a file: each is written beside its place and renamed over it."""
    for month in sorted(months):
        lo, hi = _month_bounds(month, tz)
        rows = conn.execute("SELECT record FROM task_records WHERE started_at >= ? AND started_at < ? "
                            "ORDER BY started_at DESC, id DESC", (lo, hi)).fetchall()
        path = Path(tasks_dir) / f"{month}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not rows:
                path.unlink(missing_ok=True)
                continue
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(r[0] + "\n" for r in rows))
            os.replace(tmp, path)
        except OSError:
            log.exception("couldn't write the task file for %s", month)


def export_all(conn: sqlite3.Connection, tasks_dir: Path, tz: str) -> None:
    """Every month's file, for a start: the files catch up with whatever was labeled before."""
    months = {month_of(r[0], tz) for r in conn.execute("SELECT started_at FROM task_records")}
    export_months(conn, tasks_dir, months, tz)
