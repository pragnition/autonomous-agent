//! Long projects: each builds in one conversation, which agentd tracks by name so every session can see it and pass
//! it changes (a port of core/projects.py).

use anyhow::Result;
use rusqlite::Connection;

/// "running" with a claimed turn, "queued" with a queued one, else "idle".
pub fn build_state(c: &Connection, conversation_id: &str) -> Result<&'static str> {
    let statuses: Vec<String> = c.prepare("SELECT status FROM work_items WHERE conversation_id = ? AND kind = 'turn'")?
        .query_map([conversation_id], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    Ok(if statuses.iter().any(|s| s == "claimed") { "running" } else if statuses.iter().any(|s| s == "queued") { "queued" } else { "idle" })
}

use std::sync::LazyLock;

use regex::Regex;
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use crate::db::{immediate, utcnow};
use crate::shared::Shared;
use crate::tools::ToolContext;

static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]{1,40}$").expect("valid"));

struct Project {
    repo: String,
    conversation_id: String,
    thread_link: Option<String>,
    started_by: Option<String>,
    status: String,
}

fn get(c: &Connection, name: &str) -> Result<Option<Project>> {
    Ok(c.query_row("SELECT repo, conversation_id, thread_link, started_by, status FROM projects WHERE name = ?", [name], |r| {
        Ok(Project { repo: r.get(0)?, conversation_id: r.get(1)?, thread_link: r.get(2)?, started_by: r.get(3)?, status: r.get(4)? })
    }).optional()?)
}

/// Register the project, or take it over: its build is now this conversation, and it's active again.
fn start(c: &Connection, name: &str, repo: &str, conversation_id: &str, started_by: Option<&str>, thread_link: Option<&str>) -> Result<()> {
    let now = utcnow();
    c.execute("INSERT INTO projects(name, repo, conversation_id, thread_link, started_by, status, created_at, updated_at) \
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?) ON CONFLICT(name) DO UPDATE SET repo = excluded.repo, \
               conversation_id = excluded.conversation_id, thread_link = excluded.thread_link, started_by = excluded.started_by, \
               status = 'active', updated_at = excluded.updated_at", params![name, repo, conversation_id, thread_link, started_by, now, now])?;
    Ok(())
}

pub async fn tool(s: &Shared, ctx: &ToolContext, name: &str, args: &Value) -> Result<String> {
    let project = args["name"].as_str().unwrap_or_default();
    match name {
        "start_project" => {
            let repo = args["repo"].as_str().unwrap_or_default();
            if !NAME.is_match(project) {
                return Ok("name must be lowercase letters, digits and hyphens, 2 to 41 characters, starting with a letter or digit".into());
            }
            let Some(convo) = &ctx.conversation_id else { return Ok("this conversation can't run a project".into()) };
            // looked up first, so nothing awaits between the check and the write below
            let link = match (&ctx.channel, &ctx.thread_ts) {
                (Some(ch), Some(ts)) => s.permalink(ch, ts).await,
                _ => None,
            };
            let c = s.db.lock();
            let taken = immediate(&c, |c| {
                if let Some(existing) = get(c, project)? && existing.status == "active" && existing.conversation_id != *convo {
                    return Ok(Some(existing.thread_link));
                }
                start(c, project, repo, convo, ctx.actor.as_deref(), link.as_deref())?;
                Ok(None)
            })?;
            Ok(match taken {
                Some(link) => format!("{project} is already being built in {}", link.unwrap_or_else(|| "None".into())),
                None => format!("{project} registered; every session now sees it."),
            })
        }
        "finish_project" => {
            let c = s.db.lock();
            if get(&c, project)?.is_none_or(|p| Some(&p.conversation_id) != ctx.conversation_id.as_ref()) {
                return Ok(format!("only {project}'s build can finish it"));
            }
            c.execute("UPDATE projects SET status = 'done', updated_at = ? WHERE name = ?", params![utcnow(), project])?;
            Ok(format!("{project} marked done."))
        }
        _ => tell(s, ctx, project, args["message"].as_str().unwrap_or_default()),
    }
}

/// Pass a change or a question to a project's build, as a message from the person this turn is for.
fn tell(s: &Shared, ctx: &ToolContext, name: &str, message: &str) -> Result<String> {
    let c = s.db.lock();
    let Some(p) = get(&c, name)? else {
        let active: Vec<String> = c.prepare("SELECT name FROM projects WHERE status = 'active' ORDER BY name")?
            .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        return Ok(format!("no project named {name}; active: {}", if active.is_empty() { "none".into() } else { active.join(", ") }));
    };
    if Some(&p.conversation_id) == ctx.conversation_id.as_ref() {
        return Ok(format!("this is {name}'s build; act on it here."));
    }
    let state = build_state(&c, &p.conversation_id)?;
    if p.status != "active" {
        start(&c, name, &p.repo, &p.conversation_id, p.started_by.as_deref(), p.thread_link.as_deref())?; // a finished one restarts
    }
    let asked = crate::queue::get(&c, &ctx.work_item_id)?.map(|i| i.payload).unwrap_or(Value::Null);
    let convo = crate::conversations::row(&c, &p.conversation_id)?.ok_or_else(|| anyhow::anyhow!("its conversation is gone"))?;
    drop(c);
    let mut payload = crate::payloads::turn(s, &convo, message, ctx.actor.as_deref(), false)?;
    payload["origin"] = asked["origin"].clone();
    payload["source"] = json!(asked["source"].as_str().filter(|v| !v.is_empty()).unwrap_or("slack"));
    payload["requester"] = json!(asked["requester"].as_str().map(str::to_string).or(ctx.actor.clone()));
    payload["follows"] = json!(ctx.work_item_id);
    crate::queue::enqueue(&s.db.lock(), Some(&p.conversation_id), "turn", &payload)?;
    s.wake.notify_one();
    let step = match state {
        "running" => " It's running, so it reads this at its next step.",
        "queued" => " It's about to start its next turn and reads this then.",
        _ => " It was idle and starts on this now.",
    };
    Ok(format!("Passed to {name}'s build.{step} Thread: {}", p.thread_link.unwrap_or_else(|| "(none)".into())))
}
