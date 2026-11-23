//! The KPIs, the `kpis` tool and the daily digest, against outputs recorded from core (tests/fixtures/dashboard).

mod common;

use agentd::kpis;
use chrono::Duration;
use common::*;
use serde_json::json;

#[test]
fn kpis_match_python() {
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    agentd::db::create_schema(&conn).unwrap();
    seed(&conn);
    let (e, now) = (expected(), fixture_now());
    for case in e["kpis"].as_array().unwrap() {
        let days = case["days"].as_i64().unwrap();
        let k = kpis::compute_kpis(&conn, now - Duration::days(days), now).unwrap();
        assert_json(&kpis::kpis_json(&k), &case["view"], &format!("{days} days"));
        assert_eq!(kpis::format_kpis(&k, "Jensen"), case["text"].as_str().unwrap(), "{days} days");
    }
    for pair in e["format_tokens"].as_array().unwrap() {
        assert_eq!(kpis::format_tokens(pair[0].as_i64().unwrap()), pair[1].as_str().unwrap());
    }
    for pair in e["format_duration"].as_array().unwrap() {
        assert_eq!(kpis::format_duration(pair[0].as_f64().unwrap()), pair[1].as_str().unwrap());
    }
}

#[test]
fn usage_windows_match_python() {
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    agentd::db::create_schema(&conn).unwrap();
    seed(&conn);
    let (e, now) = (expected(), fixture_now());
    let w = kpis::usage_windows(&conn, now, e["tz"].as_str().unwrap()).unwrap();
    assert_json(&kpis::windows_json(&w), &e["usage_windows"]["view"], "windows");
    assert_eq!(kpis::format_usage_windows(&w), e["usage_windows"]["text"].as_str().unwrap());
}

#[tokio::test]
async fn kpis_tool_checks_days() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    seed(&s.db.lock());
    let ctx = agentd::tools::ToolContext { tier: "read", ..Default::default() };
    for bad in [json!({"days": 0}), json!({"days": 366}), json!({"days": true}), json!({"days": "7"})] {
        assert_eq!(agentd::tools::call(&s, &ctx, "kpis", &bad).await, "days must be a whole number from 1 to 365");
    }
    let text = agentd::tools::call(&s, &ctx, "kpis", &json!({})).await;
    assert!(text.starts_with("jensen, last 7 days\nTasks: "), "{text}");
    assert!(text.contains("\n\nTokens today: "), "{text}");
}

#[tokio::test]
async fn digest_once_per_day() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.config.observability.digest_channel = "#ops".into();
    let now = fixture_now(); // 18:00 in Singapore; the digest is due from 09:00
    assert!(!kpis::post_digest(&s, now - Duration::hours(10)).await.unwrap(), "08:00: not yet");
    assert!(kpis::post_digest(&s, now).await.unwrap());
    assert!(!kpis::post_digest(&s, now + Duration::hours(5)).await.unwrap(), "the same local day, after a restart");
    assert!(kpis::post_digest(&s, now + Duration::hours(15)).await.unwrap(), "09:00 the next day");
    let posts = slack.sent("chat.postMessage");
    assert_eq!(posts.len(), 2);
    assert_eq!(posts[0]["channel"], "#ops");
    assert!(posts[0]["text"].as_str().unwrap().starts_with("jensen, last 7 days"));
}
