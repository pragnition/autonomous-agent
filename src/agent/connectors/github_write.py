"""The agent's GitHub client: draft PRs and assignees. It holds the only write token in the stack."""

from __future__ import annotations

import httpx

HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubWriter:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None, api: str = "https://api.github.com"):
        self.http = http or httpx.AsyncClient(timeout=30)
        self.api = api.rstrip("/")
        self.headers = {**HEADERS, "Authorization": f"Bearer {token}"}

    async def find_open_pr(self, repo: str, branch: str) -> int | None:
        owner = repo.split("/")[0]
        resp = await self.http.get(f"{self.api}/repos/{repo}/pulls", headers=self.headers,
                                   params={"head": f"{owner}:{branch}", "state": "open"})
        resp.raise_for_status()
        pulls = resp.json()
        return pulls[0]["number"] if pulls else None

    async def open_draft_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> tuple[int, str]:
        resp = await self.http.post(f"{self.api}/repos/{repo}/pulls", headers=self.headers,
                                    json={"title": title, "head": head, "base": base, "body": body, "draft": True})
        resp.raise_for_status()
        data = resp.json()
        return data["number"], data["html_url"]

    async def set_assignee(self, repo: str, number: int, login: str) -> None:
        resp = await self.http.post(f"{self.api}/repos/{repo}/issues/{number}/assignees", headers=self.headers,
                                    json={"assignees": [login]})
        resp.raise_for_status()
