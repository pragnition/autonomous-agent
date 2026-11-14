"""KPIs: how fast handed-over Linear issues get picked up, turned into PRs, merged and closed.

Everything comes from what core observes itself: linear_issues (the webhook, then Linear's own close times),
agent_prs (GitHub's own PR times, tied to an issue by its key) and the `session_started` event a pull appends when a
runner claims a turn. Nothing depends on the agent recording anything.
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
    pickup: Stat
    to_pr: Stat
    to_merge: Stat
    to_done_pr: Stat
    to_done_other: Stat
    queue_wait: Stat
    issues: int
    done: int
    canceled: int
    prs: int
    merged: int
    closed: int
    unmatched: int

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


# When work on an issue first started: the first turn of any of its conversations a runner claimed
FIRST_START = ("(SELECT MIN(e.ts) FROM events e JOIN work_items w ON w.id = json_extract(e.data, '$.work_item') "
               "JOIN linear_issue_conversations c ON c.conversation_id = w.conversation_id "
               "WHERE e.type = 'session_started' AND c.issue_id = i.issue_id)")
FIRST_PR = "(SELECT MIN(p.opened_at) FROM agent_prs p WHERE p.issue_identifier = i.identifier)"


def compute_kpis(conn: sqlite3.Connection, *, since: datetime, until: datetime) -> Kpis:
    lo, hi = since.astimezone(UTC).strftime(FMT), until.astimezone(UTC).strftime(FMT)

    def rows(sql: str) -> list[sqlite3.Row]:
        return conn.execute(sql, (lo, hi)).fetchall()

    def count(sql: str) -> int:
        return conn.execute(sql, (lo, hi)).fetchone()[0]

    pickup = [_secs(r["handed_at"], r["started"]) for r in rows(
        f"SELECT i.handed_at, {FIRST_START} AS started FROM linear_issues i "
        "WHERE i.handed_at >= ? AND i.handed_at < ?") if r["started"] and r["started"] >= r["handed_at"]]
    to_pr = [_secs(r["started"], r["first_pr"]) for r in rows(
        f"SELECT {FIRST_START} AS started, {FIRST_PR} AS first_pr FROM linear_issues i "
        f"WHERE {FIRST_PR} >= ? AND {FIRST_PR} < ?") if r["started"] and r["started"] <= r["first_pr"]]
    to_merge = [_secs(r["opened_at"], r["merged_at"]) for r in rows(
        "SELECT opened_at, merged_at FROM agent_prs WHERE merged_at >= ? AND merged_at < ?")]
    to_done_pr, to_done_other = [], []
    for r in rows(f"SELECT i.handed_at, i.completed_at, {FIRST_PR} AS first_pr FROM linear_issues i "
                  "WHERE i.completed_at >= ? AND i.completed_at < ?"):
        (to_done_pr if r["first_pr"] else to_done_other).append(_secs(r["handed_at"], r["completed_at"]))

    queue_wait = []
    for r in rows("SELECT ts, data FROM events WHERE type = 'session_started' AND ts >= ? AND ts < ?"):
        data = json.loads(r["data"] or "{}")
        if data.get("queued_at"):
            queue_wait.append(_secs(data["queued_at"], r["ts"]))

    return Kpis(
        since, until, _stat(pickup), _stat(to_pr), _stat(to_merge), _stat(to_done_pr), _stat(to_done_other),
        _stat(queue_wait),
        issues=count("SELECT COUNT(*) FROM linear_issues WHERE handed_at >= ? AND handed_at < ?"),
        done=count("SELECT COUNT(*) FROM linear_issues WHERE completed_at >= ? AND completed_at < ?"),
        canceled=count("SELECT COUNT(*) FROM linear_issues WHERE canceled_at >= ? AND canceled_at < ?"),
        prs=count("SELECT COUNT(*) FROM agent_prs WHERE opened_at >= ? AND opened_at < ?"),
        merged=count("SELECT COUNT(*) FROM agent_prs WHERE merged_at >= ? AND merged_at < ?"),
        closed=count("SELECT COUNT(*) FROM agent_prs WHERE closed_at >= ? AND closed_at < ? AND merged_at IS NULL"),
        unmatched=count("SELECT COUNT(*) FROM agent_prs WHERE opened_at >= ? AND opened_at < ? "
                        "AND issue_identifier IS NULL"))


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
        f"Agent KPIs, last {days} day{'s' if days != 1 else ''}",
        _line("Ticket → pickup", k.pickup),
        _line("Pickup → PR", k.to_pr),
        _line("PR → merge", k.to_merge),
        _line("Ticket → done (with PR)", k.to_done_pr),
        _line("Ticket → done (no PR)", k.to_done_other),
        _line("Queue wait", k.queue_wait),
        f"Issues: {k.issues} · done: {k.done} · canceled: {k.canceled}",
        f"PRs: {k.prs} · merged: {k.merged} · closed unmerged: {k.closed} · not linked to an issue: {k.unmatched}",
        f"Merge rate: {rate}",
    ])


def register_kpi_tools(router: AgentToolRouter) -> None:
    async def kpis(ctx: ToolContext, args: dict) -> ToolReply:
        days = args.get("days", 7)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            return ToolReply("days must be a whole number from 1 to 365")
        until = datetime.now(UTC)
        return ToolReply(format_kpis(compute_kpis(router.conn, since=until - timedelta(days=days), until=until)))

    router.register(ToolSpec(
        "kpis", "The agent's own KPIs for Linear issues handed to it: ticket-to-pickup, pickup-to-PR, PR-to-merge "
                "and ticket-to-done times, queue wait and merge rate, over the last `days` days (default 7).",
        schema({}, {"days": "integer"}), "read"), kpis)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent import db, paths

    parser = argparse.ArgumentParser(prog="agent kpis", description="Show the agent's KPIs")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", default=str(paths.DB_DIR / "agent.db"))
    args = parser.parse_args(argv)
    until = datetime.now(UTC)
    print(format_kpis(compute_kpis(db.connect(args.db), since=until - timedelta(days=args.days), until=until)))
    return 0
