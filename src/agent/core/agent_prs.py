"""The agent's own PRs, for the KPIs: core finds them by searching GitHub for the agent's login, keeps each one
current until it's merged or closed, and ties it to a handed-over Linear issue whose key the PR mentions (title, body
or branch). Times are GitHub's own; the agent records nothing."""

from __future__ import annotations

import logging
import re
import sqlite3

from agent import db
from agent.core.linear_issues import _iso

log = logging.getLogger(__name__)
FLOOR_KEY = "kpi_floor"   # KPIs count only what came after this, so nothing from before core watched is half-known


def kpi_floor(conn: sqlite3.Connection) -> str:
    floor = db.get_setting(conn, FLOOR_KEY)
    if floor is None:
        floor = db.utcnow()
        db.set_setting(conn, FLOOR_KEY, floor)
    return floor


def match_issue(texts: list[str | None], identifiers: list[str]) -> str | None:
    """The first identifier that appears as a whole token: `pra-423-x` mentions PRA-423, `PRA-4230` doesn't."""
    for ident in sorted(identifiers, key=len, reverse=True):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ident)}(?![0-9])", re.I)
        if any(t and pattern.search(t) for t in texts):
            return ident
    return None


class AgentPrPoller:
    def __init__(self, conn: sqlite3.Connection, reader, *, login: str):
        self.conn, self.reader, self.login = conn, reader, login

    async def poll(self) -> int:
        hits = await self.reader.search_prs(f"is:pr author:{self.login} created:>={kpi_floor(self.conn)}")
        identifiers = [r[0] for r in self.conn.execute("SELECT identifier FROM linear_issues")]
        for h in hits:
            repo = "/".join(h["repository_url"].rstrip("/").split("/")[-2:])
            n = h["number"]
            known = self.conn.execute("SELECT * FROM agent_prs WHERE repo = ? AND number = ?", (repo, n)).fetchone()
            if known and (known["merged_at"] or known["closed_at"]):
                continue
            try:
                pr = await self.reader.pr(repo, n)
            except Exception:
                log.exception("couldn't read PR %s#%s", repo, n)
                continue
            head = (pr.get("head") or {}).get("ref")
            issue = (known["issue_identifier"] if known else None) or \
                match_issue([pr.get("title"), pr.get("body"), head], identifiers)
            self.conn.execute(
                "INSERT INTO agent_prs(repo, number, url, title, head, base, opened_at, merged_at, closed_at, "
                "issue_identifier, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, number) DO UPDATE SET url = excluded.url, title = excluded.title, "
                "head = excluded.head, base = excluded.base, opened_at = excluded.opened_at, "
                "merged_at = excluded.merged_at, closed_at = excluded.closed_at, "
                "issue_identifier = excluded.issue_identifier, updated_at = excluded.updated_at",
                (repo, n, pr.get("html_url"), pr.get("title"), head, (pr.get("base") or {}).get("ref"),
                 _iso(pr.get("created_at")), _iso(pr.get("merged_at")), _iso(pr.get("closed_at")), issue,
                 db.utcnow()))
        return len(hits)
