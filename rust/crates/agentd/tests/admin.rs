//! The admin commands: `doctor`'s checks, and the kill switch as `agentd stop` and `agentd resume` pull it.

mod common;

use std::path::PathBuf;

use agentd::{doctor, queue};
use common::*;
use serde_json::json;

fn paths(dir: &std::path::Path, config: PathBuf) -> agentd::shared::Paths {
    agentd::shared::Paths { config, data: dir.join("data"), work: dir.join("work"),
                            defaults: PathBuf::from(REPO).join("src/agent/defaults"), plugins: PathBuf::from(REPO).join("src/agent/plugins") }
}

#[tokio::test]
async fn doctor_reports_each_check() {
    let dir = tempfile::tempdir().unwrap();
    let p = paths(dir.path(), PathBuf::from(REPO).join("examples/config"));
    let secrets = [("SLACK_BOT_TOKEN".to_string(), "xoxb-x".to_string()), ("LINEAR_API_KEY".into(), String::new())].into();
    let proxy = agentd::proxy::ProxyClient::new(&fake_proxy(usage(0.2)).await, Default::default());
    let checks = doctor::run(&p, &secrets, Some(proxy), None, "http://127.0.0.1:9", "http://127.0.0.1:9/graphql").await;
    let (text, all_ok) = doctor::report(&checks);
    assert!(!all_ok, "{text}");
    for line in ["ok    config: 2 people\n", "ok    packs: 1 pack(s): sample\n", "ok    secret:SLACK_BOT_TOKEN: set\n",
                 "FAIL  secret:SLACK_APP_TOKEN: missing\n", "FAIL  secret:LINEAR_API_KEY: missing\n",
                 "ok    proxy: 2 verified account(s), capacity available\n"] {
        assert!(text.contains(line), "{line:?} in\n{text}");
    }
    let models: Vec<&str> = text.lines().filter(|l| l.contains(" model:")).collect();
    assert!(!models.is_empty() && models.iter().all(|l| l.starts_with("ok    ") && l.ends_with(": served")), "{text}");
    assert!(!text.contains("linear:") && !text.contains("github:"), "no key, no check: {text}");

    let broken = dir.path().join("broken");
    std::fs::create_dir_all(&broken).unwrap();
    std::fs::write(broken.join("agent.toml"), "name = 5\n").unwrap();
    let checks = doctor::run(&paths(dir.path(), broken), &secrets, None, None, "", "").await;
    let (text, _) = doctor::report(&checks);
    assert!(text.starts_with("FAIL  config: ") && text.lines().count() == 1, "{text}");
}

#[test]
fn stop_and_resume_offline() {
    let c = rusqlite::Connection::open_in_memory().unwrap();
    agentd::db::create_schema(&c).unwrap();
    seed(&c);
    assert_eq!(queue::stop_all(&c, "local", None).unwrap(), ["W-3"]);
    let (status, paused): (String, Option<String>) = (
        c.query_row("SELECT status FROM work_items WHERE id = 'W-3'", [], |r| r.get(0)).unwrap(),
        agentd::db::get_setting(&c, "paused").unwrap());
    assert_eq!((status.as_str(), paused.as_deref()), ("cancelled", Some("1")));
    let data: String = c.query_row("SELECT data FROM events WHERE type = 'stop_all' ORDER BY id DESC LIMIT 1", [], |r| r.get(0)).unwrap();
    assert_eq!(serde_json::from_str::<serde_json::Value>(&data).unwrap(), json!({"cancelled": ["W-3"]}));
    queue::resume_all(&c, "local", None).unwrap();
    assert_eq!(agentd::db::get_setting(&c, "paused").unwrap().as_deref(), Some("0"));
    let actor: String = c.query_row("SELECT actor FROM events WHERE type = 'resume_all' ORDER BY id DESC LIMIT 1", [], |r| r.get(0)).unwrap();
    assert_eq!(actor, "local");
}

#[tokio::test]
async fn an_unreadable_project_env_file_is_skipped_not_fatal() {
    use std::os::unix::fs::PermissionsExt;
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let projects = s.paths.data.join("secrets").join("projects");
    std::fs::create_dir_all(&projects).unwrap();
    std::fs::write(projects.join("ok.env"), "A=1\n").unwrap();
    std::fs::write(projects.join("locked.env"), "B=2\n").unwrap();
    std::fs::set_permissions(projects.join("locked.env"), std::fs::Permissions::from_mode(0o000)).unwrap();
    agentd::serve::prepare_dirs(&s).unwrap(); // agentd still starts
    let copied = s.paths.data.join("runner").join("projects");
    assert_eq!(std::fs::read_to_string(copied.join("ok.env")).unwrap(), "A=1\n");
    assert!(!copied.join("locked.env").exists());
    assert!(s.paths.data.join("runner").join("timezone").exists());
}
