//! agent.db: the same file and tables as core (a port of src/agent/db.py), plus agentd's own tables.

use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};

use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, params};

/// Core's schema, verbatim from src/agent/db.py.
const SCHEMA: &str = include_str!("schema.sql");

/// agentd's own tables: the container of each conversation, each turn it runs (what it was asked to do, how far it
/// got, and its result until the caller has handled it), the tool results it has already sent, so a tool call
/// replayed after a restart runs once, the keeps people set, the conversations' folders the cleanup has done, and what
/// each conversation said about its work for the dashboard's Tasks page.
const AGENTD_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS agentd_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- update | question
    text TEXT NOT NULL,                     -- as people saw it: scrubbed of secrets
    steps TEXT,                             -- an update's checklist, JSON [{content, status}]; NULL when it sent none
    attention INTEGER NOT NULL DEFAULT 0,   -- 1: an update flagged as needing someone; a question always needs someone
    slack_ts TEXT,                          -- the first Slack message an update was posted as
    created_at TEXT NOT NULL,
    closed_at TEXT,                         -- a flag or question that no longer needs anyone
    closed_by TEXT                          -- reply (a person wrote in the thread) or the viewer who dismissed it
);
CREATE INDEX IF NOT EXISTS agentd_progress_conversation ON agentd_progress(conversation_id, id);
CREATE TABLE IF NOT EXISTS agentd_containers (
    conversation_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    image_id TEXT,
    created_at TEXT NOT NULL,
    last_turn_at TEXT,
    stopped_at TEXT,
    replace INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agentd_turns (
    work_item_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,                    -- running | finished | reported
    spec TEXT NOT NULL,                     -- the TurnSpec, JSON
    progress TEXT NOT NULL DEFAULT '{}',    -- attempts, forks, the current session and prompt, JSON
    forwarded_seq INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS agentd_tool_calls (
    work_item_id TEXT NOT NULL,
    call_id INTEGER NOT NULL,               -- the call's line number in the turn's log
    result TEXT NOT NULL,
    PRIMARY KEY(work_item_id, call_id)
);
CREATE TABLE IF NOT EXISTS agentd_keeps (
    kind TEXT NOT NULL,                     -- container | server | service | stack
    target TEXT NOT NULL,                   -- a conversation, conversation:port, a service's name, a stack's name
    until TEXT,                             -- when it stops; NULL: kept until someone changes it
    set_by TEXT,
    set_at TEXT NOT NULL,
    PRIMARY KEY(kind, target)
);
CREATE TABLE IF NOT EXISTS agentd_cleanup_asks (
    volume TEXT PRIMARY KEY,                -- a database volume in the agent's Docker people were asked about
    created_at TEXT NOT NULL,               -- the volume's own creation time: a new volume of that name is asked about again
    asked_at TEXT NOT NULL,
    work_item TEXT                          -- the turn that asked
);
CREATE TABLE IF NOT EXISTS agentd_cleanup_stacks (
    stack TEXT PRIMARY KEY,                 -- a stack in the agent's Docker as the cleanup last saw it
    last_used_at TEXT,                      -- the last pass that saw anything use it
    counters TEXT                           -- JSON: container id -> bytes on its network then
);
CREATE TABLE IF NOT EXISTS agentd_cleanup_volumes (
    volume TEXT PRIMARY KEY,                -- a volume in the agent's Docker as the cleanup last saw it
    created_at TEXT NOT NULL,               -- its own creation time: a new volume of that name starts over
    unused_since TEXT,                      -- since when no container has mounted it; NULL while one does
    folders TEXT, stacks TEXT, destinations TEXT, images TEXT,  -- JSON lists: who mounted it, where, with what image
    kind TEXT                               -- rebuildable | database | unknown, once known
);
CREATE TABLE IF NOT EXISTS agentd_cleanup_images (
    image TEXT PRIMARY KEY,                 -- an image id in the agent's Docker
    last_used_at TEXT NOT NULL              -- the last pass a container used it (or the first that saw it)
);
CREATE TABLE IF NOT EXISTS agentd_cleanups (
    folder TEXT PRIMARY KEY,                -- a conversation's folder under scratch
    cleaned_at TEXT NOT NULL,
    activity_at TEXT NOT NULL,              -- activity up to here is accounted for: the folder isn't walked again until after
    freed_bytes INTEGER NOT NULL DEFAULT 0  -- over every pass
);
";

/// Columns added to tables a live install already has; CREATE TABLE IF NOT EXISTS leaves those tables as they were.
const ADDED_COLUMNS: &[(&str, &str, &str)] = &[
    ("request_labels", "project", "TEXT"),
    ("request_labels", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("task_records", "project", "TEXT"),
    ("llm_usage", "cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("work_items", "delivered_into", "TEXT"),
    ("conversations", "session_account", "TEXT"),
    ("conversations", "cwd", "TEXT"),
    ("runs", "project", "TEXT"),
];

/// Filled in once, when the column is added.
fn backfill(table: &str, column: &str) -> Option<&'static str> {
    match (table, column) {
        // a conversation's session ran on the account its last run recorded
        ("conversations", "session_account") => Some(
            "UPDATE conversations SET session_account = (SELECT r.account_code FROM runs r \
             WHERE r.session_id = conversations.session_id ORDER BY r.ended_at DESC, r.rowid DESC LIMIT 1) \
             WHERE session_id IS NOT NULL"),
        // a run's project: its conversation's, else that conversation's task's, else the run's own task's
        ("runs", "project") => Some(
            "UPDATE runs SET project = COALESCE((SELECT COALESCE(c.project, t.project) FROM work_items w \
             JOIN conversations c ON c.id = w.conversation_id LEFT JOIN tasks t ON t.id = c.task_id \
             WHERE w.id = runs.id), (SELECT t.project FROM tasks t WHERE t.id = runs.task_id))"),
        _ => None,
    }
}

/// One connection shared by the whole program, as core keeps one; WAL and a busy timeout let other readers in.
#[derive(Clone)]
pub struct Db {
    conn: Arc<Mutex<Connection>>,
}

impl Db {
    pub fn open(path: &Path) -> Result<Db> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "busy_timeout", 5000)?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        create_schema(&conn)?;
        Ok(Db { conn: Arc::new(Mutex::new(conn)) })
    }

    /// The connection, for synchronous use; hold it briefly.
    pub fn lock(&self) -> MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Run `f` on the connection off the async runtime's threads.
    pub async fn call<T: Send + 'static>(&self, f: impl FnOnce(&Connection) -> Result<T> + Send + 'static) -> Result<T> {
        let db = self.clone();
        tokio::task::spawn_blocking(move || f(&db.lock())).await?
    }
}

/// Create every table and index that doesn't exist yet, and add the columns older tables lack. Run on every start.
pub fn create_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(SCHEMA)?;
    for (table, column, kind) in ADDED_COLUMNS {
        let columns: Vec<String> = conn.prepare(&format!("PRAGMA table_info({table})"))?
            .query_map([], |r| r.get(1))?.collect::<rusqlite::Result<_>>()?;
        if !columns.iter().any(|c| c == column) {
            conn.execute(&format!("ALTER TABLE {table} ADD COLUMN {column} {kind}"), [])?;
            if let Some(sql) = backfill(table, column) {
                conn.execute(sql, [])?;
            }
        }
    }
    conn.execute_batch(AGENTD_SCHEMA)?;
    Ok(())
}

/// `f` in one `BEGIN IMMEDIATE` transaction, committed when it succeeds.
pub fn immediate<T>(conn: &Connection, f: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
    conn.execute_batch("BEGIN IMMEDIATE")?;
    match f(conn) {
        Ok(value) => {
            conn.execute_batch("COMMIT")?;
            Ok(value)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

/// One line of the append-only event log, the source of truth for metrics (events.py); data as Python wrote it.
pub fn append_event(conn: &Connection, kind: &str, task_id: Option<&str>, actor: Option<&str>,
                    data: Option<&serde_json::Value>) -> Result<i64> {
    conn.execute("INSERT INTO events(ts, task_id, type, actor, data) VALUES (?, ?, ?, ?, ?)",
                 params![utcnow(), task_id, kind, actor, data.map(crate::records::py_dumps)])?;
    Ok(conn.last_insert_rowid())
}

/// A row as a JSON object, column by column.
pub fn row_json(r: &rusqlite::Row) -> rusqlite::Result<serde_json::Value> {
    use rusqlite::types::ValueRef;
    let mut map = serde_json::Map::new();
    for (i, name) in r.as_ref().column_names().iter().enumerate() {
        let value = match r.get_ref(i)? {
            ValueRef::Null => serde_json::Value::Null,
            ValueRef::Integer(n) => n.into(),
            ValueRef::Real(f) => f.into(),
            ValueRef::Text(t) | ValueRef::Blob(t) => String::from_utf8_lossy(t).into_owned().into(),
        };
        map.insert(name.to_string(), value);
    }
    Ok(serde_json::Value::Object(map))
}

/// `n` SQL placeholders, for an `IN (...)` list.
pub fn marks(n: usize) -> String {
    vec!["?"; n].join(",")
}

/// Now, as core writes times: `2026-11-23T04:05:06Z`.
pub fn utcnow() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

pub fn get_setting(conn: &Connection, key: &str) -> Result<Option<String>> {
    Ok(conn.query_row("SELECT value FROM settings WHERE key = ?", [key], |r| r.get::<_, Option<String>>(0))
        .optional()?.flatten())
}

pub fn set_setting(conn: &Connection, key: &str, value: &str) -> Result<()> {
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 params![key, value])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dump(conn: &rusqlite::Connection) -> Vec<String> {
        let mut lines = Vec::new();
        let tables: Vec<String> = conn.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE \
            'sqlite_%' AND name NOT LIKE 'agentd_%' ORDER BY name").unwrap()
            .query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
        for t in tables {
            let mut stmt = conn.prepare(&format!("PRAGMA table_info({t})")).unwrap();
            let rows = stmt.query_map([], |r| {
                let dflt: Option<String> = r.get(4)?;
                Ok(format!("table|{t}|{}|{}|{}|{}|{}|{}", r.get::<_, i64>(0)?, r.get::<_, String>(1)?,
                           r.get::<_, String>(2)?, r.get::<_, i64>(3)?, dflt.unwrap_or_else(|| "None".into()),
                           r.get::<_, i64>(5)?))
            }).unwrap();
            lines.extend(rows.map(Result::unwrap));
        }
        let idx: Vec<String> = conn.prepare("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name \
            NOT LIKE 'sqlite_%' AND tbl_name NOT LIKE 'agentd_%' ORDER BY name").unwrap()
            .query_map([], |r| Ok(format!("index|{}|{}", r.get::<_, String>(0)?, r.get::<_, String>(1)?))).unwrap()
            .map(Result::unwrap).collect();
        lines.extend(idx);
        lines
    }

    #[test]
    fn schema_matches_python_core() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(&dir.path().join("agent.db")).unwrap();
        let want: Vec<String> = std::fs::read_to_string("tests/fixtures/core_schema.txt").unwrap()
            .lines().map(String::from).collect();
        assert_eq!(dump(&db.lock()), want);
    }

    #[test]
    fn added_columns_are_migrated_and_backfilled() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("CREATE TABLE conversations (id TEXT PRIMARY KEY, channel TEXT, thread_ts TEXT, \
                project TEXT, task_id TEXT, session_id TEXT, is_primary INTEGER NOT NULL DEFAULT 1, waiting INTEGER \
                NOT NULL DEFAULT 0, model TEXT, created_at TEXT NOT NULL, last_active_at TEXT NOT NULL);
                INSERT INTO conversations (id, session_id, created_at, last_active_at) VALUES ('c1', 's1', 'x', 'x');
                CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL \
                DEFAULT 'running', session_id TEXT, account_code TEXT, forks INTEGER NOT NULL DEFAULT 0, outcome \
                TEXT, turns INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens \
                INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens \
                INTEGER NOT NULL DEFAULT 0, cost_usd_est REAL NOT NULL DEFAULT 0, transcript_path TEXT, error TEXT, \
                started_at TEXT NOT NULL, ended_at TEXT);
                INSERT INTO runs (id, kind, session_id, account_code, started_at, ended_at) \
                VALUES ('W-1', 'turn', 's1', 'acct-a', 'x', '2026-01-01');").unwrap();
        }
        let db = Db::open(&path).unwrap();
        let conn = db.lock();
        let account: Option<String> = conn.query_row("SELECT session_account FROM conversations WHERE id='c1'", [],
                                                     |r| r.get(0)).unwrap();
        assert_eq!(account.as_deref(), Some("acct-a"));
        conn.query_row("SELECT cwd FROM conversations", [], |r| r.get::<_, Option<String>>(0)).unwrap();
    }

    #[test]
    fn settings_round_trip_and_rows_survive_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent.db");
        set_setting(&Db::open(&path).unwrap().lock(), "paused", "1").unwrap();
        let db = Db::open(&path).unwrap();
        assert_eq!(get_setting(&db.lock(), "paused").unwrap().as_deref(), Some("1"));
        assert_eq!(get_setting(&db.lock(), "missing").unwrap(), None);
        assert!(utcnow().ends_with('Z') && utcnow().len() == 20);
    }
}
