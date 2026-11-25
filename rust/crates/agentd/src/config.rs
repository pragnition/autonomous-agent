//! agent.toml and the packs (a port of src/agent/config.py), plus the [containers] settings agentd adds.
//!
//! Every problem is collected before giving up, in the same order and words as core, so `doctor` shows them all.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde::Serialize;
use toml::{Table, Value};

const PERSON_ROLES: [&str; 2] = ["member", "admin"];
/// The stack's own credentials: never available to a pack, whatever the pack declares.
const RESERVED_SECRETS: [&str; 6] = ["GITHUB_WRITE_TOKEN", "GITHUB_READ_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
                                     "SLACK_ALERT_WEBHOOK", "LINEAR_API_KEY"];
static HHMM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([01][0-9]|2[0-3]):[0-5][0-9]$").unwrap());
static MODEL_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^claude-[a-z0-9][a-z0-9.-]*$").unwrap());
static UPSTREAM_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]*$").unwrap());

#[derive(Debug, Clone, Serialize)]
pub struct Person {
    pub key: String,
    pub slack: String,
    pub github: String,
    pub linear: String,
    pub role: String,
    pub tailscale: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentSettings {
    pub uid: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct LinearSettings {
    pub public_url: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct McpServer {
    #[serde(rename = "type")]
    pub kind: String,
    pub url: String,
    pub headers: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ObservabilitySettings {
    pub digest_channel: String,
    pub digest_time: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Limits {
    pub max_concurrent_sessions: i64,
    pub max_tasks_per_day: i64,
    pub idle_minutes: i64,
    pub schedule_hours: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Models {
    pub default: String,
    pub hard: String,
    pub allowed: Vec<String>,
}

impl Models {
    /// Every model a conversation may be switched to: default, hard and the allowed list, in that order.
    pub fn choices(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for m in [&self.default, &self.hard].into_iter().chain(&self.allowed) {
            if !out.contains(m) {
                out.push(m.clone());
            }
        }
        out
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CliRules {
    pub command: String,
    pub env: BTreeMap<String, String>,
    pub login: Option<Vec<String>>,
    pub login_check: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DashboardSettings {
    pub port: i64,
    pub trusted_peers: Vec<String>,
    pub agent_origins: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LlmUpstream {
    pub base_url: String,
    pub key: String,
    pub header: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct LlmPrice {
    pub input: f64,
    pub cached_input: f64,
    pub output: f64,
    pub cache_write: Option<f64>,
    pub addon: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct LlmMeterSettings {
    pub port: i64,
    pub daily_usd: f64,
    pub monthly_usd: f64,
    pub upstreams: BTreeMap<String, LlmUpstream>,
    pub prices: BTreeMap<String, LlmPrice>,
}

/// agentd's own: one container per conversation.
#[derive(Debug, Clone, Serialize)]
pub struct Containers {
    pub image: String,
    pub memory_gib: i64,
    pub cpus: f64,
    pub pids: i64,
    pub idle_stop_minutes: i64,
    pub max_age_hours: i64,
    pub keep_stopped_days: i64,
    pub min_free_gib: i64,
}

/// agentd's own: freeing disk in the conversations' folders (see `cleanup`).
#[derive(Debug, Clone, Serialize)]
pub struct Cleanup {
    /// A folder quiet this many days loses what can be rebuilt; 0 turns the cleanup off.
    pub idle_days: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Config {
    pub name: String,
    pub timezone: String,
    pub people: BTreeMap<String, Person>,
    pub limits: Limits,
    pub models: Models,
    pub agent: AgentSettings,
    pub cli: BTreeMap<String, CliRules>,
    pub observability: ObservabilitySettings,
    pub linear: LinearSettings,
    pub mcp: BTreeMap<String, McpServer>,
    pub github: String,
    pub dashboard: DashboardSettings,
    pub llm_meter: LlmMeterSettings,
    pub containers: Containers,
    pub cleanup: Cleanup,
}

impl Config {
    pub fn person_by_slack(&self, user_id: &str) -> Option<&Person> {
        self.people.values().find(|p| p.slack == user_id)
    }

    pub fn person_by_linear(&self, email: &str) -> Option<&Person> {
        (!email.is_empty()).then(|| self.people.values().find(|p| p.linear.eq_ignore_ascii_case(email))).flatten()
    }

    pub fn person_by_tailscale(&self, login: &str) -> Option<&Person> {
        (!login.is_empty())
            .then(|| self.people.values().find(|p| !p.tailscale.is_empty() && p.tailscale.eq_ignore_ascii_case(login)))
            .flatten()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Project {
    pub name: String,
    pub repo: String,
    pub default_branch: String,
    pub owner: String,
    pub slack_channel: String,
    pub linear_team: String,
    pub linear_status: BTreeMap<String, String>,
    pub secrets_worker: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Pack {
    pub project: Project,
    pub dir: PathBuf,
}

/// Python's repr of a TOML value, for messages that quote what was given.
fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) if s.contains('\'') && !s.contains('"') => format!("\"{s}\""),
        Value::String(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        Value::Boolean(b) => if *b { "True" } else { "False" }.to_string(),
        other => other.to_string(),
    }
}

struct Checker {
    problems: Vec<String>,
}

impl Checker {
    fn str(&mut self, data: &Table, key: &str, where_: &str, default: Option<&str>) -> String {
        match data.get(key).map(Value::as_str).unwrap_or(default) {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => {
                self.problems.push(format!("{where_}: '{key}' is required and must be a non-empty string"));
                String::new()
            }
        }
    }

    fn str_list(&mut self, data: &Table, key: &str, where_: &str) -> Vec<String> {
        match data.get(key) {
            None => Vec::new(),
            Some(Value::Array(items)) if items.iter().all(Value::is_str) => {
                items.iter().map(|v| v.as_str().unwrap().to_string()).collect()
            }
            Some(_) => {
                self.problems.push(format!("{where_}: '{key}' must be a list of strings"));
                Vec::new()
            }
        }
    }

    fn int(&mut self, data: &Table, key: &str, default: i64, where_: &str) -> i64 {
        match data.get(key) {
            None => default,
            Some(Value::Integer(n)) if *n >= 0 => *n,
            Some(_) => {
                self.problems.push(format!("{where_}: '{key}' must be a non-negative integer"));
                default
            }
        }
    }

    fn usd(&mut self, data: &Table, key: &str, default: f64, where_: &str) -> f64 {
        match data.get(key) {
            None => default,
            Some(Value::Integer(n)) if *n >= 0 => *n as f64,
            Some(Value::Float(f)) if *f >= 0.0 => *f,
            Some(_) => {
                self.problems.push(format!("{where_}: '{key}' must be a non-negative number (US dollars)"));
                default
            }
        }
    }

    fn cmd(&mut self, data: &Table, key: &str, where_: &str) -> Option<Vec<String>> {
        data.contains_key(key).then(|| self.str_list(data, key, where_))
    }
}

/// `data.get(key) or {}`: the table under `key`, or an empty one.
fn table<'a>(data: &'a Table, key: &str) -> std::borrow::Cow<'a, Table> {
    match data.get(key) {
        Some(Value::Table(t)) => std::borrow::Cow::Borrowed(t),
        _ => std::borrow::Cow::Owned(Table::new()),
    }
}

fn read_toml(path: &Path, problems: &mut Vec<String>) -> Table {
    match std::fs::read_to_string(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            problems.push(format!("{}: file not found", path.display()));
            Table::new()
        }
        Err(e) => {
            problems.push(format!("{}: {e}", path.display()));
            Table::new()
        }
        Ok(text) => text.parse::<Table>().unwrap_or_else(|e| {
            problems.push(format!("{}: invalid TOML ({})", path.display(), e.message()));
            Table::new()
        }),
    }
}

pub fn load(path: &Path) -> Result<Config, Vec<String>> {
    let mut problems = Vec::new();
    let data = read_toml(path, &mut problems);
    if !problems.is_empty() {
        return Err(problems);
    }
    let w = path.display().to_string();
    let mut c = Checker { problems };

    let name = c.str(&data, "name", &w, None);
    let github = if data.contains_key("github") { c.str(&data, "github", &w, None) } else { name.clone() };
    let timezone = c.str(&data, "timezone", &w, Some("Asia/Singapore"));

    let mut people = BTreeMap::new();
    for (key, p) in table(&data, "people").iter() {
        let pw = format!("{w} [people.{key}]");
        let Value::Table(p) = p else {
            c.problems.push(format!("{pw}: must be a table"));
            continue;
        };
        let role = p.get("role").map_or(Some("member"), Value::as_str);
        if !role.is_some_and(|r| PERSON_ROLES.contains(&r)) {
            c.problems.push(format!("{pw}: 'role' must be one of {}", PERSON_ROLES.join(", ")));
        }
        let text = |field: &str, c: &mut Checker| match p.get(field) {
            None => String::new(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => {
                c.problems.push(format!("{pw}: '{field}' must be a string"));
                String::new()
            }
        };
        let login = text("github", &mut c);
        let tailscale = text("tailscale", &mut c);
        let slack = c.str(p, "slack", &pw, None);
        let linear = c.str(p, "linear", &pw, None);
        people.insert(key.clone(), Person { key: key.clone(), slack, github: login, linear,
                                            role: role.unwrap_or_default().to_string(), tailscale });
    }
    if people.is_empty() {
        c.problems.push(format!("{w}: at least one [people.<key>] entry is required"));
    }

    let lim = table(&data, "limits");
    let lw = format!("{w} [limits]");
    let limits = Limits {
        max_concurrent_sessions: c.int(&lim, "max_concurrent_sessions", 10, &lw),
        max_tasks_per_day: c.int(&lim, "max_tasks_per_day", 20, &lw),
        idle_minutes: c.int(&lim, "idle_minutes", 30, &lw),
        schedule_hours: c.int(&lim, "schedule_hours", 6, &lw),
    };
    if limits.idle_minutes < 1 {
        c.problems.push(format!("{lw}: 'idle_minutes' must be at least 1"));
    }

    let claude = table(&table(&data, "claude"), "models").into_owned();
    let cw = format!("{w} [claude.models]");
    let mut models = Models { default: "claude-opus-5-5".into(), hard: "claude-fable-5-1".into(), allowed: vec![] };
    if claude.contains_key("default") {
        models.default = c.str(&claude, "default", &cw, None);
    }
    if claude.contains_key("hard") {
        models.hard = c.str(&claude, "hard", &cw, None);
    }
    if claude.contains_key("allowed") {
        models.allowed = c.str_list(&claude, "allowed", &cw);
        if !models.allowed.iter().all(|m| MODEL_ID.is_match(m)) {
            c.problems.push(format!("{cw}: 'allowed' must list model ids like \"claude-opus-4-6\""));
        }
    }

    let ag = table(&data, "agent");
    let mut agent = AgentSettings { uid: 1001 };
    match ag.get("uid") {
        None => {}
        Some(Value::Integer(n)) if *n > 0 => agent.uid = *n,
        Some(_) => c.problems.push(format!("{w} [agent]: 'uid' must be a positive integer (the agent container's uid)")),
    }

    let ob = table(&data, "observability");
    let ow = format!("{w} [observability]");
    let channel = ob.get("digest_channel").cloned().unwrap_or(Value::String(String::new()));
    let time = ob.get("digest_time").cloned().unwrap_or(Value::String("09:00".into()));
    if !channel.is_str() {
        c.problems.push(format!("{ow}: 'digest_channel' must be a string"));
    }
    if !time.as_str().is_some_and(|t| HHMM.is_match(t)) {
        c.problems.push(format!("{ow}: 'digest_time' must be HH:MM (24-hour), got {}", py_repr(&time)));
    }
    let observability = ObservabilitySettings { digest_channel: channel.as_str().unwrap_or_default().into(),
                                                digest_time: time.as_str().unwrap_or_default().into() };

    let mut cli = BTreeMap::new();
    for (cname, cdata) in table(&data, "cli").iter() {
        let Value::Table(cdata) = cdata else { continue };
        let cw = format!("{w} [cli.{cname}]");
        let env = match cdata.get("env") {
            None => BTreeMap::new(),
            Some(Value::Table(t)) if t.values().all(Value::is_str) => {
                t.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect()
            }
            Some(_) => {
                c.problems.push(format!("{cw}: 'env' must be a table of strings"));
                BTreeMap::new()
            }
        };
        let command = c.str(cdata, "command", &cw, None);
        let login = c.cmd(cdata, "login", &cw);
        let login_check = c.cmd(cdata, "login_check", &cw);
        cli.insert(cname.clone(), CliRules { command, env, login, login_check });
    }

    let lin = table(&data, "linear");
    let public_url = lin.get("public_url").cloned().unwrap_or(Value::String(String::new()));
    let truthy = !matches!(&public_url, Value::String(s) if s.is_empty()) && !matches!(public_url, Value::Boolean(false))
        && !matches!(public_url, Value::Integer(0));
    if truthy && !public_url.as_str().is_some_and(|u| u.starts_with("https://")) {
        c.problems.push(format!("{w} [linear]: 'public_url' must be an https:// URL"));
    }
    let linear = LinearSettings { public_url: public_url.as_str().unwrap_or_default().trim_end_matches('/').into() };

    let mut mcp = BTreeMap::new();
    for (mname, mdata) in table(&data, "mcp").iter() {
        let Value::Table(mdata) = mdata else { continue };
        let mw = format!("{w} [mcp.{mname}]");
        let kind = mdata.get("type").cloned().unwrap_or(Value::String("http".into()));
        let url = mdata.get("url").cloned().unwrap_or(Value::String(String::new()));
        if kind.as_str() != Some("http") {
            c.problems.push(format!("{mw}: 'type' must be \"http\" (streamable HTTP), got {}", py_repr(&kind)));
        }
        if !url.as_str().is_some_and(|u| u.starts_with("https://")) {
            c.problems.push(format!("{mw}: 'url' must be an https:// URL"));
        }
        let headers = match mdata.get("headers") {
            None => BTreeMap::new(),
            Some(Value::Table(t)) if t.values().all(Value::is_str) => {
                t.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect()
            }
            Some(_) => {
                c.problems.push(format!("{mw}: 'headers' must be a table of strings"));
                BTreeMap::new()
            }
        };
        mcp.insert(mname.clone(), McpServer { kind: kind.as_str().unwrap_or_default().into(),
                                              url: url.as_str().unwrap_or_default().into(), headers });
    }

    let dash = table(&data, "dashboard");
    let dw = format!("{w} [dashboard]");
    let port = c.int(&dash, "port", 8086, &dw);
    let trusted_peers = if dash.contains_key("trusted_peers") { c.str_list(&dash, "trusted_peers", &dw) }
                        else { vec!["172.30.0.1".into()] };
    let agent_origins = if dash.contains_key("agent_origins") { c.str_list(&dash, "agent_origins", &dw) }
                        else { vec!["agent-core".into(), "agent-sandbox".into()] };
    let dashboard = DashboardSettings { port, trusted_peers, agent_origins };

    let llm_meter = llm_meter(&table(&data, "llm_meter"), &w, &mut c);

    let ct = table(&data, "containers");
    let tw = format!("{w} [containers]");
    let image = match ct.get("image") {
        None => "autonomous-agent-agent".to_string(),
        Some(_) => c.str(&ct, "image", &tw, None),
    };
    let cpus = match ct.get("cpus") {
        None => 4.0,
        Some(Value::Integer(n)) if *n > 0 => *n as f64,
        Some(Value::Float(f)) if *f > 0.0 => *f,
        Some(_) => {
            c.problems.push(format!("{tw}: 'cpus' must be a positive number"));
            4.0
        }
    };
    let containers = Containers {
        image,
        memory_gib: c.int(&ct, "memory_gib", 8, &tw),
        cpus,
        pids: c.int(&ct, "pids", 4096, &tw),
        idle_stop_minutes: c.int(&ct, "idle_stop_minutes", 60, &tw),
        max_age_hours: c.int(&ct, "max_age_hours", 24, &tw),
        keep_stopped_days: c.int(&ct, "keep_stopped_days", 7, &tw),
        min_free_gib: c.int(&ct, "min_free_gib", 3, &tw),
    };

    let cleanup = Cleanup { idle_days: c.int(&table(&data, "cleanup"), "idle_days", 3, &format!("{w} [cleanup]")) };

    if !c.problems.is_empty() {
        return Err(c.problems);
    }
    Ok(Config { name, timezone, people, limits, models, agent, cli, observability, linear, mcp, github, dashboard,
                llm_meter, containers, cleanup })
}

fn llm_meter(data: &Table, w: &str, c: &mut Checker) -> LlmMeterSettings {
    let mw = format!("{w} [llm_meter]");
    let mut upstreams = BTreeMap::new();
    for (name, up) in table(data, "upstreams").iter() {
        let uw = format!("{mw}.upstreams.{name}");
        if !UPSTREAM_NAME.is_match(name) {
            c.problems.push(format!("{uw}: the name goes in a URL path; use lowercase letters, digits and '-'"));
        }
        let Value::Table(up) = up else {
            c.problems.push(format!("{uw}: must be a table"));
            continue;
        };
        let base = match up.get("base_url").and_then(Value::as_str) {
            Some(b) if b.starts_with("https://") => b.trim_end_matches('/').to_string(),
            _ => {
                c.problems.push(format!("{uw}: 'base_url' must be an https:// URL"));
                String::new()
            }
        };
        let header = up.get("header").cloned().unwrap_or(Value::String("api-key".into()));
        if !matches!(header.as_str(), Some("api-key" | "Authorization")) {
            c.problems.push(format!("{uw}: 'header' must be \"api-key\" or \"Authorization\""));
        }
        let key = c.str(up, "key", &uw, None);
        upstreams.insert(name.clone(), LlmUpstream { base_url: base, key,
                                                     header: header.as_str().unwrap_or_default().into() });
    }
    let mut prices = BTreeMap::new();
    for (model, price) in table(data, "prices").iter() {
        let numbers: Option<Vec<f64>> = match price {
            Value::Array(items) if items.len() == 3 => items.iter().map(|v| match v {
                Value::Integer(n) if *n >= 0 => Some(*n as f64),
                Value::Float(f) if *f >= 0.0 => Some(*f),
                _ => None,
            }).collect(),
            _ => None,
        };
        match numbers {
            Some(n) => {
                prices.insert(model.clone(), LlmPrice { input: n[0], cached_input: n[1], output: n[2],
                                                        cache_write: None, addon: 0.0 });
            }
            None => c.problems.push(format!(
                "{mw}.prices: '{model}' must be [input, cached input, output] in USD per million tokens")),
        }
    }
    if !upstreams.is_empty() && !prices.contains_key("default") {
        c.problems.push(format!("{mw}.prices: add a 'default' price for models not listed, so every call is counted"));
    }
    LlmMeterSettings { port: c.int(data, "port", 8087, &mw), daily_usd: c.usd(data, "daily_usd", 10.0, &mw),
                       monthly_usd: c.usd(data, "monthly_usd", 100.0, &mw), upstreams, prices }
}

pub fn load_pack(pack_dir: &Path, agent: &Config) -> Result<Pack, Vec<String>> {
    let mut problems = Vec::new();
    let path = pack_dir.join("project.toml");
    let data = read_toml(&path, &mut problems);
    if !problems.is_empty() {
        return Err(problems);
    }
    let w = path.display().to_string();
    let mut c = Checker { problems };
    let proj = table(&data, "project");
    let pw = format!("{w} [project]");
    let secrets = table(&data, "secrets");
    let project = Project {
        name: c.str(&proj, "name", &pw, None),
        repo: c.str(&proj, "repo", &pw, None),
        default_branch: c.str(&proj, "default_branch", &pw, Some("main")),
        owner: c.str(&proj, "owner", &pw, None),
        slack_channel: c.str(&proj, "slack_channel", &pw, None),
        linear_team: c.str(&proj, "linear_team", &pw, None),
        linear_status: table(&table(&data, "linear"), "status").iter()
            .map(|(k, v)| (k.clone(), v.as_str().map_or_else(|| v.to_string(), String::from))).collect(),
        secrets_worker: c.str_list(&secrets, "worker", &format!("{w} [secrets]")),
    };
    let mut reserved: Vec<&str> =
        RESERVED_SECRETS.iter().copied().filter(|r| project.secrets_worker.iter().any(|s| s == r)).collect();
    reserved.sort();
    for name in reserved {
        c.problems.push(format!("{w} [secrets]: {name} is the stack's own credential and cannot be given to a pack"));
    }
    if !project.owner.is_empty() && !agent.people.contains_key(&project.owner) {
        c.problems.push(format!("{pw}: owner '{}' is not in agent.toml [people]", project.owner));
    }
    let plugin = pack_dir.join("plugin");
    if !plugin.join(".claude-plugin").join("plugin.json").is_file() {
        c.problems.push(format!("{}: missing .claude-plugin/plugin.json", plugin.display()));
    }
    if !c.problems.is_empty() {
        return Err(c.problems);
    }
    Ok(Pack { project, dir: pack_dir.to_path_buf() })
}

pub fn load_packs(config_dir: &Path, agent: &Config) -> Result<BTreeMap<String, Pack>, Vec<String>> {
    let mut problems = Vec::new();
    let mut packs = BTreeMap::new();
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(config_dir.join("packs")).into_iter().flatten().flatten()
        .map(|e| e.path()).filter(|p| p.is_dir()).collect();
    dirs.sort();
    for dir in dirs {
        match load_pack(&dir, agent) {
            Ok(pack) => {
                packs.insert(pack.project.name.clone(), pack);
            }
            Err(p) => problems.extend(p),
        }
    }
    if problems.is_empty() { Ok(packs) } else { Err(problems) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn expected() -> serde_json::Map<String, Value> {
        serde_json::from_str(&std::fs::read_to_string("tests/fixtures/config/expected.json").unwrap()).unwrap()
    }

    #[test]
    fn problems_match_python_for_every_case() {
        for (case, want) in expected() {
            let Some(problems) = want.get("problems") else { continue };
            let problems: Vec<String> = serde_json::from_value(problems.clone()).unwrap();
            let got = if let Some(dir) = case.strip_suffix("/packs") {
                match load(&Path::new(dir).join("agent.toml")) {
                    Ok(agent) => load_packs(Path::new(dir), &agent).err().unwrap_or_default(),
                    Err(p) => p,
                }
            } else {
                load(Path::new(&case)).err().unwrap_or_default()
            };
            if case.ends_with("invalid.toml") {
                assert_eq!(got.len(), 1);
                assert!(got[0].starts_with(&format!("{case}: invalid TOML (")), "{}", got[0]);
            } else {
                assert_eq!(got, problems, "{case}");
            }
        }
    }

    #[test]
    fn example_config_matches_python() {
        let want = &expected()["../../../examples/config/agent.toml"]["config"];
        let cfg = load(Path::new("../../../examples/config/agent.toml")).unwrap();
        let got = serde_json::to_value(&cfg).unwrap();
        for (key, value) in want.as_object().unwrap() {
            assert_eq!(&got[key], value, "{key}");
        }
        let packs = load_packs(Path::new("../../../examples/config"), &cfg).unwrap();
        assert_eq!(serde_json::to_value(packs.keys().collect::<Vec<_>>()).unwrap(),
                   expected()["../../../examples/config/packs"]["packs"]);
    }

    #[test]
    fn container_defaults() {
        let cfg = load(Path::new("../../../examples/config/agent.toml")).unwrap();
        let c = &cfg.containers;
        assert_eq!((c.image.as_str(), c.memory_gib, c.cpus, c.pids), ("autonomous-agent-agent", 8, 4.0, 4096));
        assert_eq!((c.idle_stop_minutes, c.max_age_hours, c.keep_stopped_days, c.min_free_gib), (60, 24, 7, 3));
        assert_eq!(cfg.cleanup.idle_days, 3);
    }

    #[test]
    fn cleanup_idle_days_is_read_and_checked() {
        let dir = tempfile::tempdir().unwrap();
        let base = std::fs::read_to_string("../../../examples/config/agent.toml").unwrap();
        let path = dir.path().join("agent.toml");
        std::fs::write(&path, format!("{base}\n[cleanup]\nidle_days = 0\n")).unwrap();
        assert_eq!(load(&path).unwrap().cleanup.idle_days, 0);
        std::fs::write(&path, format!("{base}\n[cleanup]\nidle_days = -1\n")).unwrap();
        assert!(load(&path).unwrap_err().iter().any(|p| p.contains("[cleanup]: 'idle_days'")));
    }
}
