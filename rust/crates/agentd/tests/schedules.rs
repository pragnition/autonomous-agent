//! Schedules and the usage watch, against a fake Slack and proxy (a port of core's schedules.py and usage_watch.py
//! behaviour).

mod common;

use std::sync::{Arc, Mutex};

use agentd::schedules;
use agentd::usage_watch;
use chrono::{TimeZone, Utc};
use chrono_tz::Tz;
use common::*;
use serde_json::{Value, json};

fn expected() -> Value {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap()
}

fn sgt(h: u32, m: u32) -> chrono::DateTime<Utc> {
    let tz: Tz = "Asia/Singapore".parse().unwrap();
    tz.with_ymd_and_hms(2026, 11, 23, h, m, 0).unwrap().with_timezone(&Utc)
}

#[test]
fn schedule_parse_matches_python() {
    let e = expected();
    let text = r##"
[daily-report]
cron = "0 9 * * 1-5"
channel = "#general"
prompt = "Report"
by = "sam"

[Bad_Name]
cron = "0 9 * * *"
channel = "x"
prompt = "p"
by = "b"

[missing]
cron = "0 9 * * *"
channel = "x"

[never]
cron = "0 0 30 2 *"
channel = "x"
prompt = "p"
by = "b"

[six-fields]
cron = "0 0 9 * * *"
channel = "x"
prompt = "p"
by = "b"

[extra]
cron = "*/15 * * * *"
channel = "sam"
prompt = "p"
by = "b"
link = "https://x"
paused = true
color = "red"

[typed]
cron = "*/15 * * * *"
channel = "x"
prompt = "p"
by = "b"
paused = "yes"

[ok-paused]
cron = "*/15 * * * *"
channel = "sam"
prompt = "p"
by = "b"
paused = true
"##;
    let (valid, errors) = schedules::parse(text);
    let valid: Vec<Value> = valid.unwrap().iter().map(|s| json!({"name": s.name, "cron": s.cron, "channel": s.channel,
        "prompt": s.prompt, "by": s.by, "link": s.link, "paused": s.paused})).collect();
    assert_eq!(json!(valid), e["schedules"]["valid"]);
    assert_eq!(json!(errors), e["schedules"]["errors"]);
    let (none, bad) = schedules::parse("[x\n");
    assert!(none.is_none() && bad[0].starts_with("not valid TOML: "));
    let tz: Tz = "Asia/Singapore".parse().unwrap();
    let when = tz.with_ymd_and_hms(2026, 11, 23, 9, 7, 30).unwrap();
    for c in e["cron"].as_array().unwrap() {
        let cron = c["cron"].as_str().unwrap();
        assert_eq!(schedules::latest_slot(cron, when).unwrap().to_rfc3339(), c["latest"].as_str().unwrap(), "{cron}");
        let next: Vec<String> = schedules::next_slots(cron, when, 3).unwrap().iter().map(|t| t.to_rfc3339()).collect();
        assert_eq!(json!(next), c["next"], "{cron}");
    }
}

async fn setup(file: &str) -> (tempfile::TempDir, FakeSlack, Arc<agentd::shared::Shared>, schedules::Scheduler) {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let path = dir.path().join("data/schedules/schedules.toml");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, file).unwrap();
    (dir, slack, s, schedules::Scheduler::new(path))
}

const EVERY_15: &str = "[sync]\ncron = \"*/15 * * * *\"\nchannel = \"sam\"\nprompt = \"sync the mirrors\"\nby = \"sam\"\n";

fn runs(s: &agentd::shared::Shared) -> Vec<Value> {
    s.db.lock().prepare("SELECT payload FROM work_items ORDER BY created_at, rowid").unwrap()
        .query_map([], |r| Ok(serde_json::from_str(&r.get::<_, String>(0)?).unwrap())).unwrap().map(Result::unwrap).collect()
}

#[tokio::test]
async fn fires_once_per_slot() {
    let (_d, _slack, s, scheduler) = setup(EVERY_15).await;
    assert!(scheduler.tick(&s, sgt(9, 7)).await.unwrap().is_empty(), "seen for the first time: waits for its next slot");
    assert_eq!(scheduler.tick(&s, sgt(9, 16)).await.unwrap(), ["sync"]);
    assert!(scheduler.tick(&s, sgt(9, 17)).await.unwrap().is_empty());
    assert_eq!(scheduler.tick(&s, sgt(9, 31)).await.unwrap(), ["sync"]);
    let p = &runs(&s)[0];
    assert_eq!((p["text"].as_str(), p["source"].as_str(), p["unattended"].as_bool(), p["requester"].as_str()),
               (Some("sync the mirrors"), Some("schedule"), Some(true), Some("sam")));
    assert_eq!(p["origin"], "[schedule sync · */15 * * * * · set up by Name U00000001 (sam, GitHub @sam-example)]");
    assert_eq!(p["report_to"], json!({"channel": "sam", "schedule": "sync"}));
}

#[tokio::test]
async fn clock_jump_fires_once() {
    let (_d, _slack, s, scheduler) = setup(EVERY_15).await;
    scheduler.tick(&s, sgt(9, 7)).await.unwrap();
    assert_eq!(scheduler.tick(&s, sgt(13, 2)).await.unwrap(), ["sync"], "slots missed while asleep collapse into one");
    assert!(scheduler.tick(&s, sgt(13, 2)).await.unwrap().is_empty());
    assert_eq!(runs(&s).len(), 1);
}

#[tokio::test]
async fn paused_and_stopped_use_up_slots() {
    let (dir, _slack, s, scheduler) = setup(&EVERY_15.replace("by = \"sam\"\n", "by = \"sam\"\npaused = true\n")).await;
    scheduler.tick(&s, sgt(9, 7)).await.unwrap();
    assert!(scheduler.tick(&s, sgt(9, 16)).await.unwrap().is_empty(), "paused");
    std::fs::write(dir.path().join("data/schedules/schedules.toml"), EVERY_15).unwrap();
    agentd::db::set_setting(&s.db.lock(), "paused", "1").unwrap();
    assert!(scheduler.tick(&s, sgt(9, 31)).await.unwrap().is_empty(), "the agent is stopped");
    agentd::db::set_setting(&s.db.lock(), "paused", "0").unwrap();
    assert!(scheduler.tick(&s, sgt(9, 32)).await.unwrap().is_empty(), "used-up slots don't fire later");
    assert_eq!(scheduler.tick(&s, sgt(9, 46)).await.unwrap(), ["sync"]);
}

#[tokio::test]
async fn errors_reported_once() {
    let (dir, slack, s, scheduler) = setup(&format!("{EVERY_15}\n[Bad]\ncron = \"x\"\n")).await;
    scheduler.tick(&s, sgt(9, 7)).await.unwrap();
    scheduler.tick(&s, sgt(9, 8)).await.unwrap();
    let dms = slack.sent("chat.postMessage");
    assert_eq!(dms.len(), 1);
    assert!(dms[0]["text"].as_str().unwrap().starts_with("Problems in the schedules file (data/schedules/schedules.toml):\n- Bad: "));
    std::fs::write(dir.path().join("data/schedules/schedules.toml"), "[x\n").unwrap();
    scheduler.tick(&s, sgt(9, 9)).await.unwrap();
    assert_eq!(slack.sent("chat.postMessage").len(), 2, "a new problem is reported");
    assert_eq!(scheduler.tick(&s, sgt(9, 16)).await.unwrap(), ["sync"], "a broken file keeps the last valid schedules");
}

fn rec(rank: Option<i64>, used: f64, reset_in: f64, verified: bool) -> Value {
    let reset = Utc::now().timestamp() as f64 + reset_in;
    json!({"status": "ok", "rank": rank, "verified": verified, "5h": [used, reset]})
}

#[test]
fn account_states_match_python() {
    let e = expected();
    let cap65 = agentd::proxy::SoftCap { limit: 0.65, lift_s: 5400.0 };
    for c in e["account_state"].as_array().unwrap() {
        assert_eq!(usage_watch::account_state(&c["rec"], c["previous"].as_str(), 1_000_000.0, &cap65),
                   c["out"].as_str().unwrap(), "{}", c["rec"]);
    }
    let resets: Vec<Option<f64>> = [json!({"5h": [0.5, 10], "7d": [1.0, 20]}), json!({"5h": [0.5, 10], "7d": [0.5, 20]}), json!({})]
        .iter().map(usage_watch::next_reset).collect();
    let want: Vec<Option<f64>> = e["next_reset"].as_array().unwrap().iter().map(Value::as_f64).collect();
    assert_eq!(resets, want);
    for c in e["pool_level"].as_array().unwrap() {
        assert_eq!(usage_watch::pool_level(c[0].as_i64().unwrap(), c[1].as_i64().unwrap()), c[2].as_str().unwrap());
    }
}

#[tokio::test]
async fn weekly_capped_account_is_out_until_its_week_resets_without_an_alert() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let now = Utc::now().timestamp() as f64;
    let capped = json!({"status": "allowed_warning", "rank": null, "verified": true, "5h": [0.43, now + 3600.0],
                        "7d": [0.84, now + 2.0 * 86400.0], "cap_7d": 0.65});
    assert_eq!(usage_watch::next_reset(&capped), Some(now + 2.0 * 86400.0), "room comes back at the weekly reset");
    let usage = json!({"a": capped, "b": rec(Some(1), 0.1, 3600.0, true), "c": rec(Some(2), 0.1, 3600.0, true),
                       "d": rec(None, 0.1, 3600.0, true)});
    let s = shared_at(dir.path(), &slack, &fake_proxy_live(Arc::new(Mutex::new(usage))).await);
    let long_ago = now - 6.0 * 3600.0;
    let out = json!({"state": "out", "since": long_ago, "verified": true, "out_alerted": false, "cap": 0.65, "capped": false});
    agentd::db::set_setting(&s.db.lock(), "usage_watch", &json!({"pool": "normal", "accounts": {
        "a": out, "b": {"state": "ok", "since": long_ago, "verified": true}, "c": {"state": "ok", "since": long_ago, "verified": true},
        "d": out}}).to_string()).unwrap();
    usage_watch::tick(&s).await.unwrap();
    let dms: Vec<String> = slack.sent("chat.postMessage").iter().map(|m| m["text"].as_str().unwrap().to_string()).collect();
    assert_eq!(dms.len(), 1, "{dms:?}");
    assert!(dms[0].starts_with("Account d has had no capacity"), "only the account out for no reason: {dms:?}");
    let _ = dir;
}

#[tokio::test]
async fn pool_hold_and_alerts_once() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let usage = Arc::new(Mutex::new(json!({"a": rec(Some(0), 1.0, 4.0 * 3600.0, true), "b": rec(None, 1.0, 3600.0, true)})));
    let s = shared_at(dir.path(), &slack, &fake_proxy_live(usage.clone()).await);
    let hold = |s: &agentd::shared::Shared| agentd::db::get_setting(&s.db.lock(), agentd::queue::HOLD_SCHEDULED).unwrap();
    usage_watch::tick(&s).await.unwrap();
    usage_watch::tick(&s).await.unwrap();
    assert_eq!(hold(&s).as_deref(), Some("1"));
    let dms = slack.sent("chat.postMessage");
    assert_eq!(dms.len(), 1, "once");
    assert!(dms[0]["text"].as_str().unwrap().starts_with("Every Claude account is at its 5-hour soft cap, hot or out of capacity."));
    *usage.lock().unwrap() = json!({"a": rec(Some(0), 0.1, 4.0 * 3600.0, true), "b": rec(Some(1), 0.1, 3600.0, true)});
    usage_watch::tick(&s).await.unwrap();
    assert_eq!(hold(&s).as_deref(), Some("0"));
    assert_eq!(slack.sent("chat.postMessage")[1]["text"], "Claude capacity is back: 2 accounts with room.");
    let kinds: Vec<String> = s.db.lock().prepare("SELECT type FROM events ORDER BY id").unwrap().query_map([], |r| r.get(0)).unwrap()
        .map(Result::unwrap).collect();
    assert!(kinds.contains(&"usage_pool".to_string()) && kinds.contains(&"usage_account".to_string()), "{kinds:?}");
    let _ = dir;
}

#[tokio::test]
async fn limit_waits_resumed_when_ok() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let convo = agentd::conversations::create_conversation(&s.db.lock(), Some("D1"), Some("1.0"), None, None, true, None).unwrap();
    let row = agentd::conversations::row(&s.db.lock(), &convo).unwrap().unwrap();
    let item = agentd::conversations::enqueue_turn(&s, &row, "hi", Some("sam"), Default::default()).unwrap();
    s.db.lock().execute("INSERT INTO limit_waits(work_item_id, conversation_id, error, created_at) VALUES (?, ?, '429', 'x')",
                        [&item, &convo]).unwrap();
    usage_watch::tick(&s).await.unwrap();
    let resumed: Option<String> = s.db.lock().query_row("SELECT resumed_as FROM limit_waits", [], |r| r.get(0)).unwrap();
    assert!(resumed.is_some());
    usage_watch::tick(&s).await.unwrap();
    assert_eq!(runs(&s).len(), 2, "carried on once");
}

#[test]
fn check_prints_next_runs() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("schedules.toml");
    std::fs::write(&path, EVERY_15).unwrap();
    let tz: Tz = "Asia/Singapore".parse().unwrap();
    let (out, code) = schedules::check(&path, tz, tz.with_ymd_and_hms(2026, 11, 23, 9, 7, 0).unwrap());
    assert_eq!(code, 0);
    assert_eq!(out, "sync  */15 * * * *  -> sam\n  next: 2026-11-23 09:15, 2026-11-23 09:30, 2026-11-23 09:45 (Asia/Singapore)\n");
    std::fs::write(&path, "[Bad]\ncron = \"x\"\n").unwrap();
    assert_eq!(schedules::check(&path, tz, Utc::now().with_timezone(&tz)).1, 1);
}
