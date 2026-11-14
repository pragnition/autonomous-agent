import json

import httpx
import pytest

from agent import db
from agent.connectors.linear import LinearClient
from agent.core.linear_sessions import LinearAgentSessions
from agent.core.sessions import WorkQueue


class FakePayloads:
    def turn(self, convo, text, actor, *, unattended=False):
        return {"text": text, "actor": actor, "unattended": unattended}


class FakeClient:
    def __init__(self):
        self.activities = []

    async def agent_activity(self, session_id, type, body):
        self.activities.append((session_id, type, body))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def sessions(conn, client):
    return LinearAgentSessions(conn, queue=WorkQueue(conn), payloads=FakePayloads(), client=client)


def event(action, session="S1", **extra):
    return {"type": "AgentSessionEvent", "action": action, "agentSession": {"id": session}, **extra}


def turns(conn):
    return [json.loads(r["payload"]) | {"conversation_id": r["conversation_id"]}
            for r in conn.execute("SELECT * FROM work_items WHERE kind = 'turn' ORDER BY rowid")]


async def test_created_acks_and_passes_the_event_to_the_agent(sessions, client, conn):
    await sessions.on_event(event("created", promptContext="fix the login bug"))
    assert client.activities == [("S1", "thought", "On it.")]
    [turn] = turns(conn)
    assert "fix the login bug" in turn["text"]


async def test_turns_start_with_the_issue_and_its_link(sessions, conn):
    issue = {"id": "i7", "identifier": "SAM-7", "title": "Export", "url": "https://linear.app/x/issue/SAM-7"}
    await sessions.on_event(event("created", agentSession={"id": "S1", "issue": issue}))
    await sessions.on_event(event("prompted", agentSession={"id": "S1", "issue": issue}))
    await sessions.on_event(event("created", session="S2"))  # an event without its issue
    assert [t["origin"] for t in turns(conn)] == ["[Linear SAM-7 · https://linear.app/x/issue/SAM-7]"] * 2 + \
        ["[Linear]"]


async def test_webhook_records_the_issue(sessions, conn):
    issue = {"id": "i7", "identifier": "SAM-7", "title": "Export", "url": "https://linear.app/x/issue/SAM-7"}
    await sessions.on_event(event("created", agentSession={"id": "S1", "issue": issue,
                                                           "createdAt": "2026-11-14T10:00:00.123Z"}))
    await sessions.on_event(event("created", session="S2"))  # no issue: nothing to record
    [row] = conn.execute("SELECT * FROM linear_issues").fetchall()
    assert (row["identifier"], row["handed_at"]) == ("SAM-7", "2026-11-14T10:00:00Z")
    [link] = conn.execute("SELECT conversation_id FROM linear_issue_conversations").fetchall()
    assert link[0] == turns(conn)[0]["conversation_id"]


async def test_prompted_resumes_the_same_conversation(sessions, conn):
    await sessions.on_event(event("created"))
    await sessions.on_event(event("created"))                 # Linear redelivers: ignored
    await sessions.on_event(event("prompted", agentActivity={"body": "also logout"}))
    first, second = turns(conn)
    assert first["conversation_id"] == second["conversation_id"] and "also logout" in second["text"]


async def test_unknown_or_other_events_do_nothing(sessions, client, conn):
    await sessions.on_event(event("prompted", session="nope"))
    await sessions.on_event({"type": "Issue", "action": "update"})
    assert turns(conn) == [] and client.activities == []


async def test_ack_failure_does_not_stop_the_turn(sessions, client, conn):
    async def boom(*a):
        raise RuntimeError("linear down")
    client.agent_activity = boom
    await sessions.on_event(event("created"))
    assert len(turns(conn)) == 1


async def test_client_uses_app_token_when_installed():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.headers["authorization"], body["variables"]))
        return httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})

    token = {"value": None}

    async def provider():
        return token["value"]

    c = LinearClient("lin_api_key", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), token=provider)
    await c.agent_activity("S1", "thought", "On it")
    token["value"] = "app-tok"
    await c.agent_activity("S1", "thought", "On it")
    assert [s[0] for s in seen] == ["lin_api_key", "Bearer app-tok"]
    assert seen[0][1] == {"input": {"agentSessionId": "S1", "content": {"type": "thought", "body": "On it"}}}
