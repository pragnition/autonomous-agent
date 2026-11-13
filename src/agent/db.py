"""agent.db: the core's state. One connection per process; WAL and a busy timeout let threads share it."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

AGENT_QUEUE = "agent"  # the one queue for every conversation and chore (open mode)

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
    branch TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    linear_issue_id TEXT,
    slack_channel TEXT,
    slack_thread_ts TEXT,
    status_message_ts TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    role TEXT NOT NULL,
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

# Which conversation each Slack message belongs to, received or posted, so a thread started under any of them
# finds its way back
SLACK_MESSAGES = """
CREATE TABLE IF NOT EXISTS slack_messages (
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY(channel, ts)
);
"""
SCHEMA += SLACK_MESSAGES

SCHEMA_VERSION = 6

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
    is_primary INTEGER NOT NULL DEFAULT 1,
    waiting INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    consolidated_at TEXT,
    branch TEXT,
    workdir TEXT NOT NULL DEFAULT 'session'
);
CREATE INDEX IF NOT EXISTS conversations_thread ON conversations(channel, thread_ts);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _drop_column_if_exists(conn: sqlite3.Connection, table: str, column: str) -> None:
    if column in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Drop the dead outbox and proposals tables, and the five dead columns, where they exist."""
    conn.execute("DROP TABLE IF EXISTS outbox")
    conn.execute("DROP TABLE IF EXISTS proposals")
    for table, column in (("tasks", "priority"), ("tasks", "pending_input"), ("runs", "memory_tokens"),
                          ("runs", "stage"), ("conversations", "scope")):
        _drop_column_if_exists(conn, table, column)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Collapse every queue into the one AGENT_QUEUE (open mode, one queue for everything). Still-open work items
    (queued or claimed) and every conversation move; a finished item keeps its old sandbox as history, and
    `project` is untouched either way, so KPIs and auth can still tell which project (if any) a session is for."""
    conn.execute("UPDATE work_items SET sandbox = ? WHERE sandbox != ? AND status IN ('queued', 'claimed')",
                 (AGENT_QUEUE, AGENT_QUEUE))
    conn.execute("UPDATE conversations SET sandbox = ? WHERE sandbox != ?", (AGENT_QUEUE, AGENT_QUEUE))


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Give conversations a fixed branch and a working-folder mode. Existing ones get no branch and the session
    folder, so they make no checkout until one is asked for."""
    _add_column_if_missing(conn, "conversations", "branch", "TEXT")
    _add_column_if_missing(conn, "conversations", "workdir", "TEXT NOT NULL DEFAULT 'session'")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Map Slack messages to their conversations, so a DM can be one chat and a thread under any message (the
    person's or the bot's) reaches the conversation that message belongs to."""
    conn.execute(SLACK_MESSAGES)


# Keyed by the version each migration upgrades a database to. Every entry must be safe to run on a database
# that is already at or past its version (checked with PRAGMA table_info / IF EXISTS / IF NOT EXISTS), so a
# fresh database (already created at SCHEMA's current shape) runs every migration as a no-op.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
}


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
    """Create the schema, then run every migration newer than this database's schema_version. Safe to run on
    every start: a fresh database already has SCHEMA's shape, so every migration finds nothing to do."""
    conn.executescript(SCHEMA)
    conn.executescript(CONVERSATIONS)
    current = int(get_setting(conn, "schema_version", "0"))
    for version in sorted(v for v in _MIGRATIONS if v > current):
        _MIGRATIONS[version](conn)
    set_setting(conn, "schema_version", str(SCHEMA_VERSION))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
