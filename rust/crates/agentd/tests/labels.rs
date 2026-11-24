//! The task record and its labels, against Python's whole labeling sweep over the same scenario with the same
//! stand-in for Haiku (tests/fixtures/dashboard/make_labels.py).

mod common;

use std::sync::{Arc, Mutex};

use agentd::labeler::{self, Labeler};
use agentd::task_record as tr;
use common::*;
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/labels.json").unwrap()).unwrap()
}

fn tables(c: &rusqlite::Connection) -> Value {
    let rows = |sql: &str| -> Vec<Value> {
        let mut stmt = c.prepare(sql).unwrap();
        stmt.query_map([], agentd::db::row_json).unwrap().map(Result::unwrap).collect()
    };
    let mut labels = rows("SELECT * FROM request_labels ORDER BY work_item_id");
    for r in &mut labels {
        r.as_object_mut().unwrap().shift_remove("labeled_at");
    }
    json!({"request_labels": labels, "task_records": rows("SELECT * FROM task_records ORDER BY id")})
}

/// The stand-in: answers by the request's text, records what it was asked, fails on "boom".
fn stand_in(answers: Arc<Mutex<Value>>, calls: Arc<Mutex<Vec<Value>>>) -> labeler::Complete {
    Arc::new(move |_system: String, user: String, _session: String| {
        let (answers, calls) = (answers.clone(), calls.clone());
        Box::pin(async move {
            let lines: Vec<&str> = user.split('\n').collect();
            let asked = lines[lines.iter().position(|l| l.starts_with("The request (")).unwrap() + 1].to_string();
            calls.lock().unwrap().push(json!({"asked": asked, "user": user}));
            if asked == "boom" {
                anyhow::bail!("stand-in failure");
            }
            Ok(answers.lock().unwrap()[&asked].as_str().unwrap().to_string())
        })
    })
}

#[tokio::test]
async fn task_record_matches_python() {
    let e = fixture();
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    seed_rows(&s.db.lock(), &e["seed"]);
    {
        let c = s.db.lock();
        let pending = tr::pending(&c).unwrap();
        assert_eq!(json!(pending.iter().map(|i| &i.id).collect::<Vec<_>>()), e["pending"]);
        for item in &pending {
            let (source, requester, follows) = tr::origin_of(&c, &s.config, item).unwrap();
            assert_eq!(json!([source, requester, follows]), e["origins"][&item.id], "{}", item.id);
            assert_eq!(json!(tr::project_hints(&c, item, &s.packs).unwrap()), e["hints"][&item.id], "{}", item.id);
        }
    }
    let (answers, calls) = (Arc::new(Mutex::new(e["answers"].clone())), Arc::new(Mutex::new(Vec::new())));
    // room, answered at once (Python's stand-in doesn't wait either, so groups run in the same order)
    let room: labeler::Capacity = Arc::new(|| Box::pin(async { true }));
    let labeler = Labeler::new(stand_in(answers.clone(), calls.clone()), room);
    let mut sweeps = Vec::new();
    for _ in 0..4 {
        sweeps.push(labeler.sweep(&s).await.unwrap());
    }
    assert_eq!(json!(sweeps), e["sweeps"]);
    assert_json(&json!(*calls.lock().unwrap()), &e["calls"], "what Haiku was asked");
    assert_json(&tables(&s.db.lock()), &e["after_sweeps"], "after the sweeps");
    let tasks = s.paths.data.join("tasks");
    let files: serde_json::Map<String, Value> = std::fs::read_dir(&tasks).unwrap().map(|f| {
        let f = f.unwrap();
        (f.file_name().into_string().unwrap(), json!(std::fs::read_to_string(f.path()).unwrap()))
    }).collect();
    assert_eq!(Value::Object(files), e["files"]);

    {
        let c = s.db.lock();
        let w02 = tr::item(&c, "W02").unwrap().unwrap();
        tr::apply_label(&c, &w02, &tr::Label::chat(), "slack", Some("sam"), None).unwrap();
        assert_json(&tables(&c), &e["after_relabel"], "after the relabel");
        assert_eq!(json!(tr::pending(&c).unwrap().iter().map(|i| &i.id).collect::<Vec<_>>()), e["pending_after_relabel"]);
        c.execute("UPDATE request_labels SET version = 1 WHERE work_item_id = 'W05'", []).unwrap();
    }
    answers.lock().unwrap()["SAM-7 Crash on save: please look"] = json!("still not json");
    calls.lock().unwrap().clear();
    let mut sweeps = Vec::new();
    for _ in 0..3 {
        sweeps.push(labeler.sweep(&s).await.unwrap());
    }
    assert_eq!(json!(sweeps), e["relabel_sweeps"]);
    assert_json(&json!(*calls.lock().unwrap()), &e["relabel_calls"], "relabel calls");
    assert_json(&tables(&s.db.lock()), &e["after_relabel_sweeps"], "an unreadable relabel keeps its label");
}

#[test]
fn parse_label_and_work_seconds_match_python() {
    let e = fixture();
    for case in e["parse_label"].as_array().unwrap() {
        let got = labeler::parse_label(case["text"].as_str().unwrap())
            .map(|l| json!([l.label, l.task, l.title, l.outcome, l.finished, l.project]));
        assert_eq!(got.unwrap_or(Value::Null), case["out"], "{}", case["text"]);
    }
    for case in e["work_seconds"].as_array().unwrap() {
        let spans: Vec<(Option<String>, Option<String>)> = serde_json::from_value(case["in"].clone()).unwrap();
        assert_eq!(json!(tr::work_seconds(&spans)), case["out"]);
    }
    assert_eq!(labeler::PROMPT, e["prompt"].as_str().unwrap());
}

#[tokio::test]
async fn waits_without_capacity() {
    let e = fixture();
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let reset = chrono::Utc::now().timestamp() as f64 + 4.0 * 3600.0;
    let full = json!({"acct-a": {"status": "limited", "rank": null, "verified": true, "5h": [1.0, reset]},
                      "acct-b": {"status": "limited", "rank": null, "verified": true, "5h": [1.0, reset]}});
    let proxy = agentd::proxy::ProxyClient::new(&fake_proxy(full).await, agentd::proxy::SoftCap::default());
    let s = shared(dir.path(), &slack).await;
    seed_rows(&s.db.lock(), &e["seed"]);
    let calls = Arc::new(Mutex::new(Vec::new()));
    let labeler = Labeler::new(stand_in(Arc::new(Mutex::new(e["answers"].clone())), calls.clone()),
                               labeler::proxy_capacity(Arc::new(proxy)));
    assert_eq!(labeler.sweep(&s).await.unwrap(), 0);
    assert!(calls.lock().unwrap().is_empty(), "no capacity: Haiku isn't asked");
    let labeled: i64 = s.db.lock().query_row("SELECT COUNT(*) FROM request_labels", [], |r| r.get(0)).unwrap();
    assert_eq!(labeled, 0);
}

#[tokio::test]
async fn haiku_through_the_proxy() {
    use axum::{Json, Router, http::StatusCode, routing::post};
    let seen = Arc::new(Mutex::new(Vec::<Value>::new()));
    let log = seen.clone();
    let app = Router::new().route("/v1/messages", post(move |headers: axum::http::HeaderMap, Json(body): Json<Value>| {
        let log = log.clone();
        async move {
            let user = body["messages"][0]["content"].as_str().unwrap_or_default().to_string();
            log.lock().unwrap().push(json!({"body": body, "sid": headers["x-claude-code-session-id"].to_str().unwrap(),
                                             "auth": headers["authorization"].to_str().unwrap()}));
            match user.as_str() {
                "busy" => (StatusCode::TOO_MANY_REQUESTS, "{}".to_string()),
                "refused" => (StatusCode::BAD_REQUEST, "{}".to_string()),
                "junk" => (StatusCode::OK, "not json".to_string()),
                _ => (StatusCode::OK, json!({"content": [{"type": "text", "text": "{\"label\":"},
                                                         {"type": "tool_use"}, {"type": "text", "text": " \"chat\"}"}]})
                    .to_string()),
            }
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    let haiku = labeler::haiku(&url);
    assert_eq!(haiku("sys".into(), "ok".into(), "sid-1".into()).await.unwrap(), "{\"label\": \"chat\"}");
    assert!(haiku("sys".into(), "busy".into(), "sid-1".into()).await.is_err(), "a limit: try on a later sweep");
    assert_eq!(haiku("sys".into(), "refused".into(), "sid-1".into()).await.unwrap(), "", "never takes it: a bad answer");
    assert_eq!(haiku("sys".into(), "junk".into(), "sid-1".into()).await.unwrap(), "");
    let first = seen.lock().unwrap()[0].clone();
    assert_eq!(first["sid"], "sid-1");
    assert_eq!(first["auth"], format!("Bearer {}", agentd::prepare::PLACEHOLDER_TOKEN));
    assert_eq!(first["body"]["model"], "claude-haiku-4-5");
    assert_eq!(first["body"]["max_tokens"], 400);
    assert_eq!(first["body"]["system"], "sys");
}
