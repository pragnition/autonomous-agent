//! The turn runner: one turn of a conversation, from starting Claude Code in the conversation's container to its
//! result (a port of the runner's worker.py and claude.py, without the Python SDK).
//!
//! A turn is one or more attempts, each one Claude Code process: a move to another account, a resume after Claude
//! Code died, or a fresh session after a resume failed starts the next one. The holder writes everything an attempt
//! does to the turn's log; the runner reads the log as it grows and keeps what it needs to carry on in the turn's row:
//! the spec, how far the turn got, the last log line it forwarded, and the result until the caller has handled it.
//! After agentd restarts, `reattach` reads each running turn's log again from the start of its attempt and carries on;
//! nothing is forwarded twice and a tool whose result was stored doesn't run again.
//!
//! A turn ends when a result has arrived after the prompt and every message sent into it was read (echoed back), and
//! no helper or moved-aside shell is still running. A result before then is interim: the runner reads on.

pub mod fold;

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::io::{Read, SeekFrom};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Utc};
use holder_proto::{Entry, LogLine, Request, Response};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};
use tokio::sync::Notify;
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::Instant;

use crate::containers::{Containers, Engine, Probe};
use crate::db::Db;
use crate::handoff::{handoff_note, session_file};
use crate::prepare::{TurnSpec, claude_argv, claude_env, read_env_files};
use crate::proxy::ProxyClient;
use crate::records;
use crate::usage::{SessionUsage, USAGE_FIELDS};
use fold::{Attempt, Seen};

pub const FRESH_NOTE: &str = "(Your earlier session in this conversation couldn't be resumed. Call read_thread to catch \
                              up on the conversation so far before you answer.)\n\n";
/// For the caller to put before a message delivered into a running turn.
pub const MIDTURN_NOTE: &str = "(A new message arrived while you're working. Answer it now with post_update, or a \
                                Linear thought in a Linear session, because your final reply only posts when this whole \
                                turn ends. Then carry on, and change course if it asks you to.)\n\n";
pub const RESTARTED_NOTE: &str = "(Your machine restarted partway through this turn. Your session picks up where it \
                                  stopped: check what you had already done, then carry on. The message you were working \
                                  on:)\n\n";
pub const KILLED_NOTE: &str = "(Your session was killed partway through this turn, most likely for running out of \
                               memory in your container. It picks up where it stopped: check what you had already done, \
                               keep memory use down (fewer browsers and builds at once), then carry on. The message you \
                               were working on:)\n\n";

/// How long Claude Code gets to finish after its input is closed, before SIGTERM and then SIGKILL (the SDK's wait).
const CLOSE_GRACE_S: u64 = 5;
const POLL: Duration = Duration::from_millis(50);
/// With no new log line for this long, ask the holder whether Claude Code is still running.
const HOLDER_CHECK: Duration = Duration::from_secs(5);
const MOVE_NOTE: &str = "over its 5-hour soft cap";

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// What the runner needs from the rest of agentd.
pub trait TurnHost: Send + Sync {
    /// A record of the turn's stream: an SDK-shaped record (cut down when long), or a RunUsage.
    fn event<'a>(&'a self, work_item: &'a str, record: Value) -> BoxFuture<'a, ()>;
    /// Run one of agentd's tools for the turn; the text goes back to Claude Code.
    fn tool<'a>(&'a self, work_item: &'a str, name: &'a str, args: Value) -> BoxFuture<'a, String>;
}

/// What a turn reports: the fields of the runner's result to core.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct TurnResult {
    pub outcome: String,
    pub text: String,
    pub session_id: Option<String>,
    pub turns: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
    pub cost_usd_est: f64,
    pub account_code: Option<String>,
    /// Times the turn moved to a fresh session on another account.
    pub forks: u32,
    pub error: Option<String>,
    /// The work items of the messages delivered into the turn that Claude Code read, in order.
    pub read: Vec<String>,
}

pub struct RunnerSettings {
    /// How Claude Code is started: `claude` in the containers; its arguments follow.
    pub claude: Vec<String>,
    pub proxy_url: String,
    /// Claude Code's home (CLAUDE_CONFIG_DIR), the same path in agentd and in the containers.
    pub claude_home: PathBuf,
    /// Each turn's record, `<work item>.jsonl`, as the runner kept it.
    pub transcripts: PathBuf,
    pub run_root: PathBuf,
    /// Key files (agent.env, then linear.env), read again at every start.
    pub env_files: Vec<PathBuf>,
    /// Variables every session gets: the git identity, the compaction window.
    pub fixed_env: BTreeMap<String, String>,
    /// How often a running turn's account is checked with the proxy (60 s).
    pub watchdog: Duration,
    /// How often a running turn's token count is sent while it grows (30 s).
    pub usage_every: Duration,
    /// How long a busy turn may stay on an account over its soft cap, waiting for a quiet moment (30 minutes).
    pub soft_cap_grace: Duration,
    /// Moves to another account per turn, at most (2).
    pub max_forks: u32,
}

/// Why agentd is ending an attempt.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Reason {
    Stalled,
    Timeout,
    Stopped,
    /// The account is rejected, disabled or over its soft cap: carry on in a fresh session on another one.
    Account(String),
}

/// How far a turn got, kept in its row so a restarted agentd carries on from there.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct Progress {
    /// Attempts begun; the current one is the `attempts`-th start in the log.
    attempts: u32,
    forks: u32,
    crash_resumed: bool,
    fresh_retried: bool,
    /// The current attempt's session, whether it resumes it, and its first message.
    session_id: String,
    resume: bool,
    prompt: String,
    /// Each attempt's session and when it began (epoch seconds), for counting tokens from Claude Code's logs.
    sessions: Vec<(String, f64)>,
    /// The current attempt's account, once the proxy has said.
    account: Option<String>,
    reason: Option<Reason>,
    /// When the account was first seen over its soft cap with another account to move to (epoch seconds).
    over_cap_since: Option<f64>,
    /// Earlier attempts' read messages and result counts.
    read: Vec<String>,
    spent: [u64; 4],
}

/// A turn running in this process, for `deliver` and `stop`.
struct Live {
    socket: PathBuf,
    /// Whether a message may be written into Claude Code now; held while one is written, so the attempt can't decide
    /// to end between the check and the write.
    accepting: tokio::sync::Mutex<bool>,
    stopped: AtomicBool,
    stop: Notify,
}

pub struct TurnRunner<E, P> {
    containers: Arc<Containers<E, P>>,
    db: Db,
    proxy: ProxyClient,
    settings: RunnerSettings,
    live: StdMutex<HashMap<String, Arc<Live>>>,
}

/// How an attempt ended.
struct AttemptEnd {
    result: TurnResult,
    tokens: [u64; 4],
    read: Vec<String>,
    switch_wanted: bool,
    /// Claude Code exited, or its container went away, without a result and without agentd ending it.
    crashed: bool,
    killed: bool,
}

impl<E: Engine + 'static, P: Probe + 'static> TurnRunner<E, P> {
    pub fn new(containers: Arc<Containers<E, P>>, db: Db, proxy: ProxyClient, settings: RunnerSettings) -> Self {
        TurnRunner { containers, db, proxy, settings, live: StdMutex::default() }
    }

    /// Run a turn to its end. Its result stays stored until `reported`, and `reattach` hands it out again if agentd
    /// restarts before then.
    pub async fn run(&self, spec: TurnSpec, host: Arc<dyn TurnHost>) -> TurnResult {
        let work_item = spec.work_item.clone();
        if let Err(e) = self.begin(&spec).await {
            return self.finish(&work_item, failed(e)).await;
        }
        self.drive(&work_item, host).await
    }

    /// Carry on every turn that was running when agentd stopped, and hand out every result not yet reported.
    pub async fn reattach(self: &Arc<Self>, host: Arc<dyn TurnHost>) -> Vec<(String, JoinHandle<TurnResult>)> {
        let rows = self.db.call(|c| {
            Ok(c.prepare("SELECT work_item_id, state, result FROM agentd_turns WHERE state IN ('running', 'finished') \
                          ORDER BY started_at")?
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?)
        }).await.unwrap_or_else(|e| {
            tracing::error!("reading the turns to carry on: {e:#}");
            Vec::new()
        });
        rows.into_iter().map(|(work_item, state, result)| {
            let handle = if state == "finished" {
                let result = result.and_then(|r| serde_json::from_str(&r).ok()).unwrap_or_default();
                tokio::spawn(async move { result })
            } else {
                let (runner, host, item) = (self.clone(), host.clone(), work_item.clone());
                tokio::spawn(async move { runner.drive(&item, host).await })
            };
            (work_item, handle)
        }).collect()
    }

    /// The caller has handled the turn's result: it isn't handed out again.
    pub async fn reported(&self, work_item: &str) -> Result<()> {
        let id = work_item.to_string();
        self.db.call(move |c| {
            c.execute("UPDATE agentd_turns SET state = 'reported' WHERE work_item_id = ?", [&id])?;
            c.execute("DELETE FROM agentd_tool_calls WHERE work_item_id = ?", [&id])?;
            Ok(())
        }).await
    }

    /// Write a message into a running turn. It counts as read once Claude Code echoes it (`TurnResult::read`);
    /// `false` when the turn isn't taking messages, and the caller runs it as the next turn. A person's message goes
    /// in at once (`priority: now`), moving running work aside.
    pub async fn deliver(&self, work_item: &str, item_id: &str, text: &str, from_person: bool) -> bool {
        let Some(live) = self.live.lock().unwrap_or_else(|e| e.into_inner()).get(work_item).cloned() else { return false };
        let accepting = live.accepting.lock().await;
        if !*accepting || live.stopped.load(Ordering::SeqCst) {
            return false;
        }
        let mut line = user_line(&uuid4(), text);
        if from_person {
            line["priority"] = json!("now");
            line["origin"] = json!({"kind": "human"});
        }
        let sent = request(&live.socket, &Request::Send { line, tag: Some(item_id.to_string()) }).await;
        drop(accepting);
        matches!(sent, Ok(Response::Ok))
    }

    /// Stop a running turn: the turn in progress is interrupted and the attempt ends with it (`interrupted`).
    pub fn stop(&self, work_item: &str) {
        if let Some(live) = self.live.lock().unwrap_or_else(|e| e.into_inner()).get(work_item) {
            live.stopped.store(true, Ordering::SeqCst);
            live.stop.notify_one();
        }
    }

    async fn begin(&self, spec: &TurnSpec) -> Result<()> {
        // a work item run again starts its log afresh: the attempts are counted in it (a running one is left alone)
        if self.live.lock().unwrap_or_else(|e| e.into_inner()).contains_key(&spec.work_item) {
            bail!("{} is already running", spec.work_item);
        }
        let log = self.log_path(spec)?;
        if let Err(e) = tokio::fs::remove_file(&log).await && e.kind() != std::io::ErrorKind::NotFound {
            return Err(e.into());
        }
        let (id, convo, text, at) = (spec.work_item.clone(), spec.conversation.clone(), serde_json::to_string(spec)?,
                                     Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string());
        self.db.call(move |c| {
            let state: Option<String> = c.query_row("SELECT state FROM agentd_turns WHERE work_item_id = ?", [&id],
                                                    |r| r.get(0)).optional()?;
            if state.as_deref() == Some("running") {
                bail!("{id} is already running");
            }
            c.execute("DELETE FROM agentd_tool_calls WHERE work_item_id = ?", [&id])?;
            c.execute("INSERT OR REPLACE INTO agentd_turns(work_item_id, conversation_id, state, spec, progress, \
                       forwarded_seq, started_at) VALUES (?, ?, 'running', ?, '{}', 0, ?)", params![id, convo, text, at])?;
            Ok(())
        }).await
    }

    async fn drive(&self, work_item: &str, host: Arc<dyn TurnHost>) -> TurnResult {
        let result = match self.load(work_item).await {
            Ok((spec, progress, started_at, forwarded)) => {
                let live = Arc::new(Live { socket: self.settings.run_root.join(&spec.conversation).join("holder.sock"),
                                           accepting: tokio::sync::Mutex::new(false), stopped: AtomicBool::new(false),
                                           stop: Notify::new() });
                self.live.lock().unwrap_or_else(|e| e.into_inner()).insert(work_item.to_string(), live.clone());
                let conversation = spec.conversation.clone();
                let mut turn = Turn::new(self, spec, progress, host, live, started_at, forwarded);
                let result = turn.run().await.unwrap_or_else(failed);
                self.live.lock().unwrap_or_else(|e| e.into_inner()).remove(work_item);
                // the container's idle and 24-hour stops count from here
                if let Err(e) = self.containers.turn_ended(&conversation, Utc::now()).await {
                    tracing::warn!(work_item, "recording the end of the turn: {e:#}");
                }
                result
            }
            Err(e) => failed(e),
        };
        self.finish(work_item, result).await
    }

    async fn finish(&self, work_item: &str, result: TurnResult) -> TurnResult {
        let (id, text) = (work_item.to_string(), serde_json::to_string(&result).unwrap_or_default());
        let at = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        if let Err(e) = self.db.call(move |c| {
            c.execute("UPDATE agentd_turns SET state = 'finished', result = ?, ended_at = ? WHERE work_item_id = ?",
                      params![text, at, id])?;
            Ok(())
        }).await {
            tracing::error!(work_item, "recording the turn's result: {e:#}");
        }
        result
    }

    async fn load(&self, work_item: &str) -> Result<(TurnSpec, Progress, DateTime<Utc>, u64)> {
        let id = work_item.to_string();
        let (spec, progress, started_at, forwarded) = self.db.call(move |c| {
            Ok(c.query_row("SELECT spec, progress, started_at, forwarded_seq FROM agentd_turns WHERE work_item_id = ?",
                           [&id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?,
                                          r.get::<_, i64>(3)?))).optional()?)
        }).await?.ok_or_else(|| anyhow!("no turn {work_item}"))?;
        Ok((serde_json::from_str(&spec)?, serde_json::from_str(&progress)?,
            DateTime::parse_from_rfc3339(&started_at)?.with_timezone(&Utc), forwarded as u64))
    }

    fn log_path(&self, spec: &TurnSpec) -> Result<PathBuf> {
        Ok(self.settings.run_root.join(holder_proto::component(&spec.conversation)?)
            .join(format!("{}.jsonl", holder_proto::component(&spec.work_item)?)))
    }

    /// Whether resuming the session keeps it on the account it last ran on, and that account can carry on: the proxy
    /// has it pinned there, it isn't rejected or disabled, and it isn't over its soft cap with another account to move
    /// to. Anything not known means no: a fresh session is the safe side.
    async fn can_resume(&self, session_id: &str, account: Option<&str>) -> bool {
        let Some(account) = account else { return false };
        let (Ok(pinned), Ok(usage)) = (self.proxy.whoami(session_id).await, self.proxy.usage().await) else { return false };
        pinned.as_deref() == Some(account) && !usage.in_trouble(Some(account))
            && !(usage.over_cap(Some(account)) && usage.handoff_target(Some(account)).is_some())
    }
}

/// One turn being driven: its spec and progress, and the current attempt's state.
struct Turn<'a, E, P> {
    runner: &'a TurnRunner<E, P>,
    spec: TurnSpec,
    progress: Progress,
    host: Arc<dyn TurnHost>,
    live: Arc<Live>,
    deadline: Option<Instant>,
    usage: Arc<StdMutex<SessionUsage>>,
    sent_usage: Option<[u64; 4]>,
    forwarded: u64,
    // the current attempt
    fold: Attempt,
    reader: LogReader,
    starts_seen: u32,
    tools: JoinSet<(u64, u64, String)>,
    last_frame: Instant,
    last_line: Instant,
    last_check: Instant,
    end_now: bool,
    closing: Option<Instant>,
    vanished: bool,
}

impl<'a, E: Engine + 'static, P: Probe + 'static> Turn<'a, E, P> {
    fn new(runner: &'a TurnRunner<E, P>, spec: TurnSpec, progress: Progress, host: Arc<dyn TurnHost>, live: Arc<Live>,
           started_at: DateTime<Utc>, forwarded: u64) -> Self {
        // the budget covers the whole turn, every attempt, from when it was first started
        let deadline = spec.timeout_s.map(|t| {
            let left = (started_at + chrono::Duration::seconds(t as i64) - Utc::now()).to_std().unwrap_or_default();
            Instant::now() + left
        });
        let mut usage = SessionUsage::new(&runner.settings.claude_home);
        for (sid, since) in &progress.sessions {
            usage.watch(sid, *since);
        }
        let log = runner.log_path(&spec).unwrap_or_default();
        let now = Instant::now();
        Turn { runner, spec, progress, host, live, deadline, usage: Arc::new(StdMutex::new(usage)), sent_usage: None,
               forwarded, fold: Attempt::new(), reader: LogReader { path: log, offset: 0, partial: Vec::new() },
               starts_seen: 0, tools: JoinSet::new(), last_frame: now, last_line: now, last_check: now, end_now: false,
               closing: None, vanished: false }
    }

    fn settings(&self) -> &RunnerSettings {
        &self.runner.settings
    }

    async fn run(&mut self) -> Result<TurnResult> {
        if self.progress.attempts == 0 {
            self.first_attempt().await;
            self.save().await?;
        }
        loop {
            let end = self.attempt().await?;
            self.progress.read.extend(end.read.iter().cloned());
            self.progress.spent = std::array::from_fn(|i| self.progress.spent[i] + end.tokens[i]);
            let stopped = self.live.stopped.load(Ordering::SeqCst) || self.progress.reason == Some(Reason::Stopped);
            let sid = end.result.session_id.clone().unwrap_or_else(|| self.progress.session_id.clone());
            let home = self.settings().claude_home.clone();
            let next = if end.switch_wanted && self.progress.forks < self.settings().max_forks && !stopped {
                // never resume a session on another account: a fresh one, which the proxy pins to a working account,
                // told which session it picks up from
                self.progress.forks += 1;
                let transcript = self.settings().transcripts.join(format!("{}.jsonl", self.spec.work_item));
                Some((false, uuid4(), self.with_note(&handoff_note(&sid, Some(home.as_path()), Some(transcript.as_path())))))
            } else if end.crashed && !self.progress.crash_resumed && !stopped {
                self.progress.crash_resumed = true;
                if Path::new(&session_file(Some(home.as_path()), &sid)).is_file() {
                    Some((true, sid, self.with_note(if end.killed { KILLED_NOTE } else { RESTARTED_NOTE })))
                } else if self.progress.resume {
                    self.progress.fresh_retried = true; // the session it resumed is gone
                    Some((false, uuid4(), self.with_note(FRESH_NOTE)))
                } else {
                    Some((false, uuid4(), self.progress.prompt.clone())) // it died before it saved anything
                }
            } else if end.result.outcome == "error" && self.progress.resume && end.result.turns == 0 && !end.crashed
                && !end.switch_wanted && !self.progress.fresh_retried {
                // the saved session couldn't be resumed (its files are gone): a fresh session that reads the thread
                self.progress.fresh_retried = true;
                Some((false, uuid4(), self.with_note(FRESH_NOTE)))
            } else {
                None
            };
            let Some((resume, session_id, prompt)) = next else { return Ok(self.result(end).await) };
            self.next_attempt(resume, session_id, prompt);
            self.save().await?;
        }
    }

    async fn first_attempt(&mut self) {
        let (resume, session_id, prompt) = match self.spec.resume_session_id.clone() {
            Some(sid) if self.runner.can_resume(&sid, self.spec.resume_account.as_deref()).await => {
                let prompt = if self.spec.attempts > 0 { self.with_note(RESTARTED_NOTE) } else { self.spec.prompt.clone() };
                (true, sid, prompt)
            }
            Some(sid) => {
                tracing::info!(work_item = self.spec.work_item, "not resuming {sid} off its account: a fresh session");
                let home = self.settings().claude_home.clone();
                (false, uuid4(), self.with_note(&handoff_note(&sid, Some(home.as_path()), None)))
            }
            None => (false, uuid4(), self.spec.prompt.clone()),
        };
        self.next_attempt(resume, session_id, prompt);
    }

    fn next_attempt(&mut self, resume: bool, session_id: String, prompt: String) {
        let now = epoch(Utc::now());
        self.usage.lock().unwrap_or_else(|e| e.into_inner()).watch(&session_id, now);
        let p = &mut self.progress;
        p.attempts += 1;
        p.sessions.push((session_id.clone(), now));
        (p.session_id, p.resume, p.prompt) = (session_id, resume, prompt);
        (p.account, p.reason, p.over_cap_since) = (None, None, None);
    }

    /// The origin line, a note, then the message.
    fn with_note(&self, note: &str) -> String {
        let text = self.spec.prompt.strip_prefix(&self.spec.origin).unwrap_or(&self.spec.prompt);
        format!("{}{note}{text}", self.spec.origin)
    }

    async fn save(&self) -> Result<()> {
        let (id, text) = (self.spec.work_item.clone(), serde_json::to_string(&self.progress)?);
        self.runner.db.call(move |c| {
            c.execute("UPDATE agentd_turns SET progress = ? WHERE work_item_id = ?", params![text, id])?;
            Ok(())
        }).await
    }

    async fn set_reason(&mut self, reason: Reason) -> Result<()> {
        if self.progress.reason.is_none() {
            self.progress.reason = Some(reason);
            self.save().await?;
        }
        Ok(())
    }

    /// Interrupt the turn in progress; between turns (waiting on helpers) there's none, and no result is coming, so
    /// the attempt ends now.
    async fn interrupt(&mut self) {
        if !self.fold.turn {
            self.end_now = true;
            return;
        }
        let line = json!({"type": "control_request", "request_id": request_id(),
                          "request": {"subtype": "interrupt"}});
        let _ = request(&self.live.socket, &Request::Send { line, tag: None }).await;
    }

    /// Start the current attempt in the conversation's container, unless the log shows it already started.
    async fn start(&mut self) -> Result<()> {
        std::fs::create_dir_all(&self.spec.cwd).with_context(|| format!("creating {}", self.spec.cwd.display()))?;
        let _held = self.runner.containers.ensure(&self.spec.conversation, Utc::now()).await?;
        let mut shared = self.settings().fixed_env.clone();
        shared.extend(read_env_files(&self.settings().env_files));
        let argv = self.settings().claude.iter().cloned()
            .chain(claude_argv(&self.spec, &self.progress.session_id, self.progress.resume).into_iter().skip(1))
            .collect();
        let env = claude_env(&self.spec, &shared, &self.settings().proxy_url, &self.settings().claude_home);
        let init = json!({"type": "control_request", "request_id": request_id(),
                          "request": {"subtype": "initialize", "hooks": null}});
        let start = Request::Start { work_item: self.spec.work_item.clone(), argv, env,
                                     cwd: self.spec.cwd.display().to_string(),
                                     stdin: vec![init, user_line(&uuid4(), &self.progress.prompt)],
                                     tools: self.spec.tools.clone() };
        match request(&self.live.socket, &start).await? {
            Response::Started { .. } => Ok(()),
            Response::Error { message } => bail!("starting Claude Code: {message}"),
            other => bail!("starting Claude Code: unexpected {other:?}"),
        }
    }

    async fn attempt(&mut self) -> Result<AttemptEnd> {
        self.fold = Attempt::new();
        (self.starts_seen, self.end_now, self.closing, self.vanished) = (0, false, None, false);
        self.reader = LogReader { path: self.reader.path.clone(), offset: 0, partial: Vec::new() };
        let mut batch = self.reader.read().await?;
        let started = batch.iter().filter(|l| matches!(l.entry, Entry::Started { .. })).count() as u32;
        if started < self.progress.attempts {
            self.start().await?;
        }
        let now = Instant::now();
        (self.last_frame, self.last_line, self.last_check) = (now, now, now);
        *self.live.accepting.lock().await = true;
        // carrying on after a restart: an attempt agentd had decided to end gets the same ending
        let mut interrupt_again = matches!(self.progress.reason, Some(Reason::Stopped | Reason::Account(_)));
        self.end_now = matches!(self.progress.reason, Some(Reason::Stalled | Reason::Timeout));
        let (watch_every, usage_every) = (self.settings().watchdog, self.settings().usage_every);
        let mut watchdog = tokio::time::interval_at(Instant::now() + watch_every, watch_every);
        let mut usage_tick = tokio::time::interval_at(Instant::now() + usage_every, usage_every);
        loop {
            self.apply_batch(std::mem::take(&mut batch)).await?;
            if self.fold.exit.is_some() || self.vanished {
                break;
            }
            if interrupt_again {
                interrupt_again = false;
                self.interrupt().await;
            }
            self.move_if_due().await?;
            if (self.fold.result_end || self.end_now) && self.closing.is_none() {
                self.close().await?;
                continue;
            }
            let wake = match self.closing {
                Some(at) => at + Duration::from_secs(2 * CLOSE_GRACE_S + 5),
                None => {
                    let idle = self.last_frame + Duration::from_secs(self.spec.idle_s);
                    self.deadline.map_or(idle, |d| d.min(idle))
                }
            };
            let live = self.live.clone();
            let woke = tokio::select! {
                _ = tokio::time::sleep(POLL) => Wake::Poll,
                _ = live.stop.notified() => Wake::Stop,
                Some(done) = self.tools.join_next(), if !self.tools.is_empty() => Wake::Tool(done.ok()),
                _ = watchdog.tick() => Wake::Watchdog,
                _ = usage_tick.tick() => Wake::Usage,
                _ = tokio::time::sleep_until(wake) => Wake::Due,
            };
            match woke {
                Wake::Poll | Wake::Tool(None) => {}
                Wake::Stop => {
                    self.set_reason(Reason::Stopped).await?;
                    self.interrupt().await;
                }
                Wake::Tool(Some((seq, id, text))) => self.answer_tool(seq, id, text).await?,
                Wake::Watchdog => self.watch_account().await?,
                Wake::Usage => self.report_usage().await,
                Wake::Due if self.closing.is_some() => {
                    // Claude Code hasn't exited since its input was closed: gone with its container, or wait on
                    if self.holder_running().await {
                        self.closing = Some(Instant::now());
                    } else {
                        self.vanished = true;
                    }
                }
                Wake::Due => {
                    // a stop has its own reason; otherwise the idle window or the budget ran out
                    let timed_out = self.deadline.is_some_and(|d| Instant::now() >= d);
                    self.set_reason(if timed_out { Reason::Timeout } else { Reason::Stalled }).await?;
                    self.interrupt().await;
                    self.end_now = true;
                }
            }
            batch = self.reader.read().await?;
            if batch.is_empty() && self.closing.is_none() && self.last_line.elapsed() >= HOLDER_CHECK
                && self.last_check.elapsed() >= HOLDER_CHECK {
                self.last_check = Instant::now();
                if !self.holder_running().await {
                    // gone without an exit in the log (its container stopped): read what's left, then call it a crash
                    batch = self.reader.read().await?;
                    self.vanished = !batch.iter().any(|l| matches!(l.entry, Entry::Exit { .. }));
                }
            }
        }
        *self.live.accepting.lock().await = false;
        self.tools.abort_all();
        Ok(self.attempt_end().await)
    }

    /// Stop taking messages, read what was written meanwhile, and close Claude Code's input, unless a message landed
    /// after the result: then the turn reads on to its answer.
    async fn close(&mut self) -> Result<()> {
        let live = self.live.clone();
        let mut accepting = live.accepting.lock().await;
        *accepting = false;
        let late = self.reader.read().await?;
        self.apply_batch(late).await?;
        if self.fold.result_end || self.end_now {
            self.fold.frozen = true;
            self.closing = Some(Instant::now());
            let _ = request(&self.live.socket, &Request::Close { grace_s: CLOSE_GRACE_S }).await;
        } else {
            *accepting = true;
        }
        Ok(())
    }

    async fn apply_batch(&mut self, batch: Vec<LogLine>) -> Result<()> {
        // a tool call from a Claude Code that has already exited isn't run
        let exit_at = batch.iter().rposition(|l| matches!(l.entry, Entry::Exit { .. }));
        for (i, line) in batch.into_iter().enumerate() {
            if self.starts_seen < self.progress.attempts {
                if matches!(line.entry, Entry::Started { .. }) {
                    self.starts_seen += 1;
                }
                continue;
            }
            self.last_line = Instant::now();
            let stopping = self.progress.reason.is_some();
            match self.fold.apply(&line.entry, stopping) {
                Seen::Frame { frame, first_assistant } => {
                    self.last_frame = Instant::now();
                    if line.seq > self.forwarded {
                        self.forward(&frame, line.seq).await?;
                    }
                    if first_assistant && self.progress.account.is_none()
                        && let Some(sid) = self.fold.session_id.clone() {
                        self.progress.account = self.runner.proxy.whoami(&sid).await.ok().flatten();
                        self.save().await?;
                    }
                }
                Seen::Tool { id, name, args } => {
                    if let Some(text) = self.stored_tool(line.seq).await? {
                        let _ = request(&self.live.socket, &Request::ToolResult { id, text }).await;
                    } else if exit_at.is_none_or(|e| e < i) {
                        let (host, work_item, seq) = (self.host.clone(), self.spec.work_item.clone(), line.seq);
                        self.tools.spawn(async move { (seq, id, host.tool(&work_item, &name, args).await) });
                    }
                }
                Seen::Exit | Seen::Nothing => {}
            }
        }
        Ok(())
    }

    /// The frame's record into the transcript and the event stream, as the runner sent them.
    async fn forward(&mut self, frame: &Value, seq: u64) -> Result<()> {
        if let Some(record) = records::to_record(frame) {
            let path = self.settings().transcripts.join(format!("{}.jsonl", self.spec.work_item));
            tokio::fs::create_dir_all(&self.settings().transcripts).await?;
            let mut file = tokio::fs::OpenOptions::new().create(true).append(true).open(&path).await?;
            file.write_all(format!("{}\n", records::py_dumps(&record)).as_bytes()).await?;
            self.host.event(&self.spec.work_item, records::truncate_for_event(record)).await;
        }
        self.forwarded = seq;
        let id = self.spec.work_item.clone();
        self.runner.db.call(move |c| {
            c.execute("UPDATE agentd_turns SET forwarded_seq = ? WHERE work_item_id = ?", params![seq as i64, id])?;
            Ok(())
        }).await
    }

    async fn stored_tool(&self, seq: u64) -> Result<Option<String>> {
        let id = self.spec.work_item.clone();
        self.runner.db.call(move |c| {
            Ok(c.query_row("SELECT result FROM agentd_tool_calls WHERE work_item_id = ? AND call_id = ?",
                           params![id, seq as i64], |r| r.get(0)).optional()?)
        }).await
    }

    /// Store a tool's result before sending it, so the call replayed after a restart sends it again instead of running.
    async fn answer_tool(&mut self, seq: u64, id: u64, text: String) -> Result<()> {
        let (item, stored) = (self.spec.work_item.clone(), text.clone());
        self.runner.db.call(move |c| {
            c.execute("INSERT OR IGNORE INTO agentd_tool_calls(work_item_id, call_id, result) VALUES (?, ?, ?)",
                      params![item, seq as i64, stored])?;
            Ok(())
        }).await?;
        let _ = request(&self.live.socket, &Request::ToolResult { id, text }).await;
        Ok(())
    }

    /// The account's state from the proxy: a rejected or disabled account moves the turn at once; one over its soft
    /// cap, with another account under its own to move to, moves at the next quiet moment (`move_if_due`).
    async fn watch_account(&mut self) -> Result<()> {
        let Some(account) = self.progress.account.clone() else { return Ok(()) };
        let Ok(usage) = self.runner.proxy.usage().await else { return Ok(()) };
        if self.progress.reason.is_none() && usage.in_trouble(Some(&account)) {
            let status = usage.status_of(Some(&account)).unwrap_or_default().to_string();
            self.set_reason(Reason::Account(status)).await?;
            self.interrupt().await;
            return Ok(());
        }
        let wanted = self.progress.forks < self.settings().max_forks && usage.over_cap(Some(&account))
            && usage.handoff_target(Some(&account)).is_some();
        if wanted != self.progress.over_cap_since.is_some() {
            self.progress.over_cap_since = wanted.then(|| epoch(Utc::now()));
            self.save().await?;
        }
        Ok(())
    }

    /// Move off an account over its soft cap when nothing is running but Claude Code thinking or writing, or anyway
    /// once it has waited `soft_cap_grace` for such a moment.
    async fn move_if_due(&mut self) -> Result<()> {
        let Some(since) = self.progress.over_cap_since else { return Ok(()) };
        let waited = epoch(Utc::now()) - since >= self.settings().soft_cap_grace.as_secs_f64();
        if self.progress.reason.is_none() && self.closing.is_none() && (self.fold.quiet() || waited) {
            self.set_reason(Reason::Account(MOVE_NOTE.into())).await?;
            self.interrupt().await;
        }
        Ok(())
    }

    /// Send the turn's token count so far, when it has grown: the stream misses helpers' calls.
    async fn report_usage(&mut self) {
        let usage = self.usage.clone();
        let counted = tokio::task::spawn_blocking(move || usage.lock().unwrap_or_else(|e| e.into_inner()).poll())
            .await.ok().flatten();
        if let Some(counted) = counted.filter(|c| Some(*c) != self.sent_usage) {
            let fields: serde_json::Map<String, Value> = USAGE_FIELDS.iter().zip(counted)
                .map(|(k, n)| (k.to_string(), json!(n))).collect();
            self.host.event(&self.spec.work_item, json!({"type": "RunUsage", "usage": fields})).await;
            self.sent_usage = Some(counted);
        }
    }

    async fn holder_running(&self) -> bool {
        let status = tokio::time::timeout(Duration::from_secs(5), request(&self.live.socket, &Request::Status)).await;
        matches!(status, Ok(Ok(Response::Status(s))) if s.alive && s.work_item.as_deref() == Some(&self.spec.work_item))
    }

    async fn attempt_end(&mut self) -> AttemptEnd {
        let fold = std::mem::take(&mut self.fold);
        let account = self.progress.account.clone();
        let last = fold.results.last();
        let tokens = fold.results.iter().map(|r| tokens(r.get("usage"))).fold([0; 4], |a, t| std::array::from_fn(|i| a[i] + t[i]));
        let mut result = TurnResult { outcome: "error".into(), account_code: account.clone(),
                                      session_id: fold.session_id.clone().or(Some(self.progress.session_id.clone())),
                                      ..Default::default() };
        if let Some(last) = last {
            result.turns = fold.results.iter().filter_map(|r| r.get("num_turns").and_then(Value::as_u64)).sum();
            // not summed: the CLI's total is its session-wide ledger, so the last result has it all
            result.cost_usd_est = last.get("total_cost_usd").and_then(Value::as_f64).unwrap_or(0.0);
            let texts: Vec<&str> = fold.results.iter().filter_map(|r| r.get("result").and_then(Value::as_str))
                .filter(|t| !t.is_empty()).collect();
            result.text = texts.join("\n\n");
        }
        let mut switch_wanted = false;
        let set = |r: &mut TurnResult, outcome: &str, error: String| (r.outcome, r.error) = (outcome.into(), Some(error));
        match &self.progress.reason {
            Some(Reason::Timeout) => set(&mut result, "timeout",
                                         format!("no result within {} s", self.spec.timeout_s.unwrap_or_default())),
            Some(Reason::Stalled) => set(&mut result, "stalled", format!("no progress for {} s", self.spec.idle_s)),
            Some(Reason::Stopped) => set(&mut result, "interrupted", "interrupted".into()),
            Some(Reason::Account(trouble)) => {
                set(&mut result, "error", format!("account {} is {trouble}", account.as_deref().unwrap_or("None")));
                switch_wanted = true;
            }
            None => match last {
                None => set(&mut result, "error", "the session ended without a result".into()),
                Some(last) if last["subtype"] == "success" => result.outcome = "done".into(),
                Some(last) => {
                    let status = last.get("api_error_status").cloned().unwrap_or(Value::Null);
                    let errors = last.get("errors").cloned().unwrap_or(Value::Null);
                    set(&mut result, "error", format!("{} {}", py_repr(&status), py_repr(&errors)));
                    switch_wanted = fold.switching() || (status == 429 && match self.runner.proxy.usage().await {
                        Ok(usage) => usage.in_trouble(account.as_deref()),
                        Err(_) => false,
                    });
                }
            },
        }
        let crashed = self.progress.reason.is_none() && last.is_none();
        let killed = fold.exit.is_some_and(|(_, signal)| signal == Some(9));
        AttemptEnd { result, tokens, read: fold.read, switch_wanted, crashed, killed }
    }

    /// The turn's result: the last attempt's, with every attempt's tokens and read messages. Tokens come from Claude
    /// Code's logs (helpers' calls included), never below the results' own counts.
    async fn result(&mut self, end: AttemptEnd) -> TurnResult {
        let usage = self.usage.clone();
        let counted = tokio::task::spawn_blocking(move || usage.lock().unwrap_or_else(|e| e.into_inner()).poll())
            .await.ok().flatten();
        let spent = self.progress.spent;
        let total = |t: &[u64; 4]| t.iter().sum::<u64>();
        let tokens = match counted {
            Some(c) if total(&c) >= total(&spent) => c,
            _ => spent,
        };
        let mut result = end.result;
        [result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens] = tokens;
        (result.forks, result.read) = (self.progress.forks, self.progress.read.clone());
        result
    }
}

/// What woke a running attempt.
enum Wake {
    Poll,
    Stop,
    Tool(Option<(u64, u64, String)>),
    Watchdog,
    Usage,
    Due,
}

/// The holder's log of one turn, read as it grows.
struct LogReader {
    path: PathBuf,
    offset: u64,
    partial: Vec<u8>,
}

impl LogReader {
    /// The complete lines written since the last read.
    async fn read(&mut self) -> Result<Vec<LogLine>> {
        let mut file = match tokio::fs::File::open(&self.path).await {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e.into()),
        };
        file.seek(SeekFrom::Start(self.offset)).await?;
        let read = file.read_to_end(&mut self.partial).await?;
        self.offset += read as u64;
        let Some(end) = self.partial.iter().rposition(|&b| b == b'\n') else { return Ok(Vec::new()) };
        let complete: Vec<u8> = self.partial.drain(..=end).collect();
        Ok(complete.split(|&b| b == b'\n').filter(|l| !l.is_empty())
            .filter_map(|l| serde_json::from_slice(l).map_err(|e| tracing::warn!("bad log line: {e}")).ok())
            .collect())
    }
}

async fn request(socket: &Path, req: &Request) -> Result<Response> {
    agent_holder::client::request(socket, req).await
}

fn failed(e: anyhow::Error) -> TurnResult {
    TurnResult { outcome: "error".into(), error: Some(format!("the turn failed: {e:#}")), ..Default::default() }
}

/// One stream-json user message; Claude Code echoes it back with this uuid once it reads it.
fn user_line(uuid: &str, text: &str) -> Value {
    json!({"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}, "parent_tool_use_id": null})
}

fn request_id() -> String {
    format!("req_{}", &uuid4()[..8])
}

fn uuid4() -> String {
    let mut b = [0u8; 16];
    std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).expect("random bytes");
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!("{}-{}-{}-{}-{}", &h[..8], &h[8..12], &h[12..16], &h[16..20], &h[20..])
}

fn epoch(t: DateTime<Utc>) -> f64 {
    t.timestamp_millis() as f64 / 1000.0
}

/// A result's (input, output, cache read, cache write) tokens.
fn tokens(usage: Option<&Value>) -> [u64; 4] {
    USAGE_FIELDS.map(|k| usage.and_then(|u| u.get(k)).and_then(Value::as_u64).unwrap_or(0))
}

/// Python's repr of a parsed JSON value, as the runner's error text had it ("429 ['rate limited']").
fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => (if *b { "True" } else { "False" }).into(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
            let mut out = String::from(quote);
            for c in s.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if c == quote => {
                        out.push('\\');
                        out.push(c);
                    }
                    c => out.push(c),
                }
            }
            out.push(quote);
            out
        }
        Value::Array(items) => format!("[{}]", items.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(map) => format!("{{{}}}", map.iter()
            .map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v))).collect::<Vec<_>>().join(", ")),
    }
}
