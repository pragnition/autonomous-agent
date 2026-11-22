"""KPIs: what the agent worked on, how long it took, how much of it got done, what shipped, and the tokens it used.

Four numbers, the same whatever the work came from (a Slack DM or thread, a Linear issue, a schedule): Tasks, Time per
task, Finished and Shipped. Tasks come from the task record (task_record.py), which core builds itself from the
requests the agent answered; Shipped comes from GitHub's and Linear's own state (agent_prs, linear_issues). Nothing
depends on the agent recording anything.

Tokens come from the runs table. A run counts to the project of the task its request was labeled into (the labeler
runs after the run is recorded, so this is joined at query time and catches up on its own), else to the project its
conversation (else its task) was on when it ended, else to "general", so a window's projects add up to its total.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

FMT = "%Y-%m-%dT%H:%M:%SZ"
SOURCES = ("slack", "linear", "schedule")
GENERAL = "general"   # tasks on no configured project


@dataclass(frozen=True)
class Stat:
    n: int
    median_s: float | None
    p90_s: float | None


@dataclass(frozen=True)
class Usage:
    """Tokens summed over runs. No Claude cost: its estimate (API prices for a seat pool) reads as spend, so the KPIs
    never show it."""
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class AppUsage(Usage):
    """The apps' metered model calls (llm_usage): `runs` counts calls, and cost_usd is what the provider bills."""
    cost_usd: float = 0.0


USAGE_FIELDS = ("runs", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _window(since: datetime, until: datetime) -> tuple[str, str]:
    return since.astimezone(UTC).strftime(FMT), until.astimezone(UTC).strftime(FMT)


# Each run with its project: its request's task record, else that request's label, else what the run was stamped with
# when it ended (its conversation's or task's project). A run's id is its work item's id.
_RUNS = ("SELECT r.started_at, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, "
         "COALESCE(NULLIF(t.project, ''), NULLIF(l.project, ''), NULLIF(r.project, '')) AS project FROM runs r "
         "LEFT JOIN request_labels l ON l.work_item_id = r.id LEFT JOIN task_records t ON t.id = l.task_id "
         "WHERE r.started_at >= ? AND r.started_at < ?")


def _ordered(usage: dict[str, Usage]) -> dict[str, Usage]:
    """Most output first, "general" last."""
    out = dict(sorted(((p, u) for p, u in usage.items() if p != GENERAL), key=lambda kv: (-kv[1].output_tokens, kv[0])))
    if GENERAL in usage:
        out[GENERAL] = usage[GENERAL]
    return out


def tokens_by_project(conn: sqlite3.Connection, *, since: datetime, until: datetime) -> dict[str, Usage]:
    """The tokens of the runs that started in [since, until), by project ("general": none), most output first and
    "general" last."""
    rows = conn.execute(
        f"SELECT project, COUNT(*) AS runs, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
        f"SUM(cache_read_tokens) AS cr, SUM(cache_write_tokens) AS cw FROM ({_RUNS}) GROUP BY 1",
        _window(since, until)).fetchall()
    usage: dict[str, Usage] = {}
    for r in rows:   # every run lands in exactly one row, so the rows add up to the window's total
        key = r["project"] or GENERAL
        usage[key] = total((usage.get(key, Usage()), Usage(r["runs"], r["i"], r["o"], r["cr"], r["cw"])))
    return _ordered(usage)


def daily_tokens(conn: sqlite3.Connection, now: datetime, tz: str, days: int = 7) -> dict:
    """The tokens of each of the last `days` local days (today included, days in `tz`) by project: {"projects": every
    project that used any, most output first and "general" last, "days": [{"date", "by_project"}], oldest first}."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    dates = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
    since = datetime.combine(dates[0], datetime.min.time(), zone)
    per_day: dict[str, dict[str, Usage]] = {d.isoformat(): {} for d in dates}
    for r in conn.execute(_RUNS, _window(since, now)):
        day = datetime.strptime(r["started_at"], FMT).replace(tzinfo=UTC).astimezone(zone).date().isoformat()
        if day not in per_day:
            continue
        key = r["project"] or GENERAL
        run = Usage(1, r["input_tokens"] or 0, r["output_tokens"] or 0, r["cache_read_tokens"] or 0,
                    r["cache_write_tokens"] or 0)
        per_day[day][key] = total((per_day[day].get(key, Usage()), run))
    week: dict[str, Usage] = {}
    for usage in per_day.values():
        for key, u in usage.items():
            week[key] = total((week.get(key, Usage()), u))
    return {"projects": list(_ordered(week)),
            "days": [{"date": day, "by_project": _ordered(usage)} for day, usage in per_day.items()]}


def total(parts: Iterable[Usage]) -> Usage:
    parts = list(parts)
    return Usage(*(sum(getattr(u, f) for u in parts) for f in USAGE_FIELDS))


def app_tokens_by_upstream(conn: sqlite3.Connection, *, since: datetime, until: datetime) -> dict[str, AppUsage]:
    """The model calls the agent's apps made through core's meter (llm_usage) in [since, until), by upstream."""
    lo, hi = _window(since, until)
    rows = conn.execute(
        "SELECT upstream, COUNT(*) AS runs, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
        "SUM(cached_tokens) AS cr, SUM(cache_write_tokens) AS cw, SUM(cost_usd) AS usd "
        "FROM llm_usage WHERE at >= ? AND at < ? GROUP BY upstream ORDER BY usd DESC, upstream", (lo, hi))
    return {r["upstream"]: AppUsage(r["runs"], r["i"], r["o"], r["cr"], r["cw"], round(r["usd"], 6)) for r in rows}


def usage_windows(conn: sqlite3.Connection, now: datetime, tz: str) -> dict[str, dict]:
    """Today, the last 7 days and this month (days and months in `tz`): the agent's tokens by project with their
    total, and its apps' metered model calls by upstream."""
    local = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for name, since in (("today", local), ("7d", now - timedelta(days=7)), ("month", local.replace(day=1))):
        projects = tokens_by_project(conn, since=since, until=now)
        out[name] = {"since": _window(since, now)[0], "by_project": projects, "total": total(projects.values()),
                     "apps_by_upstream": app_tokens_by_upstream(conn, since=since, until=now)}
    return out


@dataclass(frozen=True)
class Kpis:
    since: datetime
    until: datetime
    tasks: int
    by_source: dict[str, int]
    by_project: dict[str, int]   # each project's tasks, most first, then "general" (tasks on no project)
    time: Stat               # the agent's own working time per task
    finished: int
    merged: int
    issues_closed: int
    tokens_by_project: dict[str, Usage] = field(default_factory=dict)   # the window's runs, see tokens_by_project

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
    lo, hi = _window(since, until)
    rows = conn.execute("SELECT source, project, work_time_s, finished FROM task_records "
                        "WHERE started_at >= ? AND started_at < ?", (lo, hi)).fetchall()
    by_source = {s: 0 for s in SOURCES}
    projects: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["project"]:
            projects[r["project"]] = projects.get(r["project"], 0) + 1
    by_project = dict(sorted(projects.items(), key=lambda kv: (-kv[1], kv[0])))
    general = sum(1 for r in rows if not r["project"])
    if general:
        by_project[GENERAL] = general

    def count(sql: str) -> int:
        return conn.execute(sql, (lo, hi)).fetchone()[0]

    return Kpis(since, until, tasks=len(rows), by_source=by_source, by_project=by_project,
                time=_stat([r["work_time_s"] for r in rows]), finished=sum(1 for r in rows if r["finished"]),
                merged=count("SELECT COUNT(*) FROM agent_prs WHERE merged_at >= ? AND merged_at < ?"),
                issues_closed=count("SELECT COUNT(*) FROM linear_issues WHERE completed_at >= ? AND completed_at < ?"),
                tokens_by_project=tokens_by_project(conn, since=since, until=until))


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n / 1_000_000_000:.2f}B".replace(".00B", "B")


def format_usage(u: Usage) -> str:
    return (f"{format_tokens(u.input_tokens)} in · {format_tokens(u.output_tokens)} out · "
            f"{format_tokens(u.cache_read_tokens)} cache read · {format_tokens(u.cache_write_tokens)} cache write")


WINDOW_NAMES = {"today": "today", "7d": "last 7 days", "month": "this month"}


def format_usage_windows(windows: dict[str, dict]) -> str:
    """Tokens by project for today, 7 days and this month, and the apps' metered calls by upstream."""
    lines = []
    for key, label in WINDOW_NAMES.items():
        w = windows[key]
        lines.append(f"Tokens {label}: {format_usage(w['total'])}")
        lines += [f"  {project}: {format_usage(u)}" for project, u in w["by_project"].items()]
        lines += [f"  app meter, {name}: {_n(u.runs, 'call', 'calls')} · {format_usage(u)} · ${u.cost_usd:.2f} billed"
                  for name, u in w["apps_by_upstream"].items()]
    return "\n".join(lines)


def format_daily_tokens(daily: dict) -> str:
    """Each local day's tokens by project, oldest first."""
    lines = [f"Tokens per day, last {_n(len(daily['days']), 'day', 'days')}:"]
    for day in daily["days"]:
        if not day["by_project"]:
            lines.append(f"  {day['date']}: none")
            continue
        lines.append(f"  {day['date']}: {format_usage(total(day['by_project'].values()))}")
        lines += [f"    {project}: {format_usage(u)}" for project, u in day["by_project"].items()]
    return "\n".join(lines)


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def format_kpis(k: Kpis, name: str = "Agent") -> str:
    days = max(1, round((k.until - k.since) / timedelta(days=1)))
    split = " · ".join(f"{name} {n}" for name, n in k.by_project.items())
    time = "n/a" if not k.time.n else \
        f"median {format_duration(k.time.median_s)} · p90 {format_duration(k.time.p90_s)}"
    rate = "n/a" if k.finished_rate is None else f"{k.finished_rate:.0%}"
    return "\n".join([
        f"{name}, last {_n(days, 'day', 'days')}",
        f"Tasks: {k.tasks}" + (f" ({split})" if split else ""),
        f"Time per task: {time}",
        f"Finished: {rate} ({k.finished} of {k.tasks})",
        f"Shipped: {_n(k.merged, 'PR', 'PRs')} merged · {_n(k.issues_closed, 'Linear issue', 'Linear issues')} closed",
        f"Tokens: {format_usage(total(k.tokens_by_project.values()))}",
        *(f"  {project}: {format_usage(u)}" for project, u in k.tokens_by_project.items()),
    ])


def register_kpi_tools(router: AgentToolRouter) -> None:
    async def kpis(ctx: ToolContext, args: dict) -> ToolReply:
        days = args.get("days", 7)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            return ToolReply("days must be a whole number from 1 to 365")
        until = datetime.now(UTC)
        tz = router.config.timezone
        k = compute_kpis(router.conn, since=until - timedelta(days=days), until=until)
        return ToolReply(format_kpis(k, router.config.name) + "\n\n"
                         + format_usage_windows(usage_windows(router.conn, until, tz)) + "\n\n"
                         + format_daily_tokens(daily_tokens(router.conn, until, tz)))

    router.register(ToolSpec(
        "kpis", "The agent's own KPIs over the last `days` days (default 7): tasks by project, time per task, the "
                "share finished, PRs merged and Linear issues closed, and its tokens by project (also today, 7 days and "
                "this month, with the apps' metered model calls by upstream, and per day for the last 7 days).",
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
