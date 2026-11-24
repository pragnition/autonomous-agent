//! What shipped, for the KPIs (ports of core/pr_stats.py, agent_prs.py and linear_issues.py's poller): each tracked
//! task's PR (review and merge times, human edits, reverts within 30 days), the agent's own PRs found by searching
//! GitHub for its login, and the state of each Linear issue handed to it. Times are GitHub's and Linear's own.

use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use crate::db::{get_setting, set_setting, utcnow};
use crate::shared::Shared;
use crate::tasks::{self, Status, Task};

const FMT: &str = "%Y-%m-%dT%H:%M:%SZ";
const REVERT_WINDOW: Duration = Duration::days(30);
/// Closed issues are still checked this long, so a reopen is seen.
const REOPEN_WINDOW: Duration = Duration::days(7);
/// KPIs count only what came after this, so nothing from before agentd (or core) watched is half-known.
const FLOOR_KEY: &str = "kpi_floor";

/// Linear's and GitHub's ISO times (milliseconds, Z or an offset) in the database's format.
pub fn iso(value: Option<&str>) -> Option<String> {
    let v = value.filter(|v| !v.is_empty())?;
    DateTime::parse_from_rfc3339(v).ok().map(|t| t.with_timezone(&Utc).format(FMT).to_string())
}

fn text(v: &Value) -> Option<String> {
    v.as_str().map(str::to_string)
}

/// Every tracked task's PR that can still change: waiting for review, or merged within the revert window. The ids
/// polled.
pub async fn pr_stats(s: &Shared, now: DateTime<Utc>) -> Result<Vec<String>> {
    let Some(gh) = &s.github else { return Ok(Vec::new()) };
    let cutoff = (now - REVERT_WINDOW).format(FMT).to_string();
    let ids: Vec<String> = s.db.lock().prepare(
        "SELECT t.id FROM tasks t LEFT JOIN pr_stats s ON s.task_id = t.id WHERE t.pr_number IS NOT NULL AND \
         (t.status = 'ready_for_review' OR (t.status = 'merged' AND s.reverted_at IS NULL AND \
         (s.merged_at IS NULL OR s.merged_at >= ?))) ORDER BY t.id")?
        .query_map([cutoff], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    for id in &ids {
        let Some(task) = tasks::get(&s.db.lock(), id)? else { continue };
        if let Err(e) = update_pr(s, gh, &task, now).await { // one PR that can't be read doesn't hold up the rest
            tracing::warn!("couldn't read the PR of {id}: {e:#}");
        }
    }
    Ok(ids)
}

async fn update_pr(s: &Shared, gh: &crate::github::GitHub, task: &Task, now: DateTime<Utc>) -> Result<()> {
    let (Some(pack), Some(n)) = (s.packs.get(&task.project), task.pr_number) else { return Ok(()) };
    let (repo, me) = (&pack.project.repo, s.config.github.as_str());
    let pr = gh.pr(repo, n).await?;
    let reviews: Vec<Value> = gh.reviews(repo, n).await?.into_iter()
        .filter(|r| r["user"]["login"] != me && crate::records::truthy(r.get("submitted_at"))).collect();
    let human: Vec<Value> = gh.pr_commits(repo, n, me).await?.into_iter().filter(|c| c["author"]["login"] != me).collect();
    let events = gh.issue_events(repo, n).await?;
    let mut ready = events.iter().find(|e| e["event"] == "ready_for_review").and_then(|e| text(&e["created_at"]));
    if ready.is_none() && !crate::records::truthy(pr.get("draft")) {
        ready = text(&pr["created_at"]);
    }
    let prev: Option<(Option<String>, Option<String>)> = s.db.lock().query_row(
        "SELECT reverted_at, revert_ref FROM pr_stats WHERE task_id = ?", [&task.id], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
    let (mut reverted_at, mut revert_ref) = prev.unwrap_or_default();
    let merged_at = text(&pr["merged_at"]);
    let merge_sha = if crate::records::truthy(pr.get("merged")) { text(&pr["merge_commit_sha"]) } else { None };
    let lines: i64 = human.iter().map(|c| c["stats"]["additions"].as_i64().unwrap_or(0) + c["stats"]["deletions"].as_i64().unwrap_or(0)).sum();
    if let Some(merged) = merged_at.as_deref().and_then(|m| DateTime::parse_from_rfc3339(m).ok())
        && reverted_at.is_none() && now - merged.with_timezone(&Utc) <= REVERT_WINDOW {
        let base = pr["base"]["ref"].as_str().filter(|b| !b.is_empty()).unwrap_or(&pack.project.default_branch);
        let title = pr["title"].as_str().map_or("None".to_string(), str::to_string);
        for c in gh.commits_since(repo, base, merged_at.as_deref().unwrap_or_default()).await? {
            let message = c["commit"]["message"].as_str().unwrap_or_default();
            if merge_sha.as_deref().is_some_and(|sha| !sha.is_empty() && message.contains(&format!("This reverts commit {sha}")))
                || message.starts_with(&format!("Revert \"{title}\"")) {
                reverted_at = text(&c["commit"]["author"]["date"]);
                revert_ref = text(&c["sha"]);
                break;
            }
        }
    }
    s.db.lock().execute(
        "INSERT INTO pr_stats(task_id, repo, pr_number, opened_at, ready_at, first_human_review_at, merged_at, closed_at, \
         merge_sha, human_commits, human_lines_changed, reverted_at, revert_ref, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET repo = excluded.repo, \
         pr_number = excluded.pr_number, opened_at = excluded.opened_at, ready_at = excluded.ready_at, \
         first_human_review_at = excluded.first_human_review_at, merged_at = excluded.merged_at, \
         closed_at = excluded.closed_at, merge_sha = excluded.merge_sha, human_commits = excluded.human_commits, \
         human_lines_changed = excluded.human_lines_changed, reverted_at = excluded.reverted_at, \
         revert_ref = excluded.revert_ref, updated_at = excluded.updated_at",
        params![task.id, repo, n, text(&pr["created_at"]), ready,
                reviews.iter().filter_map(|r| r["submitted_at"].as_str()).min(), merged_at, text(&pr["closed_at"]),
                merge_sha, human.len() as i64, lines, reverted_at, revert_ref, utcnow()])?;
    if task.status == Status::ReadyForReview && pr["state"] == "closed" {
        let to = if crate::records::truthy(pr.get("merged")) { Status::Merged } else { Status::Closed };
        let changed = tasks::transition(&s.db.lock(), &task.id, to, "github", Some(json!({"pr_number": n})))?;
        crate::notifier::task_changed(s, &changed).await?;
    }
    Ok(())
}

pub fn kpi_floor(c: &rusqlite::Connection) -> Result<String> {
    if let Some(floor) = get_setting(c, FLOOR_KEY)? {
        return Ok(floor);
    }
    let floor = utcnow();
    set_setting(c, FLOOR_KEY, &floor)?;
    Ok(floor)
}

/// The identifier the texts mention as a whole token, earlier texts first (branch, then title, then body, so a body
/// that also cites another issue doesn't win): `pra-423-x` mentions PRA-423, `PRA-4230` doesn't.
pub fn match_issue(texts: &[Option<&str>], identifiers: &[String]) -> Option<String> {
    let mut ids: Vec<&String> = identifiers.iter().collect();
    ids.sort_by_key(|i| std::cmp::Reverse(i.chars().count()));
    for text in texts.iter().flatten().filter(|t| !t.is_empty()) {
        let hay = text.to_lowercase();
        for id in &ids {
            let needle = id.to_lowercase();
            let found = hay.match_indices(&needle).any(|(i, m)| {
                !hay[..i].chars().next_back().is_some_and(|c| c.is_ascii_alphanumeric())
                    && !hay[i + m.len()..].chars().next().is_some_and(|c| c.is_ascii_digit())
            });
            if found {
                return Some((*id).clone());
            }
        }
    }
    None
}

/// New PRs come from the search; ones already stored and still open are refreshed directly, so a PR pushed off the
/// search's first page still gets its merge recorded. How many were looked at.
pub async fn agent_prs(s: &Shared) -> Result<usize> {
    let (Some(gh), login) = (&s.github, &s.config.github) else { return Ok(0) };
    if login.is_empty() {
        return Ok(0);
    }
    let floor = kpi_floor(&s.db.lock())?;
    let hits = gh.search_prs(&format!("is:pr author:{login} created:>={floor}")).await?;
    let mut todo: Vec<(String, i64)> = Vec::new();
    let mut add = |repo: String, n: i64| if !todo.contains(&(repo.clone(), n)) { todo.push((repo, n)) };
    for h in &hits {
        let url = h["repository_url"].as_str().unwrap_or_default().trim_end_matches('/');
        let parts: Vec<&str> = url.rsplitn(3, '/').collect();
        if let (Some(n), [name, owner, ..]) = (h["number"].as_i64(), parts.as_slice()) {
            add(format!("{owner}/{name}"), n);
        }
    }
    let (open, identifiers): (Vec<(String, i64)>, Vec<String>) = {
        let c = s.db.lock();
        let open = c.prepare("SELECT repo, number FROM agent_prs WHERE merged_at IS NULL AND closed_at IS NULL")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
        let ids = c.prepare("SELECT identifier FROM linear_issues")?.query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        (open, ids)
    };
    for (repo, n) in open {
        add(repo, n);
    }
    for (repo, n) in &todo {
        let known: Option<(Option<String>, Option<String>, Option<String>)> = s.db.lock().query_row(
            "SELECT merged_at, closed_at, issue_identifier FROM agent_prs WHERE repo = ? AND number = ?",
            params![repo, n], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional()?;
        if let Some((merged, closed, _)) = &known && (merged.is_some() || closed.is_some()) {
            continue;
        }
        let pr = match gh.pr(repo, *n).await {
            Ok(pr) => pr,
            Err(e) => {
                tracing::warn!("couldn't read PR {repo}#{n}: {e:#}");
                continue;
            }
        };
        let head = text(&pr["head"]["ref"]);
        let issue = known.and_then(|k| k.2).filter(|i| !i.is_empty()).or_else(|| {
            match_issue(&[head.as_deref(), pr["title"].as_str(), pr["body"].as_str()], &identifiers)
        });
        s.db.lock().execute(
            "INSERT INTO agent_prs(repo, number, url, title, head, base, opened_at, merged_at, closed_at, issue_identifier, \
             updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(repo, number) DO UPDATE SET \
             url = excluded.url, title = excluded.title, head = excluded.head, base = excluded.base, \
             opened_at = excluded.opened_at, merged_at = excluded.merged_at, closed_at = excluded.closed_at, \
             issue_identifier = excluded.issue_identifier, updated_at = excluded.updated_at",
            params![repo, n, text(&pr["html_url"]), text(&pr["title"]), head, text(&pr["base"]["ref"]),
                    iso(pr["created_at"].as_str()), iso(pr["merged_at"].as_str()), iso(pr["closed_at"].as_str()), issue,
                    utcnow()])?;
    }
    Ok(todo.len())
}

/// Each handed-over issue that is open, or closed within the last week, as Linear has it now. How many were asked.
pub async fn linear_issues(s: &Shared, now: DateTime<Utc>) -> Result<usize> {
    let Some(linear) = &s.linear else { return Ok(0) };
    let recent = (now - REOPEN_WINDOW).format(FMT).to_string();
    let ids: Vec<String> = s.db.lock().prepare(
        "SELECT issue_id FROM linear_issues WHERE (completed_at IS NULL AND canceled_at IS NULL) \
         OR completed_at >= ? OR canceled_at >= ? ORDER BY handed_at")?
        .query_map([&recent, &recent], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    for id in &ids {
        let (kind, completed, canceled) = match linear.issue_state(s, id).await {
            Ok(state) => state,
            Err(e) => {
                tracing::warn!("couldn't read Linear issue {id}: {e:#}");
                continue;
            }
        };
        s.db.lock().execute(
            "UPDATE linear_issues SET state_type = ?, completed_at = ?, canceled_at = ?, checked_at = ? WHERE issue_id = ?",
            params![kind, iso(completed.as_deref().filter(|_| kind.as_deref() == Some("completed"))),
                    iso(canceled.as_deref().filter(|_| kind.as_deref() == Some("canceled"))),
                    now.format(FMT).to_string(), id])?;
    }
    Ok(ids.len())
}
