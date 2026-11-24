//! How Slack messages become turns and turns' results go back, against a fake Slack (a port of core's
//! conversations.py behaviour; errors keep the session, from the spec).

mod common;

use agentd::conversations as cv;
use agentd::queue;
use common::*;
use serde_json::{Value, json};

fn dm(text: &str, ts: &str) -> Value {
    json!({"type": "message", "channel": "D1", "channel_type": "im", "user": "U00000001", "text": text, "ts": ts})
}

fn items(s: &agentd::shared::Shared) -> Vec<queue::WorkItem> {
    let c = s.db.lock();
    let ids: Vec<String> = c.prepare("SELECT id FROM work_items ORDER BY created_at, rowid").unwrap()
        .query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
    ids.iter().map(|id| queue::get(&c, id).unwrap().unwrap()).collect()
}

fn convo(s: &agentd::shared::Shared, id: &str) -> cv::Convo {
    cv::row(&s.db.lock(), id).unwrap().unwrap()
}

#[tokio::test]
async fn dm_starts_conversation_and_reacts() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    cv::handle_slack(&s, dm("hi there", "100.1")).await.unwrap();
    let items = items(&s);
    assert_eq!(items.len(), 1);
    let p = &items[0].payload;
    assert_eq!((p["text"].as_str(), p["actor"].as_str(), p["source"].as_str(), p["requester"].as_str(), p["message_ts"].as_str()),
               (Some("hi there"), Some("sam"), Some("slack"), Some("sam"), Some("100.1")));
    assert_eq!(p["origin"], "[from Name U00000001 (sam, GitHub @sam-example) · DM · https://slack.example/p100.1]");
    assert_eq!(p["model"], "claude-opus-5-5");
    let c = convo(&s, items[0].conversation_id.as_deref().unwrap());
    assert_eq!((c.channel.as_deref(), c.thread_ts.as_deref(), c.project.as_deref()), (Some("D1"), Some("100.1"), None));
    assert_eq!(slack.reactions(), ["+eyes"]);
}

#[tokio::test]
async fn thread_reply_goes_to_waiting_conversation() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let task = {
        let c = s.db.lock();
        let t = agentd::tasks::create(&c, agentd::tasks::NewTask { project: "sample".into(), title: "t".into(),
            kind: "bug".into(), source: "slack".into(), ..Default::default() }).unwrap();
        agentd::tasks::transition(&c, &t.id, "working".parse().unwrap(), "a", None).unwrap();
        agentd::tasks::transition(&c, &t.id, "needs_input".parse().unwrap(), "a", None).unwrap();
        t.id
    };
    let first = cv::create_conversation(&s.db.lock(), Some("C1"), Some("50.0"), None, None, true, None).unwrap();
    let asking = cv::create_conversation(&s.db.lock(), Some("C1"), Some("50.0"), None, Some(&task), false, None).unwrap();
    s.db.lock().execute("UPDATE conversations SET waiting = 1 WHERE id = ?", [&asking]).unwrap();
    let reply = json!({"type": "message", "channel": "C1", "user": "U00000002", "text": "yes", "ts": "60.0", "thread_ts": "50.0"});
    cv::handle_slack(&s, reply.clone()).await.unwrap();
    assert_eq!(items(&s)[0].conversation_id.as_deref(), Some(asking.as_str()));
    assert!(!convo(&s, &asking).waiting);
    assert_eq!(agentd::tasks::get(&s.db.lock(), &task).unwrap().unwrap().status.as_str(), "working");
    let next = json!({"type": "message", "channel": "C1", "user": "U00000002", "text": "and", "ts": "61.0", "thread_ts": "50.0"});
    cv::handle_slack(&s, next).await.unwrap();
    assert_eq!(items(&s)[1].conversation_id.as_deref(), Some(first.as_str()), "then the thread's primary conversation");
}

#[tokio::test]
async fn reply_in_own_thread_starts_new() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    // the agent posted a top-level message itself: its own event notes the thread
    cv::handle_slack(&s, json!({"type": "message", "channel": "C2", "user": "UBOT", "text": "heads up", "ts": "70.0"})).await.unwrap();
    cv::handle_slack(&s, json!({"type": "message", "channel": "C2", "user": "U00000001", "text": "why?", "ts": "71.0",
                                "thread_ts": "70.0"})).await.unwrap();
    let items = items(&s);
    assert_eq!(items.len(), 1);
    assert!(items[0].payload["origin"].as_str().unwrap().contains("· #general thread you started ·"));
    assert_eq!(convo(&s, items[0].conversation_id.as_deref().unwrap()).thread_ts.as_deref(), Some("70.0"));
}

#[tokio::test]
async fn stranger_gets_ephemeral() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    cv::handle_slack(&s, json!({"type": "message", "channel": "D9", "channel_type": "im", "user": "U99", "text": "hi", "ts": "1.0"})).await.unwrap();
    assert!(items(&s).is_empty());
    assert_eq!(slack.sent("chat.postEphemeral")[0]["text"], cv::NOT_AUTHORISED);
    // a channel message that doesn't mention the agent isn't for it
    cv::handle_slack(&s, json!({"type": "message", "channel": "C3", "user": "U00000001", "text": "lunch?", "ts": "2.0"})).await.unwrap();
    assert!(items(&s).is_empty());
}

#[tokio::test]
async fn retried_event_handled_once() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let event = dm("hello", "100.2");
    let first = shared(dir.path(), &slack).await;
    cv::handle_slack(&first, event.clone()).await.unwrap();
    let after_restart = shared(dir.path(), &slack).await;
    cv::handle_slack(&after_restart, event.clone()).await.unwrap();
    cv::handle_slack(&after_restart, json!({"type": "app_mention", "channel": "D1", "user": "U00000001", "text": "<@UBOT> hello",
                                           "ts": "100.2"})).await.unwrap();
    assert_eq!(items(&after_restart).len(), 1);
}

#[tokio::test]
async fn use_model_switches_or_explains() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    cv::handle_slack(&s, dm("please use opus 4.6 for this", "1.0")).await.unwrap();
    let id = items(&s)[0].conversation_id.clone().unwrap();
    assert_eq!(convo(&s, &id).model.as_deref(), Some("claude-opus-4-6"));
    assert_eq!(items(&s)[0].payload["model"], "claude-opus-4-6", "from this message's turn on");
    cv::handle_slack(&s, json!({"type": "message", "channel": "D1", "channel_type": "im", "user": "U00000001",
                                "text": "use haiku 4.5", "ts": "2.0", "thread_ts": "1.0"})).await.unwrap();
    assert_eq!(convo(&s, &id).model.as_deref(), Some("claude-opus-4-6"), "nothing changes");
    assert!(slack.sent("chat.postMessage")[0]["text"].as_str().unwrap().starts_with("I can't switch this conversation to haiku 4.5"));
}

async fn ran(s: &agentd::shared::Shared, event: Value) -> queue::WorkItem {
    cv::handle_slack(s, event).await.unwrap();
    let item = queue::claim(&s.db.lock()).unwrap().unwrap();
    cv::on_start(s, &item).await;
    item
}

async fn finish(s: &agentd::shared::Shared, item: &queue::WorkItem, result: Value) {
    queue::complete(&s.db.lock(), &item.id, &result).unwrap();
    let item = queue::get(&s.db.lock(), &item.id).unwrap().unwrap();
    cv::on_result(s, &item, &result).await.unwrap();
}

#[tokio::test]
async fn result_posts_reply_and_ticks() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let item = ran(&s, dm("hi", "1.0")).await;
    finish(&s, &item, json!({"outcome": "done", "text": " **pong** core-secret-0123456789 ", "session_id": "S1", "account_code": "a"})).await;
    let posts = slack.sent("chat.postMessage");
    assert_eq!((posts[0]["text"].as_str(), posts[0]["thread_ts"].as_str()), (Some("**pong** [redacted]"), Some("1.0")));
    assert_eq!(slack.reactions(), ["+eyes", "-eyes", "+hourglass_flowing_sand", "-hourglass_flowing_sand", "+white_check_mark"]);
    let c = convo(&s, item.conversation_id.as_deref().unwrap());
    assert_eq!((c.session_id.as_deref(), c.session_account.as_deref()), (Some("S1"), Some("a")));
}

#[tokio::test]
async fn error_keeps_session() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let item = ran(&s, dm("hi", "1.0")).await;
    finish(&s, &item, json!({"outcome": "done", "text": "ok", "session_id": "S1", "account_code": "a"})).await;
    let item = ran(&s, json!({"type": "message", "channel": "D1", "channel_type": "im", "user": "U00000001", "text": "more",
                               "ts": "2.0", "thread_ts": "1.0"})).await;
    finish(&s, &item, json!({"outcome": "error", "text": "", "error": "None ['boom']"})).await;
    let c = convo(&s, item.conversation_id.as_deref().unwrap());
    assert_eq!(c.session_id.as_deref(), Some("S1"), "the next turn resumes it or hands off");
    assert!(slack.sent("chat.postMessage").last().unwrap()["text"].as_str().unwrap()
            .starts_with("That failed before I could finish it:\n```\nNone ['boom']"));
    assert_eq!(slack.reactions().last().unwrap(), "+warning");
}

#[tokio::test]
async fn limit_cut_waits_and_resumes_once() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let item = ran(&s, dm("hi", "1.0")).await;
    finish(&s, &item, json!({"outcome": "error", "text": "", "error": "429 ['rate limited']", "session_id": "S1"})).await;
    assert!(slack.sent("chat.postMessage")[0]["text"].as_str().unwrap().starts_with("A usage limit cut me off"));
    assert_eq!(slack.reactions().last().unwrap(), "+hourglass_flowing_sand", "the ⏳ stays");
    let resumed = cv::resume_after_limit(&s, &item.id).unwrap().unwrap();
    let new = queue::get(&s.db.lock(), &resumed).unwrap().unwrap();
    assert_eq!((new.payload["continuation"].as_i64(), new.payload["limit_resume"].as_i64()), (Some(1), Some(1)));
    assert!(new.payload["text"].as_str().unwrap().starts_with("(A usage limit cut your last turn off: 429"));
    assert!(cv::resume_after_limit(&s, &item.id).unwrap().is_none(), "once");
}

#[tokio::test]
async fn build_retries_three_times() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let mut item = ran(&s, dm("build it", "1.0")).await;
    let id = item.conversation_id.clone().unwrap();
    s.db.lock().execute("INSERT INTO projects(name, repo, conversation_id, status, created_at, updated_at) \
                         VALUES ('app', 'o/app', ?, 'active', 'x', 'x')", [&id]).unwrap();
    for failures in 1..=3 {
        finish(&s, &item, json!({"outcome": "stalled", "error": "no progress for 1800 s"})).await;
        item = queue::claim(&s.db.lock()).unwrap().unwrap();
        assert_eq!((item.payload["failures"].as_i64(), item.payload["continuation"].as_i64()), (Some(failures), Some(failures)));
        assert!(item.payload["text"].as_str().unwrap().starts_with("(Your last turn got stuck: no progress"));
    }
    finish(&s, &item, json!({"outcome": "stalled", "error": "no progress for 1800 s"})).await;
    assert!(queue::claim(&s.db.lock()).unwrap().is_none(), "the fourth failure stops it");
    assert_eq!(slack.reactions().last().unwrap(), "+warning");
}

#[tokio::test]
async fn spawned_agent_reports_to_parent() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let parent = ran(&s, dm("research", "1.0")).await;
    let parent_convo = parent.conversation_id.clone().unwrap();
    let child = cv::create_conversation(&s.db.lock(), None, None, None, None, false, None).unwrap();
    agentd::spawned::record(&s.db.lock(), "A-00000001", &parent_convo, &child, &parent.id, "look into it", None,
                            &json!({"actor": "sam", "origin": "[from sam]", "source": "slack", "requester": "sam"})).unwrap();
    let payload = agentd::payloads::turn(&s, &convo(&s, &child), "brief", Some("sam"), false).unwrap();
    queue::enqueue(&s.db.lock(), Some(&child), "turn", &payload).unwrap();
    finish(&s, &parent, json!({"outcome": "done", "text": "spawned it"})).await;
    let child_item = queue::claim(&s.db.lock()).unwrap().unwrap();
    assert_eq!(agentd::spawned::running_count(&s.db.lock(), &parent_convo).unwrap(), 1);
    finish(&s, &child_item, json!({"outcome": "done", "text": "found the bug"})).await;
    let report = queue::claim(&s.db.lock()).unwrap().unwrap();
    assert_eq!(report.conversation_id.as_deref(), Some(parent_convo.as_str()));
    assert!(report.payload["text"].as_str().unwrap().contains("Agent A-00000001, which you spawned with spawn_agent, finished."));
    assert_eq!(report.payload["origin"], "[from sam] (report from agent A-00000001)");
    assert_eq!(agentd::spawned::running_count(&s.db.lock(), &parent_convo).unwrap(), 0);
}

#[tokio::test]
async fn scheduled_failure_reports_in_channel() {
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let s = shared(dir.path(), &slack).await;
    let id = cv::create_conversation(&s.db.lock(), None, None, None, None, true, None).unwrap();
    let mut payload = agentd::payloads::turn(&s, &convo(&s, &id), "nightly report", None, true).unwrap();
    payload["source"] = json!("schedule");
    payload["report_to"] = json!({"channel": "sam", "schedule": "nightly"});
    queue::enqueue(&s.db.lock(), Some(&id), "turn", &payload).unwrap();
    let item = queue::claim(&s.db.lock()).unwrap().unwrap();
    finish(&s, &item, json!({"outcome": "timeout", "error": "no result within 21600 s"})).await;
    let post = &slack.sent("chat.postMessage")[0];
    assert_eq!(post["channel"], "D-U00000001", "a person key means their DM");
    assert_eq!(post["text"], "My nightly run didn't finish. I ran out of time before finishing that (no result within 21600 s).");
    assert_eq!(convo(&s, &id).channel.as_deref(), Some("D-U00000001"), "replies there continue the run");
}

#[test]
fn pure_rules_match_python() {
    let e: Value = serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap();
    let config = agentd::config::load(&std::path::Path::new(REPO).join("examples/config/agent.toml")).unwrap();
    assert_eq!(e["origin_line"], json!([cv::origin_line("DM", Some("Sam (sam)"), Some("https://x/1")),
                                         cv::origin_line("Linear SAM-7", None, None), cv::origin_line("#general thread", Some("sam"), None)]));
    for c in e["model_named"].as_array().unwrap() {
        let (ok, model) = cv::model_named(&config.models, c["name"].as_str().unwrap());
        assert_eq!(json!([ok, model]), c["out"], "{}", c["name"]);
    }
    for c in e["use_model"].as_array().unwrap() {
        let text = c["text"].as_str().unwrap();
        assert_eq!(json!(cv::use_model(text)), c["match"], "{text}");
        assert_eq!(cv::says_use_fable(text), c["fable"].as_bool().unwrap(), "{text}");
    }
    assert_eq!(cv::unknown_model_reply(&config.models, " gpt-4 "), e["unknown_model_reply"].as_str().unwrap());
    for c in e["failure_reply"].as_array().unwrap() {
        assert_eq!(cv::failure_reply(c["outcome"].as_str().unwrap(), c["error"].as_str()), c["out"].as_str().unwrap());
    }
    for c in e["limit"].as_array().unwrap() {
        assert_eq!(cv::cut_by_limit("error", c["error"].as_str()), c["cut"].as_bool().unwrap());
        assert_eq!(cv::limit_wait_reply(c["error"].as_str()), c["reply"].as_str().unwrap());
    }
    let files = json!({"files": [{"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 10}, {"id": "F2", "mode": "tombstone"},
                                 {"name": "no id"}, {"id": "F3", "size": "7"}]});
    assert_eq!(json!(cv::message_files(&files)), e["message_files"]);
    let row = json!({"id": "A-1234abcd", "prompt": format!("Look into   the flaky\ntest {}", "p".repeat(400))});
    let reports = [agentd::spawned::report_text(&row, "done", "done", "found it"),
                   agentd::spawned::report_text(&row, "failed", "stalled", ""),
                   agentd::spawned::report_text(&row, "done", "done", &"r".repeat(20_005))];
    assert_eq!(json!(reports), e["report_text"]);
    let mut task = agentd::tasks::Task { id: "T-0007".into(), project: "p".into(), title: "Fix it".into(), description: String::new(),
        kind: "bug".into(), source: "slack".into(), source_ref: None, requester: None, owner: None,
        status: "ready_for_review".parse().unwrap(), branch: None, pr_number: None, pr_url: Some("https://github.com/o/r/pull/1".into()),
        linear_issue_id: None, slack_channel: None, slack_thread_ts: None, status_message_ts: None, created_at: String::new(),
        updated_at: String::new() };
    let first = agentd::notifier::status_text(&task);
    (task.status, task.pr_url) = ("merged".parse().unwrap(), None);
    assert_eq!(json!([first, agentd::notifier::status_text(&task)]), e["status_text"]);
}
