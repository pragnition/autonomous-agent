//! The dashboard's read models over agent.db (a port of dashboard/queries.py): sessions (work items with their
//! conversation, task and run), the conversations they are turns of, the event log, today's counts and the KPIs with
//! daily series. A work item's payload carries system prompts and test secrets: only its `text` and `actor` are read.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use anyhow::Result;
use chrono::{DateTime, Duration, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use rusqlite::{Connection, OptionalExtension, Row as SqlRow, params_from_iter};
use serde_json::{Map, Value, json};

use crate::kpis::{self, FMT, SOURCES, local_midnight};

/// A row as the API sends it; keys starting with `_` are the API's own and never sent.
pub type Row = Map<String, Value>;

const TERMINAL: [&str; 3] = ["done", "failed", "cancelled"];
const TITLE_CHARS: usize = 120;
const LINEAR_PREFIX: &str = "Linear agent session event:";
/// The KPI page lists at most this many of the window's tasks.
const TASK_LIST: i64 = 200;

const SOURCE: &str = "CASE
    WHEN t.source = 'linear' OR EXISTS (SELECT 1 FROM settings s WHERE s.key LIKE 'linear_session:%'
                                        AND s.value = w.conversation_id) THEN 'linear'
    WHEN c.is_primary = 0 THEN 'handover'
    WHEN c.channel IS NOT NULL THEN 'slack'
    ELSE 'other' END";

fn select() -> String {
    format!("SELECT w.id, w.kind, w.status, w.conversation_id, w.created_at, w.updated_at,
    json_extract(w.payload, '$.text') AS text, json_extract(w.payload, '$.actor') AS actor,
    COALESCE(c.project, t.project) AS project, c.channel, c.thread_ts,
    t.id AS task_id, t.title AS task_title, t.requester AS task_requester, t.pr_url,
    r.id AS run_id, r.account_code, r.turns, r.input_tokens, r.output_tokens, r.cache_read_tokens,
    r.cache_write_tokens, r.outcome, r.error, r.ended_at,
    {SOURCE} AS source
FROM work_items w
LEFT JOIN conversations c ON c.id = w.conversation_id
LEFT JOIN tasks t ON t.id = c.task_id
LEFT JOIN runs r ON r.id = w.id")
}

#[derive(Clone, Debug, Default)]
pub struct SessionFilter {
    pub state: Option<String>,
    pub project: Option<String>,
    pub source: Option<String>,
    pub account: Option<String>,
    pub q: Option<String>,
    pub limit: i64,
    pub offset: i64,
}

/// The issue in a Linear agent session event a turn was started with: {identifier, title, url}.
pub fn linear_issue(text: Option<&str>) -> Option<Value> {
    let body: Value = serde_json::from_str(text?.strip_prefix(LINEAR_PREFIX)?).ok()?;
    body.get("agentSession")?.get("issue").filter(|i| i.is_object()).cloned()
}

fn cut(line: &str) -> String {
    if line.chars().count() <= TITLE_CHARS { line.into() } else { line.chars().take(TITLE_CHARS - 1).collect::<String>() + "…" }
}

/// The turn's own message: the Linear issue it was started with, else its first line.
fn message(text: Option<&str>, kind: &str, issue: Option<&Value>) -> String {
    if let Some(issue) = issue && let Some(id) = issue["identifier"].as_str().filter(|i| !i.is_empty()) {
        let title = issue["title"].as_str().unwrap_or_default();
        return format!("{id} — {title}").trim_matches([' ', '—']).to_string();
    }
    crate::records::py_lines(text.unwrap_or_default()).map(str::trim).find(|l| !l.is_empty()).map(cut)
        .unwrap_or_else(|| kind.into())
}

fn state(status: &str) -> &str {
    if status == "claimed" || status == "delivered" { "running" } else { status }
}

struct Raw {
    v: HashMap<String, Value>,
}

impl Raw {
    fn from(r: &SqlRow) -> rusqlite::Result<Raw> {
        let Value::Object(m) = crate::db::row_json(r)? else { unreachable!() };
        Ok(Raw { v: m.into_iter().collect() })
    }

    fn get(&self, k: &str) -> Value {
        self.v.get(k).cloned().unwrap_or(Value::Null)
    }

    fn str(&self, k: &str) -> Option<&str> {
        self.v.get(k).and_then(Value::as_str)
    }

    /// Python's `a or b` on two text columns.
    fn or(&self, a: &str, b: &str) -> Value {
        match self.str(a) {
            Some(s) if !s.is_empty() => json!(s),
            _ => self.get(b),
        }
    }
}

fn row(r: &Raw, started_at: Option<&String>) -> Row {
    let issue = linear_issue(r.str("text"));
    let status = r.str("status").unwrap_or_default();
    let st = state(status);
    let has_run = !r.get("run_id").is_null();
    let kind = r.str("kind").unwrap_or_default();
    let msg = message(r.str("text"), kind, issue.as_ref());
    let title = match r.str("task_title") { Some(t) if !t.is_empty() => t.to_string(), _ => msg.clone() };
    let ended = if has_run { r.get("ended_at") } else if TERMINAL.contains(&st) { r.get("updated_at") } else { Value::Null };
    let tokens = if has_run {
        json!({"in": r.get("input_tokens"), "out": r.get("output_tokens"), "cache_read": r.get("cache_read_tokens"),
               "cache_write": r.get("cache_write_tokens")})
    } else { Value::Null };
    let row = json!({
        "id": r.get("id"), "kind": kind, "state": st, "project": r.get("project"),
        "conversation_id": r.get("conversation_id"), "task_id": r.get("task_id"), "title": title, "message": msg,
        "source": r.get("source"), "requester": r.or("task_requester", "actor"),
        "created_at": r.get("created_at"), "started_at": started_at, "ended_at": ended,
        "account": r.get("account_code"), "turns": if has_run { r.get("turns") } else { Value::Null }, "tokens": tokens,
        "outcome": r.get("outcome"), "error": r.get("error"), "live": false,
        "links": {"slack": null, "linear": issue.as_ref().map_or(Value::Null, |i| i["url"].clone()), "pr": r.get("pr_url")},
        // private to the API layer, which removes them: where to ask Slack for the thread's permalink
        "_channel": r.get("channel"), "_thread_ts": r.get("thread_ts"),
    });
    let Value::Object(m) = row else { unreachable!() };
    m
}

fn marks(n: usize) -> String {
    vec!["?"; n].join(",")
}

fn started(c: &Connection, ids: &[String]) -> Result<HashMap<String, String>> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let sql = format!("SELECT json_extract(data, '$.work_item') AS item, MIN(ts) AS ts FROM events \
                       WHERE type = 'session_started' AND json_extract(data, '$.work_item') IN ({}) GROUP BY item",
                      marks(ids.len()));
    let rows = c.prepare(&sql)?.query_map(params_from_iter(ids), |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

fn state_values(state: &str) -> Vec<&str> {
    match state {
        "running" => vec!["claimed", "delivered"],
        s => vec![s],
    }
}

fn where_of(f: &SessionFilter, with_state: bool) -> (String, Vec<String>) {
    let (mut wh, mut args): (Vec<String>, Vec<String>) = (Vec::new(), Vec::new());
    if with_state && let Some(s) = f.state.as_deref().filter(|s| !s.is_empty()) {
        let values = state_values(s);
        wh.push(format!("w.status IN ({})", marks(values.len())));
        args.extend(values.into_iter().map(str::to_string));
    }
    if let Some(p) = f.project.as_deref().filter(|s| !s.is_empty()) {
        wh.push("COALESCE(c.project, t.project) = ?".into());
        args.push(p.into());
    }
    if let Some(s) = f.source.as_deref().filter(|s| !s.is_empty()) {
        wh.push(format!("{SOURCE} = ?"));
        args.push(s.into());
    }
    if let Some(a) = f.account.as_deref().filter(|s| !s.is_empty()) {
        wh.push("r.account_code = ?".into());
        args.push(a.into());
    }
    if let Some(q) = f.q.as_deref().filter(|s| !s.is_empty()) {
        wh.push("(t.title LIKE ? OR json_extract(w.payload, '$.text') LIKE ?)".into());
        args.extend([format!("%{q}%"), format!("%{q}%")]);
    }
    (if wh.is_empty() { String::new() } else { format!(" WHERE {}", wh.join(" AND ")) }, args)
}

fn raws(c: &Connection, sql: &str, args: &[String]) -> Result<Vec<Raw>> {
    Ok(c.prepare(sql)?.query_map(params_from_iter(args), Raw::from)?.collect::<rusqlite::Result<_>>()?)
}

fn rows_of(c: &Connection, raws: Vec<Raw>) -> Result<Vec<Row>> {
    let ids: Vec<String> = raws.iter().filter_map(|r| r.str("id").map(str::to_string)).collect();
    let started = started(c, &ids)?;
    Ok(raws.iter().map(|r| row(r, r.str("id").and_then(|id| started.get(id)))).collect())
}

pub fn list_sessions(c: &Connection, f: &SessionFilter) -> Result<(Vec<Row>, i64)> {
    let (clause, args) = where_of(f, true);
    let total: i64 = c.query_row(&format!("SELECT COUNT(*) FROM ({}{clause})", select()), params_from_iter(&args), |r| r.get(0))?;
    let mut page = args.clone();
    page.extend([f.limit.to_string(), f.offset.to_string()]);
    let sql = format!("{}{clause} ORDER BY w.created_at DESC, w.rowid DESC LIMIT CAST(? AS INTEGER) OFFSET CAST(? AS INTEGER)", select());
    Ok((rows_of(c, raws(c, &sql, &page)?)?, total))
}

/// A work item outside any conversation stands on its own.
const KEY: &str = "COALESCE(w.conversation_id, w.id)";

/// Per conversation: its latest message and its state (running or queued while any turn is, else its latest turn's).
fn groups_sql() -> String {
    format!("SELECT g.key, g.last_at, g.last_rowid,
    CASE WHEN g.running THEN 'claimed' WHEN g.queued THEN 'queued' ELSE l.status END AS status
FROM (SELECT {KEY} AS key, MAX(w.created_at) AS last_at, MAX(w.rowid) AS last_rowid,
             SUM(w.status = 'claimed') AS running, SUM(w.status = 'queued') AS queued
      FROM work_items w GROUP BY key) g
JOIN work_items l ON l.rowid = g.last_rowid")
}

fn turns(c: &Connection, keys: &[String]) -> Result<HashMap<String, Vec<Row>>> {
    if keys.is_empty() {
        return Ok(HashMap::new());
    }
    let sql = format!("{} WHERE {KEY} IN ({}) ORDER BY w.created_at, w.rowid", select(), marks(keys.len()));
    let mut out: HashMap<String, Vec<Row>> = HashMap::new();
    for r in rows_of(c, raws(c, &sql, keys)?)? {
        let key = r["conversation_id"].as_str().or(r["id"].as_str()).unwrap_or_default().to_string();
        out.entry(key).or_default().push(r);
    }
    Ok(out)
}

/// A page of conversations, latest message first, each as its turns (oldest first). A conversation matches the
/// filters when any of its turns does; the state filter is on the conversation's own state.
pub fn list_conversations(c: &Connection, f: &SessionFilter) -> Result<(Vec<Vec<Row>>, i64)> {
    let (clause, args) = where_of(f, false);
    let (mut wh, mut params): (Vec<String>, Vec<String>) = (Vec::new(), Vec::new());
    if let Some(s) = f.state.as_deref().filter(|s| !s.is_empty()) {
        let values = state_values(s);
        wh.push(format!("status IN ({})", marks(values.len())));
        params.extend(values.into_iter().map(str::to_string));
    }
    if !clause.is_empty() {
        wh.push(format!("key IN (SELECT COALESCE(conversation_id, id) FROM ({}{clause}))", select()));
        params.extend(args);
    }
    let groups = format!("SELECT * FROM ({}){}", groups_sql(),
                         if wh.is_empty() { String::new() } else { format!(" WHERE {}", wh.join(" AND ")) });
    let total: i64 = c.query_row(&format!("SELECT COUNT(*) FROM ({groups})"), params_from_iter(&params), |r| r.get(0))?;
    let mut page = params.clone();
    page.extend([f.limit.to_string(), f.offset.to_string()]);
    let keys: Vec<String> = c.prepare(&format!("{groups} ORDER BY last_at DESC, last_rowid DESC LIMIT CAST(? AS INTEGER) \
                                               OFFSET CAST(? AS INTEGER)"))?
        .query_map(params_from_iter(&page), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    let mut by_key = turns(c, &keys)?;
    Ok((keys.iter().filter_map(|k| by_key.remove(k)).collect(), total))
}

/// The turns of the conversation `reference` names, and the turn it named when it is a work item's id.
pub fn get_conversation(c: &Connection, reference: &str) -> Result<Option<(Vec<Row>, Option<String>)>> {
    let item: Option<(String, Option<String>)> = c.query_row("SELECT id, conversation_id FROM work_items WHERE id = ?",
                                                             [reference], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
    let key = match &item {
        Some((id, convo)) => convo.clone().unwrap_or_else(|| id.clone()),
        None => reference.to_string(),
    };
    Ok(turns(c, std::slice::from_ref(&key))?.remove(&key).filter(|t| !t.is_empty()).map(|t| (t, item.map(|i| i.0))))
}

fn at(ts: &Value) -> Option<DateTime<Utc>> {
    ts.as_str().and_then(|t| DateTime::parse_from_rfc3339(t).ok()).map(|t| t.with_timezone(&Utc))
}

fn truthy_str(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}

/// One row for a conversation from its turns, oldest first: the first says what it is and who asked, the latest how
/// it went, and the numbers add up over every turn. busy_s is the time spent working, not waiting for people.
pub fn conversation_row(turns: &[Row], now: DateTime<Utc>) -> Row {
    let (first, last) = (&turns[0], &turns[turns.len() - 1]);
    let states: BTreeSet<&str> = turns.iter().filter_map(|t| t["state"].as_str()).collect();
    let state = if states.contains("running") { json!("running") } else if states.contains("queued") { json!("queued") } else { last["state"].clone() };
    let counted: Vec<&Value> = turns.iter().map(|t| &t["tokens"]).filter(|t| t.is_object()).collect();
    let calls: Vec<i64> = turns.iter().filter_map(|t| t["turns"].as_i64()).collect();
    let accounts: Vec<&Row> = turns.iter().filter(|t| truthy_str(&t["account"])).collect();
    let busy: f64 = turns.iter().filter(|t| t["state"] != "queued").map(|t| {
        let end = if truthy_str(&t["ended_at"]) { at(&t["ended_at"]).unwrap_or(now) } else { now };
        let start = if truthy_str(&t["started_at"]) { at(&t["started_at"]) } else { at(&t["created_at"]) }.unwrap_or(now);
        (end - start).num_milliseconds() as f64 / 1000.0
    }).sum();
    let running = state == "running" || state == "queued";
    let last_active = turns.iter().flat_map(|t| [&t["created_at"], &t["started_at"], &t["ended_at"]])
        .filter_map(|v| v.as_str().filter(|s| !s.is_empty())).max();
    let tokens = if counted.is_empty() { Value::Null } else {
        Value::Object(["in", "out", "cache_read", "cache_write"].iter()
            .map(|k| (k.to_string(), json!(counted.iter().map(|c| c[*k].as_i64().unwrap_or(0)).sum::<i64>()))).collect())
    };
    let row = json!({
        "id": if truthy_str(&first["conversation_id"]) { first["conversation_id"].clone() } else { first["id"].clone() },
        "kind": first["kind"], "state": state, "project": last["project"], "conversation_id": first["conversation_id"],
        "task_id": last["task_id"], "title": first["title"], "source": first["source"], "requester": first["requester"],
        "created_at": first["created_at"], "started_at": first["started_at"],
        "ended_at": if running { Value::Null } else { last["ended_at"].clone() }, "last_active_at": last_active,
        "messages": turns.len(), "busy_s": busy.round_ties_even() as i64,
        "account": accounts.last().map_or(Value::Null, |a| a["account"].clone()),
        "account_name": accounts.last().and_then(|a| a.get("account_name")).cloned().unwrap_or(Value::Null),
        "turns": if calls.is_empty() { Value::Null } else { json!(calls.iter().sum::<i64>()) }, "tokens": tokens,
        "outcome": last["outcome"], "error": last["error"], "live": turns.iter().any(|t| t["live"] == true),
        "links": {"slack": null,
                  "linear": turns.iter().map(|t| &t["links"]["linear"]).find(|l| truthy_str(l)).cloned().unwrap_or(Value::Null),
                  "pr": turns.iter().rev().map(|t| &t["links"]["pr"]).find(|l| truthy_str(l)).cloned().unwrap_or(Value::Null)},
    });
    let Value::Object(m) = row else { unreachable!() };
    m
}

pub fn get_session(c: &Connection, item_id: &str) -> Result<Option<Row>> {
    let found = raws(c, &format!("{} WHERE w.id = ?", select()), &[item_id.to_string()])?;
    Ok(rows_of(c, found)?.into_iter().next())
}

pub fn overview_counts(c: &Connection, now: DateTime<Utc>, tz: &str) -> Result<Value> {
    let since = local_midnight(now, tz.parse().unwrap_or(chrono_tz::UTC)).format(FMT).to_string();
    let count = |sql: &str, args: &[&str]| -> Result<i64> { Ok(c.query_row(sql, params_from_iter(args), |r| r.get(0))?) };
    Ok(json!({"running": count("SELECT COUNT(*) FROM work_items WHERE status = 'claimed'", &[])?,
              "queued": count("SELECT COUNT(*) FROM work_items WHERE status = 'queued'", &[])?,
              "done_today": count("SELECT COUNT(*) FROM work_items WHERE status = 'done' AND updated_at >= ?", &[&since])?,
              "failed_today": count("SELECT COUNT(*) FROM work_items WHERE status = 'failed' AND updated_at >= ?", &[&since])?}))
}

pub fn paused_by(c: &Connection) -> Result<Value> {
    if crate::db::get_setting(c, crate::queue::PAUSED)?.as_deref() != Some("1") {
        return Ok(Value::Null);
    }
    let last: Option<(Option<String>, String)> = c.query_row(
        "SELECT actor, ts FROM events WHERE type = 'stop_all' ORDER BY id DESC LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
    Ok(match last {
        Some((actor, ts)) => json!({"actor": actor, "at": ts}),
        None => json!({"actor": null, "at": null}),
    })
}

const SYSTEM_ACTORS: [&str; 3] = ["local", "agent", "system"];

fn session_source(source: &str) -> &'static str {
    match source { "slack" => "slack", "linear" => "linear", "handover" => "agent", _ => "other" }
}

struct Event {
    id: i64,
    ts: String,
    kind: String,
    actor: Option<String>,
    task_id: Option<String>,
    data: Value,
}

/// Where each event came from (slack, linear, dashboard, command line, agent, other) and, for Slack and the
/// dashboard, who: a person key or tailscale:<login>.
fn event_sources(c: &Connection, events: &[Event]) -> Result<Vec<(String, Option<String>)>> {
    let items: BTreeSet<String> = events.iter().filter_map(|e| e.data["work_item"].as_str().map(str::to_string)).collect();
    let mut sessions: HashMap<String, (String, Value)> = HashMap::new();
    if !items.is_empty() {
        let items: Vec<String> = items.into_iter().collect();
        for r in raws(c, &format!("{} WHERE w.id IN ({})", select(), marks(items.len())), &items)? {
            sessions.insert(r.str("id").unwrap_or_default().into(), (r.str("source").unwrap_or_default().into(), r.or("task_requester", "actor")));
        }
    }
    let task_ids: BTreeSet<String> = events.iter().filter_map(|e| e.task_id.clone().filter(|t| !t.is_empty())).collect();
    let mut tasks: HashMap<String, (String, Option<String>)> = HashMap::new();
    if !task_ids.is_empty() {
        let ids: Vec<String> = task_ids.into_iter().collect();
        let sql = format!("SELECT id, source, requester FROM tasks WHERE id IN ({})", marks(ids.len()));
        for (id, source, requester) in c.prepare(&sql)?.query_map(params_from_iter(&ids), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .collect::<rusqlite::Result<Vec<(String, String, Option<String>)>>>()? {
            tasks.insert(id, (source, requester));
        }
    }
    Ok(events.iter().map(|e| {
        let actor = e.actor.clone();
        if e.data["via"] == "dashboard" {
            return ("dashboard".into(), actor);
        }
        if actor.as_deref() == Some("local") {
            return ("command line".into(), None);
        }
        if let Some((source, requester)) = e.data["work_item"].as_str().and_then(|w| sessions.get(w)) {
            let source = session_source(source);
            return (source.into(), if source == "slack" { requester.as_str().map(str::to_string) } else { None });
        }
        if let Some((source, requester)) = e.task_id.as_ref().and_then(|t| tasks.get(t)) {
            let source = if source == "slack" || source == "linear" { source.as_str() } else { "other" };
            return (source.into(), if source == "slack" { requester.clone() } else { None });
        }
        match actor {
            Some(a) if !a.is_empty() && !SYSTEM_ACTORS.contains(&a.as_str()) && !a.starts_with("tailscale:") =>
                ("slack".into(), Some(a)), // a person's request to the agent, made in a Slack conversation
            _ => ("other".into(), None),
        }
    }).collect())
}

pub fn list_events(c: &Connection, kind: Option<&str>, limit: i64, before: Option<i64>) -> Result<Value> {
    let (mut wh, mut args): (Vec<&str>, Vec<String>) = (Vec::new(), Vec::new());
    if let Some(k) = kind.filter(|k| !k.is_empty()) {
        wh.push("type = ?");
        args.push(k.into());
    }
    if let Some(b) = before {
        wh.push("id < CAST(? AS INTEGER)");
        args.push(b.to_string());
    }
    args.push(limit.to_string());
    let clause = if wh.is_empty() { String::new() } else { format!(" WHERE {}", wh.join(" AND ")) };
    let events: Vec<Event> = c.prepare(&format!("SELECT id, ts, type, actor, task_id, data FROM events{clause} ORDER BY id DESC \
                                                 LIMIT CAST(? AS INTEGER)"))?
        .query_map(params_from_iter(&args), |r| {
            let data: Option<String> = r.get(5)?;
            Ok(Event { id: r.get(0)?, ts: r.get(1)?, kind: r.get(2)?, actor: r.get(3)?, task_id: r.get(4)?,
                       data: data.and_then(|d| serde_json::from_str::<Value>(&d).ok()).filter(Value::is_object)
                           .unwrap_or_else(|| json!({})) })
        })?.collect::<rusqlite::Result<_>>()?;
    let types: Vec<String> = c.prepare("SELECT DISTINCT type FROM events ORDER BY type")?
        .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    let sources = event_sources(c, &events)?;
    Ok(json!({"rows": events.iter().zip(sources).map(|(e, (source, sender))| json!({
        "id": e.id, "ts": e.ts, "type": e.kind, "source": source, "sender": sender})).collect::<Vec<_>>(), "types": types}))
}

/// The four KPIs, tasks per local day by source, the window's tasks, newest first, token use by project for today,
/// 7 days and this month, and tokens per local day by project for the last 7 days.
pub fn kpi_view(c: &Connection, days: i64, now: DateTime<Utc>, tz: &str) -> Result<Value> {
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    let today = now.with_timezone(&zone).date_naive();
    let dates: Vec<NaiveDate> = (0..days).rev().map(|n| today - Duration::days(n)).collect();
    let first = dates.first().copied().unwrap_or(today);
    let lo = zone.from_local_datetime(&first.and_time(NaiveTime::MIN)).earliest().map_or(now, |t| t.with_timezone(&Utc))
        .format(FMT).to_string();
    let hi = now.format(FMT).to_string();
    let mut daily: BTreeMap<String, Map<String, Value>> = dates.iter().map(|d| {
        let mut m = Map::new();
        m.insert("date".into(), json!(d.to_string()));
        for s in SOURCES {
            m.insert(s.into(), json!(0));
        }
        (d.to_string(), m)
    }).collect();
    let rows: Vec<(String, String)> = c.prepare("SELECT started_at, source FROM task_records WHERE started_at >= ? AND started_at < ?")?
        .query_map([&lo, &hi], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
    for (started, source) in rows {
        let Some(day) = at(&json!(started)).map(|t| t.with_timezone(&zone).date_naive().to_string()) else { continue };
        if let Some(m) = daily.get_mut(&day) && let Some(n) = m.get_mut(&source) {
            *n = json!(n.as_i64().unwrap_or(0) + 1);
        }
    }
    let since = now - Duration::days(days);
    let tasks: Vec<Value> = c.prepare("SELECT record FROM task_records WHERE started_at >= ? AND started_at < ? \
                                       ORDER BY started_at DESC, id DESC LIMIT ?")?
        .query_map(rusqlite::params![since.format(FMT).to_string(), hi, TASK_LIST], |r| r.get::<_, String>(0))?
        .filter_map(|r| r.ok().and_then(|t| serde_json::from_str(&t).ok())).collect();
    Ok(json!({"kpis": kpis::kpis_json(&kpis::compute_kpis(c, since, now)?),
              "daily": daily.into_values().map(Value::Object).collect::<Vec<_>>(), "tasks": tasks,
              "usage": kpis::windows_json(&kpis::usage_windows(c, now, tz)?),
              "daily_tokens": kpis::daily_json(&kpis::daily_tokens(c, now, tz, 7)?)}))
}
