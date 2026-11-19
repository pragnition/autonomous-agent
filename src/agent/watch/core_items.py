"""Where a handful of queued turns stand, for the yardstick on the host waiting on the turns it queued; and the
one way it gives up on a turn that never started.

Self-contained on purpose, like `core_collect.py`: piped into the `agent-core` container's own Python on stdin
(`docker exec -i agent-core python - --ids a,b,c`, see `agent.watch.host.Host.core_python`) and opening core's DB
read-only, so improve-jensen can change how it waits without ever rebuilding or redeploying core's image. Reading
needs nothing beyond the standard library. It's also plain enough to import directly in tests, against a tmp DB
built with `agent.db.create_schema`.

`--cancel ID` opens the DB for writing instead and cancels that turn through core's own `WorkQueue.cancel` (which
`agent-core` ships), but only while it is still `queued` in a yardstick conversation (`V-yardstick-`): a turn the
yardstick timed out before it ever started would otherwise still start, long after the run gave up on it. A
person's turn, or one already running or finished, is never touched. Prints `{"cancelled": true|false}`.

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
# The only conversations whose turns `--cancel` may touch.
_CANCEL_PREFIX = "V-yardstick-"
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


def cancel(conn: sqlite3.Connection, item_id: str) -> bool:
    """Cancel the turn `item_id` if it is still `queued` in a yardstick conversation; whether it was. `conn` must
    be writable, in autocommit mode (`agent.db.connect`), the way `WorkQueue` expects it."""
    from agent.core.sessions import WorkQueue

    row = conn.execute("SELECT conversation_id, status FROM work_items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["status"] != "queued" or not str(row["conversation_id"] or "").startswith(_CANCEL_PREFIX):
        return False
    queue = WorkQueue(conn)
    queue.cancel(item_id)
    item = queue.get(item_id)
    return item is not None and item.status == "cancelled"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ids")
    target.add_argument("--cancel")
    args = parser.parse_args(argv)

    if args.cancel:
        from agent import db

        print(json.dumps({"cancelled": cancel(db.connect(_DB_PATH), args.cancel)}))
        return 0

    ids = [item_id for item_id in args.ids.split(",") if item_id]
    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")  # core writes this DB concurrently; wait rather than fail on a lock
    print(json.dumps(items(conn, ids)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
