//! `agentd doctor`: one pass over everything the install needs, each check reported by name (a port of doctor.py,
//! with Docker and the agent image in place of the runner token).

use std::collections::BTreeMap;

use crate::github::GitHub;
use crate::linear::{Linear, LinearClient};
use crate::proxy::ProxyClient;
use crate::shared::{Paths, Shared};
use crate::slack::{self, Slack};

pub struct Check {
    pub name: String,
    pub ok: bool,
    pub detail: String,
}

fn check(name: impl Into<String>, ok: bool, detail: impl Into<String>) -> Check {
    Check { name: name.into(), ok, detail: detail.into() }
}

/// Every check; `proxy` is the account proxy's relay for agentd's own calls, `docker` the engine sessions run on.
pub async fn run(paths: &Paths, secrets: &BTreeMap<String, String>, proxy: Option<ProxyClient>, docker: Option<bollard::Docker>,
                 github_api: &str, linear_api: &str) -> Vec<Check> {
    let config = match crate::config::load(&paths.config.join("agent.toml")) {
        Ok(c) => c,
        Err(problems) => return vec![check("config", false, problems.join("; "))],
    };
    let mut out = vec![check("config", true, format!("{} people", config.people.len()))];
    let packs = match crate::config::load_packs(&paths.config, &config) {
        Ok(p) => {
            out.push(check("packs", true, format!("{} pack(s): {}", p.len(), p.keys().cloned().collect::<Vec<_>>().join(", "))));
            p
        }
        Err(problems) => {
            out.push(check("packs", false, problems.join("; ")));
            BTreeMap::new()
        }
    };
    let mut names = vec!["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "LINEAR_API_KEY", "GITHUB_READ_TOKEN"];
    if !config.linear.public_url.is_empty() { // the Linear agent app
        names.extend(["LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET"]);
    }
    let mut meter: Vec<&str> = config.llm_meter.upstreams.values().map(|u| u.key.as_str()).collect(); // keys the meter adds
    meter.sort();
    for name in names.into_iter().chain(meter) {
        if !out.iter().any(|c| c.name == format!("secret:{name}")) {
            let set = secrets.get(name).is_some_and(|v| !v.is_empty());
            out.push(check(format!("secret:{name}"), set, if set { "set" } else { "missing" }));
        }
    }
    if let Some(proxy) = &proxy {
        match proxy.usage().await {
            Ok(usage) => {
                let verified = usage.accounts.values().filter(|a| a.verified).count();
                out.push(check("proxy", verified > 0, format!("{verified} verified account(s), {}",
                    if usage.has_capacity() { "capacity available" } else { "no capacity right now" })));
            }
            Err(e) => out.push(check("proxy", false, format!("{e:#}"))),
        }
        for model in config.models.choices() { // every model a conversation may run on
            match proxy.serves(&model).await {
                Ok(served) => out.push(check(format!("model:{model}"), served, if served { "served" } else { "the proxy doesn't serve it" })),
                Err(e) => out.push(check(format!("model:{model}"), false, format!("{e:#}"))),
            }
        }
    }
    let github = secrets.get("GITHUB_READ_TOKEN").filter(|t| !t.is_empty()).map(|t| GitHub::new(t, github_api));
    let linear_key = secrets.get("LINEAR_API_KEY").filter(|t| !t.is_empty());
    let shared = linear_key.and_then(|_| {
        Shared::open(paths.clone(), secrets.clone(), Slack::new("", slack::API), None,
                     proxy.clone().unwrap_or_else(|| ProxyClient::new("http://127.0.0.1:9", Default::default())), None).ok()
    }).map(|mut s| {
        s.linear = Some(Linear { client: LinearClient::new(linear_key.map_or("", |k| k), linear_api), app: None,
                                 webhook_secret: String::new() });
        s
    });
    for (name, pack) in &packs {
        if let Some(gh) = &github {
            let ok = gh.can_read(&pack.project.repo).await;
            out.push(check(format!("github:{}", pack.project.repo), ok, if ok { "readable" } else { "cannot read the repo" }));
        }
        if let Some(s) = &shared && !pack.project.linear_status.is_empty() && let Some(linear) = &s.linear {
            match linear.team_state_names(s, &pack.project.linear_team).await {
                Ok(existing) => {
                    let missing: Vec<&String> = pack.project.linear_status.values().filter(|st| !existing.contains(*st)).collect();
                    let detail = if missing.is_empty() { "states exist".to_string() } else {
                        format!("missing states: {}", missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "))
                    };
                    out.push(check(format!("linear:{name}"), missing.is_empty(), detail));
                }
                Err(e) => out.push(check(format!("linear:{name}"), false, format!("{e:#}"))),
            }
        }
    }
    if let Some(docker) = docker {
        match docker.ping().await {
            Ok(_) => {
                out.push(check("docker", true, "reachable"));
                let image = &config.containers.image;
                let found = docker.inspect_image(image).await.is_ok();
                out.push(check(format!("image:{image}"), found, if found { "present" } else { "not built or pulled" }));
            }
            Err(e) => out.push(check("docker", false, e.to_string())),
        }
    }
    out
}

/// The checks as `doctor` prints them, and whether all passed.
pub fn report(checks: &[Check]) -> (String, bool) {
    let text = checks.iter().map(|c| format!("{}  {}: {}\n", if c.ok { "ok  " } else { "FAIL" }, c.name, c.detail)).collect();
    (text, checks.iter().all(|c| c.ok))
}
