"""HTTP client for sandboxd, the host service that runs Docker Sandboxes for core (spec v2 §5)."""

from __future__ import annotations

import httpx


class _Client:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, body: dict | None = None) -> httpx.Response:
        resp = await self.http.post(f"{self.base_url}{path}", json=body or {}, headers=self.headers)
        resp.raise_for_status()
        return resp

    async def health(self) -> bool:
        try:
            return (await self.http.get(f"{self.base_url}/health", timeout=5)).status_code == 200
        except httpx.HTTPError:
            return False


class SandboxdClient(_Client):
    async def ensure(self, name: str, profile: str, project: str | None, sandbox_token: str,
                     inject: dict[str, str]) -> None:
        await self._post("/ensure", {"name": name, "profile": profile, "project": project,
                                     "sandbox_token": sandbox_token, "inject": inject})

    async def stop(self, name: str) -> None:
        await self._post("/stop", {"name": name})

    async def rebuild(self, name: str) -> None:
        await self._post("/rebuild", {"name": name})

    async def exec(self, name: str, command: str) -> dict:
        return (await self._post("/exec", {"name": name, "command": command})).json()

    async def status(self) -> dict[str, dict]:
        resp = await self.http.get(f"{self.base_url}/status", headers=self.headers)
        resp.raise_for_status()
        return resp.json()
