"""Linear agent sessions (spec v3 §8): a Linear webhook is just another way a message reaches Claude Code.

Code acks at once (Linear marks a session unresponsive after about 10 s) and hands the raw event to the agent.
Follow-ups in the same Linear session resume the same Claude session. Everything else (reading the issue, picking
the project, replying, posting activities) the agent does itself with the Linear MCP server.
"""

from __future__ import annotations

import json
import logging
import uuid

from agent import db

log = logging.getLogger(__name__)
SESSION_KEY = "linear_session:{}"


class LinearAgentSessions:
    def __init__(self, conn, *, queue, payloads, client):
        self.conn, self.queue, self.payloads, self.client = conn, queue, payloads, client

    async def on_event(self, body: dict) -> None:
        sid = (body.get("agentSession") or {}).get("id")
        if body.get("type") != "AgentSessionEvent" or not sid:
            return
        convo_id = db.get_setting(self.conn, SESSION_KEY.format(sid))
        if body.get("action") == "created" and convo_id is None:
            try:
                await self.client.agent_activity(sid, "thought", "On it.")
            except Exception:
                log.exception("couldn't ack Linear session %s", sid)
            convo_id, now = f"V-{uuid.uuid4().hex[:12]}", db.utcnow()
            self.conn.execute("INSERT INTO conversations(id, sandbox, is_primary, created_at, last_active_at) "
                              "VALUES (?, 'general', 1, ?, ?)", (convo_id, now, now))
            db.set_setting(self.conn, SESSION_KEY.format(sid), convo_id)
        elif body.get("action") != "prompted" or convo_id is None:
            return  # a redelivered `created`, or an event for a session we never saw
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()
        text = f"Linear agent session event:\n{json.dumps(body, indent=2)}"
        self.queue.enqueue(convo_id, convo["sandbox"], "turn", self.payloads.turn(convo, text, None, unattended=True))
