import pytest

from agent import db, tasks
from agent.core.agent_tools import AgentToolRouter
from agent.core.prs import pr_body, register_pr_tools
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent.tasks import Status
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

SHA = "a" * 40


class FakeGitHost:
    def __init__(self, tips=None):
        self.tips, self.pushes = dict(tips or {}), []

    def tip(self, project, branch):
        return self.tips.get(branch)

    def push_to_github(self, project, repo, branch, sha, write_token):
        self.pushes.append((project, repo, branch, sha, write_token))


class FakeWriter:
    def __init__(self, open_pr=None):
        self.open_pr, self.drafts, self.assignee = open_pr, [], None

    async def find_open_pr(self, repo, branch):
        return self.open_pr

    async def open_draft_pr(self, repo, *, head, base, title, body):
        self.drafts.append((repo, head))
        self.body = body
        return 42, f"https://github.com/{repo}/pull/42"

    async def set_assignee(self, repo, number, login):
        self.assignee = login


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


def build(conn, tmp_path, *, writer=None, tips=None):
    router = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=["WRITE-TOKEN-VALUE"])
    githost = FakeGitHost(tips if tips is not None else {"agent/t1-x": SHA})
    writer = writer or FakeWriter()
    register_pr_tools(router, githost=githost, writer=writer, write_token="WRITE-TOKEN-VALUE")
    cid = add_conversation(conn, sandbox="code-sample")
    ctx = router.context_for(add_item(router.queue, cid, "code-sample", actor="bob"))
    return router, ctx, githost, writer


def pr_args(branch="agent/t1-x", sha=SHA, passed=True):
    return {"title": "Fix export", "body": "Fixes the crash.", "branch": branch, "sha": sha,
            "checks": {"passed": passed, "log_tail": "1 failed: test_export"}}


async def test_refuses_wrong_branch_prefix(conn, tmp_path):
    router, ctx, githost, _ = build(conn, tmp_path, tips={"main": SHA})
    assert "must start with agent/" in (await router.call(ctx, "open_pr", pr_args(branch="main"))).text
    assert githost.pushes == []


async def test_refuses_non_tip_sha(conn, tmp_path):
    router, ctx, githost, _ = build(conn, tmp_path)
    r = await router.call(ctx, "open_pr", pr_args(sha="0" * 40))
    assert "not the tip" in r.text and githost.pushes == []


async def test_refuses_unknown_branch(conn, tmp_path):
    router, ctx, _, _ = build(conn, tmp_path, tips={})
    assert "not the tip" in (await router.call(ctx, "open_pr", pr_args())).text


async def test_refuses_failed_checks(conn, tmp_path):
    router, ctx, githost, _ = build(conn, tmp_path)
    r = await router.call(ctx, "open_pr", pr_args(passed=False))
    assert "checks failed" in r.text and "test_export" in r.text and githost.pushes == []


async def test_opens_draft_assigned_to_requester(conn, tmp_path):
    router, ctx, githost, writer = build(conn, tmp_path)
    r = await router.call(ctx, "open_pr", pr_args())
    assert githost.pushes == [("sample", "o/r", "agent/t1-x", SHA, "WRITE-TOKEN-VALUE")]
    assert writer.drafts == [("o/r", "agent/t1-x")] and writer.assignee == "bob-gh"
    assert "https://github.com/o/r/pull/42" in r.text and "WRITE-TOKEN-VALUE" not in r.text
    task_id = conn.execute("SELECT task_id FROM conversations WHERE id = ?", (ctx.conversation_id,)).fetchone()[0]
    task = tasks.get_task(conn, task_id)
    assert task.status == Status.ready_for_review and task.pr_number == 42 and task.branch == "agent/t1-x"
    assert "agent (AI agent)" in writer.body and "Fixes the crash." in writer.body


async def test_owner_is_assignee_for_unmapped_requester(conn, tmp_path):
    router, _, _, writer = build(conn, tmp_path)
    cid = add_conversation(conn, sandbox="code-sample", thread_ts="2.0")
    ctx = router.context_for(add_item(router.queue, cid, "code-sample", actor=None, unattended=True))
    await router.call(ctx, "open_pr", pr_args())
    assert writer.assignee == "ann-gh"


async def test_reuses_open_pr(conn, tmp_path):
    router, ctx, githost, writer = build(conn, tmp_path, writer=FakeWriter(open_pr=7))
    r = await router.call(ctx, "open_pr", pr_args())
    assert writer.drafts == [] and len(githost.pushes) == 1 and "#7" in r.text


async def test_open_pr_only_in_code_profile(conn, tmp_path):
    router, _, _, _ = build(conn, tmp_path)
    cid = add_conversation(conn, sandbox="debug-sample", thread_ts="3.0")
    ctx = router.context_for(add_item(router.queue, cid, "debug-sample"))
    assert (await router.call(ctx, "open_pr", pr_args())).text == "not available here"


def test_pr_body_format():
    body = pr_body(agent_name="agent", requester_github="bob-gh", origin_ref="Slack", summary="S",
                   checks=["pytest -q"])
    assert body.startswith("🤖 agent (AI agent) · Requested by @bob-gh in Slack")
    assert "- `pytest -q`" in body
