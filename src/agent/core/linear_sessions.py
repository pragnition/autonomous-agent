"""Linear agent sessions: a Linear webhook is one more way a message reaches Claude Code.

Code acks at once (Linear marks a session unresponsive after about 10 s) and hands the raw event to the agent.
Follow-ups in the same Linear session resume the same Claude session. Everything else (reading the issue, picking
the project, replying, posting activities) the agent does itself with the Linear MCP server.
"""

from __future__ import annotations

import json
import logging

from agent import db
from agent.core.conversations import create_conversation, origin_line
from agent.core.linear_issues import record_issue

log = logging.getLogger(__name__)
SESSION_KEY = "linear_session:{}"


def linear_requester(body: dict, config=None) -> str | None:
    """Who asked, from a session webhook: the person who prompted it, else the one who started it. A team member
    (matched by their Linear email) is named by their person key, anyone else by their Linear name."""
    user = (body.get("agentActivity") or {}).get("user") if body.get("action") == "prompted" else None
    user = user or (body.get("agentSession") or {}).get("creator") or {}
    person = config.person_by_linear(user.get("email")) if config is not None else None
    return person.key if person else user.get("name")


class LinearAgentSessions:
    def __init__(self, conn, *, queue, payloads, client, config=None):
        self.conn, self.queue, self.payloads, self.client, self.config = conn, queue, payloads, client, config

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
            # no project yet, so no branch to name: the agent picks the project itself
            convo_id = create_conversation(self.conn, channel=None, thread_ts=None, project=None)
            db.set_setting(self.conn, SESSION_KEY.format(sid), convo_id)
        elif body.get("action") != "prompted" or convo_id is None:
            return  # a redelivered `created`, or an event for a session we never saw
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()
        text = f"Linear agent session event:\n{json.dumps(body, indent=2)}"
        payload = self.payloads.turn(convo, text, None, unattended=True)
        try:
            record_issue(self.conn, body["agentSession"], convo_id)   # for the KPIs; never at the turn's expense
        except Exception:
            log.exception("couldn't record the issue of Linear session %s", sid)
        issue = body["agentSession"].get("issue") or {}
        payload["origin"] = origin_line(f"Linear {issue['identifier']}" if issue.get("identifier") else "Linear",
                                        link=issue.get("url"))
        payload.update(source="linear", requester=linear_requester(body, self.config), follows=None)
        self.queue.enqueue(convo_id, "turn", payload)
