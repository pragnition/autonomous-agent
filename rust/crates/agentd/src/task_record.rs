//! The task record (a port of core/task_record.py): what the agent worked on, who asked, when, for how long, and
//! how it went.
//!
//! The labeler (labeler.rs) gives each request the agent answered a label: chat, a new task, or the same task as an
//! earlier one. A task is its `new` request plus every `same` request after it; its row in task_records is rebuilt
//! from those requests whenever one is labeled, and the agent reads a copy of the records, one file per month, in
//! data/tasks. Everything here comes from what agentd recorded itself.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::LazyLock;

use anyhow::Result;
use chrono::{DateTime, Duration, TimeZone, Utc};
use chrono_tz::Tz;
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Value, json};

use crate::config::{Config, Pack};
use crate::db::utcnow;
use crate::records::{py_dumps, truthy};

pub const NO_TEXT: &str = "(no text)";
const LINEAR_EVENT: &str = "Linear agent session event:";
const ASKED_CHARS: usize = 2000;
const REPLY_CHARS: usize = 3000;
const STEP_CHARS: usize = 300;
const EARLIER_MAX: i64 = 5;
const RECENT_MAX: usize = 10;
const RECENT_HOURS: i64 = 24;
/// The labeler's version. Raising it relabels the whole history, oldest first, the next time the sweep runs.
pub const LABEL_VERSION: i64 = 2;
const FAILED_OUTCOMES: [&str; 4] = ["error", "timeout", "stalled", "interrupted"];
const FMT: &str = "%Y-%m-%dT%H:%M:%SZ";

static URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https?://[^\s\]|>]+").unwrap());
static LINKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|https://linear\.app/[\w.-]+/issue/[\w-]+(?:/[\w-]+)?").unwrap());

/// A finished work item, as the record reads it.
#[derive(Clone, Debug)]
pub struct Item {
    pub id: String,
    pub conversation_id: Option<String>,
    pub payload: Value,
    pub status: String,
    pub result: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

fn item_of(r: &Row) -> rusqlite::Result<Item> {
    let payload: String = r.get("payload")?;
    Ok(Item { id: r.get("id")?, conversation_id: r.get("conversation_id")?,
              payload: serde_json::from_str(&payload).unwrap_or(Value::Null), status: r.get("status")?,
              result: r.get("result")?, created_at: r.get("created_at")?, updated_at: r.get("updated_at")? })
}

pub fn item(c: &Connection, id: &str) -> Result<Option<Item>> {
    Ok(c.query_row("SELECT * FROM work_items WHERE id = ?", [id], item_of).optional()?)
}

#[derive(Clone, Debug, PartialEq)]
pub struct Label {
    /// chat | new | same
    pub label: String,
    /// For same: the task it continues.
    pub task: Option<String>,
    /// For new.
    pub title: Option<String>,
    pub outcome: Option<String>,
    pub finished: Option<bool>,
    /// A configured project's name; None for general work.
    pub project: Option<String>,
}

impl Label {
    pub fn chat() -> Label {
        Label { label: "chat".into(), task: None, title: None, outcome: None, finished: None, project: None }
    }
}

pub struct RequestContext {
    pub asked: String,
    pub reply: String,
    pub errored: bool,
    /// This conversation's earlier labeled requests, oldest first: {task, label, title, asked, project}.
    pub earlier: Vec<Value>,
    /// The requester's other tasks from the 24 hours before this request: {task, title, outcome, project}.
    pub recent: Vec<Value>,
}

fn cut(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

fn at(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value).ok().map(|d| d.with_timezone(&Utc))
}

/// Finished turns with no label yet, or one from an older labeler, oldest first. Smoke-test conversations are never
/// labeled, nor a message read inside another turn (its result carries `read_by`).
pub fn pending(c: &Connection) -> Result<Vec<Item>> {
    let mut stmt = c.prepare(
        "SELECT w.* FROM work_items w LEFT JOIN request_labels l ON l.work_item_id = w.id \
         WHERE w.kind = 'turn' AND w.status IN ('done', 'failed', 'cancelled') \
         AND COALESCE(w.conversation_id, '') NOT LIKE 'V-smoke%' AND (l.label IS NULL OR l.version < ?) \
         AND json_extract(w.result, '$.read_by') IS NULL ORDER BY w.created_at, w.rowid")?;
    let rows = stmt.query_map([LABEL_VERSION], item_of)?.collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

fn webhook(text: &str) -> Option<Value> {
    serde_json::from_str::<Value>(text.strip_prefix(LINEAR_EVENT)?).ok().filter(Value::is_object)
}

fn text_of(v: &Value) -> Option<String> {
    v.as_str().map(str::to_string)
}

/// (source, requester, follows). Turns queued since the task record shipped carry them; older ones are inferred
/// from what they do carry.
pub fn origin_of(c: &Connection, config: &Config, row: &Item) -> Result<(String, Option<String>, Option<String>)> {
    let p = &row.payload;
    if truthy(p.get("source")) {
        return Ok((p["source"].as_str().unwrap_or_default().into(), text_of(&p["requester"]), text_of(&p["follows"])));
    }
    let body = webhook(p["text"].as_str().unwrap_or_default());
    let linked = c.query_row("SELECT 1 FROM linear_issue_conversations WHERE conversation_id = ?",
                             [&row.conversation_id], |_| Ok(())).optional()?.is_some();
    if body.is_some() || linked {
        let requester = match &body {
            Some(b) if truthy(Some(b)) => crate::linear::requester(b, config),
            _ => text_of(&p["actor"]),
        };
        return Ok(("linear".into(), requester, None));
    }
    if p["origin"].as_str().unwrap_or_default().starts_with("[schedule") {
        return Ok(("schedule".into(), text_of(&p["actor"]), None));
    }
    let mut follows = None;
    if truthy(p.get("continuation")) {
        let rowid: i64 = c.query_row("SELECT rowid FROM work_items WHERE id = ?", [&row.id], |r| r.get(0))?;
        follows = c.query_row(
            "SELECT id FROM work_items WHERE conversation_id = ? AND kind = 'turn' AND (created_at < ? OR \
             (created_at = ? AND rowid < ?)) ORDER BY created_at DESC, rowid DESC LIMIT 1",
            params![row.conversation_id, row.created_at, row.created_at, rowid], |r| r.get(0)).optional()?;
    }
    Ok(("slack".into(), text_of(&p["actor"]), follows))
}

/// What was asked, in words: a Linear webhook becomes its issue and the prompt.
pub fn asked_text(payload: &Value) -> String {
    let mut text = payload["text"].as_str().unwrap_or_default().to_string();
    if let Some(body) = webhook(&text) {
        let session = &body["agentSession"];
        let issue = &session["issue"];
        let prompt = [&body["agentActivity"]["content"]["body"], &session["comment"]["body"], &body["promptContext"]]
            .into_iter().find(|v| truthy(Some(v))).and_then(|v| v.as_str()).unwrap_or_default();
        let head: Vec<&str> = [&issue["identifier"], &issue["title"]].into_iter()
            .filter_map(|v| v.as_str().filter(|s| !s.is_empty())).collect();
        text = if head.is_empty() { prompt.to_string() } else { format!("{}: {prompt}", head.join(" ")) };
    }
    let text = text.trim();
    if text.is_empty() { NO_TEXT.into() } else { text.into() }
}

fn result_of(row: &Item) -> Value {
    row.result.as_deref().and_then(|r| serde_json::from_str::<Value>(r).ok()).filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn bounded(text: &str, needle: &str) -> bool {
    // `needle` as a whole token: no word character, dot or dash right before or after it (Python's lookarounds)
    let (hay, needle) = (text.to_lowercase(), needle.to_lowercase());
    let edge = |c: Option<char>| c.is_none_or(|c| !(c.is_alphanumeric() || c == '_' || c == '.' || c == '-'));
    hay.match_indices(&needle).any(|(i, m)| edge(hay[..i].chars().next_back()) && edge(hay[i + m.len()..].chars().next()))
}

/// Projects agentd can tell a request is about, strongest first: the conversation's bound project, then a Linear
/// key of the project's team or its repo in the request or the reply, or its name in the request. The name in the
/// reply doesn't count: the agent mentions its main project in most of what it says.
pub fn project_hints(c: &Connection, row: &Item, packs: &BTreeMap<String, Pack>) -> Result<Vec<String>> {
    let mut hints: Vec<String> = Vec::new();
    let bound: Option<Option<String>> = c.query_row("SELECT project FROM conversations WHERE id = ?",
                                                    [&row.conversation_id], |r| r.get(0)).optional()?;
    if let Some(Some(p)) = bound && packs.contains_key(&p) {
        hints.push(p);
    }
    let asked = asked_text(&row.payload);
    let text = format!("{asked}\n{}", result_of(row)["text"].as_str().unwrap_or_default());
    for (name, pack) in packs {
        let (team, repo) = (&pack.project.linear_team, &pack.project.repo);
        let word = |pattern: String, hay: &str| Regex::new(&format!("(?i){pattern}")).is_ok_and(|re| re.is_match(hay));
        if (!team.is_empty() && word(format!(r"\b{}-\d+\b", regex::escape(team)), &text))
            || (!repo.is_empty() && bounded(&text, repo))
            || word(format!(r"\b{}\b", regex::escape(name)), &asked) {
            hints.push(name.clone());
        }
    }
    let mut seen = BTreeSet::new();
    hints.retain(|h| seen.insert(h.clone()));
    Ok(hints)
}

pub fn context_for(c: &Connection, row: &Item, requester: Option<&str>) -> Result<RequestContext> {
    let result = result_of(row);
    let errored = row.status != "done" || result["outcome"].as_str().is_some_and(|o| FAILED_OUTCOMES.contains(&o));
    let mut earlier: Vec<Value> = c.prepare(
        "SELECT l.task_id, l.label, (SELECT n.title FROM request_labels n WHERE n.task_id = l.task_id AND n.label = 'new'), \
         l.asked, l.project FROM request_labels l WHERE l.conversation_id = ? AND l.label IS NOT NULL AND l.arrived_at <= ? \
         AND l.work_item_id != ? ORDER BY l.arrived_at DESC, l.work_item_id DESC LIMIT ?")?
        .query_map(params![row.conversation_id, row.created_at, row.id, EARLIER_MAX], |r| {
            let asked: String = r.get(3)?;
            Ok(json!({"task": r.get::<_, Option<String>>(0)?, "label": r.get::<_, Option<String>>(1)?,
                      "title": r.get::<_, Option<String>>(2)?, "asked": cut(&asked, STEP_CHARS),
                      "project": r.get::<_, Option<String>>(4)?}))
        })?.collect::<rusqlite::Result<_>>()?;
    earlier.reverse();
    let mut recent = Vec::new();
    if let Some(requester) = requester.filter(|r| !r.is_empty()) {
        let since = at(&row.created_at).map(|t| (t - Duration::hours(RECENT_HOURS)).format(FMT).to_string())
            .unwrap_or_default();
        let seen: BTreeSet<&str> = earlier.iter().filter_map(|e| e["task"].as_str()).collect();
        let tasks: Vec<String> = c.prepare(
            "SELECT task_id, MAX(arrived_at) AS last FROM request_labels WHERE requester = ? AND task_id IS NOT NULL \
             AND arrived_at >= ? AND arrived_at <= ? AND work_item_id != ? GROUP BY task_id ORDER BY last DESC")?
            .query_map(params![requester, since, row.created_at, row.id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        for task in tasks {
            if seen.contains(task.as_str()) {
                continue;
            }
            if let Some(rec) = task_record(c, &task)? {
                recent.push(json!({"task": rec["id"], "title": rec["title"], "outcome": rec["outcome"],
                                   "project": rec.get("project").cloned().unwrap_or(Value::Null)}));
            }
            if recent.len() >= RECENT_MAX {
                break;
            }
        }
    }
    let reply = result["text"].as_str().unwrap_or_default().trim().to_string();
    Ok(RequestContext { asked: cut(&asked_text(&row.payload), ASKED_CHARS),
                        reply: cut(if reply.is_empty() { NO_TEXT } else { &reply }, REPLY_CHARS),
                        errored, earlier, recent })
}

fn new_task_id(c: &Connection) -> Result<String> {
    let top: Option<i64> = c.query_row("SELECT MAX(CAST(SUBSTR(task_id, 4) AS INTEGER)) FROM request_labels \
                                        WHERE task_id LIKE 'TK-%'", [], |r| r.get(0))?;
    Ok(format!("TK-{:04}", top.unwrap_or(0) + 1))
}

type Field = (&'static str, rusqlite::types::Value);

fn text(v: Option<&str>) -> rusqlite::types::Value {
    v.map_or(rusqlite::types::Value::Null, |s| rusqlite::types::Value::Text(s.into()))
}

fn save(c: &Connection, row: &Item, source: &str, requester: Option<&str>, follows: Option<&str>, fields: Vec<Field>)
        -> Result<()> {
    let origin = row.payload["origin"].as_str().unwrap_or_default();
    let link = URL.find_iter(origin).last().map(|m| m.as_str().to_string());
    let started: Option<String> = c.query_row(
        "SELECT MAX(ts) FROM events WHERE type = 'session_started' AND json_extract(data, '$.work_item') = ?",
        [&row.id], |r| r.get(0))?;
    let mut base: Vec<Field> = vec![
        ("conversation_id", text(row.conversation_id.as_deref())), ("source", text(Some(source))),
        ("requester", text(requester)), ("follows", text(follows)),
        ("asked", text(Some(&cut(&asked_text(&row.payload), STEP_CHARS)))), ("link", text(link.as_deref())),
        ("arrived_at", text(Some(&row.created_at))), ("started_at", text(started.as_deref())),
        ("answered_at", text(Some(&row.updated_at))),
    ];
    base.extend(fields);
    let cols: Vec<&str> = base.iter().map(|(k, _)| *k).collect();
    let sql = format!("INSERT INTO request_labels(work_item_id, {}) VALUES (?, {}) ON CONFLICT(work_item_id) DO UPDATE SET {}",
                      cols.join(", "), vec!["?"; cols.len()].join(", "),
                      cols.iter().map(|k| format!("{k} = excluded.{k}")).collect::<Vec<_>>().join(", "));
    let values: Vec<rusqlite::types::Value> =
        std::iter::once(text(Some(&row.id))).chain(base.into_iter().map(|(_, v)| v)).collect();
    c.execute(&sql, rusqlite::params_from_iter(values))?;
    Ok(())
}

/// One more unparsable answer for this request; how many it has had.
pub fn count_try(c: &Connection, row: &Item, source: &str, requester: Option<&str>, follows: Option<&str>) -> Result<i64> {
    let tries: Option<i64> = c.query_row("SELECT tries FROM request_labels WHERE work_item_id = ?", [&row.id],
                                         |r| r.get(0)).optional()?;
    let tries = tries.unwrap_or(0) + 1;
    save(c, row, source, requester, follows, vec![("tries", tries.into())])?;
    Ok(tries)
}

/// Save the request's label and rebuild its task (and the task it was in before, when relabeled); the task's id, or
/// None for chat. A request relabeled `new` keeps the task id it had.
pub fn apply_label(c: &Connection, row: &Item, label: &Label, source: &str, requester: Option<&str>,
                   follows: Option<&str>) -> Result<Option<String>> {
    let before: Option<(Option<String>, Option<String>)> = c.query_row(
        "SELECT label, task_id FROM request_labels WHERE work_item_id = ?", [&row.id], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()?;
    let task_id = match label.label.as_str() {
        "new" => match &before {
            Some((Some(l), Some(t))) if l == "new" => Some(t.clone()),
            _ => Some(new_task_id(c)?),
        },
        "same" => label.task.clone(),
        _ => None,
    };
    let is_task = task_id.is_some();
    let keep = |v: &Option<String>| text(v.as_deref().filter(|_| is_task));
    save(c, row, source, requester, follows, vec![
        ("label", text(Some(&label.label))), ("task_id", text(task_id.as_deref())),
        ("title", text(label.title.as_deref().filter(|_| label.label == "new"))),
        ("outcome", keep(&label.outcome)),
        ("finished", label.finished.filter(|_| is_task).map_or(rusqlite::types::Value::Null, |f| (f as i64).into())),
        ("project", keep(&label.project)), ("labeled_at", text(Some(&utcnow()))), ("version", LABEL_VERSION.into()),
        ("tries", 0i64.into()),
    ])?;
    if let Some(t) = &task_id {
        rebuild_task(c, t)?;
    }
    if let Some((_, Some(old))) = &before && Some(old) != task_id.as_ref() {
        rebuild_task(c, old)?;
    }
    Ok(task_id)
}

/// The seconds covered by the union of [start, end) intervals: overlapping requests count once.
pub fn work_seconds(intervals: &[(Option<String>, Option<String>)]) -> i64 {
    let mut spans: Vec<(DateTime<Utc>, DateTime<Utc>)> = intervals.iter()
        .filter_map(|(a, b)| match (a.as_deref(), b.as_deref()) {
            (Some(a), Some(b)) if !a.is_empty() && b > a => Some((at(a)?, at(b)?)),
            _ => None,
        }).collect();
    spans.sort();
    let (mut total, mut cur): (f64, Option<(DateTime<Utc>, DateTime<Utc>)>) = (0.0, None);
    for (start, end) in spans {
        match cur {
            Some((s, e)) if start <= e => cur = Some((s, e.max(end))),
            _ => {
                if let Some((s, e)) = cur {
                    total += (e - s).num_milliseconds() as f64 / 1000.0;
                }
                cur = Some((start, end));
            }
        }
    }
    if let Some((s, e)) = cur {
        total += (e - s).num_milliseconds() as f64 / 1000.0;
    }
    total as i64
}

fn distinct(values: impl IntoIterator<Item = Option<String>>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    values.into_iter().flatten().filter(|v| !v.is_empty() && seen.insert(v.clone())).collect()
}

pub fn rebuild_task(c: &Connection, task_id: &str) -> Result<()> {
    let reqs: Vec<Value> = c.prepare(
        "SELECT l.*, w.result FROM request_labels l LEFT JOIN work_items w ON w.id = l.work_item_id \
         WHERE l.task_id = ? AND l.label IN ('new', 'same') ORDER BY l.arrived_at, l.work_item_id")?
        .query_map([task_id], crate::db::row_json)?.collect::<rusqlite::Result<_>>()?;
    let Some(first) = reqs.iter().find(|r| r["label"] == "new") else {
        // Its first request was relabeled away: the task is gone. Follow-ups labeled by this labeler are labeled
        // again (an older labeler's are pending anyway), so none of them is left in a task that doesn't exist.
        c.execute("DELETE FROM task_records WHERE id = ?", [task_id])?;
        c.execute("UPDATE request_labels SET label = NULL, task_id = NULL, version = 0 WHERE task_id = ? AND version >= ?",
                  params![task_id, LABEL_VERSION])?;
        return Ok(());
    };
    let s = |r: &Value, k: &str| r[k].as_str().map(str::to_string);
    // where the task stands: the latest request with a verdict (one labeled without one says nothing new)
    let latest = reqs.iter().rev().find(|r| !r["finished"].is_null() || !r["outcome"].is_null()).unwrap_or(&reqs[reqs.len() - 1]);
    let answered: Vec<String> = reqs.iter().filter_map(|r| s(r, "answered_at")).filter(|a| !a.is_empty()).collect();
    let finished = latest["finished"].as_i64().map(|f| f != 0);
    let project = reqs.iter().find_map(|r| s(r, "project").filter(|p| !p.is_empty()));
    let requested_by = s(first, "requester");
    let text_of_result = |r: &Value| r["result"].as_str().and_then(|t| serde_json::from_str::<Value>(t).ok())
        .and_then(|v| v["text"].as_str().map(str::to_string)).unwrap_or_default();
    let ended = answered.iter().max().cloned();
    let work_time = work_seconds(&reqs.iter().map(|r| {
        // from pickup to answer; a request never picked up (cancelled while queued, so no result) adds nothing
        let start = s(r, "started_at").filter(|v| !v.is_empty()).or_else(|| {
            r["result"].as_str().filter(|t| !t.is_empty()).and_then(|_| s(r, "arrived_at"))
        });
        (start, s(r, "answered_at"))
    }).collect::<Vec<_>>());
    let record = json!({
        "id": task_id, "title": s(first, "title").filter(|t| !t.is_empty()).unwrap_or_else(|| "Untitled".into()),
        "source": first["source"], "project": project, "requested_by": requested_by,
        "followed_up_by": distinct(reqs.iter().map(|r| s(r, "requester"))).into_iter()
            .filter(|p| Some(p) != requested_by.as_ref()).collect::<Vec<_>>(),
        "where": distinct(reqs.iter().map(|r| s(r, "link"))),
        "started_at": reqs[0]["arrived_at"], "ended_at": ended, "work_time_s": work_time, "requests": reqs.len(),
        "finished": finished, "outcome": latest["outcome"],
        "links": distinct(reqs.iter().flat_map(|r| {
            let t = text_of_result(r);
            LINKS.find_iter(&t).map(|m| Some(m.as_str().to_string())).collect::<Vec<_>>()
        })),
        "steps": reqs.iter().map(|r| json!({"at": r["arrived_at"], "by": r["requester"], "asked": r["asked"],
                                            "outcome": r["outcome"], "finished": r["finished"].as_i64().map(|f| f != 0),
                                            "work_item": r["work_item_id"], "conversation": r["conversation_id"]}))
            .collect::<Vec<_>>(),
    });
    c.execute("INSERT INTO task_records(id, source, project, requested_by, started_at, ended_at, work_time_s, finished, \
               record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET source = excluded.source, \
               project = excluded.project, requested_by = excluded.requested_by, started_at = excluded.started_at, \
               ended_at = excluded.ended_at, work_time_s = excluded.work_time_s, finished = excluded.finished, \
               record = excluded.record",
              params![task_id, record["source"].as_str(), record["project"].as_str(), record["requested_by"].as_str(),
                      record["started_at"].as_str(), record["ended_at"].as_str(), work_time,
                      finished.map(|f| f as i64), py_dumps(&record)])?;
    Ok(())
}

pub fn task_record(c: &Connection, task_id: &str) -> Result<Option<Value>> {
    let row: Option<String> = c.query_row("SELECT record FROM task_records WHERE id = ?", [task_id], |r| r.get(0)).optional()?;
    Ok(row.and_then(|r| serde_json::from_str(&r).ok()))
}

pub fn month_of(started_at: &str, tz: &str) -> String {
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    at(started_at).map(|t| t.with_timezone(&zone).format("%Y-%m").to_string()).unwrap_or_default()
}

fn month_bounds(month: &str, tz: &str) -> Option<(String, String)> {
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    let (year, mon) = month.split_once('-')?;
    let (year, mon): (i32, u32) = (year.parse().ok()?, mon.parse().ok()?);
    let start = zone.with_ymd_and_hms(year, mon, 1, 0, 0, 0).earliest()?;
    let end = zone.with_ymd_and_hms(year + (mon / 12) as i32, mon % 12 + 1, 1, 0, 0, 0).earliest()?;
    Some((start.with_timezone(&Utc).format(FMT).to_string(), end.with_timezone(&Utc).format(FMT).to_string()))
}

/// Rewrite each month's file: its tasks by when they started (in the team's timezone), newest first. A reader never
/// sees half a file: each is written beside its place and renamed over it.
pub fn export_months(c: &Connection, tasks_dir: &Path, months: &BTreeSet<String>, tz: &str) -> Result<()> {
    for month in months {
        let Some((lo, hi)) = month_bounds(month, tz) else { continue };
        let rows: Vec<String> = c.prepare("SELECT record FROM task_records WHERE started_at >= ? AND started_at < ? \
                                           ORDER BY started_at DESC, id DESC")?
            .query_map([lo, hi], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        let path = tasks_dir.join(format!("{month}.jsonl"));
        let written = (|| -> std::io::Result<()> {
            std::fs::create_dir_all(tasks_dir)?;
            if rows.is_empty() {
                return std::fs::remove_file(&path).or_else(|e| if e.kind() == std::io::ErrorKind::NotFound { Ok(()) } else { Err(e) });
            }
            let tmp = path.with_extension("jsonl.tmp");
            std::fs::write(&tmp, rows.iter().map(|r| format!("{r}\n")).collect::<String>())?;
            std::fs::rename(&tmp, &path)
        })();
        if let Err(e) = written {
            tracing::error!("couldn't write the task file for {month}: {e}");
        }
    }
    Ok(())
}

/// Every month's file, for a start: the files catch up with whatever was labeled before.
pub fn export_all(c: &Connection, tasks_dir: &Path, tz: &str) -> Result<()> {
    let months: BTreeSet<String> = c.prepare("SELECT started_at FROM task_records")?
        .query_map([], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?
        .iter().map(|s| month_of(s, tz)).collect();
    export_months(c, tasks_dir, &months, tz)
}
