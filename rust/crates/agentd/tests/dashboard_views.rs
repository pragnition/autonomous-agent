//! The dashboard's read models against outputs recorded from core on the same seeded database
//! (tests/fixtures/dashboard: make.py for the queries, make_views.py for accounts and transcripts).

mod common;

use agentd::dashboard::{accounts, queries as q, transcript as t};
use common::*;
use serde_json::{Value, json};

fn views() -> Value {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/views.json").unwrap()).unwrap()
}

fn seeded() -> rusqlite::Connection {
    let c = rusqlite::Connection::open_in_memory().unwrap();
    agentd::db::create_schema(&c).unwrap();
    seed(&c);
    c
}

fn filter(f: &Value) -> q::SessionFilter {
    let s = |k: &str| f[k].as_str().map(str::to_string);
    q::SessionFilter { state: s("state"), project: s("project"), source: s("source"), account: s("account"), q: s("q"),
                       limit: f["limit"].as_i64().unwrap_or(50), offset: f["offset"].as_i64().unwrap_or(0) }
}

fn rows(list: &[q::Row]) -> Value {
    json!(list)
}

#[test]
fn queries_match_python() {
    let (c, e, now) = (seeded(), expected(), fixture_now());
    let tz = e["tz"].as_str().unwrap();
    for case in e["list_sessions"].as_array().unwrap() {
        let (got, total) = q::list_sessions(&c, &filter(&case["filter"])).unwrap();
        assert_json(&json!({"rows": rows(&got), "total": total}), &json!({"rows": case["rows"], "total": case["total"]}),
                    &format!("sessions {}", case["filter"]));
    }
    for case in e["list_conversations"].as_array().unwrap() {
        let (groups, total) = q::list_conversations(&c, &filter(&case["filter"])).unwrap();
        let what = format!("conversations {}", case["filter"]);
        assert_json(&json!(groups), &case["groups"], &what);
        assert_eq!(json!(total), case["total"], "{what}");
        let convo_rows: Vec<q::Row> = groups.iter().map(|g| q::conversation_row(g, now)).collect();
        assert_json(&rows(&convo_rows), &case["rows"], &what);
    }
    for case in e["get_conversation"].as_array().unwrap() {
        let got = q::get_conversation(&c, case["ref"].as_str().unwrap()).unwrap().map(|(turns, focus)| json!([turns, focus]));
        assert_json(&got.unwrap_or(Value::Null), &case["out"], &format!("conversation {}", case["ref"]));
    }
    for case in e["get_session"].as_array().unwrap() {
        let got = q::get_session(&c, case["id"].as_str().unwrap()).unwrap();
        assert_json(&json!(got), &case["out"], &format!("session {}", case["id"]));
    }
    assert_json(&q::overview_counts(&c, now, tz).unwrap(), &e["overview_counts"], "counts");
    assert_json(&q::paused_by(&c).unwrap(), &e["paused_by"], "paused_by");
    for case in e["list_events"].as_array().unwrap() {
        let a = &case["args"];
        let got = q::list_events(&c, a[0].as_str(), a[1].as_i64().unwrap(), a[2].as_i64()).unwrap();
        assert_json(&got, &case["out"], &format!("events {a}"));
    }
    for case in e["kpi_view"].as_array().unwrap() {
        let got = q::kpi_view(&c, case["days"].as_i64().unwrap(), now, tz).unwrap();
        assert_json(&got, &case["view"], &format!("kpis {}", case["days"]));
    }
}

#[test]
fn accounts_match_python() {
    let v = views();
    let a = &v["accounts"];
    let origins: Vec<String> = serde_json::from_value(a["origins"].clone()).unwrap();
    let cap = agentd::proxy::SoftCap { limit: 0.65, lift_s: 5400.0 };
    let now = v["now"].as_f64().unwrap();
    assert_json(&accounts::accounts_view(Some(&a["raw"]), &origins, None, cap, now), &a["view"], "accounts");
    assert_json(&accounts::accounts_view(None, &origins, Some("proxy unreachable: refused"), cap, now), &a["missing"], "missing");
    let h = &v["history"];
    for case in h["cases"].as_array().unwrap() {
        let hours = case["hours"].as_i64().unwrap();
        assert_json(&accounts::history_view(h["rows"].as_array().unwrap(), hours, &origins, now), &case["view"],
                    &format!("history {hours}"));
    }
    for pair in h["bucket_seconds"].as_array().unwrap() {
        assert_eq!(json!(accounts::bucket_seconds(pair[0].as_i64().unwrap())), pair[1]);
    }
}

#[test]
fn transcript_matches_python() {
    let v = views();
    let tr = &v["transcript"];
    let scrub = t::Scrubber::new(&[tr["secret"].as_str().unwrap().to_string(), "short".into()]);
    let path = std::path::Path::new("tests/fixtures/dashboard/runs.jsonl");
    let (entries, offset, line) = t::read_from(path, 0, 0, &scrub);
    assert_json(&json!(entries), &tr["entries"], "entries");
    assert_eq!((json!(offset), json!(line)), (tr["offset"].clone(), tr["line"].clone()));
    let mid = &tr["from_mid"];
    let (later, end, line) = t::read_from(path, mid["offset"].as_u64().unwrap(), 4, &scrub);
    assert_json(&json!(later), &mid["entries"], "from the middle");
    assert_eq!((json!(end), json!(line)), (mid["end"].clone(), mid["line"].clone()));
    for pair in tr["lines_before"].as_array().unwrap() {
        assert_eq!(json!(t::lines_before(path, pair[0].as_u64().unwrap())), pair[1], "lines before {}", pair[0]);
    }
    let cache = t::TranscriptCache::new(scrub.clone(), 5);
    let (stats, recent) = cache.follow(path);
    assert_json(&json!({"session_id": stats.session_id, "tokens": stats.tokens, "turns": stats.turns, "done": stats.done}),
                &tr["follow"]["stats"], "live numbers");
    assert_json(&json!(recent), &tr["follow"]["recent"], "recent entries");
}

#[test]
fn partial_line_waits() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("W.jsonl");
    let scrub = t::Scrubber::new(&[]);
    std::fs::write(&path, "{\"type\": \"UserMessage\", \"content\": \"one\"}\n{\"type\": \"UserMess").unwrap();
    let (first, offset, line) = t::read_from(&path, 0, 0, &scrub);
    assert_eq!((first.len(), line), (1, 1), "the half-written line is left for the next read");
    use std::io::Write;
    std::fs::OpenOptions::new().append(true).open(&path).unwrap().write_all(b"age\", \"content\": \"two\"}\n").unwrap();
    let (second, _, line) = t::read_from(&path, offset, line, &scrub);
    assert_eq!(second.len(), 1);
    assert_eq!((second[0]["id"].as_str(), second[0]["text"].as_str(), line), (Some("1:0"), Some("two"), 2));
}

#[test]
fn cache_reads_only_new_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("W.jsonl");
    let cache = t::TranscriptCache::new(t::Scrubber::new(&[]), 5);
    let assistant = |id: &str, n: i64| format!(
        "{{\"type\": \"AssistantMessage\", \"message_id\": \"{id}\", \"usage\": {{\"output_tokens\": {n}}}, \"content\": []}}\n");
    std::fs::write(&path, assistant("m1", 5) + &assistant("m1", 5)).unwrap();
    let (stats, _) = cache.follow(&path);
    assert_eq!((stats.tokens["out"].as_i64(), stats.turns), (Some(5), 1), "one reply's usage counted once");
    use std::io::Write;
    std::fs::OpenOptions::new().append(true).open(&path).unwrap().write_all(assistant("m2", 7).as_bytes()).unwrap();
    let (stats, _) = cache.follow(&path);
    assert_eq!((stats.tokens["out"].as_i64(), stats.turns, stats.done), (Some(12), 2, false));
    std::fs::write(&path, "").unwrap(); // a shorter file isn't re-read from the start: the cache only reads what's new
    let (stats, _) = cache.follow(&path);
    assert_eq!(stats.tokens["out"], 12);
}
