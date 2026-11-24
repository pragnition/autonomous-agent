//! The dashboard's HTTP API over the seeded database (tests/fixtures/dashboard), a fake proxy and a fake Slack: who
//! may use it, the pages' reads, the live stream, the kill switch and the cap changes, and the web app's files.

mod common;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use agentd::dashboard;
use common::*;
use serde_json::{Value, json};

struct Rig {
    _dir: tempfile::TempDir,
    _slack: FakeSlack,
    s: Arc<agentd::shared::Shared>,
    url: String,
}

async fn rig(trusted: &str, static_dir: Option<std::path::PathBuf>, upstreams: bool) -> Rig {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.config.dashboard.trusted_peers = vec![trusted.to_string()];
    s.config.dashboard.agent_origins = vec!["agent-sandbox".into()];
    if upstreams {
        s.config.llm_meter.upstreams = [("foundry".to_string(), agentd::config::LlmUpstream {
            base_url: "https://x.openai.azure.com/openai/v1".into(), key: "K".into(), header: "api-key".into() })].into();
    }
    seed(&s.db.lock());
    let s = Arc::new(s);
    let runs = s.paths.data.join("runs");
    std::fs::create_dir_all(&runs).unwrap();
    std::fs::write(runs.join("W-1.jsonl"), concat!(
        "{\"type\": \"SystemMessage\", \"subtype\": \"init\", \"data\": {\"session_id\": \"sid-1\"}}\n",
        "{\"type\": \"UserMessage\", \"content\": \"hi there\", \"_ts\": \"2026-11-15T10:06:00Z\"}\n",
        "{\"type\": \"ResultMessage\", \"subtype\": \"success\", \"num_turns\": 3, \"result\": \"hello core-secret-0123456789\",",
        " \"usage\": {\"input_tokens\": 1}}\n")).unwrap();
    std::fs::write(runs.join("W-3.jsonl"), concat!(
        "{\"type\": \"SystemMessage\", \"subtype\": \"init\", \"data\": {\"session_id\": \"sid-3\"}}\n",
        "{\"type\": \"AssistantMessage\", \"message_id\": \"m1\", \"usage\": {\"output_tokens\": 9}, \"content\": [{\"text\": \"working\"}]}\n"))
        .unwrap();
    let app = dashboard::app(s.clone(), dashboard::Options { static_dir, poll: std::time::Duration::from_millis(50),
                                                            host: Arc::new(Mutex::new(agentd::host::HostStore::default())),
                                                            rates: None });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await });
    Rig { _dir: dir, _slack: slack, s, url }
}

fn as_person(req: reqwest::RequestBuilder, login: &str) -> reqwest::RequestBuilder {
    req.header("Tailscale-User-Login", login).header("Tailscale-User-Name", "Sam Example")
}

async fn get(r: &Rig, path: &str) -> (u16, Value) {
    let resp = as_person(reqwest::Client::new().get(format!("{}{path}", r.url)), "sam@example.com").send().await.unwrap();
    (resp.status().as_u16(), resp.json().await.unwrap_or(Value::Null))
}

async fn post(r: &Rig, path: &str, login: &str, body: Value) -> (u16, Value) {
    let resp = as_person(reqwest::Client::new().post(format!("{}{path}", r.url)), login).header("X-Dashboard", "1")
        .json(&body).send().await.unwrap();
    (resp.status().as_u16(), resp.json().await.unwrap_or(Value::Null))
}

#[tokio::test]
async fn access_rules() {
    let outside = rig("10.9.9.9", None, false).await;
    let resp = as_person(reqwest::Client::new().get(format!("{}/api/me", outside.url)), "sam@example.com").send().await.unwrap();
    assert_eq!(resp.status(), 403);
    assert_eq!(resp.json::<Value>().await.unwrap()["error"], "not from the tailnet");

    let r = rig("127.0.0.1", None, false).await;
    let anonymous = reqwest::get(format!("{}/api/me", r.url)).await.unwrap();
    assert_eq!((anonymous.status().as_u16(), anonymous.json::<Value>().await.unwrap()["error"].clone()),
               (403, json!("no Tailscale identity")));
    let (status, me) = get(&r, "/api/me").await;
    assert_eq!(status, 200);
    assert_eq!(me, json!({"login": "sam@example.com", "name": "Sam Example", "person": "sam", "role": "admin",
                          "can_resume": true, "agent": "jensen"}));
    let no_header = as_person(reqwest::Client::new().post(format!("{}/api/stop", r.url)), "sam@example.com").send().await.unwrap();
    assert_eq!(no_header.json::<Value>().await.unwrap()["error"], "missing X-Dashboard header");
    let cross = as_person(reqwest::Client::new().post(format!("{}/api/stop", r.url)), "sam@example.com")
        .header("X-Dashboard", "1").header("Origin", "https://evil.example").send().await.unwrap();
    assert_eq!(cross.json::<Value>().await.unwrap()["error"], "cross-site request");
    let host = r.url.trim_start_matches("http://").to_string();
    let same = as_person(reqwest::Client::new().post(format!("{}/api/resume", r.url)), "sam@example.com")
        .header("X-Dashboard", "1").header("Origin", format!("http://{host}")).send().await.unwrap();
    assert_eq!(same.status(), 200);
    let (status, bad) = get(&r, "/api/conversations?limit=abc").await;
    assert_eq!((status, bad), (400, json!({"error": "limit must be a whole number"})));
    let (status, bad) = get(&r, "/api/kpis?days=400").await;
    assert_eq!((status, bad), (400, json!({"error": "days must be from 1 to 365"})));
}

#[tokio::test]
async fn overview_conversations_and_sessions() {
    let r = rig("127.0.0.1", None, false).await;
    let (_, o) = get(&r, "/api/overview").await;
    assert_eq!(o["paused"], true);
    assert_eq!(o["paused_by"]["actor"], "tailscale:pat@example.com");
    let running: Vec<&str> = o["running"].as_array().unwrap().iter().map(|x| x["id"].as_str().unwrap()).collect();
    assert_eq!(running, ["W-3", "W-6"]);
    let w3 = &o["running"][0];
    assert_eq!((&w3["live"], &w3["account"], &w3["tokens"]["out"], &w3["turns"]),
               (&json!(true), &json!("acct-a"), &json!(9), &json!(1)), "live numbers from the transcript and the proxy");
    assert_eq!(w3["recent"][0]["text"], "working");
    assert!(w3.get("_channel").is_none(), "the API's own keys stay in");
    assert_eq!(o["queued"][0]["id"], "W-4");
    assert!(o["queued"][0].get("recent").is_none());
    assert_eq!(o["counts"]["running"], 1);

    let (_, page) = get(&r, "/api/conversations?limit=2&state=done").await;
    assert_eq!(page["total"], 2);
    assert_eq!(page["rows"].as_array().unwrap().len(), 2);
    let (status, convo) = get(&r, "/api/conversations/W-6").await;
    assert_eq!((status, &convo["id"], &convo["focus"]), (200, &json!("C-thr"), &json!("W-6")));
    assert_eq!(convo["items"].as_array().unwrap().len(), 2);
    assert_eq!(get(&r, "/api/conversations/nope").await, (404, json!({"error": "no such conversation"})));

    let (_, session) = get(&r, "/api/sessions/W-1").await;
    let kinds: Vec<&str> = session["entries"].as_array().unwrap().iter().map(|e| e["kind"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["text", "result"]);
    assert_eq!(session["entries"][1]["text"], "hello [secret]", "secrets never reach the page");
    assert_eq!((&session["offset"], &session["line"]), (&json!(std::fs::metadata(r.s.paths.data.join("runs/W-1.jsonl")).unwrap().len()), &json!(3)));
    let (_, header) = get(&r, "/api/sessions/W-1?entries=0").await;
    assert!(header.get("entries").is_none());
    assert_eq!(get(&r, "/api/sessions/nope").await.0, 404);

    let (_, events) = get(&r, "/api/events?type=stop_all&limit=3").await;
    assert_eq!(events["rows"].as_array().unwrap().len(), 2);
    assert_eq!((&events["rows"][0]["source"], &events["rows"][0]["sender"]), (&json!("dashboard"), &json!("pat@example.com")),
               "a dashboard user by their login");
    let (_, kpis) = get(&r, "/api/kpis?days=7").await;
    assert!(kpis["kpis"]["tasks"].is_number());
}

async fn stream(r: &Rig, path: &str) -> String {
    let resp = as_person(reqwest::Client::new().get(format!("{}{path}", r.url)), "sam@example.com").send().await.unwrap();
    assert_eq!(resp.headers()["content-type"], "text/event-stream");
    resp.text().await.unwrap()
}

#[tokio::test]
async fn stream_sends_entries_then_end() {
    let r = rig("127.0.0.1", None, false).await;
    let done = stream(&r, "/api/sessions/W-1/stream").await;
    let events: Vec<&str> = done.split("\n\n").filter(|e| !e.is_empty()).collect();
    assert_eq!(events.len(), 2, "{done}");
    assert!(events[0].starts_with("event: entries\ndata: "));
    assert_eq!(events[1], "event: end\ndata: {\"state\":\"done\"}");
    let resumed = stream(&r, "/api/sessions/W-1/stream?offset=0&line=0").await;
    assert!(resumed.contains("\"line\":3"));

    let s = r.s.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        let runs = s.paths.data.join("runs/W-3.jsonl");
        let mut text = std::fs::read_to_string(&runs).unwrap();
        text.push_str("{\"type\": \"UserMessage\", \"content\": \"later\"}\n");
        std::fs::write(&runs, text).unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        s.db.lock().execute("UPDATE work_items SET status = 'done' WHERE id = 'W-3'", []).unwrap();
    });
    let live = stream(&r, "/api/sessions/W-3/stream").await;
    let kinds: Vec<&str> = live.split("\n\n").filter(|e| !e.is_empty()).map(|e| e.lines().next().unwrap()).collect();
    assert_eq!(kinds, ["event: entries", "event: entries", "event: end"], "{live}");
    assert!(live.contains("later"));
    assert_eq!(get(&r, "/api/sessions/nope/stream").await.0, 404);
}

#[tokio::test]
async fn cancel_stop_resume() {
    let r = rig("127.0.0.1", None, false).await;
    assert_eq!(post(&r, "/api/sessions/W-4/cancel", "sam@example.com", json!({})).await,
               (200, json!({"ok": true, "state": "cancelled"})));
    assert_eq!(post(&r, "/api/sessions/W-1/cancel", "sam@example.com", json!({})).await,
               (409, json!({"error": "already finished", "state": "done"})));
    let via: String = r.s.db.lock().query_row("SELECT data FROM events WHERE type = 'session_cancelled' ORDER BY id DESC LIMIT 1",
                                             [], |x| x.get(0)).unwrap();
    assert_eq!(via, r#"{"work_item": "W-4", "via": "dashboard"}"#);
    assert_eq!(post(&r, "/api/resume", "someone@example.com", json!({})).await,
               (403, json!({"error": "only an admin can resume"})));
    assert_eq!(post(&r, "/api/stop", "someone@example.com", json!({})).await, (200, json!({"ok": true, "cancelled": ["W-3"]})));
    let actor: String = r.s.db.lock().query_row("SELECT actor FROM events WHERE type = 'stop_all' ORDER BY id DESC LIMIT 1",
                                               [], |x| x.get(0)).unwrap();
    assert_eq!(actor, "tailscale:someone@example.com", "anyone on the dashboard may stop; only an admin resumes");
    assert_eq!(post(&r, "/api/resume", "sam@example.com", json!({})).await, (200, json!({"ok": true})));
    assert_eq!(agentd::db::get_setting(&r.s.db.lock(), "paused").unwrap().as_deref(), Some("0"));
}

#[tokio::test]
async fn accounts_history_and_meter() {
    let r = rig("127.0.0.1", None, true).await;
    let (_, a) = get(&r, "/api/accounts").await;
    assert_eq!(a["available"], true);
    assert_eq!(a["accounts"][0]["code"], "acct-b");
    let (_, h) = get(&r, "/api/accounts/history?hours=6").await;
    assert_eq!((&h["hours"], &h["bucket_s"]), (&json!(6), &json!(300)));
    assert_eq!(get(&r, "/api/accounts/history?hours=5").await, (400, json!({"error": "hours must be one of 6, 24, 168, 720"})));

    let (_, llm) = get(&r, "/api/llm").await;
    assert_eq!((&llm["installed"], &llm["caps"]["daily_usd"]), (&json!(true), &json!(10.0)));
    assert_eq!(post(&r, "/api/llm/caps", "someone@example.com", json!({"daily_usd": 5})).await,
               (403, json!({"error": "only an admin can change the caps"})));
    assert_eq!(post(&r, "/api/llm/caps", "sam@example.com", json!({})).await,
               (400, json!({"error": "give daily_usd, monthly_usd or both"})));
    assert_eq!(post(&r, "/api/llm/caps", "sam@example.com", json!({"daily_usd": true})).await,
               (400, json!({"error": "daily_usd must be a number of US dollars"})));
    let (status, changed) = post(&r, "/api/llm/caps", "sam@example.com", json!({"daily_usd": 5, "monthly_usd": null})).await;
    assert_eq!((status, &changed["caps"]["daily_usd"], &changed["caps"]["changed_at_runtime"]), (200, &json!(5.0), &json!(true)));
    let none = rig("127.0.0.1", None, false).await;
    assert_eq!(get(&none, "/api/llm").await, (200, json!({"installed": false})));

    let (_, host) = get(&r, "/api/host").await;
    assert_eq!((&host["available"], &host["stale"]), (&json!(false), &json!(true)));
    let (_, consumers) = get(&r, "/api/host/consumers").await;
    assert_eq!(consumers["consumers"], json!([]));
}

#[tokio::test]
async fn web_app_files_and_fallback() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("assets")).unwrap();
    std::fs::write(dir.path().join("index.html"), "<html>app</html>").unwrap();
    std::fs::write(dir.path().join("assets/app-1.js"), "console.log(1)").unwrap();
    let r = rig("127.0.0.1", Some(dir.path().to_path_buf()), false).await;
    let fetch = |path: &str| reqwest::Client::new().get(format!("{}{path}", r.url)).send();
    let asset = fetch("/assets/app-1.js").await.unwrap();
    assert_eq!(asset.headers()["cache-control"], "public, max-age=31536000, immutable");
    assert_eq!(asset.headers()["content-type"], "text/javascript; charset=utf-8");
    for path in ["/", "/sessions/W-1", "/assets/missing.js", "/..%2f..%2fetc%2fpasswd"] {
        let page = fetch(path).await.unwrap();
        assert_eq!(page.headers()["cache-control"], "no-cache", "{path}");
        assert_eq!(page.text().await.unwrap(), "<html>app</html>", "{path}");
    }
    let api = as_person(reqwest::Client::new().get(format!("{}/api/nope", r.url)), "sam@example.com").send().await.unwrap();
    assert_eq!((api.status().as_u16(), api.json::<Value>().await.unwrap()), (404, json!({"error": "no such route"})));
    let unbuilt = rig("127.0.0.1", None, false).await;
    let text = reqwest::get(format!("{}/", unbuilt.url)).await.unwrap().text().await.unwrap();
    assert!(text.starts_with("The dashboard's web app isn't built into this image"), "{text}");
}
