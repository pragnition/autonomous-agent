//! `agentd serve`: what core and the runner ran, as one process. Settings come from the environment (see `Env`);
//! core's secrets from its own file, never the environment.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::{Value, json};
use tokio::sync::mpsc;

use crate::admission::Admission;
use crate::containers::{Containers, Docker, HolderProbe, Host};
use crate::dispatch::{self, Dispatcher};
use crate::github::GitHub;
use crate::linear::{Linear, LinearApp, LinearClient};
use crate::proxy::{ProxyClient, SoftCap};
use crate::schedules::Scheduler;
use crate::shared::{Paths, Shared};
use crate::slack::{self, Slack};
use crate::turn::{RunnerSettings, TurnRunner};
use crate::azure_rates::{self, AzureRates};
use crate::host::{HostStore, Sampler, SamplerSettings};
use crate::labeler::{self, Labeler};
use crate::{conversations, dashboard, kpis, linear, meter, parking, pollers, scrub, task_record, usage_watch};

const HEALTH_PORT: u16 = 8080;
/// Linear's webhook and OAuth callback, the only thing Tailscale Funnel exposes.
const PUBLIC_PORT: u16 = 8085;

fn env(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

/// The data folders, from the environment.
pub fn paths() -> Paths {
    let data = PathBuf::from(env("AGENTD_DATA", "/data"));
    Paths { config: data.join("config"), data: data.clone(), work: PathBuf::from(env("AGENTD_WORK", "/var/agent")),
                        defaults: PathBuf::from(env("AGENTD_DEFAULTS", "/app/src/agent/defaults")),
                        plugins: PathBuf::from(env("AGENTD_PLUGINS", "/app/src/agent/plugins")) }
}

/// agentd's state for a command run beside `agentd serve` (no Slack calls, no Linear).
pub fn open_offline() -> Result<Shared> {
    Shared::open(paths(), BTreeMap::new(), Slack::new("", slack::API), None,
                 ProxyClient::new(&env("AGENTD_PROXY", "http://172.30.0.1:28090"), SoftCap::default()), None)
}

pub async fn serve() -> Result<()> {
    let paths = paths();
    let data = paths.data.clone();
    let secrets = scrub::load_secrets(Path::new(&env("AGENTD_SECRETS", "/run/agent/secrets.env")));
    let secret = |k: &str| secrets.get(k).filter(|v| !v.is_empty()).cloned();
    let slack = Slack::new(&secret("SLACK_BOT_TOKEN").context("SLACK_BOT_TOKEN is missing from core's secrets")?,
                           &env("AGENTD_SLACK_API", slack::API));
    let bot = slack.auth_test().await.context("Slack auth.test")?;
    let proxy = ProxyClient::new(&env("AGENTD_PROXY", "http://172.30.0.1:28090"), SoftCap::from_env(|k| std::env::var(k).ok()));
    let github = secret("GITHUB_READ_TOKEN").map(|t| GitHub::new(&t, "https://api.github.com"));
    let mut shared = Shared::open(paths, secrets.clone(), slack, Some(bot), proxy, github)?;
    let app = (!shared.config.linear.public_url.is_empty()).then(|| secret("LINEAR_CLIENT_ID")).flatten().map(|id| {
        LinearApp::new(&id, &secret("LINEAR_CLIENT_SECRET").unwrap_or_default(),
                       &format!("{}/linear/oauth/callback", shared.config.linear.public_url.trim_end_matches('/')),
                       data.join("runner").join("linear.env"), "https://api.linear.app")
    });
    if secret("LINEAR_API_KEY").is_some() || app.is_some() {
        shared.linear = Some(Linear { client: LinearClient::new(&secret("LINEAR_API_KEY").unwrap_or_default(), "https://api.linear.app/graphql"),
                                      app, webhook_secret: secret("LINEAR_WEBHOOK_SECRET").unwrap_or_default() });
    }
    let s = Arc::new(shared);
    prepare_dirs(&s)?;

    let containers = Arc::new(Containers::new(Docker::connect()?, HolderProbe { run_root: holder_proto::RUN_ROOT.into() }, s.db.clone(),
        s.config.containers.clone(), Host {
            data_dir: std::env::var("AGENTD_DATA_HOST").context("AGENTD_DATA_HOST: the data folder's path on the Docker host")?,
            work_volume: env("AGENTD_WORK_VOLUME", "autonomous-agent_agentwork"), network_container: env("AGENTD_NETWORK", "agent-docker"),
            uid: 1001, run_root: holder_proto::RUN_ROOT.into() }));
    let name = env("AGENT_NAME", "agent");
    let email = env("AGENT_GIT_EMAIL", &format!("{name}@users.noreply.github.com"));
    let fixed_env = BTreeMap::from([("GIT_AUTHOR_NAME".into(), name.clone()), ("GIT_COMMITTER_NAME".into(), name),
        ("GIT_AUTHOR_EMAIL".into(), email.clone()), ("GIT_COMMITTER_EMAIL".into(), email),
        // sessions and their helpers compact near this many tokens instead of near the model's whole window
        ("CLAUDE_CODE_AUTO_COMPACT_WINDOW".into(), env("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000"))]);
    let work = s.paths.work.clone();
    let runner = Arc::new(TurnRunner::new(containers.clone(), s.db.clone(), s.proxy.clone(), RunnerSettings {
        claude: vec!["claude".into()], proxy_url: env("AGENTD_SESSION_PROXY", "http://172.30.0.1:28091"),
        claude_home: work.join("claude"), transcripts: work.join("transcripts"), run_root: holder_proto::RUN_ROOT.into(),
        env_files: dispatch::env_files(&s), fixed_env, watchdog: Duration::from_secs(60), usage_every: Duration::from_secs(30),
        soft_cap_grace: Duration::from_secs(1800), max_forks: 2 }));
    let admission = Admission { max_sessions: s.config.limits.max_concurrent_sessions.max(0) as usize,
                                min_free_gib: s.config.containers.min_free_gib.max(0) as u64, meminfo: "/proc/meminfo".into() };
    tokio::spawn(Dispatcher::new(s.clone(), runner, containers.clone(), admission).run());

    let scheduler = Arc::new(Scheduler::new(data.join("schedules").join("schedules.toml")));
    every(&s, 60, "Linear intake", |s| async move { linear::intake(&s).await.map(drop) });
    every(&s, 60, "schedules", move |s| { let sc = scheduler.clone(); async move { sc.tick(&s, chrono::Utc::now()).await.map(drop) } });
    every(&s, 60, "usage watch", |s| async move { usage_watch::tick(&s).await });
    every(&s, 60, "parks", |s| async move { parking::tick(&s, chrono::Utc::now()).await });
    every(&s, 600, "Linear token", |s| async move {
        match s.linear.as_ref().and_then(|l| l.app.as_ref()) {
            Some(app) => app.token(&s).await.map(drop), // kept fresh for the sessions even when agentd makes no Linear calls
            None => Ok(()),
        }
    });
    every(&s, 60, "container sweep", move |_| { let c = containers.clone(); async move { c.sweep(chrono::Utc::now()).await } });

    // the task record: its files catch up with whatever was labeled before; Haiku labels what was answered since
    if let Err(e) = task_record::export_all(&s.db.lock(), &data.join("tasks"), &s.config.timezone) {
        tracing::warn!("writing the task files failed: {e:#}");
    }
    let own_proxy = env("AGENTD_PROXY", "http://172.30.0.1:28090");
    let labeler = Arc::new(Labeler::new(labeler::haiku(&own_proxy), labeler::proxy_capacity(Arc::new(s.proxy.clone()))));
    tokio::spawn(labeler.run(s.clone()));
    every(&s, 300, "KPI digest", |s| async move { kpis::post_digest(&s, chrono::Utc::now()).await.map(drop) });
    if s.github.is_some() {
        every(&s, 300, "PR stats", |s| async move { pollers::pr_stats(&s, chrono::Utc::now()).await.map(drop) });
        every(&s, 300, "agent PRs", |s| async move { pollers::agent_prs(&s).await.map(drop) });
    }
    if s.linear.is_some() {
        every(&s, 300, "Linear issues", |s| async move { pollers::linear_issues(&s, chrono::Utc::now()).await.map(drop) });
    }

    // the meter, with Azure's billed prices when the az login is configured
    let rates = match (s.config.llm_meter.upstreams.is_empty(), s.config.cli.get("azure")) {
        (false, Some(rules)) => Some(Arc::new(AzureRates::load(&s.db.lock(), Some(rules.clone()))?)),
        _ => None,
    };
    if let Some(r) = rates.clone() {
        every(&s, azure_rates::CHECK_EVERY.as_secs(), "Azure rates", move |s| {
            let r = r.clone();
            async move { r.refresh(&s, chrono::Utc::now()).await }
        });
    }
    if !s.config.llm_meter.upstreams.is_empty() {
        listen(s.config.llm_meter.port as u16, meter::app(s.clone(), rates.clone())).await?;
    }

    // the dashboard and its Host page
    let store = Arc::new(Mutex::new(HostStore::default()));
    let sampler = Sampler::new(SamplerSettings {
        proc: "/proc".into(), sys_block: "/sys/block".into(), work: s.paths.work.clone(), data: data.clone(),
        project: env("AGENTD_COMPOSE_PROJECT", "autonomous-agent"), network_container: env("AGENTD_NETWORK", "agent-docker"),
        apps_docker: std::env::var("AGENTD_APPS_DOCKER").ok().filter(|v| !v.is_empty()) });
    match sampler {
        Ok(sampler) => { tokio::spawn(sampler.run(s.clone(), store.clone())); }
        Err(e) => tracing::warn!("the Host page has no sampler: {e:#}"),
    }
    let dash = dashboard::app(s.clone(), dashboard::Options {
        static_dir: Some(PathBuf::from(env("AGENTD_DASHBOARD_STATIC", "/app/web"))), poll: Duration::from_secs(1),
        host: store, rates });
    listen(s.config.dashboard.port as u16, dash).await?;

    let health = axum::Router::new().route("/health", axum::routing::get(|| async { axum::Json(json!({"ok": true})) }));
    listen(HEALTH_PORT, health).await?;
    if s.linear.as_ref().is_some_and(|l| l.app.is_some()) {
        listen(PUBLIC_PORT, linear::public_app(s.clone())).await?;
    }
    if let Some(app_token) = secret("SLACK_APP_TOKEN") {
        slack_intake(s.clone(), app_token);
    }
    tracing::info!("agentd is serving");
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        _ = term.recv() => {}
        _ = tokio::signal::ctrl_c() => {}
    }
    tracing::info!("stopping; running turns carry on in their containers");
    Ok(())
}

/// `f` now and every `seconds`; a failure is logged and the loop carries on.
fn every<F, Fut>(s: &Arc<Shared>, seconds: u64, name: &'static str, f: F)
where F: Fn(Arc<Shared>) -> Fut + Send + 'static, Fut: Future<Output = Result<()>> + Send {
    let s = s.clone();
    tokio::spawn(async move {
        loop {
            if let Err(e) = f(s.clone()).await {
                tracing::error!("{name} failed: {e:#}");
            }
            tokio::time::sleep(Duration::from_secs(seconds)).await;
        }
    });
}

async fn listen(port: u16, app: axum::Router) -> Result<()> {
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await.with_context(|| format!("listening on {port}"))?;
    tokio::spawn(async move { // with each request's peer address: the dashboard lets in only trusted peers
        if let Err(e) = axum::serve(listener, app.into_make_service_with_connect_info::<std::net::SocketAddr>()).await {
            tracing::error!("the listener on {port} stopped: {e}");
        }
    });
    Ok(())
}

/// Slack's events, each channel's handled in the order they arrived; channels side by side.
fn slack_intake(s: Arc<Shared>, app_token: String) {
    let (tx, mut rx) = mpsc::unbounded_channel::<Value>();
    tokio::spawn(async move { slack::socket_mode(&app_token, slack::API, move |event| { let _ = tx.send(event); }).await });
    tokio::spawn(async move {
        let mut channels: HashMap<String, mpsc::UnboundedSender<Value>> = HashMap::new();
        while let Some(event) = rx.recv().await {
            let channel = event["channel"].as_str().unwrap_or_default().to_string();
            let sender = channels.entry(channel).or_insert_with(|| {
                let (tx, mut rx) = mpsc::unbounded_channel::<Value>();
                let s = s.clone();
                tokio::spawn(async move {
                    while let Some(event) = rx.recv().await {
                        if let Err(e) = conversations::handle_slack(&s, event).await {
                            tracing::error!("handling a Slack message failed: {e:#}");
                        }
                    }
                });
                tx
            });
            let _ = sender.send(event);
        }
    });
}

/// What the containers read from the data folder: the team's time zone for `agentd schedules`, and each project's app
/// env file (secrets/projects/<project>.env) as runner/projects/<project>.env.
fn prepare_dirs(s: &Shared) -> Result<()> {
    for dir in ["cli", "runner", "schedules", "tasks"] {
        std::fs::create_dir_all(s.paths.data.join(dir))?;
    }
    std::fs::write(s.paths.data.join("runner").join("timezone"), format!("{}\n", s.config.timezone))?;
    let (source, dest) = (s.paths.data.join("secrets").join("projects"), s.paths.data.join("runner").join("projects"));
    let Ok(entries) = std::fs::read_dir(&source) else { return Ok(()) };
    std::fs::create_dir_all(&dest)?;
    use std::os::unix::fs::PermissionsExt;
    for entry in entries.flatten().filter(|e| e.path().extension().is_some_and(|x| x == "env")) {
        let name = entry.file_name().to_string_lossy().into_owned();
        let tmp = dest.join(format!(".{name}.tmp"));
        std::fs::write(&tmp, std::fs::read(entry.path())?)?;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
        std::fs::rename(&tmp, dest.join(&name))?;
    }
    Ok(())
}
