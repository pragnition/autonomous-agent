"""Where a handful of queued turns stand, for the yardstick on the host waiting on the turns it queued.

Self-contained on purpose, like `core_collect.py`: piped into the `agent-core` container's own Python on stdin
(`docker exec -i agent-core python - --ids a,b,c`, see `agent.watch.host.Host.core_python`) and opening core's DB
read-only, so improve-jensen can change how it waits without ever rebuilding or redeploying core's image. It
needs nothing beyond the standard library. It's also plain enough to import directly in tests, against a tmp DB
built with `agent.db.create_schema`.

Prints `{id: {status, outcome, latest}}` for each asked id core knows (an unknown id is left out, so the caller
keeps waiting on it until its own time limit), and never a turn's text. A turn isn't finished just because its
own work item is: one a time or step limit cut off is carried on by a new turn in the same conversation, whose
payload `follows` the one before, so each id reports the newest turn of its own chain (`latest`). A turn a usage
limit cut off waits for an account with capacity before it is carried on (an unresumed `limit_waits` row): that
reads `waiting`, not finished.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

_DB_PATH = "/data/db/agent.db"

_FINISHED_STATUSES = ("done", "failed", "cancelled")
# A chain longer than this is broken data, not a real run of continuations; stop following it.
_MAX_CHAIN = 50


def _outcome(raw: str | None) -> str | None:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return data.get("outcome") if isinstance(data, dict) else None


def _next_in_chain(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, conversation_id, status, result FROM work_items WHERE conversation_id IS ? AND kind = 'turn' "
        "AND id != ? AND json_extract(payload, '$.follows') = ? ORDER BY created_at, rowid LIMIT 1",
        (row["conversation_id"], row["id"], row["id"])).fetchone()


def _waiting_on_limit(conn: sqlite3.Connection, item_id: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM limit_waits WHERE work_item_id = ? AND resumed_at IS NULL",
                           (item_id,)).fetchone()
    except sqlite3.OperationalError:  # a DB from before usage-limit waits existed
        return False
    return row is not None


def items(conn: sqlite3.Connection, ids: list[str]) -> dict:
    out = {}
    for item_id in ids:
        row = conn.execute("SELECT id, conversation_id, status, result FROM work_items WHERE id = ?",
                           (item_id,)).fetchone()
        if row is None:
            continue
        for _ in range(_MAX_CHAIN):
            following = _next_in_chain(conn, row)
            if following is None:
                break
            row = following
        status = row["status"]
        if status in _FINISHED_STATUSES and _waiting_on_limit(conn, row["id"]):
            status = "waiting"
        out[item_id] = {"status": status, "outcome": _outcome(row["result"]), "latest": row["id"]}
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", required=True)
    args = parser.parse_args(argv)

    ids = [item_id for item_id in args.ids.split(",") if item_id]
    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # core writes this DB concurrently; wait rather than fail on a lock
    print(json.dumps(items(conn, ids)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
