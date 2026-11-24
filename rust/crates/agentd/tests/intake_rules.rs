//! Stage 3's pure rules against the outputs recorded from the Python code (tests/fixtures/intake/make.py).

use std::sync::LazyLock;

use serde_json::Value;

static EXPECTED: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap()
});

fn case(name: &str) -> &'static Value {
    &EXPECTED[name]
}

#[test]
fn scrub_matches_python() {
    for c in case("scrub").as_array().unwrap() {
        let secrets = ["s3cret-value-0123456789".to_string(), "short".into(), String::new()];
        assert_eq!(agentd::scrub::scrub(c["text"].as_str().unwrap(), &secrets), c["out"].as_str().unwrap());
    }
}

#[test]
fn load_secrets_matches_python() {
    let got = agentd::scrub::load_secrets(std::path::Path::new("tests/fixtures/intake/secrets.env"));
    let want: std::collections::BTreeMap<String, String> = serde_json::from_value(case("secrets").clone()).unwrap();
    assert_eq!(got, want);
}

#[test]
fn task_transitions_match_python() {
    let dir = tempfile::tempdir().unwrap();
    let db = agentd::db::Db::open(&dir.path().join("a.db")).unwrap();
    let conn = db.lock();
    for m in case("transitions").as_array().unwrap() {
        let (from, to, ok) = (m[0].as_str().unwrap(), m[1].as_str().unwrap(), m[2].as_bool().unwrap());
        conn.execute("DELETE FROM tasks", []).unwrap();
        conn.execute("INSERT INTO tasks(id, project, title, kind, source, status, created_at, updated_at) \
                      VALUES ('T-0001', 'p', 't', 'feature', 'slack', ?, 'x', 'x')", [from]).unwrap();
        let events = |c: &rusqlite::Connection| c.query_row("SELECT COUNT(*) FROM events", [], |r| r.get::<_, i64>(0)).unwrap();
        let before = events(&conn);
        let moved = agentd::tasks::transition(&conn, "T-0001", to.parse().unwrap(), "a", None);
        assert_eq!(moved.is_ok(), ok, "{from} -> {to}");
        assert_eq!(events(&conn) - before, ok as i64, "an event for each move, none for a refusal");
    }
}

#[test]
fn task_ids_and_fields() {
    let dir = tempfile::tempdir().unwrap();
    let db = agentd::db::Db::open(&dir.path().join("a.db")).unwrap();
    let conn = db.lock();
    let new = |title: &str| agentd::tasks::create(&conn, agentd::tasks::NewTask { project: "p".into(), title: title.into(),
        kind: "bug".into(), source: "slack".into(), ..Default::default() }).unwrap();
    assert_eq!((new("a").id, new("b").id), ("T-0001".to_string(), "T-0002".to_string()));
    let t = agentd::tasks::set_fields(&conn, "T-0002", &[("pr_url", Some("https://x/1")), ("slack_channel", None)]).unwrap();
    assert_eq!((t.pr_url.as_deref(), t.status.as_str()), (Some("https://x/1"), "queued"));
    assert!(agentd::tasks::set_fields(&conn, "T-0002", &[("status", Some("done"))]).is_err(), "status only moves by transition");
    assert_eq!(agentd::tasks::list(&conn, Some("queued"), Some("p")).unwrap().len(), 2);
}

#[test]
fn persona_matches_python() {
    let defaults = std::path::Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../src/agent/defaults"));
    let text = agentd::persona::render(&agentd::persona::load(None, defaults).unwrap(), "agent").unwrap();
    assert_eq!(text, case("persona")["text"].as_str().unwrap());
    assert_eq!(agentd::persona::estimate_tokens(&text), case("persona")["tokens"].as_u64().unwrap() as usize);
}
