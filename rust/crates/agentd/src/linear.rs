//! Linear (a port of connectors/linear.py and core's linear_app.py, linear_sessions.py, linear_issues.py and
//! public_api.py): issues labelled with the agent's name become tasks; the agent's Linear app (installed with
//! actor=app) lets people delegate issues to it, and each agent-session event reaches Claude Code raw; core's own
//! Linear calls show as the agent once the app is installed. The public listener (:8085, the only thing Tailscale
//! Funnel exposes) takes the webhook and the install callback, and nothing else.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use hmac::{Hmac, Mac};
use regex::Regex;
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use crate::config::Config;
use crate::conversations::{self as cv, TurnFrom};
use crate::db::{get_setting, set_setting, utcnow};
use crate::shared::Shared;
use crate::tasks::{self, NewTask, Status, Task};
use crate::tools::ToolContext;

pub const INSTALL_DM_KEY: &str = "linear_install_dm";
const AUTHORIZE_URL: &str = "https://linear.app/oauth/authorize";
const SCOPES: &str = "read,write,app:assignable,app:mentionable";
const STATE_TTL_S: f64 = 15.0 * 60.0;
const REFRESH_BEFORE_S: f64 = 3600.0;
const TOKENS_KEY: &str = "linear_app_tokens";
const STATE_KEY: &str = "linear_oauth_state";
/// How far a webhook's own timestamp may be from now.
const MAX_SKEW_MS: f64 = 60_000.0;

const LABELLED_ISSUES: &str = "query LabelledIssues($label: String!, $team: String!) {
  issues(first: 50, filter: {labels: {name: {eq: $label}}, team: {key: {eq: $team}},
                             state: {type: {nin: [\"completed\", \"canceled\"]}}}) {
    nodes { id identifier title description url creator { email } labels { nodes { name } } }
  }
}";
const TEAM_STATES: &str = "query TeamStates($team: String!) {
  teams(filter: {key: {eq: $team}}) { nodes { id states { nodes { id name } } } }
}";
const ISSUE_UPDATE: &str = "mutation IssueUpdate($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) { success }
}";
const COMMENT_CREATE: &str = "mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}";
const AGENT_ACTIVITY: &str = "mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success }
}";
pub const ISSUE_STATE: &str = "query IssueState($id: String!) {
  issue(id: $id) { state { type } completedAt canceledAt }
}";
const VIEWER: &str = "query Viewer { viewer { id } }";

static LINK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^Linear: (https?://\S+)\s*\z").expect("valid"));

fn http() -> reqwest::Client {
    reqwest::Client::builder().timeout(Duration::from_secs(30)).build().expect("an HTTP client")
}

pub struct Linear {
    pub client: LinearClient,
    /// The agent's Linear app, when it is set up (a public URL and an OAuth client).
    pub app: Option<LinearApp>,
    pub webhook_secret: String,
}

/// GraphQL calls with the personal API key, or as the agent once its app is installed.
pub struct LinearClient {
    http: reqwest::Client,
    api_key: String,
    url: String,
    states: Mutex<HashMap<String, HashMap<String, String>>>,
}

impl LinearClient {
    pub fn new(api_key: &str, url: &str) -> LinearClient {
        LinearClient { http: http(), api_key: api_key.into(), url: url.into(), states: Mutex::default() }
    }
}

impl Linear {
    pub async fn gql(&self, s: &Shared, operation: &str, query: &str, variables: Value) -> Result<Value> {
        let app_token = match &self.app {
            Some(app) => app.token(s).await?,
            None => None,
        };
        // personal API keys go without "Bearer"
        let auth = app_token.map(|t| format!("Bearer {t}")).unwrap_or_else(|| self.client.api_key.clone());
        let body: Value = self.client.http.post(&self.client.url).header("Authorization", auth)
            .json(&json!({"query": query, "variables": variables, "operationName": operation}))
            .send().await?.error_for_status()?.json().await?;
        if body.get("errors").is_some_and(|e| !e.is_null()) {
            bail!("Linear {operation}: {}", body["errors"]);
        }
        Ok(body["data"].clone())
    }

    /// An issue's state as Linear has it: (state type, completedAt, canceledAt).
    pub async fn issue_state(&self, s: &Shared, issue_id: &str) -> Result<(Option<String>, Option<String>, Option<String>)> {
        let node = &self.gql(s, "IssueState", ISSUE_STATE, json!({"id": issue_id})).await?["issue"];
        let text = |v: &Value| v.as_str().map(str::to_string);
        Ok((text(&node["state"]["type"]), text(&node["completedAt"]), text(&node["canceledAt"])))
    }

    /// The names of a team's workflow states.
    pub async fn team_state_names(&self, s: &Shared, team: &str) -> Result<std::collections::BTreeSet<String>> {
        self.state_id(s, team, "").await?;
        Ok(self.client.states.lock().unwrap_or_else(|e| e.into_inner()).get(team)
            .map(|m| m.keys().cloned().collect()).unwrap_or_default())
    }

    async fn state_id(&self, s: &Shared, team: &str, name: &str) -> Result<Option<String>> {
        if !self.client.states.lock().unwrap_or_else(|e| e.into_inner()).contains_key(team) {
            let data = self.gql(s, "TeamStates", TEAM_STATES, json!({"team": team})).await?;
            let states = data["teams"]["nodes"].as_array().into_iter().flatten()
                .flat_map(|t| t["states"]["nodes"].as_array().cloned().unwrap_or_default())
                .filter_map(|st| Some((st["name"].as_str()?.to_string(), st["id"].as_str()?.to_string()))).collect();
            self.client.states.lock().unwrap_or_else(|e| e.into_inner()).insert(team.into(), states);
        }
        Ok(self.client.states.lock().unwrap_or_else(|e| e.into_inner()).get(team).and_then(|m| m.get(name).cloned()))
    }

    pub async fn comment(&self, s: &Shared, issue_id: &str, body: &str) -> Result<()> {
        self.gql(s, "CommentCreate", COMMENT_CREATE, json!({"issueId": issue_id, "body": body})).await.map(drop)
    }

    /// An activity on an agent session: thought, response, error or elicitation.
    pub async fn agent_activity(&self, s: &Shared, session_id: &str, kind: &str, body: &str) -> Result<()> {
        self.gql(s, "AgentActivityCreate", AGENT_ACTIVITY,
                 json!({"input": {"agentSessionId": session_id, "content": {"type": kind, "body": body}}})).await.map(drop)
    }

    /// The task's status mirrored on its issue, and a comment with the draft PR once it's ready for review.
    pub async fn sync(&self, s: &Shared, task: &Task) -> Result<()> {
        let (Some(pack), Some(issue)) = (s.packs.get(&task.project), &task.linear_issue_id) else { return Ok(()) };
        let project = &pack.project;
        if let Some(state) = project.linear_status.get(task.status.as_str()) {
            let id = self.state_id(s, &project.linear_team, state).await?
                .ok_or_else(|| anyhow!("Linear team {} has no state named {state:?}", project.linear_team))?;
            self.gql(s, "IssueUpdate", ISSUE_UPDATE, json!({"id": issue, "stateId": id})).await?;
        }
        if let (Status::ReadyForReview, Some(url)) = (task.status, &task.pr_url) {
            self.comment(s, issue, &format!("Draft PR ready for review: {url}")).await?;
        }
        Ok(())
    }
}

/// The issue's URL, from the last line the poller adds to a task's description.
pub fn issue_link(description: &str) -> Option<String> {
    LINK.captures(description).map(|c| c[1].to_string())
}

/// The agent's Linear app: an admin gets a single-use install link (15 minutes); Linear sends the code to the public
/// callback, exchanged here. Access tokens last a day and each refresh rotates the refresh token, so agentd refreshes
/// ahead of time and keeps the current one in data/runner/linear.env, which sessions read. No person handles it.
pub struct LinearApp {
    client_id: String,
    client_secret: String,
    redirect_uri: String,
    token_path: PathBuf,
    api: String,
    http: reqwest::Client,
}

fn now() -> f64 {
    chrono::Utc::now().timestamp_millis() as f64 / 1000.0
}

impl LinearApp {
    /// `api` is Linear's API host: its /oauth/token and /graphql.
    pub fn new(client_id: &str, client_secret: &str, redirect_uri: &str, token_path: PathBuf, api: &str) -> LinearApp {
        LinearApp { client_id: client_id.into(), client_secret: client_secret.into(), redirect_uri: redirect_uri.into(), token_path,
                    api: api.trim_end_matches('/').into(), http: http() }
    }

    pub fn new_authorize_url(&self, s: &Shared) -> Result<String> {
        let state = crate::turn::uuid4().replace('-', "");
        set_setting(&s.db.lock(), STATE_KEY, &json!({"state": state, "expires_at": now() + STATE_TTL_S}).to_string())?;
        Ok(format!("{AUTHORIZE_URL}?{}", serde_urlencoded::to_string([
            ("client_id", self.client_id.as_str()), ("redirect_uri", &self.redirect_uri), ("response_type", "code"), ("scope", SCOPES),
            ("actor", "app"), ("state", &state), ("prompt", "consent")])?))
    }

    /// The link's state, once and before it expires.
    pub fn check_state(&self, s: &Shared, state: &str) -> Result<bool> {
        let c = s.db.lock();
        let saved: Value = serde_json::from_str(&get_setting(&c, STATE_KEY)?.unwrap_or_else(|| "{}".into())).unwrap_or_default();
        let expected = saved["state"].as_str().unwrap_or_default();
        let same = !state.is_empty() && expected.len() == state.len()
            && expected.bytes().zip(state.bytes()).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0;
        let ok = same && now() < saved["expires_at"].as_f64().unwrap_or(0.0);
        if ok {
            set_setting(&c, STATE_KEY, "{}")?; // single use
        }
        Ok(ok)
    }

    pub async fn exchange(&self, s: &Shared, code: &str) -> Result<String> {
        let mut tokens = self.token_request(&[("code", code), ("redirect_uri", &self.redirect_uri), ("client_id", &self.client_id),
                                              ("client_secret", &self.client_secret), ("grant_type", "authorization_code")]).await?;
        let viewer: Value = self.http.post(format!("{}/graphql", self.api)).bearer_auth(tokens["access"].as_str().unwrap_or_default())
            .json(&json!({"query": VIEWER})).send().await?.error_for_status()?.json().await?;
        let user = viewer["data"]["viewer"]["id"].as_str().ok_or_else(|| anyhow!("Linear gave no viewer"))?.to_string();
        tokens["app_user_id"] = json!(user);
        self.save(s, &tokens)?;
        Ok(user)
    }

    /// The current access token, refreshed when it has under an hour left; None before the app is installed.
    pub async fn token(&self, s: &Shared) -> Result<Option<String>> {
        let stored: Value = serde_json::from_str(&get_setting(&s.db.lock(), TOKENS_KEY)?.unwrap_or_else(|| "{}".into())).unwrap_or_default();
        let Some(access) = stored["access"].as_str().filter(|a| !a.is_empty()) else { return Ok(None) };
        if stored["expires_at"].as_f64().unwrap_or(0.0) - now() >= REFRESH_BEFORE_S {
            return Ok(Some(access.to_string()));
        }
        let fresh = self.token_request(&[("grant_type", "refresh_token"), ("refresh_token", stored["refresh"].as_str().unwrap_or_default()),
                                         ("client_id", &self.client_id), ("client_secret", &self.client_secret)]).await?;
        let mut merged = stored.clone();
        if let (Some(m), Some(f)) = (merged.as_object_mut(), fresh.as_object()) {
            m.extend(f.clone());
        }
        self.save(s, &merged)?;
        Ok(merged["access"].as_str().map(str::to_string))
    }

    async fn token_request(&self, form: &[(&str, &str)]) -> Result<Value> {
        let body: Value = self.http.post(format!("{}/oauth/token", self.api)).form(form).send().await?.error_for_status()?.json().await?;
        Ok(json!({"access": body["access_token"], "refresh": body["refresh_token"].as_str().unwrap_or_default(),
                  "expires_at": now() + body["expires_in"].as_f64().unwrap_or(86399.0)}))
    }

    fn save(&self, s: &Shared, tokens: &Value) -> Result<()> {
        set_setting(&s.db.lock(), TOKENS_KEY, &tokens.to_string())?;
        let dir = self.token_path.parent().unwrap_or(std::path::Path::new("."));
        std::fs::create_dir_all(dir)?;
        let tmp = dir.join(format!(".{}.tmp", self.token_path.file_name().unwrap_or_default().to_string_lossy()));
        std::fs::write(&tmp, format!("LINEAR_APP_TOKEN={}\n", tokens["access"].as_str().unwrap_or_default()))?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
        std::fs::rename(&tmp, &self.token_path).context("writing the Linear token file")
    }

    /// The linear_install tool: the link goes to the admin who asked, by DM.
    pub async fn install_tool(&self, s: &Shared, ctx: &ToolContext) -> Result<String> {
        let Some(person) = ctx.actor.as_deref().and_then(|a| s.config.people.get(a)) else {
            return Ok("only a configured person can install the Linear app".into());
        };
        let dm = s.slack.open_dm(&person.slack).await?;
        set_setting(&s.db.lock(), INSTALL_DM_KEY, &dm)?;
        let text = format!("Install me in Linear (you need to be a Linear admin; the link works once, for 15 minutes): {}",
                           self.new_authorize_url(s)?);
        s.slack.post(&dm, None, &text).await?;
        Ok(format!("Sent the Linear install link to {} by DM.", person.key))
    }
}

/// Who asked, from a session webhook: who prompted it, else who started it; a team member by their person key, anyone
/// else by their Linear name.
pub fn requester(body: &Value, config: &Config) -> Option<String> {
    let prompted = (body["action"] == "prompted").then(|| &body["agentActivity"]["user"]).filter(|u| u.is_object());
    let user = prompted.unwrap_or(&body["agentSession"]["creator"]);
    match config.person_by_linear(user["email"].as_str().unwrap_or_default()) {
        Some(p) => Some(p.key.clone()),
        None => user["name"].as_str().map(str::to_string),
    }
}

/// The issue a session is on, for the KPIs: one row however many sessions it gets; the first hand-over is when it
/// started.
fn record_issue(c: &rusqlite::Connection, session: &Value, conversation_id: &str) -> Result<()> {
    let issue = &session["issue"];
    let (Some(id), Some(identifier)) = (issue["id"].as_str(), issue["identifier"].as_str()) else { return Ok(()) };
    let handed = session["createdAt"].as_str().and_then(|t| chrono::DateTime::parse_from_rfc3339(t).ok())
        .map(|t| t.with_timezone(&chrono::Utc).format("%Y-%m-%dT%H:%M:%SZ").to_string()).unwrap_or_else(utcnow);
    c.execute("INSERT INTO linear_issues(issue_id, identifier, title, url, handed_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(issue_id) \
               DO UPDATE SET identifier = excluded.identifier, title = excluded.title, url = excluded.url, \
               handed_at = MIN(handed_at, excluded.handed_at)", params![id, identifier, issue["title"].as_str(), issue["url"].as_str(), handed])?;
    c.execute("INSERT OR IGNORE INTO linear_issue_conversations(issue_id, conversation_id) VALUES (?, ?)", params![id, conversation_id])?;
    Ok(())
}

/// An agent-session webhook: a new session gets "On it." at once and its own conversation; a prompt in a known one is
/// its next turn. The event goes to Claude Code raw: reading the issue, picking the project and replying are the
/// agent's work.
pub async fn on_event(s: &Shared, body: &Value) -> Result<()> {
    let Some(sid) = body["agentSession"]["id"].as_str().filter(|_| body["type"] == "AgentSessionEvent") else { return Ok(()) };
    let key = format!("linear_session:{sid}");
    let mut convo_id = get_setting(&s.db.lock(), &key)?;
    if body["action"] == "created" && convo_id.is_none() {
        if let Some(linear) = &s.linear && let Err(e) = linear.agent_activity(s, sid, "thought", "On it.").await {
            tracing::error!("couldn't ack Linear session {sid}: {e:#}");
        }
        let c = s.db.lock();
        let id = cv::create_conversation(&c, None, None, None, None, true, None)?;
        set_setting(&c, &key, &id)?;
        convo_id = Some(id);
    } else if body["action"] != "prompted" || convo_id.is_none() {
        return Ok(()); // a redelivered `created`, or a session never seen
    }
    let convo_id = convo_id.unwrap_or_default();
    let convo = cv::row(&s.db.lock(), &convo_id)?.ok_or_else(|| anyhow!("its conversation is gone"))?;
    if let Err(e) = record_issue(&s.db.lock(), &body["agentSession"], &convo_id) {
        tracing::error!("couldn't record the issue of Linear session {sid}: {e:#}"); // for the KPIs, never at the turn's expense
    }
    let issue = &body["agentSession"]["issue"];
    let where_ = issue["identifier"].as_str().map(|i| format!("Linear {i}")).unwrap_or_else(|| "Linear".into());
    let text = format!("Linear agent session event:\n{}", serde_json::to_string_pretty(body)?);
    let mut payload = crate::payloads::turn(s, &convo, &text, None, true)?;
    payload["origin"] = json!(cv::origin_line(&where_, None, issue["url"].as_str()));
    payload["source"] = json!("linear");
    payload["requester"] = json!(requester(body, &s.config));
    payload["follows"] = Value::Null;
    crate::queue::enqueue(&s.db.lock(), Some(&convo_id), "turn", &payload)?;
    s.wake.notify_one();
    Ok(())
}

async fn labelled_issues(s: &Shared, linear: &Linear, team: &str) -> Result<Vec<Value>> {
    let data = linear.gql(s, "LabelledIssues", LABELLED_ISSUES, json!({"label": s.config.name, "team": team})).await?;
    Ok(data["issues"]["nodes"].as_array().cloned().unwrap_or_default())
}

/// Issues labelled with the agent's name become tasks, one per issue ever.
async fn poll(s: &Shared, linear: &Linear) -> Result<Vec<String>> {
    let mut created = Vec::new();
    for pack in s.packs.values() {
        let project = &pack.project;
        for issue in labelled_issues(s, linear, &project.linear_team).await? {
            let id = issue["id"].as_str().unwrap_or_default();
            let known = s.db.lock().query_row("SELECT 1 FROM tasks WHERE linear_issue_id = ? LIMIT 1", [id], |_| Ok(())).optional()?.is_some();
            if known {
                continue; // follow-ups go through the task's Slack thread
            }
            let person = s.config.person_by_linear(issue["creator"]["email"].as_str().unwrap_or_default());
            let bug = issue["labels"]["nodes"].as_array().into_iter().flatten()
                .any(|l| l["name"].as_str().is_some_and(|n| n.eq_ignore_ascii_case("bug")));
            let description = format!("{}\n\nLinear: {}", issue["description"].as_str().unwrap_or_default(),
                                      issue["url"].as_str().unwrap_or("None")).trim().to_string();
            let task = tasks::create(&s.db.lock(), NewTask {
                project: project.name.clone(), title: issue["title"].as_str().unwrap_or_default().into(), description,
                kind: if bug { "bug" } else { "feature" }.into(), source: "linear".into(),
                source_ref: issue["identifier"].as_str().map(str::to_string), requester: person.map(|p| p.key.clone()),
                owner: Some(person.map(|p| p.key.clone()).unwrap_or_else(|| project.owner.clone())),
                slack_channel: Some(project.slack_channel.clone()), slack_thread_ts: None, linear_issue_id: Some(id.into()) })?;
            created.push(task.id);
        }
    }
    Ok(created)
}

/// Every minute: new labelled issues, and tasks from an earlier poll whose Slack thread couldn't be made yet, each
/// get their thread and first turn. Returns the tasks started.
pub async fn intake(s: &Shared) -> Result<Vec<String>> {
    let Some(linear) = &s.linear else { return Ok(Vec::new()) };
    let created = poll(s, linear).await?;
    let mut ids: Vec<String> = s.db.lock().prepare("SELECT id FROM tasks WHERE source = 'linear' AND status = 'queued' AND id NOT IN \
                                                    (SELECT task_id FROM conversations WHERE task_id IS NOT NULL) ORDER BY id")?
        .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    for id in created {
        if !ids.contains(&id) {
            ids.push(id);
        }
    }
    let mut attached = Vec::new();
    for id in ids {
        match attach(s, &id).await {
            Ok(()) => attached.push(id),
            Err(e) => tracing::error!("couldn't start {id}; will retry on the next poll: {e:#}"),
        }
    }
    Ok(attached)
}

async fn attach(s: &Shared, task_id: &str) -> Result<()> {
    let task = tasks::get(&s.db.lock(), task_id)?.ok_or_else(|| anyhow!("no task {task_id}"))?;
    let pack = s.packs.get(&task.project).ok_or_else(|| anyhow!("no project {}", task.project))?;
    let reference = task.source_ref.clone().unwrap_or_default();
    let (channel, ts) = s.slack.post(&pack.project.slack_channel, None, &format!("Linear {reference}: {}", task.title)).await?;
    let (convo, task) = {
        let c = s.db.lock();
        let task = tasks::set_fields(&c, task_id, &[("slack_channel", Some(&channel)), ("slack_thread_ts", Some(&ts))])?;
        let id = cv::create_conversation(&c, Some(&channel), Some(&ts), Some(&task.project), Some(task_id), true, None)?;
        (cv::row(&c, &id)?.ok_or_else(|| anyhow!("conversation vanished"))?, tasks::transition(&c, task_id, Status::Working, "linear", None)?)
    };
    cv::enqueue_turn(s, &convo, &format!("Linear issue {reference}: {}\n\n{}", task.title, task.description), task.requester.as_deref(),
                     TurnFrom { unattended: true, source: "linear".into(), requester: task.requester.clone(),
                                origin: cv::origin_line(&format!("Linear {reference}"), None, issue_link(&task.description).as_deref()),
                                ..Default::default() })?;
    crate::notifier::task_changed(s, &task).await
}

const PAGE: &str = "<!doctype html><title>{title}</title><p>{text}</p>";

fn page(status: u16, title: &str, text: &str) -> (u16, String) {
    (status, PAGE.replace("{title}", title).replace("{text}", text))
}

/// The install callback: (HTTP status, page).
pub async fn callback(s: &Shared, code: Option<&str>, state: Option<&str>, error: Option<&str>) -> (u16, String) {
    let Some(app) = s.linear.as_ref().and_then(|l| l.app.as_ref()) else { return page(404, "Not found", "Not found.") };
    let valid = error.is_none() && code.is_some_and(|c| !c.is_empty()) && app.check_state(s, state.unwrap_or_default()).unwrap_or(false);
    if !valid {
        return page(400, "Not installed", "This install link is invalid or expired. Ask the agent for a new one.");
    }
    let user = match app.exchange(s, code.unwrap_or_default()).await {
        Ok(user) => user,
        Err(e) => {
            tracing::error!("the Linear OAuth exchange failed: {e:#}");
            return page(502, "Not installed", "Linear didn't accept the install. Ask the agent for a new link.");
        }
    };
    tracing::info!("the Linear app is installed (app user {user})");
    let dm = get_setting(&s.db.lock(), INSTALL_DM_KEY).ok().flatten();
    if let Some(dm) = dm {
        let _ = s.slack.post(&dm, None, "I'm installed in Linear: delegate issues to me there.").await;
    }
    page(200, "Installed", "The agent is installed in Linear. You can close this tab.")
}

/// The public listener: `POST /linear/webhook` (HMAC-SHA256 of the raw body in `Linear-Signature`, `webhookTimestamp`
/// within a minute, answered at once and handled after) and `GET /linear/oauth/callback`. Everything else is 404.
pub fn public_app(s: Arc<Shared>) -> axum::Router {
    use axum::extract::{Query, State};
    use axum::http::{HeaderMap, StatusCode};
    use axum::response::{Html, IntoResponse};
    async fn webhook(State(s): State<Arc<Shared>>, headers: HeaderMap, raw: axum::body::Bytes) -> axum::response::Response {
        let secret = s.linear.as_ref().map(|l| l.webhook_secret.clone()).unwrap_or_default();
        let signed = !secret.is_empty() && Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes()).ok().is_some_and(|mut mac| {
            mac.update(&raw);
            let given = headers.get("Linear-Signature").and_then(|v| v.to_str().ok()).unwrap_or_default();
            hex_bytes(given).is_some_and(|sig| mac.verify_slice(&sig).is_ok())
        });
        if !signed {
            return (StatusCode::UNAUTHORIZED, axum::Json(json!({"error": "bad signature"}))).into_response();
        }
        let body: Option<Value> = serde_json::from_slice(&raw).ok();
        let fresh = body.as_ref().and_then(|b| b["webhookTimestamp"].as_f64())
            .is_some_and(|t| (chrono::Utc::now().timestamp_millis() as f64 - t).abs() <= MAX_SKEW_MS);
        let (true, Some(body)) = (fresh, body) else {
            return (StatusCode::UNAUTHORIZED, axum::Json(json!({"error": "stale or malformed"}))).into_response();
        };
        tokio::spawn(async move {
            tracing::info!("Linear {}/{} event", body["type"], body["action"]);
            if let Err(e) = on_event(&s, &body).await {
                tracing::error!("handling a Linear {}/{} event failed: {e:#}", body["type"], body["action"]);
            }
        });
        axum::Json(json!({"ok": true})).into_response()
    }
    async fn oauth(State(s): State<Arc<Shared>>, Query(q): Query<HashMap<String, String>>) -> axum::response::Response {
        let (status, html) = callback(&s, q.get("code").map(String::as_str), q.get("state").map(String::as_str),
                                      q.get("error").map(String::as_str)).await;
        (StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST), Html(html)).into_response()
    }
    axum::Router::new().route("/linear/webhook", axum::routing::post(webhook))
        .route("/linear/oauth/callback", axum::routing::get(oauth)).with_state(s)
}

fn hex_bytes(text: &str) -> Option<Vec<u8>> {
    (text.len() % 2 == 0).then(|| (0..text.len()).step_by(2).map(|i| u8::from_str_radix(&text[i..i + 2], 16).ok()).collect())?
}
