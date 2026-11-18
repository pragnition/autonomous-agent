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
    kind TEXT NOT NULL,
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
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    delivered_into TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_items_status ON work_items(status);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
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
    cost_usd REAL NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_usage_at ON llm_usage(at);
-- One row per agent session: a Slack thread or DM chat, a Linear issue, a hand-over or a split
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT,
    thread_ts TEXT,
    project TEXT,
    task_id TEXT,
    session_id TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1,
    waiting INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_thread ON conversations(channel, thread_ts);
-- Which conversation each Slack message belongs to, received or posted, so a thread started under any of them
-- finds its way back
CREATE TABLE IF NOT EXISTS slack_messages (
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY(channel, ts)
);
-- Linear issues handed to the agent, recorded from the webhook; Linear's own state is polled into them (KPIs)
CREATE TABLE IF NOT EXISTS linear_issues (
    issue_id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    title TEXT,
    url TEXT,
    handed_at TEXT NOT NULL,
    state_type TEXT,
    completed_at TEXT,
    canceled_at TEXT,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS linear_issue_conversations (
    issue_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY(issue_id, conversation_id)
);
-- The agent's own PRs, found and kept current by polling GitHub (KPIs)
CREATE TABLE IF NOT EXISTS agent_prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    url TEXT,
    title TEXT,
    head TEXT,
    base TEXT,
    opened_at TEXT NOT NULL,
    merged_at TEXT,
    closed_at TEXT,
    issue_identifier TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(repo, number)
);
-- The labeler's verdict on each request the agent answered: chat, a new task or the same task as an earlier one
CREATE TABLE IF NOT EXISTS request_labels (
    work_item_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    source TEXT NOT NULL,         -- slack | linear | schedule
    requester TEXT,               -- a person key, or a Linear name when no person matches
    follows TEXT,                 -- the work item this one carries on (a continuation or a hand-over)
    asked TEXT NOT NULL DEFAULT '',
    link TEXT,                    -- the origin line's URL: the Slack permalink or the Linear issue
    label TEXT,                   -- chat | new | same; NULL until labeled
    task_id TEXT,
    title TEXT,                   -- for a `new` request: the task's title
    project TEXT,                 -- the configured project the work is on; NULL for general work
    outcome TEXT,
    finished INTEGER,
    arrived_at TEXT NOT NULL,     -- the work item's created_at
    started_at TEXT,              -- its latest session_started
    answered_at TEXT,             -- its updated_at once finished
    tries INTEGER NOT NULL DEFAULT 0,
    labeled_at TEXT,
    version INTEGER NOT NULL DEFAULT 1  -- the labeler's version; an older one is relabeled
);
CREATE INDEX IF NOT EXISTS request_labels_task ON request_labels(task_id);
CREATE INDEX IF NOT EXISTS request_labels_requester ON request_labels(requester, arrived_at);
-- One row per task, rebuilt from its labeled requests; `record` is the whole record as the agent's copy has it
CREATE TABLE IF NOT EXISTS task_records (
    id TEXT PRIMARY KEY,          -- TK-0001
    source TEXT NOT NULL,
    project TEXT,                 -- NULL: a general task
    requested_by TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    work_time_s INTEGER NOT NULL DEFAULT 0,
    finished INTEGER,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_records_started ON task_records(started_at);
-- A long project's name, where its code is and which conversation is building it
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    thread_link TEXT,
    started_by TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active | done
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Turns a usage limit cut off, each re-queued once when an account has capacity again (core/usage_watch.py)
CREATE TABLE IF NOT EXISTS limit_waits (
    work_item_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    resumed_at TEXT,
    resumed_as TEXT                -- the work item that carried it on
);
"""


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added to tables a live install already has; CREATE TABLE IF NOT EXISTS leaves those tables as they were
ADDED_COLUMNS = [("request_labels", "project", "TEXT"), ("request_labels", "version", "INTEGER NOT NULL DEFAULT 1"),
                 ("task_records", "project", "TEXT"), ("llm_usage", "cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
                 ("work_items", "delivered_into", "TEXT")]


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index that doesn't exist yet, and add the columns older tables lack. Run on every
    start."""
    conn.executescript(SCHEMA)
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
