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
        .route("/proxy/whoami", axum::routing::get(|| async { axum::Json(json!({"account": "acct-a"})) }));
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
    let config_dir = PathBuf::from(REPO).join("examples/config");
    let paths = agentd::shared::Paths { config: config_dir, data: dir.join("data"), work: dir.join("work"),
                                        defaults: PathBuf::from(REPO).join("src/agent/defaults") };
    let secrets = [("GH_TOKEN_CORE".to_string(), "core-secret-0123456789".to_string())].into();
    let proxy = agentd::proxy::ProxyClient::new(proxy_url, agentd::proxy::SoftCap::default());
    Arc::new(agentd::shared::Shared::open(paths, secrets, agentd::slack::Slack::new("xoxb-test", &format!("{}/api", slack.base)),
                                          Some("UBOT".into()), proxy, None).unwrap())
}
