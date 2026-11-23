//! agentd end to end: a Slack message to its reply, with the dispatcher, the turn runner, holders and the fake
//! Claude Code.

mod common;

use agentd::conversations as cv;
use common::*;
use serde_json::{Value, json};

fn dm(text: &str, ts: &str, thread: Option<&str>) -> Value {
    let mut e = json!({"type": "message", "channel": "D1", "channel_type": "im", "user": "U00000001", "text": text, "ts": ts});
    if let Some(t) = thread {
        e["thread_ts"] = json!(t);
    }
    e
}

fn status(rig: &Rig, id: &str) -> String {
    rig.s.db.lock().query_row("SELECT status FROM work_items WHERE id = ?", [id], |r| r.get(0)).unwrap()
}

fn item_ids(rig: &Rig) -> Vec<String> {
    rig.s.db.lock().prepare("SELECT id FROM work_items ORDER BY created_at, rowid").unwrap().query_map([], |r| r.get(0)).unwrap()
        .map(Result::unwrap).collect()
}

fn runs_file(rig: &Rig, id: &str) -> String {
    std::fs::read_to_string(rig.s.paths.data.join("runs").join(format!("{id}.jsonl"))).unwrap_or_default()
}

#[tokio::test(flavor = "multi_thread")]
async fn dm_to_reply_end_to_end() {
    let rig = Rig::new(&[vec![init(), read_prompt(), say("pong"), result("pong")]]).await;
    tokio::spawn(rig.dispatcher().run());
    cv::handle_slack(&rig.s, dm("ping", "1.0", None)).await.unwrap();
    rig.until("the reply", |r| r.posted().contains(&"pong".to_string())).await;
    let reply = rig.slack.sent("chat.postMessage").into_iter().find(|p| p["text"] == "pong").unwrap();
    assert_eq!(reply["thread_ts"], "1.0");
    rig.until("the tick", |r| r.slack.reactions().last().map(String::as_str) == Some("+white_check_mark")).await;
    let id = item_ids(&rig)[0].clone();
    assert_eq!(status(&rig, &id), "done");
    let (outcome, session): (String, Option<String>) = rig.s.db.lock()
        .query_row("SELECT outcome, session_id FROM runs WHERE id = ?", [&id], |r| Ok((r.get(0)?, r.get(1)?))).unwrap();
    assert_eq!(outcome, "done");
    let convo_id: String = rig.s.db.lock().query_row("SELECT conversation_id FROM work_items WHERE id = ?", [&id], |r| r.get(0)).unwrap();
    let convo = cv::row(&rig.s.db.lock(), &convo_id).unwrap().unwrap();
    assert_eq!(convo.session_id, session, "the next turn resumes it");
    let events = runs_file(&rig, &id);
    assert!(events.contains("\"AssistantMessage\"") && events.contains("\"_ts\""), "{events}");
    let started: i64 = rig.s.db.lock().query_row("SELECT COUNT(*) FROM events WHERE type = 'session_started'", [], |r| r.get(0)).unwrap();
    assert_eq!(started, 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn message_during_turn_delivered_and_ticked() {
    let rig = Rig::new(&[vec![init(), read_prompt(), say("working"), json!({"wait_stdin": 3}), say("both"), result("both answered")]]).await;
    tokio::spawn(rig.dispatcher().run());
    cv::handle_slack(&rig.s, dm("first", "1.0", None)).await.unwrap();
    let first = item_ids(&rig)[0].clone();
    rig.until("the turn to start", |r| runs_file(r, &first).contains("working")).await;
    cv::handle_slack(&rig.s, dm("second", "2.0", Some("1.0"))).await.unwrap();
    rig.until("the reply", |r| r.posted().contains(&"both answered".to_string())).await;
    let second = item_ids(&rig)[1].clone();
    rig.until("the second message read", |r| status(r, &second) == "done").await;
    assert_eq!(rig.starts(), 1, "read in the same turn");
    let ticks: Vec<String> = rig.slack.calls.lock().unwrap().iter()
        .filter(|(m, b)| m.starts_with("reactions.") && b["timestamp"] == "2.0")
        .map(|(m, b)| format!("{}{}", if m == "reactions.add" { "+" } else { "-" }, b["name"].as_str().unwrap())).collect();
    assert_eq!(ticks, ["+eyes", "-eyes", "+hourglass_flowing_sand", "-hourglass_flowing_sand", "+white_check_mark"]);
    let result: Value = serde_json::from_str(&rig.s.db.lock().query_row("SELECT result FROM work_items WHERE id = ?", [&first],
                                                                        |r| r.get::<_, String>(0)).unwrap()).unwrap();
    assert_eq!(result["read"], json!([second]));
}

#[tokio::test(flavor = "multi_thread")]
async fn message_during_turn_end_not_lost() {
    let rig = Rig::new(&[]).await;
    let dispatcher = rig.dispatcher();
    let convo = cv::create_conversation(&rig.s.db.lock(), Some("D1"), Some("1.0"), None, None, true, None).unwrap();
    let row = cv::row(&rig.s.db.lock(), &convo).unwrap().unwrap();
    let ending = cv::enqueue_turn(&rig.s, &row, "first", Some("sam"), Default::default()).unwrap();
    rig.s.db.lock().execute("UPDATE work_items SET status = 'claimed' WHERE id = ?", [&ending]).unwrap();
    let late = cv::enqueue_turn(&rig.s, &row, "second", Some("sam"), Default::default()).unwrap();
    // the turn is ending: its Claude Code no longer takes messages
    dispatcher.track(&ending, &convo);
    dispatcher.deliver_waiting().await;
    assert_eq!(status(&rig, &late), "queued", "it runs as the next turn");
}

#[tokio::test(flavor = "multi_thread")]
async fn stop_all_interrupts_running() {
    let rig = Rig::new(&[vec![init(), read_prompt(), say("busy"), json!({"wait_interrupt": true}),
                              json!({"emit": {"type": "result", "subtype": "error_during_execution", "is_error": true, "duration_ms": 1,
                                              "duration_api_ms": 1, "num_turns": 0}})]]).await;
    tokio::spawn(rig.dispatcher().run());
    cv::handle_slack(&rig.s, dm("long job", "1.0", None)).await.unwrap();
    let id = item_ids(&rig)[0].clone();
    rig.until("the turn to start", |r| runs_file(r, &id).contains("busy")).await;
    let ctx = agentd::tools::context_for(&rig.s, &id).unwrap();
    agentd::tools::call(&rig.s, &ctx, "stop_all", &json!({})).await;
    rig.until("the run recorded", |r| r.s.db.lock().query_row("SELECT outcome FROM runs WHERE id = ?", [&id], |x| x.get::<_, String>(0)).is_ok()).await;
    let outcome: String = rig.s.db.lock().query_row("SELECT outcome FROM runs WHERE id = ?", [&id], |r| r.get(0)).unwrap();
    assert_eq!((outcome.as_str(), status(&rig, &id).as_str()), ("interrupted", "cancelled"));
    assert_eq!(rig.posted(), ["Stopped by sam. I'll pick this up again when an admin resumes."], "nothing else is posted");
}

#[tokio::test(flavor = "multi_thread")]
async fn restart_reattaches_and_reports_once() {
    let rig = Rig::new(&[vec![init(), read_prompt(), say("before"), json!({"sleep_ms": 1500}), say("after"), result("after")]]).await;
    let first = tokio::spawn(rig.dispatcher().run());
    cv::handle_slack(&rig.s, dm("go", "1.0", None)).await.unwrap();
    let id = item_ids(&rig)[0].clone();
    rig.until("the turn to start", |r| runs_file(r, &id).contains("before")).await;
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    first.abort();
    let _ = first.await;
    tokio::spawn(rig.dispatcher().run());
    rig.until("the reply", |r| r.posted().contains(&"after".to_string())).await;
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(rig.posted().iter().filter(|t| *t == "after").count(), 1);
    assert_eq!((rig.starts(), status(&rig, &id).as_str()), (1, "done"));
    assert_eq!(runs_file(&rig, &id).matches("\"before\"").count(), 1, "nothing forwarded twice");
}
