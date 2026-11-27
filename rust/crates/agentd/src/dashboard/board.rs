//! The Tasks page's read model: every conversation that is going, or was active in the last day, with what it has
//! said about its work (progress.rs), grouped by the Slack channel it is in. Slack's permalinks and channel names
//! need I/O, so the API layer fills `slack_url` and passes the names to `channels`.

use std::collections::HashMap;

use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use rusqlite::{Connection, params_from_iter};
use serde::Serialize;
use serde_json::Value;

use super::queries::{self, Row};
use crate::db::marks;
use crate::kpis::FMT;
use crate::progress::{self, OpenKind, Step};

/// A finished conversation stays on the board this long after its last activity.
pub const WINDOW: Duration = Duration::hours(24);
/// A task's status once its work is over; any other keeps a conversation whose turns are done on the board as idle.
const TASK_CLOSED: [&str; 4] = ["done", "merged", "closed", "cancelled"];
const DMS: &str = "dm";
const OUTSIDE: &str = "none";

#[derive(Debug, Serialize)]
pub struct Task {
    pub conversation_id: String,
    pub title: Value,
    pub state: &'static str,
    pub source: Value,
    pub project: Value,
    pub requester: Value,
    pub started_at: Value,
    pub last_active_at: Value,
    pub update: Option<Update>,
    pub steps: Vec<Step>,
    pub attention: Vec<Attention>,
    pub slack_url: Option<String>,
    pub parent: Option<Parent>,
    /// Where its thread is, for its permalink.
    #[serde(skip)]
    pub channel: Option<String>,
    #[serde(skip)]
    pub thread_ts: Option<String>,
    /// The channel it shows under: its own, or for a helper agent its parent's.
    #[serde(skip)]
    pub group: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Update {
    pub text: String,
    pub at: String,
}

#[derive(Debug, Serialize)]
pub struct Attention {
    pub id: i64,
    pub kind: OpenKind,
    pub text: String,
    pub at: String,
    pub slack_url: Option<String>,
    /// The message a flag was posted as; a question links to its thread.
    #[serde(skip)]
    pub slack_ts: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Parent {
    pub conversation_id: String,
    pub title: Value,
}

#[derive(Debug, Serialize)]
pub struct Channel {
    pub key: String,
    pub label: String,
    pub kind: &'static str,
    pub tasks: Vec<Task>,
}

struct Facts {
    channel: Option<String>,
    thread_ts: Option<String>,
    waiting: bool,
    parked: bool,
    task_status: Option<String>,
    parent: Option<String>,
}

/// The board's state for a conversation: what its turns are doing, else what it waits on, else how its last turn
/// ended; a finished turn of a task still open leaves it idle rather than done.
fn state(turns_state: &str, f: &Facts) -> &'static str {
    match turns_state {
        "running" => "running",
        "queued" => "queued",
        _ if f.waiting => "waiting",
        _ if f.parked => "parked",
        "failed" => "failed",
        "cancelled" => "cancelled",
        _ if f.task_status.as_deref().is_some_and(|s| !TASK_CLOSED.contains(&s)) => "idle",
        _ => "done",
    }
}

fn facts(c: &Connection, ids: &[String]) -> Result<HashMap<String, Facts>> {
    let sql = format!("SELECT cv.id, cv.channel, cv.thread_ts, cv.waiting, t.status, \
                              EXISTS (SELECT 1 FROM parks p WHERE p.conversation_id = cv.id AND p.ended_at IS NULL), \
                              sa.parent_conversation_id \
                       FROM conversations cv LEFT JOIN tasks t ON t.id = cv.task_id \
                       LEFT JOIN spawned_agents sa ON sa.child_conversation_id = cv.id WHERE cv.id IN ({})", marks(ids.len()));
    let rows = c.prepare(&sql)?.query_map(params_from_iter(ids), |r| Ok((r.get::<_, String>(0)?, Facts {
        channel: r.get(1)?, thread_ts: r.get(2)?, waiting: r.get(3)?, task_status: r.get(4)?, parked: r.get(5)?,
        parent: r.get(6)?,
    })))?.collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

/// The conversations on the board, latest activity first.
pub fn tasks(c: &Connection, now: DateTime<Utc>) -> Result<Vec<Task>> {
    let since = (now - WINDOW).format(FMT).to_string();
    let ids: Vec<String> = c.prepare(
        "SELECT cv.id FROM conversations cv WHERE cv.last_active_at >= ?1 OR cv.waiting = 1 \
         OR EXISTS (SELECT 1 FROM work_items w WHERE w.conversation_id = cv.id AND w.status IN ('queued', 'claimed')) \
         OR EXISTS (SELECT 1 FROM parks p WHERE p.conversation_id = cv.id AND p.ended_at IS NULL)")?
        .query_map([&since], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let mut facts = facts(c, &ids)?;
    let parents: Vec<String> = facts.values().filter_map(|f| f.parent.clone()).collect();
    let mut keys = ids.clone();
    keys.extend(parents.iter().cloned());
    let rows: HashMap<String, Row> = queries::turns(c, &keys)?.into_iter()
        .map(|(id, turns)| (id, queries::conversation_row(&turns, now))).collect();
    let parent_channel = parent_channels(c, &parents)?;
    let mut said = progress::said(c, &ids)?;
    let mut out: Vec<Task> = ids.iter().filter_map(|id| {
        let row = rows.get(id)?;
        let f = facts.remove(id)?;
        let said = said.remove(id).unwrap_or_default();
        let parent = f.parent.as_ref().map(|p| Parent {
            conversation_id: p.clone(), title: rows.get(p).map_or(Value::Null, |r| r["title"].clone()) });
        let group = f.channel.clone().or_else(|| f.parent.as_ref().and_then(|p| parent_channel.get(p).cloned()));
        Some(Task {
            conversation_id: id.clone(), title: row["title"].clone(), state: state(row["state"].as_str().unwrap_or_default(), &f),
            source: row["source"].clone(), project: row["project"].clone(), requester: row["requester"].clone(),
            started_at: if row["started_at"].is_null() { row["created_at"].clone() } else { row["started_at"].clone() },
            last_active_at: row["last_active_at"].clone(),
            update: said.update.map(|(text, at)| Update { text, at }), steps: said.steps,
            attention: said.open.into_iter().map(|o| Attention { id: o.id, kind: o.kind, text: o.text, at: o.at,
                                                                 slack_url: None, slack_ts: o.slack_ts }).collect(),
            slack_url: None, parent, channel: f.channel, thread_ts: f.thread_ts, group,
        })
    }).collect();
    out.sort_by(|a, b| b.last_active_at.as_str().cmp(&a.last_active_at.as_str()));
    Ok(out)
}

/// Each parent conversation's channel.
fn parent_channels(c: &Connection, parents: &[String]) -> Result<HashMap<String, String>> {
    if parents.is_empty() {
        return Ok(HashMap::new());
    }
    let sql = format!("SELECT id, channel FROM conversations WHERE channel IS NOT NULL AND id IN ({})", marks(parents.len()));
    let rows = c.prepare(&sql)?.query_map(params_from_iter(parents), |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

/// Whether a Slack channel id is a direct message.
fn is_dm(channel: &str) -> bool {
    channel.starts_with('D')
}

/// The channels `tasks` will be shown under, besides DMs: the ones whose names the page needs.
pub fn named_channels(tasks: &[Task]) -> Vec<String> {
    let mut out: Vec<String> = tasks.iter().filter_map(|t| t.group.clone()).filter(|g| !is_dm(g)).collect();
    out.sort();
    out.dedup();
    out
}

/// The board's tabs: a channel's tasks under its name (its id when Slack gave none), every DM together, and the
/// conversations outside Slack. Channels come in order of their latest activity, outside Slack last; each keeps its
/// tasks' order.
pub fn channels(tasks: Vec<Task>, names: &HashMap<String, String>) -> Vec<Channel> {
    let mut out: Vec<Channel> = Vec::new();
    for task in tasks {
        let (key, label, kind) = match task.group.as_deref() {
            Some(g) if is_dm(g) => (DMS.to_string(), "Direct messages".to_string(), "dm"),
            Some(g) => (g.to_string(), names.get(g).filter(|n| !n.is_empty()).cloned().unwrap_or_else(|| g.to_string()), "channel"),
            None => (OUTSIDE.to_string(), "Outside Slack".to_string(), "none"),
        };
        match out.iter_mut().find(|ch| ch.key == key) {
            Some(ch) => ch.tasks.push(task),
            None => out.push(Channel { key, label, kind, tasks: vec![task] }),
        }
    }
    out.sort_by_key(|ch| ch.kind == "none");
    out
}
