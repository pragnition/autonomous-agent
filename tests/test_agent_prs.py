import pytest

from agent import db
from agent.core.agent_prs import AgentPrPoller, match_issue
from agent.core.linear_issues import record_issue


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    db.set_setting(c, "kpi_floor", "2026-11-15T00:00:00Z")
    record_issue(c, {"createdAt": "2026-11-15T01:00:00Z", "issue": {"id": "i1", "identifier": "PRA-423"}}, "V-1")
    return c


def hit(n, title="", body="", repo="pragnition/chilliclaw"):
    return {"number": n, "title": title, "body": body, "repository_url": f"https://api.github.com/repos/{repo}",
            "html_url": f"https://github.com/{repo}/pull/{n}"}


def pull(n, head, merged=None, closed=None, title="t", body=""):
    return {"number": n, "title": title, "body": body, "html_url": f"https://github.com/pragnition/chilliclaw/pull/{n}",
            "head": {"ref": head}, "base": {"ref": "main"}, "created_at": "2026-11-15T02:00:00Z",
            "merged_at": merged, "closed_at": closed}


class FakeGitHub:
    def __init__(self, hits, pulls):
        self.hits, self.pulls, self.queries, self.fetched = hits, pulls, [], []

    async def search_prs(self, query):
        self.queries.append(query)
        return self.hits

    async def pr(self, repo, n):
        self.fetched.append((repo, n))
        return self.pulls[n]


def prs(conn):
    return {r["number"]: dict(r) for r in conn.execute("SELECT * FROM agent_prs")}


def test_match_issue_finds_the_key_as_a_whole_token():
    assert match_issue(["kunming/pra-423-model-usage"], ["PRA-423"]) == "PRA-423"
    assert match_issue(["Fixes PRA-423: tables"], ["PRA-42", "PRA-423"]) == "PRA-423"
    assert match_issue(["fixes PRA-4230"], ["PRA-423"]) is None
    assert match_issue(["XPRA-423"], ["PRA-423"]) is None
    assert match_issue([None, ""], ["PRA-423"]) is None


async def test_poll_stores_prs_with_github_times_and_their_issue(conn):
    gh = FakeGitHub([hit(1), hit(2)], {1: pull(1, "jensen/pra-423-models", merged="2026-11-15T05:00:00Z",
                                                closed="2026-11-15T05:00:00Z"),
                                       2: pull(2, "jensen/cleanup")})
    assert await AgentPrPoller(conn, gh, login="pragnitionlabs").poll() == 2
    assert gh.queries == ["is:pr author:pragnitionlabs created:>=2026-11-15T00:00:00Z"]
    one, two = prs(conn)[1], prs(conn)[2]
    assert (one["repo"], one["opened_at"], one["merged_at"], one["issue_identifier"], one["base"]) == \
        ("pragnition/chilliclaw", "2026-11-15T02:00:00Z", "2026-11-15T05:00:00Z", "PRA-423", "main")
    assert two["issue_identifier"] is None and two["merged_at"] is None


async def test_finished_prs_are_not_fetched_again_open_ones_are(conn):
    gh = FakeGitHub([hit(1), hit(2)], {1: pull(1, "a", merged="2026-11-15T05:00:00Z", closed="2026-11-15T05:00:00Z"),
                                       2: pull(2, "b")})
    poller = AgentPrPoller(conn, gh, login="pragnitionlabs")
    await poller.poll()
    gh.fetched.clear()
    gh.pulls[2] = pull(2, "b", merged="2026-11-15T07:00:00Z", closed="2026-11-15T07:00:00Z")
    await poller.poll()
    assert gh.fetched == [("pragnition/chilliclaw", 2)]
    assert prs(conn)[2]["merged_at"] == "2026-11-15T07:00:00Z"


async def test_floor_is_set_on_first_poll(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    gh = FakeGitHub([], {})
    await AgentPrPoller(c, gh, login="bot").poll()
    floor = db.get_setting(c, "kpi_floor")
    assert floor and gh.queries == [f"is:pr author:bot created:>={floor}"]


def test_branch_then_title_then_body_decides_the_issue():
    assert match_issue(["jensen/pra-423-x", "title", "Implements PRA-423, see PRA-400"], ["PRA-400", "PRA-423"]) \
        == "PRA-423"
    assert match_issue(["cleanup", "PRA-400: tidy", "also PRA-423"], ["PRA-423", "PRA-400"]) == "PRA-400"


async def test_open_prs_off_the_search_page_are_still_refreshed(conn):
    gh = FakeGitHub([hit(1)], {1: pull(1, "a")})
    poller = AgentPrPoller(conn, gh, login="pragnitionlabs")
    await poller.poll()
    gh.hits = []                                           # pushed off the first page by newer PRs
    gh.pulls[1] = pull(1, "a", merged="2026-11-16T05:00:00Z", closed="2026-11-16T05:00:00Z")
    await poller.poll()
    assert prs(conn)[1]["merged_at"] == "2026-11-16T05:00:00Z"
