"""Client for the host's Claude account proxy (claude_proxy/), reached through this service's relay.

Only the answered-locally /proxy/ API is used here: usage per account (by code name), a session's pin, and for the
dashboard the raw usage reply and the 5-minute perf rows. The one exception is doctor's check that the proxy serves a
model, a free GET of Anthropic's /v1/models/<id>.

The soft cap on each account's 5-hour window lives here too, so core's usage watch, the runner's watchdog and the
dashboard read it the same way: an account counts as full at SOFT_CAP_5H of its window until SOFT_CAP_LIFT_S before
the window resets, and at 100% from then on (what is left would go unused at the reset anyway). An unknown reset time
keeps the soft cap.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

PLACEHOLDER_TOKEN = "sk-ant-oat01-container-via-host-proxy"


class ProxyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SoftCap:
    limit: float = 0.65    # the share of the 5-hour window an account may use ...
    lift_s: float = 5400   # ... until this long before the window resets; then all of it

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SoftCap:
        environ = os.environ if environ is None else environ
        default = cls()
        try:
            limit = float(environ.get("SOFT_CAP_5H") or default.limit)
        except ValueError:
            limit = default.limit
        try:
            lift_s = float(environ.get("SOFT_CAP_LIFT_S") or default.lift_s)
        except ValueError:
            lift_s = default.lift_s
        return cls(min(max(limit, 0.0), 1.0), max(lift_s, 0.0))

    def cap(self, reset_5h: float | None, now: float) -> float:
        """The account's effective cap: the soft cap, or 1.0 within lift_s of a known reset."""
        if reset_5h is not None and reset_5h - now <= self.lift_s:
            return 1.0
        return self.limit

    def over(self, used_5h: float | None, reset_5h: float | None, now: float) -> bool:
        """Whether the account has reached its effective cap. Unknown use is not over: the proxy's rank decides."""
        return used_5h is not None and used_5h >= self.cap(reset_5h, now)


def _num(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def window_5h(rec: dict) -> tuple[float | None, float | None]:
    """The (used share, reset epoch) of the proxy's 5-hour window for one account, None for what isn't there."""
    pair = rec.get("5h")
    if isinstance(pair, list) and len(pair) == 2:
        return _num(pair[0]), _num(pair[1])
    return None, None


@dataclass(frozen=True)
class AccountUsage:
    status: str
    rank: int | None
    verified: bool
    used_5h: float | None = None
    reset_5h: float | None = None


@dataclass(frozen=True)
class Usage:
    accounts: dict[str, AccountUsage]
    soft_cap: SoftCap = field(default_factory=SoftCap)
    now: float | None = None  # when the usage was read; None is now

    def _now(self) -> float:
        return time.time() if self.now is None else self.now

    def has_capacity(self) -> bool:
        return any(a.rank is not None for a in self.accounts.values())

    def status_of(self, code: str | None) -> str | None:
        account = self.accounts.get(code) if code else None
        return account.status if account else None

    def over_cap(self, code: str | None) -> bool:
        account = self.accounts.get(code) if code else None
        return bool(account) and self.soft_cap.over(account.used_5h, account.reset_5h, self._now())

    def handoff_target(self, code: str | None) -> str | None:
        """The account a fresh session would move to from `code`, which is over its soft cap: the proxy's top-ranked
        account, when that is another one and is under its own cap. None otherwise, and then the session stays put:
        the proxy picks a fresh session's account by its rank, so handing off when the top-ranked account is this
        one, or is capped too, would only land the fresh session on a capped account and hand off again."""
        ranked = sorted((a.rank, c) for c, a in self.accounts.items() if isinstance(a.rank, int))
        if not ranked:
            return None
        top = ranked[0][1]
        return top if top != code and not self.over_cap(top) else None


class ProxyClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None, soft_cap: SoftCap | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=10)
        self.soft_cap = soft_cap or SoftCap.from_env()
        self._sid = str(uuid.uuid4())  # the session id serves() asks with

    async def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        try:
            resp = await self.http.get(f"{self.base_url}{path}", params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ProxyUnavailable(f"proxy unreachable: {e}") from e
        if resp.status_code >= 500:
            raise ProxyUnavailable(f"proxy answered {resp.status_code}")
        return resp

    async def raw_usage(self) -> dict:
        resp = await self._get("/proxy/usage")
        resp.raise_for_status()
        return resp.json()

    async def perf(self, hours: float) -> list[dict]:
        resp = await self._get("/proxy/perf", params={"hours": f"{hours:g}"})
        resp.raise_for_status()
        return resp.json().get("rows") or []

    async def usage(self) -> Usage:
        data = await self.raw_usage()
        return Usage({code: AccountUsage(status=rec.get("status", ""), rank=rec.get("rank"),
                                         verified=bool(rec.get("verified")), used_5h=window_5h(rec)[0],
                                         reset_5h=window_5h(rec)[1])
                      for code, rec in data.items() if code != "_all" and isinstance(rec, dict)},
                     soft_cap=self.soft_cap, now=time.time())

    async def whoami(self, sid: str) -> str | None:
        resp = await self._get("/proxy/whoami", params={"sid": sid})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("account")

    async def serves(self, model: str) -> bool:
        """Whether a session can use `model` through the proxy: Anthropic's model lookup, which spends nothing. The
        proxy puts an account's token only on a request that carries a Claude Code session id, so it asks as one."""
        resp = await self._get(f"/v1/models/{model}", headers={
            "Authorization": f"Bearer {PLACEHOLDER_TOKEN}", "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20", "x-claude-code-session-id": self._sid})
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise ProxyUnavailable(f"proxy answered {resp.status_code} for model {model}")
        return True
