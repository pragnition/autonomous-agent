"""The runner's connection to core's agent API. The runner token pulls work and bundles; each work item's session
token does everything else. A 401 means the item was finished, cancelled or lost."""

from __future__ import annotations

import httpx


class SessionRevoked(Exception):
    pass


class CoreLink:
    def __init__(self, base_url: str, runner_token: str, http: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        self.runner_token = runner_token
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))

    @staticmethod
    def _auth(tok: str) -> dict:
        return {"Authorization": f"Bearer {tok}"}

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code == 401:
            raise SessionRevoked()
        resp.raise_for_status()
        return resp

    async def _post(self, path: str, tok: str, body: dict) -> dict:
        return self._check(await self.http.post(f"{self.base}{path}", headers=self._auth(tok), json=body)).json()

    async def pull(self) -> dict | None:
        resp = self._check(await self.http.post(f"{self.base}/agent/pull", headers=self._auth(self.runner_token),
                                                timeout=40))
        return None if resp.status_code == 204 else resp.json()

    async def heartbeat(self, tok: str) -> bool:
        return bool((await self._post("/agent/heartbeat", tok, {})).get("cancel"))

    async def event(self, tok: str, event: dict) -> bool:
        return bool((await self._post("/agent/events", tok, {"event": event})).get("cancel"))

    async def tool(self, tok: str, name: str, args: dict) -> dict:
        resp = self._check(await self.http.post(f"{self.base}/agent/tool", headers=self._auth(tok),
                                                json={"name": name, "args": args}, timeout=20 * 60))
        return resp.json()

    async def files(self, tok: str, items: list[tuple[str, bytes]], text: str) -> bool:
        """Whether the files reached the conversation's Slack thread."""
        files = [("file", (name, data)) for name, data in items]
        resp = self._check(await self.http.post(f"{self.base}/agent/files", headers=self._auth(tok),
                                                data={"text": text}, files=files))
        return resp.json().get("posted", True)

    async def result(self, tok: str, result: dict) -> None:
        await self._post("/agent/result", tok, {"result": result})

    async def bundle(self) -> tuple[str, bytes]:
        resp = self._check(await self.http.get(f"{self.base}/agent/bundle", headers=self._auth(self.runner_token)))
        return resp.headers["X-Bundle-Version"], resp.content
