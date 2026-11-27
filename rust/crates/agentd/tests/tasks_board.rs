//! The Tasks page end to end: what a session says with post_update and ask, as the dashboard's /api/tasks shows it,
//! and how a reply in the thread or a dismiss on the page closes what needed someone. Against a fake Slack.

mod common;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use agentd::conversations as cv;
use agentd::shared::Shared;
use agentd::tools;
use common::*;
use serde_json::{Value, json};

struct Rig {
    _dir: tempfile::TempDir,
    slack: FakeSlack,
    s: Arc<Shared>,
    url: String,
}

async fn rig() -> Rig {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.config.dashboard.trusted_peers = vec!["127.0.0.1".into()];
    let s = Arc::new(s);
    let app = agentd::dashboard::app(s.clone(), agentd::dashboard::Options {
        static_dir: None, poll: std::time::Duration::from_millis(50),
        host: Arc::new(Mutex::new(agentd::host::HostStore::default())), rates: None, workloads: None });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await });
    Rig { _dir: dir, slack, s, url }
}

/// A turn running for sam in a new conversation in `channel`'s thread `thread_ts` (none: outside Slack): its work item.
fn turn(s: &Shared, channel: Option<&str>, thread_ts: Option<&str>) -> String {
    let c = s.db.lock();
    let convo = cv::create_conversation(&c, channel, thread_ts, None, None, true, None).unwrap();
    let row = cv::row(&c, &convo).unwrap().unwrap();
    drop(c);
    let mut payload = agentd::payloads::turn(s, &row, "fix the shift swap", Some("sam"), false).unwrap();
    payload["source"] = json!("slack");
    payload["requester"] = json!("sam");
    let c = s.db.lock();
    let item = agentd::queue::enqueue(&c, Some(&convo), "turn", &payload).unwrap();
    c.execute("UPDATE work_items SET status = 'claimed' WHERE id = ?", [&item]).unwrap();
    item
}

async fn call(s: &Shared, item: &str, name: &str, args: Value) -> String {
    tools::call(s, &tools::context_for(s, item).unwrap(), name, &args).await
}

async fn board(r: &Rig) -> Value {
    let resp = reqwest::Client::new().get(format!("{}/api/tasks", r.url))
        .header("Tailscale-User-Login", "sam@example.com").send().await.unwrap();
    assert_eq!(resp.status(), 200);
    resp.json().await.unwrap()
}

async fn dismiss(r: &Rig, id: &Value) -> (u16, Value) {
    let resp = reqwest::Client::new().post(format!("{}/api/tasks/attention/{id}/dismiss", r.url))
        .header("Tailscale-User-Login", "sam@example.com").header("X-Dashboard", "1").send().await.unwrap();
    (resp.status().as_u16(), resp.json().await.unwrap_or(Value::Null))
}

/// The only task on the board.
fn only(b: &Value) -> &Value {
    assert_eq!(b["channels"].as_array().unwrap().len(), 1, "{b}");
    &b["channels"][0]["tasks"][0]
}

async fn reply_in_thread(s: &Shared, channel: &str, thread_ts: &str) {
    let msg = json!({"type": "message", "channel": channel, "user": "U00000001", "text": "go with hybrid", "ts": "70.0",
                     "thread_ts": thread_ts});
    cv::handle_slack(s, msg).await.unwrap();
}

#[tokio::test]
async fn an_update_shows_its_text_and_checklist_under_its_channel() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    let steps = json!([{"content": "Reproduce the bug", "status": "completed"}, {"content": "Fix it", "status": "in_progress"}]);

    let reply = call(&r.s, &item, "post_update", json!({"text": "Found it", "steps": steps})).await;

    assert_eq!(reply, "posted");
    let b = board(&r).await;
    assert_eq!((&b["channels"][0]["key"], &b["channels"][0]["label"], &b["channels"][0]["kind"]),
               (&json!("C1"), &json!("general"), &json!("channel")));
    let task = only(&b);
    assert_eq!((&task["state"], &task["title"], &task["update"]["text"], &task["slack_url"]),
               (&json!("running"), &json!("fix the shift swap"), &json!("Found it"), &json!("https://slack.example/p50.0")));
    assert_eq!(task["steps"], steps);
}

#[tokio::test]
async fn a_later_checklist_replaces_the_earlier_one_and_an_update_without_one_keeps_it() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "post_update", json!({"text": "a", "steps": [{"content": "One", "status": "in_progress"}]})).await;
    call(&r.s, &item, "post_update", json!({"text": "b", "steps": [{"content": "One", "status": "completed"},
                                                                    {"content": "Two", "status": "pending"}]})).await;

    call(&r.s, &item, "post_update", json!({"text": "c"})).await;

    let task = only(&board(&r).await).clone();
    assert_eq!(task["update"]["text"], "c");
    assert_eq!(task["steps"], json!([{"content": "One", "status": "completed"}, {"content": "Two", "status": "pending"}]));
}

#[tokio::test]
async fn steps_that_are_not_a_checklist_are_refused_and_nothing_is_posted() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));

    let reply = call(&r.s, &item, "post_update", json!({"text": "x", "steps": [{"content": "One", "status": "done"}]})).await;

    assert_eq!(reply, "steps must be a list of {content, status}, status one of pending, in_progress, completed, blocked");
    assert!(r.slack.sent("chat.postMessage").is_empty());
    assert_eq!(only(&board(&r).await)["update"], Value::Null);
}

#[tokio::test]
async fn an_update_is_scrubbed_of_secrets_on_the_board() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));

    call(&r.s, &item, "post_update", json!({"text": "token core-secret-0123456789", "needs_attention": true})).await;

    let task = only(&board(&r).await).clone();
    assert_eq!((&task["update"]["text"], &task["attention"][0]["text"]),
               (&json!("token [redacted]"), &json!("token [redacted]")));
}

#[tokio::test]
async fn a_question_is_open_until_someone_replies_in_the_thread() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "ask", json!({"question": "Include phone numbers?"})).await;
    r.s.db.lock().execute("UPDATE work_items SET status = 'done' WHERE id = ?", [&item]).unwrap();
    let asked = only(&board(&r).await).clone();
    assert_eq!((&asked["state"], &asked["attention"][0]["kind"], &asked["attention"][0]["text"], &asked["attention"][0]["slack_url"]),
               (&json!("waiting"), &json!("question"), &json!("Include phone numbers?"), &json!("https://slack.example/p50.0")));

    reply_in_thread(&r.s, "C1", "50.0").await;

    assert_eq!(only(&board(&r).await)["attention"], json!([]));
}

#[tokio::test]
async fn a_flagged_update_stays_open_until_dismissed_and_links_to_its_message() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "post_update", json!({"text": "Please check the hosting choice", "needs_attention": true})).await;
    let flag = only(&board(&r).await)["attention"][0].clone();
    assert_eq!((&flag["kind"], &flag["slack_url"]), (&json!("look"), &json!("https://slack.example/p900.000001")));

    let (status, body) = dismiss(&r, &flag["id"]).await;

    assert_eq!((status, body), (200, json!({"ok": true, "closed": true})));
    assert_eq!(only(&board(&r).await)["attention"], json!([]));
}

#[tokio::test]
async fn dismissing_twice_changes_nothing_and_an_unknown_flag_is_not_found() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "post_update", json!({"text": "Look", "needs_attention": true})).await;
    let id = only(&board(&r).await)["attention"][0]["id"].clone();
    dismiss(&r, &id).await;

    assert_eq!(dismiss(&r, &id).await, (200, json!({"ok": true, "closed": false})));
    assert_eq!(dismiss(&r, &json!(999)).await, (404, json!({"error": "no such flag"})));
}

#[tokio::test]
async fn a_new_flag_after_a_dismiss_is_open_again() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "post_update", json!({"text": "First", "needs_attention": true})).await;
    dismiss(&r, &only(&board(&r).await)["attention"][0]["id"].clone()).await;

    call(&r.s, &item, "post_update", json!({"text": "Second", "needs_attention": true})).await;

    let open: Vec<Value> = only(&board(&r).await)["attention"].as_array().unwrap().iter().map(|a| a["text"].clone()).collect();
    assert_eq!(open, [json!("Second")]);
}

#[tokio::test]
async fn a_reply_in_the_thread_closes_a_flag() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    call(&r.s, &item, "post_update", json!({"text": "Please check", "needs_attention": true})).await;

    reply_in_thread(&r.s, "C1", "50.0").await;

    assert_eq!(only(&board(&r).await)["attention"], json!([]));
}

#[tokio::test]
async fn a_conversation_outside_slack_reports_to_the_board_only() {
    let r = rig().await;
    let item = turn(&r.s, None, None);

    let reply = call(&r.s, &item, "post_update", json!({"text": "Swept 3 repos"})).await;

    assert_eq!(reply, "recorded on the dashboard; this conversation has no Slack thread");
    let b = board(&r).await;
    assert_eq!((&b["channels"][0]["key"], &b["channels"][0]["label"]), (&json!("none"), &json!("Outside Slack")));
    assert_eq!((&only(&b)["update"]["text"], &only(&b)["slack_url"]), (&json!("Swept 3 repos"), &Value::Null));
}

#[tokio::test]
async fn direct_messages_share_one_tab_and_outside_slack_comes_last() {
    let r = rig().await;
    turn(&r.s, None, None);
    turn(&r.s, Some("D1"), Some("10.0"));
    turn(&r.s, Some("D2"), Some("20.0"));

    let b = board(&r).await;

    let tabs: Vec<(Value, usize)> = b["channels"].as_array().unwrap().iter()
        .map(|ch| (ch["key"].clone(), ch["tasks"].as_array().unwrap().len())).collect();
    assert_eq!(tabs, [(json!("dm"), 2), (json!("none"), 1)]);
}

#[tokio::test]
async fn a_finished_chat_is_done_but_a_finished_turn_of_an_open_task_is_idle() {
    let r = rig().await;
    turn(&r.s, Some("C1"), Some("50.0"));
    let tracked = turn(&r.s, Some("C1"), Some("60.0"));
    call(&r.s, &tracked, "track_task", json!({"project": "sample", "title": "Fix login", "kind": "bug"})).await;
    r.s.db.lock().execute("UPDATE work_items SET status = 'done'", []).unwrap();

    let b = board(&r).await;

    let states: Vec<(Value, Value)> = b["channels"][0]["tasks"].as_array().unwrap().iter()
        .map(|t| (t["title"].clone(), t["state"].clone())).collect();
    assert!(states.contains(&(json!("Fix login"), json!("idle"))), "{states:?}");
    assert!(states.contains(&(json!("fix the shift swap"), json!("done"))), "{states:?}");
}

#[tokio::test]
async fn a_finished_conversation_leaves_the_board_after_a_day() {
    let r = rig().await;
    let item = turn(&r.s, Some("C1"), Some("50.0"));
    let c = r.s.db.lock();
    c.execute("UPDATE work_items SET status = 'done' WHERE id = ?", [&item]).unwrap();
    c.execute("UPDATE conversations SET last_active_at = '2020-01-01T00:00:00Z'", []).unwrap();
    drop(c);

    assert_eq!(board(&r).await["channels"], json!([]));
}
