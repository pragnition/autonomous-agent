//! Parking: a conversation waits out the account pool's soft caps (or until a time someone names) and carries itself
//! on (a port of core/parking.py). The agent calls `park` and ends its turn; every minute agentd carries on each park
//! that is due as a new turn with the agent's note. A person's message in the thread ends the park at once. Without a
//! time, a park waits out the whole pool: it refuses while any account is under its cap.
//!
//! The status tool's account lines come from here too.

use std::sync::LazyLock;

use anyhow::Result;
use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use chrono_tz::Tz;
use regex::Regex;
use rusqlite::params;
use serde_json::{Value, json};

use crate::db::append_event;
use crate::proxy::Usage;
use crate::shared::Shared;
use crate::tools::ToolContext;

/// A park further out than this is a mistake, not a plan.
const MAX_PARK_S: f64 = 7.0 * 24.0 * 3600.0;
const FMT: &str = "%Y-%m-%dT%H:%M:%SZ";
static CLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\d{1,2}):(\d{2})$").expect("valid"));

fn iso(epoch: f64) -> String {
    DateTime::from_timestamp(epoch as i64, 0).unwrap_or_default().format(FMT).to_string()
}

fn epoch(iso: &str) -> f64 {
    NaiveDateTime::parse_from_str(iso, FMT).map(|t| t.and_utc().timestamp() as f64).unwrap_or(0.0)
}

/// `1h05m`, `42m`.
pub fn span(seconds: f64) -> String {
    let minutes = ((seconds / 60.0).floor() as i64).max(0);
    if minutes >= 60 { format!("{}h{:02}m", minutes / 60, minutes % 60) } else { format!("{minutes}m") }
}

fn tz(s: &Shared) -> Tz {
    s.config.timezone.parse().unwrap_or(chrono_tz::UTC)
}

/// A time as people in the team read it: `14:30 +08` today, `Fri 14:30 +08` on another day.
pub fn when(at: f64, now: f64, tz: Tz) -> String {
    let (at, now) = (tz.timestamp_opt(at as i64, 0).unwrap(), tz.timestamp_opt(now as i64, 0).unwrap());
    at.format(if at.date_naive() == now.date_naive() { "%H:%M %Z" } else { "%a %H:%M %Z" }).to_string()
}

/// `text` as an epoch: HH:MM is the next such time in `tz`; an ISO date-time without an offset is in `tz` too.
pub fn parse_until(text: &str, now: f64, tz: Tz) -> Option<f64> {
    let text = text.trim();
    if let Some(m) = CLOCK.captures(text) {
        let (hour, minute): (u32, u32) = (m[1].parse().ok()?, m[2].parse().ok()?);
        if hour > 23 || minute > 59 {
            return None;
        }
        let local_now = tz.timestamp_opt(now as i64, 0).single()?;
        let mut day = local_now.date_naive();
        loop {
            let at = tz.from_local_datetime(&day.and_hms_opt(hour, minute, 0)?).earliest()?;
            if at.timestamp() as f64 > now {
                return Some(at.timestamp() as f64);
            }
            day = day.succ_opt()?;
        }
    }
    if let Ok(at) = DateTime::parse_from_rfc3339(text) {
        return Some(at.timestamp() as f64 + f64::from(at.timestamp_subsec_millis()) / 1000.0);
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z", "%Y-%m-%d %H:%M:%S%:z", "%Y-%m-%d %H:%M%:z"] {
        if let Ok(at) = DateTime::parse_from_str(text, format) {
            return Some(at.timestamp() as f64);
        }
    }
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d %H:%M"].iter()
        .find_map(|f| NaiveDateTime::parse_from_str(text, f).ok())
        .or_else(|| chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0)))?;
    Some(tz.from_local_datetime(&naive).earliest()?.timestamp() as f64)
}

/// The accounts the proxy would give a session (it ranks them) that are under their cap.
fn under_cap(usage: &Usage) -> Vec<String> {
    usage.accounts.iter().filter(|(c, a)| a.rank.is_some() && !usage.over_cap(Some(c))).map(|(c, _)| c.clone()).collect()
}

/// When the first account has room again, with none under its cap now: the earliest soft-cap lift of an account the
/// proxy still ranks, the reset of one with no capacity, or the weekly reset of one past its weekly cap. A disabled
/// account doesn't come back at a reset.
fn room_at(usage: &Usage, now: f64) -> Option<f64> {
    usage.accounts.values().filter(|a| a.status != "disabled").filter_map(|a| {
        if a.over_weekly_cap() {
            return a.reset_7d.filter(|r| *r > now);
        }
        let reset = a.reset_5h.filter(|r| *r > now)?;
        let lift = reset - usage.soft_cap.lift_s;
        Some(if a.rank.is_some() && lift > now { lift } else { reset })
    }).min_by(f64::total_cmp)
}

fn now_epoch() -> f64 {
    Utc::now().timestamp_millis() as f64 / 1000.0
}

/// The status tool's account lines: how much of its 5-hour window the conversation's account has used, its cap, when
/// the window resets and the cap lifts; with a weekly cap, the same for its week.
pub async fn status_lines(s: &Shared, ctx: &ToolContext) -> Vec<String> {
    let code: Option<String> = ctx.conversation_id.as_deref().and_then(|id| s.db.lock()
        .query_row("SELECT session_account FROM conversations WHERE id = ?", [id], |r| r.get(0)).ok().flatten());
    let Ok(usage) = s.proxy.usage().await else { return vec!["account: usage not known (the proxy isn't answering)".into()] };
    let (now, tz) = (now_epoch(), tz(s));
    let room = under_cap(&usage).len();
    let mut lines = Vec::new();
    match code.as_deref().and_then(|c| usage.accounts.get(c).map(|a| (c, a))) {
        None => lines.push("account: not known yet for this session".into()),
        Some((code, account)) => {
            let cap = usage.soft_cap.cap(account.reset_5h, now);
            let used = account.used_5h.map(|u| format!("{:.0}%", u * 100.0)).unwrap_or_else(|| "unknown".into());
            let mut parts = vec![format!("account: {code}"), format!("5h window {used} used of its {:.0}% cap", cap * 100.0)];
            if let Some(reset) = account.reset_5h {
                parts.push(format!("resets {} (in {})", when(reset, now, tz), span(reset - now)));
                let lift = reset - usage.soft_cap.lift_s;
                parts.push(if lift > now { format!("cap lifts to 100% at {} (in {})", when(lift, now, tz), span(lift - now)) }
                           else { "cap lifted".into() });
            }
            if let Some(cap_7d) = account.cap_7d {
                let used = account.used_7d.map(|u| format!("{:.0}%", u * 100.0)).unwrap_or_else(|| "unknown".into());
                parts.push(format!("7-day window {used} used of its {:.0}% cap", cap_7d * 100.0));
                if let Some(reset) = account.reset_7d {
                    parts.push(format!("week resets {} (in {})", when(reset, now, tz), span(reset - now)));
                }
            }
            if usage.over_cap(Some(code)) {
                let state = if account.over_weekly_cap() { "over its weekly cap" }
                            else if account.rank.is_some() { "over its cap" } else { "no capacity" };
                parts.push(format!("{state}{}", if room > 0 { "" } else { ", like every account: park until one lifts" }));
            }
            lines.push(parts.join(" · "));
        }
    }
    lines.push(format!("accounts under their cap: {room} of {}", usage.accounts.len()));
    lines
}

pub async fn park(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let Some(convo) = &ctx.conversation_id else { return Ok("this turn has no conversation to park".into()) };
    let (now, tz) = (now_epoch(), tz(s));
    let raw = args["until"].as_str().unwrap_or_default().trim();
    let until = if !raw.is_empty() {
        match parse_until(raw, now, tz) {
            Some(u) => u,
            None => return Ok(format!("can't read until '{raw}': give HH:MM or an ISO date-time")),
        }
    } else {
        // the turn that carries the park on runs on whichever account the proxy gives it: wait out the pool
        let Ok(usage) = s.proxy.usage().await else {
            return Ok("the account pool's usage isn't known (the proxy isn't answering). Give until to park anyway.".into());
        };
        let room = under_cap(&usage);
        if !room.is_empty() {
            return Ok(format!("No account needs waiting out: {} {} under its cap, so a turn can run now. Give until to park \
                               until a time anyway.", room.join(", "), if room.len() == 1 { "is" } else { "are" }));
        }
        match room_at(&usage, now) {
            Some(u) => u,
            None => return Ok("every account is over its cap, but none says when it has room again. Give until to park anyway.".into()),
        }
    };
    if until <= now {
        return Ok(format!("{} has passed; give a time in the future", when(until, now, tz)));
    }
    if until - now > MAX_PARK_S {
        return Ok("that's more than 7 days away; park for less".into());
    }
    let (note, stamp) = (args["note"].as_str().unwrap_or_default().trim(), iso(now));
    let c = s.db.lock();
    c.execute("UPDATE parks SET ended_at = ?, ended_how = 'replaced' WHERE conversation_id = ? AND ended_at IS NULL", params![stamp, convo])?;
    c.execute("INSERT INTO parks(conversation_id, work_item_id, until_at, note, created_at) VALUES (?, ?, ?, ?, ?)",
              params![convo, ctx.work_item_id, iso(until), note, stamp])?;
    append_event(&c, "parked", ctx.task_id.as_deref(), ctx.actor.as_deref(),
                 Some(&json!({"conversation_id": convo, "work_item": ctx.work_item_id, "until": iso(until)})))?;
    let at = when(until, now, tz);
    // nothing is posted: like a teammate, the agent tells the thread itself if it wants to
    Ok(format!("Parked until {at} (in {}). End this turn now; at {at} core carries this conversation on in a new turn with \
                your note. A reply in the thread resumes it earlier.", span(until - now)))
}

/// Every minute: carry on each park that is due, once.
pub async fn tick(s: &Shared, now: DateTime<Utc>) -> Result<()> {
    let (stamp, now_s, tz) = (now.format(FMT).to_string(), now.timestamp() as f64, tz(s));
    let due: Vec<(i64, String, String, String, String, Option<String>)> = s.db.lock().prepare(
        "SELECT id, conversation_id, work_item_id, created_at, until_at, note FROM parks WHERE ended_at IS NULL AND until_at <= ? \
         ORDER BY until_at")?.query_map([&stamp], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (id, convo, item, created, until, note) in due {
        let note = note.filter(|n| !n.is_empty());
        let text = format!("(You parked this conversation at {} until {}. It's time: carry on from where you stopped{}",
                           when(epoch(&created), now_s, tz), when(epoch(&until), now_s, tz),
                           note.map(|n| format!(". Your note: {n})")).unwrap_or_else(|| ".)".into()));
        let new_id = match crate::conversations::resume_parked(s, &convo, &item, &text) {
            Ok(n) => n,
            Err(e) => {
                tracing::warn!("couldn't carry on park {id} of {convo}: {e:#}"); // stays open for the next tick
                continue;
            }
        };
        let c = s.db.lock();
        c.execute("UPDATE parks SET ended_at = ?, ended_how = 'resumed', resumed_as = ? WHERE id = ?", params![stamp, new_id, id])?;
        append_event(&c, "park_resumed", None, Some("parking"), Some(&json!({"conversation_id": convo, "resumed_as": new_id})))?;
    }
    Ok(())
}
