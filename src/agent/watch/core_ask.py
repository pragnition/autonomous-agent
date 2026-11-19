"""Queue one turn into core's work queue, for handing Jensen a question or a review note from the host.

Self-contained on purpose: piped into the `agent-core` container's own Python on stdin
(`docker exec -i agent-core python - <args>`, see `agent.watch.host.Host.core_python`), the same way
`core_collect.py` reads the queue. `main` builds the turn's `Payloads` exactly as `scripts/smoke.py` does, so a
queued review turn gets the same system prompt, model and budgets as any other turn.
"""

from __future__ import annotations

import argparse
import base64
import sys
import uuid

from agent import db
from agent.core.sessions import WorkQueue

# Marks a turn as improve-jensen's own, the way a Slack-originated turn is marked "source": "slack" (see
# agent/core/conversations.py) — so core_collect.py's watcher can tell a review turn apart from a person's.
SOURCE = "improve-jensen"
ORIGIN = "improve-jensen review"
# A fresh conversation's id is this prefix plus 8 hex, which marks it as improve-jensen's own
# (see `agent.watch.state.is_improve_jensens_own`).
NEW_PREFIX = "V-review-"


def enqueue_turn(conn, payloads, conversation_id: str | None, text: str, new_prefix: str = NEW_PREFIX) -> str:
    """Queue `text` as a turn in `conversation_id`, or in a fresh `<new_prefix><8 hex>` conversation (no channel,
    the way `scripts/smoke.py` creates its own) when `conversation_id` is `None`. Raises `KeyError` for a
    `conversation_id` that doesn't exist.

    The new conversation (when one is created) and the queued item go in one explicit transaction — `conn` is
    autocommit (`isolation_level=None`, see `agent.db.connect`), so without this, a `payloads.turn` that raises
    would still leave the just-inserted conversation committed, with no turn ever queued into it."""
    conn.execute("BEGIN")
    try:
        if conversation_id is None:
            conversation_id, now = f"{new_prefix}{uuid.uuid4().hex[:8]}", db.utcnow()
            conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, is_primary, created_at, "
                         "last_active_at) VALUES (?, NULL, NULL, NULL, 0, ?, ?)", (conversation_id, now, now))

        convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if convo is None:
            raise KeyError(conversation_id)

        payload = payloads.turn(convo, text, None, unattended=True)
        payload["source"] = SOURCE
        payload["origin"] = ORIGIN
        item_id = WorkQueue(conn).enqueue(conversation_id, "turn", payload)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return item_id


def build_parser() -> argparse.ArgumentParser:
    """`--conversation ID` queues into that conversation; `--new` (or neither) opens a fresh one named
    `--new-prefix` plus 8 hex, `V-review-` unless given."""
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--conversation")
    target.add_argument("--new", action="store_true")
    parser.add_argument("--new-prefix", default=NEW_PREFIX)
    parser.add_argument("--text-b64", required=True)
    return parser


def main(argv: list[str]) -> int:
    from agent import paths
    from agent.config import load_agent_config, load_packs
    from agent.core.payloads import Payloads
    from agent.secrets import load_secrets

    args = build_parser().parse_args(argv)

    conn = db.connect(paths.DB_DIR / "agent.db")
    config = load_agent_config(paths.CONFIG_DIR / "agent.toml")
    payloads = Payloads(conn, config=config, packs=load_packs(paths.CONFIG_DIR, config), persona_text="",
                        config_dir=paths.CONFIG_DIR, secrets=load_secrets(paths.SECRETS_FILE))

    text = base64.b64decode(args.text_b64).decode()
    print(enqueue_turn(conn, payloads, args.conversation, text, new_prefix=args.new_prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
