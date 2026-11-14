"""The agent container's runner token.

core keeps the runner token in a one-line file the agent container reads (data/runner/runner-token). The token
stays while it is valid for at least another week, so the runner keeps it across core restarts; a revoked,
expired or soon-expiring one is replaced, and the agent container picks the new one up from the file.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.core.sessions import mint_token, resolve_token

RUNNER_TOKEN_TTL_S = 60 * 86400
RENEW_BEFORE_S = 7 * 86400


def read_runner_token(path: Path) -> str:
    """The token in the file, or "" when there is none."""
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return ""


def sync_runner_token(conn: sqlite3.Connection, path: Path, *, owner_uid: int | None = None,
                      ttl_s: int = RUNNER_TOKEN_TTL_S, renew_before_s: int = RENEW_BEFORE_S,
                      now: datetime | None = None) -> str:
    check_at = (now or datetime.now(UTC)) + timedelta(seconds=renew_before_s)
    current = read_runner_token(path)
    info = resolve_token(conn, current, now=check_at) if current else None
    token = current if info is not None and info.kind == "runner" else mint_token(conn, "runner", ttl_s=ttl_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(token + "\n")
    os.chmod(tmp, 0o600)
    if owner_uid is not None:
        os.chown(tmp, owner_uid, -1)
    os.replace(tmp, path)
    return token
