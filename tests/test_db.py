from agent import db

TABLES = {"tasks", "runs", "events", "pr_stats", "slack_events", "settings", "work_items", "tokens", "llm_usage",
          "conversations", "slack_messages", "linear_issues", "linear_issue_conversations",
          "agent_prs"}


def columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_has_every_table_and_column(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    db.create_schema(conn)  # run on every start: a second run changes nothing

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%'")}
    assert tables == TABLES
    assert columns(conn, "work_items") == {"id", "conversation_id", "kind", "payload", "status", "lease_until",
                                           "attempts", "result", "created_at", "updated_at"}
    assert columns(conn, "tokens") == {"hash", "kind", "work_item_id", "expires_at", "revoked"}
    assert columns(conn, "conversations") == {"id", "channel", "thread_ts", "project", "task_id", "session_id",
                                              "is_primary", "waiting", "model", "created_at", "last_active_at"}
    assert columns(conn, "slack_messages") == {"channel", "ts", "conversation_id"}
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_a_database_from_before_the_agent_made_its_own_clones_still_works(tmp_path):
    """A live install's conversations table still has the branch and workdir columns nothing uses any more."""
    from agent.core.conversations import create_conversation

    conn = db.connect(tmp_path / "agent.db")
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, channel TEXT, thread_ts TEXT, project TEXT, "
                 "task_id TEXT, session_id TEXT, is_primary INTEGER NOT NULL DEFAULT 1, waiting INTEGER NOT NULL "
                 "DEFAULT 0, model TEXT, created_at TEXT NOT NULL, last_active_at TEXT NOT NULL, branch TEXT, "
                 "workdir TEXT NOT NULL DEFAULT 'session')")
    db.create_schema(conn)
    cid = create_conversation(conn, channel="C1", thread_ts="1.0", project="sample")
    assert conn.execute("SELECT project FROM conversations WHERE id = ?", (cid,)).fetchone()[0] == "sample"
