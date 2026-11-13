"""agent.db: the core's state. One connection per process; WAL and a busy timeout let threads share it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
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
"""

SCHEMA_VERSION = 2

# One row per agent session: a Slack thread, a Linear issue, a hand-over or a split (spec v2 §6, §7)
CONVERSATIONS = """
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

_V1_WORKING = ("building", "investigating", "specifying", "testing")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema, or upgrade a Phase 1 (v1) database to v2. Safe to run on every start."""
    conn.executescript(SCHEMA)
    if get_setting(conn, "schema_version") == str(SCHEMA_VERSION):
        return
    old = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    if old and "id" not in old:  # v1 keyed conversations by (channel, thread_ts); keep them aside for reference
        conn.execute("ALTER TABLE conversations RENAME TO conversations_v1")
    conn.executescript(CONVERSATIONS)
    marks = ",".join("?" * len(_V1_WORKING))
    conn.execute(f"UPDATE tasks SET status = 'working' WHERE status IN ({marks})", _V1_WORKING)
    set_setting(conn, "schema_version", str(SCHEMA_VERSION))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
