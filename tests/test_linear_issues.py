from datetime import UTC, datetime

import pytest

from agent import db
from agent.core.linear_issues import LinearIssuePoller, record_issue


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


ISSUE = {"id": "iss-1", "identifier": "PRA-423", "title": "Model Usage Information",
         "url": "https://linear.app/pragnition/issue/PRA-423/model-usage-information"}


def session(created, issue=ISSUE):
    return {"id": f"s-{created}", "createdAt": created, "issue": issue}


def test_record_issue_once_per_issue(conn):
    record_issue(conn, session("2026-11-14T14:39:07.039Z"), "V-a")
    record_issue(conn, session("2026-11-14T14:27:53Z"), "V-b")
    rows = conn.execute("SELECT * FROM linear_issues").fetchall()
    assert len(rows) == 1
    assert rows[0]["identifier"] == "PRA-423" and rows[0]["handed_at"] == "2026-11-14T14:27:53Z"
    links = {r[0] for r in conn.execute("SELECT conversation_id FROM linear_issue_conversations")}
    assert links == {"V-a", "V-b"}


def test_record_issue_ignores_sessions_without_an_issue(conn):
    record_issue(conn, {"id": "s", "createdAt": "2026-11-14T14:27:53Z"}, "V-a")
    assert conn.execute("SELECT COUNT(*) FROM linear_issues").fetchone()[0] == 0

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeLinear:
    def __init__(self, states):
        self.states, self.asked = states, []

    async def issue_state(self, issue_id):
        self.asked.append(issue_id)
        state = self.states[issue_id]
        if isinstance(state, Exception):
            raise state
        return state


def add(conn, issue_id, ident, **cols):
    record_issue(conn, {"createdAt": "2026-11-14T10:00:00Z",
                        "issue": {"id": issue_id, "identifier": ident}}, f"V-{issue_id}")
    for k, v in cols.items():
        conn.execute(f"UPDATE linear_issues SET {k} = ? WHERE issue_id = ?", (v, issue_id))


def row(conn, issue_id):
    return conn.execute("SELECT * FROM linear_issues WHERE issue_id = ?", (issue_id,)).fetchone()


async def test_poll_records_linear_close_times(conn):
    add(conn, "a", "PRA-1")
    add(conn, "b", "PRA-2")
    linear = FakeLinear({"a": {"state_type": "completed", "completed_at": "2026-11-15T09:30:00.500Z",
                               "canceled_at": None},
                         "b": {"state_type": "canceled", "completed_at": None,
                               "canceled_at": "2026-11-15T08:00:00.000Z"}})
    assert await LinearIssuePoller(conn, linear, clock=lambda: NOW).poll() == 2
    assert row(conn, "a")["completed_at"] == "2026-11-15T09:30:00Z" and row(conn, "a")["state_type"] == "completed"
    assert row(conn, "b")["canceled_at"] == "2026-11-15T08:00:00Z" and row(conn, "b")["completed_at"] is None
    assert row(conn, "a")["checked_at"] is not None


async def test_reopened_issue_loses_its_close_time(conn):
    add(conn, "a", "PRA-1", state_type="completed", completed_at="2026-11-19T09:00:00Z")
    linear = FakeLinear({"a": {"state_type": "started", "completed_at": None, "canceled_at": None}})
    await LinearIssuePoller(conn, linear, clock=lambda: NOW).poll()
    assert row(conn, "a")["completed_at"] is None and row(conn, "a")["state_type"] == "started"


async def test_issues_closed_over_a_week_ago_are_left_alone(conn):
    add(conn, "a", "PRA-1", state_type="completed", completed_at="2026-11-12T09:00:00Z")
    linear = FakeLinear({})
    assert await LinearIssuePoller(conn, linear, clock=lambda: NOW).poll() == 0
    assert linear.asked == []


async def test_one_failing_issue_does_not_stop_the_rest(conn):
    add(conn, "a", "PRA-1")
    add(conn, "b", "PRA-2")
    linear = FakeLinear({"a": RuntimeError("linear down"),
                         "b": {"state_type": "completed", "completed_at": "2026-11-15T09:30:00Z",
                               "canceled_at": None}})
    await LinearIssuePoller(conn, linear, clock=lambda: NOW).poll()
    assert row(conn, "b")["completed_at"] == "2026-11-15T09:30:00Z"
