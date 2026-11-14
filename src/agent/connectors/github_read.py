"""Core's read-only GitHub client: PR state and metrics, plus the agent's GitHub read tools."""

from __future__ import annotations

import httpx

HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubReader:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None, api: str = "https://api.github.com"):
        self.http = http or httpx.AsyncClient(timeout=30)
        self.api = api.rstrip("/")
        self.headers = {**HEADERS, "Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict | None = None):
        resp = await self.http.get(f"{self.api}{path}", headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def pr(self, repo: str, n: int) -> dict:
        return await self._get(f"/repos/{repo}/pulls/{n}")

    async def reviews(self, repo: str, n: int) -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls/{n}/reviews", {"per_page": 100})

    async def pr_commits(self, repo: str, n: int, *, skip_author: str | None = None) -> list[dict]:
        commits = await self._get(f"/repos/{repo}/pulls/{n}/commits", {"per_page": 100})
        for c in commits:
            if skip_author and (c.get("author") or {}).get("login") == skip_author:
                continue
            c["stats"] = (await self._get(f"/repos/{repo}/commits/{c['sha']}")).get("stats", {})
        return commits

    async def issue_events(self, repo: str, n: int) -> list[dict]:
        return await self._get(f"/repos/{repo}/issues/{n}/events", {"per_page": 100})

    async def commits_since(self, repo: str, branch: str, since_iso: str) -> list[dict]:
        return await self._get(f"/repos/{repo}/commits", {"sha": branch, "since": since_iso, "per_page": 100})

    async def can_read(self, repo: str) -> bool:
        resp = await self.http.get(f"{self.api}/repos/{repo}", headers=self.headers)
        return resp.status_code == 200

    async def list_prs(self, repo: str, state: str = "open") -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls", {"state": state, "per_page": 30})

    async def checks(self, repo: str, ref: str) -> list[dict]:
        return (await self._get(f"/repos/{repo}/commits/{ref}/check-runs", {"per_page": 100})).get("check_runs", [])

    async def job_log(self, repo: str, job_id: int | str, max_chars: int = 20000) -> str:
        resp = await self.http.get(f"{self.api}/repos/{repo}/actions/jobs/{job_id}/logs", headers=self.headers,
                                   follow_redirects=True)
        resp.raise_for_status()
        return resp.text[-max_chars:]

    async def issue(self, repo: str, n: int) -> dict:
        issue = await self._get(f"/repos/{repo}/issues/{n}")
        issue["comments_list"] = await self._get(f"/repos/{repo}/issues/{n}/comments", {"per_page": 50})
        return issue

    async def file_at(self, repo: str, path: str, ref: str) -> str:
        resp = await self.http.get(f"{self.api}/repos/{repo}/contents/{path}", params={"ref": ref},
                                   headers={**self.headers, "Accept": "application/vnd.github.raw"})
        resp.raise_for_status()
        return resp.text
