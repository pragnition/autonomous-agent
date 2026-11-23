//! agentd, the control plane: `serve` runs it; `schedules` checks the schedules file (the agent runs it after editing
//! the file); `run-turn` runs one turn end to end without Slack, for a live check (started again with the same work
//! item, it carries the turn on).

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use agentd::containers::{Containers, Docker, HolderProbe, Host};
use agentd::prepare::TurnSpec;
use agentd::proxy::{ProxyClient, SoftCap};
use agentd::turn::{BoxFuture, RunnerSettings, TurnHost, TurnRunner};
use anyhow::{Context, Result, bail};
use serde_json::Value;

const USAGE: &str = "usage: agentd serve | agentd schedules [--file <path>] [--timezone <zone>] | \
                     agentd run-turn --conversation <id> --work-item <id> --prompt <text>";

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

/// Prints what the turn sends; it has no tools.
struct Print;

impl TurnHost for Print {
    fn event<'a>(&'a self, work_item: &'a str, record: Value) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            let text = record.pointer("/content/0/text").and_then(Value::as_str).unwrap_or_default();
            println!("{work_item} event {} {}", record["type"].as_str().unwrap_or("?"), text.chars().take(80).collect::<String>());
        })
    }
    fn tool<'a>(&'a self, _: &'a str, name: &'a str, _: Value) -> BoxFuture<'a, String> {
        Box::pin(async move { format!("{name} isn't available here") })
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().with_writer(std::io::stderr).init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("serve") => return agentd::serve::serve().await,
        Some("schedules") => std::process::exit(schedules(&args)),
        Some("run-turn") => {}
        _ => bail!(USAGE),
    }
    let (Some(conversation), Some(work_item), Some(prompt)) =
        (flag(&args, "--conversation"), flag(&args, "--work-item"), flag(&args, "--prompt")) else { bail!(USAGE) };
    let root = PathBuf::from(env("AGENTD_ROOT", "/var/agent"));
    let check = root.join("rcp-check");
    std::fs::create_dir_all(&check)?;
    let db = agentd::db::Db::open(&check.join("agent.db"))?;
    let settings = agentd::config::Containers {
        image: env("AGENTD_IMAGE", "autonomous-agent-agent"), memory_gib: 4, cpus: 2.0, pids: 4096,
        idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 3,
    };
    let run_root = PathBuf::from(holder_proto::RUN_ROOT);
    let host = Host { data_dir: std::env::var("AGENTD_DATA_HOST").context("AGENTD_DATA_HOST: the data folder on the host")?,
                      work_volume: env("AGENTD_WORK_VOLUME", "autonomous-agent_agentwork"),
                      network_container: env("AGENTD_NETWORK", "agent-docker"), uid: 1001, run_root: run_root.clone() };
    let containers = Arc::new(Containers::new(Docker::connect()?, HolderProbe { run_root: run_root.clone() }, db.clone(),
                                              settings, host));
    let proxy_url = env("AGENTD_PROXY", "http://172.30.0.1:28091");
    let proxy = ProxyClient::new(&proxy_url, SoftCap::from_env(|k| std::env::var(k).ok()));
    let runner = Arc::new(TurnRunner::new(containers, db, proxy, RunnerSettings {
        claude: vec!["claude".into()], proxy_url, claude_home: root.join("claude"), transcripts: check.join("transcripts"),
        run_root, env_files: vec![], fixed_env: BTreeMap::new(), watchdog: Duration::from_secs(60),
        usage_every: Duration::from_secs(30), soft_cap_grace: Duration::from_secs(1800), max_forks: 2,
    }));
    let host: Arc<dyn TurnHost> = Arc::new(Print);
    let mut ours = None;
    for (item, handle) in runner.reattach(host.clone()).await {
        if item == work_item {
            println!("{item} carrying on");
            ours = Some(handle);
        } else {
            println!("{item} {}", serde_json::to_string(&handle.await?)?);
            runner.reported(&item).await?;
        }
    }
    let result = match ours {
        Some(handle) => handle.await?,
        None => {
            let spec = TurnSpec { work_item: work_item.clone(), conversation: conversation.clone(), prompt,
                                  cwd: check.join("scratch").join(&conversation), model: env("AGENTD_MODEL", "claude-opus-5-5"),
                                  system_prompt: "You are a test session. Do what the message asks.".into(), idle_s: 600,
                                  ..Default::default() };
            runner.run(spec, host).await
        }
    };
    println!("{work_item} result {}", serde_json::to_string(&result)?);
    runner.reported(&work_item).await
}

/// The schedules file's schedules with their next runs, and its problems; 1 when there are problems. The time zone is
/// the config's where it's readable, else the copy agentd leaves for the containers, else UTC.
fn schedules(args: &[String]) -> i32 {
    let file = flag(args, "--file").unwrap_or_else(|| "/data/schedules/schedules.toml".into());
    let zone = flag(args, "--timezone").or_else(|| agentd::config::load(std::path::Path::new("/data/config/agent.toml")).ok().map(|c| c.timezone))
        .or_else(|| std::fs::read_to_string("/run/agent/timezone").ok().map(|t| t.trim().to_string()).filter(|t| !t.is_empty()))
        .unwrap_or_else(|| "UTC".into());
    let tz: chrono_tz::Tz = zone.parse().unwrap_or(chrono_tz::UTC);
    let (out, code) = agentd::schedules::check(std::path::Path::new(&file), tz, chrono::Utc::now().with_timezone(&tz));
    print!("{out}");
    code
}
