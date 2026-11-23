//! The PR and Linear issue pollers (the KPIs' Shipped numbers) against fake GitHub and Linear servers.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use agentd::pollers;
use chrono::Utc;
use common::*;
use serde_json::{Value, json};

/// A fake GitHub API: a JSON answer per path; every request's path and query recorded.
#[derive(Clone, Default)]
struct FakeGitHub {
    routes: Arc<Mutex<HashMap<String, Value>>>,
    seen: Arc<Mutex<Vec<String>>>,
}

impl FakeGitHub {
    async fn start(routes: Value) -> (FakeGitHub, String) {
        let fake = FakeGitHub::default();
        *fake.routes.lock().unwrap() = serde_json::from_value(routes).unwrap();
        let state = fake.clone();
        let app = axum::Router::new().fallback(move |uri: axum::http::Uri| {
            let f = state.clone();
            async move {
                f.seen.lock().unwrap().push(uri.to_string());
                match f.routes.lock().unwrap().get(uri.path()) {
                    Some(v) => (axum::http::StatusCode::OK, axum::Json(v.clone())),
                    None => (axum::http::StatusCode::NOT_FOUND, axum::Json(json!({"message": "Not Found"}))),
                }
            }
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await });
        (fake, url)
    }

    fn seen(&self) -> Vec<String> {
        self.seen.lock().unwrap().clone()
    }
}

async fn with_github(routes: Value) -> (tempfile::TempDir, FakeSlack, FakeGitHub, Arc<agentd::shared::Shared>) {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let (gh, url) = FakeGitHub::start(routes).await;
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.github = Some(agentd::github::GitHub::new("ghs-test", &url));
    s.config.github = "jensen-bot".into();
    (dir, slack, gh, Arc::new(s))
}

fn task(c: &rusqlite::Connection, id: &str, status: &str, pr: i64) {
    c.execute("INSERT INTO tasks(id, project, title, kind, source, status, pr_number, pr_url, created_at, updated_at) \
               VALUES (?, 'sample', 'Fix login', 'bug', 'slack', ?, ?, 'https://github.com/x/pull/1', 'x', 'x')",
              rusqlite::params![id, status, pr]).unwrap();
}

#[tokio::test]
async fn pr_stats_records_reverts_and_moves_task() {
    let repo = "/repos/example-org/agent-sample";
    let (_dir, _slack, gh, s) = with_github(json!({
        format!("{repo}/pulls/12"): {"created_at": "2026-11-20T01:00:00Z", "draft": false, "merged": true,
            "merged_at": "2026-11-20T05:00:00Z", "closed_at": "2026-11-20T05:00:00Z", "merge_commit_sha": "abc",
            "state": "closed", "title": "Fix login", "base": {"ref": "dev"}},
        format!("{repo}/pulls/12/reviews"): [
            {"user": {"login": "jensen-bot"}, "submitted_at": "2026-11-20T01:10:00Z"},
            {"user": {"login": "sam"}, "submitted_at": "2026-11-20T03:00:00Z"},
            {"user": {"login": "kim"}, "submitted_at": "2026-11-20T02:00:00Z"},
            {"user": {"login": "x"}, "submitted_at": null}],
        format!("{repo}/pulls/12/commits"): [{"sha": "c1", "author": {"login": "jensen-bot"}},
                                              {"sha": "c2", "author": {"login": "sam"}}, {"sha": "c3", "author": null}],
        format!("{repo}/commits/c2"): {"stats": {"additions": 5, "deletions": 2}},
        format!("{repo}/commits/c3"): {"stats": {"additions": 1}},
        format!("{repo}/issues/12/events"): [{"event": "labeled", "created_at": "2026-11-20T01:05:00Z"},
                                              {"event": "ready_for_review", "created_at": "2026-11-20T01:30:00Z"}],
        format!("{repo}/commits"): [{"sha": "r0", "commit": {"message": "unrelated"}},
                                    {"sha": "r1", "commit": {"message": "Undo\n\nThis reverts commit abc.",
                                                             "author": {"date": "2026-11-21T00:00:00Z"}}}],
    })).await;
    {
        let c = s.db.lock();
        task(&c, "T-0001", "ready_for_review", 12);
        task(&c, "T-0002", "merged", 13); // merged 40 days ago: past the revert window
        c.execute("INSERT INTO pr_stats(task_id, repo, pr_number, merged_at, updated_at) \
                   VALUES ('T-0002', 'x', 13, '2026-10-11T00:00:00Z', 'x')", []).unwrap();
        task(&c, "T-0003", "working", 14);
    }
    let now = "2026-11-20T10:00:00Z".parse().unwrap();
    assert_eq!(pollers::pr_stats(&s, now).await.unwrap(), ["T-0001"]);
    let row = s.db.lock().query_row("SELECT * FROM pr_stats WHERE task_id = 'T-0001'", [], agentd::db::row_json).unwrap();
    for (k, v) in [("repo", json!("example-org/agent-sample")), ("pr_number", json!(12)),
                   ("opened_at", json!("2026-11-20T01:00:00Z")), ("ready_at", json!("2026-11-20T01:30:00Z")),
                   ("first_human_review_at", json!("2026-11-20T02:00:00Z")), ("merged_at", json!("2026-11-20T05:00:00Z")),
                   ("merge_sha", json!("abc")), ("human_commits", json!(2)), ("human_lines_changed", json!(8)),
                   ("reverted_at", json!("2026-11-21T00:00:00Z")), ("revert_ref", json!("r1"))] {
        assert_eq!(row[k], v, "{k}");
    }
    assert!(!gh.seen().iter().any(|p| p.ends_with("/commits/c1")), "the agent's own commits aren't counted");
    assert!(gh.seen().iter().any(|p| p.contains("/commits?") && p.contains("sha=dev") && p.contains("since=2026-11-20T05")));
    let t = agentd::tasks::get(&s.db.lock(), "T-0001").unwrap().unwrap();
    assert_eq!(t.status.as_str(), "merged");
    let moved: String = s.db.lock().query_row("SELECT actor FROM events WHERE type = 'status_changed' AND task_id = 'T-0001'",
                                              [], |r| r.get(0)).unwrap();
    assert_eq!(moved, "github");
}

#[tokio::test]
async fn agent_prs_found_and_tied_to_issue() {
    let (_dir, _slack, gh, s) = with_github(json!({
        "/search/issues": {"items": [{"repository_url": "https://api.example/repos/o/r/", "number": 5}]},
        "/repos/o/r/pulls/5": {"html_url": "https://github.com/o/r/pull/5", "title": "Fix", "body": "see SAM-70",
                               "head": {"ref": "sam-7-fix"}, "base": {"ref": "main"},
                               "created_at": "2026-11-20T01:00:00.000Z", "merged_at": null, "closed_at": null},
        "/repos/o/r/pulls/3": {"title": "Older", "head": {"ref": "x"}, "base": {"ref": "main"},
                               "created_at": "2026-11-19T01:00:00Z", "merged_at": "2026-11-20T02:00:00Z",
                               "closed_at": "2026-11-20T02:00:00Z"},
    })).await;
    {
        let c = s.db.lock();
        for (id, ident) in [("i1", "SAM-7"), ("i2", "SAM-70")] {
            c.execute("INSERT INTO linear_issues(issue_id, identifier, handed_at) VALUES (?, ?, 'x')", [id, ident]).unwrap();
        }
        for (n, merged) in [(3, None), (2, Some("2026-11-01T00:00:00Z"))] {
            c.execute("INSERT INTO agent_prs(repo, number, opened_at, merged_at, issue_identifier, updated_at) \
                       VALUES ('o/r', ?, 'x', ?, 'SAM-1', 'x')", rusqlite::params![n, merged]).unwrap();
        }
    }
    assert_eq!(pollers::agent_prs(&s).await.unwrap(), 2);
    let floor = agentd::db::get_setting(&s.db.lock(), "kpi_floor").unwrap().unwrap();
    assert!(gh.seen()[0].contains(&format!("created%3A%3E%3D{}", floor.replace(':', "%3A"))), "{:?}", gh.seen());
    let rows: Vec<Value> = s.db.lock().prepare("SELECT * FROM agent_prs ORDER BY number").unwrap()
        .query_map([], agentd::db::row_json).unwrap().map(Result::unwrap).collect();
    assert_eq!(rows[0]["updated_at"], "x", "merged: left alone");
    assert_eq!((&rows[1]["merged_at"], &rows[1]["issue_identifier"]), (&json!("2026-11-20T02:00:00Z"), &json!("SAM-1")),
               "an open one kept current, keeping its issue");
    assert_eq!((&rows[2]["opened_at"], &rows[2]["issue_identifier"], &rows[2]["head"]),
               (&json!("2026-11-20T01:00:00Z"), &json!("SAM-7"), &json!("sam-7-fix")), "the branch names the issue first");
    assert!(!gh.seen().iter().any(|p| p.contains("/pulls/2")));
}

#[test]
fn match_issue_and_iso_match_python() {
    let e = expected();
    let ids: Vec<String> = ["PRA-423", "PRA-42", "SAM-7", "SAM-70"].map(String::from).to_vec();
    for case in e["match_issue"].as_array().unwrap() {
        let texts: Vec<Option<String>> = serde_json::from_value(case["texts"].clone()).unwrap();
        let texts: Vec<Option<&str>> = texts.iter().map(Option::as_deref).collect();
        assert_eq!(json!(pollers::match_issue(&texts, &ids)), case["out"], "{}", case["texts"]);
    }
    for case in e["iso"].as_array().unwrap() {
        assert_eq!(json!(pollers::iso(case[0].as_str())), case[1], "{}", case[0]);
    }
}

#[tokio::test]
async fn issue_states_polled_until_a_week_after_close() {
    let calls = Arc::new(Mutex::new(Vec::<String>::new()));
    let log = calls.clone();
    let app = axum::Router::new().route("/graphql", axum::routing::post(move |axum::Json(body): axum::Json<Value>| {
        let log = log.clone();
        async move {
            let id = body["variables"]["id"].as_str().unwrap_or_default().to_string();
            log.lock().unwrap().push(format!("{}:{id}", body["operationName"].as_str().unwrap_or_default()));
            axum::Json(match id.as_str() {
                "i1" => json!({"data": {"issue": {"state": {"type": "completed"}, "completedAt": "2026-11-20T08:00:00.000Z",
                                                  "canceledAt": null}}}),
                "i2" => json!({"data": {"issue": {"state": {"type": "started"}, "completedAt": "2026-11-17T00:00:00.000Z",
                                                  "canceledAt": null}}}),
                "i4" => json!({"data": {"issue": {"state": {"type": "canceled"}, "completedAt": null,
                                                  "canceledAt": "2026-11-18T00:00:00.000Z"}}}),
                _ => json!({"errors": [{"message": "no such issue"}]}),
            })
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/graphql", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.linear = Some(agentd::linear::Linear { client: agentd::linear::LinearClient::new("lin-test", &url), app: None,
                                             webhook_secret: String::new() });
    let now = "2026-11-20T10:00:00Z".parse::<chrono::DateTime<Utc>>().unwrap();
    {
        let c = s.db.lock();
        for (id, handed, completed, canceled) in [("i1", "2026-11-01", None, None),
                                                  ("i2", "2026-11-02", Some("2026-11-17T00:00:00Z"), None),
                                                  ("i3", "2026-11-03", Some("2026-11-10T00:00:00Z"), None),
                                                  ("i4", "2026-11-04", None, Some("2026-11-18T00:00:00Z")),
                                                  ("i5", "2026-11-05", None, None)] {
            c.execute("INSERT INTO linear_issues(issue_id, identifier, handed_at, completed_at, canceled_at) \
                       VALUES (?, ?, ?, ?, ?)", rusqlite::params![id, id.to_uppercase(), handed, completed, canceled]).unwrap();
        }
    }
    assert_eq!(pollers::linear_issues(&s, now).await.unwrap(), 4);
    assert_eq!(*calls.lock().unwrap(), ["IssueState:i1", "IssueState:i2", "IssueState:i4", "IssueState:i5"],
               "closed over a week ago: not asked");
    let rows: HashMap<String, (Option<String>, Option<String>, Option<String>, Option<String>)> = s.db.lock()
        .prepare("SELECT issue_id, state_type, completed_at, canceled_at, checked_at FROM linear_issues").unwrap()
        .query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))).unwrap()
        .map(Result::unwrap).collect();
    let checked = Some("2026-11-20T10:00:00Z".to_string());
    assert_eq!(rows["i1"], (Some("completed".into()), Some("2026-11-20T08:00:00Z".into()), None, checked.clone()));
    assert_eq!(rows["i2"], (Some("started".into()), None, None, checked.clone()), "reopened");
    assert_eq!(rows["i3"].3, None);
    assert_eq!(rows["i4"], (Some("canceled".into()), None, Some("2026-11-18T00:00:00Z".into()), checked));
    assert_eq!(rows["i5"].3, None, "a failed read leaves the row as it was");
}
