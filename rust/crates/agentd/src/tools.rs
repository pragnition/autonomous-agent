//! agentd's tools for the agent: where the guarantees live (a port of core/agent_tools.py). Every call carries its
//! work item; agentd resolves it to a `ToolContext` (conversation, project, and the person whose message started the
//! turn). The tier comes from that person, never from the model's arguments, and a tool is offered only by tier.
//! Every reply is stripped of secrets. The catalogue (names, descriptions, inputs, tiers) is tools.json, as core had
//! it; the device-code login tools are made per CLI from the config.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Result, anyhow};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::conversations::{self as cv, record_message};
use crate::db::{append_event, get_setting, set_setting, utcnow};
use crate::queue;
use crate::shared::Shared;
use crate::tasks::{self, NewTask, Status};
use crate::{cli_login, github, notifier, parking, payloads, projects, spawned};

pub const NOT_AVAILABLE: &str = "not available here";
const KINDS: [&str; 3] = ["feature", "bug", "chore"];
const SLACK_READ_METHODS: [&str; 8] = ["conversations.history", "conversations.replies", "conversations.list",
    "conversations.info", "users.info", "users.list", "pins.list", "bookmarks.list"];
const SLACK_READ_CHARS: usize = 20_000;
/// post_files: at most this many files, each at most this big (Slack's own upload limit is higher).
const MAX_FILES: usize = 10;
const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize)]
pub struct Spec {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
    pub tier: String,
}

static CATALOGUE: LazyLock<Vec<Spec>> =
    LazyLock::new(|| serde_json::from_str(include_str!("tools.json")).expect("tools.json is valid"));

pub fn catalogue() -> &'static [Spec] {
    &CATALOGUE
}

fn tier_rank(tier: &str) -> u8 {
    match tier {
        "admin" => 2,
        "act" => 1,
        _ => 0,
    }
}

#[derive(Clone, Debug)]
pub struct ToolContext {
    pub work_item_id: String,
    pub conversation_id: Option<String>,
    pub project: Option<String>,
    pub actor: Option<String>,
    pub tier: &'static str,
    pub unattended: bool,
    pub task_id: Option<String>,
    pub channel: Option<String>,
    pub thread_ts: Option<String>,
}

pub fn context_for(s: &Shared, work_item_id: &str) -> Result<ToolContext> {
    let c = s.db.lock();
    let item = queue::get(&c, work_item_id)?.ok_or_else(|| anyhow!("no work item {work_item_id}"))?;
    let convo = item.conversation_id.as_deref().map(|id| cv::row(&c, id)).transpose()?.flatten();
    let p = &item.payload;
    let actor = p["actor"].as_str().map(str::to_string);
    let unattended = p["unattended"].as_bool().unwrap_or(false);
    let tier = match actor.as_deref().and_then(|a| s.config.people.get(a)) {
        Some(person) if person.role == "admin" => "admin",
        Some(_) => "act",
        None if unattended && actor.is_none() => "act",
        None => "read",
    };
    let text = |k: &str| p[k].as_str().map(str::to_string);
    Ok(ToolContext { work_item_id: work_item_id.to_string(), conversation_id: item.conversation_id.clone(),
                     project: convo.as_ref().and_then(|c| c.project.clone()).or_else(|| text("project")), actor, tier, unattended,
                     task_id: match &convo { Some(c) => c.task_id.clone(), None => text("task_id") },
                     channel: convo.as_ref().and_then(|c| c.channel.clone()), thread_ts: convo.and_then(|c| c.thread_ts) })
}

/// The tools this install offers: the catalogue's (the GitHub ones with a read token, linear_install with the Linear
/// app) and a device-code login per CLI that has one.
fn offered(s: &Shared) -> Vec<Spec> {
    let mut specs: Vec<Spec> = catalogue().iter()
        .filter(|t| !t.name.starts_with("gh_") || s.github.is_some())
        .filter(|t| t.name != "linear_install" || s.linear.as_ref().is_some_and(|l| l.app.is_some()))
        .cloned().collect();
    specs.extend(cli_login::tools(&s.config));
    specs
}

pub fn manifest(s: &Shared, ctx: &ToolContext) -> Vec<holder_proto::ToolSpec> {
    offered(s).into_iter().filter(|t| tier_rank(&t.tier) <= tier_rank(ctx.tier))
        .map(|t| holder_proto::ToolSpec { name: t.name, description: t.description, input_schema: t.input_schema }).collect()
}

/// Run a tool; a failure is the agent's information, never agentd's crash. The reply is stripped of secrets.
pub async fn call(s: &Shared, ctx: &ToolContext, name: &str, args: &Value) -> String {
    if !offered(s).iter().any(|t| t.name == name && tier_rank(&t.tier) <= tier_rank(ctx.tier)) {
        return NOT_AVAILABLE.into();
    }
    let reply = match run(s, ctx, name, args).await {
        Ok(text) => text,
        Err(e) => {
            tracing::warn!("tool {name} failed: {e:#}");
            format!("{name} failed: {e:#}")
        }
    };
    s.scrub(&reply)
}

fn arg<'a>(args: &'a Value, key: &str) -> &'a str {
    args[key].as_str().unwrap_or_default()
}

async fn run(s: &Shared, ctx: &ToolContext, name: &str, args: &Value) -> Result<String> {
    match name {
        "post_update" => post(s, ctx, arg(args, "text")).await,
        "ask" => ask(s, ctx, arg(args, "question").trim()).await,
        "react" => {
            let Some(channel) = &ctx.channel else { return Ok("this conversation has no Slack thread".into()) };
            let payload = payload_of(s, ctx)?;
            let ts = payload["message_ts"].as_str().map(str::to_string).or(ctx.thread_ts.clone()).unwrap_or_default();
            s.slack.react(channel, &ts, arg(args, "emoji").trim_matches(':')).await?;
            Ok("reacted".into())
        }
        "post_files" => post_files(s, ctx, args).await,
        "track_task" => track_task(s, ctx, args).await,
        "finish_task" => finish_task(s, ctx, args).await,
        "start_task" => start_task(s, ctx, args).await,
        "spawn_agent" => spawn_agent(s, ctx, args),
        "status" => status(s, ctx).await,
        "list_tasks" => {
            let status = args["status"].as_str();
            if let Some(st) = status && st.parse::<Status>().is_err() {
                return Ok(format!("unknown status '{st}'"));
            }
            let found = tasks::list(&s.db.lock(), status, args["project"].as_str())?;
            let lines: Vec<String> = found.iter().rev().take(50).rev()
                .map(|t| format!("{} [{}] {}: {}", t.id, t.status, t.project, t.title)).collect();
            Ok(if lines.is_empty() { "no tasks".into() } else { lines.join("\n") })
        }
        "get_task" => {
            let Some(t) = tasks::get(&s.db.lock(), arg(args, "task_id"))? else { return Ok("no such task".into()) };
            let mut lines = vec![format!("{} · {} — {}", t.id, t.title, t.status), format!("project: {} · kind: {}", t.project, t.kind),
                                 format!("requester: {}", t.requester.as_deref().unwrap_or("-"))];
            lines.extend(t.pr_url.map(|u| format!("PR: {u}")));
            Ok(lines.join("\n"))
        }
        "cancel_task" => cancel_task(s, ctx, arg(args, "task_id")).await,
        "stop_all" => stop_all(s, ctx).await,
        "resume_all" => {
            let c = s.db.lock();
            set_setting(&c, queue::PAUSED, "0")?;
            append_event(&c, "resume_all", None, ctx.actor.as_deref(), None)?;
            drop(c);
            s.wake.notify_one();
            Ok("Resumed.".into())
        }
        "read_thread" => read_thread(s, ctx, args).await,
        "slack_read" => slack_read(s, args).await,
        "park" => parking::park(s, ctx, args).await,
        "linear_install" => match s.linear.as_ref().and_then(|l| l.app.as_ref()) {
            Some(app) => app.install_tool(s, ctx).await,
            None => Ok(NOT_AVAILABLE.into()),
        },
        "start_project" | "finish_project" | "tell_project" => projects::tool(s, ctx, name, args).await,
        n if n.starts_with("gh_") => github::tool(s, ctx, n, args).await,
        n if n.ends_with("_login") => cli_login::start(s, ctx, n).await,
        _ => Ok(NOT_AVAILABLE.into()),
    }
}

pub fn payload_of(s: &Shared, ctx: &ToolContext) -> Result<Value> {
    Ok(queue::get(&s.db.lock(), &ctx.work_item_id)?.map(|i| i.payload).unwrap_or(Value::Null))
}

/// Post the agent's text in the conversation's thread, recording each message against it.
pub async fn post(s: &Shared, ctx: &ToolContext, text: &str) -> Result<String> {
    let Some(channel) = &ctx.channel else { return Ok("this conversation has no Slack thread".into()) };
    let posted = s.slack.post_markdown(channel, ctx.thread_ts.as_deref(), &s.scrub(text)).await?;
    if let Some(convo) = &ctx.conversation_id {
        let c = s.db.lock();
        for ts in posted {
            record_message(&c, channel, &ts, convo)?;
        }
    }
    Ok("posted".into())
}

async fn ask(s: &Shared, ctx: &ToolContext, question: &str) -> Result<String> {
    let task = {
        let c = s.db.lock();
        if let Some(convo) = &ctx.conversation_id {
            c.execute("UPDATE conversations SET waiting = 1 WHERE id = ?", [convo])?;
        }
        match ctx.task_id.as_deref().map(|t| tasks::get(&c, t)).transpose()?.flatten() {
            Some(t) if t.status == Status::Working => Some(tasks::transition(&c, &t.id, Status::NeedsInput,
                ctx.actor.as_deref().unwrap_or("agent"), Some(json!({"question": question})))?),
            other => other,
        }
    };
    match task.filter(|t| t.slack_channel.is_some()) {
        Some(task) => notifier::ask(s, &task, question).await?,
        None => {
            post(s, ctx, question).await?;
        }
    }
    Ok("Asked. Their reply arrives as your next message; end this turn now.".into())
}

/// A path made absolute, `.` and `..` taken out, and symlinks resolved where it exists (Python's `Path.resolve()`).
fn resolve(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other),
        }
    }
    out.canonicalize().unwrap_or(out)
}

/// A path the agent gave (relative ones are from its conversation's folder), when it is inside that folder.
fn inside(folder: &Path, raw: &str) -> Option<PathBuf> {
    let path = resolve(&if Path::new(raw).is_absolute() { PathBuf::from(raw) } else { folder.join(raw) });
    path.starts_with(resolve(folder)).then_some(path)
}

/// Files from the conversation's folder into its thread. Every post is logged as evidence.
async fn post_files(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let folder = s.conversation_dir(ctx.conversation_id.as_deref().unwrap_or_default());
    let mut files = Vec::new();
    for raw in args["paths"].as_array().into_iter().flatten().filter_map(Value::as_str) {
        let Some(path) = inside(&folder, raw) else { return Ok(format!("refused: {raw} is outside your session folder")) };
        if !path.is_file() {
            return Ok(format!("no such file: {raw}"));
        }
        if files.len() >= MAX_FILES {
            return Ok(format!("at most {MAX_FILES} files"));
        }
        if std::fs::metadata(&path)?.len() > MAX_FILE_BYTES {
            return Ok(format!("{raw} is over 25 MB"));
        }
        files.push((path.file_name().unwrap_or_default().to_string_lossy().into_owned(), std::fs::read(&path)?));
    }
    if files.is_empty() {
        return Ok("no files given".into());
    }
    let text = s.scrub(arg(args, "text"));
    let names: Vec<String> = files.iter().map(|(n, _)| n.clone()).collect();
    let count = files.len();
    if let Some(channel) = &ctx.channel {
        s.slack.upload_files(channel, ctx.thread_ts.as_deref(), &text, files).await?;
    }
    append_event(&s.db.lock(), "files_posted", ctx.task_id.as_deref(), ctx.actor.as_deref(),
                 Some(&json!({"conversation_id": ctx.conversation_id, "files": names, "text": text, "slack": ctx.channel.is_some()})))?;
    Ok(if ctx.channel.is_some() { format!("posted {count} file(s)") } else {
        format!("recorded {count} file(s) as evidence, but this conversation has no Slack thread, so nobody saw them. Attach \
                 them where the request came from (e.g. the Linear issue) if they matter.")
    })
}

fn new_task_problem(s: &Shared, project: &str, kind: &str) -> Result<Option<String>> {
    if !s.packs.contains_key(project) {
        let known: Vec<&str> = s.packs.keys().map(String::as_str).collect();
        return Ok(Some(format!("unknown project '{project}' (known: {})", if known.is_empty() { "none".into() } else { known.join(", ") })));
    }
    if !KINDS.contains(&kind) {
        return Ok(Some("kind must be one of feature, bug, chore".into()));
    }
    let today = &utcnow()[..10];
    let count: i64 = s.db.lock().query_row("SELECT COUNT(*) FROM tasks WHERE created_at >= ?", [today], |r| r.get(0))?;
    if count >= s.config.limits.max_tasks_per_day {
        return Ok(Some(format!("daily task limit reached ({}); try again tomorrow", s.config.limits.max_tasks_per_day)));
    }
    Ok(None)
}

fn create_task(s: &Shared, ctx: &ToolContext, project: &str, title: &str, kind: &str, description: &str,
               thread_ts: Option<&str>) -> Result<tasks::Task> {
    let c = s.db.lock();
    let task = tasks::create(&c, NewTask {
        project: project.into(), title: title.into(), description: description.into(), kind: kind.into(),
        source: if ctx.unattended { "linear" } else { "slack" }.into(),
        source_ref: ctx.channel.as_ref().map(|ch| format!("{ch}/{}", thread_ts.unwrap_or("None"))),
        requester: ctx.actor.clone(), owner: s.packs.get(project).map(|p| p.project.owner.clone()),
        slack_channel: ctx.channel.clone(), slack_thread_ts: thread_ts.map(str::to_string), linear_issue_id: None })?;
    tasks::transition(&c, &task.id, Status::Working, ctx.actor.as_deref().unwrap_or("agent"), None)
}

async fn track_task(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    if let Some(t) = &ctx.task_id {
        return Ok(format!("Already tracking {t}."));
    }
    let (project, kind) = (arg(args, "project"), arg(args, "kind"));
    if let Some(problem) = new_task_problem(s, project, kind)? {
        return Ok(problem);
    }
    let title = Some(arg(args, "title").trim()).filter(|t| !t.is_empty()).unwrap_or("Untitled");
    let text = payload_of(s, ctx)?["text"].as_str().unwrap_or_default().to_string();
    let task = create_task(s, ctx, project, title, kind, &text, ctx.thread_ts.as_deref())?;
    if let Some(convo) = &ctx.conversation_id {
        s.db.lock().execute("UPDATE conversations SET task_id = ?, project = ? WHERE id = ?", params![task.id, project, convo])?;
    }
    notifier::task_changed(s, &task).await?;
    Ok(format!("Tracking {}: {}", task.id, task.title))
}

async fn finish_task(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let actor = ctx.actor.as_deref().unwrap_or("agent");
    let task = {
        let c = s.db.lock();
        let Some(mut task) = ctx.task_id.as_deref().map(|t| tasks::get(&c, t)).transpose()?.flatten() else {
            return Ok("there is no task to finish in this conversation".into());
        };
        if task.status == Status::NeedsInput {
            task = tasks::transition(&c, &task.id, Status::Working, actor, None)?;
        }
        match tasks::transition(&c, &task.id, Status::Done, actor, Some(json!({"outcome": arg(args, "outcome")}))) {
            Ok(t) => t,
            Err(e) => return Ok(format!("can't finish {}: {e}", task.id)),
        }
    };
    notifier::task_changed(s, &task).await?;
    Ok(format!("{} is done.", task.id))
}

/// A hand-over turn's first line: the calling turn's own, marked as handed over once however often it is handed on.
fn handover_origin(s: &Shared, ctx: &ToolContext, asked: &Value) -> String {
    let origin = asked["origin"].as_str().filter(|o| !o.is_empty()).map(str::to_string)
        .unwrap_or_else(|| cv::origin_line(ctx.channel.as_deref().unwrap_or("no Slack thread"), ctx.actor.as_deref(), None));
    let mark = format!(" (handed over by {})", s.config.name);
    if origin.ends_with(&mark) { origin } else { origin + &mark }
}

async fn start_task(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let (project, kind) = (arg(args, "project"), arg(args, "kind"));
    if let Some(problem) = new_task_problem(s, project, kind)? {
        return Ok(problem);
    }
    let brief = arg(args, "brief").trim();
    let title = Some(arg(args, "title").trim()).filter(|t| !t.is_empty()).unwrap_or("Untitled");
    // a conversation without a thread of its own: post the new one's first line before anything is created
    let (thread_ts, primary) = match (&ctx.channel, &ctx.thread_ts) {
        (Some(channel), None) => (Some(s.slack.post(channel, None, &s.scrub(&format!("Working on {title}"))).await?.1), true),
        _ => (ctx.thread_ts.clone(), false),
    };
    let task = create_task(s, ctx, project, title, kind, brief, thread_ts.as_deref())?;
    let asked = payload_of(s, ctx)?;
    let convo = {
        let c = s.db.lock();
        let id = cv::create_conversation(&c, ctx.channel.as_deref(), thread_ts.as_deref(), Some(project), Some(&task.id), primary, None)?;
        if let (true, Some(channel), Some(ts)) = (primary, &ctx.channel, &thread_ts) {
            record_message(&c, channel, ts, &id)?;
        }
        cv::row(&c, &id)?.ok_or_else(|| anyhow!("conversation vanished"))?
    };
    let mut payload = payloads::turn(s, &convo, brief, ctx.actor.as_deref(), ctx.unattended)?;
    payload["origin"] = json!(handover_origin(s, ctx, &asked));
    payload["source"] = json!(asked["source"].as_str().filter(|v| !v.is_empty()).unwrap_or("slack"));
    payload["requester"] = if asked.get("requester").is_some() { asked["requester"].clone() } else { json!(ctx.actor) };
    payload["follows"] = json!(ctx.work_item_id);
    queue::enqueue(&s.db.lock(), Some(&convo.id), "turn", &payload)?;
    s.wake.notify_one();
    notifier::task_changed(s, &task).await?;
    Ok(format!("Started {} ({}) in a new session for {project}.", task.id, task.title))
}

fn spawn_agent(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let raw_cwd = arg(args, "cwd").trim();
    let cwd = if raw_cwd.is_empty() { None } else {
        // made absolute against the conversation's folder; it must exist on the shared volume
        let folder = s.conversation_dir(ctx.conversation_id.as_deref().unwrap_or_default());
        let path = resolve(&if Path::new(raw_cwd).is_absolute() { PathBuf::from(raw_cwd) } else { folder.join(raw_cwd) });
        match Some(path).filter(|p| p.is_dir()) {
            Some(p) => Some(p.display().to_string()),
            None => return Ok(format!("no such directory: {raw_cwd}")),
        }
    };
    let prompt = arg(args, "prompt").trim();
    if prompt.is_empty() {
        return Ok("spawn_agent needs a prompt: the agent's self-contained brief".into());
    }
    let Some(parent) = &ctx.conversation_id else { return Ok("spawn_agent needs a conversation to report back to".into()) };
    let c = s.db.lock();
    if spawned::is_spawned(&c, parent)? {
        return Ok("a spawned agent can't spawn agents of its own; do the work in this session".into());
    }
    if spawned::running_count(&c, parent)? >= spawned::MAX_RUNNING {
        return Ok(format!("{} agents you spawned are still running, the most one conversation may have at once: wait for one \
                           to report back before spawning another, or do the work yourself", spawned::MAX_RUNNING));
    }
    if spawned::running_total(&c)? >= spawned::MAX_RUNNING_ALL {
        return Ok(format!("{} spawned agents are running already, the most at once across every conversation: do the work \
                           yourself, or spawn it once one of yours reports back", spawned::MAX_RUNNING_ALL));
    }
    let agent_id = spawned::new_id();
    let child = cv::create_conversation(&c, None, None, ctx.project.as_deref(), None, false, cwd.as_deref())?;
    let child_row = cv::row(&c, &child)?.ok_or_else(|| anyhow!("conversation vanished"))?;
    let asked = queue::get(&c, &ctx.work_item_id)?.map(|i| i.payload).unwrap_or(Value::Null);
    drop(c);
    let mut payload = payloads::turn(s, &child_row, &(spawned::CHILD_NOTE.replace("{id}", &agent_id) + prompt),
                                     ctx.actor.as_deref(), ctx.unattended)?;
    let origin = asked["origin"].as_str().filter(|o| !o.is_empty()).map(str::to_string)
        .unwrap_or_else(|| cv::origin_line(ctx.channel.as_deref().unwrap_or("no Slack thread"), ctx.actor.as_deref(), None));
    let requester = if asked.get("requester").is_some() { asked["requester"].clone() } else { json!(ctx.actor) };
    let source = asked["source"].as_str().filter(|v| !v.is_empty()).unwrap_or("slack");
    payload["origin"] = json!(format!("{origin} (spawned by {} as agent {agent_id})", s.config.name));
    payload["source"] = json!(source);
    payload["requester"] = requester.clone();
    payload["follows"] = json!(ctx.work_item_id);
    payload["spawned_agent"] = json!(agent_id);
    let c = s.db.lock();
    spawned::record(&c, &agent_id, parent, &child, &ctx.work_item_id, prompt, cwd.as_deref(),
                    &json!({"actor": ctx.actor, "unattended": ctx.unattended, "origin": asked["origin"], "source": asked["source"],
                            "requester": requester}))?;
    queue::enqueue(&c, Some(&child), "turn", &payload)?;
    drop(c);
    s.wake.notify_one();
    Ok(format!("Spawned agent {agent_id}. It runs on its own and keeps going after your turn ends; its report arrives here as \
                an <agent-notification> when it finishes. Don't wait for it: carry on, or end your turn if there's nothing else \
                to do now."))
}

async fn status(s: &Shared, ctx: &ToolContext) -> Result<String> {
    let (paused, running, queued, open) = {
        let c = s.db.lock();
        let paused = get_setting(&c, queue::PAUSED)?.as_deref() == Some("1");
        // each running item named by its conversation's project ("-" when unbound), with how long it has run
        let running: Vec<(String, Option<i64>)> = c.prepare(
            "SELECT COALESCE(c.project, '-'), (SELECT CAST((julianday('now') - julianday(MAX(e.ts))) * 1440 AS INTEGER) \
             FROM events e WHERE e.type = 'session_started' AND json_extract(e.data, '$.work_item') = w.id) \
             FROM work_items w LEFT JOIN conversations c ON c.id = w.conversation_id WHERE w.status = 'claimed' \
             ORDER BY w.created_at, w.rowid")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
        let queued: i64 = c.query_row("SELECT COUNT(*) FROM work_items WHERE status = 'queued'", [], |r| r.get(0))?;
        let open: i64 = c.query_row("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'working', 'needs_input', \
                                     'ready_for_review')", [], |r| r.get(0))?;
        (paused, running, queued, open)
    };
    let names: Vec<String> = running.iter().map(|(p, m)| match m { Some(m) => format!("{p} {m}m"), None => p.clone() }).collect();
    let mut lines = vec![format!("paused: {}", if paused { "yes" } else { "no" }),
                         format!("running: {}{}", running.len(), if names.is_empty() { String::new() } else { format!(" ({})", names.join(", ")) }),
                         format!("queued: {queued}"), format!("open tasks: {open}")];
    lines.extend(parking::status_lines(s, ctx).await);
    Ok(lines.join("\n"))
}

async fn cancel_task(s: &Shared, ctx: &ToolContext, task_id: &str) -> Result<String> {
    let task = {
        let c = s.db.lock();
        let task = match tasks::transition(&c, task_id, Status::Cancelled, ctx.actor.as_deref().unwrap_or("agent"), None) {
            Ok(t) => t,
            Err(e) => return Ok(format!("can't cancel: {e}")),
        };
        let items: Vec<String> = c.prepare("SELECT w.id FROM work_items w JOIN conversations c ON c.id = w.conversation_id \
                                             WHERE c.task_id = ? AND w.status IN ('queued', 'claimed')")?
            .query_map([task_id], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        for item in items {
            queue::cancel(&c, &item)?;
        }
        task
    };
    s.wake.notify_one(); // the dispatcher stops what was running for it
    notifier::task_changed(s, &task).await?;
    Ok(format!("Cancelled {task_id}."))
}

async fn stop_all(s: &Shared, ctx: &ToolContext) -> Result<String> {
    let (threads, cancelled) = {
        let c = s.db.lock();
        set_setting(&c, queue::PAUSED, "1")?;
        let mut threads: Vec<(String, Option<String>)> = c.prepare(
            "SELECT DISTINCT c.channel, c.thread_ts FROM work_items w JOIN conversations c ON c.id = w.conversation_id \
             WHERE w.status = 'claimed' AND c.channel IS NOT NULL")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        threads.sort();
        let cancelled = queue::claimed(&c)?;
        for item in &cancelled {
            queue::cancel(&c, item)?;
        }
        append_event(&c, "stop_all", None, ctx.actor.as_deref(), Some(&json!({"cancelled": cancelled})))?;
        (threads, cancelled)
    };
    s.wake.notify_one();
    let text = s.scrub(&format!("Stopped by {}. I'll pick this up again when an admin resumes.", ctx.actor.as_deref().unwrap_or("an admin")));
    for (channel, thread_ts) in threads {
        s.slack.post(&channel, thread_ts.as_deref(), &text).await?;
    }
    Ok(format!("Stopped. Interrupted {} running sessions; queued messages wait, and nothing new starts until an admin resumes.",
               cancelled.len()))
}

async fn read_thread(s: &Shared, ctx: &ToolContext, args: &Value) -> Result<String> {
    let Some(channel) = &ctx.channel else { return Ok("this conversation has no Slack thread".into()) };
    let limit = args["limit"].as_u64().unwrap_or(50).min(200) as u32;
    let messages = match &ctx.thread_ts {
        Some(ts) => s.slack.replies(channel, ts, limit).await?,
        None => {
            // a DM chat: its top-level messages from the first one recorded for this conversation
            let first: Option<String> = s.db.lock().query_row(
                "SELECT ts FROM slack_messages WHERE channel = ? AND conversation_id = ? ORDER BY CAST(ts AS REAL) LIMIT 1",
                params![channel, ctx.conversation_id], |r| r.get(0)).optional()?;
            let Some(first) = first else { return Ok("no Slack messages in this conversation yet".into()) };
            s.slack.history(channel, &first, limit).await?.into_iter().rev().collect()
        }
    };
    let lines: Vec<String> = messages.iter().map(|m| {
        let user = m["user"].as_str().unwrap_or_default();
        let who = match s.config.person_by_slack(user) {
            Some(p) => p.key.clone(),
            None if m.get("bot_id").is_some_and(|b| !b.is_null()) => "agent".into(),
            None => if user.is_empty() { "someone".into() } else { user.to_string() },
        };
        format!("{who}: {}", m["text"].as_str().unwrap_or_default())
    }).collect();
    Ok(if lines.is_empty() { "the thread is empty".into() } else { lines.join("\n") })
}

async fn slack_read(s: &Shared, args: &Value) -> Result<String> {
    let method = arg(args, "method");
    if !SLACK_READ_METHODS.contains(&method) {
        return Ok(format!("slack_read can't call '{method}'. Methods: {}", SLACK_READ_METHODS.join(", ")));
    }
    let params = match &args["params"] {
        Value::Null => serde_json::Map::new(),
        Value::Object(m) => m.clone(),
        _ => return Ok("params must be an object".into()),
    };
    // a query string takes strings and numbers; Slack reads 1/0 as true/false and comma lists as lists
    let query: Vec<(String, String)> = params.into_iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| {
        let text = match &v {
            Value::Bool(b) => if *b { "1".into() } else { "0".into() },
            Value::Array(a) => a.iter().map(|x| x.as_str().map(str::to_string).unwrap_or_else(|| x.to_string())).collect::<Vec<_>>().join(","),
            Value::String(t) => t.clone(),
            Value::Number(n) => n.to_string(),
            other => other.to_string(),
        };
        (k, text)
    }).collect();
    let body = s.slack.api_get(method, &query).await?;
    // scrubbed before the cut, so a cut never lands inside a secret
    let text = s.scrub(&crate::records::py_dumps(&body));
    let n = text.chars().count();
    Ok(if n > SLACK_READ_CHARS {
        text.chars().take(SLACK_READ_CHARS).collect::<String>()
            + &format!("\n[truncated: {n} characters in all; ask for less with limit, or page with cursor]")
    } else { text })
}
