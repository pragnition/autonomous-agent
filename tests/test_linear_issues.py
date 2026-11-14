import pytest

from agent import db
from agent.core.linear_issues import record_issue


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
