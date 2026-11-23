//! The turn runner against `agent-holder serve` (on a task in this process), the fake Claude Code in rust/testdata,
//! and a fake account proxy.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agentd::containers::{ContainerSpec, Containers, Engine, HolderProbe, Host};
use agentd::db::Db;
use agentd::prepare::TurnSpec;
use agentd::proxy::{ProxyClient, SoftCap};
use agentd::turn::{BoxFuture, RunnerSettings, TurnHost, TurnResult, TurnRunner};
use holder_proto::{Entry, LogLine, ToolSpec};
use serde_json::{Value, json};

const FAKE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../testdata/fake_claude.py");
const CONVO: &str = "V-1";
const ORIGIN: &str = "[from Ivan in #general]\n";

/// Containers as holders in this process: `start` runs `agent-holder serve` on a task.
struct LocalEngine {
    root: PathBuf,
    holders: Mutex<HashMap<String, Option<tokio::task::JoinHandle<()>>>>,
}

impl Engine for LocalEngine {
    async fn inspect(&self, name: &str) -> anyhow::Result<Option<bool>> {
        Ok(self.holders.lock().unwrap().get(name).map(|h| h.as_ref().is_some_and(|h| !h.is_finished())))
    }
    async fn create(&self, spec: &ContainerSpec) -> anyhow::Result<()> {
        self.holders.lock().unwrap().insert(spec.name.clone(), None);
        Ok(())
    }
    async fn start(&self, name: &str) -> anyhow::Result<()> {
        let (root, conversation) = (self.root.clone(), name.trim_start_matches("agent-conv-").to_string());
        let task = tokio::spawn(async move { agent_holder::serve::serve(root, conversation).await.unwrap() });
        self.holders.lock().unwrap().insert(name.into(), Some(task));
        Ok(())
    }
    async fn stop(&self, name: &str) -> anyhow::Result<()> {
        if let Some(task) = self.holders.lock().unwrap().get_mut(name).and_then(Option::take) {
            task.abort();
        }
        Ok(())
    }
    async fn remove(&self, name: &str) -> anyhow::Result<()> {
        self.stop(name).await?;
        self.holders.lock().unwrap().remove(name);
        Ok(())
    }
}

#[derive(Default)]
struct TestHost {
    events: Mutex<Vec<Value>>,
    tool_calls: Mutex<Vec<(String, Value)>>,
}

impl TurnHost for TestHost {
    fn event<'a>(&'a self, _work_item: &'a str, record: Value) -> BoxFuture<'a, ()> {
        Box::pin(async move { self.events.lock().unwrap().push(record) })
    }
    fn tool<'a>(&'a self, _work_item: &'a str, name: &'a str, args: Value) -> BoxFuture<'a, String> {
        Box::pin(async move {
            self.tool_calls.lock().unwrap().push((name.to_string(), args));
            "tool says hi".to_string()
        })
    }
}

impl TestHost {
    /// The kinds of record forwarded, sorted: the fake's echo and its first frame come in either order.
    fn kinds(&self) -> Vec<String> {
        let mut kinds: Vec<String> = self.events.lock().unwrap().iter().filter(|e| e["type"] != "RunUsage")
            .map(|e| e["type"].as_str().unwrap().to_string()).collect();
        kinds.sort();
        kinds
    }
    fn said(&self) -> Vec<String> {
        self.events.lock().unwrap().iter().filter(|e| e["type"] == "AssistantMessage")
            .map(|e| e["content"][0]["text"].as_str().unwrap_or_default().to_string()).collect()
    }
}

/// The account proxy: each new session is pinned to the next account in the list (the last one after that).
struct ProxyState {
    accounts: Vec<String>,
    pins: HashMap<String, String>,
    usage: Value,
}

async fn fake_proxy(usage: Value) -> String {
    use axum::extract::{Query, State};
    let state = Arc::new(Mutex::new(ProxyState { accounts: vec!["acct-a".into(), "acct-b".into()],
                                                 pins: HashMap::new(), usage }));
    let app = axum::Router::new()
        .route("/proxy/usage", axum::routing::get(|State(s): State<Arc<Mutex<ProxyState>>>| async move {
            axum::Json(s.lock().unwrap().usage.clone())
        }))
        .route("/proxy/whoami", axum::routing::get(
            |State(s): State<Arc<Mutex<ProxyState>>>, Query(q): Query<HashMap<String, String>>| async move {
                let mut s = s.lock().unwrap();
                let next = s.accounts[s.pins.len().min(s.accounts.len() - 1)].clone();
                let account = s.pins.entry(q["sid"].clone()).or_insert(next).clone();
                axum::Json(json!({"account": account}))
            }))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    url
}

fn usage(a_used: f64) -> Value {
    let reset = chrono::Utc::now().timestamp() as f64 + 4.0 * 3600.0;
    json!({"acct-a": {"status": "ok", "rank": 1, "5h": [a_used, reset]},
           "acct-b": {"status": "ok", "rank": 0, "5h": [0.10, reset]}})
}

type Runner = TurnRunner<LocalEngine, HolderProbe>;

struct Rig {
    dir: tempfile::TempDir,
    db: Db,
    containers: Arc<Containers<LocalEngine, HolderProbe>>,
    proxy_url: String,
    host: Arc<TestHost>,
}

impl Rig {
    async fn new(a_used: f64) -> Rig {
        let dir = tempfile::tempdir().unwrap();
        let run = dir.path().join("run");
        let db = Db::open(&dir.path().join("agent.db")).unwrap();
        let settings = agentd::config::Containers { image: "unused".into(), memory_gib: 1, cpus: 1.0, pids: 100,
            idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 0 };
        let host = Host { data_dir: "/unused".into(), work_volume: "unused".into(), network_container: "unused".into(),
                          uid: 1001, run_root: run.clone() };
        let engine = LocalEngine { root: run.clone(), holders: Mutex::default() };
        let containers = Arc::new(Containers::new(engine, HolderProbe { run_root: run }, db.clone(), settings, host));
        Rig { proxy_url: fake_proxy(usage(a_used)).await, dir, db, containers, host: Arc::default() }
    }

    /// A runner whose Claude Code plays `attempts[k]` on its k-th start.
    fn runner(&self, attempts: &[Vec<Value>], tweak: impl FnOnce(&mut RunnerSettings)) -> Arc<Runner> {
        let scenario = self.dir.path().join("scenario");
        for (k, steps) in attempts.iter().enumerate() {
            std::fs::write(format!("{}.{k}", scenario.display()), steps.iter().map(|s| format!("{s}\n")).collect::<String>())
                .unwrap();
        }
        std::fs::write(&scenario, "").unwrap();
        let mut settings = RunnerSettings {
            claude: vec!["python3".into(), FAKE.into(), scenario.display().to_string()],
            proxy_url: self.proxy_url.clone(),
            claude_home: self.dir.path().join("claude"),
            transcripts: self.dir.path().join("transcripts"),
            run_root: self.dir.path().join("run"),
            env_files: vec![],
            fixed_env: BTreeMap::from([("FAKE_RUN_ROOT".into(), self.dir.path().join("run").display().to_string())]),
            watchdog: Duration::from_millis(100),
            usage_every: Duration::from_millis(100),
            soft_cap_grace: Duration::from_secs(1800),
            max_forks: 2,
        };
        tweak(&mut settings);
        let proxy = ProxyClient::new(&self.proxy_url, SoftCap::default());
        Arc::new(TurnRunner::new(self.containers.clone(), self.db.clone(), proxy, settings))
    }

    fn spec(&self, work_item: &str) -> TurnSpec {
        TurnSpec { work_item: work_item.into(), conversation: CONVO.into(), prompt: format!("{ORIGIN}ping"),
                   origin: ORIGIN.into(), cwd: self.dir.path().join("scratch").join(CONVO), model: "fake".into(),
                   system_prompt: "be useful".into(), idle_s: 30, ..Default::default() }
    }

    fn log(&self, work_item: &str) -> Vec<LogLine> {
        std::fs::read_to_string(self.dir.path().join("run").join(CONVO).join(format!("{work_item}.jsonl")))
            .unwrap_or_default().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
    }

    /// The user messages written to Claude Code, with their tags.
    fn sent(&self, work_item: &str) -> Vec<(Value, Option<String>)> {
        self.log(work_item).into_iter().filter_map(|l| match l.entry {
            Entry::Sent { line, tag } if line["type"] == "user" => Some((line, tag)),
            _ => None,
        }).collect()
    }

    /// Claude Code's arguments at each start.
    fn starts(&self) -> Vec<Vec<String>> {
        std::fs::read_to_string(self.dir.path().join("scenario.starts")).unwrap_or_default().lines()
            .map(|l| serde_json::from_str(l).unwrap()).collect()
    }

    async fn wait_for(&self, what: &str, cond: impl Fn(&Rig) -> bool) {
        for _ in 0..300 {
            if cond(self) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("timed out waiting for {what}");
    }
}

fn spawn_run(runner: &Arc<Runner>, spec: TurnSpec, host: &Arc<TestHost>) -> tokio::task::JoinHandle<TurnResult> {
    let (runner, host) = (runner.clone(), host.clone());
    tokio::spawn(async move { runner.run(spec, host).await })
}

fn session_flag(argv: &[String]) -> String {
    argv.iter().find(|a| a.starts_with("--session-id=") || a.starts_with("--resume=")).unwrap().clone()
}

fn read_prompt() -> Value {
    json!({"wait_stdin": 2}) // the initialize request and the prompt: the prompt has been echoed
}

fn init() -> Value {
    json!({"emit": {"type": "system", "subtype": "init"}})
}

fn say(text: &str) -> Value {
    json!({"emit": {"type": "assistant", "parent_tool_use_id": null,
                    "message": {"role": "assistant", "model": "fake", "id": format!("msg-{text}"),
                                "content": [{"type": "text", "text": text}]}}})
}

fn bash_call() -> Value {
    json!({"emit": {"type": "assistant", "parent_tool_use_id": null,
                    "message": {"role": "assistant", "model": "fake", "id": "msg-bash",
                                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                             "input": {"command": "make test"}}]}}})
}

fn bash_done() -> Value {
    json!({"emit": {"type": "user", "parent_tool_use_id": null,
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                             "content": "ok"}]}}})
}

fn result(text: &str) -> Value {
    json!({"emit": {"type": "result", "subtype": "success", "is_error": false, "duration_ms": 10, "duration_api_ms": 5,
                    "num_turns": 1, "result": text, "total_cost_usd": 0.25,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                              "cache_creation_input_tokens": 20}}})
}

fn failed(status: Option<u16>) -> Value {
    json!({"emit": {"type": "result", "subtype": "error_during_execution", "is_error": true, "duration_ms": 10,
                    "duration_api_ms": 5, "num_turns": 0, "api_error_status": status, "errors": ["boom"]}})
}

fn task(subtype: &str, id: &str, kind: &str) -> Value {
    json!({"emit": {"type": "system", "subtype": subtype, "task_id": id, "task_type": kind, "description": "work",
                    "status": "completed", "output_file": "/tmp/out", "summary": "done", "uuid": format!("{subtype}-{id}")}})
}

#[tokio::test(flavor = "multi_thread")]
async fn plain_turn_done() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), say("pong"), result("pong")]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.text.as_str(), r.turns, r.error.as_deref()), ("done", "pong", 1, None));
    assert_eq!([r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens], [10, 5, 100, 20]);
    assert_eq!((r.cost_usd_est, r.account_code.as_deref(), r.forks), (0.25, Some("acct-a"), 0));
    assert_eq!(session_flag(&rig.starts()[0]), format!("--session-id={}", r.session_id.unwrap()));
    assert_eq!(rig.sent("W-1")[0].0["message"]["content"], format!("{ORIGIN}ping"));
    assert_eq!(rig.host.kinds(), ["AssistantMessage", "ResultMessage", "SystemMessage", "UserMessage"]);
    let transcript = std::fs::read_to_string(rig.dir.path().join("transcripts/W-1.jsonl")).unwrap();
    assert_eq!(transcript.lines().count(), 4);
}

#[tokio::test(flavor = "multi_thread")]
async fn result_before_prompt_echo_is_ignored() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![json!({"defer_echo": true}), init(), read_prompt(), result("stale"),
                                   json!({"flush_echo": true}), say("fresh"), result("fresh")]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.text.as_str()), ("done", "fresh"));
}

#[tokio::test(flavor = "multi_thread")]
async fn midturn_message_read_and_reported() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), say("working"), json!({"wait_stdin": 4}), say("got both"),
                                   result("done")]], |_| {});
    let run = spawn_run(&runner, rig.spec("W-1"), &rig.host);
    rig.wait_for("the first reply", |r| r.host.said().contains(&"working".to_string())).await;
    assert!(runner.deliver("W-1", "item-2", "from a person", true).await);
    assert!(runner.deliver("W-1", "item-3", "from the system", false).await);
    let r = run.await.unwrap();
    assert_eq!((r.outcome.as_str(), r.read.clone()), ("done", vec!["item-2".to_string(), "item-3".to_string()]));
    let sent = rig.sent("W-1");
    let person = sent.iter().find(|(_, tag)| tag.as_deref() == Some("item-2")).unwrap();
    assert_eq!((&person.0["priority"], &person.0["origin"]), (&json!("now"), &json!({"kind": "human"})));
    assert_eq!(person.0["message"]["content"], "from a person");
    let system = sent.iter().find(|(_, tag)| tag.as_deref() == Some("item-3")).unwrap();
    assert!(system.0.get("priority").is_none());
    assert!(!runner.deliver("W-1", "item-4", "too late", true).await, "the turn is over");
}

#[tokio::test(flavor = "multi_thread")]
async fn background_agent_makes_result_interim() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), task("task_started", "a1", "local_agent"), result("first"),
                                   json!({"sleep_ms": 300}), task("task_notification", "a1", "local_agent"),
                                   say("second"), result("second")]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.text.as_str(), r.turns), ("done", "first\n\nsecond", 2));
}

#[tokio::test(flavor = "multi_thread")]
async fn moved_shell_holds_turn_until_notification() {
    let rig = Rig::new(0.1).await;
    let moved = json!({"emit": {"type": "user", "parent_tool_use_id": null,
                                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                                                         "content": "moved to the background"}]},
                                "tool_use_result": {"backgroundedToDeliverMessage": true, "backgroundTaskId": "b1"}}});
    let runner = rig.runner(&[vec![init(), read_prompt(), task("task_started", "b1", "local_bash"),
                                   json!({"wait_stdin": 3}), moved, say("ok"), result("ok"), json!({"sleep_ms": 300}),
                                   task("task_notification", "b1", "local_bash"), say("finished"),
                                   result("finished")]], |_| {});
    let run = spawn_run(&runner, rig.spec("W-1"), &rig.host);
    rig.wait_for("the shell", |r| r.host.kinds().contains(&"TaskStartedMessage".to_string())).await;
    assert!(runner.deliver("W-1", "item-2", "stop and answer", true).await);
    let r = run.await.unwrap();
    assert_eq!((r.text.as_str(), r.read.clone()), ("ok\n\nfinished", vec!["item-2".to_string()]));
}

#[tokio::test(flavor = "multi_thread")]
async fn stalled_after_idle_s() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), json!({"wait_interrupt": true}), failed(None)]], |_| {});
    let r = runner.run(TurnSpec { idle_s: 1, ..rig.spec("W-1") }, rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.error.as_deref()), ("stalled", Some("no progress for 1 s")));
}

#[tokio::test(flavor = "multi_thread")]
async fn timeout_at_budget() {
    let rig = Rig::new(0.1).await;
    let busy: Vec<Value> = (0..8).flat_map(|i| [say(&i.to_string()), json!({"sleep_ms": 300})]).collect();
    let steps = [vec![init(), read_prompt()], busy, vec![json!({"wait_interrupt": true}), failed(None)]].concat();
    let runner = rig.runner(&[steps], |_| {});
    let r = runner.run(TurnSpec { timeout_s: Some(1), ..rig.spec("W-1") }, rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.error.as_deref()), ("timeout", Some("no result within 1 s")));
}

#[tokio::test(flavor = "multi_thread")]
async fn crash_resumes_once_then_errors_keeping_session() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), say("working"), json!({"exit": 1})],
                              vec![init(), read_prompt(), json!({"exit": 1})]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    let sid = r.session_id.clone().unwrap();
    assert_eq!((r.outcome.as_str(), r.error.as_deref()), ("error", Some("the session ended without a result")));
    let starts = rig.starts();
    assert_eq!(starts.len(), 2, "resumed once");
    assert_eq!((session_flag(&starts[0]), session_flag(&starts[1])),
               (format!("--session-id={sid}"), format!("--resume={sid}")));
    let second = rig.sent("W-1")[1].0["message"]["content"].as_str().unwrap().to_string();
    assert!(second.starts_with(&format!("{ORIGIN}(Your machine restarted partway")) && second.ends_with("ping"), "{second}");
}

#[tokio::test(flavor = "multi_thread")]
async fn rejected_account_moves_to_fresh_session_with_handoff() {
    let rig = Rig::new(0.1).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), say("on a"), failed(Some(403))],
                              vec![init(), read_prompt(), say("on b"), result("done on b")]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.text.as_str(), r.forks, r.account_code.as_deref()),
               ("done", "done on b", 1, Some("acct-b")));
    let starts = rig.starts();
    let first = session_flag(&starts[0]).trim_start_matches("--session-id=").to_string();
    assert!(session_flag(&starts[1]).starts_with("--session-id=") && !session_flag(&starts[1]).contains(&first));
    let second = rig.sent("W-1")[1].0["message"]["content"].as_str().unwrap().to_string();
    assert!(second.starts_with(&format!("{ORIGIN}(This is a fresh session. Your earlier session in this conversation, \
                                         {first}, ran on another Claude account")), "{second}");
    assert!(second.contains("[1] on a") && second.ends_with("ping"), "{second}");
}

fn interrupt_seq(log: &[LogLine]) -> u64 {
    log.iter().find(|l| matches!(&l.entry, Entry::Sent { line, .. } if line["request"]["subtype"] == "interrupt"))
        .expect("an interrupt").seq
}

#[tokio::test(flavor = "multi_thread")]
async fn soft_cap_moves_only_at_quiet_moment() {
    let rig = Rig::new(0.90).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), bash_call(), json!({"sleep_ms": 800}), bash_done(),
                                   json!({"wait_interrupt": true}), failed(None)],
                              vec![init(), read_prompt(), say("on b"), result("moved")]], |_| {});
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.outcome.as_str(), r.text.as_str(), r.forks), ("done", "moved", 1));
    let log = rig.log("W-1");
    let tool_done = log.iter().find(|l| matches!(&l.entry, Entry::Out { line } if line.contains("tool_result"))).unwrap();
    assert!(interrupt_seq(&log) > tool_done.seq, "interrupted only once the command had finished");
}

#[tokio::test(flavor = "multi_thread")]
async fn soft_cap_forced_after_30_minutes() {
    let rig = Rig::new(0.90).await;
    let runner = rig.runner(&[vec![init(), read_prompt(), bash_call(), json!({"wait_interrupt": true}), failed(None)],
                              vec![init(), read_prompt(), result("moved anyway")]],
                            |s| s.soft_cap_grace = Duration::from_millis(500));
    let r = runner.run(rig.spec("W-1"), rig.host.clone()).await;
    assert_eq!((r.text.as_str(), r.forks), ("moved anyway", 1));
    assert!(!rig.log("W-1").iter().any(|l| matches!(&l.entry, Entry::Out { line } if line.contains("tool_result"))));
}

#[tokio::test(flavor = "multi_thread")]
async fn restart_after_result_reports_once() {
    let rig = Rig::new(0.1).await;
    let steps = vec![init(), read_prompt(), say("pong"), result("pong")];
    let first = rig.runner(&[steps.clone()], |_| {}).run(rig.spec("W-1"), rig.host.clone()).await;
    let events = rig.host.events.lock().unwrap().len();
    let after_restart = rig.runner(&[steps.clone()], |_| {});
    let reattached = after_restart.reattach(rig.host.clone()).await;
    assert_eq!(reattached.len(), 1);
    let (work_item, handle) = reattached.into_iter().next().unwrap();
    assert_eq!((work_item.as_str(), handle.await.unwrap()), ("W-1", first));
    assert_eq!(rig.host.events.lock().unwrap().len(), events, "nothing sent twice");
    assert_eq!(rig.starts().len(), 1, "Claude Code not started again");
    after_restart.reported("W-1").await.unwrap();
    assert!(rig.runner(&[steps], |_| {}).reattach(rig.host.clone()).await.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn reattach_mid_turn_reads_missed_output() {
    let rig = Rig::new(0.1).await;
    let steps = vec![init(), read_prompt(), say("before"), json!({"sleep_ms": 1500}), say("after"), result("after")];
    let run = spawn_run(&rig.runner(&[steps.clone()], |_| {}), rig.spec("W-1"), &rig.host);
    rig.wait_for("the first reply", |r| r.host.said() == ["before"]).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    run.abort();
    let _ = run.await;
    let reattached = rig.runner(&[steps], |_| {}).reattach(rig.host.clone()).await;
    let r = reattached.into_iter().next().unwrap().1.await.unwrap();
    assert_eq!((r.outcome.as_str(), r.text.as_str()), ("done", "after"));
    assert_eq!(rig.host.said(), ["before", "after"]);
    assert_eq!(rig.host.kinds(), ["AssistantMessage", "AssistantMessage", "ResultMessage", "SystemMessage", "UserMessage"]);
    assert_eq!(rig.starts().len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn tool_call_replayed_after_restart_runs_once() {
    let rig = Rig::new(0.1).await;
    let steps = vec![init(), read_prompt(), json!({"tool": {"name": "post_update", "args": {"text": "hi"}}}),
                     json!({"sleep_ms": 1000}), say("bye"), result("bye")];
    let tools = vec![ToolSpec { name: "post_update".into(), description: "post".into(), input_schema: json!({}) }];
    let spec = TurnSpec { tools, ..rig.spec("W-1") };
    let run = spawn_run(&rig.runner(&[steps.clone()], |_| {}), spec, &rig.host);
    rig.wait_for("the tool's answer", |r| r.host.said().contains(&"tool says hi".to_string())).await;
    run.abort();
    let _ = run.await;
    let reattached = rig.runner(&[steps], |_| {}).reattach(rig.host.clone()).await;
    let r = reattached.into_iter().next().unwrap().1.await.unwrap();
    assert_eq!((r.outcome.as_str(), r.text.as_str()), ("done", "bye"));
    assert_eq!(*rig.host.tool_calls.lock().unwrap(), [("post_update".to_string(), json!({"text": "hi"}))]);
    assert_eq!(rig.host.said(), ["tool says hi", "bye"]);
}

