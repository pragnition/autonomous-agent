"""The usage watch: every minute, the account pool's state from the proxy's /proxy/usage, and what core does about it.

Each account is `ok`, `hot` or `out`:
- `out` while the proxy gives it no rank (no capacity);
- `hot` once its 5-hour window is HOT_AT used, or it is ahead of its 7-day pace by more than PACE_HOT (it will run
  out before the window resets); it is `ok` again only below COOL_BELOW and back on pace, so it doesn't flap.

The pool, counted in `ok` accounts:
- two or more (or every verified account, in a pool smaller than that): normal;
- fewer: scheduled runs wait in the queue (sessions.HOLD_SCHEDULED) and people's requests still start;
- none: admins are told once, with the earliest reset. The Gate already holds everything once no account has a rank.

Turns a usage limit cut off (Conversations marks them in limit_waits) are each carried on once, as soon as an account
is `ok` again. An account whose `verified` goes false, or that has been `out` for longer than its 5-hour window, is
reported to the admins. Every change goes to the events log, where the dashboard's Events page shows it.

The proxy picks each new session's account by its own ranking, so this doesn't steer sessions between accounts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agent import db, events
from agent.core.sessions import HOLD_SCHEDULED
from agent.proxy import ProxyUnavailable

log = logging.getLogger(__name__)

HOT_AT, COOL_BELOW = 0.85, 0.75   # 5-hour window used
PACE_HOT = 0.05                   # this far ahead of the 7-day pace counts as running out before the reset
OUT_TOO_LONG_S = 5 * 3600         # out for longer than the 5-hour window: something is wrong with the account
STATE_KEY = "usage_watch"         # the last state seen, so a restart neither repeats nor misses a change
OK, HOT, OUT = "ok", "hot", "out"
NORMAL, HOLD, EMPTY = "normal", "hold_scheduled", "empty"


def _num(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _window(rec: dict, w: str) -> tuple[float | None, float | None]:
    pair = rec.get(w)
    if isinstance(pair, list) and len(pair) == 2:
        return _num(pair[0]), _num(pair[1])
    return None, None


def account_state(rec: dict, previous: str | None) -> str:
    if not isinstance(rec.get("rank"), int):
        return OUT
    used_5h, _ = _window(rec, "5h")
    pace = _num(rec.get("pace_delta"))
    if (used_5h is not None and used_5h >= HOT_AT) or (pace is not None and pace > PACE_HOT):
        return HOT
    if previous == HOT and ((used_5h is not None and used_5h >= COOL_BELOW) or (pace is not None and pace > 0)):
        return HOT
    return OK


def next_reset(rec: dict) -> float | None:
    """When the account gets room again: its 7-day reset when that window is spent, else its 5-hour one."""
    used_7d, reset_7d = _window(rec, "7d")
    _, reset_5h = _window(rec, "5h")
    return reset_7d if used_7d is not None and used_7d >= 1 else reset_5h


def pool_level(ok: int, verified: int) -> str:
    if ok == 0:
        return EMPTY
    return NORMAL if ok >= min(2, max(verified, 1)) else HOLD


class UsageWatch:
    def __init__(self, conn: sqlite3.Connection, proxy, *, alert: Callable[[str], Awaitable[None]],
                 resume: Callable[[str], str | None], timezone: str = "UTC",
                 clock: Callable[[], float] = time.time):
        """alert(text) tells the admins; resume(work_item_id) carries on one turn a limit cut off."""
        self.conn, self.proxy, self.alert, self.resume = conn, proxy, alert, resume
        self.tz, self.clock = ZoneInfo(timezone), clock

    def _load(self) -> dict:
        try:
            state = json.loads(db.get_setting(self.conn, STATE_KEY) or "{}")
        except ValueError:
            state = {}
        return {"accounts": state.get("accounts") or {}, "pool": state.get("pool")}

    def _when(self, epoch: float | None) -> str:
        if epoch is None:
            return "unknown"
        return datetime.fromtimestamp(epoch, UTC).astimezone(self.tz).strftime("%a %H:%M %Z")

    def _event(self, type: str, data: dict) -> None:
        events.append(self.conn, type, task_id=None, actor="usage_watch", data=data)

    async def _tell(self, text: str) -> None:
        try:
            await self.alert(text)
        except Exception:
            log.exception("couldn't send the usage alert: %s", text)

    async def tick(self) -> None:
        try:
            raw = await self.proxy.raw_usage()
        except ProxyUnavailable:
            return  # the Gate already waits while the proxy is away; keep the last state
        now, old = self.clock(), self._load()
        accounts = {code: rec for code, rec in raw.items() if code != "_all" and isinstance(rec, dict)}
        seen: dict[str, dict] = {}
        for code, rec in sorted(accounts.items()):
            before = old["accounts"].get(code) or {}
            state = account_state(rec, before.get("state"))
            verified = bool(rec.get("verified"))
            entry = {"state": state, "since": before.get("since", now) if before.get("state") == state else now,
                     "verified": verified, "out_alerted": bool(before.get("out_alerted")) and state == OUT}
            used_5h, _ = _window(rec, "5h")
            if before.get("state") and before["state"] != state:
                self._event("usage_account", {"account": code, "from": before["state"], "to": state,
                                              "used_5h": used_5h, "pace_delta": _num(rec.get("pace_delta"))})
            if before.get("verified") and not verified:
                self._event("usage_unverified", {"account": code})
                await self._tell(f"Account {code} is no longer verified by the proxy; it gets no new sessions until "
                                 "someone signs it in again.")
            if verified and state == OUT and not entry["out_alerted"] and now - entry["since"] > OUT_TOO_LONG_S:
                entry["out_alerted"] = True
                self._event("usage_out_too_long", {"account": code, "since": entry["since"]})
                await self._tell(f"Account {code} has had no capacity for over {OUT_TOO_LONG_S // 3600} hours "
                                 f"(next reset {self._when(next_reset(rec))}, status {rec.get('status') or 'none'}).")
            seen[code] = entry

        ok = sum(1 for e in seen.values() if e["state"] == OK)
        verified = sum(1 for e in seen.values() if e["verified"])
        level = pool_level(ok, verified)
        db.set_setting(self.conn, HOLD_SCHEDULED, "0" if level == NORMAL else "1")
        if old["pool"] and old["pool"] != level:
            self._event("usage_pool", {"from": old["pool"], "to": level, "ok": ok})
        if level == EMPTY and old["pool"] != EMPTY:  # once, including a core that starts with the pool empty
            resets = [r for code, rec in accounts.items() if seen[code]["verified"]
                      if (r := next_reset(rec)) is not None]
            await self._tell("Every Claude account is hot or out of capacity. People's requests still start on a "
                             "hot account while one has room; scheduled runs wait. Earliest reset: "
                             f"{self._when(min(resets) if resets else None)}.")
        elif old["pool"] == EMPTY and level != EMPTY:
            await self._tell(f"Claude capacity is back: {ok} account{'s' if ok != 1 else ''} with room.")
        db.set_setting(self.conn, STATE_KEY, json.dumps({"accounts": seen, "pool": level}))

        if ok:
            for row in self.conn.execute("SELECT work_item_id FROM limit_waits WHERE resumed_at IS NULL "
                                         "ORDER BY created_at").fetchall():
                new_id = self.resume(row["work_item_id"])
                self._event("usage_limit_resumed", {"work_item": row["work_item_id"], "resumed_as": new_id})
