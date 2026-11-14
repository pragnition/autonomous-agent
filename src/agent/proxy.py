"""Client for the host's Claude account proxy (claude_proxy/), reached through this service's relay.

Only the answered-locally /proxy/ API is used here: usage per account (by code name), a session's pin, and for the
dashboard the raw usage reply and the 5-minute perf rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

PLACEHOLDER_TOKEN = "sk-ant-oat01-container-via-host-proxy"


class ProxyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class AccountUsage:
    status: str
    rank: int | None
    verified: bool


@dataclass(frozen=True)
class Usage:
    accounts: dict[str, AccountUsage]

    def has_capacity(self) -> bool:
        return any(a.rank is not None for a in self.accounts.values())

    def status_of(self, code: str | None) -> str | None:
        account = self.accounts.get(code) if code else None
        return account.status if account else None


class ProxyClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=10)

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            resp = await self.http.get(f"{self.base_url}{path}", params=params)
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
                                         verified=bool(rec.get("verified")))
                      for code, rec in data.items() if code != "_all" and isinstance(rec, dict)})

    async def whoami(self, sid: str) -> str | None:
        resp = await self._get("/proxy/whoami", params={"sid": sid})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("account")
