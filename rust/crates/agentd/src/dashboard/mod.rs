//! The dashboard (a port of dashboard/app.py and access.py): JSON read models, a live stream per session, the kill
//! switch and the meter's caps, and the built web app with an index.html fallback for its client-side routes. The
//! web app lists conversations, each with its turns (work items, here called sessions).
//!
//! Who may use it: Tailscale Serve is the only way in, so requests arrive from trusted peers (the Docker gateway) and
//! name the person in Tailscale-User-Login. The headers are trusted only from those peers; anything else gets 403.
//! POSTs also need a header a cross-site form can't send, and a same-site Origin when there is one.

pub mod accounts;
pub mod queries;
pub mod transcript;

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{ConnectInfo, Path as UrlPath, Query, Request, State};
use axum::http::{Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use chrono::Utc;
use serde_json::{Value, json};

use crate::azure_rates::AzureRates;
use crate::db::{append_event, get_setting};
use crate::host::{HostStore, consumers_view, host_view};
use crate::queue;
use crate::shared::Shared;
use queries::{Row, SessionFilter};
use transcript::{Scrubber, TranscriptCache};

const ACTIVE: [&str; 2] = ["queued", "claimed"];
const RECENT: usize = 5;
const MAX_ROWS: i64 = 200;
const PROXY_TIMEOUT: Duration = Duration::from_secs(8);
/// A session's account: worth a short wait, never a slow page.
const LOOKUP_TIMEOUT: Duration = Duration::from_secs(2);
const HISTORY_TTL: Duration = Duration::from_secs(60);
const PING: Duration = Duration::from_secs(15);
const NOT_BUILT: &str = "The dashboard's web app isn't built into this image: run `npm run build` in dashboard/ or rebuild the image.";

pub struct Options {
    /// The built web app; None serves a note saying it isn't built.
    pub static_dir: Option<PathBuf>,
    /// How often a live stream looks for more of its transcript.
    pub poll: Duration,
    pub host: Arc<Mutex<HostStore>>,
    pub rates: Option<Arc<AzureRates>>,
}

struct Dash {
    s: Arc<Shared>,
    root: Option<PathBuf>,
    poll: Duration,
    host: Arc<Mutex<HostStore>>,
    rates: Option<Arc<AzureRates>>,
    scrub: Scrubber,
    transcripts: TranscriptCache,
    /// session id -> account; a session never moves to another account.
    pinned: Mutex<HashMap<String, String>>,
    history: Mutex<HashMap<i64, (Instant, Value)>>,
    names: AccountNames,
}

type D = State<Arc<Dash>>;

/// The person looking: their Tailscale login and name, and the person in [people] it matches.
#[derive(Clone)]
struct Viewer {
    login: String,
    name: String,
    person: Option<(String, String)>,
}

impl Viewer {
    fn actor(&self) -> String {
        self.person.as_ref().map_or_else(|| format!("tailscale:{}", self.login), |p| p.0.clone())
    }

    fn can_resume(&self) -> bool {
        self.person.as_ref().is_some_and(|p| p.1 == "admin")
    }
}

/// Account code names → {name, email}, from the config's account-names.toml; read again when it changes.
struct AccountNames {
    path: PathBuf,
    cache: Mutex<(Option<std::time::SystemTime>, serde_json::Map<String, Value>)>,
}

impl AccountNames {
    fn all(&self) -> serde_json::Map<String, Value> {
        let Ok(mtime) = std::fs::metadata(&self.path).and_then(|m| m.modified()) else { return Default::default() };
        let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
        if cache.0 != Some(mtime) {
            let data: Value = std::fs::read_to_string(&self.path).ok().and_then(|t| toml::from_str(&t).ok()).unwrap_or(Value::Null);
            cache.1 = data["accounts"].as_object().into_iter().flatten()
                .filter(|(_, r)| r["name"].as_str().is_some_and(|n| !n.is_empty())).map(|(k, v)| (k.clone(), v.clone())).collect();
            cache.0 = Some(mtime);
        }
        cache.1.clone()
    }

    fn name(&self, code: Option<&str>) -> Value {
        code.and_then(|c| self.all().get(c).map(|r| r["name"].clone())).unwrap_or(Value::Null)
    }
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, axum::Json(json!({"error": message}))).into_response()
}

fn ok(v: Value) -> Response {
    axum::Json(v).into_response()
}

/// A whole number from the query, within bounds; the error the page shows otherwise.
fn int(q: &HashMap<String, String>, key: &str, default: Option<i64>, lo: i64, hi: i64) -> Result<Option<i64>, Response> {
    match q.get(key).filter(|v| !v.is_empty()) {
        None => Ok(default),
        Some(raw) => match raw.parse::<i64>() {
            Err(_) => Err(error(StatusCode::BAD_REQUEST, &format!("{key} must be a whole number"))),
            Ok(v) if !(lo..=hi).contains(&v) => Err(error(StatusCode::BAD_REQUEST, &format!("{key} must be from {lo} to {hi}"))),
            Ok(v) => Ok(Some(v)),
        },
    }
}

macro_rules! tri {
    ($e:expr) => {
        match $e {
            Ok(v) => v,
            Err(r) => return r,
        }
    };
}

fn internal(e: anyhow::Error) -> Response {
    tracing::error!("dashboard: {e:#}");
    error(StatusCode::INTERNAL_SERVER_ERROR, &format!("{e:#}"))
}

/// The dashboard's listener.
pub fn app(s: Arc<Shared>, options: Options) -> Router {
    let root = options.static_dir.and_then(|d| d.canonicalize().ok());
    let dash = Arc::new(Dash {
        scrub: Scrubber::new(&s.secret_values), transcripts: TranscriptCache::new(Scrubber::new(&s.secret_values), RECENT),
        names: AccountNames { path: s.paths.config.join("account-names.toml"), cache: Mutex::default() },
        root, poll: options.poll, host: options.host, rates: options.rates, pinned: Mutex::default(),
        history: Mutex::default(), s,
    });
    Router::new()
        .route("/api/me", get(me))
        .route("/api/overview", get(overview))
        .route("/api/conversations", get(conversations))
        .route("/api/conversations/{id}", get(conversation))
        .route("/api/sessions/{id}", get(session))
        .route("/api/sessions/{id}/stream", get(stream))
        .route("/api/sessions/{id}/cancel", post(cancel))
        .route("/api/accounts", get(accounts_page))
        .route("/api/accounts/history", get(history))
        .route("/api/kpis", get(kpis))
        .route("/api/events", get(events))
        .route("/api/stop", post(stop))
        .route("/api/resume", post(resume))
        .route("/api/llm", get(llm))
        .route("/api/llm/caps", post(llm_caps))
        .route("/api/host", get(host_totals))
        .route("/api/host/consumers", get(host_consumers))
        .fallback(web_app)
        .layer(middleware::from_fn_with_state(dash.clone(), access))
        .with_state(dash)
}

async fn access(State(d): D, ConnectInfo(peer): ConnectInfo<SocketAddr>, mut req: Request, next: Next) -> Response {
    if !d.s.config.dashboard.trusted_peers.iter().any(|p| p.parse().ok() == Some(peer.ip())) {
        return error(StatusCode::FORBIDDEN, "not from the tailnet");
    }
    if req.uri().path().starts_with("/api/") {
        let h = req.headers();
        let text = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).map(str::trim).unwrap_or_default().to_string();
        let login = text("Tailscale-User-Login");
        if login.is_empty() {
            return error(StatusCode::FORBIDDEN, "no Tailscale identity");
        }
        if req.method() != Method::GET && req.method() != Method::HEAD {
            if text("X-Dashboard") != "1" {
                return error(StatusCode::FORBIDDEN, "missing X-Dashboard header");
            }
            let origin = text("Origin");
            if !origin.is_empty() {
                let netloc = origin.split_once("://").map_or("", |(_, rest)| rest).split('/').next().unwrap_or_default();
                if netloc != text("Host") && netloc != text("X-Forwarded-Host") {
                    return error(StatusCode::FORBIDDEN, "cross-site request");
                }
            }
        }
        let name = Some(text("Tailscale-User-Name")).filter(|n| !n.is_empty()).unwrap_or_else(|| login.clone());
        let person = d.s.config.person_by_tailscale(&login).map(|p| (p.key.clone(), p.role.clone()));
        req.extensions_mut().insert(Viewer { login, name, person });
    }
    next.run(req).await
}

impl Dash {
    fn transcript(&self, item: &str) -> PathBuf {
        self.s.paths.data.join("runs").join(format!("{item}.jsonl"))
    }

    async fn account_of(&self, session_id: Option<&str>) -> Option<String> {
        let sid = session_id?;
        if let Some(a) = self.pinned.lock().unwrap_or_else(|e| e.into_inner()).get(sid) {
            return Some(a.clone());
        }
        let account = tokio::time::timeout(LOOKUP_TIMEOUT, self.s.proxy.whoami(sid)).await.ok()?.ok()??;
        self.pinned.lock().unwrap_or_else(|e| e.into_inner()).insert(sid.into(), account.clone());
        Some(account)
    }

    async fn slack_link(&self, row: &Row) -> Value {
        match (row.get("_channel").and_then(Value::as_str), row.get("_thread_ts").and_then(Value::as_str)) {
            (Some(channel), Some(ts)) => self.s.permalink(channel, ts).await.map_or(Value::Null, Value::String),
            _ => Value::Null,
        }
    }

    /// Add what needs I/O: the Slack permalink (detail pages only), and the numbers a running session has so far
    /// (or a finished one that never reported), read incrementally from its transcript.
    async fn fill(&self, mut row: Row, links: bool, recent: bool) -> Row {
        if links {
            let link = self.slack_link(&row).await;
            row["links"]["slack"] = link;
        }
        let mut last = Vec::new();
        let id = row["id"].as_str().unwrap_or_default().to_string();
        let running = row["state"] == "running";
        if running || (row["tokens"].is_null() && row["state"] != "queued" && self.transcript(&id).exists()) {
            let (stats, recent_entries) = self.transcripts.follow(&self.transcript(&id));
            last = recent_entries;
            row["tokens"] = stats.tokens.clone();
            row["turns"] = json!(stats.turns);
            if running {
                row["live"] = json!(true);
                row["account"] = self.account_of(stats.session_id.as_deref()).await.map_or(Value::Null, Value::String);
            }
        }
        row.insert("account_name".into(), self.names.name(row["account"].as_str()));
        row.retain(|k, _| !k.starts_with('_'));
        if recent {
            row.insert("recent".into(), json!(last));
        }
        row
    }

    async fn fill_all(&self, rows: Vec<Row>, recent: bool) -> Vec<Row> {
        futures_util::future::join_all(rows.into_iter().map(|r| self.fill(r, false, recent))).await
    }
}

fn viewer(req_ext: &axum::http::Extensions) -> Viewer {
    req_ext.get::<Viewer>().cloned().unwrap_or(Viewer { login: String::new(), name: String::new(), person: None })
}

async fn me(State(d): D, req: Request) -> Response {
    let v = viewer(req.extensions());
    ok(json!({"login": v.login, "name": v.name, "person": v.person.as_ref().map(|p| &p.0),
              "role": v.person.as_ref().map(|p| &p.1), "can_resume": v.can_resume(), "agent": d.s.config.name}))
}

async fn overview(State(d): D) -> Response {
    let now = Utc::now();
    let (running, queued, paused, paused_by, counts) = {
        let c = d.s.db.lock();
        let list = |state: &str| queries::list_sessions(&c, &SessionFilter { state: Some(state.into()), limit: MAX_ROWS, ..Default::default() });
        match (list("running"), list("queued"), queries::paused_by(&c), queries::overview_counts(&c, now, &d.s.config.timezone)) {
            (Ok(r), Ok(q), Ok(p), Ok(n)) => (r.0, q.0, get_setting(&c, queue::PAUSED).ok().flatten().as_deref() == Some("1"), p, n),
            (Err(e), ..) | (_, Err(e), ..) | (_, _, Err(e), _) | (.., Err(e)) => return internal(e),
        }
    };
    ok(json!({"paused": paused, "paused_by": paused_by, "running": d.fill_all(running, true).await,
              "queued": d.fill_all(queued, false).await, "counts": counts}))
}

async fn conversations(State(d): D, Query(q): Query<HashMap<String, String>>) -> Response {
    let text = |k: &str| q.get(k).filter(|v| !v.is_empty()).cloned();
    let f = SessionFilter { state: text("state"), project: text("project"), source: text("source"), account: text("account"),
                            q: text("q"), limit: tri!(int(&q, "limit", Some(50), 1, 100_000)).unwrap_or(50).min(MAX_ROWS),
                            offset: tri!(int(&q, "offset", Some(0), 0, 1_000_000_000)).unwrap_or(0) };
    let found = queries::list_conversations(&d.s.db.lock(), &f);
    let (groups, total) = match found { Ok(g) => g, Err(e) => return internal(e) };
    let now = Utc::now();
    let mut rows = Vec::new();
    for turns in groups {
        rows.push(queries::conversation_row(&d.fill_all(turns, false).await, now));
    }
    ok(json!({"rows": rows, "total": total}))
}

/// A conversation and its turns (`items`; a row's `turns` counts model calls), named by its id or by one of its
/// turns' (then `focus`).
async fn conversation(State(d): D, UrlPath(id): UrlPath<String>) -> Response {
    let found = queries::get_conversation(&d.s.db.lock(), &id);
    let Some((turns, focus)) = (match found { Ok(f) => f, Err(e) => return internal(e) }) else {
        return error(StatusCode::NOT_FOUND, "no such conversation");
    };
    let link = d.slack_link(&turns[turns.len() - 1]).await;
    let filled = d.fill_all(turns, false).await;
    let mut row = queries::conversation_row(&filled, Utc::now());
    row["links"]["slack"] = link;
    row.insert("items".into(), json!(filled));
    row.insert("focus".into(), json!(focus));
    ok(Value::Object(row))
}

async fn session(State(d): D, UrlPath(id): UrlPath<String>, Query(q): Query<HashMap<String, String>>) -> Response {
    let found = queries::get_session(&d.s.db.lock(), &id);
    let Some(row) = (match found { Ok(r) => r, Err(e) => return internal(e) }) else {
        return error(StatusCode::NOT_FOUND, "no such session");
    };
    if q.get("entries").map(String::as_str) == Some("0") { // the page polls the header; the timeline comes from the stream
        return ok(Value::Object(d.fill(row, true, false).await));
    }
    let (entries, offset, line) = transcript::read_from(&d.transcript(&id), 0, 0, &d.scrub);
    let mut row = d.fill(row, true, false).await;
    row.extend([("entries".to_string(), json!(entries)), ("offset".into(), json!(offset)), ("line".into(), json!(line))]);
    ok(Value::Object(row))
}

fn status_of(s: &Shared, id: &str) -> Option<String> {
    queue::get(&s.db.lock(), id).ok().flatten().map(|i| i.status)
}

async fn stream(State(d): D, UrlPath(id): UrlPath<String>, Query(q): Query<HashMap<String, String>>) -> Response {
    if status_of(&d.s, &id).is_none() {
        return error(StatusCode::NOT_FOUND, "no such session");
    }
    let offset = tri!(int(&q, "offset", Some(0), 0, 1_000_000_000_000)).unwrap_or(0) as u64;
    let path = d.transcript(&id);
    // no cursor: count the lines before `offset` once, then follow with the cursor
    let line = match tri!(int(&q, "line", None, 0, 1_000_000_000)) {
        Some(l) => l as usize,
        None => transcript::lines_before(&path, offset),
    };
    struct Follow {
        d: Arc<Dash>,
        id: String,
        path: PathBuf,
        offset: u64,
        line: usize,
        pinged: Instant,
        done: bool,
    }
    let follow = Follow { d, id, path, offset, line, pinged: Instant::now(), done: false };
    let events = futures_util::stream::unfold(follow, |mut f| async move {
        loop {
            if f.done {
                return None;
            }
            let (entries, offset, line) = transcript::read_from(&f.path, f.offset, f.line, &f.d.scrub);
            (f.offset, f.line) = (offset, line);
            let status = status_of(&f.d.s, &f.id);
            let active = status.as_deref().is_some_and(|s| ACTIVE.contains(&s));
            if !entries.is_empty() {
                let event = format!("event: entries\ndata: {}\n\n", json!({"entries": entries, "offset": offset, "line": line}));
                return Some((Ok::<Bytes, std::io::Error>(Bytes::from(event)), f));
            }
            if !active { // drained: nothing more will be written for it
                f.done = true;
                let event = format!("event: end\ndata: {}\n\n", json!({"state": status.unwrap_or_else(|| "gone".into())}));
                return Some((Ok(Bytes::from(event)), f));
            }
            if f.pinged.elapsed() >= PING {
                f.pinged = Instant::now();
                return Some((Ok(Bytes::from_static(b": ping\n\n")), f));
            }
            tokio::time::sleep(f.d.poll).await;
        }
    });
    ([(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache"),
      (header::HeaderName::from_static("x-accel-buffering"), "no")], Body::from_stream(events)).into_response()
}

async fn cancel(State(d): D, UrlPath(id): UrlPath<String>, req: Request) -> Response {
    let v = viewer(req.extensions());
    let Some(status) = status_of(&d.s, &id) else { return error(StatusCode::NOT_FOUND, "no such session") };
    if !ACTIVE.contains(&status.as_str()) {
        return (StatusCode::CONFLICT, axum::Json(json!({"error": "already finished", "state": status}))).into_response();
    }
    let done = (|| -> anyhow::Result<()> {
        let c = d.s.db.lock();
        queue::cancel(&c, &id)?;
        let task = queries::get_session(&c, &id)?.and_then(|r| r["task_id"].as_str().map(str::to_string));
        append_event(&c, "session_cancelled", task.as_deref(), Some(&v.actor()), Some(&json!({"work_item": id, "via": "dashboard"})))?;
        Ok(())
    })();
    if let Err(e) = done {
        return internal(e);
    }
    d.s.wake.notify_one();
    ok(json!({"ok": true, "state": "cancelled"}))
}

async fn stop(State(d): D, req: Request) -> Response {
    let v = viewer(req.extensions());
    let stopped = queue::stop_all(&d.s.db.lock(), &v.actor(), Some("dashboard"));
    match stopped {
        Ok(cancelled) => {
            d.s.wake.notify_one();
            ok(json!({"ok": true, "cancelled": cancelled}))
        }
        Err(e) => internal(e),
    }
}

async fn resume(State(d): D, req: Request) -> Response {
    let v = viewer(req.extensions());
    if !v.can_resume() {
        return error(StatusCode::FORBIDDEN, "only an admin can resume");
    }
    if let Err(e) = queue::resume_all(&d.s.db.lock(), &v.actor(), Some("dashboard")) {
        return internal(e);
    }
    d.s.wake.notify_one();
    ok(json!({"ok": true}))
}

async fn accounts_page(State(d): D) -> Response {
    let origins = &d.s.config.dashboard.agent_origins;
    let now = Utc::now().timestamp_millis() as f64 / 1000.0;
    let raw = match tokio::time::timeout(PROXY_TIMEOUT, d.s.proxy.raw_usage()).await {
        Ok(Ok(raw)) => raw,
        Ok(Err(e)) => return ok(accounts::accounts_view(None, origins, Some(&format!("{e:#}")), d.s.proxy.soft_cap, now)),
        Err(_) => return ok(accounts::accounts_view(None, origins, Some("TimeoutError"), d.s.proxy.soft_cap, now)),
    };
    let mut view = accounts::accounts_view(Some(&raw), origins, None, d.s.proxy.soft_cap, now);
    let known = d.names.all();
    for a in view["accounts"].as_array_mut().into_iter().flatten() {
        let rec = a["code"].as_str().and_then(|c| known.get(c)).cloned().unwrap_or(Value::Null);
        a["name"] = rec.get("name").cloned().unwrap_or(Value::Null);
        a["email"] = rec.get("email").cloned().unwrap_or(Value::Null);
    }
    ok(view)
}

async fn history(State(d): D, Query(q): Query<HashMap<String, String>>) -> Response {
    let hours = tri!(int(&q, "hours", Some(24), 1, 720)).unwrap_or(24);
    if !accounts::HOURS.contains(&hours) {
        return error(StatusCode::BAD_REQUEST, "hours must be one of 6, 24, 168, 720");
    }
    let names = |view: &Value| -> Value {
        Value::Object(view["accounts"].as_array().into_iter().flatten().filter_map(|c| c.as_str())
            .filter_map(|c| d.names.name(Some(c)).as_str().map(|n| (c.to_string(), json!(n)))).collect())
    };
    let cached = d.history.lock().unwrap_or_else(|e| e.into_inner()).get(&hours)
        .filter(|(at, _)| at.elapsed() < HISTORY_TTL).map(|(_, v)| v.clone());
    let view = match cached {
        Some(v) => v,
        None => {
            let rows = match tokio::time::timeout(PROXY_TIMEOUT * 3, d.s.proxy.perf(hours)).await {
                Ok(Ok(rows)) => rows,
                failed => {
                    let why = match failed { Ok(Err(e)) => format!("{e:#}"), _ => "TimeoutError".into() };
                    return ok(json!({"error": why, "hours": hours, "bucket_s": accounts::bucket_seconds(hours),
                                     "accounts": [], "origins": [], "buckets": []}));
                }
            };
            let now = Utc::now().timestamp_millis() as f64 / 1000.0;
            let view = accounts::history_view(&rows, hours, &d.s.config.dashboard.agent_origins, now);
            d.history.lock().unwrap_or_else(|e| e.into_inner()).insert(hours, (Instant::now(), view.clone()));
            view
        }
    };
    let mut out = view.clone();
    out["names"] = names(&view);
    ok(out)
}

async fn kpis(State(d): D, Query(q): Query<HashMap<String, String>>) -> Response {
    let days = tri!(int(&q, "days", Some(7), 1, 365)).unwrap_or(7);
    match queries::kpi_view(&d.s.db.lock(), days, Utc::now(), &d.s.config.timezone) {
        Ok(v) => ok(v),
        Err(e) => internal(e),
    }
}

async fn events(State(d): D, Query(q): Query<HashMap<String, String>>) -> Response {
    let limit = tri!(int(&q, "limit", Some(100), 1, 500)).unwrap_or(100);
    let before = tri!(int(&q, "before", None, 0, 1 << 62));
    let found = queries::list_events(&d.s.db.lock(), q.get("type").map(String::as_str).filter(|t| !t.is_empty()), limit, before);
    let mut page = match found { Ok(p) => p, Err(e) => return internal(e) };
    let mut shown: HashMap<String, String> = HashMap::new();
    for r in page["rows"].as_array().into_iter().flatten() {
        if let Some(sender) = r["sender"].as_str().filter(|s| !s.is_empty()) && !shown.contains_key(sender) {
            let display = match sender.strip_prefix("tailscale:") {
                Some(login) => login.to_string(),
                None => d.s.person_name(sender).await.unwrap_or_else(|| sender.to_string()),
            };
            shown.insert(sender.to_string(), display);
        }
    }
    for r in page["rows"].as_array_mut().into_iter().flatten() {
        if let Some(name) = r["sender"].as_str().and_then(|s| shown.get(s)) {
            r["sender"] = json!(name);
        }
    }
    ok(page)
}

fn meter_installed(s: &Shared) -> bool {
    !s.config.llm_meter.upstreams.is_empty()
}

fn meter_view(d: &Dash) -> anyhow::Result<Value> {
    let s = &d.s;
    let view = crate::meter::meter_view(&s.db.lock(), &s.config.llm_meter, &s.config.timezone, Utc::now(), d.rates.as_deref())?;
    let mut out = serde_json::Map::from_iter([("installed".to_string(), json!(true))]);
    out.extend(view.as_object().cloned().unwrap_or_default());
    Ok(Value::Object(out))
}

/// What the apps under test spent on their model provider through the meter.
async fn llm(State(d): D) -> Response {
    if !meter_installed(&d.s) {
        return ok(json!({"installed": false}));
    }
    meter_view(&d).map_or_else(internal, ok)
}

async fn llm_caps(State(d): D, req: Request) -> Response {
    if !meter_installed(&d.s) {
        return error(StatusCode::NOT_FOUND, "the model-call meter isn't installed");
    }
    let v = viewer(req.extensions());
    if !v.can_resume() {
        return error(StatusCode::FORBIDDEN, "only an admin can change the caps");
    }
    let body = axum::body::to_bytes(req.into_body(), 1 << 20).await.unwrap_or_default();
    let body: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let Some(fields) = body.as_object() else { return error(StatusCode::BAD_REQUEST, "give daily_usd, monthly_usd or both") };
    let mut values = [None, None];
    for (i, key) in ["daily_usd", "monthly_usd"].iter().enumerate() {
        match fields.get(*key) {
            None | Some(Value::Null) => {}
            Some(Value::Number(n)) => values[i] = n.as_f64(),
            Some(_) => return error(StatusCode::BAD_REQUEST, &format!("{key} must be a number of US dollars")),
        }
    }
    if values.iter().all(Option::is_none) {
        return error(StatusCode::BAD_REQUEST, "give daily_usd, monthly_usd or both");
    }
    if let Err(e) = crate::meter::set_caps(&d.s.db.lock(), &v.actor(), values[0], values[1]) {
        return error(StatusCode::BAD_REQUEST, &format!("{e:#}"));
    }
    meter_view(&d).map_or_else(internal, ok)
}

fn wall_now() -> f64 {
    Utc::now().timestamp_millis() as f64 / 1000.0
}

async fn host_totals(State(d): D) -> Response {
    ok(host_view(&d.host.lock().unwrap_or_else(|e| e.into_inner()), wall_now()))
}

/// The dashboard session a Host row links to: its own turn, else its conversation's newest.
fn session_of(s: &Shared, item: Option<&str>, conversation: Option<&str>) -> Value {
    let c = s.db.lock();
    let item = match (item, conversation) {
        (Some(i), _) => Some(i.to_string()),
        (None, Some(convo)) => Some(c.query_row("SELECT id FROM work_items WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
                                                [convo], |r| r.get::<_, String>(0)).unwrap_or_else(|_| convo.to_string())),
        (None, None) => None,
    };
    match item.and_then(|i| queries::get_session(&c, &i).ok().flatten()) {
        Some(r) => json!({"id": r["id"], "title": r["title"], "state": r["state"]}),
        None => Value::Null,
    }
}

async fn host_consumers(State(d): D) -> Response {
    let store = d.host.lock().unwrap_or_else(|e| e.into_inner());
    ok(consumers_view(&store, wall_now(), |item, convo| session_of(&d.s, item, convo)))
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or_default() {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

async fn web_app(State(d): D, req: Request) -> Response {
    let path = req.uri().path().to_string();
    if path == "/api" || path.starts_with("/api/") {
        return error(StatusCode::NOT_FOUND, "no such route");
    }
    let Some(root) = d.root.as_ref().filter(|r| r.join("index.html").is_file()) else { return NOT_BUILT.into_response() };
    let tail = percent_decode(path.trim_start_matches('/'));
    let wanted = root.join(&tail).canonicalize().ok().filter(|w| w != root && w.starts_with(root) && w.is_file());
    let (file, cache) = match wanted {
        Some(w) => {
            let immutable = w.parent().and_then(|p| p.file_name()).is_some_and(|n| n == "assets");
            (w, if immutable { "public, max-age=31536000, immutable" } else { "no-cache" })
        }
        None => (root.join("index.html"), "no-cache"),
    };
    match tokio::fs::read(&file).await {
        Ok(data) => ([(header::CONTENT_TYPE, content_type(&file)), (header::CACHE_CONTROL, cache)], data).into_response(),
        Err(_) => error(StatusCode::NOT_FOUND, "not found"),
    }
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let (mut out, mut i) = (Vec::with_capacity(bytes.len()), 0);
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len()
            && let Some(b) = std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
            out.push(b);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
