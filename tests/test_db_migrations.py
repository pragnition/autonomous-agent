import sqlite3

from agent import db

# Phase 2 (v2) SCHEMA and CONVERSATIONS, copied here so the v2 -> v3 migration has something real to upgrade.
V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    requester TEXT,
    owner TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    linear_issue_id TEXT,
    slack_channel TEXT,
    slack_thread_ts TEXT,
    status_message_ts TEXT,
    pending_input TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    role TEXT NOT NULL,
    stage TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    session_id TEXT,
    account_code TEXT,
    forks INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    turns INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd_est REAL NOT NULL DEFAULT 0,
    memory_tokens INTEGER NOT NULL DEFAULT 0,
    transcript_path TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task_id TEXT,
    type TEXT NOT NULL,
    actor TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS pr_stats (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    opened_at TEXT,
    ready_at TEXT,
    first_human_review_at TEXT,
    merged_at TEXT,
    closed_at TEXT,
    merge_sha TEXT,
    human_commits INTEGER NOT NULL DEFAULT 0,
    human_lines_changed INTEGER NOT NULL DEFAULT 0,
    reverted_at TEXT,
    revert_ref TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS slack_events (
    key TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    sandbox TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_items_sandbox ON work_items(sandbox, status);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    work_item_id TEXT,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    upstream TEXT NOT NULL,
    model TEXT NOT NULL,
    status INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_usage_at ON llm_usage(at);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT,
    thread_ts TEXT,
    sandbox TEXT NOT NULL,
    project TEXT,
    task_id TEXT,
    session_id TEXT,
    scope TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1,
    waiting INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    consolidated_at TEXT
);
CREATE INDEX IF NOT EXISTS conversations_thread ON conversations(channel, thread_ts);
"""


def _v2_database(path) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.executescript(V2_SCHEMA)
    db.set_setting(conn, "schema_version", "2")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _assert_v3_shape(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    assert "outbox" not in tables and "proposals" not in tables
    assert "priority" not in _columns(conn, "tasks") and "pending_input" not in _columns(conn, "tasks")
    assert "stage" not in _columns(conn, "runs") and "memory_tokens" not in _columns(conn, "runs")
    assert "scope" not in _columns(conn, "conversations")
    assert {"branch", "workdir"} <= _columns(conn, "conversations")
    assert "slack_messages" in tables
    assert db.get_setting(conn, "schema_version") == str(db.SCHEMA_VERSION)


def test_v2_database_upgrades_to_v3(tmp_path):
    conn = _v2_database(tmp_path / "agent.db")
    now = db.utcnow()
    conn.execute(
        "INSERT INTO tasks(id, project, title, description, kind, source, status, priority, created_at, updated_at) "
        "VALUES ('T-0001', 'sample', 'Fix the login', '', 'bug', 'slack', 'queued', 5, ?, ?)", (now, now))

    db.migrate(conn)

    _assert_v3_shape(conn)
    row = conn.execute("SELECT id, title FROM tasks WHERE id = 'T-0001'").fetchone()
    assert row is not None and row["title"] == "Fix the login"


def test_fresh_database_is_v3(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")

    db.migrate(conn)

    _assert_v3_shape(conn)


def test_migration_moves_queue_and_leaves_old_dms(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    conn.executescript(db.SCHEMA)
    conn.executescript(db.CONVERSATIONS)
    db.set_setting(conn, "schema_version", "3")
    now = db.utcnow()
    conn.execute("INSERT INTO work_items(id, conversation_id, sandbox, kind, payload, status, created_at, "
                 "updated_at) VALUES ('W-1', NULL, 'general', 'turn', '{}', 'queued', ?, ?)", (now, now))
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, sandbox, project, is_primary, created_at, "
                 "last_active_at) VALUES ('V-1', 'D1', '5.1', 'general', NULL, 1, ?, ?)", (now, now))

    db.migrate(conn)

    item = conn.execute("SELECT sandbox FROM work_items WHERE id = 'W-1'").fetchone()
    convo = conn.execute("SELECT sandbox, thread_ts FROM conversations WHERE id = 'V-1'").fetchone()
    assert item["sandbox"] == db.AGENT_QUEUE
    assert convo["sandbox"] == db.AGENT_QUEUE and convo["thread_ts"] == "5.1"
    assert db.get_setting(conn, "schema_version") == str(db.SCHEMA_VERSION)


# The v4 conversations table, before a conversation had a fixed branch and a working-folder mode.
V4_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT,
    thread_ts TEXT,
    sandbox TEXT NOT NULL,
    project TEXT,
    task_id TEXT,
    session_id TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1,
    waiting INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    consolidated_at TEXT
);
"""


def test_v4_conversations_gain_branch_and_workdir(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    conn.executescript(db.SCHEMA)
    conn.executescript(V4_CONVERSATIONS)
    db.set_setting(conn, "schema_version", "4")
    now = db.utcnow()
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, sandbox, project, is_primary, created_at, "
                 "last_active_at) VALUES ('V-1', 'C1', '5.1', ?, 'sample', 1, ?, ?)", (db.AGENT_QUEUE, now, now))

    db.migrate(conn)

    convo = conn.execute("SELECT * FROM conversations WHERE id = 'V-1'").fetchone()
    assert (convo["project"], convo["branch"], convo["workdir"]) == ("sample", None, "session")
    assert db.get_setting(conn, "schema_version") == str(db.SCHEMA_VERSION)


def test_v5_database_gains_slack_messages(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    conn.executescript(db.SCHEMA)
    conn.executescript(db.CONVERSATIONS)
    conn.execute("DROP TABLE IF EXISTS slack_messages")
    db.set_setting(conn, "schema_version", "5")

    db._MIGRATIONS[6](conn)

    assert {r[1]: r[5] for r in conn.execute("PRAGMA table_info(slack_messages)")} == \
        {"channel": 1, "ts": 2, "conversation_id": 0}  # primary key (channel, ts)
    db._MIGRATIONS[6](conn)  # safe to run again
    db.migrate(conn)
    assert db.get_setting(conn, "schema_version") == "6"
