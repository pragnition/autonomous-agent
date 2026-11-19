"""Parking: a conversation waits out its account's soft cap (or any time a person names) and carries itself on.

The agent calls `park` and ends its turn. Core records the park (the parks table), says in the thread when it carries
on and that a reply resumes it earlier, and every minute carries on each park that is due: a new turn in the same
conversation with the agent's own note. Like any next turn, the runner resumes the session when the proxy keeps it on
the account it ran on, and otherwise starts a fresh one with a digest of it (runner/handoff.py). A person's message
in the thread ends the park at once (Conversations._unpark) and is the next turn itself.

Without a time, a park lasts until the soft cap on the conversation's account lifts (proxy.SoftCap: SOFT_CAP_LIFT_S
before its 5-hour window resets), or until the reset when the account has no capacity at all.

The status tool's account line comes from here too: how much of its 5-hour window the conversation's account has
used, its cap, and when the window resets and the cap lifts, so the agent can pace itself and tell people.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent import events
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema
from agent.proxy import ProxyUnavailable, Usage

log = logging.getLogger(__name__)

MAX_PARK_S = 7 * 24 * 3600   # a park further out than this is a mistake, not a plan
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
PARK_DESCRIPTION = (
    "Park this conversation until a time, then end your turn: core carries it on by itself at that time, in a new "
    "turn of this same conversation with your note, and a person's reply in the thread resumes it earlier. Use it "
    "to wait out your account's 5-hour soft cap (see status) or when someone asks you to pause until a time, instead "
    "of keeping the turn alive. until: HH:MM in the team's timezone (the next one), or an ISO date-time; leave it out "
    "to wait until the soft cap on your account lifts. note: what you were doing and what's next, for the turn that "
    "carries on. Background agents and shells end with your turn: let them finish first, or say in the note where "
    "they were.")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def _span(seconds: float) -> str:
    minutes = max(int(seconds // 60), 0)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def parse_until(text: str, *, now: float, tz: ZoneInfo) -> float | None:
    """`text` as an epoch: HH:MM is the next such time in `tz`, an ISO date-time without an offset is in `tz` too.
    None when it is neither."""
    text = text.strip()
    if m := _CLOCK.match(text):
        hour, minute = int(m[1]), int(m[2])
        if hour > 23 or minute > 59:
            return None
        local_now = datetime.fromtimestamp(now, tz)
        at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at.timestamp() <= now:
            at = (local_now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return at.timestamp()
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=tz)).timestamp()


class Parking:
    def __init__(self, conn: sqlite3.Connection, proxy, *, resume: Callable[[str, str, str], str | None],
                 timezone: str = "UTC", clock: Callable[[], float] = time.time):
        """resume(conversation_id, work_item_id, note) queues the turn that carries a park on (Conversations.
        resume_parked) and returns its id, or None when the conversation is gone."""
        self.conn, self.proxy, self.resume = conn, proxy, resume
        self.tz, self.clock = ZoneInfo(timezone), clock

    def when(self, epoch: float, now: float | None = None) -> str:
        """A time as people in the team read it: 14:30 SGT today, Fri 14:30 SGT on another day."""
        now = self.clock() if now is None else now
        at = datetime.fromtimestamp(epoch, self.tz)
        same_day = at.date() == datetime.fromtimestamp(now, self.tz).date()
        return at.strftime("%H:%M %Z" if same_day else "%a %H:%M %Z")

    def account_of(self, conversation_id: str | None) -> str | None:
        row = self.conn.execute("SELECT session_account FROM conversations WHERE id = ?",
                                (conversation_id,)).fetchone() if conversation_id else None
        return row["session_account"] if row else None

    async def _usage(self) -> Usage | None:
        try:
            return await self.proxy.usage()
        except ProxyUnavailable:
            return None

    def lift_at(self, usage: Usage, code: str | None, now: float) -> float | None:
        """When the account has room again: its soft cap's lift, or its reset when it has no capacity at all."""
        account = usage.accounts.get(code) if code else None
        if account is None or account.reset_5h is None:
            return None
        lift = account.reset_5h - usage.soft_cap.lift_s
        if lift > now:
            return lift
        return account.reset_5h if account.rank is None and account.reset_5h > now else None

    # the status tool's lines

    async def status_lines(self, ctx: ToolContext) -> list[str]:
        code = self.account_of(ctx.conversation_id)
        usage = await self._usage()
        if usage is None:
            return ["account: usage not known (the proxy isn't answering)"]
        now = self.clock()
        lines = []
        account = usage.accounts.get(code) if code else None
        if account is None:
            lines.append("account: not known yet for this session")
        else:
            cap = usage.soft_cap.cap(account.reset_5h, now)
            used = "unknown" if account.used_5h is None else f"{account.used_5h:.0%}"
            parts = [f"account: {code}", f"5h window {used} used of its {cap:.0%} cap"]
            if account.reset_5h is not None:
                parts.append(f"resets {self.when(account.reset_5h, now)} (in {_span(account.reset_5h - now)})")
                lift = account.reset_5h - usage.soft_cap.lift_s
                parts.append(f"cap lifts to 100% at {self.when(lift, now)} (in {_span(lift - now)})" if lift > now
                             else "cap lifted")
            if usage.over_cap(code):
                parts.append("over its cap: park until it lifts" if account.rank is not None
                             else "no capacity: park until it resets")
            lines.append(" · ".join(parts))
        room = sum(1 for c, a in usage.accounts.items() if a.rank is not None and not usage.over_cap(c))
        lines.append(f"accounts under their cap: {room} of {len(usage.accounts)}")
        return lines

    # the park tool

    async def park(self, router: AgentToolRouter, ctx: ToolContext, args: dict) -> ToolReply:
        if ctx.conversation_id is None:
            return ToolReply("this turn has no conversation to park")
        now, raw = self.clock(), str(args.get("until") or "").strip()
        if raw:
            until = parse_until(raw, now=now, tz=self.tz)
            if until is None:
                return ToolReply(f"can't read until '{raw}': give HH:MM or an ISO date-time")
        else:
            code, usage = self.account_of(ctx.conversation_id), await self._usage()
            until = self.lift_at(usage, code, now) if usage is not None else None
            if until is None:
                return ToolReply(f"there is no cap to wait out on account {code or '(not known yet)'}: its cap is "
                                 "lifted or its reset isn't known. Give until to park anyway.")
        if until <= now:
            return ToolReply(f"{self.when(until, now)} has passed; give a time in the future")
        if until - now > MAX_PARK_S:
            return ToolReply(f"that's more than {MAX_PARK_S // 86400} days away; park for less")
        note = str(args.get("note") or "").strip()
        stamp = _iso(now)
        self.conn.execute("UPDATE parks SET ended_at = ?, ended_how = 'replaced' WHERE conversation_id = ? AND "
                          "ended_at IS NULL", (stamp, ctx.conversation_id))
        self.conn.execute("INSERT INTO parks(conversation_id, work_item_id, until_at, note, created_at) "
                          "VALUES (?, ?, ?, ?, ?)", (ctx.conversation_id, ctx.work_item_id, _iso(until), note, stamp))
        events.append(self.conn, "parked", task_id=ctx.task_id, actor=ctx.actor,
                      data={"conversation_id": ctx.conversation_id, "work_item": ctx.work_item_id,
                            "until": _iso(until)})
        at = self.when(until, now)
        if ctx.channel:
            await router._post(ctx, f"Parked until {at}; reply here to resume earlier.")
        return ToolReply(f"Parked until {at} (in {_span(until - now)}). End this turn now; at {at} core carries "
                         "this conversation on in a new turn with your note. A reply in the thread resumes it "
                         "earlier.")

    # every minute

    async def tick(self) -> None:
        now = self.clock()
        for row in self.conn.execute("SELECT * FROM parks WHERE ended_at IS NULL AND until_at <= ? ORDER BY until_at",
                                     (_iso(now),)).fetchall():
            parked_at = self.when(_epoch(row["created_at"]), now)
            text = (f"(You parked this conversation at {parked_at} until {self.when(_epoch(row['until_at']), now)}. "
                    "It's time: carry on from where you stopped"
                    + (f". Your note: {row['note']})" if row["note"] else ".)"))
            new_id = self.resume(row["conversation_id"], row["work_item_id"], text)
            self.conn.execute("UPDATE parks SET ended_at = ?, ended_how = 'resumed', resumed_as = ? WHERE id = ?",
                              (_iso(now), new_id, row["id"]))
            events.append(self.conn, "park_resumed", task_id=None, actor="parking",
                          data={"conversation_id": row["conversation_id"], "resumed_as": new_id})


def register_parking_tools(router: AgentToolRouter, parking: Parking) -> None:
    async def park(ctx: ToolContext, args: dict) -> ToolReply:
        return await parking.park(router, ctx, args)

    router.register(ToolSpec("park", PARK_DESCRIPTION, schema(optional={"until": "string", "note": "string"}), "act"),
                    park)
    router.status_lines.append(parking.status_lines)
