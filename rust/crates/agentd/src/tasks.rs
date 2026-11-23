//! Tasks and their lifecycle: every change is one transaction that also appends an event (a port of tasks.py).

use std::str::FromStr;

use anyhow::{Result, anyhow, bail};
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Value, json};

use crate::db::{append_event, immediate, utcnow};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Queued,
    Working,
    NeedsInput,
    ReadyForReview,
    Done,
    Merged,
    Closed,
    Failed,
    Cancelled,
}

const ALL: [(Status, &str); 9] = [(Status::Queued, "queued"), (Status::Working, "working"),
    (Status::NeedsInput, "needs_input"), (Status::ReadyForReview, "ready_for_review"), (Status::Done, "done"),
    (Status::Merged, "merged"), (Status::Closed, "closed"), (Status::Failed, "failed"), (Status::Cancelled, "cancelled")];

impl Status {
    pub fn as_str(self) -> &'static str {
        ALL.iter().find(|(s, _)| *s == self).map(|(_, n)| *n).unwrap_or_default()
    }

    fn terminal(self) -> bool {
        matches!(self, Status::Done | Status::Merged | Status::Closed | Status::Cancelled)
    }

    /// The agent decides the work; these moves only record where a task stands.
    fn may_become(self, to: Status) -> bool {
        use Status::*;
        let legal = match self {
            Queued => &[Working][..],
            Working => &[NeedsInput, ReadyForReview, Done, Failed, Queued][..],
            NeedsInput => &[Working, Queued][..],
            ReadyForReview => &[Merged, Closed, Working][..],
            Failed => &[Queued][..],
            _ => &[][..],
        };
        legal.contains(&to) || (to == Cancelled && !self.terminal())
    }
}

impl FromStr for Status {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Status> {
        ALL.iter().find(|(_, n)| *n == s).map(|(st, _)| *st).ok_or_else(|| anyhow!("unknown status '{s}'"))
    }
}

impl std::fmt::Display for Status {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Task {
    pub id: String,
    pub project: String,
    pub title: String,
    pub description: String,
    pub kind: String,
    pub source: String,
    pub source_ref: Option<String>,
    pub requester: Option<String>,
    pub owner: Option<String>,
    pub status: Status,
    pub branch: Option<String>,
    pub pr_number: Option<i64>,
    pub pr_url: Option<String>,
    pub linear_issue_id: Option<String>,
    pub slack_channel: Option<String>,
    pub slack_thread_ts: Option<String>,
    pub status_message_ts: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

const COLUMNS: [&str; 19] = ["id", "project", "title", "description", "kind", "source", "source_ref", "requester",
    "owner", "status", "branch", "pr_number", "pr_url", "linear_issue_id", "slack_channel", "slack_thread_ts",
    "status_message_ts", "created_at", "updated_at"];
const PROTECTED: [&str; 4] = ["id", "status", "created_at", "updated_at"];

fn task(r: &Row) -> rusqlite::Result<Task> {
    let status: String = r.get("status")?;
    Ok(Task { id: r.get("id")?, project: r.get("project")?, title: r.get("title")?, description: r.get("description")?,
              kind: r.get("kind")?, source: r.get("source")?, source_ref: r.get("source_ref")?,
              requester: r.get("requester")?, owner: r.get("owner")?, status: status.parse().unwrap_or(Status::Queued),
              branch: r.get("branch")?, pr_number: r.get("pr_number")?, pr_url: r.get("pr_url")?,
              linear_issue_id: r.get("linear_issue_id")?, slack_channel: r.get("slack_channel")?,
              slack_thread_ts: r.get("slack_thread_ts")?, status_message_ts: r.get("status_message_ts")?,
              created_at: r.get("created_at")?, updated_at: r.get("updated_at")? })
}

#[derive(Clone, Debug, Default)]
pub struct NewTask {
    pub project: String,
    pub title: String,
    pub description: String,
    pub kind: String,
    pub source: String,
    pub source_ref: Option<String>,
    pub requester: Option<String>,
    pub owner: Option<String>,
    pub slack_channel: Option<String>,
    pub slack_thread_ts: Option<String>,
    pub linear_issue_id: Option<String>,
}

pub fn create(conn: &Connection, t: NewTask) -> Result<Task> {
    let id = immediate(conn, |c| {
        let n: Option<i64> = c.query_row("SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) FROM tasks", [], |r| r.get(0))?;
        let (id, now) = (format!("T-{:04}", n.unwrap_or(0) + 1), utcnow());
        c.execute("INSERT INTO tasks(id, project, title, description, kind, source, source_ref, requester, owner, \
                   status, linear_issue_id, slack_channel, slack_thread_ts, created_at, updated_at) \
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                  params![id, t.project, t.title, t.description, t.kind, t.source, t.source_ref, t.requester, t.owner,
                          t.linear_issue_id, t.slack_channel, t.slack_thread_ts, now, now])?;
        append_event(c, "task_created", Some(&id), t.requester.as_deref(),
                     Some(&json!({"project": t.project, "kind": t.kind, "source": t.source})))?;
        Ok(id)
    })?;
    get(conn, &id)?.ok_or_else(|| anyhow!("{id} vanished"))
}

pub fn get(conn: &Connection, id: &str) -> Result<Option<Task>> {
    Ok(conn.query_row("SELECT * FROM tasks WHERE id = ?", [id], task).optional()?)
}

/// Move a task; an illegal move is an error saying so (`T-0001: done -> working is not allowed`).
pub fn transition(conn: &Connection, id: &str, to: Status, actor: &str, data: Option<Value>) -> Result<Task> {
    immediate(conn, |c| {
        let Some(t) = get(c, id)? else { bail!("{id}: no such task") };
        if !t.status.may_become(to) {
            bail!("{id}: {} -> {to} is not allowed", t.status);
        }
        c.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", params![to.as_str(), utcnow(), id])?;
        let mut event = json!({"from": t.status.as_str(), "to": to.as_str()});
        if let (Some(Value::Object(extra)), Some(map)) = (data, event.as_object_mut()) {
            map.extend(extra);
        }
        append_event(c, "status_changed", Some(id), Some(actor), Some(&event))?;
        Ok(())
    })?;
    get(conn, id)?.ok_or_else(|| anyhow!("{id} vanished"))
}

/// Plain fields; the status only moves through `transition`.
pub fn set_fields(conn: &Connection, id: &str, values: &[(&str, Option<&str>)]) -> Result<Task> {
    let bad: Vec<&str> = values.iter().map(|(k, _)| *k).filter(|k| !COLUMNS.contains(k) || PROTECTED.contains(k)).collect();
    if !bad.is_empty() {
        bail!("cannot set {bad:?}");
    }
    let sets: Vec<String> = values.iter().map(|(k, _)| format!("{k} = ?")).collect();
    let mut args: Vec<Option<String>> = values.iter().map(|(_, v)| v.map(str::to_string)).collect();
    args.extend([Some(utcnow()), Some(id.to_string())]);
    conn.execute(&format!("UPDATE tasks SET {}, updated_at = ? WHERE id = ?", sets.join(", ")),
                 rusqlite::params_from_iter(args))?;
    get(conn, id)?.ok_or_else(|| anyhow!("no task {id}"))
}

pub fn list(conn: &Connection, status: Option<&str>, project: Option<&str>) -> Result<Vec<Task>> {
    let mut sql = "SELECT * FROM tasks WHERE 1=1".to_string();
    let mut args = Vec::new();
    if let Some(s) = status {
        sql += " AND status = ?";
        args.push(s.parse::<Status>()?.as_str().to_string());
    }
    if let Some(p) = project {
        sql += " AND project = ?";
        args.push(p.to_string());
    }
    let rows = conn.prepare(&(sql + " ORDER BY id"))?
        .query_map(rusqlite::params_from_iter(args), task)?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}
