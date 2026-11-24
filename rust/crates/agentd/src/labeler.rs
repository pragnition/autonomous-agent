//! The labeler (a port of core/task_labeler.py): after the agent answers a request, Haiku says whether it was chat,
//! a new task or the same task as an earlier one, with a title and a one-line outcome. The agent does nothing for it.
//!
//! agentd calls Haiku through the account proxy, like the sessions do, and only while the proxy has capacity, so
//! labeling never takes room from real work. A sweep labels every finished request that has no label yet: in arrival
//! order per requester (a label depends on that person's earlier requests), different requesters side by side. A
//! request Haiku can't be reached for waits for the next sweep; one it keeps answering badly becomes chat after
//! MAX_TRIES. Nothing here runs in a turn's path: a result only wakes the sweep loop.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Result, anyhow};
use futures_util::StreamExt;
use rusqlite::OptionalExtension;
use serde_json::{Value, json};

use crate::shared::Shared;
use crate::task_record::{self as tr, Item, Label, NO_TEXT, RequestContext};

pub const MODEL: &str = "claude-haiku-4-5";
pub const PROMPT: &str = include_str!("labeler_prompt.txt");
const MAX_TRIES: i64 = 3;
const TITLE_CHARS: usize = 60;
const CONCURRENCY: usize = 4;
const SWEEP_EVERY: Duration = Duration::from_secs(300);

/// complete(system, user, session id) -> Haiku's text. An error means Haiku can't answer now: the request waits for
/// a later sweep without using up a try.
pub type Complete =
    Arc<dyn Fn(String, String, String) -> Pin<Box<dyn Future<Output = Result<String>> + Send>> + Send + Sync>;

/// Whether the proxy has room: labeling only runs on capacity real work isn't using.
pub type Capacity = Arc<dyn Fn() -> Pin<Box<dyn Future<Output = bool> + Send>> + Send + Sync>;

/// The proxy's own answer: any account under its soft cap.
pub fn proxy_capacity(proxy: Arc<crate::proxy::ProxyClient>) -> Capacity {
    Arc::new(move || {
        let proxy = proxy.clone();
        Box::pin(async move {
            match proxy.usage().await {
                Ok(u) => u.has_capacity(),
                Err(e) => {
                    tracing::warn!("couldn't ask the proxy for capacity; labeling waits: {e:#}");
                    false
                }
            }
        })
    })
}

/// Haiku through the account proxy at `proxy_url` (agentd's own relay, AGENTD_PROXY).
pub fn haiku(proxy_url: &str) -> Complete {
    let (base, http) = (proxy_url.trim_end_matches('/').to_string(),
                        reqwest::Client::builder().timeout(Duration::from_secs(60)).build().expect("an HTTP client"));
    Arc::new(move |system, user, session| {
        let (base, http) = (base.clone(), http.clone());
        Box::pin(async move {
            let resp = http.post(format!("{base}/v1/messages"))
                .header("authorization", format!("Bearer {}", crate::prepare::PLACEHOLDER_TOKEN))
                .header("anthropic-version", "2023-06-01").header("anthropic-beta", "oauth-2025-04-20")
                .header("x-claude-code-session-id", session)
                .json(&json!({"model": MODEL, "max_tokens": 400, "system": system,
                              "messages": [{"role": "user", "content": user}]}))
                .send().await.map_err(|e| anyhow!("proxy unreachable: {e}"))?;
            let status = resp.status();
            if status.as_u16() == 429 || status.is_server_error() {
                return Err(anyhow!("proxy answered {status}"));
            }
            let body = resp.text().await.unwrap_or_default();
            if status.is_client_error() { // a request it will never take: counts as a bad answer, so it ends as chat
                tracing::warn!("Haiku refused a label request: {status} {}", body.chars().take(300).collect::<String>());
                return Ok(String::new());
            }
            let Ok(answer) = serde_json::from_str::<Value>(&body) else {
                tracing::warn!("the proxy answered a label request with something that isn't JSON");
                return Ok(String::new());
            };
            Ok(answer["content"].as_array().into_iter().flatten().filter(|b| b["type"] == "text")
                .filter_map(|b| b["text"].as_str()).collect())
        })
    })
}

/// The one JSON object in Haiku's answer, fences and prose around it allowed.
pub fn parse_label(text: &str) -> Option<Label> {
    let (start, end) = (text.find('{')?, text.rfind('}')?);
    if end < start {
        return None;
    }
    let data: Value = serde_json::from_str(&text[start..=end]).ok()?;
    let label = data["label"].as_str().filter(|l| ["chat", "new", "same"].contains(l))?;
    let text_of = |key: &str| data[key].as_str().map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
    Some(Label { label: label.into(), task: text_of("task"), title: text_of("title"), outcome: text_of("outcome"),
                 finished: data["finished"].as_bool(), project: text_of("project") })
}

fn where_of(p: &crate::config::Project) -> String {
    [(!p.repo.is_empty()).then(|| format!("repo {}", p.repo)),
     (!p.slack_channel.is_empty()).then(|| format!("Slack {}", p.slack_channel)),
     (!p.linear_team.is_empty()).then(|| format!("Linear team {}", p.linear_team))]
        .into_iter().flatten().collect::<Vec<_>>().join(", ")
}

/// The user message: the projects, what came before, the ids Haiku may use, the request and the reply.
pub fn render(ctx: &RequestContext, source: &str, requester: Option<&str>,
              packs: &BTreeMap<String, crate::config::Pack>, hints: &[String]) -> String {
    let s = |v: &Value| v.as_str().map_or("None".to_string(), str::to_string); // a missing title reads as Python's
    let mut lines = vec!["Projects:".to_string()];
    lines.extend(packs.iter().map(|(name, pack)| format!("- {name}: {}", where_of(&pack.project))));
    if packs.is_empty() {
        lines.push("- none configured (project is always null)".into());
    }
    lines.push(format!("Signals core found for this request: {}", if hints.is_empty() { "none".into() } else { hints.join(", ") }));
    lines.push("Earlier requests in this conversation (oldest first):".into());
    for e in &ctx.earlier {
        let what = match e["task"].as_str() {
            Some(t) => format!("{t} \"{}\"", s(&e["title"])),
            None => "chat".into(),
        };
        let project = e["project"].as_str().filter(|p| !p.is_empty()).map(|p| format!(", {p}")).unwrap_or_default();
        lines.push(format!("- [{what}{project}] {}", s(&e["asked"])));
    }
    if ctx.earlier.is_empty() {
        lines.push("- none".into());
    }
    lines.push("This person's other tasks from the last 24 hours:".into());
    for t in &ctx.recent {
        let or = |v: &Value, d: &str| v.as_str().filter(|x| !x.is_empty()).unwrap_or(d).to_string();
        lines.push(format!("- {} \"{}\" ({}; latest: {})", s(&t["task"]), s(&t["title"]), or(&t["project"], "general"),
                           or(&t["outcome"], "no outcome yet")));
    }
    if ctx.recent.is_empty() {
        lines.push("- none".into());
    }
    let mut ids: Vec<String> = Vec::new();
    for id in ctx.earlier.iter().chain(&ctx.recent).filter_map(|e| e["task"].as_str()) {
        if !ids.iter().any(|i| i == id) {
            ids.push(id.into());
        }
    }
    lines.extend([format!("Task ids you may use for \"same\": {}", if ids.is_empty() { "none".into() } else { ids.join(", ") }),
                  String::new(), format!("The request (from {}, via {source}):", requester.unwrap_or("someone")),
                  ctx.asked.clone(), String::new(),
                  format!("The agent's reply{}:", if ctx.errored { " (the turn ended in an error)" } else { "" }),
                  ctx.reply.clone()]);
    lines.join("\n")
}

/// One request to label, with where it came from.
struct Request {
    row: Item,
    source: String,
    requester: Option<String>,
    follows: Option<String>,
}

enum Outcome {
    /// Labeled: its task, or None for chat.
    Labeled(Option<String>),
    /// This requester's later requests wait for the next sweep.
    Stop,
}

pub struct Labeler {
    complete: Complete,
    capacity: Capacity,
    sweeping: tokio::sync::Mutex<()>,
}

impl Labeler {
    pub fn new(complete: Complete, capacity: Capacity) -> Labeler {
        Labeler { complete, capacity, sweeping: tokio::sync::Mutex::new(()) }
    }

    /// Sweep every 5 minutes, and whenever a request is answered (`s.labels`); a wake during a sweep makes one more.
    pub async fn run(self: Arc<Self>, s: Arc<Shared>) {
        loop {
            if let Err(e) = self.sweep(&s).await {
                tracing::error!("labeling requests failed: {e:#}");
            }
            let _ = tokio::time::timeout(SWEEP_EVERY, s.labels.notified()).await;
        }
    }

    /// Label what can be labeled now; how many requests got a label.
    pub async fn sweep(&self, s: &Shared) -> Result<usize> {
        let _one = self.sweeping.lock().await;
        let rows = tr::pending(&s.db.lock())?;
        if rows.is_empty() || !(self.capacity)().await {
            return Ok(0);
        }
        let groups = groups(s, rows)?;
        let (session, months, labeled) = (crate::turn::uuid4(), Mutex::new(BTreeSet::new()), Mutex::new(0usize));
        let run = |group: Vec<Request>| {
            let (session, months, labeled) = (&session, &months, &labeled);
            async move {
                for req in group {
                    match self.label(s, &req, session, months).await {
                        Ok(Outcome::Stop) => return,
                        Ok(Outcome::Labeled(task)) => {
                            *labeled.lock().unwrap() += 1;
                            if let Some(t) = task {
                                let started = tr::task_record(&s.db.lock(), &t).ok().flatten()
                                    .and_then(|r| r["started_at"].as_str().map(str::to_string)).unwrap_or_default();
                                months.lock().unwrap().insert(tr::month_of(&started, &s.config.timezone));
                            }
                        }
                        Err(e) => {
                            tracing::error!("labeling {}'s requests failed: {e:#}", req.row.id);
                            return;
                        }
                    }
                }
            }
        };
        // groups run side by side, every one finishing before the sweep ends
        futures_util::stream::iter(groups).map(run).buffer_unordered(CONCURRENCY).collect::<Vec<()>>().await;
        let months = months.into_inner().unwrap();
        if !months.is_empty() {
            tr::export_months(&s.db.lock(), &s.paths.data.join("tasks"), &months, &s.config.timezone)?;
        }
        let labeled = labeled.into_inner().unwrap();
        if labeled > 0 {
            tracing::info!("labeled {labeled} request(s)");
        }
        Ok(labeled)
    }

    /// Label one request. A relabeled request adds the month of the task it was in to `months`, so a task it
    /// leaves is rewritten too.
    async fn label(&self, s: &Shared, req: &Request, session: &str, months: &Mutex<BTreeSet<String>>) -> Result<Outcome> {
        let Request { row, source, requester, follows } = req;
        let mut fixed = None;
        if let Some(follows) = follows {
            let parent: Option<(Option<String>, Option<String>, Option<String>)> = s.db.lock().query_row(
                "SELECT w.conversation_id, l.label, l.task_id FROM work_items w LEFT JOIN request_labels l \
                 ON l.work_item_id = w.id WHERE w.id = ?", [follows], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional()?;
            match parent {
                Some((_, Some(_), task)) => fixed = task,
                Some((convo, None, _)) if !convo.as_deref().unwrap_or_default().starts_with("V-smoke") => return Ok(Outcome::Stop),
                _ => {}
            }
        }
        if !(self.capacity)().await {
            return Ok(Outcome::Stop);
        }
        let (ctx, hints) = {
            let c = s.db.lock();
            (tr::context_for(&c, row, requester.as_deref())?, tr::project_hints(&c, row, &s.packs)?)
        };
        let offered: BTreeSet<&str> = ctx.earlier.iter().chain(&ctx.recent).filter_map(|e| e["task"].as_str()).collect();
        let user = render(&ctx, source, requester.as_deref(), &s.packs, &hints);
        let answer = match (self.complete)(PROMPT.into(), user, session.into()).await {
            Ok(a) => a,
            Err(e) => { // not the request's fault: it waits for the next sweep without using up a try
                tracing::info!("labeling waits: {e:#}");
                return Ok(Outcome::Stop);
            }
        };
        let c = s.db.lock();
        let before: Option<HashMap<String, Value>> = c.query_row(
            "SELECT * FROM request_labels WHERE work_item_id = ?", [&row.id], crate::db::row_json).optional()?
            .and_then(|v| serde_json::from_value(v).ok());
        let text = |b: &HashMap<String, Value>, k: &str| b.get(k).and_then(Value::as_str).map(str::to_string);
        let mut label = match parse_label(&answer) {
            None => {
                let tries = tr::count_try(&c, row, source, requester.as_deref(), follows.as_deref())?;
                if tries < MAX_TRIES {
                    return Ok(Outcome::Stop);
                }
                match &before {
                    Some(b) if text(b, "label").is_some() => {
                        tracing::warn!("gave up relabeling {} after {tries} unreadable answers; it keeps its label", row.id);
                        Label { label: text(b, "label").unwrap_or_default(), task: text(b, "task_id"), title: text(b, "title"),
                                outcome: text(b, "outcome"), finished: b.get("finished").and_then(Value::as_i64).map(|f| f != 0),
                                project: text(b, "project") }
                    }
                    _ => {
                        tracing::warn!("gave up labeling {} after {tries} unreadable answers; it counts as chat", row.id);
                        match &fixed {
                            Some(t) => Label { label: "same".into(), task: Some(t.clone()), ..Label::chat() },
                            None => Label::chat(),
                        }
                    }
                }
            }
            Some(l) if fixed.is_some() => Label { label: "same".into(), task: fixed.clone(), title: None, ..l },
            Some(l) if l.label == "same" && !l.task.as_deref().is_some_and(|t| offered.contains(t)) =>
                Label { label: "new".into(), task: None, title: None, ..l },
            Some(l) => l,
        };
        if label.label == "new" && label.title.is_none() {
            let title = if ctx.asked != NO_TEXT { ctx.asked.as_str() } else { "Untitled" };
            label = Label { task: None, title: Some(title.chars().take(TITLE_CHARS).collect::<String>().trim().to_string()), ..label };
        }
        label.project = project(&s.packs, label.project.as_deref(), &hints);
        if let Some(b) = &before && let Some(old) = text(b, "task_id") && let Some(rec) = tr::task_record(&c, &old)? {
            months.lock().unwrap().insert(tr::month_of(rec["started_at"].as_str().unwrap_or_default(), &s.config.timezone));
        }
        Ok(Outcome::Labeled(tr::apply_label(&c, row, &label, source, requester.as_deref(), follows.as_deref())?))
    }
}

/// Haiku's project when it's a configured one; a name that isn't falls back to what agentd can see. None stays
/// general: general work can happen in a project's channel too.
fn project(packs: &BTreeMap<String, crate::config::Pack>, named: Option<&str>, hints: &[String]) -> Option<String> {
    let named = named.filter(|n| !n.is_empty())?;
    packs.keys().find(|k| k.to_lowercase() == named.to_lowercase()).cloned().or_else(|| hints.first().cloned())
}

/// Requests that can depend on each other, each group in arrival order: a label reads the earlier requests of its
/// conversation and its requester's recent tasks, so requests sharing either (or a hand-over's parent's
/// conversation) are labeled one after another. Unrelated groups run side by side.
fn groups(s: &Shared, rows: Vec<Item>) -> Result<Vec<Vec<Request>>> {
    let mut parent: HashMap<String, String> = HashMap::new();
    fn root(parent: &mut HashMap<String, String>, key: &str) -> String {
        let mut key = key.to_string();
        loop {
            let up = parent.entry(key.clone()).or_insert_with(|| key.clone()).clone();
            if up == key {
                return key;
            }
            let grand = parent.get(&up).cloned().unwrap_or_else(|| up.clone());
            parent.insert(key, grand.clone());
            key = grand;
        }
    }
    let c = s.db.lock();
    let mut items = Vec::new();
    for row in rows {
        let (source, requester, follows) = tr::origin_of(&c, &s.config, &row)?;
        let mut keys = vec![format!("c:{}", row.conversation_id.as_deref().unwrap_or("None"))];
        if let Some(r) = requester.as_deref().filter(|r| !r.is_empty()) {
            keys.push(format!("r:{r}"));
        }
        if let Some(f) = follows.as_deref().filter(|f| !f.is_empty()) {
            let up: Option<Option<String>> = c.query_row("SELECT conversation_id FROM work_items WHERE id = ?", [f],
                                                         |r| r.get(0)).optional()?;
            if let Some(up) = up {
                keys.push(format!("c:{}", up.as_deref().unwrap_or("None")));
            }
        }
        for key in &keys[1..] {
            let (a, b) = (root(&mut parent, key), root(&mut parent, &keys[0]));
            parent.insert(a, b);
        }
        items.push((keys[0].clone(), Request { row, source, requester, follows }));
    }
    let mut order: Vec<String> = Vec::new();
    let mut groups: HashMap<String, Vec<Request>> = HashMap::new();
    for (key, req) in items {
        let r = root(&mut parent, &key);
        if !groups.contains_key(&r) {
            order.push(r.clone());
        }
        groups.entry(r).or_default().push(req);
    }
    Ok(order.into_iter().filter_map(|k| groups.remove(&k)).collect())
}
