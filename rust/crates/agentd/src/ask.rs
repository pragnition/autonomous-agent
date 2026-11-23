//! `agentd ask`: hand the agent a turn from the host, without Slack, into an existing conversation or a fresh one
//! with no thread (a port of scripts/smoke.py and watch/core_ask.py). Nothing is posted; the answer is the work item's
//! result.

use std::time::Duration;

use anyhow::{Result, anyhow};
use serde_json::json;

use crate::conversations as cv;
use crate::db::immediate;
use crate::queue::{self, WorkItem};
use crate::shared::Shared;

/// Queue `text` as an unattended turn in `conversation`, or in a fresh `<new_prefix><8 hex>` conversation; returns the
/// work item. `source` and `origin` mark whose turn it is (improve-jensen's reviews carry their own).
pub fn ask(s: &Shared, conversation: Option<&str>, new_prefix: &str, text: &str, source: Option<&str>, origin: Option<&str>) -> Result<String> {
    let c = s.db.lock();
    let convo = immediate(&c, |c| {
        let id = match conversation {
            Some(id) => id.to_string(),
            None => {
                let (id, now) = (format!("{new_prefix}{}", &crate::turn::uuid4().replace('-', "")[..8]), crate::db::utcnow());
                c.execute("INSERT INTO conversations(id, is_primary, created_at, last_active_at) VALUES (?, 0, ?, ?)",
                          rusqlite::params![id, now, now])?;
                id
            }
        };
        cv::row(c, &id)?.ok_or_else(|| anyhow!("no conversation {id}"))
    })?;
    drop(c);
    let mut payload = crate::payloads::turn(s, &convo, text, None, true)?;
    if let Some(source) = source {
        payload["source"] = json!(source);
    }
    if let Some(origin) = origin {
        payload["origin"] = json!(origin);
    }
    let id = queue::enqueue(&s.db.lock(), Some(&convo.id), "turn", &payload)?;
    s.wake.notify_one();
    Ok(id)
}

/// The item once it is done, failed or cancelled, or an error after `limit`.
pub async fn wait(s: &Shared, item: &str, limit: Duration) -> Result<WorkItem> {
    let deadline = tokio::time::Instant::now() + limit;
    loop {
        let current = queue::get(&s.db.lock(), item)?.ok_or_else(|| anyhow!("no work item {item}"))?;
        if matches!(current.status.as_str(), "done" | "failed" | "cancelled") {
            return Ok(current);
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(anyhow!("{item} is still {} after {} s", current.status, limit.as_secs()));
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}
