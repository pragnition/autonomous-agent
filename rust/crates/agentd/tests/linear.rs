//! Linear: agent-session webhooks, the app install and its token, label intake and status sync, against a fake
//! Linear (a port of core's linear_*.py and public_api.py behaviour).

mod common;

use std::sync::{Arc, Mutex};

use agentd::linear::{self, Linear, LinearApp, LinearClient};
use agentd::shared::Shared;
use common::*;
use hmac::Mac;
use serde_json::{Value, json};

#[derive(Clone, Default)]
struct FakeLinear {
    base: String,
    calls: Arc<Mutex<Vec<(String, Value)>>>,
    issues: Arc<Mutex<Vec<Value>>>,
}

impl FakeLinear {
    async fn start() -> FakeLinear {
        let mut fake = FakeLinear::default();
        let state = fake.clone();
        let app = axum::Router::new()
            .route("/graphql", axum::routing::post(|axum::extract::State(f): axum::extract::State<FakeLinear>, axum::Json(body): axum::Json<Value>| async move {
                // by its name, or the query's own name when none is sent
                let op = body["operationName"].as_str().map(str::to_string).unwrap_or_else(|| {
                    body["query"].as_str().unwrap_or_default().split_whitespace().nth(1).unwrap_or_default().to_string()
                });
                f.calls.lock().unwrap().push((op.clone(), body["variables"].clone()));
                axum::Json(match op.as_str() {
                    "LabelledIssues" => json!({"data": {"issues": {"nodes": *f.issues.lock().unwrap()}}}),
                    "TeamStates" => json!({"data": {"teams": {"nodes": [{"id": "team", "states": {"nodes": [
                        {"id": "st-progress", "name": "In Progress"}, {"id": "st-review", "name": "In Review"}]}}]}}}),
                    "Viewer" => json!({"data": {"viewer": {"id": "app-user-1"}}}),
                    _ => json!({"data": {"ok": {"success": true}}}),
                })
            }))
            .route("/oauth/token", axum::routing::post(|axum::extract::State(f): axum::extract::State<FakeLinear>, body: String| async move {
                let n = { let mut c = f.calls.lock().unwrap(); c.push(("token".into(), json!(body))); c.len() };
                axum::Json(json!({"access_token": format!("acc-{n}"), "refresh_token": format!("ref-{n}"), "expires_in": 86399}))
            }))
            .with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        fake.base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        fake
    }

    fn ops(&self) -> Vec<(String, Value)> {
        self.calls.lock().unwrap().clone()
    }
}

async fn setup(app: bool) -> (tempfile::TempDir, FakeSlack, FakeLinear, Arc<Shared>) {
    let (dir, slack, fake) = (tempfile::tempdir().unwrap(), FakeSlack::start().await, FakeLinear::start().await);
    let mut s = Arc::into_inner(shared(dir.path(), &slack).await).unwrap();
    let app = app.then(|| LinearApp::new("client-id", "client-secret", "https://agent.example/linear/oauth/callback",
                                          dir.path().join("data/runner/linear.env"), &fake.base));
    s.linear = Some(Linear { client: LinearClient::new("lin-key", &format!("{}/graphql", fake.base)), app,
                             webhook_secret: "hook-secret".into() });
    (dir, slack, fake, Arc::new(s))
}

fn session(action: &str, sid: &str) -> Value {
    json!({"type": "AgentSessionEvent", "action": action, "createdAt": "2026-11-23T01:02:03.000Z",
           "agentSession": {"id": sid, "createdAt": "2026-11-23T01:02:03.000Z", "creator": {"name": "Robin", "email": "robin@example.com"},
                            "issue": {"id": "iss-1", "identifier": "SAM-7", "title": "Broken login", "url": "https://linear.app/x/issue/SAM-7"}},
           "agentActivity": {"user": {"name": "Sam", "email": "sam@example.com"}, "content": {"body": "also check the logs"}},
           "webhookTimestamp": chrono::Utc::now().timestamp_millis()})
}

fn turns(s: &Shared) -> Vec<(String, Value)> {
    s.db.lock().prepare("SELECT conversation_id, payload FROM work_items ORDER BY created_at, rowid").unwrap()
        .query_map([], |r| Ok((r.get::<_, String>(0)?, serde_json::from_str(&r.get::<_, String>(1)?).unwrap()))).unwrap()
        .map(Result::unwrap).collect()
}

#[tokio::test]
async fn session_created_acks_and_queues() {
    let (_d, _slack, fake, s) = setup(false).await;
    linear::on_event(&s, &session("created", "sess-1")).await.unwrap();
    assert_eq!(fake.ops()[0], ("AgentActivityCreate".to_string(),
               json!({"input": {"agentSessionId": "sess-1", "content": {"type": "thought", "body": "On it."}}})));
    let turns = turns(&s);
    assert_eq!(turns.len(), 1);
    let p = &turns[0].1;
    assert!(p["text"].as_str().unwrap().starts_with("Linear agent session event:\n{"));
    assert_eq!((p["origin"].as_str(), p["source"].as_str(), p["requester"].as_str(), p["unattended"].as_bool()),
               (Some("[Linear SAM-7 · https://linear.app/x/issue/SAM-7]"), Some("linear"), Some("Robin"), Some(true)));
    let handed: String = s.db.lock().query_row("SELECT handed_at FROM linear_issues WHERE issue_id = 'iss-1'", [], |r| r.get(0)).unwrap();
    assert_eq!(handed, "2026-11-23T01:02:03Z");
}

#[tokio::test]
async fn prompted_follows_same_conversation() {
    let (_d, _slack, _fake, s) = setup(false).await;
    linear::on_event(&s, &session("created", "sess-1")).await.unwrap();
    linear::on_event(&s, &session("created", "sess-1")).await.unwrap(); // redelivered
    linear::on_event(&s, &session("prompted", "sess-1")).await.unwrap();
    linear::on_event(&s, &session("prompted", "sess-unknown")).await.unwrap();
    let turns = turns(&s);
    assert_eq!(turns.len(), 2);
    assert_eq!(turns[0].0, turns[1].0, "the same conversation, so the same Claude session");
    assert_eq!(turns[1].1["requester"], "sam", "who prompted it, by person key when on the team");
}

#[tokio::test]
async fn webhook_refuses_bad_signature_and_stale() {
    let (_d, _slack, _fake, s) = setup(false).await;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/linear/webhook", listener.local_addr().unwrap());
    let app = linear::public_app(s.clone());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let send = |body: Value, secret: &str| {
        let raw = body.to_string();
        let mut mac = hmac::Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes()).unwrap();
        mac.update(raw.as_bytes());
        let sig: String = mac.finalize().into_bytes().iter().map(|b| format!("{b:02x}")).collect();
        let url = url.clone();
        async move { reqwest::Client::new().post(url).header("Linear-Signature", sig).body(raw).send().await.unwrap().status().as_u16() }
    };
    assert_eq!(send(session("created", "sess-1"), "wrong").await, 401);
    let mut stale = session("created", "sess-1");
    stale["webhookTimestamp"] = json!(chrono::Utc::now().timestamp_millis() - 61_000);
    assert_eq!(send(stale, "hook-secret").await, 401);
    assert_eq!(send(session("created", "sess-1"), "hook-secret").await, 200);
    for _ in 0..50 {
        if !turns(&s).is_empty() { break }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    assert_eq!(turns(&s).len(), 1, "handled after the answer");
    let other = reqwest::Client::new().get(url.replace("/linear/webhook", "/anything")).send().await.unwrap();
    assert_eq!(other.status().as_u16(), 404);
}

#[tokio::test]
async fn oauth_state_single_use_and_exchange() {
    let (dir, slack, fake, s) = setup(true).await;
    let app = s.linear.as_ref().unwrap().app.as_ref().unwrap();
    let url = app.new_authorize_url(&s).unwrap();
    assert!(url.starts_with("https://linear.app/oauth/authorize?") && url.contains("actor=app") && url.contains("client_id=client-id"));
    let state = url.split("state=").nth(1).unwrap().split('&').next().unwrap().to_string();
    assert!(!app.check_state(&s, "forged").unwrap());
    agentd::db::set_setting(&s.db.lock(), linear::INSTALL_DM_KEY, "D-admin").unwrap();
    let page = linear::callback(&s, Some("the-code"), Some(&state), None).await;
    assert_eq!(page.0, 200);
    assert_eq!(linear::callback(&s, Some("the-code"), Some(&state), None).await.0, 400, "a state works once");
    assert_eq!(std::fs::read_to_string(dir.path().join("data/runner/linear.env")).unwrap(), "LINEAR_APP_TOKEN=acc-1\n");
    assert!(fake.ops()[0].1.as_str().unwrap().contains("grant_type=authorization_code"));
    assert_eq!(slack.sent("chat.postMessage")[0]["text"], "I'm installed in Linear: delegate issues to me there.");
    assert_eq!(app.token(&s).await.unwrap().as_deref(), Some("acc-1"), "fresh: no refresh");
}

#[tokio::test]
async fn token_refreshed_ahead_and_written() {
    let (dir, _slack, fake, s) = setup(true).await;
    let app = s.linear.as_ref().unwrap().app.as_ref().unwrap();
    let soon = chrono::Utc::now().timestamp() as f64 + 1800.0;
    agentd::db::set_setting(&s.db.lock(), "linear_app_tokens",
                            &json!({"access": "old", "refresh": "r0", "expires_at": soon, "app_user_id": "u"}).to_string()).unwrap();
    assert_eq!(app.token(&s).await.unwrap().as_deref(), Some("acc-1"));
    assert!(fake.ops()[0].1.as_str().unwrap().contains("refresh_token=r0"));
    assert_eq!(std::fs::read_to_string(dir.path().join("data/runner/linear.env")).unwrap(), "LINEAR_APP_TOKEN=acc-1\n");
    let saved: Value = serde_json::from_str(&agentd::db::get_setting(&s.db.lock(), "linear_app_tokens").unwrap().unwrap()).unwrap();
    assert_eq!((saved["refresh"].as_str(), saved["app_user_id"].as_str()), (Some("ref-1"), Some("u")));
}

#[tokio::test]
async fn labelled_issue_becomes_one_task() {
    let (_d, slack, fake, s) = setup(false).await;
    *fake.issues.lock().unwrap() = vec![json!({"id": "iss-9", "identifier": "SAM-9", "title": "Add export", "description": "CSV please",
        "url": "https://linear.app/x/issue/SAM-9", "creator": {"email": "alice@example.com"}, "labels": {"nodes": [{"name": "jensen"}, {"name": "Bug"}]}})];
    assert_eq!(linear::intake(&s).await.unwrap(), ["T-0001"]);
    assert!(linear::intake(&s).await.unwrap().is_empty(), "one task per issue");
    let task = agentd::tasks::get(&s.db.lock(), "T-0001").unwrap().unwrap();
    assert_eq!((task.kind.as_str(), task.requester.as_deref(), task.status.as_str(), task.description.as_str()),
               ("bug", Some("alice"), "working", "CSV please\n\nLinear: https://linear.app/x/issue/SAM-9"));
    let posts = slack.sent("chat.postMessage");
    assert_eq!((posts[0]["channel"].as_str(), posts[0]["text"].as_str()), (Some("#jensen"), Some("Linear SAM-9: Add export")));
    let turns = turns(&s);
    assert_eq!(turns[0].1["origin"], "[Linear SAM-9 · https://linear.app/x/issue/SAM-9]");
    assert_eq!(turns[0].1["text"], "Linear issue SAM-9: Add export\n\nCSV please\n\nLinear: https://linear.app/x/issue/SAM-9");
    let update = fake.ops().into_iter().find(|(op, _)| op == "IssueUpdate").unwrap();
    assert_eq!(update.1, json!({"id": "iss-9", "stateId": "st-progress"}), "the issue moves with the task");
}

#[test]
fn pure_rules_match_python() {
    let e: Value = serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap();
    let links: Vec<Option<String>> = ["body\n\nLinear: https://linear.app/x/issue/SAM-7", "no link", "Linear: https://a\nmore"]
        .iter().map(|d| linear::issue_link(d)).collect();
    assert_eq!(json!(links), e["issue_link"]);
    let config = agentd::config::load(&std::path::Path::new(REPO).join("examples/config/agent.toml")).unwrap();
    let who: Vec<Option<String>> = [json!({"action": "prompted", "agentActivity": {"user": {"email": "nobody@example.com", "name": "Pat"}}}),
                                    json!({"action": "created", "agentSession": {"creator": {"name": "Robin"}}}), json!({"action": "created"})]
        .iter().map(|b| linear::requester(b, &config)).collect();
    assert_eq!(json!(who), e["linear_requester"]);
}
