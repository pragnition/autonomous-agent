//! What a conversation tells people about its work, for the dashboard's Tasks page: each `post_update` (its text, the
//! checklist it may carry and a flag that it needs someone) and each question it asks with `ask`. A flag or question
//! is open until a person writes in the conversation's thread or, for a flag, someone dismisses it on the page.

use std::collections::HashMap;

use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::db::{marks, utcnow};

/// A checklist holds at most this many steps, each at most this many characters; the rest is dropped.
const MAX_STEPS: usize = 30;
const STEP_CHARS: usize = 200;

/// A step's status: Claude Code's own todo-list statuses, plus `blocked` for a step waiting on someone.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepStatus {
    Pending,
    InProgress,
    Completed,
    Blocked,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Step {
    pub content: String,
    pub status: StepStatus,
}

/// The `steps` a tool call sent: None when it sent none, else the checklist or what is wrong with it.
pub fn parse_steps(v: &Value) -> Option<Result<Vec<Step>, String>> {
    if v.is_null() {
        return None;
    }
    let parsed = serde_json::from_value::<Vec<Step>>(v.clone()).map_err(|_| {
        "steps must be a list of {content, status}, status one of pending, in_progress, completed, blocked".to_string()
    });
    Some(parsed.map(|steps| {
        steps.into_iter()
            .map(|s| Step { content: s.content.trim().chars().take(STEP_CHARS).collect(), status: s.status })
            .filter(|s| !s.content.is_empty())
            .take(MAX_STEPS)
            .collect()
    }))
}

/// An update as the conversation posted it; `text` already scrubbed.
pub struct Update<'a> {
    pub text: &'a str,
    pub steps: Option<&'a [Step]>,
    pub needs_attention: bool,
    pub slack_ts: Option<&'a str>,
}

pub fn record_update(c: &Connection, conversation: &str, work_item: &str, u: &Update) -> Result<i64> {
    let steps = u.steps.map(serde_json::to_string).transpose()?;
    c.execute("INSERT INTO agentd_progress(conversation_id, work_item_id, kind, text, steps, attention, slack_ts, created_at) \
               VALUES (?, ?, 'update', ?, ?, ?, ?, ?)",
              params![conversation, work_item, u.text, steps, u.needs_attention, u.slack_ts, utcnow()])?;
    Ok(c.last_insert_rowid())
}

/// A question the conversation asked; `text` already scrubbed. It is open while the conversation waits on it.
pub fn record_question(c: &Connection, conversation: &str, work_item: &str, text: &str) -> Result<i64> {
    c.execute("INSERT INTO agentd_progress(conversation_id, work_item_id, kind, text, attention, created_at) \
               VALUES (?, ?, 'question', ?, 1, ?)", params![conversation, work_item, text, utcnow()])?;
    Ok(c.last_insert_rowid())
}

/// A person wrote in the conversation's thread: whatever it needed someone for is answered.
pub fn close_on_reply(c: &Connection, conversation: &str) -> Result<()> {
    c.execute("UPDATE agentd_progress SET closed_at = ?, closed_by = 'reply' \
               WHERE conversation_id = ? AND attention = 1 AND closed_at IS NULL", params![utcnow(), conversation])?;
    Ok(())
}

/// Dismiss an open flag; a question isn't dismissed, it is answered. Whether this closed it.
pub fn dismiss(c: &Connection, id: i64, by: &str) -> Result<bool> {
    let n = c.execute("UPDATE agentd_progress SET closed_at = ?, closed_by = ? \
                       WHERE id = ? AND kind = 'update' AND attention = 1 AND closed_at IS NULL", params![utcnow(), by, id])?;
    Ok(n == 1)
}

/// Whether there is a flag `id`, open or not; tells a stale dismiss from a wrong one.
pub fn flag_exists(c: &Connection, id: i64) -> Result<bool> {
    Ok(c.query_row("SELECT 1 FROM agentd_progress WHERE id = ? AND kind = 'update' AND attention = 1", [id], |_| Ok(()))
        .optional()?.is_some())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OpenKind {
    Question,
    Look,
}

/// Something open that the conversation needs a person for.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Open {
    pub id: i64,
    pub kind: OpenKind,
    pub text: String,
    pub at: String,
    pub slack_ts: Option<String>,
}

/// Where a conversation stands by what it has said: its latest update, its latest checklist and what is open.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Said {
    pub update: Option<(String, String)>,
    pub steps: Vec<Step>,
    pub open: Vec<Open>,
}

/// What each of `conversations` has said. A question counts as open only while its conversation is still waiting on
/// an answer, and only the latest one; an older question was overtaken by the next.
pub fn said(c: &Connection, conversations: &[String]) -> Result<HashMap<String, Said>> {
    let mut out: HashMap<String, Said> = HashMap::new();
    if conversations.is_empty() {
        return Ok(out);
    }
    let sql = format!("SELECT p.id, p.conversation_id, p.kind, p.text, p.steps, p.attention, p.slack_ts, p.created_at, \
                              p.closed_at, cv.waiting FROM agentd_progress p JOIN conversations cv ON cv.id = p.conversation_id \
                       WHERE p.conversation_id IN ({}) ORDER BY p.id", marks(conversations.len()));
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(params_from_iter(conversations), |r| Ok(Row {
        id: r.get(0)?, conversation: r.get(1)?, kind: r.get(2)?, text: r.get(3)?, steps: r.get(4)?,
        attention: r.get(5)?, slack_ts: r.get(6)?, at: r.get(7)?, closed: r.get::<_, Option<String>>(8)?.is_some(),
        waiting: r.get(9)?,
    }))?;
    for row in rows {
        let row = row?;
        let s = out.entry(row.conversation.clone()).or_default();
        let open = |kind| Open { id: row.id, kind, text: row.text.clone(), at: row.at.clone(), slack_ts: row.slack_ts.clone() };
        if row.kind == "question" {
            s.open.retain(|o| o.kind != OpenKind::Question);
            if row.waiting && !row.closed {
                s.open.push(open(OpenKind::Question));
            }
            continue;
        }
        if let Some(steps) = row.steps.as_deref().and_then(|j| serde_json::from_str(j).ok()) {
            s.steps = steps;
        }
        if row.attention && !row.closed {
            s.open.push(open(OpenKind::Look));
        }
        s.update = Some((row.text, row.at));
    }
    Ok(out)
}

struct Row {
    id: i64,
    conversation: String,
    kind: String,
    text: String,
    steps: Option<String>,
    attention: bool,
    slack_ts: Option<String>,
    at: String,
    closed: bool,
    waiting: bool,
}
