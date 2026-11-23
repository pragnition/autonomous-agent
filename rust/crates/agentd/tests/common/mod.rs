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
}

impl FakeSlack {
    pub async fn start() -> FakeSlack {
        let mut fake = FakeSlack::default();
        let app = axum::Router::new()
            .route("/api/{method}", axum::routing::any(handle))
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        fake.base = format!("http://{}", listener.local_addr().unwrap());
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
        _ => json!({"ok": true}),
    })
}

/// agentd's shared state over a temp folder: the example config and pack, the fake Slack, bot user UBOT.
pub fn shared(dir: &std::path::Path, slack: &FakeSlack) -> Arc<agentd::shared::Shared> {
    let config_dir = PathBuf::from(REPO).join("examples/config");
    let paths = agentd::shared::Paths { config: config_dir, data: dir.to_path_buf(),
                                        defaults: PathBuf::from(REPO).join("src/agent/defaults") };
    let secrets = [("GH_TOKEN_CORE".to_string(), "core-secret-0123456789".to_string())].into();
    Arc::new(agentd::shared::Shared::open(paths, secrets, agentd::slack::Slack::new("xoxb-test", &format!("{}/api", slack.base)),
                                          Some("UBOT".into())).unwrap())
}
