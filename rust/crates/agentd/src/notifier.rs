//! A task's status in Slack: one status message per task, edited in place, and a status reaction on the thread's
//! first message. Every message posted for a task is recorded against its conversation, so a reply under it reaches
//! that conversation (a port of core/notifier.py).

use anyhow::Result;
use rusqlite::OptionalExtension;

use crate::conversations::record_message;
use crate::db::{get_setting, set_setting};
use crate::shared::Shared;
use crate::tasks::{self, Status, Task};

fn reaction(status: Status) -> Option<&'static str> {
    Some(match status {
        Status::Queued => "eyes",
        Status::Working => "hammer",
        Status::NeedsInput => "question",
        Status::ReadyForReview | Status::Done => "white_check_mark",
        Status::Failed => "x",
        _ => return None,
    })
}

fn words(status: Status) -> &'static str {
    match status {
        Status::Queued => "queued",
        Status::Working => "working on it",
        Status::NeedsInput => "waiting for an answer",
        Status::ReadyForReview => "draft PR ready for review",
        Status::Done => "done",
        Status::Merged => "merged",
        Status::Closed => "PR closed",
        Status::Failed => "failed",
        Status::Cancelled => "cancelled",
    }
}

pub fn status_text(task: &Task) -> String {
    let text = format!("{} · {} — {}", task.id, task.title, words(task.status));
    match &task.pr_url {
        Some(url) => format!("{text}\n{url}"),
        None => text,
    }
}

/// Record posted messages against the task's conversation, its primary one first.
fn record(s: &Shared, task: &Task, ts: &[String]) -> Result<()> {
    let c = s.db.lock();
    let convo: Option<String> = c.query_row("SELECT id FROM conversations WHERE task_id = ? ORDER BY is_primary DESC, \
                                             created_at LIMIT 1", [&task.id], |r| r.get(0)).optional()?;
    if let (Some(convo), Some(channel)) = (convo, &task.slack_channel) {
        for one in ts {
            record_message(&c, channel, one, &convo)?;
        }
    }
    Ok(())
}

/// A task changed: its Slack status message and reaction, and its Linear issue; one failing doesn't stop the other.
pub async fn task_changed(s: &Shared, task: &Task) -> Result<()> {
    if let Err(e) = slack_changed(s, task).await {
        tracing::error!("Slack status of {} failed: {e:#}", task.id);
    }
    if let Some(linear) = &s.linear && let Err(e) = linear.sync(s, task).await {
        tracing::error!("Linear status of {} failed: {e:#}", task.id);
    }
    Ok(())
}

async fn slack_changed(s: &Shared, task: &Task) -> Result<()> {
    let Some(channel) = task.slack_channel.clone() else { return Ok(()) };
    let mut task = task.clone();
    if let Some(ts) = &task.status_message_ts {
        s.slack.update(&channel, ts, &status_text(&task)).await?;
    } else {
        let (posted_channel, ts) = s.slack.post(&channel, task.slack_thread_ts.as_deref(), &status_text(&task)).await?;
        let thread = task.slack_thread_ts.clone().unwrap_or_else(|| ts.clone());
        task = tasks::set_fields(&s.db.lock(), &task.id, &[("slack_channel", Some(&posted_channel)),
                                                         ("slack_thread_ts", Some(&thread)), ("status_message_ts", Some(&ts))])?;
    }
    record(s, &task, &task.status_message_ts.clone().into_iter().collect::<Vec<_>>())?;
    react(s, &task).await
}

async fn react(s: &Shared, task: &Task) -> Result<()> {
    let key = format!("reaction:{}", task.id);
    let previous = get_setting(&s.db.lock(), &key)?;
    let (Some(name), Some(thread), Some(channel)) = (reaction(task.status), &task.slack_thread_ts, &task.slack_channel)
        else { return Ok(()) };
    if previous.as_deref() == Some(name) {
        return Ok(());
    }
    if let Some(previous) = previous {
        let _ = s.slack.unreact(channel, thread, &previous).await; // removed by someone already: not worth failing over
    }
    match s.slack.react(channel, thread, name).await {
        Err(e) if crate::slack::error_code(&e) != Some("already_reacted") => return Err(e),
        _ => {}
    }
    set_setting(&s.db.lock(), &key, name)
}

pub async fn ask(s: &Shared, task: &Task, question: &str) -> Result<()> {
    if let (Some(linear), Some(issue)) = (&s.linear, &task.linear_issue_id) {
        let body = format!("I have a question; answer it in the Slack thread for {}: {question}", task.id);
        if let Err(e) = linear.comment(s, issue, &body).await {
            tracing::error!("Linear question on {} failed: {e:#}", task.id);
        }
    }
    let Some(channel) = &task.slack_channel else { return Ok(()) };
    let text = format!("Question about {}: {question}\nReply in this thread and I'll pick it back up.", task.id);
    let posted = s.slack.post_markdown(channel, task.slack_thread_ts.as_deref(), &text).await?;
    record(s, task, &posted)
}
