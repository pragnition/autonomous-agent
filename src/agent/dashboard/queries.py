"""The dashboard's read models over agent.db: sessions (work items with their conversation, task and run), the event
log, today's counts and the KPIs with daily series.

A work item's payload carries system prompts and test secrets: only its `text` and `actor` are ever read here.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent import db
from agent.core.kpis import FMT, compute_kpis

STATES = {"queued": "queued", "running": "claimed", "done": "done", "failed": "failed", "cancelled": "cancelled"}
TERMINAL = {"done", "failed", "cancelled"}
TITLE_CHARS = 120
LINEAR_PREFIX = "Linear agent session event:"

_SOURCE = """CASE
    WHEN t.source = 'linear' OR EXISTS (SELECT 1 FROM settings s WHERE s.key LIKE 'linear_session:%'
                                        AND s.value = w.conversation_id) THEN 'linear'
    WHEN c.is_primary = 0 THEN 'handover'
    WHEN c.channel IS NOT NULL THEN 'slack'
    ELSE 'other' END"""

_SELECT = f"""SELECT w.id, w.kind, w.status, w.conversation_id, w.created_at, w.updated_at,
    json_extract(w.payload, '$.text') AS text, json_extract(w.payload, '$.actor') AS actor,
    COALESCE(c.project, t.project) AS project, c.channel, c.thread_ts,
    t.id AS task_id, t.title AS task_title, t.requester AS task_requester, t.pr_url,
    r.id AS run_id, r.account_code, r.turns, r.input_tokens, r.output_tokens, r.cache_read_tokens,
    r.cache_write_tokens, r.outcome, r.error, r.ended_at,
    {_SOURCE} AS source
FROM work_items w
LEFT JOIN conversations c ON c.id = w.conversation_id
LEFT JOIN tasks t ON t.id = c.task_id
LEFT JOIN runs r ON r.id = w.id"""


@dataclass(frozen=True)
class SessionFilter:
    state: str | None = None
    project: str | None = None
    source: str | None = None
    account: str | None = None
    q: str | None = None
    limit: int = 50
    offset: int = 0


def linear_issue(text: str | None) -> dict | None:
    """The issue in a Linear agent session event a turn was started with: {identifier, title, url}."""
    if not text or not text.startswith(LINEAR_PREFIX):
        return None
    try:
        issue = json.loads(text[len(LINEAR_PREFIX):])["agentSession"]["issue"]
    except (ValueError, KeyError, TypeError):
        return None
    return issue if isinstance(issue, dict) else None


def _title(r: sqlite3.Row, issue: dict | None) -> str:
    if r["task_title"]:
        return r["task_title"]
    if issue and issue.get("identifier"):
        return f"{issue['identifier']} — {issue.get('title') or ''}".strip(" —")
    for line in (r["text"] or "").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 1] + "…"
    return r["kind"]


def _state(status: str) -> str:
    return "running" if status == "claimed" else status


def _row(r: sqlite3.Row, started_at: str | None) -> dict:
    issue = linear_issue(r["text"])
    state = _state(r["status"])
    has_run = r["run_id"] is not None
    return {
        "id": r["id"], "kind": r["kind"], "state": state, "project": r["project"],
        "conversation_id": r["conversation_id"], "task_id": r["task_id"], "title": _title(r, issue),
        "source": r["source"], "requester": r["task_requester"] or r["actor"],
        "created_at": r["created_at"], "started_at": started_at,
        "ended_at": r["ended_at"] if has_run else (r["updated_at"] if state in TERMINAL else None),
        "account": r["account_code"], "turns": r["turns"] if has_run else None,
        "tokens": {"in": r["input_tokens"], "out": r["output_tokens"], "cache_read": r["cache_read_tokens"],
                   "cache_write": r["cache_write_tokens"]} if has_run else None,
        "outcome": r["outcome"], "error": r["error"],
        "live": False,
        "links": {"slack": None, "linear": issue.get("url") if issue else None, "pr": r["pr_url"]},
        # private to the API layer, which removes them: where to ask Slack for the thread's permalink
        "_channel": r["channel"], "_thread_ts": r["thread_ts"],
    }


def _started(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT json_extract(data, '$.work_item') AS item, MIN(ts) AS ts FROM events "
        f"WHERE type = 'session_started' AND json_extract(data, '$.work_item') IN ({marks}) "
        f"GROUP BY item", ids)
    return {r["item"]: r["ts"] for r in rows}


def list_sessions(conn: sqlite3.Connection, f: SessionFilter) -> tuple[list[dict], int]:
    where, args = [], []
    if f.state:
        where.append("w.status = ?")
        args.append(STATES.get(f.state, f.state))
    if f.project:
        where.append("COALESCE(c.project, t.project) = ?")
        args.append(f.project)
    if f.source:
        where.append(f"{_SOURCE} = ?")
        args.append(f.source)
    if f.account:
        where.append("r.account_code = ?")
        args.append(f.account)
    if f.q:
        where.append("(t.title LIKE ? OR json_extract(w.payload, '$.text') LIKE ?)")
        args += [f"%{f.q}%"] * 2
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM ({_SELECT}{clause})", args).fetchone()[0]
    rows = conn.execute(f"{_SELECT}{clause} ORDER BY w.created_at DESC, w.rowid DESC LIMIT ? OFFSET ?",
                        [*args, f.limit, f.offset]).fetchall()
    started = _started(conn, [r["id"] for r in rows])
    return [_row(r, started.get(r["id"])) for r in rows], total


def get_session(conn: sqlite3.Connection, item_id: str) -> dict | None:
    r = conn.execute(f"{_SELECT} WHERE w.id = ?", (item_id,)).fetchone()
    return _row(r, _started(conn, [item_id]).get(item_id)) if r else None


def _local_midnight_utc(now: datetime, tz: str) -> str:
    local = now.astimezone(ZoneInfo(tz))
    return datetime.combine(local.date(), time(), ZoneInfo(tz)).astimezone(UTC).strftime(FMT)


def overview_counts(conn: sqlite3.Connection, now: datetime, tz: str) -> dict:
    since = _local_midnight_utc(now, tz)

    def count(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    return {"running": count("SELECT COUNT(*) FROM work_items WHERE status = 'claimed'"),
            "queued": count("SELECT COUNT(*) FROM work_items WHERE status = 'queued'"),
            "done_today": count("SELECT COUNT(*) FROM work_items WHERE status = 'done' AND updated_at >= ?", since),
            "failed_today": count("SELECT COUNT(*) FROM work_items WHERE status = 'failed' AND updated_at >= ?",
                                  since)}


def paused_by(conn: sqlite3.Connection) -> dict | None:
    if db.get_setting(conn, "paused", "0") != "1":
        return None
    row = conn.execute("SELECT actor, ts FROM events WHERE type = 'stop_all' ORDER BY id DESC LIMIT 1").fetchone()
    return {"actor": row["actor"], "at": row["ts"]} if row else {"actor": None, "at": None}


SESSION_SOURCES = {"slack": "slack", "linear": "linear", "handover": "agent", "other": "other"}
SYSTEM_ACTORS = {"local", "agent", "system"}


def _event_sources(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[tuple[str, str | None]]:
    """Where each event came from (slack, linear, dashboard, command line, agent, other) and, for
    Slack and the dashboard, who: a person key or tailscale:<login>."""
    datas = [d if isinstance(d := _json(r["data"]), dict) else {} for r in rows]
    items = sorted({d["work_item"] for d in datas if isinstance(d.get("work_item"), str)})
    sessions = {}
    if items:
        marks = ",".join("?" * len(items))
        sessions = {r["id"]: (r["source"], r["task_requester"] or r["actor"])
                    for r in conn.execute(f"{_SELECT} WHERE w.id IN ({marks})", items)}
    task_ids = sorted({r["task_id"] for r in rows if r["task_id"]})
    task_rows = {}
    if task_ids:
        marks = ",".join("?" * len(task_ids))
        task_rows = {r["id"]: (r["source"], r["requester"])
                     for r in conn.execute(f"SELECT id, source, requester FROM tasks WHERE id IN ({marks})", task_ids)}
    out = []
    for r, d in zip(rows, datas):
        actor = r["actor"]
        if d.get("via") == "dashboard":
            out.append(("dashboard", actor))
        elif actor == "local":
            out.append(("command line", None))
        elif d.get("work_item") in sessions:
            source, requester = sessions[d["work_item"]]
            source = SESSION_SOURCES.get(source, "other")
            out.append((source, requester if source == "slack" else None))
        elif r["task_id"] in task_rows:
            source, requester = task_rows[r["task_id"]]
            source = source if source in ("slack", "linear") else "other"
            out.append((source, requester if source == "slack" else None))
        elif actor and actor not in SYSTEM_ACTORS and not actor.startswith("tailscale:"):
            out.append(("slack", actor))   # a person's request to the agent, made in a Slack conversation
        else:
            out.append(("other", None))
    return out


def list_events(conn: sqlite3.Connection, type: str | None, limit: int, before: int | None) -> dict:
    where, args = [], []
    if type:
        where.append("type = ?")
        args.append(type)
    if before is not None:
        where.append("id < ?")
        args.append(before)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT id, ts, type, actor, task_id, data FROM events{clause} ORDER BY id DESC LIMIT ?",
                        [*args, limit]).fetchall()
    types = [r["type"] for r in conn.execute("SELECT DISTINCT type FROM events ORDER BY type")]
    return {"rows": [{"id": r["id"], "ts": r["ts"], "type": r["type"], "source": source, "sender": sender}
                     for r, (source, sender) in zip(rows, _event_sources(conn, rows))], "types": types}


def _json(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def _kpis_dict(k) -> dict:
    out = {}
    for f in dataclasses.fields(k):
        value = getattr(k, f.name)
        if isinstance(value, datetime):
            value = value.astimezone(UTC).strftime(FMT)
        elif dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        out[f.name] = value
    out["merge_rate"] = k.merge_rate
    return out


def kpi_view(conn: sqlite3.Connection, days: int, project: str | None, now: datetime, tz: str) -> dict:
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    dates = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
    lo = datetime.combine(dates[0], time(), zone).astimezone(UTC).strftime(FMT)
    hi = now.astimezone(UTC).strftime(FMT)
    daily = {d.isoformat(): {"date": d.isoformat(), "tasks": 0, "prs": 0, "merged": 0} for d in dates}
    by_project = " AND t.project = ?" if project else ""
    p = [project] if project else []

    def bucket(sql: str, key: str) -> None:
        for r in conn.execute(sql, (lo, hi, *p)):
            day = datetime.strptime(r["ts"], FMT).replace(tzinfo=UTC).astimezone(zone).date().isoformat()
            if day in daily:
                daily[day][key] += 1

    bucket(f"SELECT t.created_at AS ts FROM tasks t WHERE t.created_at >= ? AND t.created_at < ?{by_project}", "tasks")
    bucket(f"SELECT s.opened_at AS ts FROM pr_stats s JOIN tasks t ON t.id = s.task_id "
           f"WHERE s.opened_at >= ? AND s.opened_at < ?{by_project}", "prs")
    bucket(f"SELECT s.merged_at AS ts FROM pr_stats s JOIN tasks t ON t.id = s.task_id "
           f"WHERE s.merged_at >= ? AND s.merged_at < ?{by_project}", "merged")
    projects = [r[0] for r in conn.execute("SELECT DISTINCT project FROM tasks ORDER BY project")]
    kpis = compute_kpis(conn, since=now - timedelta(days=days), until=now, project=project)
    return {"kpis": _kpis_dict(kpis), "daily": list(daily.values()), "projects": projects}
