//! What every part of intake shares: the database, config and packs, the Slack client, core's secrets, cached Slack
//! lookups, and the signal that wakes the dispatcher when work is queued.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use tokio::sync::Notify;

use crate::config::{self, Config, Pack};
use crate::db::Db;
use crate::github::GitHub;
use crate::proxy::ProxyClient;
use crate::slack::Slack;

/// How long a failed Slack lookup is remembered, and how many permalinks are kept.
const LOOKUP_RETRY: Duration = Duration::from_secs(600);
const PERMALINKS_KEPT: usize = 5000;

pub struct Paths {
    /// agent.toml, persona.toml, agent/system.md and packs/.
    pub config: PathBuf,
    /// The data folder: db/, runs/, runner/, schedules/, tasks/, secrets/.
    pub data: PathBuf,
    /// The shared volume every container mounts at /var/agent, as agentd sees it.
    pub work: PathBuf,
    /// The shipped defaults: system.md and persona.toml.
    pub defaults: PathBuf,
    /// The core and effortless plugins every session loads.
    pub plugins: PathBuf,
}

#[derive(Default)]
struct Lookups {
    permalinks: HashMap<(String, String), (Instant, Option<String>)>,
    order: VecDeque<(String, String)>,
    names: HashMap<String, (Instant, Option<String>)>,
    channels: HashMap<String, String>,
}

pub struct Shared {
    pub db: Db,
    pub config: Config,
    pub packs: BTreeMap<String, Pack>,
    pub slack: Slack,
    pub bot_user_id: Option<String>,
    pub secrets: BTreeMap<String, String>,
    pub secret_values: Vec<String>,
    pub persona: String,
    pub paths: Paths,
    pub proxy: ProxyClient,
    /// GitHub, read-only, when core's read token is set.
    pub github: Option<GitHub>,
    /// Linear, when a key or the agent's Linear app is set up.
    pub linear: Option<crate::linear::Linear>,
    /// Notified whenever work is queued, cancelled or paused.
    pub wake: Notify,
    /// Notified when a request is answered, so the labeler sweeps.
    pub labels: Notify,
    lookups: Mutex<Lookups>,
}

impl Shared {
    pub fn open(paths: Paths, secrets: BTreeMap<String, String>, slack: Slack, bot_user_id: Option<String>, proxy: ProxyClient,
                github: Option<GitHub>) -> Result<Shared> {
        let config = config::load(&paths.config.join("agent.toml")).map_err(|p| anyhow!("agent.toml: {}", p.join("; ")))?;
        let packs = config::load_packs(&paths.config, &config).map_err(|p| anyhow!("packs: {}", p.join("; ")))?;
        std::fs::create_dir_all(paths.data.join("db"))?;
        let db = Db::open(&paths.data.join("db").join("agent.db"))?;
        let persona = crate::persona::render(&crate::persona::load(Some(&paths.config.join("persona.toml")), &paths.defaults)?,
                                             &config.name).context("the persona")?;
        let secret_values = secrets.values().filter(|v| !v.is_empty()).cloned().collect();
        Ok(Shared { db, config, packs, slack, bot_user_id, secrets, secret_values, persona, paths, proxy, github,
                    linear: None, wake: Notify::new(), labels: Notify::new(), lookups: Mutex::default() })
    }

    /// The conversation's own folder on the shared volume, the same every turn; its clones live here.
    pub fn conversation_dir(&self, conversation_id: &str) -> PathBuf {
        self.paths.work.join("scratch").join(conversation_id)
    }

    pub fn scrub(&self, text: &str) -> String {
        crate::scrub::scrub(text, &self.secret_values)
    }

    fn lookups(&self) -> std::sync::MutexGuard<'_, Lookups> {
        self.lookups.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// A message's link; a failure (the bot left the channel, a rate limit) is remembered for ten minutes.
    pub async fn permalink(&self, channel: &str, ts: &str) -> Option<String> {
        let key = (channel.to_string(), ts.to_string());
        if let Some((at, link)) = self.lookups().permalinks.get(&key)
            && (link.is_some() || at.elapsed() < LOOKUP_RETRY) {
            return link.clone();
        }
        let link = self.slack.permalink(channel, ts).await.ok().filter(|l| !l.is_empty());
        let mut l = self.lookups();
        if l.permalinks.insert(key.clone(), (Instant::now(), link.clone())).is_none() {
            l.order.push_back(key);
        }
        while l.order.len() > PERMALINKS_KEPT {
            if let Some(old) = l.order.pop_front() {
                l.permalinks.remove(&old);
            }
        }
        link
    }

    /// A person's name as Slack has it; failures remembered like permalinks'.
    pub async fn person_name(&self, key: &str) -> Option<String> {
        let slack_id = self.config.people.get(key).map(|p| p.slack.clone()).filter(|s| !s.is_empty())?;
        if let Some((at, name)) = self.lookups().names.get(key) && (name.is_some() || at.elapsed() < LOOKUP_RETRY) {
            return name.clone();
        }
        let name = self.slack.user_name(&slack_id).await.ok().flatten();
        self.lookups().names.insert(key.to_string(), (Instant::now(), name.clone()));
        name
    }

    /// A channel's name, or "" when Slack won't say.
    pub async fn channel_name(&self, channel: &str) -> String {
        if let Some(name) = self.lookups().channels.get(channel) {
            return name.clone();
        }
        let name = self.slack.channel_name(channel).await.unwrap_or_default();
        self.lookups().channels.insert(channel.to_string(), name.clone());
        name
    }

    /// A DM to each admin; one that fails doesn't stop the others.
    pub async fn dm_admins(&self, text: &str) {
        for person in self.config.people.values().filter(|p| p.role == "admin" && !p.slack.is_empty()) {
            let sent = async { self.slack.post(&self.slack.open_dm(&person.slack).await?, None, text).await };
            if let Err(e) = sent.await {
                tracing::error!("couldn't DM {}: {e:#}", person.key);
            }
        }
    }
}
