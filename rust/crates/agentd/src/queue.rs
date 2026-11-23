//! The work queue (a port of core/sessions.py's WorkQueue). One queue holds every conversation's turns; a
//! conversation never has two items running at once, so a second message in a busy thread is delivered into the
//! running turn or becomes the next turn of the same session.
//!
//! Without a separate runner there are no leases: `claimed` means agentd is running the item. At start, a claimed
//! item with no turn agentd is carrying on goes back in the queue (attempts + 1, failed at the fifth).

use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Value, json};

use crate::db::{get_setting, immediate, utcnow};
use crate::records::py_dumps;

/// Set by the usage watch while the pool is down to its last account with room: scheduled runs wait in the queue
/// and people's requests still start.
pub const HOLD_SCHEDULED: &str = "usage_hold_scheduled";
pub const PAUSED: &str = "paused";
const MAX_ATTEMPTS: i64 = 5;

#[derive(Clone, Debug, PartialEq)]
pub struct WorkItem {
    pub id: String,
    pub conversation_id: Option<String>,
    pub kind: String,
    pub payload: Value,
    pub status: String,
    pub attempts: i64,
    pub result: Option<Value>,
    pub created_at: String,
}

fn item(r: &Row) -> rusqlite::Result<WorkItem> {
    let json = |s: Option<String>| s.and_then(|s| serde_json::from_str(&s).ok());
    Ok(WorkItem { id: r.get("id")?, conversation_id: r.get("conversation_id")?, kind: r.get("kind")?,
                  payload: json(r.get("payload")?).unwrap_or(Value::Null), status: r.get("status")?,
                  attempts: r.get("attempts")?, result: json(r.get("result")?), created_at: r.get("created_at")? })
}

pub fn enqueue(c: &Connection, conversation_id: Option<&str>, kind: &str, payload: &Value) -> Result<String> {
    let (id, now) = (format!("W-{}", &crate::turn::uuid4().replace('-', "")[..12]), utcnow());
    c.execute("INSERT INTO work_items(id, conversation_id, kind, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
              params![id, conversation_id, kind, py_dumps(payload), now, now])?;
    Ok(id)
}

pub fn get(c: &Connection, id: &str) -> Result<Option<WorkItem>> {
    Ok(c.query_row("SELECT * FROM work_items WHERE id = ?", [id], item).optional()?)
}

pub fn set_payload(c: &Connection, id: &str, payload: &Value) -> Result<()> {
    c.execute("UPDATE work_items SET payload = ?, updated_at = ? WHERE id = ?", params![py_dumps(payload), utcnow(), id])?;
    Ok(())
}

/// Queued items whose conversation has nothing claimed; schedules held back while the pool is low.
fn claimable(c: &Connection) -> Result<String> {
    let held = get_setting(c, HOLD_SCHEDULED)?.as_deref() == Some("1");
    Ok("w.status = 'queued' AND (w.conversation_id IS NULL OR NOT EXISTS (SELECT 1 FROM work_items o WHERE \
        o.conversation_id = w.conversation_id AND o.status = 'claimed'))".to_string()
       + if held { " AND COALESCE(json_extract(w.payload, '$.source'), '') != 'schedule'" } else { "" })
}

pub fn has_claimable(c: &Connection) -> Result<bool> {
    Ok(c.query_row(&format!("SELECT 1 FROM work_items w WHERE {} LIMIT 1", claimable(c)?), [], |_| Ok(()))
        .optional()?.is_some())
}

/// The oldest claimable item, now claimed.
pub fn claim(c: &Connection) -> Result<Option<WorkItem>> {
    immediate(c, |c| {
        let sql = format!("SELECT id FROM work_items w WHERE {} ORDER BY w.created_at, w.rowid LIMIT 1", claimable(c)?);
        let Some(id) = c.query_row(&sql, [], |r| r.get::<_, String>(0)).optional()? else { return Ok(None) };
        c.execute("UPDATE work_items SET status = 'claimed', lease_until = NULL, updated_at = ? WHERE id = ?",
                  params![utcnow(), id])?;
        get(c, &id)
    })
}

/// Done, with its result. The messages the result says the turn read are marked done first, so they are never among
/// the ones handed back to the queue.
pub fn complete(c: &Connection, id: &str, result: &Value) -> Result<()> {
    immediate(c, |c| {
        let read: Vec<String> = result.get("read").and_then(Value::as_array).into_iter().flatten()
            .filter_map(|v| v.as_str().map(str::to_string)).collect();
        read_into(c, &read, id)?;
        c.execute("UPDATE work_items SET status = 'done', result = ?, lease_until = NULL, updated_at = ? \
                   WHERE id = ? AND status IN ('queued', 'claimed')", params![py_dumps(result), utcnow(), id])?;
        release_from(c, id)?;
        Ok(())
    })
}

pub fn cancel(c: &Connection, id: &str) -> Result<()> {
    immediate(c, |c| {
        c.execute("UPDATE work_items SET status = 'cancelled', lease_until = NULL, updated_at = ? \
                   WHERE id = ? AND status IN ('queued', 'claimed')", params![utcnow(), id])?;
        release_from(c, id)?;
        Ok(())
    })
}

/// Hand a busy conversation's queued turns to the one running, oldest first. A carry-on (its payload has
/// `continuation`) never goes in: it runs as its own turn, after whatever unread message is ahead of it.
pub fn deliver(c: &Connection, conversation_id: &str, into: &str) -> Result<Vec<WorkItem>> {
    immediate(c, |c| {
        let ids: Vec<String> = c.prepare(
            "SELECT id FROM work_items WHERE conversation_id = ? AND kind = 'turn' AND status = 'queued' AND id != ? \
             AND json_extract(payload, '$.continuation') IS NULL ORDER BY created_at, rowid")?
            .query_map(params![conversation_id, into], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        let now = utcnow();
        let mut out = Vec::new();
        for id in ids {
            c.execute("UPDATE work_items SET status = 'delivered', delivered_into = ?, updated_at = ? WHERE id = ?",
                      params![into, now, id])?;
            out.extend(get(c, &id)?);
        }
        Ok(out)
    })
}

/// The listed items, delivered into this turn, are done: it answered them. An item already done is skipped.
pub fn mark_read(c: &Connection, ids: &[String], into: &str) -> Result<Vec<WorkItem>> {
    immediate(c, |c| read_into(c, ids, into))
}

fn read_into(c: &Connection, ids: &[String], into: &str) -> Result<Vec<WorkItem>> {
    let (now, result) = (utcnow(), py_dumps(&json!({"outcome": "done", "text": "", "read_by": into})));
    let mut changed = Vec::new();
    for id in ids {
        let n = c.execute("UPDATE work_items SET status = 'done', result = ?, updated_at = ? \
                           WHERE id = ? AND status = 'delivered' AND delivered_into = ?", params![result, now, id, into])?;
        if n > 0 {
            changed.extend(get(c, id)?);
        }
    }
    Ok(changed)
}

/// Every item still delivered into this turn goes back to the queue: the turn ended without reading it.
pub fn release(c: &Connection, into: &str) -> Result<Vec<String>> {
    immediate(c, |c| release_from(c, into))
}

fn release_from(c: &Connection, into: &str) -> Result<Vec<String>> {
    let ids: Vec<String> = c.prepare("SELECT id FROM work_items WHERE status = 'delivered' AND delivered_into = ?")?
        .query_map([into], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    for id in &ids {
        c.execute("UPDATE work_items SET status = 'queued', delivered_into = NULL, updated_at = ? WHERE id = ?",
                  params![utcnow(), id])?;
    }
    Ok(ids)
}

/// The items running now, oldest first.
pub fn claimed(c: &Connection) -> Result<Vec<String>> {
    Ok(c.prepare("SELECT id FROM work_items WHERE status = 'claimed' ORDER BY created_at, rowid")?
        .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

pub fn claimed_count(c: &Connection) -> Result<usize> {
    Ok(claimed(c)?.len())
}

/// At start: a claimed item with no turn agentd is carrying on (it stopped between claiming and starting) goes back
/// in the queue, or fails at its fifth attempt. Returns how many.
pub fn requeue_orphans(c: &Connection) -> Result<usize> {
    immediate(c, |c| {
        let rows: Vec<(String, i64)> = c.prepare(
            "SELECT id, attempts FROM work_items WHERE status = 'claimed' AND id NOT IN \
             (SELECT work_item_id FROM agentd_turns WHERE state IN ('running', 'finished'))")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
        for (id, attempts) in &rows {
            let status = if attempts + 1 >= MAX_ATTEMPTS { "failed" } else { "queued" };
            c.execute("UPDATE work_items SET status = ?, attempts = ?, updated_at = ? WHERE id = ?",
                      params![status, attempts + 1, utcnow(), id])?;
            release_from(c, id)?;
        }
        Ok(rows.len())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn setup() -> (tempfile::TempDir, crate::db::Db) {
        let dir = tempfile::tempdir().unwrap();
        let db = crate::db::Db::open(&dir.path().join("a.db")).unwrap();
        (dir, db)
    }

    fn status(c: &rusqlite::Connection, id: &str) -> String {
        c.query_row("SELECT status FROM work_items WHERE id = ?", [id], |r| r.get(0)).unwrap()
    }

    #[test]
    fn one_claim_per_conversation() {
        let (_d, db) = setup();
        let c = db.lock();
        let a1 = enqueue(&c, Some("A"), "turn", &json!({"text": "1"})).unwrap();
        let a2 = enqueue(&c, Some("A"), "turn", &json!({"text": "2"})).unwrap();
        let b1 = enqueue(&c, Some("B"), "turn", &json!({"text": "3"})).unwrap();
        assert_eq!(claim(&c).unwrap().unwrap().id, a1);
        assert_eq!(claim(&c).unwrap().unwrap().id, b1, "A is busy");
        assert!(claim(&c).unwrap().is_none() && !has_claimable(&c).unwrap());
        complete(&c, &a1, &json!({"outcome": "done"})).unwrap();
        assert_eq!(claim(&c).unwrap().unwrap().id, a2);
        assert_eq!(claimed_count(&c).unwrap(), 2);
    }

    #[test]
    fn delivered_then_read_or_released() {
        let (_d, db) = setup();
        let c = db.lock();
        let a1 = enqueue(&c, Some("A"), "turn", &json!({})).unwrap();
        claim(&c).unwrap();
        let a2 = enqueue(&c, Some("A"), "turn", &json!({})).unwrap();
        let carry_on = enqueue(&c, Some("A"), "turn", &json!({"continuation": 1})).unwrap();
        let delivered: Vec<String> = deliver(&c, "A", &a1).unwrap().into_iter().map(|w| w.id).collect();
        assert_eq!(delivered, [a2.clone()], "a carry-on runs as its own turn");
        assert_eq!(mark_read(&c, &[a2.clone()], &a1).unwrap().len(), 1);
        assert_eq!(status(&c, &a2), "done");
        assert!(mark_read(&c, &[a2.clone()], &a1).unwrap().is_empty(), "read once");
        let a4 = enqueue(&c, Some("A"), "turn", &json!({})).unwrap();
        deliver(&c, "A", &a1).unwrap();
        assert_eq!(release(&c, &a1).unwrap(), [a4.clone()]);
        assert_eq!((status(&c, &a4), status(&c, &carry_on)), ("queued".into(), "queued".into()));
    }

    #[test]
    fn complete_marks_read_before_release() {
        let (_d, db) = setup();
        let c = db.lock();
        let a1 = enqueue(&c, Some("A"), "turn", &json!({})).unwrap();
        claim(&c).unwrap();
        let (a2, a3) = (enqueue(&c, Some("A"), "turn", &json!({})).unwrap(), enqueue(&c, Some("A"), "turn", &json!({})).unwrap());
        deliver(&c, "A", &a1).unwrap();
        complete(&c, &a1, &json!({"outcome": "done", "read": [a2]})).unwrap();
        assert_eq!((status(&c, &a1), status(&c, &a2), status(&c, &a3)), ("done".into(), "done".into(), "queued".into()));
        let result: String = c.query_row("SELECT result FROM work_items WHERE id = ?", [&a2], |r| r.get(0)).unwrap();
        assert_eq!(serde_json::from_str::<serde_json::Value>(&result).unwrap()["read_by"], a1);
    }

    #[test]
    fn hold_scheduled_keeps_schedules_waiting() {
        let (_d, db) = setup();
        let c = db.lock();
        crate::db::set_setting(&c, HOLD_SCHEDULED, "1").unwrap();
        enqueue(&c, Some("S"), "turn", &json!({"source": "schedule"})).unwrap();
        let person = enqueue(&c, Some("P"), "turn", &json!({"source": "slack"})).unwrap();
        assert_eq!(claim(&c).unwrap().unwrap().id, person);
        assert!(claim(&c).unwrap().is_none());
        crate::db::set_setting(&c, HOLD_SCHEDULED, "0").unwrap();
        assert!(claim(&c).unwrap().is_some());
    }

    #[test]
    fn orphan_claim_requeued_at_start() {
        let (_d, db) = setup();
        let c = db.lock();
        let (orphan, running, tired) = (enqueue(&c, Some("A"), "turn", &json!({})).unwrap(),
                                        enqueue(&c, Some("B"), "turn", &json!({})).unwrap(),
                                        enqueue(&c, Some("C"), "turn", &json!({})).unwrap());
        for _ in 0..3 {
            claim(&c).unwrap();
        }
        c.execute("UPDATE work_items SET attempts = 4 WHERE id = ?", [&tired]).unwrap();
        c.execute("INSERT INTO agentd_turns(work_item_id, conversation_id, state, spec, started_at) \
                   VALUES (?, 'B', 'running', '{}', 'x')", [&running]).unwrap();
        assert_eq!(requeue_orphans(&c).unwrap(), 2);
        assert_eq!((status(&c, &orphan), status(&c, &running), status(&c, &tired)),
                   ("queued".into(), "claimed".into(), "failed".into()));
        assert_eq!(get(&c, &orphan).unwrap().unwrap().attempts, 1);
    }
}
