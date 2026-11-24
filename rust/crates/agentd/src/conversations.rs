//! Conversations: Slack and Linear messages become work items, and turns' results go back (a port of
//! core/conversations.py).
//!
//! Code only routes. A Slack thread, a DM or a Linear issue is a conversation mapped to one agent session; each new
//! message is the next turn of that session. Every top-level message starts its own thread and conversation. A reply
//! in a thread goes to a session that asked a question there, else the thread's primary conversation, else the
//! conversation of the message the thread hangs off (every message agentd receives or posts is recorded in
//! slack_messages). A thread the agent started itself with its own bot token is noted in own_threads: a reply there
//! starts a new conversation. Each turn carries a first line: who sent the message, where, and its link.
//!
//! Unlike core, an error keeps the conversation's session: the next turn resumes it or hands off from it.
//! Messages of one channel must be handled in the order they arrived (the caller does that).

use std::sync::LazyLock;
use std::time::Duration;

use anyhow::Result;
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Value, json};

use crate::config::Models;
use crate::db::{append_event, utcnow};
use crate::queue::{self, WorkItem};
use crate::shared::Shared;
use crate::{notifier, payloads, spawned, tasks};

pub const NOT_AUTHORISED: &str = "Sorry, I only work with people on this team's list.";
const FAILED_OUTCOMES: [&str; 4] = ["error", "timeout", "stalled", "interrupted"];
/// A long project's build carries on after a turn that errored or got stuck, at most this many times in a row.
const RETRY_OUTCOMES: [&str; 2] = ["error", "stalled"];
const BUILD_RETRIES: i64 = 3;
/// A message's progress: seen, working, then done or failed.
pub const SEEN: &str = "eyes";
pub const WORKING: &str = "hourglass_flowing_sand";
pub const DONE: &str = "white_check_mark";
pub const FAILED: &str = "warning";
/// The longest a Slack lookup for a turn's first line may take; the line goes without it after that.
const LOOKUP: Duration = Duration::from_secs(3);
/// A person's message with files attached; any other subtype isn't something a person said to the agent.
const FILE_SHARE: &str = "file_share";
/// How improve-jensen's proposals start: it answers the replies in their threads itself.
const IMPROVE_JENSEN_PROPOSALS: &str = "*improve-jensen wants to change Jensen*";

static LIMIT_ERROR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^429\b|\baccount \S+ is (rejected|disabled)\b|rate.?limit").expect("valid"));
/// "use <model>" at a sentence's start (a few words of asking may come first); "default" only at a sentence's end,
/// checked after the match (the Python pattern's lookahead).
static USE_MODEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?im)(?:^|[.!?;]\s+)(?:(?:please|pls|ok|okay|now|and|then|so|can you|could you|would you)\s+)*use\s+(?:the\s+)?(claude-[a-z]+-\d[a-z0-9.-]*[a-z0-9]|(?:claude[\s-]+)?(?:opus|sonnet|haiku|fable)(?:[\s-]*\d+(?:[.-]\d+)*)?\b|default(?:\s+model)?)"
).expect("valid"));
static FABLE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\buse fable\b").expect("valid"));
static MENTION: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<@[A-Z0-9]+>\s*").expect("valid"));
static MODEL_SEPARATORS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\s.-]+").expect("valid"));

#[derive(Clone, Debug, PartialEq)]
pub struct Convo {
    pub id: String,
    pub channel: Option<String>,
    pub thread_ts: Option<String>,
    pub project: Option<String>,
    pub task_id: Option<String>,
    pub session_id: Option<String>,
    pub session_account: Option<String>,
    pub is_primary: bool,
    pub waiting: bool,
    pub model: Option<String>,
    pub cwd: Option<String>,
}

fn convo(r: &Row) -> rusqlite::Result<Convo> {
    Ok(Convo { id: r.get("id")?, channel: r.get("channel")?, thread_ts: r.get("thread_ts")?, project: r.get("project")?,
               task_id: r.get("task_id")?, session_id: r.get("session_id")?, session_account: r.get("session_account")?,
               is_primary: r.get::<_, i64>("is_primary")? != 0, waiting: r.get::<_, i64>("waiting")? != 0,
               model: r.get("model")?, cwd: r.get("cwd")? })
}

pub fn row(c: &Connection, id: &str) -> Result<Option<Convo>> {
    Ok(c.query_row("SELECT * FROM conversations WHERE id = ?", [id], convo).optional()?)
}

pub fn create_conversation(c: &Connection, channel: Option<&str>, thread_ts: Option<&str>, project: Option<&str>,
                           task_id: Option<&str>, is_primary: bool, cwd: Option<&str>) -> Result<String> {
    let (id, now) = (format!("V-{}", &crate::turn::uuid4().replace('-', "")[..12]), utcnow());
    c.execute("INSERT INTO conversations(id, channel, thread_ts, project, task_id, is_primary, cwd, created_at, \
               last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              params![id, channel, thread_ts, project, task_id, is_primary as i64, cwd, now, now])?;
    Ok(id)
}

/// Which conversation a Slack message belongs to, so a thread started under it finds its way back.
pub fn record_message(c: &Connection, channel: &str, ts: &str, conversation_id: &str) -> Result<()> {
    c.execute("INSERT OR IGNORE INTO slack_messages(channel, ts, conversation_id) VALUES (?, ?, ?)",
              params![channel, ts, conversation_id])?;
    Ok(())
}

/// A turn's first line: `[from Sam Taylor (sam) · DM · <permalink>]`, `[Linear SAM-7 · <url>]`.
pub fn origin_line(where_: &str, who: Option<&str>, link: Option<&str>) -> String {
    let who = who.map(|w| format!("from {w}"));
    let parts: Vec<&str> = [who.as_deref(), Some(where_), link].into_iter().flatten().filter(|p| !p.is_empty()).collect();
    format!("[{}]", parts.join(" · "))
}

fn model_key(name: &str) -> String {
    let key = MODEL_SEPARATORS.replace_all(&name.trim().to_lowercase(), "-").into_owned();
    key.strip_prefix("claude-").map(str::to_string).unwrap_or(key)
}

/// The model "use <name>" asks for: (true, id) for an allowed one, (true, None) for "default", (false, None) for one
/// that isn't allowed or a name that fits more than one. An exact id wins; else the name may leave out the rest.
pub fn model_named(models: &Models, name: &str) -> (bool, Option<String>) {
    let key = model_key(name);
    if key == "default" || key == "default-model" {
        return (true, None);
    }
    if key == "fable" {
        return (true, Some(models.hard.clone()));
    }
    let choices = models.choices();
    let exact: Vec<&String> = choices.iter().filter(|m| model_key(m) == key).collect();
    let fits = if exact.is_empty() { choices.iter().filter(|m| model_key(m).starts_with(&format!("{key}-"))).collect() } else { exact };
    if fits.len() == 1 { (true, Some(fits[0].clone())) } else { (false, None) }
}

/// The name in a "use <model>" sentence, if the text has one.
pub fn use_model(text: &str) -> Option<String> {
    for caps in USE_MODEL.captures_iter(text) {
        let m = caps.get(1)?;
        if !m.as_str().to_lowercase().starts_with("default") {
            return Some(m.as_str().to_string());
        }
        // "default" only at a sentence's end: "use default." yes, "use default settings" no
        let at_end = |end: usize| {
            let rest = &text[end..];
            let after = rest.trim_start();
            rest[..rest.len() - after.len()].contains('\n') || after.is_empty() || after.starts_with(['.', ',', '!', '?', ';', ':'])
        };
        if at_end(m.end()) {
            return Some(m.as_str().to_string());
        }
        if at_end(m.start() + "default".len()) {
            return Some(m.as_str()[.."default".len()].to_string());
        }
    }
    None
}

pub fn says_use_fable(text: &str) -> bool {
    FABLE.is_match(text)
}

pub fn unknown_model_reply(models: &Models, name: &str) -> String {
    let listed: Vec<String> = models.choices().iter()
        .map(|m| format!("`{m}`{}", if *m == models.default { " (default)" } else { "" })).collect();
    format!("I can't switch this conversation to {}, so it stays on the model it has. Models I can switch to: {}. Say \
             \"use <model>\" with one of them, or \"use default\".", name.trim(), listed.join(", "))
}

pub fn cut_by_limit(outcome: &str, error: Option<&str>) -> bool {
    outcome == "error" && error.is_some_and(|e| !e.is_empty() && LIMIT_ERROR.is_match(e))
}

pub fn limit_wait_reply(error: Option<&str>) -> String {
    format!("A usage limit cut me off ({}). I'll carry on from where I stopped once an account has capacity again.",
            error.unwrap_or_default().trim())
}

/// What the thread is told when a turn fails: what happened, with the runner's own error text.
pub fn failure_reply(outcome: &str, error: Option<&str>) -> String {
    let error = error.filter(|e| !e.is_empty());
    let with = |e: Option<&str>| e.map(|e| format!(" ({e}).")).unwrap_or_else(|| ".".into());
    match outcome {
        "timeout" => format!("I ran out of time before finishing that{}", with(error)),
        "stalled" => format!("I got stuck before finishing that{}", with(error)),
        "interrupted" => "That was stopped before I finished it.".into(),
        _ => match error {
            None => "That failed before I could finish it, and the runner didn't say why.".into(),
            Some(e) => format!("That failed before I could finish it:\n```\n{}\n```", e.trim()),
        },
    }
}

/// The files attached to a Slack message, as the turn carries them; files Slack won't serve are left out.
pub fn message_files(event: &Value) -> Vec<Value> {
    event["files"].as_array().into_iter().flatten()
        .filter(|f| f["id"].as_str().is_some_and(|i| !i.is_empty()) && !matches!(f["mode"].as_str(), Some("tombstone" | "hidden_by_limit")))
        .map(|f| {
            let size = f["size"].as_i64().or_else(|| f["size"].as_str().and_then(|s| s.parse().ok())).unwrap_or(0);
            json!({"id": f["id"], "name": f["name"].as_str().filter(|n| !n.is_empty()).unwrap_or(f["id"].as_str().unwrap_or_default()),
                   "mimetype": f["mimetype"].as_str().unwrap_or_default(), "size": size})
        }).collect()
}

/// Where a queued turn came from and what it carries, besides its text.
#[derive(Default)]
pub struct TurnFrom {
    pub origin: String,
    pub source: String,
    pub requester: Option<String>,
    pub follows: Option<String>,
    pub unattended: bool,
    pub message_ts: Option<String>,
    pub reply_thread_ts: Option<String>,
    pub continuation: i64,
    pub report_to: Option<Value>,
    pub files: Vec<Value>,
    pub failures: i64,
    pub limit_resume: bool,
}

/// Queue a turn of the conversation and wake the dispatcher.
pub fn enqueue_turn(s: &Shared, convo: &Convo, text: &str, actor: Option<&str>, from: TurnFrom) -> Result<String> {
    let mut p = payloads::turn(s, convo, text, actor, from.unattended)?;
    if !from.files.is_empty() {
        p["files"] = json!(from.files);
    }
    p["origin"] = json!(from.origin);
    p["source"] = json!(from.source);
    p["requester"] = json!(from.requester);
    p["follows"] = json!(from.follows);
    for (key, value) in [("continuation", from.continuation), ("failures", from.failures), ("limit_resume", from.limit_resume as i64)] {
        if value != 0 {
            p[key] = json!(value);
        }
    }
    for (key, value) in [("message_ts", from.message_ts), ("reply_thread_ts", from.reply_thread_ts)] {
        if let Some(v) = value {
            p[key] = json!(v);
        }
    }
    if let Some(report_to) = from.report_to {
        p["report_to"] = report_to;
    }
    let c = s.db.lock();
    c.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", params![utcnow(), convo.id])?;
    let id = queue::enqueue(&c, Some(&convo.id), "turn", &p)?;
    drop(c);
    s.wake.notify_one();
    Ok(id)
}

async fn answer<T>(lookup: impl std::future::Future<Output = T>) -> Option<T> {
    tokio::time::timeout(LOOKUP, lookup).await.ok()
}

/// A person as a turn's first line names them: `Sam Taylor (sam, GitHub @sam)`.
pub(crate) async fn who(s: &Shared, key: &str) -> String {
    let name = answer(s.person_name(key)).await.flatten();
    let github = s.config.people.get(key).map(|p| p.github.clone()).unwrap_or_default();
    let tags: Vec<String> = [name.as_ref().map(|_| key.to_string()), (!github.is_empty()).then(|| format!("GitHub @{github}"))]
        .into_iter().flatten().collect();
    if tags.is_empty() { key.to_string() } else { format!("{} ({})", name.unwrap_or_else(|| key.into()), tags.join(", ")) }
}

async fn slack_origin(s: &Shared, key: &str, event: &Value, is_dm: bool, own: bool) -> String {
    let channel = event["channel"].as_str().unwrap_or_default();
    let ts = event["ts"].as_str().unwrap_or_default();
    let (who, link, name) = tokio::join!(who(s, key), answer(s.permalink(channel, ts)),
                                         async { if is_dm { None } else { answer(s.channel_name(channel)).await } });
    let where_ = if is_dm { "DM".to_string() } else {
        let name = name.filter(|n| !n.is_empty()).unwrap_or_else(|| channel.to_string());
        format!("#{name}{}{}", if event["thread_ts"].is_string() { " thread" } else { "" }, if own { " you started" } else { "" })
    };
    origin_line(&where_, Some(&who), link.flatten().as_deref())
}

pub(crate) async fn project_for_channel(s: &Shared, channel: &str) -> Option<String> {
    if let Some((name, _)) = s.packs.iter().find(|(_, p)| p.project.slack_channel == channel) {
        return Some(name.clone());
    }
    let named: Vec<(&String, String)> = s.packs.iter().filter_map(|(n, p)| p.project.slack_channel.strip_prefix('#').map(|c| (n, c.to_string()))).collect();
    if named.is_empty() {
        return None;
    }
    let name = s.channel_name(channel).await;
    named.into_iter().find(|(_, c)| *c == name).map(|(n, _)| n.clone())
}

pub async fn react(s: &Shared, channel: &str, ts: &str, name: &str, replaces: Option<&str>) {
    if let Some(old) = replaces {
        let _ = s.slack.unreact(channel, ts, old).await; // already gone: not worth skipping the new one over
    }
    if let Err(e) = s.slack.react(channel, ts, name).await {
        tracing::warn!("couldn't add the {name} reaction to {channel}:{ts}: {e:#}");
    }
}

/// A Slack message or mention.
pub async fn handle_slack(s: &Shared, event: Value) -> Result<()> {
    let text_of = |k: &str| event[k].as_str().unwrap_or_default().to_string();
    let (user, channel, subtype) = (text_of("user"), text_of("channel"), event["subtype"].as_str());
    if !channel.is_empty() && s.bot_user_id.as_deref() == Some(user.as_str()) {
        return note_own_post(s, &event);
    }
    if user.is_empty() || channel.is_empty() || event.get("bot_id").is_some_and(|b| !b.is_null())
        || subtype.is_some_and(|t| t != FILE_SHARE) {
        return Ok(());
    }
    let is_dm = event["channel_type"] == "im";
    let ts = text_of("ts");
    let thread_ts = event["thread_ts"].as_str().unwrap_or(&ts).to_string();
    let mentioned = event["type"] == "app_mention"
        || s.bot_user_id.as_ref().is_some_and(|b| text_of("text").contains(&format!("<@{b}>")));
    let in_known_thread = event["thread_ts"].is_string() && known_thread(&s.db.lock(), &channel, &thread_ts)?;
    if !(is_dm || mentioned || in_known_thread) {
        return Ok(());
    }
    // the first time this message is seen: Slack retries, a mention arriving as a message too
    if s.db.lock().execute("INSERT OR IGNORE INTO slack_events(key, at) VALUES (?, ?)", params![format!("{channel}:{ts}"), utcnow()])? == 0 {
        return Ok(());
    }
    let Some(person) = s.config.person_by_slack(&user).cloned() else {
        if is_dm || mentioned {
            s.slack.ephemeral(&channel, &user, NOT_AUTHORISED).await?;
        }
        return Ok(());
    };
    let text = MENTION.replace_all(&text_of("text"), "").trim().to_string();
    let own = event["thread_ts"].is_string() && s.db.lock().query_row(
        "SELECT 1 FROM own_threads WHERE channel = ? AND ts = ?", params![channel, thread_ts], |_| Ok(())).optional()?.is_some();
    let origin = slack_origin(s, &person.key, &event, is_dm, own).await;
    let convo = route(s, &channel, &thread_ts, is_dm).await?;
    {
        let c = s.db.lock();
        record_message(&c, &channel, &ts, &convo.id)?;
        unpark(&c, &convo.id, &person.key)?;
    }
    let not_allowed = switch_model(s, &convo.id, &text)?;
    let convo = row(&s.db.lock(), &convo.id)?.ok_or_else(|| anyhow::anyhow!("conversation vanished"))?;
    let came_from = event["thread_ts"].as_str().map(str::to_string);
    let reply_thread_ts = came_from.filter(|t| Some(t) != convo.thread_ts.as_ref());
    enqueue_turn(s, &convo, &text, Some(&person.key), TurnFrom {
        origin, source: "slack".into(), requester: Some(person.key.clone()), message_ts: Some(ts.clone()),
        files: message_files(&event), reply_thread_ts, ..Default::default() })?;
    react(s, &channel, &ts, SEEN, None).await; // the person sees it landed long before the session replies
    if let Some(name) = not_allowed {
        match s.slack.post(&channel, Some(&thread_ts), &unknown_model_reply(&s.config.models, &name)).await {
            Ok((posted_channel, posted)) => record_message(&s.db.lock(), &posted_channel, &posted, &convo.id)?,
            Err(e) => tracing::warn!("couldn't say that {name:?} isn't an allowed model: {e:#}"),
        }
    }
    Ok(())
}

fn known_thread(c: &Connection, channel: &str, thread_ts: &str) -> Result<bool> {
    Ok(c.query_row("SELECT 1 FROM conversations WHERE channel = ?1 AND thread_ts IS ?2 UNION ALL \
                    SELECT 1 FROM slack_messages WHERE channel = ?1 AND ts = ?2 UNION ALL \
                    SELECT 1 FROM own_threads WHERE channel = ?1 AND ts = ?2 LIMIT 1", params![channel, thread_ts], |_| Ok(()))
        .optional()?.is_some())
}

/// The agent's own top-level channel message opens a thread whose replies should reach it, even when it posted with
/// Slack's Web API directly. A DM needs nothing; improve-jensen's proposals are left out.
fn note_own_post(s: &Shared, event: &Value) -> Result<()> {
    let subtype_ok = event["subtype"].is_null() || event["subtype"] == FILE_SHARE;
    if !subtype_ok || event["thread_ts"].is_string() || !event["ts"].is_string() || event["channel_type"] == "im"
        || event["text"].as_str().unwrap_or_default().starts_with(IMPROVE_JENSEN_PROPOSALS) {
        return Ok(());
    }
    s.db.lock().execute("INSERT OR IGNORE INTO own_threads(channel, ts, at) VALUES (?, ?, ?)",
                        params![event["channel"].as_str(), event["ts"].as_str(), utcnow()])?;
    Ok(())
}

/// A session waiting on an answer in the thread takes the message first; then the thread's own conversation; else a
/// new one. The conversation is settled before anything is awaited.
async fn route(s: &Shared, channel: &str, thread_ts: &str, is_dm: bool) -> Result<Convo> {
    let found = {
        let c = s.db.lock();
        let waiting = c.query_row("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND waiting = 1 \
                                   ORDER BY last_active_at DESC LIMIT 1", params![channel, thread_ts], convo).optional()?;
        match waiting {
            Some(w) => Some(w),
            None => in_thread(&c, channel, thread_ts)?,
        }
    };
    let Some(found) = found else {
        let project = if is_dm { None } else { project_for_channel(s, channel).await };
        let c = s.db.lock();
        let id = create_conversation(&c, Some(channel), Some(thread_ts), project.as_deref(), None, true, None)?;
        return row(&c, &id)?.ok_or_else(|| anyhow::anyhow!("conversation vanished"));
    };
    let task = {
        let c = s.db.lock();
        c.execute("UPDATE conversations SET waiting = 0, last_active_at = ? WHERE id = ?", params![utcnow(), found.id])?;
        match (&found.task_id, found.waiting) {
            (Some(t), true) => tasks::get(&c, t)?.filter(|t| t.status == tasks::Status::NeedsInput)
                .map(|t| tasks::transition(&c, &t.id, tasks::Status::Working, "human", None)).transpose()?,
            _ => None,
        }
    };
    if let Some(task) = task {
        notifier::task_changed(s, &task).await?; // the message answers its question
    }
    row(&s.db.lock(), &found.id)?.ok_or_else(|| anyhow::anyhow!("conversation vanished"))
}

fn in_thread(c: &Connection, channel: &str, thread_ts: &str) -> Result<Option<Convo>> {
    if let Some(primary) = c.query_row("SELECT * FROM conversations WHERE channel = ? AND thread_ts IS ? AND is_primary = 1 \
                                        ORDER BY created_at LIMIT 1", params![channel, thread_ts], convo).optional()? {
        return Ok(Some(primary));
    }
    Ok(c.query_row("SELECT c.* FROM slack_messages m JOIN conversations c ON c.id = m.conversation_id \
                    WHERE m.channel = ? AND m.ts = ?", params![channel, thread_ts], convo).optional()?)
}

/// A person's message ends the conversation's open park: it carries the conversation on now.
fn unpark(c: &Connection, convo_id: &str, by: &str) -> Result<()> {
    if c.execute("UPDATE parks SET ended_at = ?, ended_how = 'replied' WHERE conversation_id = ? AND ended_at IS NULL",
                 params![utcnow(), convo_id])? > 0 {
        append_event(c, "park_ended", None, Some(by), Some(&json!({"conversation_id": convo_id, "how": "replied"})))?;
    }
    Ok(())
}

/// "use <model>" switches the conversation from this message's turn on; returns the name asked for when that model
/// isn't allowed (nothing changes and the caller tells the person).
fn switch_model(s: &Shared, convo_id: &str, text: &str) -> Result<Option<String>> {
    let c = s.db.lock();
    let Some(asked) = use_model(text) else {
        if says_use_fable(text) {
            c.execute("UPDATE conversations SET model = ? WHERE id = ?", params![s.config.models.hard, convo_id])?;
        }
        return Ok(None);
    };
    let (ok, model) = model_named(&s.config.models, &asked);
    if !ok {
        return Ok(Some(asked));
    }
    c.execute("UPDATE conversations SET model = ? WHERE id = ?", params![model, convo_id])?;
    Ok(None)
}

/// A turn starts: its message gets ⏳ (a carry-on's message already has it).
pub async fn on_start(s: &Shared, item: &WorkItem) {
    if item.kind != "turn" || item.payload.get("continuation").is_some() {
        return;
    }
    let Some(convo) = item.conversation_id.as_deref().and_then(|id| row(&s.db.lock(), id).ok().flatten()) else { return };
    if let (Some(channel), Some(ts)) = (&convo.channel, item.payload["message_ts"].as_str()) {
        react(s, channel, ts, WORKING, Some(SEEN)).await;
    }
}

/// Messages handed into a running turn get ⏳.
pub async fn delivered(s: &Shared, items: &[WorkItem]) {
    for item in items {
        on_start(s, item).await;
    }
}

/// Messages a running turn read are done: ✅.
pub async fn read(s: &Shared, into: &str, ids: &[String]) -> Result<()> {
    let items = queue::mark_read(&s.db.lock(), ids, into)?;
    for item in items {
        let channel = item.conversation_id.as_deref().and_then(|id| row(&s.db.lock(), id).ok().flatten()).and_then(|c| c.channel);
        if let (Some(channel), Some(ts)) = (channel, item.payload["message_ts"].as_str()) {
            react(s, &channel, ts, DONE, Some(WORKING)).await;
        }
    }
    Ok(())
}

fn builds(c: &Connection, convo_id: &str) -> Result<bool> {
    Ok(c.query_row("SELECT 1 FROM projects WHERE conversation_id = ? AND status = 'active'", [convo_id], |_| Ok(()))
        .optional()?.is_some())
}

/// A turn's result: the reply in its thread, its message's tick, the session for the next turn, and whatever carries
/// it on (a long build's retry, a usage-limit wait, a spawned agent's report, a failed schedule's message).
pub async fn on_result(s: &Shared, item: &WorkItem, result: &Value) -> Result<()> {
    if item.kind != "turn" || item.status == "cancelled" {
        return Ok(());
    }
    let Some(convo) = item.conversation_id.as_deref().map(|id| row(&s.db.lock(), id)).transpose()?.flatten() else { return Ok(()) };
    let p = &item.payload;
    let outcome = result["outcome"].as_str().unwrap_or("done");
    let error = result["error"].as_str();
    let (step, failures) = (p["continuation"].as_i64().unwrap_or(0), p["failures"].as_i64().unwrap_or(0));
    let (retrying, waiting) = {
        let c = s.db.lock();
        // a spawned agent that fails is never retried by itself: it reports and its parent decides
        let retrying = RETRY_OUTCOMES.contains(&outcome) && failures < BUILD_RETRIES && builds(&c, &convo.id)?
            && !spawned::is_spawned(&c, &convo.id)?;
        (retrying, !retrying && p.get("limit_resume").is_none() && cut_by_limit(outcome, error))
    };
    let failed = FAILED_OUTCOMES.contains(&outcome) && !retrying && !waiting;
    let text = if waiting { limit_wait_reply(error) }
               else if retrying || failed { failure_reply(outcome, error) }
               else { result["text"].as_str().unwrap_or_default().trim().to_string() };
    let text = s.scrub(&text);
    {
        // saved before anything is awaited: the next turn may start as soon as this one completed. Unlike core, an
        // error keeps the session: the next turn resumes it, or hands off from it if that fails.
        let c = s.db.lock();
        match result["session_id"].as_str() {
            Some(sid) => c.execute("UPDATE conversations SET session_id = ?, session_account = ?, last_active_at = ? WHERE id = ?",
                                   params![sid, result["account_code"].as_str(), utcnow(), convo.id])?,
            None => c.execute("UPDATE conversations SET last_active_at = ? WHERE id = ?", params![utcnow(), convo.id])?,
        };
    }
    let actor = p["actor"].as_str();
    let carried = |continuation: i64| TurnFrom {
        origin: p["origin"].as_str().unwrap_or_default().into(), source: p["source"].as_str().unwrap_or("slack").into(),
        requester: p["requester"].as_str().map(str::to_string), follows: Some(item.id.clone()),
        unattended: p["unattended"].as_bool().unwrap_or(false), message_ts: p["message_ts"].as_str().map(str::to_string),
        reply_thread_ts: p["reply_thread_ts"].as_str().map(str::to_string), continuation,
        report_to: p.get("report_to").filter(|r| !r.is_null()).cloned(), ..Default::default() };
    if retrying {
        let what = if outcome == "stalled" { "got stuck" } else { "ended with an error" };
        let note = format!("(Your last turn {what}: {}. Carry on from where you stopped. If you don't remember the work, \
                            read your project's memory note and the thread (read_thread) first.)", error.unwrap_or(outcome));
        let fresh = row(&s.db.lock(), &convo.id)?.unwrap_or_else(|| convo.clone());
        enqueue_turn(s, &fresh, &note, actor, TurnFrom { failures: failures + 1, ..carried(step + 1) })?;
    }
    if waiting {
        let c = s.db.lock();
        c.execute("INSERT OR IGNORE INTO limit_waits(work_item_id, conversation_id, error, created_at) VALUES (?, ?, ?, ?)",
                  params![item.id, convo.id, error, utcnow()])?;
        append_event(&c, "usage_limit_cut", convo.task_id.as_deref(), None,
                     Some(&json!({"work_item": item.id, "conversation_id": convo.id, "error": error})))?;
    }
    if !retrying && !waiting {
        report_to_spawner(s, &convo, item, outcome, failed, &text)?;
    }
    let report_to = p.get("report_to").filter(|r| r.is_object());
    if let (true, None, Some(report_to)) = (failed, &convo.channel, report_to) {
        report_scheduled_failure(s, &convo, report_to, &text).await?;
    } else if let (false, Some(channel)) = (text.is_empty(), &convo.channel) {
        // answered in the thread the message came from, else the conversation's own
        let reply_to = p["reply_thread_ts"].as_str().map(str::to_string).or(convo.thread_ts.clone());
        for ts in s.slack.post_markdown(channel, reply_to.as_deref(), &text).await? {
            record_message(&s.db.lock(), channel, &ts, &convo.id)?;
        }
    }
    if let (false, false, Some(channel), Some(ts)) = (retrying, waiting, &convo.channel, p["message_ts"].as_str()) {
        react(s, channel, ts, if failed { FAILED } else { DONE }, Some(WORKING)).await;
    }
    let task = {
        let c = s.db.lock();
        match &convo.task_id {
            Some(t) if failed => tasks::get(&c, t)?.filter(|t| t.status == tasks::Status::Working)
                .map(|t| tasks::transition(&c, &t.id, tasks::Status::Failed, "agent", Some(json!({"outcome": outcome})))).transpose()?,
            _ => None,
        }
    };
    if let Some(task) = task {
        notifier::task_changed(s, &task).await?;
    }
    Ok(())
}

/// A finished spawned agent's report, queued as a message in the conversation that spawned it, with the spawning
/// turn's actor and origin. Only once it is really finished: nothing parked or queued in its conversation.
fn report_to_spawner(s: &Shared, convo: &Convo, item: &WorkItem, outcome: &str, failed: bool, text: &str) -> Result<Option<String>> {
    let (row_, parent) = {
        let c = s.db.lock();
        let Some(r) = spawned::running_for_child(&c, &convo.id)? else { return Ok(None) };
        let status = if failed { "failed" } else { "done" };
        if spawned::more_to_come(&c, &convo.id)? || !spawned::finish(&c, r["id"].as_str().unwrap_or_default(), status, outcome, text)? {
            return Ok(None);
        }
        let parent = row(&c, r["parent_conversation_id"].as_str().unwrap_or_default())?;
        (r, parent)
    };
    let Some(parent) = parent else { return Ok(None) };
    let status = if failed { "failed" } else { "done" };
    let id = row_["id"].as_str().unwrap_or_default();
    let base = row_["origin"].as_str().filter(|o| !o.is_empty()).map(str::to_string)
        .unwrap_or_else(|| origin_line("no Slack thread", row_["actor"].as_str(), None));
    let new_id = enqueue_turn(s, &parent, &spawned::report_text(&row_, status, outcome, text), row_["actor"].as_str(), TurnFrom {
        origin: format!("{base} (report from agent {id})"), source: row_["source"].as_str().unwrap_or("slack").into(),
        requester: row_["requester"].as_str().map(str::to_string), follows: Some(item.id.clone()),
        unattended: row_["unattended"].as_i64() == Some(1), ..Default::default() })?;
    append_event(&s.db.lock(), "agent_reported", parent.task_id.as_deref(), row_["actor"].as_str(),
                 Some(&json!({"agent": id, "parent": parent.id, "status": status, "outcome": outcome, "work_item": new_id})))?;
    Ok(Some(new_id))
}

/// A scheduled run that failed says so in its schedule's channel (a person key means their DM), and that message's
/// thread becomes the run's conversation, so replies there continue it.
async fn report_scheduled_failure(s: &Shared, convo: &Convo, report_to: &Value, text: &str) -> Result<()> {
    let target = report_to["channel"].as_str().unwrap_or_default();
    let channel = match s.config.people.get(target) {
        Some(person) => s.slack.open_dm(&person.slack).await?,
        None => target.to_string(),
    };
    let text = format!("My {} run didn't finish. {text}", report_to["schedule"].as_str().unwrap_or_default());
    let (posted_channel, ts) = s.slack.post(&channel, None, &text).await?;
    let c = s.db.lock();
    c.execute("UPDATE conversations SET channel = ?, thread_ts = ? WHERE id = ?", params![posted_channel, ts, convo.id])?;
    record_message(&c, &posted_channel, &ts, &convo.id)
}

/// Carry on, once, a turn a usage limit cut off: a new turn in its conversation. None when there is nothing to carry
/// on (already carried on, or the conversation is gone).
pub fn resume_after_limit(s: &Shared, work_item_id: &str) -> Result<Option<String>> {
    let (wait, item, convo) = {
        let c = s.db.lock();
        let Some((convo_id, error)) = c.query_row("SELECT conversation_id, error FROM limit_waits WHERE work_item_id = ? AND resumed_at IS NULL",
                                                   [work_item_id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))).optional()?
        else { return Ok(None) };
        (error, queue::get(&c, work_item_id)?, row(&c, &convo_id)?)
    };
    let mut new_id = None;
    if let (Some(item), Some(convo)) = (item, convo) {
        let p = &item.payload;
        let note = format!("(A usage limit cut your last turn off: {}. An account has capacity again; carry on from where you stopped.)",
                           wait.as_deref().unwrap_or("no error text"));
        new_id = Some(enqueue_turn(s, &convo, &note, p["actor"].as_str(), TurnFrom {
            origin: p["origin"].as_str().unwrap_or_default().into(), source: p["source"].as_str().unwrap_or("slack").into(),
            requester: p["requester"].as_str().map(str::to_string), follows: Some(item.id.clone()),
            unattended: p["unattended"].as_bool().unwrap_or(false), message_ts: p["message_ts"].as_str().map(str::to_string),
            reply_thread_ts: p["reply_thread_ts"].as_str().map(str::to_string),
            continuation: p["continuation"].as_i64().unwrap_or(0) + 1, report_to: p.get("report_to").filter(|r| !r.is_null()).cloned(),
            limit_resume: true, ..Default::default() })?);
    }
    s.db.lock().execute("UPDATE limit_waits SET resumed_at = ?, resumed_as = ? WHERE work_item_id = ?",
                        params![utcnow(), new_id, work_item_id])?;
    Ok(new_id)
}

/// Carry on a conversation the agent parked: a new turn in it, as whoever the parking turn was for, with `note` as its
/// text. Its message was already answered, so this turn neither reacts nor counts as a continuation.
pub fn resume_parked(s: &Shared, conversation_id: &str, work_item_id: &str, note: &str) -> Result<Option<String>> {
    let (convo, item) = {
        let c = s.db.lock();
        (row(&c, conversation_id)?, queue::get(&c, work_item_id)?)
    };
    let Some(convo) = convo else { return Ok(None) };
    let p = item.map(|i| i.payload).unwrap_or(Value::Null);
    Ok(Some(enqueue_turn(s, &convo, note, p["actor"].as_str(), TurnFrom {
        origin: p["origin"].as_str().unwrap_or_default().into(), source: p["source"].as_str().unwrap_or("slack").into(),
        requester: p["requester"].as_str().map(str::to_string), follows: Some(work_item_id.to_string()),
        unattended: p["unattended"].as_bool().unwrap_or(false), reply_thread_ts: p["reply_thread_ts"].as_str().map(str::to_string),
        report_to: p.get("report_to").filter(|r| !r.is_null()).cloned(), ..Default::default() })?))
}
