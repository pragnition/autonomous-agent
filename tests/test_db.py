from agent import db

TABLES = {"tasks", "runs", "events", "pr_stats", "slack_events", "settings", "work_items", "tokens", "llm_usage",
          "conversations", "slack_messages", "linear_issues", "linear_issue_conversations",
          "agent_prs", "request_labels", "task_records", "projects", "limit_waits"}


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
                                           "attempts", "result", "created_at", "updated_at", "delivered_into"}
    assert columns(conn, "tokens") == {"hash", "kind", "work_item_id", "expires_at", "revoked"}
    assert columns(conn, "conversations") == {"id", "channel", "thread_ts", "project", "task_id", "session_id",
                                              "is_primary", "waiting", "model", "created_at", "last_active_at"}
    assert columns(conn, "slack_messages") == {"channel", "ts", "conversation_id"}
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_work_items_gain_delivered_into(tmp_path):
    """A live install's work_items table predates the delivered_into column."""
    conn = db.connect(tmp_path / "agent.db")
    conn.execute("CREATE TABLE work_items (id TEXT PRIMARY KEY, conversation_id TEXT, kind TEXT NOT NULL, payload "
                 "TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', lease_until TEXT, attempts INTEGER NOT NULL "
                 "DEFAULT 0, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.create_schema(conn)
    assert "delivered_into" in columns(conn, "work_items")


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


def test_the_task_record_tables_from_before_projects_gain_their_new_columns(tmp_path):
    """The first task-record release created these tables without a project or a label version."""
    conn = db.connect(tmp_path / "agent.db")
    conn.execute("CREATE TABLE request_labels (work_item_id TEXT PRIMARY KEY, conversation_id TEXT, source TEXT NOT "
                 "NULL, requester TEXT, follows TEXT, asked TEXT NOT NULL DEFAULT '', link TEXT, label TEXT, task_id "
                 "TEXT, title TEXT, outcome TEXT, finished INTEGER, arrived_at TEXT NOT NULL, started_at TEXT, "
                 "answered_at TEXT, tries INTEGER NOT NULL DEFAULT 0, labeled_at TEXT)")
    conn.execute("INSERT INTO request_labels(work_item_id, source, label, arrived_at) VALUES ('W1', 'slack', 'chat', "
                 "'2026-11-15T10:00:00Z')")
    conn.execute("CREATE TABLE task_records (id TEXT PRIMARY KEY, source TEXT NOT NULL, requested_by TEXT, started_at "
                 "TEXT NOT NULL, ended_at TEXT, work_time_s INTEGER NOT NULL DEFAULT 0, finished INTEGER, record TEXT "
                 "NOT NULL)")
    db.create_schema(conn)
    db.create_schema(conn)
    assert {"project", "version"} <= columns(conn, "request_labels")
    assert "project" in columns(conn, "task_records")
    assert tuple(conn.execute("SELECT project, version FROM request_labels").fetchone()) == (None, 1)
