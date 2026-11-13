"""The agent container's runner tokens (spec v3 O8).

core keeps one sandbox token per queue in a file the agent container reads: `{queue: token}`. A token stays
while it is valid for at least another week, so the agent's runners keep theirs across core restarts; a revoked,
expired or soon-expiring one is replaced, and the agent's runners pick the new one up from the file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.core.sessions import mint_token, resolve_token

SANDBOX_TOKEN_TTL_S = 60 * 86400
RENEW_BEFORE_S = 7 * 86400


def queue_names(packs: dict) -> list[str]:
    return [*(f"code-{project}" for project in sorted(packs)), "general"]


def _read(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def sync_runner_tokens(conn: sqlite3.Connection, path: Path, names: list[str], *, owner_uid: int | None = None,
                       ttl_s: int = SANDBOX_TOKEN_TTL_S, renew_before_s: int = RENEW_BEFORE_S,
                       now: datetime | None = None) -> dict[str, str]:
    check_at = (now or datetime.now(UTC)) + timedelta(seconds=renew_before_s)
    old, tokens = _read(path), {}
    for name in names:
        info = resolve_token(conn, old[name], now=check_at) if name in old else None
        keep = info is not None and info.kind == "sandbox" and info.sandbox == name
        tokens[name] = old[name] if keep else mint_token(conn, "sandbox", sandbox=name, work_item_id=None,
                                                         ttl_s=ttl_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(tokens, indent=1))
    os.chmod(tmp, 0o600)
    if owner_uid is not None:
        os.chown(tmp, owner_uid, -1)
    os.replace(tmp, path)
    return tokens
