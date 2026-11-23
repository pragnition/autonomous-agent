//! Logging a CLI in with a device code: code-driven, the model only asks (a port of core/cli_admin.py). The URL and
//! code go by DM to the admin who asked, never to a channel.

use std::process::Stdio;
use std::sync::LazyLock;
use std::time::Duration;

use anyhow::Result;
use regex::Regex;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use crate::config::{CliRules, Config};
use crate::shared::Shared;
use crate::tools::{Spec, ToolContext};

const LOGIN_TIMEOUT: Duration = Duration::from_secs(15 * 60);
static URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https://\S+").expect("valid"));
static CODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)enter the code\s+([A-Za-z0-9-]+)").expect("valid"));

pub fn parse_device_code(output: &str) -> Option<(String, String)> {
    let (url, code) = (URL.find(output)?, CODE.captures(output)?);
    Some((url.as_str().trim_end_matches('.').to_string(), code[1].to_string()))
}

fn command_name(rules: &CliRules) -> String {
    std::path::Path::new(&rules.command).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// `<command>_login` for each CLI with a login.
pub fn tools(config: &Config) -> Vec<Spec> {
    config.cli.values().filter(|r| r.login.as_ref().is_some_and(|l| !l.is_empty())).map(|r| {
        let name = command_name(r);
        Spec { name: format!("{name}_login"), description: format!("Log {name} in with a device code; the code goes to you by DM."),
               input_schema: json!({"type": "object", "properties": {}, "required": []}), tier: "admin".into() }
    }).collect()
}

pub async fn start(s: &Shared, ctx: &ToolContext, tool: &str) -> Result<String> {
    let Some(rules) = s.config.cli.values().find(|r| format!("{}_login", command_name(r)) == tool).cloned() else {
        return Ok("no login is configured for this CLI".into());
    };
    let (Some(login), Some(person)) = (rules.login.clone().filter(|l| !l.is_empty()), ctx.actor.as_deref().and_then(|a| s.config.people.get(a)))
        else { return Ok("no login is configured for this CLI".into()) };
    let mut child = Command::new(&login[0]).args(&login[1..]).envs(&rules.env).stdin(Stdio::null())
        .stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(false).spawn()?;
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    for out in [child.stdout.take().map(|o| Box::new(o) as Box<dyn tokio::io::AsyncRead + Unpin + Send>),
                child.stderr.take().map(|e| Box::new(e) as Box<dyn tokio::io::AsyncRead + Unpin + Send>)].into_iter().flatten() {
        let tx = tx.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = tx.send(line);
            }
        });
    }
    drop(tx);
    let mut seen = String::new();
    let found = tokio::time::timeout(Duration::from_secs(60), async {
        while let Some(line) = rx.recv().await {
            seen += &line;
            seen.push('\n');
            if let Some(found) = parse_device_code(&seen) {
                return Some(found);
            }
        }
        None
    }).await.ok().flatten();
    let Some((url, code)) = found else {
        let tail: String = seen.chars().rev().take(500).collect::<Vec<_>>().into_iter().rev().collect();
        return Ok(format!("the login didn't print a device code:\n{tail}"));
    };
    let dm = s.slack.open_dm(&person.slack).await?;
    s.slack.post(&dm, None, &format!("{} login: open {url} and enter {code}. It expires in 15 minutes.", command_name(&rules))).await?;
    let (slack, channel, thread) = (s.slack.clone(), ctx.channel.clone(), ctx.thread_ts.clone());
    tokio::spawn(async move {
        let text = match tokio::time::timeout(LOGIN_TIMEOUT, child.wait()).await {
            Err(_) => "The login didn't complete (TimeoutError).".to_string(),
            Ok(_) => match check(&rules).await {
                Some(info) => format!("Logged in as {} ({}).", info["user"]["name"].as_str().unwrap_or("?"), info["name"].as_str().unwrap_or("?")),
                None => "The login didn't complete (JSONDecodeError).".to_string(),
            },
        };
        if let Some(channel) = channel {
            let _ = slack.post(&channel, thread.as_deref(), &text).await;
        }
    });
    Ok(format!("Sent the login code to {} by DM.", person.key))
}

async fn check(rules: &CliRules) -> Option<Value> {
    let check = rules.login_check.clone().filter(|c| !c.is_empty())?;
    let out = Command::new(&check[0]).args(&check[1..]).envs(&rules.env).stdin(Stdio::null()).output().await.ok()?;
    serde_json::from_slice(&out.stdout).ok()
}
