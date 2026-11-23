//! Shared by stage 3's tests: a fake Slack Web API that records every call, and agentd's shared state on a temp
//! database with the example config.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use axum::extract::{Path, RawQuery, State};
use serde_json::{Value, json};

pub const REPO: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../..");

#[derive(Clone, Default)]
pub struct FakeSlack {
    pub base: String,
    pub calls: Arc<Mutex<Vec<(String, Value)>>>,
    /// What conversations.replies and conversations.history answer with.
    pub messages: Arc<Mutex<Vec<Value>>>,
    ts: Arc<AtomicU64>,
    url: Arc<std::sync::OnceLock<String>>,
}

impl FakeSlack {
    pub async fn start() -> FakeSlack {
        let mut fake = FakeSlack::default();
        let app = axum::Router::new()
            .route("/api/{method}", axum::routing::any(handle))
            .route("/upload/{id}", axum::routing::post(|| async { "OK" }))
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        fake.base = format!("http://{}", listener.local_addr().unwrap());
        fake.url.set(fake.base.clone()).unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        fake
    }

    /// The bodies of every call to `method`, in order.
    pub fn sent(&self, method: &str) -> Vec<Value> {
        self.calls.lock().unwrap().iter().filter(|(m, _)| m == method).map(|(_, b)| b.clone()).collect()
    }

    pub fn reactions(&self) -> Vec<String> {
        self.calls.lock().unwrap().iter().filter(|(m, _)| m.starts_with("reactions."))
            .map(|(m, b)| format!("{}{}", if m == "reactions.add" { "+" } else { "-" }, b["name"].as_str().unwrap()))
            .collect()
    }
}

async fn handle(State(fake): State<FakeSlack>, Path(method): Path<String>, RawQuery(query): RawQuery,
                body: axum::body::Bytes) -> axum::Json<Value> {
    let mut args: Value = serde_json::from_slice(&body).unwrap_or_else(|_| {
        json!(serde_urlencoded::from_bytes::<HashMap<String, String>>(&body).unwrap_or_default())
    });
    for (k, v) in serde_urlencoded::from_str::<HashMap<String, String>>(&query.unwrap_or_default()).unwrap_or_default() {
        args[k] = json!(v);
    }
    fake.calls.lock().unwrap().push((method.clone(), args.clone()));
    let n = fake.ts.fetch_add(1, Ordering::SeqCst) + 1;
    axum::Json(match method.as_str() {
        "chat.postMessage" => json!({"ok": true, "channel": args["channel"], "ts": format!("900.{n:06}")}),
        "conversations.open" => json!({"ok": true, "channel": {"id": format!("D-{}", args["users"].as_str().unwrap_or(""))}}),
        "chat.getPermalink" => json!({"ok": true, "permalink": format!("https://slack.example/p{}", args["message_ts"].as_str().unwrap_or(""))}),
        "users.info" => json!({"ok": true, "user": {"real_name": format!("Name {}", args["user"].as_str().unwrap_or(""))}}),
        "conversations.info" => json!({"ok": true, "channel": {"name": "general"}}),
        "conversations.replies" | "conversations.history" => json!({"ok": true, "messages": *fake.messages.lock().unwrap()}),
        "auth.test" => json!({"ok": true, "user_id": "UBOT"}),
        "files.getUploadURLExternal" => json!({"ok": true, "upload_url": format!("{}/upload/F{n}", fake.url.get().unwrap()),
                                              "file_id": format!("F{n}")}),
        _ => json!({"ok": true}),
    })
}

/// The account proxy: `usage` as given; every session is pinned to acct-a.
pub async fn fake_proxy(usage: Value) -> String {
    fake_proxy_live(Arc::new(Mutex::new(usage))).await
}

/// The same, with usage a test can change.
pub async fn fake_proxy_live(usage: Arc<Mutex<Value>>) -> String {
    let app = axum::Router::new()
        .route("/proxy/usage", axum::routing::get(move || { let u = usage.lock().unwrap().clone(); async move { axum::Json(u) } }))
        .route("/proxy/whoami", axum::routing::get(|| async { axum::Json(json!({"account": "acct-a"})) }))
        .route("/proxy/perf", axum::routing::get(|| async { axum::Json(json!({"rows": []})) }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

/// acct-a used `a_used` of its window (resetting in 4 hours), acct-b 10%; acct-b ranks first.
pub fn usage(a_used: f64) -> Value {
    let reset = chrono::Utc::now().timestamp() as f64 + 4.0 * 3600.0;
    json!({"acct-a": {"status": "ok", "rank": 1, "verified": true, "5h": [a_used, reset]},
           "acct-b": {"status": "ok", "rank": 0, "verified": true, "5h": [0.10, reset]}})
}

/// agentd's shared state over a temp folder: the example config and pack, the fake Slack, bot user UBOT, and a
/// proxy where both accounts have room.
pub async fn shared(dir: &std::path::Path, slack: &FakeSlack) -> Arc<agentd::shared::Shared> {
    shared_with(dir, slack, usage(0.2)).await
}

pub async fn shared_with(dir: &std::path::Path, slack: &FakeSlack, usage: Value) -> Arc<agentd::shared::Shared> {
    shared_at(dir, slack, &fake_proxy(usage).await)
}

pub fn shared_at(dir: &std::path::Path, slack: &FakeSlack, proxy_url: &str) -> Arc<agentd::shared::Shared> {
    Arc::new(shared_plain(dir, slack, proxy_url))
}

/// The shared state before it is shared, for a test that changes its config.
pub fn shared_plain(dir: &std::path::Path, slack: &FakeSlack, proxy_url: &str) -> agentd::shared::Shared {
    let config_dir = PathBuf::from(REPO).join("examples/config");
    let paths = agentd::shared::Paths { config: config_dir, data: dir.join("data"), work: dir.join("work"),
                                        defaults: PathBuf::from(REPO).join("src/agent/defaults"),
                                        plugins: PathBuf::from(REPO).join("src/agent/plugins") };
    let secrets = [("GH_TOKEN_CORE".to_string(), "core-secret-0123456789".to_string())].into();
    let proxy = agentd::proxy::ProxyClient::new(proxy_url, agentd::proxy::SoftCap::default());
    agentd::shared::Shared::open(paths, secrets, agentd::slack::Slack::new("xoxb-test", &format!("{}/api", slack.base)),
                                 Some("UBOT".into()), proxy, None).unwrap()
}

/// The stage 4 fixture: its rows inserted into `conn` (tests/fixtures/dashboard/make.py wrote both files).
pub fn seed(conn: &rusqlite::Connection) {
    seed_rows(conn, &serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/seed.json").unwrap()).unwrap());
}

/// Rows given as {table: [{column: value}]} inserted into `conn`.
pub fn seed_rows(conn: &rusqlite::Connection, rows: &Value) {
    for (table, list) in rows.as_object().unwrap() {
        for row in list.as_array().unwrap() {
            let row = row.as_object().unwrap();
            let cols: Vec<&str> = row.keys().map(String::as_str).collect();
            let marks = vec!["?"; cols.len()].join(", ");
            let values: Vec<rusqlite::types::Value> = row.values().map(|v| match v {
                Value::Null => rusqlite::types::Value::Null,
                Value::Number(n) if n.is_i64() => rusqlite::types::Value::Integer(n.as_i64().unwrap()),
                Value::Number(n) => rusqlite::types::Value::Real(n.as_f64().unwrap()),
                Value::String(s) => rusqlite::types::Value::Text(s.clone()),
                other => panic!("{other} in seed.json"),
            }).collect();
            conn.execute(&format!("INSERT INTO {table}({}) VALUES ({marks})", cols.join(", ")),
                         rusqlite::params_from_iter(values)).unwrap();
        }
    }
}

pub fn expected() -> Value {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/expected.json").unwrap()).unwrap()
}

pub fn fixture_now() -> chrono::DateTime<chrono::Utc> {
    expected()["now"].as_str().unwrap().parse().unwrap()
}

/// Equal as JSON, numbers by value (Python writes 20.0 where Rust writes 20) and keys in the same order (the web app
/// shows maps in their order); panics with the path of the first difference.
pub fn assert_json(got: &Value, want: &Value, what: &str) {
    fn diff(got: &Value, want: &Value, path: String) -> Option<String> {
        match (got, want) {
            (Value::Number(a), Value::Number(b)) => {
                let (a, b) = (a.as_f64().unwrap(), b.as_f64().unwrap());
                ((a - b).abs() > 1e-9 * b.abs().max(1.0)).then(|| format!("{path}: {a} != {b}"))
            }
            (Value::Object(a), Value::Object(b)) => {
                if !a.keys().eq(b.keys()) && a.len() == b.len() && a.keys().all(|k| b.contains_key(k)) {
                    return Some(format!("{path}: keys in another order: {:?} != {:?}", a.keys().collect::<Vec<_>>(),
                                        b.keys().collect::<Vec<_>>()));
                }
                let keys: std::collections::BTreeSet<&String> = a.keys().chain(b.keys()).collect();
                keys.into_iter().find_map(|k| match (a.get(k), b.get(k)) {
                    (Some(x), Some(y)) => diff(x, y, format!("{path}.{k}")),
                    (x, y) => Some(format!("{path}.{k}: {x:?} != {y:?}")),
                })
            }
            (Value::Array(a), Value::Array(b)) if a.len() == b.len() =>
                a.iter().zip(b).enumerate().find_map(|(i, (x, y))| diff(x, y, format!("{path}[{i}]"))),
            _ => (got != want).then(|| format!("{path}: {got} != {want}")),
        }
    }
    if let Some(d) = diff(got, want, String::new()) {
        panic!("{what}: {d}");
    }
}

pub const FAKE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../testdata/fake_claude.py");

/// Containers as holders on tasks in this process: `start` runs `agent-holder serve`.
pub struct LocalEngine {
    pub root: PathBuf,
    pub holders: Mutex<HashMap<String, Option<tokio::task::JoinHandle<()>>>>,
}

impl agentd::containers::Engine for LocalEngine {
    async fn inspect(&self, name: &str) -> anyhow::Result<Option<bool>> {
        Ok(self.holders.lock().unwrap().get(name).map(|h| h.as_ref().is_some_and(|h| !h.is_finished())))
    }
    async fn create(&self, spec: &agentd::containers::ContainerSpec) -> anyhow::Result<()> {
        self.holders.lock().unwrap().insert(spec.name.clone(), None);
        Ok(())
    }
    async fn start(&self, name: &str) -> anyhow::Result<()> {
        let (root, conversation) = (self.root.clone(), name.trim_start_matches("agent-conv-").to_string());
        let task = tokio::spawn(async move { agent_holder::serve::serve(root, conversation).await.unwrap() });
        self.holders.lock().unwrap().insert(name.into(), Some(task));
        Ok(())
    }
    async fn stop(&self, name: &str) -> anyhow::Result<()> {
        if let Some(task) = self.holders.lock().unwrap().get_mut(name).and_then(Option::take) {
            task.abort();
        }
        Ok(())
    }
    async fn remove(&self, name: &str) -> anyhow::Result<()> {
        self.stop(name).await?;
        self.holders.lock().unwrap().remove(name);
        Ok(())
    }
}

pub type Local = agentd::dispatch::Dispatcher<LocalEngine, agentd::containers::HolderProbe>;

/// Everything `agentd serve` puts together, on a temp folder, with Claude Code played by the fake from `attempts`.
pub struct Rig {
    pub dir: tempfile::TempDir,
    pub slack: FakeSlack,
    pub s: Arc<agentd::shared::Shared>,
    pub containers: Arc<agentd::containers::Containers<LocalEngine, agentd::containers::HolderProbe>>,
    pub proxy_url: String,
}

impl Rig {
    pub async fn new(attempts: &[Vec<Value>]) -> Rig {
        let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
        let proxy_url = fake_proxy(usage(0.2)).await;
        let s = shared_at(dir.path(), &slack, &proxy_url);
        let run = s.paths.work.join("run");
        let settings = agentd::config::Containers { image: "unused".into(), memory_gib: 1, cpus: 1.0, pids: 100,
            idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 0 };
        let host = agentd::containers::Host { data_dir: "/unused".into(), work_volume: "unused".into(),
                                              network_container: "unused".into(), uid: 1001, run_root: run.clone() };
        let engine = LocalEngine { root: run.clone(), holders: Mutex::default() };
        let containers = Arc::new(agentd::containers::Containers::new(engine, agentd::containers::HolderProbe { run_root: run },
                                                                      s.db.clone(), settings, host));
        let scenario = dir.path().join("scenario");
        for (k, steps) in attempts.iter().enumerate() {
            std::fs::write(format!("{}.{k}", scenario.display()), steps.iter().map(|s| format!("{s}\n")).collect::<String>()).unwrap();
        }
        std::fs::write(&scenario, "").unwrap();
        Rig { dir, slack, s, containers, proxy_url }
    }

    /// A dispatcher, as a freshly started agentd would make one.
    pub fn dispatcher(&self) -> Arc<Local> {
        let work = &self.s.paths.work;
        let settings = agentd::turn::RunnerSettings {
            claude: vec!["python3".into(), FAKE.into(), self.dir.path().join("scenario").display().to_string()],
            proxy_url: self.proxy_url.clone(), claude_home: work.join("claude"), transcripts: work.join("transcripts"),
            run_root: work.join("run"), env_files: vec![],
            fixed_env: [("FAKE_RUN_ROOT".to_string(), work.join("run").display().to_string())].into(),
            watchdog: std::time::Duration::from_millis(100), usage_every: std::time::Duration::from_millis(100),
            soft_cap_grace: std::time::Duration::from_secs(1800), max_forks: 2 };
        let proxy = agentd::proxy::ProxyClient::new(&self.proxy_url, agentd::proxy::SoftCap::default());
        let runner = Arc::new(agentd::turn::TurnRunner::new(self.containers.clone(), self.s.db.clone(), proxy, settings));
        let admission = agentd::admission::Admission { max_sessions: 10, min_free_gib: 0, meminfo: "/nonexistent".into() };
        agentd::dispatch::Dispatcher::new(self.s.clone(), runner, self.containers.clone(), admission)
    }

    /// The fake Claude Code's starts so far.
    pub fn starts(&self) -> usize {
        std::fs::read_to_string(self.dir.path().join("scenario.starts")).unwrap_or_default().lines().count()
    }

    pub async fn until(&self, what: &str, cond: impl Fn(&Rig) -> bool) {
        for _ in 0..400 {
            if cond(self) {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        panic!("timed out waiting for {what}");
    }

    pub fn posted(&self) -> Vec<String> {
        self.slack.sent("chat.postMessage").iter().map(|p| p["text"].as_str().unwrap_or_default().to_string()).collect()
    }
}

pub fn read_prompt() -> Value {
    json!({"wait_stdin": 2})
}

pub fn init() -> Value {
    json!({"emit": {"type": "system", "subtype": "init"}})
}

pub fn say(text: &str) -> Value {
    json!({"emit": {"type": "assistant", "parent_tool_use_id": null,
                    "message": {"role": "assistant", "model": "fake", "id": format!("msg-{text}"), "content": [{"type": "text", "text": text}]}}})
}

pub fn result(text: &str) -> Value {
    json!({"emit": {"type": "result", "subtype": "success", "is_error": false, "duration_ms": 10, "duration_api_ms": 5, "num_turns": 1,
                    "result": text, "total_cost_usd": 0.25, "usage": {"input_tokens": 10, "output_tokens": 5,
                    "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}})
}
