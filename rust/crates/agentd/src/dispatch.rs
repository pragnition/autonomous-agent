//! The dispatcher: what the runner's pull loop and core's runner API did. It claims work items when there is room,
//! builds each turn's spec from its payload, runs it on the turn runner, delivers messages that arrive for a running
//! conversation at once, stops turns whose items were cancelled, and hands each result to the conversation rules.
//! After a restart it carries on the turns that were running and hands out results not yet handled.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use anyhow::{Context, Result, anyhow};
use tokio::task::JoinSet;
use rusqlite::params;
use serde_json::{Map, Value, json};

use crate::admission::Admission;
use crate::containers::{Containers, Engine, Probe};
use crate::conversations as cv;
use crate::db::{append_event, get_setting, utcnow};
use crate::prepare::{TurnSpec, build_bundle, expand_mcp, memory_block, read_env_files};
use crate::queue::{self, WorkItem};
use crate::shared::Shared;
use crate::turn::{BoxFuture, MIDTURN_NOTE, TurnHost, TurnResult, TurnRunner};
use crate::tools;

/// A Slack file larger than this isn't fetched.
const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;
const SLACK_FILES_DIR: &str = "slack-files";
/// How often the dispatcher looks again without being woken.
const LOOK_EVERY: Duration = Duration::from_secs(5);

pub struct Dispatcher<E, P> {
    s: Arc<Shared>,
    runner: Arc<TurnRunner<E, P>>,
    containers: Arc<Containers<E, P>>,
    admission: Admission,
    /// The turns running: work item -> conversation.
    running: StdMutex<HashMap<String, String>>,
}

/// What the turn runner needs from the rest of agentd: the event stream, the tools, and read messages.
struct Host {
    s: Arc<Shared>,
}

impl TurnHost for Host {
    fn event<'a>(&'a self, work_item: &'a str, mut record: Value) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            if let Some(map) = record.as_object_mut() {
                map.insert("_ts".into(), json!(utcnow())); // when it arrived, for the dashboard's timeline
            }
            let dir = self.s.paths.data.join("runs");
            let line = format!("{}\n", crate::records::py_dumps(&record));
            let written = async {
                tokio::fs::create_dir_all(&dir).await?;
                use tokio::io::AsyncWriteExt;
                let mut f = tokio::fs::OpenOptions::new().create(true).append(true).open(dir.join(format!("{work_item}.jsonl"))).await?;
                f.write_all(line.as_bytes()).await
            };
            if let Err(e) = written.await {
                tracing::warn!(work_item, "writing an event: {e}");
            }
        })
    }

    fn tool<'a>(&'a self, work_item: &'a str, name: &'a str, args: Value) -> BoxFuture<'a, String> {
        Box::pin(async move {
            match tools::context_for(&self.s, work_item) {
                Ok(ctx) => tools::call(&self.s, &ctx, name, &args).await,
                Err(e) => format!("{name} failed: {e:#}"),
            }
        })
    }

    fn read<'a>(&'a self, work_item: &'a str, item_id: &'a str) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            if let Err(e) = cv::read(&self.s, work_item, &[item_id.to_string()]).await {
                tracing::warn!(work_item, "marking {item_id} read: {e:#}");
            }
        })
    }
}

impl<E: Engine + 'static, P: Probe + 'static> Dispatcher<E, P> {
    pub fn new(s: Arc<Shared>, runner: Arc<TurnRunner<E, P>>, containers: Arc<Containers<E, P>>, admission: Admission) -> Arc<Self> {
        Arc::new(Dispatcher { s, runner, containers, admission, running: StdMutex::default() })
    }

    fn host(&self) -> Arc<dyn TurnHost> {
        Arc::new(Host { s: self.s.clone() })
    }

    pub fn track(&self, work_item: &str, conversation: &str) {
        self.running.lock().unwrap_or_else(|e| e.into_inner()).insert(work_item.into(), conversation.into());
    }

    fn running(&self) -> Vec<(String, String)> {
        self.running.lock().unwrap_or_else(|e| e.into_inner()).iter().map(|(a, b)| (a.clone(), b.clone())).collect()
    }

    /// Run forever. The turns run as tasks this owns, so stopping it stops them, as a restart of agentd would.
    pub async fn run(self: Arc<Self>) {
        let mut turns = JoinSet::new();
        if let Err(e) = self.carry_on(&mut turns).await {
            tracing::error!("carrying on after a restart: {e:#}");
        }
        loop {
            self.stop_cancelled();
            self.deliver_waiting().await;
            if let Err(e) = self.start_waiting(&mut turns).await {
                tracing::error!("starting turns: {e:#}");
            }
            tokio::select! {
                _ = self.s.wake.notified() => {}
                _ = tokio::time::sleep(LOOK_EVERY) => {}
                Some(_) = turns.join_next(), if !turns.is_empty() => {}
            }
        }
    }

    /// After a restart: items claimed but never started go back in the queue; running turns carry on; finished ones
    /// not yet handled are handled now.
    async fn carry_on(self: &Arc<Self>, turns: &mut JoinSet<()>) -> Result<()> {
        let requeued = queue::requeue_orphans(&self.s.db.lock())?;
        if requeued > 0 {
            tracing::info!("{requeued} claimed items went back in the queue");
        }
        for (item, handle) in self.runner.reattach(self.host()).await {
            let convo = queue::get(&self.s.db.lock(), &item)?.and_then(|w| w.conversation_id).unwrap_or_default();
            self.track(&item, &convo);
            let (me, stop_with_this) = (self.clone(), AbortOnDrop(handle.abort_handle()));
            turns.spawn(async move {
                let _stop_with_this = stop_with_this;
                let result = handle.await.unwrap_or_else(|e| failed(&format!("the turn's task failed: {e}")));
                me.finish(&item, result).await;
            });
        }
        Ok(())
    }

    /// Items cancelled while their turn runs (cancel_task, stop_all): the turn is stopped.
    fn stop_cancelled(&self) {
        for (item, _) in self.running() {
            let cancelled = queue::get(&self.s.db.lock(), &item).ok().flatten().is_some_and(|w| w.status == "cancelled");
            if cancelled {
                self.runner.stop(&item);
            }
        }
    }

    /// Messages queued for a conversation with a running turn go into it now. One its Claude Code doesn't take (the turn
    /// is ending) goes back in the queue and runs as the next turn.
    pub async fn deliver_waiting(&self) {
        for (into, convo) in self.running() {
            let delivered = match queue::deliver(&self.s.db.lock(), &convo, &into) {
                Ok(items) => items,
                Err(e) => {
                    tracing::warn!("delivering into {into}: {e:#}");
                    continue;
                }
            };
            let mut taken = Vec::new();
            for item in delivered {
                let folder = self.s.conversation_dir(&convo);
                let attached = attachments(&self.s, &folder, &item.payload["files"]).await;
                let text = format!("{}{MIDTURN_NOTE}{}{attached}", origin_of(&item.payload), item.payload["text"].as_str().unwrap_or_default());
                if self.runner.deliver(&into, &item.id, &text, from_person(&item.payload)).await {
                    taken.push(item);
                } else if let Err(e) = self.s.db.lock().execute(
                    "UPDATE work_items SET status = 'queued', delivered_into = NULL, updated_at = ? WHERE id = ? AND status = 'delivered'",
                    params![utcnow(), item.id]) {
                    tracing::error!("putting {} back in the queue: {e:#}", item.id);
                }
            }
            cv::delivered(&self.s, &taken).await;
        }
    }

    /// Whether a turn may start now: not paused, an account with room, under the session cap, memory to spare (making
    /// room by stopping an idle container when that's all that's missing).
    async fn may_start(&self) -> Result<bool> {
        {
            let c = self.s.db.lock();
            if get_setting(&c, queue::PAUSED)?.as_deref() == Some("1") || !queue::has_claimable(&c)? {
                return Ok(false);
            }
        }
        if !self.s.proxy.usage().await.is_ok_and(|u| u.has_capacity()) {
            return Ok(false); // waiting for capacity
        }
        let running = queue::claimed_count(&self.s.db.lock())?;
        if running >= self.s.config.limits.max_concurrent_sessions.max(0) as usize {
            return Ok(false);
        }
        if !self.admission.room(running) {
            let stopped = self.containers.stop_one_idle("", chrono::Utc::now()).await?;
            tracing::info!("low on memory; stopped an idle container: {stopped}");
            return Ok(false);
        }
        Ok(true)
    }

    async fn start_waiting(self: &Arc<Self>, turns: &mut JoinSet<()>) -> Result<()> {
        while self.may_start().await? {
            let Some(item) = queue::claim(&self.s.db.lock())? else { break };
            self.start_turn(item, turns).await;
        }
        Ok(())
    }

    async fn start_turn(self: &Arc<Self>, mut item: WorkItem, turns: &mut JoinSet<()>) {
        let convo = item.conversation_id.clone().unwrap_or_default();
        self.track(&item.id, &convo);
        let prepared = async {
            // the conversation as it is now: the session to resume, the system prompt
            let row = cv::row(&self.s.db.lock(), &convo)?;
            if let Some(row) = row {
                item.payload = crate::payloads::refresh(&self.s, &row, item.payload.clone())?;
                queue::set_payload(&self.s.db.lock(), &item.id, &item.payload)?;
            }
            let ctx = tools::context_for(&self.s, &item.id)?;
            append_event(&self.s.db.lock(), "session_started", ctx.task_id.as_deref(), None,
                         Some(&json!({"work_item": item.id, "queued_at": item.created_at, "project": ctx.project})))?;
            cv::on_start(&self.s, &item).await;
            spec(&self.s, &item).await
        };
        let me = self.clone();
        match prepared.await {
            Ok(spec) => {
                turns.spawn(async move {
                    let result = me.runner.run(spec, me.host()).await;
                    me.finish(&item.id, result).await;
                });
            }
            Err(e) => {
                tracing::error!("preparing {} failed: {e:#}", item.id);
                turns.spawn(async move { me.finish(&item.id, failed(&format!("preparing the session failed: {e:#}"))).await });
            }
        }
    }

    /// The turn's result: the item done (messages it read marked done, unread ones back in the queue), the run
    /// recorded, the reply posted, and the result marked handled.
    async fn finish(&self, work_item: &str, result: TurnResult) {
        self.running.lock().unwrap_or_else(|e| e.into_inner()).remove(work_item);
        let value = serde_json::to_value(&result).unwrap_or_default();
        let handled = async {
            queue::complete(&self.s.db.lock(), work_item, &value)?;
            let item = queue::get(&self.s.db.lock(), work_item)?.ok_or_else(|| anyhow!("no item {work_item}"))?;
            record_run(&self.s, &item, &value)?;
            cv::on_result(&self.s, &item, &value).await
        };
        if let Err(e) = handled.await {
            tracing::error!(work_item, "handling the result failed: {e:#}"); // the item is done either way
        }
        if let Err(e) = self.runner.reported(work_item).await {
            tracing::error!(work_item, "marking the result handled: {e:#}");
        }
        self.s.wake.notify_one();
    }
}

/// Stops a task when dropped: a carried-on turn stops with the loop that follows it.
struct AbortOnDrop(tokio::task::AbortHandle);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn failed(error: &str) -> TurnResult {
    TurnResult { outcome: "error".into(), error: Some(error.into()), ..Default::default() }
}

/// The turn's first line: who sent the message, where and its link.
fn origin_of(payload: &Value) -> String {
    payload["origin"].as_str().filter(|o| !o.is_empty()).map(|o| format!("{o}\n")).unwrap_or_default()
}

/// A person's message (from Slack, or a prompt in a Linear session) goes in at once; the system's wait for a pause.
fn from_person(payload: &Value) -> bool {
    payload.get("message_ts").is_some_and(|t| !t.is_null())
        || (payload["source"] == "linear" && payload["follows"].is_null() && payload.get("continuation").is_none())
}

fn size(n: u64) -> String {
    if n >= 1024 * 1024 { format!("{:.1} MB", n as f64 / 1024.0 / 1024.0) } else { format!("{} KB", (n / 1024).max(1)) }
}

/// A message's attachments saved into the conversation's folder, and the note that tells the session where they are
/// ("" without any). A file that can't be fetched is named with the reason, so the session can say so.
async fn attachments(s: &Shared, folder: &Path, files: &Value) -> String {
    let mut lines = Vec::new();
    for f in files.as_array().into_iter().flatten() {
        let id = f["id"].as_str().unwrap_or_default();
        if id.is_empty() || id.contains('/') || id == "." || id == ".." {
            continue;
        }
        let name = Path::new(f["name"].as_str().unwrap_or_default()).file_name().map(|n| n.to_string_lossy().into_owned())
            .filter(|n| !n.is_empty()).unwrap_or_else(|| id.to_string());
        let bytes = f["size"].as_u64().unwrap_or(0);
        let kind: Vec<String> = [f["mimetype"].as_str().unwrap_or_default().to_string(), if bytes > 0 { size(bytes) } else { String::new() }]
            .into_iter().filter(|p| !p.is_empty()).collect();
        let path = folder.join(SLACK_FILES_DIR).join(id).join(&name);
        let saved = async {
            if !path.is_file() {
                // already saved when a restarted turn runs again
                anyhow::ensure!(bytes <= MAX_FILE_BYTES, "over 25 MB");
                let data = s.slack.download_file(id).await?;
                anyhow::ensure!(data.len() as u64 <= MAX_FILE_BYTES, "over 25 MB");
                tokio::fs::create_dir_all(path.parent().unwrap_or(folder)).await?;
                tokio::fs::write(&path, data).await?;
            }
            Ok::<_, anyhow::Error>(())
        };
        match saved.await {
            Ok(()) => lines.push(format!("- {}{}", path.display(), if kind.is_empty() { String::new() } else { format!(" ({})", kind.join(", ")) })),
            Err(e) => {
                tracing::warn!("couldn't fetch Slack file {id}: {e:#}");
                lines.push(format!("- {name}: couldn't be fetched ({e:#})"));
            }
        }
    }
    if lines.is_empty() { String::new() } else {
        format!("\n\n(The message came with these files attached, saved in your session folder. Read them before you answer:\n{})", lines.join("\n"))
    }
}

/// The files with the session's keys: agent.env, then the Linear app's token.
pub fn env_files(s: &Shared) -> Vec<PathBuf> {
    vec![s.paths.data.join("secrets").join("agent.env"), s.paths.data.join("runner").join("linear.env")]
}

/// A turn's spec from its work item, as the runner's `_prepare` built it.
async fn spec(s: &Shared, item: &WorkItem) -> Result<TurnSpec> {
    let p = &item.payload;
    let convo = item.conversation_id.clone().ok_or_else(|| anyhow!("{} has no conversation", item.id))?;
    let folder = s.conversation_dir(&convo);
    std::fs::create_dir_all(&folder).with_context(|| format!("creating {}", folder.display()))?;
    let attached = attachments(s, &folder, &p["files"]).await;
    let origin = origin_of(p);
    let memory = s.paths.work.join("memory");
    std::fs::create_dir_all(&memory)?;
    let cwd = p["cwd"].as_str().map(PathBuf::from).filter(|d| d.is_dir()).unwrap_or(folder);
    let mut settings = Map::new();
    settings.insert("autoMemoryDirectory".into(), json!(memory.display().to_string()));
    let ctx = tools::context_for(s, &item.id)?;
    let env: std::collections::BTreeMap<String, String> = p["test_secrets"].as_object().into_iter().flatten()
        .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string()))).collect();
    Ok(TurnSpec {
        work_item: item.id.clone(), conversation: convo, prompt: format!("{origin}{}{attached}", p["text"].as_str().unwrap_or_default()),
        origin, cwd, model: p["model"].as_str().map(str::to_string).unwrap_or_else(|| s.config.models.default.clone()),
        system_prompt: format!("{}\n\n{}", p["system_prompt"].as_str().unwrap_or_default(), memory_block(&memory, 200)),
        idle_s: p["idle_s"].as_u64().unwrap_or(s.config.limits.idle_minutes.max(1) as u64 * 60), timeout_s: p["timeout_s"].as_u64(),
        env, mcp_servers: expand_mcp(&s.config.mcp, &read_env_files(&env_files(s))),
        plugins: build_bundle(&s.paths.plugins, &s.paths.config, &s.paths.work.join("bundles"))?, settings,
        tools: tools::manifest(s, &ctx), resume_session_id: p["resume_session_id"].as_str().map(str::to_string),
        resume_account: p["resume_account"].as_str().map(str::to_string), attempts: item.attempts.max(0) as u32,
    })
}

/// The run's row, as core recorded it: its tokens count to its conversation's project, else its task's.
fn record_run(s: &Shared, item: &WorkItem, result: &Value) -> Result<()> {
    let c = s.db.lock();
    let (task, project): (Option<String>, Option<String>) = match &item.conversation_id {
        Some(id) => c.query_row("SELECT c.task_id, COALESCE(c.project, t.project) FROM conversations c LEFT JOIN tasks t ON t.id = c.task_id \
                                 WHERE c.id = ?", [id], |r| Ok((r.get(0)?, r.get(1)?))).unwrap_or((None, None)),
        None => (None, None),
    };
    let now = utcnow();
    let n = |k: &str| result[k].as_i64().unwrap_or(0);
    c.execute("INSERT OR REPLACE INTO runs(id, task_id, kind, status, session_id, account_code, forks, outcome, turns, input_tokens, \
               output_tokens, cache_read_tokens, cache_write_tokens, cost_usd_est, transcript_path, error, started_at, ended_at, project) \
               VALUES (?, ?, ?, 'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              params![item.id, task, item.kind, result["session_id"].as_str(), result["account_code"].as_str(), n("forks"),
                      result["outcome"].as_str(), n("turns"), n("input_tokens"), n("output_tokens"), n("cache_read_tokens"),
                      n("cache_write_tokens"), result["cost_usd_est"].as_f64().unwrap_or(0.0),
                      s.paths.data.join("runs").join(format!("{}.jsonl", item.id)).display().to_string(), result["error"].as_str(),
                      now, now, project])?;
    Ok(())
}
