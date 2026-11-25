//! The tools Claude Code calls, against a fake Slack and a fake account proxy (a port of core's agent_tools.py,
//! parking.py and projects.py behaviour).

mod common;

use agentd::conversations as cv;
use agentd::shared::Shared;
use agentd::tools;
use common::*;
use serde_json::{Value, json};

/// A running turn in a new conversation: (conversation, work item).
fn turn(s: &Shared, actor: Option<&str>, unattended: bool, channel: Option<&str>, thread_ts: Option<&str>) -> (String, String) {
    let c = s.db.lock();
    let convo = cv::create_conversation(&c, channel, thread_ts, Some("sample"), None, true, None).unwrap();
    let row = cv::row(&c, &convo).unwrap().unwrap();
    drop(c);
    let mut payload = agentd::payloads::turn(s, &row, "please do it", actor, unattended).unwrap();
    payload["origin"] = json!("[from sam · DM]");
    payload["source"] = json!("slack");
    payload["requester"] = json!(actor);
    payload["message_ts"] = json!(thread_ts);
    let c = s.db.lock();
    let item = agentd::queue::enqueue(&c, Some(&convo), "turn", &payload).unwrap();
    c.execute("UPDATE work_items SET status = 'claimed' WHERE id = ?", [&item]).unwrap();
    (convo, item)
}

async fn call(s: &Shared, item: &str, name: &str, args: Value) -> String {
    let ctx = tools::context_for(s, item).unwrap();
    tools::call(s, &ctx, name, &args).await
}

fn names(s: &Shared, item: &str) -> Vec<String> {
    tools::manifest(s, &tools::context_for(s, item).unwrap()).into_iter().map(|t| t.name).collect()
}

#[test]
fn catalogue_matches_python() {
    let python: Vec<Value> = serde_json::from_str(include_str!("../src/tools.json")).unwrap();
    let ours: Vec<Value> = tools::catalogue().iter().map(|t| json!({"name": t.name, "description": t.description,
                                                                    "input_schema": t.input_schema, "tier": t.tier})).collect();
    assert_eq!(ours, python);
}

#[tokio::test]
async fn tiers_decide_manifest_and_calls() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (_, member) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    let (_, admin) = turn(&s, Some("sam"), false, Some("C1"), Some("2.0"));
    let (_, stranger) = turn(&s, Some("pat"), false, Some("C1"), Some("3.0"));
    let (_, linear) = turn(&s, None, true, None, None);
    let (m, a, r, l) = (names(&s, &member), names(&s, &admin), names(&s, &stranger), names(&s, &linear));
    assert!(m.contains(&"post_update".into()) && !m.contains(&"resume_all".into()));
    assert!(a.contains(&"resume_all".into()) && a.contains(&"az_login".into()), "{a:?}");
    assert!(!r.contains(&"post_update".into()) && r.contains(&"status".into()));
    assert!(l.contains(&"park".into()) && !l.contains(&"resume_all".into()));
    assert!(!a.iter().any(|n| n.starts_with("gh_") || n == "linear_install"), "no GitHub token, no Linear app");
    assert_eq!(call(&s, &member, "resume_all", json!({})).await, "not available here");
    assert_eq!(call(&s, &stranger, "post_update", json!({"text": "x"})).await, "not available here");
    assert_eq!(call(&s, &member, "nope", json!({})).await, "not available here");
}

#[tokio::test]
async fn post_update_posts_and_records() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &item, "post_update", json!({"text": "50% core-secret-0123456789"})).await, "posted");
    let post = &slack.sent("chat.postMessage")[0];
    assert_eq!((post["text"].as_str(), post["thread_ts"].as_str()), (Some("50% [redacted]"), Some("1.0")));
    let recorded: String = s.db.lock().query_row("SELECT conversation_id FROM slack_messages WHERE ts = ?",
                                                 [post_ts(&slack)], |r| r.get(0)).unwrap();
    assert_eq!(recorded, convo);
    let (_, threadless) = turn(&s, None, true, None, None);
    assert_eq!(call(&s, &threadless, "post_update", json!({"text": "x"})).await, "this conversation has no Slack thread");
}

fn post_ts(slack: &FakeSlack) -> String {
    // the fake numbers each answer; the first post is the first call
    let n = slack.calls.lock().unwrap().iter().position(|(m, _)| m == "chat.postMessage").unwrap() + 1;
    format!("900.{n:06}")
}

#[tokio::test]
async fn ask_sets_waiting_and_needs_input() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &item, "track_task", json!({"project": "sample", "title": "Fix login", "kind": "bug"})).await,
               "Tracking T-0001: Fix login");
    let reply = call(&s, &item, "ask", json!({"question": " Which env? "})).await;
    assert_eq!(reply, "Asked. Their reply arrives as your next message; end this turn now.");
    assert!(cv::row(&s.db.lock(), &convo).unwrap().unwrap().waiting);
    assert_eq!(agentd::tasks::get(&s.db.lock(), "T-0001").unwrap().unwrap().status.as_str(), "needs_input");
    assert!(slack.sent("chat.postMessage").iter().any(|p| p["text"] == "Question about T-0001: Which env?\nReply in this thread and I'll pick it back up."));
}

#[tokio::test]
async fn track_and_finish_task() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &item, "track_task", json!({"project": "nope", "title": "x", "kind": "bug"})).await,
               "unknown project 'nope' (known: sample)");
    assert_eq!(call(&s, &item, "track_task", json!({"project": "sample", "title": "x", "kind": "epic"})).await,
               "kind must be one of feature, bug, chore");
    assert_eq!(call(&s, &item, "finish_task", json!({})).await, "there is no task to finish in this conversation");
    call(&s, &item, "track_task", json!({"project": "sample", "title": "Fix login", "kind": "bug"})).await;
    assert_eq!(cv::row(&s.db.lock(), &convo).unwrap().unwrap().task_id.as_deref(), Some("T-0001"));
    assert_eq!(call(&s, &item, "track_task", json!({"project": "sample", "title": "again", "kind": "bug"})).await, "Already tracking T-0001.");
    assert_eq!(slack.sent("chat.postMessage")[0]["text"], "T-0001 · Fix login — working on it");
    assert_eq!(call(&s, &item, "finish_task", json!({"outcome": "PR #1"})).await, "T-0001 is done.");
    assert_eq!(slack.sent("chat.update")[0]["text"], "T-0001 · Fix login — done");
    assert!(call(&s, &item, "finish_task", json!({})).await.starts_with("can't finish T-0001: T-0001: done -> done is not allowed"));
    assert!(call(&s, &item, "get_task", json!({"task_id": "T-0001"})).await.starts_with("T-0001 · Fix login — done\nproject: sample · kind: bug"));
    assert_eq!(call(&s, &item, "list_tasks", json!({"status": "done"})).await, "T-0001 [done] sample: Fix login");
    assert_eq!(call(&s, &item, "list_tasks", json!({"status": "nah"})).await, "unknown status 'nah'");
}

#[tokio::test]
async fn start_task_new_session() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    let reply = call(&s, &item, "start_task", json!({"project": "sample", "title": "Docs", "kind": "chore", "brief": "write them"})).await;
    assert_eq!(reply, "Started T-0001 (Docs) in a new session for sample.");
    let c = s.db.lock();
    let (convo, primary, task): (String, i64, String) = c.query_row(
        "SELECT id, is_primary, task_id FROM conversations WHERE task_id IS NOT NULL", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap();
    assert_eq!((primary, task.as_str()), (0, "T-0001"));
    let payload: String = c.query_row("SELECT payload FROM work_items WHERE conversation_id = ?", [&convo], |r| r.get(0)).unwrap();
    let payload: Value = serde_json::from_str(&payload).unwrap();
    assert_eq!((payload["text"].as_str(), payload["origin"].as_str(), payload["follows"].as_str()),
               (Some("write them"), Some("[from sam · DM] (handed over by jensen)"), Some(item.as_str())));
}

#[tokio::test]
async fn spawn_agent_limits() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &item, "spawn_agent", json!({"prompt": " "})).await, "spawn_agent needs a prompt: the agent's self-contained brief");
    assert_eq!(call(&s, &item, "spawn_agent", json!({"prompt": "go", "cwd": "missing"})).await, "no such directory: missing");
    std::fs::create_dir_all(s.conversation_dir(&convo).join("repo")).unwrap();
    let reply = call(&s, &item, "spawn_agent", json!({"prompt": "go", "cwd": "repo"})).await;
    assert!(reply.starts_with("Spawned agent A-"), "{reply}");
    let (child, cwd): (String, String) = s.db.lock().query_row("SELECT child_conversation_id, cwd FROM spawned_agents", [],
                                                               |r| Ok((r.get(0)?, r.get(1)?))).unwrap();
    assert_eq!(std::path::PathBuf::from(cwd), s.conversation_dir(&convo).join("repo").canonicalize().unwrap());
    let child_item: String = s.db.lock().query_row("SELECT id FROM work_items WHERE conversation_id = ?", [&child], |r| r.get(0)).unwrap();
    s.db.lock().execute("UPDATE work_items SET status = 'claimed' WHERE id = ?", [&child_item]).unwrap();
    assert_eq!(call(&s, &child_item, "spawn_agent", json!({"prompt": "more"})).await,
               "a spawned agent can't spawn agents of its own; do the work in this session");
    call(&s, &item, "spawn_agent", json!({"prompt": "2"})).await;
    call(&s, &item, "spawn_agent", json!({"prompt": "3"})).await;
    assert!(call(&s, &item, "spawn_agent", json!({"prompt": "4"})).await.starts_with("3 agents you spawned are still running"));
}

#[tokio::test]
async fn status_lines() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    s.db.lock().execute("UPDATE conversations SET session_account = 'acct-a' WHERE id = ?", [&convo]).unwrap();
    let text = call(&s, &item, "status", json!({})).await;
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(&lines[..4], ["paused: no", "running: 1 (sample)", "queued: 0", "open tasks: 0"]);
    assert!(lines[4].starts_with("account: acct-a · 5h window 20% used of its 100% cap · resets "), "{}", lines[4]);
    assert!(!lines[4].contains("cap lifts"), "{}", lines[4]);
    assert_eq!(lines[5], "accounts under their cap: 2 of 2");
}

/// acct-a is past the proxy's weekly cap (no rank, week resets in 2 days); acct-b is under both caps.
fn weekly_capped_usage(b_rank: Option<i64>) -> Value {
    let now = chrono::Utc::now().timestamp() as f64;
    json!({"acct-a": {"status": "allowed_warning", "rank": null, "verified": true, "5h": [0.43, now + 3.0 * 3600.0],
                      "7d": [0.84, now + 2.0 * 86400.0], "cap_7d": 0.65},
           "acct-b": {"status": "allowed", "rank": b_rank, "verified": true, "5h": [0.10, now + 4.0 * 3600.0],
                      "7d": [0.30, now + 6.0 * 86400.0], "cap_7d": 0.65}})
}

#[tokio::test]
async fn status_lines_show_the_weekly_cap() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = common::shared_at(dir.path(), &slack, &common::fake_proxy(weekly_capped_usage(Some(1))).await);
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    s.db.lock().execute("UPDATE conversations SET session_account = 'acct-a' WHERE id = ?", [&convo]).unwrap();
    let text = call(&s, &item, "status", json!({})).await;
    let line = text.lines().find(|l| l.starts_with("account: acct-a")).unwrap();
    assert!(line.contains(" · 7-day window 84% used of its 65% cap · week resets "), "{line}");
    assert!(line.ends_with(" · over its weekly cap"), "{line}");
    assert!(text.lines().any(|l| l == "accounts under their cap: 1 of 2"), "{text}");
}

#[tokio::test]
async fn park_waits_for_the_week_to_reset_on_a_weekly_capped_pool() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut usage = weekly_capped_usage(None);
    usage["acct-b"]["7d"][0] = json!(0.70); // past the cap too, its week resetting in 6 days
    let s = common::shared_at(dir.path(), &slack, &common::fake_proxy(usage).await);
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    let reply = call(&s, &item, "park", json!({})).await;
    assert!(reply.starts_with("Parked until ") && reply.contains("(in 47h5"),"acct-a's week resets first: {reply}");
}

#[tokio::test]
async fn cancel_task_cancels_items() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    call(&s, &item, "track_task", json!({"project": "sample", "title": "x", "kind": "bug"})).await;
    assert_eq!(call(&s, &item, "cancel_task", json!({"task_id": "T-0001"})).await, "Cancelled T-0001.");
    let status: String = s.db.lock().query_row("SELECT status FROM work_items WHERE id = ?", [&item], |r| r.get(0)).unwrap();
    assert_eq!(status, "cancelled");
    assert!(call(&s, &item, "cancel_task", json!({"task_id": "T-0001"})).await.starts_with("can't cancel: "));
}

#[tokio::test]
async fn stop_all_and_resume() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    let (_, admin) = turn(&s, Some("sam"), false, None, None);
    assert_eq!(call(&s, &item, "stop_all", json!({})).await,
               "Stopped. Interrupted 2 running sessions; queued messages wait, and nothing new starts until an admin resumes.");
    assert_eq!(agentd::db::get_setting(&s.db.lock(), "paused").unwrap().as_deref(), Some("1"));
    assert_eq!(slack.sent("chat.postMessage")[0]["text"], "Stopped by alice. I'll pick this up again when an admin resumes.");
    assert_eq!(call(&s, &admin, "resume_all", json!({})).await, "Resumed.");
    assert_eq!(agentd::db::get_setting(&s.db.lock(), "paused").unwrap().as_deref(), Some("0"));
}

#[tokio::test]
async fn read_thread_dm_and_thread() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    *slack.messages.lock().unwrap() = vec![json!({"user": "U00000002", "text": "hi"}), json!({"bot_id": "B1", "text": "hello"}),
                                           json!({"user": "U77", "text": "?"})];
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &item, "read_thread", json!({"limit": 500})).await, "alice: hi\nagent: hello\nU77: ?");
    assert_eq!(slack.sent("conversations.replies")[0]["limit"], "200");
}

#[tokio::test]
async fn slack_read_allowlist() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (_, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert!(call(&s, &item, "slack_read", json!({"method": "chat.delete"})).await.starts_with("slack_read can't call 'chat.delete'"));
    let text = call(&s, &item, "slack_read", json!({"method": "conversations.info", "params": {"channel": "C1", "include_num_members": true}})).await;
    assert_eq!(serde_json::from_str::<Value>(&text).unwrap()["channel"]["name"], "general");
    assert_eq!(slack.sent("conversations.info")[0]["include_num_members"], "1");
}

#[tokio::test]
async fn post_files_only_from_folder() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    let folder = s.conversation_dir(&convo);
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::write(folder.join("shot.png"), b"png").unwrap();
    std::fs::write(dir.path().join("secret.txt"), b"no").unwrap();
    let outside = dir.path().join("secret.txt").display().to_string();
    assert_eq!(call(&s, &item, "post_files", json!({"paths": [outside.clone()]})).await, format!("refused: {outside} is outside your session folder"));
    assert_eq!(call(&s, &item, "post_files", json!({"paths": ["../../secret.txt"]})).await, "refused: ../../secret.txt is outside your session folder");
    assert_eq!(call(&s, &item, "post_files", json!({"paths": ["gone.png"]})).await, "no such file: gone.png");
    assert_eq!(call(&s, &item, "post_files", json!({"paths": ["shot.png"], "text": "look"})).await, "posted 1 file(s)");
    assert_eq!(slack.sent("files.completeUploadExternal")[0]["initial_comment"], "look");
}

#[tokio::test]
async fn park_until_and_pool() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert!(call(&s, &item, "park", json!({})).await.starts_with("No account needs waiting out: acct-a, acct-b are under its cap"));
    assert_eq!(call(&s, &item, "park", json!({"until": "soon"})).await, "can't read until 'soon': give HH:MM or an ISO date-time");
    let reply = call(&s, &item, "park", json!({"until": "2020-01-01T00:00", "note": "x"})).await;
    assert!(reply.ends_with("has passed; give a time in the future"), "{reply}");
    let until = (chrono::Utc::now() + chrono::Duration::minutes(90)).format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
    let reply = call(&s, &item, "park", json!({"until": until, "note": "wait for CI"})).await;
    assert!(reply.starts_with("Parked until ") && (reply.contains("(in 1h29m)") || reply.contains("(in 1h30m)")), "{reply}");
    // due: carried on once, however often the tick runs
    let later = chrono::Utc::now() + chrono::Duration::minutes(91);
    agentd::parking::tick(&s, later).await.unwrap();
    agentd::parking::tick(&s, later).await.unwrap();
    let texts: Vec<String> = s.db.lock().prepare("SELECT json_extract(payload, '$.text') FROM work_items WHERE conversation_id = ? AND id != ?")
        .unwrap().query_map([&convo, &item], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
    assert_eq!(texts.len(), 1);
    assert!(texts[0].starts_with("(You parked this conversation at ") && texts[0].ends_with("Your note: wait for CI)"), "{}", texts[0]);
}

#[tokio::test]
async fn tell_project_states() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (build, builder) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert_eq!(call(&s, &builder, "start_project", json!({"name": "Bad Name", "repo": "o/r"})).await,
               "name must be lowercase letters, digits and hyphens, 2 to 41 characters, starting with a letter or digit");
    assert_eq!(call(&s, &builder, "start_project", json!({"name": "app", "repo": "o/app"})).await, "app registered; every session now sees it.");
    s.db.lock().execute("UPDATE work_items SET status = 'done' WHERE id = ?", [&builder]).unwrap();
    let (_, other) = turn(&s, Some("sam"), false, Some("C2"), Some("2.0"));
    assert_eq!(call(&s, &other, "start_project", json!({"name": "app", "repo": "o/app"})).await,
               "app is already being built in https://slack.example/p1.0");
    let reply = call(&s, &other, "tell_project", json!({"name": "app", "message": "add dark mode"})).await;
    assert_eq!(reply, "Passed to app's build. It was idle and starts on this now. Thread: https://slack.example/p1.0");
    let text: String = s.db.lock().query_row("SELECT json_extract(payload, '$.text') FROM work_items WHERE conversation_id = ? AND status = 'queued'",
                                             [&build], |r| r.get(0)).unwrap();
    assert_eq!(text, "add dark mode");
    assert_eq!(call(&s, &other, "finish_project", json!({"name": "app"})).await, "only app's build can finish it");
}

#[test]
fn parse_until_and_device_code_match_python() {
    let e: Value = serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap();
    let tz: chrono_tz::Tz = "Asia/Singapore".parse().unwrap();
    let now = e["parse_until"]["now"].as_f64().unwrap();
    for c in e["parse_until"]["cases"].as_array().unwrap() {
        assert_eq!(agentd::parking::parse_until(c["text"].as_str().unwrap(), now, tz), c["out"].as_f64(), "{}", c["text"]);
    }
    let spans: Vec<String> = [59.0, 60.0, 3599.0, 3600.0, 3660.0, 90061.0, -5.0].iter().map(|s| agentd::parking::span(*s)).collect();
    assert_eq!(json!(spans), e["park_span"]);
    let got: Vec<Option<(String, String)>> = ["To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter \
        the code ABCD-1234 to authenticate.", "nothing here"].iter().map(|t| agentd::cli_login::parse_device_code(t)).collect();
    assert_eq!(json!(got), e["device_code"]);
}

#[tokio::test]
async fn keep_running_sets_an_expiry_for_the_person_who_asked() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let (convo, item) = turn(&s, Some("alice"), false, Some("C1"), Some("1.0"));
    assert!(call(&s, &item, "keep_running", json!({"what": "server", "port": 18098, "hours": 48})).await.contains("kept until"));
    let k = agentd::keeps::get(&s.db.lock(), "server", &format!("{convo}:18098")).unwrap().unwrap();
    assert_eq!(k.set_by.as_deref(), Some("alice"));
    assert!(k.until.is_some_and(|t| t > chrono::Utc::now() + chrono::Duration::hours(47)));
    call(&s, &item, "keep_running", json!({"what": "container", "forever": true})).await;
    assert!(agentd::keeps::get(&s.db.lock(), "container", &convo).unwrap().unwrap().until.is_none());
    call(&s, &item, "keep_running", json!({"what": "container", "clear": true})).await;
    assert!(agentd::keeps::get(&s.db.lock(), "container", &convo).unwrap().is_none());
    assert!(call(&s, &item, "keep_running", json!({"what": "service", "name": "nope"})).await.contains("no service"));
    assert!(call(&s, &item, "keep_running", json!({"what": "stack", "name": "../x", "hours": 1})).await.contains("name"));
}
