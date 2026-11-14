import json
from pathlib import Path

import httpx
import pytest

from agent import db, tasks
from agent.config import load_agent_config, load_packs
from agent.connectors.linear import LinearClient, LinearPoller
from agent.tasks import Status

EXAMPLES = Path(__file__).parent.parent / "examples" / "config"


def issue(n, email="alice@example.com", labels=("jensen",)):
    return {"id": f"iss-{n}", "identifier": f"SAM-{n}", "title": f"Issue {n}", "description": "Do the thing",
            "url": f"https://linear.app/x/issue/SAM-{n}", "creator": {"email": email},
            "labels": {"nodes": [{"name": l} for l in labels]}}


class Linear:
    def __init__(self, issues):
        self.issues, self.calls = issues, []

    def handler(self, req):
        body = json.loads(req.content)
        self.calls.append((body["operationName"], body["variables"], req.headers.get("authorization")))
        op = body["operationName"]
        if op == "LabelledIssues":
            return httpx.Response(200, json={"data": {"issues": {"nodes": self.issues}}})
        if op == "TeamStates":
            return httpx.Response(200, json={"data": {"teams": {"nodes": [{"id": "team-1", "states": {"nodes": [
                {"id": "st-ip", "name": "In Progress"}, {"id": "st-rev", "name": "In Review"}]}}]}}})
        return httpx.Response(200, json={"data": {"ok": {"success": True}}})

    def client(self):
        return LinearClient("lin_api_key", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    config = load_agent_config(EXAMPLES / "agent.toml")
    return conn, config, load_packs(EXAMPLES, config)


def poller(env, linear):
    conn, config, packs = env
    return LinearPoller(conn, linear.client(), config=config, packs=packs, agent_label="jensen")


async def test_poll_creates_tasks_with_mapped_requester(env):
    conn = env[0]
    lin = Linear([issue(1), issue(2, labels=("jensen", "Bug"))])
    created = await poller(env, lin).poll()
    t1, t2 = (tasks.get_task(conn, i) for i in created)
    assert (t1.source, t1.source_ref, t1.requester, t1.owner, t1.linear_issue_id, t1.project) == \
        ("linear", "SAM-1", "alice", "alice", "iss-1", "sample")
    assert t1.slack_channel == "#jensen" and "https://linear.app/x/issue/SAM-1" in t1.description
    assert (t1.kind, t2.kind) == ("feature", "bug")
    assert lin.calls[0][1] == {"label": "jensen", "team": "SAM"}


async def test_poll_skips_issues_that_already_have_a_task(env):
    conn = env[0]
    lin = Linear([issue(1)])
    p = poller(env, lin)
    [tid] = await p.poll()
    assert await p.poll() == []
    tasks.transition(conn, tid, Status.cancelled, actor="x")
    assert await p.poll() == []  # one task per issue; a cancelled one is not recreated


async def test_unmapped_creator_owner_is_pack_owner(env):
    conn = env[0]
    [tid] = await poller(env, Linear([issue(1, email="stranger@example.com")])).poll()
    t = tasks.get_task(conn, tid)
    assert (t.requester, t.owner) == (None, "sam")


async def test_sync_sets_mapped_state_and_comments_pr(env):
    conn = env[0]
    lin = Linear([issue(1)])
    p = poller(env, lin)
    [tid] = await p.poll()
    tasks.transition(conn, tid, Status.working, actor="x")
    await p.sync(tasks.get_task(conn, tid))
    assert ("IssueUpdate", {"id": "iss-1", "stateId": "st-ip"}) in [(c[0], c[1]) for c in lin.calls]
    tasks.set_fields(conn, tid, pr_url="https://github.com/x/y/pull/7")
    tasks.transition(conn, tid, Status.ready_for_review, actor="x")
    await p.sync(tasks.get_task(conn, tid))
    comment = [c for c in lin.calls if c[0] == "CommentCreate"][-1]
    assert comment[1]["issueId"] == "iss-1" and "https://github.com/x/y/pull/7" in comment[1]["body"]
    assert [c[0] for c in lin.calls].count("TeamStates") == 1  # states are cached per team


async def test_sync_unmapped_status_noop(env):
    conn = env[0]
    lin = Linear([issue(1)])
    p = poller(env, lin)
    [tid] = await p.poll()
    before = len(lin.calls)
    await p.sync(tasks.get_task(conn, tid))  # "queued" has no mapping in the sample pack
    assert len(lin.calls) == before


async def test_auth_header_is_raw_key(env):
    lin = Linear([])
    await poller(env, lin).poll()
    assert lin.calls[0][2] == "lin_api_key"


async def test_graphql_errors_raise(env):
    def handler(req):
        return httpx.Response(200, json={"errors": [{"message": "bad"}]})
    client = LinearClient("k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError):
        await client.labelled_issues("jensen", "SAM")


async def test_team_state_names(env):
    names = await Linear([]).client().team_state_names("SAM")
    assert names == {"In Progress", "In Review"}


async def test_issue_state_reads_the_state_type_and_close_times():
    def handler(req):
        body = json.loads(req.content)
        assert body["operationName"] == "IssueState" and body["variables"] == {"id": "iss-1"}
        return httpx.Response(200, json={"data": {"issue": {"state": {"type": "completed"},
                                                            "completedAt": "2026-11-15T09:30:00.500Z",
                                                            "canceledAt": None}}})
    client = LinearClient("k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.issue_state("iss-1") == {"state_type": "completed",
                                                 "completed_at": "2026-11-15T09:30:00.500Z", "canceled_at": None}
