from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent import db, tasks
from agent.config import load_agent_config, load_packs
from agent.connectors.github_read import GitHubReader
from agent.core.pr_stats import PrStatsPoller
from agent.tasks import Status

EXAMPLES = Path(__file__).parent.parent / "examples" / "config"
REPO = "/repos/example-org/agent-sample"


class GH:
    def __init__(self, *, merged=True, state="closed", reviews=(), commits=(), events=(), main_commits=(),
                 title="Add copy button"):
        self.pr = {"number": 7, "state": state, "merged": merged, "draft": False, "title": title,
                   "created_at": "2026-11-01T00:00:00Z", "closed_at": "2026-11-03T00:00:00Z" if state == "closed" else None,
                   "merged_at": "2026-11-03T00:00:00Z" if merged else None, "merge_commit_sha": "abc123",
                   "base": {"ref": "main"}}
        self.reviews, self.commits, self.events, self.main_commits = reviews, commits, events, main_commits
        self.paths = []

    def handler(self, req):
        p = req.url.path
        self.paths.append(p)
        if p == f"{REPO}/pulls/7":
            return httpx.Response(200, json=self.pr)
        if p == f"{REPO}/pulls/7/reviews":
            return httpx.Response(200, json=list(self.reviews))
        if p == f"{REPO}/pulls/7/commits":
            return httpx.Response(200, json=[{"sha": c[0], "author": {"login": c[1]}} for c in self.commits])
        if p.startswith(f"{REPO}/commits/"):
            sha = p.rsplit("/", 1)[1]
            add, dele = next((c[2], c[3]) for c in self.commits if c[0] == sha)
            return httpx.Response(200, json={"sha": sha, "stats": {"additions": add, "deletions": dele}})
        if p == f"{REPO}/issues/7/events":
            return httpx.Response(200, json=list(self.events))
        if p == f"{REPO}/commits":
            return httpx.Response(200, json=[{"sha": s, "commit": {"message": m, "author": {"date": d}}}
                                             for s, m, d in self.main_commits])
        return httpx.Response(404, json={})

    def reader(self):
        return GitHubReader("t", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    config = load_agent_config(EXAMPLES / "agent.toml")
    packs = load_packs(EXAMPLES, config)
    t = tasks.create_task(conn, project="sample", title="Add copy button", description="d", kind="feature",
                          source="slack", source_ref="C1/1", requester="alice", owner="alice")
    tasks.transition(conn, t.id, Status.working, actor="x")
    tasks.set_fields(conn, t.id, pr_number=7, pr_url="u")
    tasks.transition(conn, t.id, Status.ready_for_review, actor="x")
    return conn, packs, t.id


def poller(env, gh, now=datetime(2026, 11, 10, tzinfo=UTC)):
    conn, packs, _ = env
    return PrStatsPoller(conn, gh.reader(), agent_login="jensen", packs=packs, clock=lambda: now)


def stats(conn, tid):
    return dict(conn.execute("SELECT * FROM pr_stats WHERE task_id = ?", (tid,)).fetchone())


async def test_merged_pr_transitions_and_records(env):
    conn, _, tid = env
    await poller(env, GH()).update(tasks.get_task(conn, tid))
    assert tasks.get_task(conn, tid).status == Status.merged
    s = stats(conn, tid)
    assert (s["merged_at"], s["merge_sha"], s["opened_at"], s["repo"]) == \
        ("2026-11-03T00:00:00Z", "abc123", "2026-11-01T00:00:00Z", "example-org/agent-sample")


async def test_closed_unmerged_transitions_closed(env):
    conn, _, tid = env
    await poller(env, GH(merged=False)).update(tasks.get_task(conn, tid))
    assert tasks.get_task(conn, tid).status == Status.closed


async def test_open_pr_stays_ready_for_review(env):
    conn, _, tid = env
    await poller(env, GH(merged=False, state="open")).update(tasks.get_task(conn, tid))
    assert tasks.get_task(conn, tid).status == Status.ready_for_review


async def test_human_commits_exclude_agent(env):
    conn, _, tid = env
    gh = GH(commits=[("s1", "jensen", 50, 5), ("s2", "alice", 3, 2), ("s3", "bob", 1, 0)])
    await poller(env, gh).update(tasks.get_task(conn, tid))
    s = stats(conn, tid)
    assert (s["human_commits"], s["human_lines_changed"]) == (2, 6)
    assert f"{REPO}/commits/s1" not in gh.paths  # no stats fetched for the agent's own commits


async def test_first_human_review_ignores_agent_reviews(env):
    conn, _, tid = env
    gh = GH(reviews=[{"user": {"login": "jensen"}, "state": "COMMENTED", "submitted_at": "2026-11-01T01:00:00Z"},
                     {"user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-11-02T09:00:00Z"},
                     {"user": {"login": "bob"}, "state": "COMMENTED", "submitted_at": "2026-11-01T05:00:00Z"}])
    await poller(env, gh).update(tasks.get_task(conn, tid))
    assert stats(conn, tid)["first_human_review_at"] == "2026-11-01T05:00:00Z"


async def test_ready_at_from_events(env):
    conn, _, tid = env
    gh = GH(events=[{"event": "labeled", "created_at": "2026-11-01T00:30:00Z"},
                    {"event": "ready_for_review", "created_at": "2026-11-01T02:00:00Z"}])
    await poller(env, gh).update(tasks.get_task(conn, tid))
    assert stats(conn, tid)["ready_at"] == "2026-11-01T02:00:00Z"


async def test_revert_detected_by_sha(env):
    conn, _, tid = env
    gh = GH(main_commits=[("r1", 'Revert "Something"\n\nThis reverts commit abc123.', "2026-11-05T00:00:00Z")])
    await poller(env, gh).update(tasks.get_task(conn, tid))
    s = stats(conn, tid)
    assert (s["reverted_at"], s["revert_ref"]) == ("2026-11-05T00:00:00Z", "r1")


async def test_revert_detected_by_title(env):
    conn, _, tid = env
    gh = GH(main_commits=[("r2", 'Revert "Add copy button" (#9)', "2026-11-06T00:00:00Z")])
    await poller(env, gh).update(tasks.get_task(conn, tid))
    assert stats(conn, tid)["revert_ref"] == "r2"


async def test_no_revert_check_after_30_days(env):
    conn, _, tid = env
    gh = GH(main_commits=[("r1", "This reverts commit abc123.", "2026-12-20T00:00:00Z")])
    await poller(env, gh, now=datetime(2026, 12, 20, tzinfo=UTC)).update(tasks.get_task(conn, tid))
    assert stats(conn, tid)["reverted_at"] is None
    assert f"{REPO}/commits" not in gh.paths


async def test_poll_all_selects_open_and_recent(env):
    conn, _, tid = env
    gh = GH()
    p = poller(env, gh)
    assert await p.poll_all() == [tid]
    assert await p.poll_all() == [tid]  # merged within 30 days: still watched for reverts
    later = poller(env, gh, now=datetime(2026, 12, 20, tzinfo=UTC))
    assert await later.poll_all() == []


async def test_on_change_called_when_task_transitions(env):
    conn, packs, tid = env
    seen = []

    async def on_change(task):
        seen.append(task.status)

    p = PrStatsPoller(conn, GH().reader(), agent_login="jensen", packs=packs, on_change=on_change,
                      clock=lambda: datetime(2026, 11, 10, tzinfo=UTC))
    await p.update(tasks.get_task(conn, tid))
    assert seen == [Status.merged]
