//! What a turn starts with: the work item's payload, refreshed when the turn starts (a port of core/payloads.py).

use anyhow::Result;
use serde_json::{Map, Value, json};

use crate::conversations::Convo;
use crate::shared::Shared;

/// The system prompt: the install's own (config/agent/system.md) or the shipped one, with the name, the projects and
/// the long projects being built filled in, then the persona.
pub fn system_prompt(s: &Shared) -> Result<String> {
    let custom = s.paths.config.join("agent").join("system.md");
    let path = if custom.is_file() { custom } else { s.paths.defaults.join("system.md") };
    let text = std::fs::read_to_string(&path)?.replace("{name}", &s.config.name).replace("{projects}", &projects(s))
        .replace("{active_projects}", &active_projects(s)?);
    Ok([text.trim(), s.persona.as_str()].iter().filter(|p| !p.is_empty()).copied().collect::<Vec<_>>().join("\n\n"))
}

fn projects(s: &Shared) -> String {
    let lines: Vec<String> = s.packs.iter().map(|(name, pack)| {
        let p = &pack.project;
        let slack = if p.slack_channel.is_empty() { String::new() } else { format!(", Slack {}", p.slack_channel) };
        format!("- {name}: https://github.com/{} (default branch {}{slack})", p.repo, p.default_branch)
    }).collect();
    if lines.is_empty() { "- none configured".into() } else { lines.join("\n") }
}

fn active_projects(s: &Shared) -> Result<String> {
    let c = s.db.lock();
    let rows: Vec<(String, String, Option<String>, String)> = c.prepare(
        "SELECT name, repo, thread_link, conversation_id FROM projects WHERE status = 'active' ORDER BY name")?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
    let mut lines = Vec::new();
    for (name, repo, link, convo) in rows {
        lines.push(format!("- {name}: https://github.com/{repo}, thread {}, build {}",
                           link.unwrap_or_else(|| "(none)".into()), crate::projects::build_state(&c, &convo)?));
    }
    Ok(if lines.is_empty() { "- none right now".into() } else { lines.join("\n") })
}

/// Every pack's test-only keys, for every session.
pub fn test_secrets(s: &Shared) -> Map<String, Value> {
    s.packs.values().flat_map(|p| p.project.secrets_worker.iter())
        .filter_map(|k| s.secrets.get(k).map(|v| (k.clone(), json!(v)))).collect()
}

/// A new turn's payload. `${VAR}` in MCP headers stays unexpanded: it is filled from the session's environment.
pub fn turn(s: &Shared, convo: &Convo, text: &str, actor: Option<&str>, unattended: bool) -> Result<Value> {
    let mcp: Map<String, Value> = s.config.mcp.iter()
        .map(|(n, m)| (n.clone(), json!({"type": m.kind, "url": m.url, "headers": m.headers}))).collect();
    let payload = json!({"text": text, "actor": actor, "unattended": unattended,
                         "model": convo.model.clone().unwrap_or_else(|| s.config.models.default.clone()),
                         "mcp": mcp, "conversation_id": convo.id});
    refresh(s, convo, payload)
}

/// The turn with its conversation as it is now: the session to resume and its account, the project, the test keys,
/// the system prompt and the budgets. Done again when the turn starts, since a turn queued while another ran must
/// resume the session that one ended with. The text stays as queued.
pub fn refresh(s: &Shared, convo: &Convo, mut payload: Value) -> Result<Value> {
    let schedule = payload["source"] == "schedule";
    let hours = s.config.limits.schedule_hours;
    let fields = json!({"resume_session_id": convo.session_id, "resume_account": convo.session_account,
                        "project": convo.project, "cwd": convo.cwd, "test_secrets": test_secrets(s),
                        "system_prompt": system_prompt(s)?, "idle_s": s.config.limits.idle_minutes * 60,
                        "timeout_s": if schedule && hours > 0 { json!(hours * 3600) } else { Value::Null }});
    if let (Some(p), Value::Object(f)) = (payload.as_object_mut(), fields) {
        p.extend(f);
    }
    Ok(payload)
}
