import json

import httpx
import pytest

from agent import db, tasks
from agent.connectors.github_read import GitHubReader
from agent.connectors.linear import LinearClient
from agent.core.agent_tools import AgentToolRouter
from agent.core.sessions import WorkQueue
from agent.core.tracker_tools import register_tracker_tools
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

LOG = "x" * 30000 + "ERROR test_export failed"


def github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/o/r/pulls":
        return httpx.Response(200, json=[{"number": 5, "title": "Fix export", "draft": True, "user": {"login": "agent"},
                                          "head": {"ref": "agent/t1-x"}, "base": {"ref": "main"}}])
    if path == "/repos/o/r/commits/abc/check-runs":
        return httpx.Response(200, json={"check_runs": [{"name": "test", "status": "completed",
                                                         "conclusion": "failure", "details_url": "https://ci/1"}]})
    if path == "/repos/o/r/actions/jobs/9/logs":
        return httpx.Response(200, text=LOG)
    if path == "/repos/o/r/issues/12":
        return httpx.Response(200, json={"number": 12, "title": "Crash", "state": "open", "body": "It crashes",
                                         "user": {"login": "bob-gh"}})
    if path == "/repos/o/r/issues/12/comments":
        return httpx.Response(200, json=[{"user": {"login": "ann-gh"}, "body": "seen it too"}])
    if path == "/repos/o/r/contents/src/app.py":
        assert request.url.params["ref"] == "main"
        return httpx.Response(200, text="print('hi')\n")
    return httpx.Response(404, json={"message": "not found"})


def linear_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    op = body["operationName"]
    if op == "Issue":
        return httpx.Response(200, json={"data": {"issue": {
            "identifier": "SAM-7", "title": "Export broken", "description": "Steps...", "url": "https://linear/SAM-7",
            "state": {"name": "Todo"},
            "comments": {"nodes": [{"body": "happens daily", "user": {"name": "Bob"}, "createdAt": "2026-11-12"}]}}}})
    if op == "TeamStates":
        return httpx.Response(200, json={"data": {"teams": {"nodes": [{"id": "t", "states": {"nodes": [
            {"id": "s1", "name": "In Progress"}, {"id": "s2", "name": "In Review"}]}}]}}})
    linear_handler.calls.append((op, body["variables"]))
    return httpx.Response(200, json={"data": {"ok": {"success": True}}})


linear_handler.calls = []


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def router(conn, tmp_path):
    r = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                        slack=FakeSlack(), notifier=FakeNotifier(),
                        secret_values=[])
    reader = GitHubReader("t", http=httpx.AsyncClient(transport=httpx.MockTransport(github_handler)),
                          api="https://api.github.test")
    linear = LinearClient("k", http=httpx.AsyncClient(transport=httpx.MockTransport(linear_handler)),
                          url="https://linear.test/graphql")
    register_tracker_tools(r, reader=reader, linear=linear)
    linear_handler.calls.clear()
    return r


@pytest.fixture
def ctx(router, conn):
    t = tasks.create_task(conn, project="sample", title="t", description="", kind="bug", source="linear",
                          source_ref=None, requester="bob", owner="ann", linear_issue_id="iss-7")
    cid = add_conversation(conn, task_id=t.id)
    return router.context_for(add_item(router.queue, cid, actor="bob"))


async def call(router, ctx, name, **args):
    return (await router.call(ctx, name, args)).text


async def test_gh_prs(router, ctx):
    assert "#5 Fix export (agent/t1-x → main) by agent [draft]" in await call(router, ctx, "gh_prs")


async def test_gh_checks(router, ctx):
    assert "test: completed/failure https://ci/1" in await call(router, ctx, "gh_checks", ref="abc")


async def test_gh_ci_log_keeps_tail(router, ctx):
    text = await call(router, ctx, "gh_ci_log", job_id=9)
    assert text.endswith("ERROR test_export failed") and len(text) <= 20100


async def test_gh_issue_with_comments(router, ctx):
    text = await call(router, ctx, "gh_issue", number=12)
    assert "Crash" in text and "It crashes" in text and "ann-gh: seen it too" in text


async def test_gh_file_defaults_to_default_branch(router, ctx):
    assert "print('hi')" in await call(router, ctx, "gh_file", path="src/app.py")


async def test_linear_issue_defaults_to_task_issue(router, ctx):
    text = await call(router, ctx, "linear_issue")
    assert "SAM-7" in text and "Export broken" in text and "Bob: happens daily" in text


async def test_linear_comment(router, ctx):
    await call(router, ctx, "linear_comment", body="Looking into it")
    assert ("CommentCreate", {"issueId": "iss-7", "body": "Looking into it"}) in linear_handler.calls


async def test_linear_set_status(router, ctx):
    assert "In Review" in await call(router, ctx, "linear_set_status", status_key="ready_for_review")
    assert ("IssueUpdate", {"id": "iss-7", "stateId": "s2"}) in linear_handler.calls


async def test_linear_set_status_refuses_unknown_key(router, ctx):
    text = await call(router, ctx, "linear_set_status", status_key="nope")
    assert "nope" in text and "working" in text and "ready_for_review" in text


async def test_github_error_is_reported_not_raised(router, ctx):
    assert "failed" in await call(router, ctx, "gh_issue", number=999)
