"""Schedules: work that starts on a timer.

The schedules file (data/schedules/schedules.toml, /data/schedules/schedules.toml in core and the agent container)
holds one [name] table per schedule. The agent edits it when someone asks for work on a schedule; people can edit it
by hand. Core reads it every minute and fires each due schedule as a turn. `python -m agent schedules` checks it.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

NAME = re.compile(r"^[a-z0-9-]{1,40}$")
REQUIRED = ("cron", "channel", "prompt", "by")
OPTIONAL = {"link": str, "paused": bool}


@dataclass(frozen=True)
class Schedule:
    name: str
    cron: str
    channel: str         # a channel name or ID, or a person key for their DM
    prompt: str
    by: str              # who asked: a person key, or whatever was written
    link: str | None = None
    paused: bool = False


def _entry(name: str, table) -> tuple[Schedule | None, str | None]:
    if not isinstance(table, dict):
        return None, f"{name}: each schedule is a [name] table, not a top-level value"
    if not NAME.match(name):
        return None, f"{name}: a name is lowercase letters, digits and dashes, at most 40 characters"
    unknown = sorted(set(table) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        return None, f"{name}: unknown keys {', '.join(unknown)}"
    for key in REQUIRED:
        if not isinstance(table.get(key), str) or not table[key].strip():
            return None, f"{name}: '{key}' is required and must be text"
    for key, kind in OPTIONAL.items():
        if key in table and not isinstance(table[key], kind):
            return None, f"{name}: '{key}' must be {'true or false' if kind is bool else 'text'}"
    cron = table["cron"].strip()
    if len(cron.split()) != 5 or not croniter.is_valid(cron):
        return None, f"{name}: '{cron}' isn't a five-field cron expression"
    return Schedule(name=name, cron=cron, channel=table["channel"].strip(), prompt=table["prompt"],
                    by=table["by"].strip(), link=table.get("link"), paused=table.get("paused", False)), None


def parse(text: str) -> tuple[list[Schedule] | None, list[str]]:
    """The valid schedules and one error per invalid entry; None when the text isn't TOML at all."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return None, [f"not valid TOML: {e}"]
    schedules, errors = [], []
    for name, table in data.items():
        schedule, error = _entry(name, table)
        if schedule is not None:
            schedules.append(schedule)
        else:
            errors.append(error)
    return schedules, errors


def latest_slot(cron: str, now: datetime) -> datetime:
    """The latest time the schedule was due, at or before now (to the minute)."""
    return croniter(cron, now.replace(second=0, microsecond=0) + timedelta(seconds=1)).get_prev(datetime)


def next_slots(cron: str, now: datetime, n: int = 3) -> list[datetime]:
    it = croniter(cron, now)
    return [it.get_next(datetime) for _ in range(n)]


# python -m agent schedules

def _timezone() -> str:
    """agent.toml's timezone where it's readable (core), else the copy core leaves for the agent, else UTC."""
    from agent import paths
    from agent.config import load_agent_config

    config = paths.CONFIG_DIR / "agent.toml"
    if config.is_file():
        try:
            return load_agent_config(config).timezone
        except Exception:
            pass
    if paths.RUNNER_TIMEZONE.is_file():
        return paths.RUNNER_TIMEZONE.read_text().strip() or "UTC"
    return "UTC"


def main(argv: list[str]) -> int:
    from agent import paths

    parser = argparse.ArgumentParser(prog="agent schedules", description="Check the schedules file")
    parser.add_argument("--file", type=Path, default=paths.SCHEDULES_FILE)
    parser.add_argument("--timezone", default=None)
    args = parser.parse_args(argv)
    if not args.file.is_file():
        print(f"no schedules file at {args.file}")
        return 0
    tz_name = args.timezone or _timezone()
    now = datetime.now(ZoneInfo(tz_name))
    schedules, errors = parse(args.file.read_text())
    for s in schedules or []:
        times = ", ".join(t.strftime("%Y-%m-%d %H:%M") for t in next_slots(s.cron, now))
        print(f"{s.name}  {s.cron}  -> {s.channel}{'  (paused)' if s.paused else ''}\n  next: {times} ({tz_name})")
    for error in errors:
        print(f"error: {error}")
    return 1 if errors else 0
