//! Spawned agents: sessions a session starts that outlive its turn and report back to it (a port of
//! core/spawned.py). Each runs in its own conversation with no Slack thread; once finished (its turn ended with
//! nothing queued, parked or waiting in its conversation) its outcome and final message are queued as a message in
//! the spawning conversation. One that fails reports the failure and isn't retried.

use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

use crate::db::{append_event, row_json, utcnow};

/// Spawned agents one conversation may have running at once, and all conversations together.
pub const MAX_RUNNING: i64 = 3;
pub const MAX_RUNNING_ALL: i64 = 4;
const REPORT_CHARS: usize = 20_000;
const BRIEF_CHARS: usize = 300;

pub const CHILD_NOTE: &str = "(You are agent {id}, an agent another session spawned with spawn_agent. You have no Slack \
    thread and nobody watches you work. When you're done, your final message is delivered to the session that spawned \
    you, so make it a complete, self-contained report of what you found or did. Don't ask questions: decide, say what \
    you assumed and finish.)\n\n";

pub fn new_id() -> String {
    format!("A-{}", &crate::turn::uuid4()[..8])
}

/// Still to run in a conversation: a turn queued, running or delivered into one, a park not yet carried on, or a
/// turn a usage limit cut off that hasn't been carried on.
fn more_sql(c: &str) -> String {
    format!("EXISTS (SELECT 1 FROM work_items w WHERE w.conversation_id = {c} AND w.status IN ('queued', 'claimed', 'delivered')) \
             OR EXISTS (SELECT 1 FROM parks p WHERE p.conversation_id = {c} AND p.ended_at IS NULL) \
             OR EXISTS (SELECT 1 FROM limit_waits l WHERE l.conversation_id = {c} AND l.resumed_at IS NULL)")
}

pub fn more_to_come(conn: &Connection, conversation_id: &str) -> Result<bool> {
    Ok(conn.query_row(&format!("SELECT {}", more_sql("?1")), [conversation_id], |r| r.get(0))?)
}

fn running() -> String {
    format!("s.status = 'running' AND ({})", more_sql("s.child_conversation_id"))
}

/// The agents a conversation spawned that are still running (parked or waiting out a usage limit too).
pub fn running_count(conn: &Connection, parent: &str) -> Result<i64> {
    Ok(conn.query_row(&format!("SELECT COUNT(*) FROM spawned_agents s WHERE s.parent_conversation_id = ? AND {}", running()),
                      [parent], |r| r.get(0))?)
}

pub fn running_total(conn: &Connection) -> Result<i64> {
    Ok(conn.query_row(&format!("SELECT COUNT(*) FROM spawned_agents s WHERE {}", running()), [], |r| r.get(0))?)
}

pub fn is_spawned(conn: &Connection, conversation_id: &str) -> Result<bool> {
    Ok(conn.query_row("SELECT 1 FROM spawned_agents WHERE child_conversation_id = ?", [conversation_id], |_| Ok(()))
        .optional()?.is_some())
}

/// `parent` is what the report-back turn needs from the spawning turn: actor, unattended, origin, source, requester.
#[allow(clippy::too_many_arguments)]
pub fn record(conn: &Connection, agent_id: &str, parent_conversation: &str, child_conversation: &str, spawned_by: &str,
              prompt: &str, cwd: Option<&str>, parent: &Value) -> Result<()> {
    let text = |k: &str| parent[k].as_str().map(str::to_string);
    conn.execute("INSERT INTO spawned_agents(id, parent_conversation_id, child_conversation_id, spawned_by, actor, \
                  unattended, origin, source, requester, prompt, cwd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 params![agent_id, parent_conversation, child_conversation, spawned_by, text("actor"),
                         parent["unattended"].as_bool().unwrap_or(false) as i64, text("origin").unwrap_or_default(),
                         text("source").unwrap_or_else(|| "slack".into()), text("requester"), prompt, cwd, utcnow()])?;
    append_event(conn, "agent_spawned", None, text("actor").as_deref(),
                 Some(&json!({"agent": agent_id, "parent": parent_conversation, "child": child_conversation, "cwd": cwd})))?;
    Ok(())
}

pub fn running_for_child(conn: &Connection, child: &str) -> Result<Option<Value>> {
    Ok(conn.query_row("SELECT * FROM spawned_agents WHERE child_conversation_id = ? AND status = 'running'", [child],
                      row_json).optional()?)
}

/// Mark a running agent finished; false when it already was, so it reports once.
pub fn finish(conn: &Connection, agent_id: &str, status: &str, outcome: &str, text: &str) -> Result<bool> {
    Ok(conn.execute("UPDATE spawned_agents SET status = ?, outcome = ?, result = ?, finished_at = ? \
                     WHERE id = ? AND status = 'running'", params![status, outcome, text, utcnow(), agent_id])? == 1)
}

/// The message the spawning session gets when one of its agents finishes.
pub fn report_text(row: &Value, status: &str, outcome: &str, text: &str) -> String {
    let brief = row["prompt"].as_str().unwrap_or_default().split_whitespace().collect::<Vec<_>>().join(" ");
    let brief = if brief.chars().count() > BRIEF_CHARS { brief.chars().take(BRIEF_CHARS).collect::<String>() + "…" } else { brief };
    let trimmed = text.trim();
    let mut report = if trimmed.is_empty() { "(it ended without a final message)".to_string() } else { trimmed.to_string() };
    if report.chars().count() > REPORT_CHARS {
        report = report.chars().take(REPORT_CHARS).collect::<String>() + &format!("\n[cut: {} characters in all]", text.chars().count());
    }
    let how = if status == "done" { "finished".to_string() }
              else { format!("failed ({outcome}). It won't be retried: spawn it again if the work still needs it") };
    format!("<agent-notification>\nAgent {}, which you spawned with spawn_agent, {how}.\nIts brief: {brief}\nIts report:\n\
             {report}\n</agent-notification>\nAct on it: carry on with the work it was part of, and tell the thread what it \
             found when that matters to them. Agents you spawned that are still running report back the same way.",
            row["id"].as_str().unwrap_or_default())
}
