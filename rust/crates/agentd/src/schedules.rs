//! Schedules: work that starts on a timer (a port of core/schedules.py). The schedules file
//! (data/schedules/schedules.toml) holds one [name] table per schedule; the agent edits it when someone asks for work on
//! a schedule, and people can edit it by hand. Every minute agentd reads it and fires each due schedule as a turn.
//! `agentd schedules` checks it.

use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{LazyLock, Mutex};

use anyhow::{Result, anyhow};
use chrono::{DateTime, Timelike, Utc};
use chrono_tz::Tz;
use croner::Cron;
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::conversations::{self as cv, TurnFrom};
use crate::db::{get_setting, set_setting};
use crate::shared::Shared;

static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9-]{1,40}$").expect("valid"));
const REQUIRED: [&str; 4] = ["cron", "channel", "prompt", "by"];
const OPTIONAL: [&str; 2] = ["link", "paused"];

#[derive(Clone, Debug, PartialEq)]
pub struct Schedule {
    pub name: String,
    pub cron: String,
    /// A channel name or id, or a person key for their DM.
    pub channel: String,
    pub prompt: String,
    /// Who asked: a person key, or whatever was written.
    pub by: String,
    pub link: Option<String>,
    pub paused: bool,
}

fn cron(expr: &str) -> Result<Cron> {
    Cron::from_str(expr).map_err(|e| anyhow!("{e}"))
}

fn entry(name: &str, value: &toml::Value) -> Result<Schedule, String> {
    let Some(table) = value.as_table() else { return Err(format!("{name}: each schedule is a [name] table, not a top-level value")) };
    if !NAME.is_match(name) {
        return Err(format!("{name}: a name is lowercase letters, digits and dashes, at most 40 characters"));
    }
    let mut unknown: Vec<&str> = table.keys().map(String::as_str).filter(|k| !REQUIRED.contains(k) && !OPTIONAL.contains(k)).collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(format!("{name}: unknown keys {}", unknown.join(", ")));
    }
    for key in REQUIRED {
        if !table.get(key).and_then(toml::Value::as_str).is_some_and(|v| !v.trim().is_empty()) {
            return Err(format!("{name}: '{key}' is required and must be text"));
        }
    }
    if table.get("link").is_some_and(|v| !v.is_str()) {
        return Err(format!("{name}: 'link' must be text"));
    }
    if table.get("paused").is_some_and(|v| !v.is_bool()) {
        return Err(format!("{name}: 'paused' must be true or false"));
    }
    let text = |k: &str| table[k].as_str().unwrap_or_default().to_string();
    let expr = text("cron").trim().to_string();
    let Some(parsed) = (expr.split_whitespace().count() == 5).then(|| cron(&expr).ok()).flatten() else {
        return Err(format!("{name}: '{expr}' isn't a five-field cron expression"));
    };
    if parsed.find_next_occurrence(&Utc::now(), false).is_err() {
        return Err(format!("{name}: '{expr}' never runs (no date matches it)"));
    }
    Ok(Schedule { name: name.into(), cron: expr, channel: text("channel").trim().into(), prompt: text("prompt"), by: text("by").trim().into(),
                  link: table.get("link").and_then(toml::Value::as_str).map(str::to_string),
                  paused: table.get("paused").and_then(toml::Value::as_bool).unwrap_or(false) })
}

/// The valid schedules and one error per invalid entry; None when the text isn't TOML at all.
pub fn parse(text: &str) -> (Option<Vec<Schedule>>, Vec<String>) {
    let data: toml::Table = match toml::from_str(text) {
        Ok(d) => d,
        Err(e) => return (None, vec![format!("not valid TOML: {}", e.message())]),
    };
    let (mut valid, mut errors) = (Vec::new(), Vec::new());
    for (name, value) in &data {
        match entry(name, value) {
            Ok(s) => valid.push(s),
            Err(e) => errors.push(e),
        }
    }
    (Some(valid), errors)
}

/// The file's schedules; a missing file is none, one that can't be read is like invalid TOML.
pub fn load(path: &Path) -> (Option<Vec<Schedule>>, Vec<String>) {
    if !path.is_file() {
        return (Some(Vec::new()), Vec::new());
    }
    match std::fs::read(path).map_err(|e| e.to_string()).and_then(|b| String::from_utf8(b).map_err(|e| e.to_string())) {
        Ok(text) => parse(&text),
        Err(e) => (None, vec![format!("can't read the file: {e}")]),
    }
}

/// The latest time the schedule was due, at or before now (to the minute).
pub fn latest_slot(expr: &str, now: DateTime<Tz>) -> Result<DateTime<Tz>> {
    let minute = now.with_second(0).and_then(|t| t.with_nanosecond(0)).ok_or_else(|| anyhow!("bad time"))?;
    cron(expr)?.find_previous_occurrence(&minute, true).map_err(|e| anyhow!("{e}"))
}

pub fn next_slots(expr: &str, now: DateTime<Tz>, n: usize) -> Result<Vec<DateTime<Tz>>> {
    let parsed = cron(expr)?;
    let mut out: Vec<DateTime<Tz>> = Vec::new();
    for _ in 0..n {
        let from = out.last().cloned().unwrap_or(now);
        out.push(parsed.find_next_occurrence(&from, false).map_err(|e| anyhow!("{e}"))?);
    }
    Ok(out)
}

/// Fires each due schedule once per cron slot. One seen for the first time, or whose cron changed, waits for its next
/// slot; slots missed while agentd was down (or the host asleep) collapse into one firing; slots due while it is
/// paused or the agent is stopped are used up without firing; one that leaves the file and comes back counts as new. A
/// broken file keeps the last valid schedules. Errors are reported to the admins once per distinct text.
pub struct Scheduler {
    path: PathBuf,
    valid: Mutex<Vec<Schedule>>,
}

impl Scheduler {
    pub fn new(path: PathBuf) -> Scheduler {
        Scheduler { path, valid: Mutex::default() }
    }

    pub async fn tick(&self, s: &Shared, now: DateTime<Utc>) -> Result<Vec<String>> {
        let tz: Tz = s.config.timezone.parse().unwrap_or(chrono_tz::UTC);
        let now = now.with_timezone(&tz);
        let (schedules, mut errors) = load(&self.path);
        if let Some(found) = schedules {
            let names: Vec<String> = found.iter().map(|s| s.name.clone()).collect();
            *self.valid.lock().unwrap_or_else(|e| e.into_inner()) = found;
            forget_all_but(s, &names)?;
        }
        let stopped = get_setting(&s.db.lock(), crate::queue::PAUSED)?.as_deref() == Some("1");
        let valid = self.valid.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let mut fired = Vec::new();
        for sched in valid {
            let slot = match latest_slot(&sched.cron, now) {
                Ok(slot) => slot,
                Err(e) => {
                    errors.push(format!("{}: {e}", sched.name));
                    continue;
                }
            };
            let key = format!("schedule.{}.last", sched.name);
            let last: Option<Value> = get_setting(&s.db.lock(), &key)?.and_then(|v| serde_json::from_str(&v).ok());
            let record = |slot: &DateTime<Tz>| set_setting(&s.db.lock(), &key, &json!({"slot": slot.to_rfc3339(), "cron": sched.cron}).to_string());
            let Some(last) = last.filter(|l| l["cron"] == sched.cron.as_str()) else {
                record(&slot)?;
                continue;
            };
            let last_slot = last["slot"].as_str().and_then(|t| DateTime::parse_from_rfc3339(t).ok());
            if last_slot.is_some_and(|l| slot <= l) {
                continue;
            }
            if !(sched.paused || stopped) {
                if let Err(e) = start(s, &sched).await {
                    tracing::error!("schedule {} couldn't start: {e:#}", sched.name);
                    errors.push(format!("{}: couldn't start: {e:#}", sched.name));
                    continue;
                }
                fired.push(sched.name.clone());
            }
            record(&slot)?;
        }
        report(s, &errors).await?;
        Ok(fired)
    }
}

/// A schedule that left the file (removed, renamed or invalid) counts as new if it comes back.
fn forget_all_but(s: &Shared, names: &[String]) -> Result<()> {
    let c = s.db.lock();
    let keys: Vec<String> = c.prepare("SELECT key FROM settings WHERE key LIKE 'schedule.%.last'")?.query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for key in keys {
        let name = &key["schedule.".len()..key.len() - ".last".len()];
        if !names.iter().any(|n| n == name) {
            c.execute("DELETE FROM settings WHERE key = ?", [&key])?;
        }
    }
    Ok(())
}

async fn report(s: &Shared, errors: &[String]) -> Result<()> {
    let text: String = errors.iter().map(|e| format!("- {e}")).collect::<Vec<_>>().join("\n");
    let digest = if errors.is_empty() { String::new() } else { Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect() };
    if get_setting(&s.db.lock(), "schedules.errors_hash")?.unwrap_or_default() == digest {
        return Ok(());
    }
    if !errors.is_empty() {
        tracing::warn!("schedules: {}", errors.join("; "));
        s.dm_admins(&format!("Problems in the schedules file (data/schedules/schedules.toml):\n{text}")).await;
    }
    set_setting(&s.db.lock(), "schedules.errors_hash", &digest)
}

/// A due schedule: an unattended turn with its prompt in a new conversation. Nothing is posted; a run that fails says
/// so in the schedule's channel (a person key means their DM).
pub async fn start(s: &Shared, sched: &Schedule) -> Result<String> {
    let project = if s.config.people.contains_key(&sched.channel) { None } else { cv::project_for_channel(s, &sched.channel).await };
    let by = s.config.people.get(&sched.by).cloned();
    let who = match &by {
        Some(person) => cv::who(s, &person.key).await,
        None => sched.by.clone(),
    };
    let convo = {
        let c = s.db.lock();
        let id = cv::create_conversation(&c, None, None, project.as_deref(), None, true, None)?;
        cv::row(&c, &id)?.ok_or_else(|| anyhow!("conversation vanished"))?
    };
    let origin = cv::origin_line(&format!("schedule {} · {} · set up by {who}", sched.name, sched.cron), None, sched.link.as_deref());
    let key = by.map(|p| p.key);
    let queued = cv::enqueue_turn(s, &convo, &sched.prompt, key.as_deref(), TurnFrom {
        unattended: true, source: "schedule".into(), requester: Some(key.clone().unwrap_or_else(|| sched.by.clone())), origin,
        report_to: Some(json!({"channel": sched.channel, "schedule": sched.name})), ..Default::default() });
    if queued.is_err() {
        // the scheduler reports it and fires again next minute, with a new conversation
        s.db.lock().execute("DELETE FROM conversations WHERE id = ?", [&convo.id])?;
    }
    queued
}

/// `agentd schedules`: each schedule's next three runs, and each problem; exit code 1 when there are problems.
pub fn check(path: &Path, tz: Tz, now: DateTime<Tz>) -> (String, i32) {
    if !path.is_file() {
        return (format!("no schedules file at {}\n", path.display()), 0);
    }
    let (valid, errors) = load(path);
    let mut out = String::new();
    for sched in valid.unwrap_or_default() {
        let times: Vec<String> = next_slots(&sched.cron, now, 3).unwrap_or_default().iter().map(|t| t.format("%Y-%m-%d %H:%M").to_string()).collect();
        out += &format!("{}  {}  -> {}{}\n  next: {} ({})\n", sched.name, sched.cron, sched.channel,
                        if sched.paused { "  (paused)" } else { "" }, times.join(", "), tz.name());
    }
    for e in &errors {
        out += &format!("error: {e}\n");
    }
    (out, if errors.is_empty() { 0 } else { 1 })
}
