import sqlite3

from agent import db

# The v1 tables that v2 changes (Phase 1's SCHEMA, trimmed to what the migration touches)
V1_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT, requester TEXT, owner TEXT, status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0, criteria TEXT, branch TEXT, pr_number INTEGER, pr_url TEXT,
    linear_issue_id TEXT, slack_channel TEXT, slack_thread_ts TEXT, status_message_ts TEXT, pending_input TEXT,
    fix_cycles INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE conversations (
    channel TEXT NOT NULL, thread_ts TEXT NOT NULL, session_id TEXT, project TEXT, scope TEXT,
    created_at TEXT NOT NULL, last_active_at TEXT NOT NULL, consolidated_at TEXT, PRIMARY KEY (channel, thread_ts));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
"""


def v1_database_with_task(tmp_path, status: str) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "agent.db")
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO tasks(id, project, title, kind, source, status, created_at, updated_at) "
                 "VALUES ('T-0001', 'p', 't', 'bug', 'slack', ?, 'x', 'x')", (status,))
    conn.execute("INSERT INTO conversations(channel, thread_ts, created_at, last_active_at) VALUES ('C', '1', 'x', 'x')")
    return conn


def table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_v1_maps_statuses(tmp_path):
    conn = v1_database_with_task(tmp_path, status="building")
    db.migrate(conn)
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "working"
    assert db.get_setting(conn, "schema_version") == "2"
    assert {"work_items", "tokens", "proposals"} <= table_names(conn)


def test_migrate_v1_keeps_old_conversations_aside(tmp_path):
    conn = v1_database_with_task(tmp_path, status="testing")
    db.migrate(conn)
    assert "conversations_v1" in table_names(conn)
    assert {"id", "sandbox", "is_primary", "waiting", "model"} <= columns(conn, "conversations")
    assert conn.execute("SELECT COUNT(*) FROM conversations_v1").fetchone()[0] == 1


def test_fresh_database_is_v2_and_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.migrate(conn)
    db.migrate(conn)
    assert db.get_setting(conn, "schema_version") == "2"
    assert "conversations_v1" not in table_names(conn)
    assert {"id", "conversation_id", "sandbox", "kind", "payload", "status", "lease_until", "attempts",
            "result"} <= columns(conn, "work_items")
