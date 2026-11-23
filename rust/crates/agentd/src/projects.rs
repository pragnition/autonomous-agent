//! Long projects: each builds in one conversation, which agentd tracks by name so every session can see it and pass
//! it changes (a port of core/projects.py).

use anyhow::Result;
use rusqlite::Connection;

/// "running" with a claimed turn, "queued" with a queued one, else "idle".
pub fn build_state(c: &Connection, conversation_id: &str) -> Result<&'static str> {
    let statuses: Vec<String> = c.prepare("SELECT status FROM work_items WHERE conversation_id = ? AND kind = 'turn'")?
        .query_map([conversation_id], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    Ok(if statuses.iter().any(|s| s == "claimed") { "running" } else if statuses.iter().any(|s| s == "queued") { "queued" } else { "idle" })
}
