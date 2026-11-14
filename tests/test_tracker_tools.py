
import httpx
import pytest

from agent import db, tasks
from agent.connectors.github_read import GitHubReader
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
    register_tracker_tools(r, reader=reader)
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


async def test_github_error_is_reported_not_raised(router, ctx):
    assert "failed" in await call(router, ctx, "gh_issue", number=999)
