//! Expiries set by people (from the dashboard or by asking the agent) on what runs: a conversation's container, one
//! server in it (by port), one of the agent's services, or a stack in the agent's own Docker. A keep says when the thing
//! stops (`until`), or that it stays until someone changes it (`until` NULL). Without a keep, the usual rules apply:
//! a container stops an hour after it goes idle or 24 hours after its last turn; services and stacks have no expiry.

use anyhow::{Result, bail};
use chrono::{DateTime, NaiveDateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};

const TIME_FORMAT: &str = "%Y-%m-%dT%H:%M:%SZ";
pub const KINDS: [&str; 4] = ["container", "server", "service", "stack"];

#[derive(Clone, Debug, PartialEq)]
pub struct Keep {
    pub kind: String,
    pub target: String,
    /// None: kept until someone changes it.
    pub until: Option<DateTime<Utc>>,
    pub set_by: Option<String>,
    pub set_at: DateTime<Utc>,
}

impl serde::Serialize for Keep {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        serde_json::json!({"kind": self.kind, "target": self.target, "until": self.until.map(fmt), "set_by": self.set_by,
                           "set_at": fmt(self.set_at)}).serialize(s)
    }
}

impl Keep {
    /// Whether it still holds at `now`.
    pub fn holds(&self, now: DateTime<Utc>) -> bool {
        self.until.is_none_or(|t| now < t)
    }
}

pub fn fmt(t: DateTime<Utc>) -> String {
    t.format(TIME_FORMAT).to_string()
}

fn parse(s: &str) -> DateTime<Utc> {
    NaiveDateTime::parse_from_str(s, TIME_FORMAT).map(|t| t.and_utc()).unwrap_or(DateTime::UNIX_EPOCH)
}

fn from_row(r: &rusqlite::Row) -> rusqlite::Result<Keep> {
    Ok(Keep { kind: r.get(0)?, target: r.get(1)?, until: r.get::<_, Option<String>>(2)?.map(|t| parse(&t)),
              set_by: r.get(3)?, set_at: parse(&r.get::<_, String>(4)?) })
}

/// Keep `kind`/`target` until `until` (None: until changed), replacing any earlier keep.
pub fn set(c: &Connection, kind: &str, target: &str, until: Option<DateTime<Utc>>, by: &str, now: DateTime<Utc>) -> Result<()> {
    if !KINDS.contains(&kind) || target.is_empty() {
        bail!("can't keep {kind} {target:?}");
    }
    c.execute("INSERT OR REPLACE INTO agentd_keeps(kind, target, until, set_by, set_at) VALUES (?, ?, ?, ?, ?)",
              params![kind, target, until.map(fmt), by, fmt(now)])?;
    Ok(())
}

/// Back to the usual rules.
pub fn clear(c: &Connection, kind: &str, target: &str) -> Result<()> {
    c.execute("DELETE FROM agentd_keeps WHERE kind = ? AND target = ?", params![kind, target])?;
    Ok(())
}

pub fn get(c: &Connection, kind: &str, target: &str) -> Result<Option<Keep>> {
    Ok(c.query_row("SELECT kind, target, until, set_by, set_at FROM agentd_keeps WHERE kind = ? AND target = ?",
                   params![kind, target], from_row).optional()?)
}

pub fn all(c: &Connection) -> Result<Vec<Keep>> {
    Ok(c.prepare("SELECT kind, target, until, set_by, set_at FROM agentd_keeps ORDER BY kind, target")?
        .query_map([], from_row)?.collect::<rusqlite::Result<Vec<_>>>()?)
}

/// The kept servers of a conversation's container: (port, keep).
pub fn servers(c: &Connection, conversation: &str) -> Result<Vec<(u16, Keep)>> {
    Ok(all(c)?.into_iter().filter(|k| k.kind == "server").filter_map(|k| {
        let (convo, port) = k.target.rsplit_once(':')?;
        (convo == conversation).then(|| Some((port.parse().ok()?, k.clone()))).flatten()
    }).collect())
}

pub fn server_target(conversation: &str, port: u16) -> String {
    format!("{conversation}:{port}")
}

/// Whether a conversation's container must stay up at `now`: its own keep holds, or one of its kept servers' does.
pub fn holds_container(c: &Connection, conversation: &str, now: DateTime<Utc>) -> Result<bool> {
    Ok(get(c, "container", conversation)?.is_some_and(|k| k.holds(now))
        || servers(c, conversation)?.iter().any(|(_, k)| k.holds(now)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn db() -> (tempfile::TempDir, crate::db::Db) {
        let dir = tempfile::tempdir().unwrap();
        let db = crate::db::Db::open(&dir.path().join("agent.db")).unwrap();
        (dir, db)
    }

    #[test]
    fn a_keep_holds_until_its_time_or_for_good() {
        let (_dir, db) = db();
        let c = db.lock();
        let now = Utc::now();
        set(&c, "container", "V-1", Some(now + Duration::hours(2)), "sam", now).unwrap();
        assert!(holds_container(&c, "V-1", now).unwrap());
        assert!(!holds_container(&c, "V-1", now + Duration::hours(2)).unwrap(), "expired at its time");
        set(&c, "container", "V-1", None, "sam", now).unwrap();
        assert!(holds_container(&c, "V-1", now + Duration::days(400)).unwrap(), "kept until changed");
        clear(&c, "container", "V-1").unwrap();
        assert!(!holds_container(&c, "V-1", now).unwrap());
    }

    #[test]
    fn a_kept_server_holds_its_container() {
        let (_dir, db) = db();
        let c = db.lock();
        let now = Utc::now();
        set(&c, "server", &server_target("V-1", 18098), Some(now + Duration::hours(1)), "sam", now).unwrap();
        assert_eq!(servers(&c, "V-1").unwrap().iter().map(|(p, _)| *p).collect::<Vec<_>>(), vec![18098]);
        assert!(servers(&c, "V-10").unwrap().is_empty(), "another conversation's server");
        assert!(holds_container(&c, "V-1", now).unwrap());
        assert!(!holds_container(&c, "V-1", now + Duration::hours(1)).unwrap());
        assert!(set(&c, "nonsense", "x", None, "sam", now).is_err());
    }
}
