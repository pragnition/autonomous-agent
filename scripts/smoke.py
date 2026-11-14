"""End-to-end smoke test on a real install: queue one turn and wait for the agent container to answer it. It
proves core → runner → Claude (through the account proxy) → result.

Run inside the core container with the stack up: `just smoke`. Posts nothing to Slack (the conversation has no thread).
"""

from __future__ import annotations

import sys
import time
import uuid

TIMEOUT_S = 5 * 60
PROMPT = "Reply with the single word pong, and nothing else."


def main() -> int:
    from agent import db, paths
    from agent.config import load_agent_config, load_packs
    from agent.core.payloads import Payloads
    from agent.core.sessions import WorkQueue
    from agent.secrets import load_secrets

    conn = db.connect(paths.DB_DIR / "agent.db")
    config = load_agent_config(paths.CONFIG_DIR / "agent.toml")
    payloads = Payloads(conn, config=config, packs=load_packs(paths.CONFIG_DIR, config), persona_text="",
                        config_dir=paths.CONFIG_DIR, secrets=load_secrets(paths.SECRETS_FILE))
    convo_id, now = f"V-smoke-{uuid.uuid4().hex[:8]}", db.utcnow()
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, is_primary, created_at, last_active_at) "
                 "VALUES (?, NULL, NULL, NULL, 0, ?, ?)", (convo_id, now, now))
    convo = conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()
    queue = WorkQueue(conn)
    item_id = queue.enqueue(convo_id, "turn", payloads.turn(convo, PROMPT, None, unattended=True))
    print(f"queued {item_id}; waiting for the agent container (up to {TIMEOUT_S // 60} min)", flush=True)
    deadline, last = time.time() + TIMEOUT_S, None
    while time.time() < deadline:
        item = queue.get(item_id)
        if item.status != last:
            print(f"  {item.status}", flush=True)
            last = item.status
        if item.status in ("done", "failed", "cancelled"):
            break
        time.sleep(3)
    else:
        print("smoke FAILED: timed out (is the agent container running? see `just logs agent`)")
        return 1
    text = ((item.result or {}).get("text") or "").strip()
    if item.status == "done" and "pong" in text.lower():
        print("smoke ok")
        return 0
    print(f"smoke FAILED: {item.status}: {(item.result or {}).get('error') or text[:500]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
