//! agentd, the control plane: `serve` runs it; `ask` hands it a turn from the host without Slack (and can wait for
//! the answer); `smoke` is one such turn end to end; `schedules` checks the schedules file (the agent runs it after
//! editing the file); `doctor` checks the install; `kpis` prints the KPIs; `stop` and `resume` pull and release the
//! kill switch when Slack is not an option; `run-turn` runs one turn end to end without the queue, for a live check
//! (started again with the same work item, it carries the turn on); `cleanup` runs one pass of the folders' cleanup
//! now, or with `--dry-run` says what it would remove without touching anything.

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

const USAGE: &str = "usage: agentd serve\n\
    agentd doctor | smoke | stop | resume\n\
    agentd kpis [--days <n>]\n\
    agentd cleanup [--dry-run] [--idle-days <n>]\n\
    agentd ask [--conversation <id> | --new-prefix <prefix>] (--text <text> | --text-file <path or ->) [--source <s>] \
[--origin <o>] [--wait <seconds>] [--expect <word>]\n\
    agentd schedules [--file <path>] [--timezone <zone>]\n\
    agentd run-turn --conversation <id> --work-item <id> --prompt <text>";

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

/// One cleanup pass now: the report as JSON on stdout, its summary on stderr.
async fn cleanup(args: &[String]) -> Result<()> {
    use agentd::cleanup;
    let s = agentd::serve::open_offline()?;
    let idle_days = match flag(args, "--idle-days") {
        Some(n) => n.parse().context("--idle-days takes a whole number")?,
        None => s.config.cleanup.idle_days,
    };
    if idle_days <= 0 {
        bail!("the cleanup is off ([cleanup] idle_days = 0); pass --idle-days to run it anyway");
    }
    let dry_run = args.iter().any(|a| a == "--dry-run");
    // SAFETY: geteuid has no preconditions.
    if !dry_run && unsafe { libc::geteuid() } == 0 {
        bail!("run it as the agent's user (uid 1001), not root: the trash and lock it leaves must stay agentd's");
    }
    let settings = cleanup::Settings::new(&s.paths.work, idle_days);
    let apps: Arc<dyn cleanup::Apps> = Arc::new(cleanup::DockerApps::new(&env("AGENTD_NETWORK", "agent-docker"), &settings)?);
    let report = cleanup::pass(s.db.clone(), apps, settings, chrono::Utc::now(), dry_run).await?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    eprintln!("{}", report.summary());
    Ok(())
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
        Some("ask") => std::process::exit(ask(&args).await?),
        Some("doctor") => std::process::exit(doctor().await),
        Some("smoke") => std::process::exit(smoke().await?),
        Some("kpis") => std::process::exit(kpis(&args)?),
        Some("cleanup") => return cleanup(&args).await,
        Some("stop") => {
            let s = agentd::serve::open_offline()?;
            let cancelled = agentd::queue::stop_all(&s.db.lock(), "local", None)?;
            println!("stopped; cancelled {}", if cancelled.is_empty() { "nothing".into() } else { cancelled.join(", ") });
            return Ok(());
        }
        Some("resume") => {
            agentd::queue::resume_all(&agentd::serve::open_offline()?.db.lock(), "local", None)?;
            println!("resumed");
            return Ok(());
        }
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

/// Queue a turn; with --wait, wait for it and print its answer (exit 1 unless it's done, and contains --expect when
/// given).
async fn ask(args: &[String]) -> Result<i32> {
    let text = match (flag(args, "--text"), flag(args, "--text-file")) {
        (Some(t), _) => t,
        (None, Some(path)) if path == "-" => std::io::read_to_string(std::io::stdin())?,
        (None, Some(path)) => std::fs::read_to_string(path)?,
        _ => bail!(USAGE),
    };
    let s = agentd::serve::open_offline()?;
    let prefix = flag(args, "--new-prefix").unwrap_or_else(|| "V-ask-".into());
    let item = agentd::ask::ask(&s, flag(args, "--conversation").as_deref(), &prefix, &text, flag(args, "--source").as_deref(),
                                flag(args, "--origin").as_deref())?;
    println!("{item}");
    let Some(seconds) = flag(args, "--wait").and_then(|w| w.parse().ok()) else { return Ok(0) };
    let done = agentd::ask::wait(&s, &item, Duration::from_secs(seconds)).await?;
    let result = done.result.unwrap_or(Value::Null);
    let answer = result["text"].as_str().unwrap_or_default().trim();
    println!("{}: {}", done.status, if answer.is_empty() { result["error"].as_str().unwrap_or_default() } else { answer });
    let expected = flag(args, "--expect").is_none_or(|w| answer.to_lowercase().contains(&w.to_lowercase()));
    Ok(if done.status == "done" && expected { 0 } else { 1 })
}

/// Every check of the install, one line each; 1 when any fails.
async fn doctor() -> i32 {
    let paths = agentd::serve::paths();
    let secrets = agentd::scrub::load_secrets(std::path::Path::new(&env("AGENTD_SECRETS", "/run/agent/secrets.env")));
    let proxy = ProxyClient::new(&env("AGENTD_PROXY", "http://172.30.0.1:28090"), SoftCap::from_env(|k| std::env::var(k).ok()));
    let docker = bollard::Docker::connect_with_local_defaults().ok();
    let checks = agentd::doctor::run(&paths, &secrets, Some(proxy), docker, "https://api.github.com",
                                     "https://api.linear.app/graphql").await;
    let (text, ok) = agentd::doctor::report(&checks);
    print!("{text}");
    if ok { 0 } else { 1 }
}

/// One unattended turn end to end (agentd, a container, the account proxy, Claude) with no Slack thread; prints
/// `smoke ok`.
async fn smoke() -> Result<i32> {
    let s = agentd::serve::open_offline()?;
    let item = agentd::ask::ask(&s, None, "V-smoke-", "Reply with the single word pong, and nothing else.", None, None)?;
    println!("queued {item}; waiting for its container (up to 5 min)");
    let done = agentd::ask::wait(&s, &item, Duration::from_secs(300)).await?;
    let result = done.result.unwrap_or(Value::Null);
    let text = result["text"].as_str().unwrap_or_default().trim().to_string();
    if done.status == "done" && text.to_lowercase().contains("pong") {
        println!("smoke ok");
        return Ok(0);
    }
    let why = if text.is_empty() { result["error"].as_str().unwrap_or("no answer").to_string() } else { text.chars().take(500).collect() };
    println!("smoke FAILED: {}: {why}", done.status);
    Ok(1)
}

/// The KPIs over the last --days (7 by default).
fn kpis(args: &[String]) -> Result<i32> {
    let days: i64 = flag(args, "--days").map_or(Ok(7), |d| d.parse()).context("--days must be a whole number")?;
    let s = agentd::serve::open_offline()?;
    let now = chrono::Utc::now();
    let k = agentd::kpis::compute_kpis(&s.db.lock(), now - chrono::Duration::days(days), now)?;
    println!("{}", agentd::kpis::format_kpis(&k, &s.config.name));
    Ok(0)
}
