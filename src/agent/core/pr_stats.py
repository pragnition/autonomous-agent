"""PR outcome metrics: review and merge times, human edits, and reverts within 30 days."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from agent import tasks
from agent.config import Pack
from agent.connectors.github_read import GitHubReader
from agent.db import utcnow
from agent.tasks import Status, Task

REVERT_WINDOW = timedelta(days=30)


def _parse(ts: str | None) -> datetime | None:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if ts else None


class PrStatsPoller:
    def __init__(self, conn: sqlite3.Connection, reader: GitHubReader, *, agent_login: str, packs: dict[str, Pack],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 on_change: Callable[[Task], Awaitable[None]] | None = None):
        self.conn, self.reader, self.agent_login, self.packs, self.clock = conn, reader, agent_login, packs, clock
        self.on_change = on_change

    def _stats(self, task_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM pr_stats WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else {}

    async def update(self, task: Task) -> None:
        pack = self.packs.get(task.project)
        if pack is None or not task.pr_number:
            return
        repo, n = pack.project.repo, task.pr_number
        pr = await self.reader.pr(repo, n)
        reviews = [r for r in await self.reader.reviews(repo, n)
                   if (r.get("user") or {}).get("login") != self.agent_login and r.get("submitted_at")]
        commits = await self.reader.pr_commits(repo, n, skip_author=self.agent_login)
        human = [c for c in commits if (c.get("author") or {}).get("login") != self.agent_login]
        events = await self.reader.issue_events(repo, n)
        ready = next((e["created_at"] for e in events if e.get("event") == "ready_for_review"), None)
        if ready is None and not pr.get("draft"):
            ready = pr.get("created_at")
        prev = self._stats(task.id)
        values = {
            "repo": repo, "pr_number": n, "opened_at": pr.get("created_at"), "ready_at": ready,
            "first_human_review_at": min((r["submitted_at"] for r in reviews), default=None),
            "merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at"),
            "merge_sha": pr.get("merge_commit_sha") if pr.get("merged") else None,
            "human_commits": len(human),
            "human_lines_changed": sum(int((c.get("stats") or {}).get("additions") or 0) +
                                       int((c.get("stats") or {}).get("deletions") or 0) for c in human),
            "reverted_at": prev.get("reverted_at"), "revert_ref": prev.get("revert_ref"),
        }
        merged_at = _parse(values["merged_at"])
        if merged_at and not values["reverted_at"] and self.clock() - merged_at <= REVERT_WINDOW:
            branch = (pr.get("base") or {}).get("ref") or pack.project.default_branch
            for c in await self.reader.commits_since(repo, branch, values["merged_at"]):
                message = (c.get("commit") or {}).get("message", "")
                if (values["merge_sha"] and f"This reverts commit {values['merge_sha']}" in message) or \
                        message.startswith(f'Revert "{pr.get("title")}"'):
                    values["reverted_at"] = ((c.get("commit") or {}).get("author") or {}).get("date")
                    values["revert_ref"] = c.get("sha")
                    break
        cols = list(values)
        self.conn.execute(
            f"INSERT INTO pr_stats(task_id, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' * len(cols))}, ?) "
            f"ON CONFLICT(task_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}, "
            "updated_at = excluded.updated_at", (task.id, *values.values(), utcnow()))
        if task.status == Status.ready_for_review and pr.get("state") == "closed":
            to = Status.merged if pr.get("merged") else Status.closed
            changed = tasks.transition(self.conn, task.id, to, actor="github", data={"pr_number": n})
            if self.on_change:
                await self.on_change(changed)

    async def poll_all(self) -> list[str]:
        cutoff = (self.clock() - REVERT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN pr_stats s ON s.task_id = t.id WHERE t.pr_number IS NOT NULL AND "
            "(t.status = 'ready_for_review' OR (t.status = 'merged' AND s.reverted_at IS NULL AND "
            "(s.merged_at IS NULL OR s.merged_at >= ?))) ORDER BY t.id", (cutoff,)).fetchall()
        for row in rows:
            await self.update(tasks.get_task(self.conn, row["id"]))
        return [r["id"] for r in rows]
