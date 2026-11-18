"""A read-only snapshot of core's work queue, for the hourly watch running on the host.

Self-contained on purpose: piped into the `agent-core` container's own Python on stdin
(`docker exec -i agent-core python - <args>`, see `agent.watch.host.Host.core_python`) rather than imported there,
so improve-jensen can change what it watches without ever rebuilding or redeploying core's image. It only imports
`agent.db`, which already ships in every core image. It's also plain enough to import directly in tests, against a
tmp DB built with `agent.db.create_schema`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from agent import db

_DB_PATH = "/data/db/agent.db"

# A turn still in flight (queued, claimed, ...) isn't finished work yet; only these are.
_FINISHED_STATUSES = ("done", "failed", "cancelled")
# What a watcher needs from a payload/result — never the system prompt, resume ids or test secrets.
_PAYLOAD_KEEP = ("text", "actor", "unattended", "source", "origin", "continuation", "project")
_RESULT_KEEP = ("text", "outcome", "error")


def _reduced(raw: str | None, keep: tuple[str, ...]) -> dict:
    data = json.loads(raw) if raw else {}
    return {key: data[key] for key in keep if key in data}


def collect(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Finished work items with `updated_at` in `(since, until]`, their conversations and the long projects being
    built, the usage watch's current pool, and the queue's depth (`claimed`, not limited to the window).

    A `V-smoke-*` conversation (the end-to-end smoke test) never counts as real work and is left out of `items`.
    """
    rows = conn.execute(
        "SELECT id, conversation_id, kind, status, payload, result, delivered_into, created_at, updated_at "
        "FROM work_items WHERE updated_at > ? AND updated_at <= ? AND status IN (?, ?, ?) ORDER BY updated_at",
        (since, until, *_FINISHED_STATUSES),
    ).fetchall()

    items = []
    conversation_ids: set[str] = set()
    for row in rows:
        conversation_id = row["conversation_id"]
        if conversation_id and conversation_id.startswith("V-smoke"):
            continue
        items.append({
            "id": row["id"], "conversation_id": conversation_id, "kind": row["kind"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "delivered_into": row["delivered_into"],
            "payload": _reduced(row["payload"], _PAYLOAD_KEEP), "result": _reduced(row["result"], _RESULT_KEEP),
        })
        if conversation_id:
            conversation_ids.add(conversation_id)

    conversations = {}
    for conversation_id in conversation_ids:
        convo = conn.execute("SELECT channel, thread_ts, project FROM conversations WHERE id = ?",
                              (conversation_id,)).fetchone()
        if convo is not None:
            conversations[conversation_id] = {"channel": convo["channel"], "thread_ts": convo["thread_ts"],
                                               "project": convo["project"]}

    projects = [{"name": r["name"], "repo": r["repo"], "conversation_id": r["conversation_id"],
                 "status": r["status"]} for r in
                conn.execute("SELECT name, repo, conversation_id, status FROM projects")]

    setting = db.get_setting(conn, "usage_watch")
    pool = (json.loads(setting).get("pool") if setting else None) or "normal"

    claimed = conn.execute("SELECT COUNT(*) FROM work_items WHERE status = 'claimed'").fetchone()[0]

    return {"items": items, "conversations": conversations, "projects": projects, "pool": pool, "claimed": claimed}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(json.dumps(collect(conn, args.since, args.until)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
