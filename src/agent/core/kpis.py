"""KPIs: how fast tickets get picked up, turned into PRs and merged, and how many of them get merged.

Everything comes from what core already records: tasks, pr_stats (the PR poller) and the `session_started` event
a pull appends when a runner claims a turn. No Claude cost is computed or shown here.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Stat:
    n: int
    median_s: float | None
    p90_s: float | None


@dataclass(frozen=True)
class Kpis:
    since: datetime
    until: datetime
    project: str | None
    pickup: Stat
    to_pr: Stat
    to_merge: Stat
    queue_wait: Stat
    tasks: int
    prs: int
    merged: int
    closed: int
    reverted: int
    failed: int
    cancelled: int

    @property
    def merge_rate(self) -> float | None:
        return self.merged / self.prs if self.prs else None


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _secs(start: str, end: str) -> float:
    return (_ts(end) - _ts(start)).total_seconds()


def _stat(values: list[float]) -> Stat:
    if not values:
        return Stat(0, None, None)
    ordered = sorted(values)
    return Stat(len(ordered), statistics.median(ordered), ordered[math.ceil(0.9 * len(ordered)) - 1])


def compute_kpis(conn: sqlite3.Connection, *, since: datetime, until: datetime, project: str | None = None) -> Kpis:
    lo, hi = since.astimezone(UTC).strftime(FMT), until.astimezone(UTC).strftime(FMT)
    by_project = " AND t.project = ?" if project else ""
    p = [project] if project else []

    def rows(sql: str, *args) -> list[sqlite3.Row]:
        return conn.execute(sql, (*args, *p)).fetchall()

    def count(sql: str, *args) -> int:
        return conn.execute(sql, (*args, *p)).fetchone()[0]

    first_start = "(SELECT MIN(e.ts) FROM events e WHERE e.task_id = t.id AND e.type = 'session_started')"
    pickup = [_secs(r["created_at"], r["started"]) for r in rows(
        f"SELECT t.created_at, {first_start} AS started FROM tasks t WHERE t.source = 'linear' "
        f"AND t.created_at >= ? AND t.created_at < ?{by_project}", lo, hi) if r["started"]]
    # A Linear task waits for its first session; any other task was created by the session that works on it
    # (its first session_started carries no task id), so a later turn in the thread must not move the start.
    to_pr = []
    for r in rows(f"SELECT t.source, s.opened_at, t.created_at, {first_start} AS started FROM pr_stats s "
                  f"JOIN tasks t ON t.id = s.task_id WHERE s.opened_at >= ? AND s.opened_at < ?{by_project}", lo, hi):
        origin = (r["started"] or r["created_at"]) if r["source"] == "linear" else r["created_at"]
        if origin <= r["opened_at"]:
            to_pr.append(_secs(origin, r["opened_at"]))
    to_merge = [_secs(r["opened_at"], r["merged_at"]) for r in rows(
        f"SELECT s.opened_at, s.merged_at FROM pr_stats s JOIN tasks t ON t.id = s.task_id "
        f"WHERE s.merged_at >= ? AND s.merged_at < ? AND s.opened_at IS NOT NULL{by_project}", lo, hi)]

    started = conn.execute("SELECT ts, data FROM events WHERE type = 'session_started' AND ts >= ? AND ts < ?",
                           (lo, hi)).fetchall()
    queue_wait = []
    for r in started:
        data = json.loads(r["data"] or "{}")
        if data.get("queued_at") and (not project or data.get("project") == project):
            queue_wait.append(_secs(data["queued_at"], r["ts"]))

    pr_base = "SELECT COUNT(*) FROM pr_stats s JOIN tasks t ON t.id = s.task_id WHERE "
    task_base = "SELECT COUNT(*) FROM tasks t WHERE t.created_at >= ? AND t.created_at < ?"
    tasks = count(task_base + by_project, lo, hi)
    prs = count(pr_base + f"s.opened_at >= ? AND s.opened_at < ?{by_project}", lo, hi)
    merged = count(pr_base + f"s.merged_at >= ? AND s.merged_at < ?{by_project}", lo, hi)
    closed = count(pr_base + f"s.closed_at >= ? AND s.closed_at < ? AND s.merged_at IS NULL{by_project}", lo, hi)
    reverted = count(pr_base + f"s.reverted_at >= ? AND s.reverted_at < ?{by_project}", lo, hi)
    failed = count(task_base + f" AND t.status = 'failed'{by_project}", lo, hi)
    cancelled = count(task_base + f" AND t.status = 'cancelled'{by_project}", lo, hi)
    return Kpis(since, until, project, _stat(pickup), _stat(to_pr), _stat(to_merge), _stat(queue_wait),
                tasks, prs, merged, closed, reverted, failed, cancelled)


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _line(label: str, stat: Stat) -> str:
    if not stat.n:
        return f"{label}: n/a (n=0)"
    return f"{label}: median {format_duration(stat.median_s)}, p90 {format_duration(stat.p90_s)} (n={stat.n})"


def format_kpis(k: Kpis) -> str:
    days = max(1, round((k.until - k.since) / timedelta(days=1)))
    rate = "n/a" if k.merge_rate is None else f"{k.merge_rate:.0%}"
    return "\n".join([
        f"Agent KPIs, last {days} day{'s' if days != 1 else ''}" + (f" ({k.project})" if k.project else ""),
        _line("Ticket → pickup", k.pickup),
        _line("Pickup → PR", k.to_pr),
        _line("PR → merge", k.to_merge),
        _line("Queue wait", k.queue_wait),
        f"Tasks: {k.tasks} · PRs: {k.prs} · merged: {k.merged} · closed unmerged: {k.closed} · "
        f"reverted: {k.reverted} · failed: {k.failed} · cancelled: {k.cancelled}",
        f"Merge rate: {rate}",
    ])


def register_kpi_tools(router: AgentToolRouter) -> None:
    async def kpis(ctx: ToolContext, args: dict) -> ToolReply:
        project = args.get("project") or None
        if project is not None and project not in router.packs:
            return ToolReply(f"unknown project {project!r}; the projects are {', '.join(sorted(router.packs))}")
        days = args.get("days", 7)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            return ToolReply("days must be a whole number from 1 to 365")
        until = datetime.now(UTC)
        return ToolReply(format_kpis(compute_kpis(router.conn, since=until - timedelta(days=days), until=until,
                                                  project=project)))

    router.register(ToolSpec(
        "kpis", "The agent's own KPIs: ticket-to-pickup, pickup-to-PR and PR-to-merge times, queue wait and merge "
                "rate, over the last `days` days (default 7), for one project or all.",
        schema({}, {"days": "integer", "project": "string"}), "read"), kpis)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent import db, paths

    parser = argparse.ArgumentParser(prog="agent kpis", description="Show the agent's KPIs")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--project")
    parser.add_argument("--db", default=str(paths.DB_DIR / "agent.db"))
    args = parser.parse_args(argv)
    until = datetime.now(UTC)
    print(format_kpis(compute_kpis(db.connect(args.db), since=until - timedelta(days=args.days), until=until,
                                   project=args.project)))
    return 0
