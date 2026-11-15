"""KPIs: what the agent worked on, how long it took, how much of it got done, and what shipped.

Four numbers, the same whatever the work came from (a Slack DM or thread, a Linear issue, a schedule): Tasks, Time per
task, Finished and Shipped. Tasks come from the task record (task_record.py), which core builds itself from the
requests the agent answered; Shipped comes from GitHub's and Linear's own state (agent_prs, linear_issues). Nothing
depends on the agent recording anything.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

FMT = "%Y-%m-%dT%H:%M:%SZ"
SOURCES = ("slack", "linear", "schedule")


@dataclass(frozen=True)
class Stat:
    n: int
    median_s: float | None
    p90_s: float | None


@dataclass(frozen=True)
class Kpis:
    since: datetime
    until: datetime
    tasks: int
    by_source: dict[str, int]
    time: Stat               # the agent's own working time per task
    finished: int
    merged: int
    issues_closed: int

    @property
    def finished_rate(self) -> float | None:
        return self.finished / self.tasks if self.tasks else None


def _stat(values: list[float]) -> Stat:
    if not values:
        return Stat(0, None, None)
    ordered = sorted(values)
    return Stat(len(ordered), statistics.median(ordered), ordered[math.ceil(0.9 * len(ordered)) - 1])


def compute_kpis(conn: sqlite3.Connection, *, since: datetime, until: datetime) -> Kpis:
    """Tasks that started in [since, until); PRs merged and Linear issues completed in it."""
    lo, hi = since.astimezone(UTC).strftime(FMT), until.astimezone(UTC).strftime(FMT)
    rows = conn.execute("SELECT source, jensen_time_s, finished FROM task_records "
                        "WHERE started_at >= ? AND started_at < ?", (lo, hi)).fetchall()
    by_source = {s: 0 for s in SOURCES}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    def count(sql: str) -> int:
        return conn.execute(sql, (lo, hi)).fetchone()[0]

    return Kpis(since, until, tasks=len(rows), by_source=by_source,
                time=_stat([r["jensen_time_s"] for r in rows]), finished=sum(1 for r in rows if r["finished"]),
                merged=count("SELECT COUNT(*) FROM agent_prs WHERE merged_at >= ? AND merged_at < ?"),
                issues_closed=count("SELECT COUNT(*) FROM linear_issues WHERE completed_at >= ? AND completed_at < ?"))


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def format_kpis(k: Kpis, name: str = "Agent") -> str:
    days = max(1, round((k.until - k.since) / timedelta(days=1)))
    s = k.by_source
    time = "n/a" if not k.time.n else \
        f"median {format_duration(k.time.median_s)} · p90 {format_duration(k.time.p90_s)}"
    rate = "n/a" if k.finished_rate is None else f"{k.finished_rate:.0%}"
    return "\n".join([
        f"{name}, last {_n(days, 'day', 'days')}",
        f"Tasks: {k.tasks} (Slack {s.get('slack', 0)} · Linear {s.get('linear', 0)} · "
        f"schedules {s.get('schedule', 0)})",
        f"Time per task: {time}",
        f"Finished: {rate} ({k.finished} of {k.tasks})",
        f"Shipped: {_n(k.merged, 'PR', 'PRs')} merged · {_n(k.issues_closed, 'Linear issue', 'Linear issues')} closed",
    ])


def register_kpi_tools(router: AgentToolRouter) -> None:
    async def kpis(ctx: ToolContext, args: dict) -> ToolReply:
        days = args.get("days", 7)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            return ToolReply("days must be a whole number from 1 to 365")
        until = datetime.now(UTC)
        return ToolReply(format_kpis(compute_kpis(router.conn, since=until - timedelta(days=days), until=until),
                                     router.config.name))

    router.register(ToolSpec(
        "kpis", "The agent's own KPIs over the last `days` days (default 7): tasks by source, time per task, the "
                "share finished, and PRs merged and Linear issues closed.",
        schema({}, {"days": "integer"}), "read"), kpis)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent import db, paths

    parser = argparse.ArgumentParser(prog="agent kpis", description="Show the agent's KPIs")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", default=str(paths.DB_DIR / "agent.db"))
    args = parser.parse_args(argv)
    try:
        from agent.config import load_agent_config
        name = load_agent_config(paths.CONFIG_DIR / "agent.toml").name
    except Exception:  # the KPIs don't need the config; the header just says "Agent"
        name = "Agent"
    until = datetime.now(UTC)
    print(format_kpis(compute_kpis(db.connect(args.db), since=until - timedelta(days=args.days), until=until), name))
    return 0
