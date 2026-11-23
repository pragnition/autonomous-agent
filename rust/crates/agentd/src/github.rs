//! agentd's read-only GitHub client and the agent's GitHub read tools (a port of connectors/github_read.py and
//! core/tracker_tools.py): agentd holds the token; the agent gets text back.

use std::time::Duration;

use anyhow::Result;
use serde_json::Value;

use crate::shared::Shared;
use crate::tools::ToolContext;

const MAX_FILE_CHARS: usize = 20_000;

#[derive(Clone)]
pub struct GitHub {
    http: reqwest::Client,
    token: String,
    api: String,
}

impl GitHub {
    pub fn new(token: &str, api: &str) -> GitHub {
        let http = reqwest::Client::builder().timeout(Duration::from_secs(30)).user_agent("agentd").build().expect("an HTTP client");
        GitHub { http, token: token.into(), api: api.trim_end_matches('/').into() }
    }

    fn get(&self, path: &str) -> reqwest::RequestBuilder {
        self.http.get(format!("{}{path}", self.api)).bearer_auth(&self.token)
            .header("Accept", "application/vnd.github+json").header("X-GitHub-Api-Version", "2022-11-28")
    }

    pub async fn json(&self, path: &str, query: &[(&str, &str)]) -> Result<Value> {
        Ok(self.get(path).query(query).send().await?.error_for_status()?.json().await?)
    }

    pub async fn text(&self, path: &str, query: &[(&str, &str)], accept: Option<&str>) -> Result<String> {
        let mut req = self.get(path).query(query);
        if let Some(accept) = accept {
            req = req.header("Accept", accept);
        }
        Ok(req.send().await?.error_for_status()?.text().await?)
    }
}

fn s(v: &Value) -> String {
    match v {
        Value::String(t) => t.clone(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

pub async fn tool(sh: &Shared, ctx: &ToolContext, name: &str, args: &Value) -> Result<String> {
    let Some(gh) = &sh.github else { return Ok(crate::tools::NOT_AVAILABLE.into()) };
    let Some(project) = ctx.project.as_ref().and_then(|p| sh.packs.get(p)).map(|p| &p.project) else {
        return Ok("this conversation isn't tied to a project".into());
    };
    let repo = &project.repo;
    let arg = |k: &str| args[k].as_str().map(str::to_string).or_else(|| args[k].as_i64().map(|n| n.to_string())).unwrap_or_default();
    Ok(match name {
        "gh_prs" => {
            let state = Some(arg("state")).filter(|v| !v.is_empty()).unwrap_or_else(|| "open".into());
            let prs = gh.json(&format!("/repos/{repo}/pulls"), &[("state", &state), ("per_page", "30")]).await?;
            let lines: Vec<String> = prs.as_array().into_iter().flatten().map(|p| format!("#{} {} ({} → {}) by {}{}",
                s(&p["number"]), s(&p["title"]), s(&p["head"]["ref"]), s(&p["base"]["ref"]),
                p["user"]["login"].as_str().unwrap_or("?"), if p["draft"] == true { " [draft]" } else { "" })).collect();
            if lines.is_empty() { "no pull requests".into() } else { lines.join("\n") }
        }
        "gh_checks" => {
            let runs = gh.json(&format!("/repos/{repo}/commits/{}/check-runs", arg("ref")), &[("per_page", "100")]).await?;
            let lines: Vec<String> = runs["check_runs"].as_array().into_iter().flatten().map(|r| format!("{}: {}/{} {}",
                s(&r["name"]), s(&r["status"]), s(&r["conclusion"]), r["details_url"].as_str().unwrap_or_default()).trim_end().to_string()).collect();
            if lines.is_empty() { "no checks for that ref".into() } else { lines.join("\n") }
        }
        "gh_ci_log" => {
            let log = gh.text(&format!("/repos/{repo}/actions/jobs/{}/logs", arg("job_id")), &[], None).await?;
            let chars: Vec<char> = log.chars().collect();
            chars[chars.len().saturating_sub(MAX_FILE_CHARS)..].iter().collect()
        }
        "gh_issue" => {
            let n = arg("number");
            let issue = gh.json(&format!("/repos/{repo}/issues/{n}"), &[]).await?;
            let comments = gh.json(&format!("/repos/{repo}/issues/{n}/comments"), &[("per_page", "50")]).await?;
            let mut lines = vec![format!("#{} {} [{}] by {}", s(&issue["number"]), s(&issue["title"]), s(&issue["state"]), s(&issue["user"]["login"])),
                                 issue["body"].as_str().unwrap_or_default().to_string(), "Comments:".into()];
            lines.extend(comments.as_array().into_iter().flatten()
                .map(|c| format!("{}: {}", c["user"]["login"].as_str().unwrap_or("?"), c["body"].as_str().unwrap_or_default())));
            lines.join("\n")
        }
        _ => {
            let reference = Some(arg("ref")).filter(|v| !v.is_empty()).unwrap_or_else(|| project.default_branch.clone());
            let text = gh.text(&format!("/repos/{repo}/contents/{}", arg("path")), &[("ref", &reference)], Some("application/vnd.github.raw")).await?;
            text.chars().take(MAX_FILE_CHARS).collect()
        }
    })
}
