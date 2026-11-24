//! Slack: the Web API calls core made, posting the agent's Markdown, files both ways, and Socket Mode (a port of
//! slack_sdk's use in core, slack_markdown.py and connectors/slack_app.py).

use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio_tungstenite::tungstenite::Message;

pub const API: &str = "https://slack.com/api";
/// Slack's cap on all markdown blocks in one message.
const MARKDOWN_LIMIT: usize = 12_000;
const FENCE: &str = "```";

/// Slack said no: `error` is its code (`invalid_blocks`, `already_reacted`, `not_in_channel` ...).
#[derive(Debug)]
pub struct SlackError {
    pub method: String,
    pub error: String,
}

impl std::fmt::Display for SlackError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Slack {}: {}", self.method, self.error)
    }
}

impl std::error::Error for SlackError {}

/// Slack's error code, when `e` is one.
pub fn error_code(e: &anyhow::Error) -> Option<&str> {
    e.downcast_ref::<SlackError>().map(|s| s.error.as_str())
}

#[derive(Clone)]
pub struct Slack {
    http: reqwest::Client,
    token: String,
    api: String,
}

impl Slack {
    pub fn new(token: &str, api: &str) -> Slack {
        let http = reqwest::Client::builder().timeout(Duration::from_secs(60)).build().expect("an HTTP client");
        Slack { http, token: token.to_string(), api: api.trim_end_matches('/').to_string() }
    }

    /// One call; a rate limit is waited out once (Slack's Retry-After, at most 30 s).
    async fn send(&self, method: &str, req: reqwest::RequestBuilder) -> Result<Value> {
        let mut req = req.bearer_auth(&self.token);
        for attempt in 0..2 {
            let retry = req.try_clone();
            let resp = req.send().await.with_context(|| format!("Slack {method}"))?;
            if resp.status() == reqwest::StatusCode::TOO_MANY_REQUESTS && attempt == 0 && let Some(retry) = retry {
                let wait = resp.headers().get("retry-after").and_then(|v| v.to_str().ok()?.parse().ok()).unwrap_or(1u64);
                tokio::time::sleep(Duration::from_secs(wait.min(30))).await;
                req = retry;
                continue;
            }
            let body: Value = resp.json().await.with_context(|| format!("Slack {method}"))?;
            if body["ok"] != true {
                return Err(SlackError { method: method.into(), error: body["error"].as_str().unwrap_or("unknown").into() }.into());
            }
            return Ok(body);
        }
        bail!("Slack {method}: rate limited")
    }

    async fn call(&self, method: &str, body: Value) -> Result<Value> {
        self.send(method, self.http.post(format!("{}/{method}", self.api)).json(&body)).await
    }

    async fn form(&self, method: &str, form: &[(&str, String)]) -> Result<Value> {
        self.send(method, self.http.post(format!("{}/{method}", self.api)).form(form)).await
    }

    /// A read method, its params as a query string.
    pub async fn api_get(&self, method: &str, params: &[(String, String)]) -> Result<Value> {
        self.send(method, self.http.get(format!("{}/{method}", self.api)).query(params)).await
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    /// The bot's own user id.
    pub async fn auth_test(&self) -> Result<String> {
        Ok(self.call("auth.test", json!({})).await?["user_id"].as_str().unwrap_or_default().to_string())
    }

    /// Post plain text; returns (channel, ts).
    pub async fn post(&self, channel: &str, thread_ts: Option<&str>, text: &str) -> Result<(String, String)> {
        let body = self.call("chat.postMessage", json!({"channel": channel, "thread_ts": thread_ts, "text": text})).await?;
        Ok((body["channel"].as_str().unwrap_or(channel).to_string(), body["ts"].as_str().unwrap_or_default().to_string()))
    }

    /// Post text the agent wrote, standard Markdown, as one or more messages with a markdown block (`text` stays as
    /// the notification fallback); a block Slack rejects is posted as plain text. Returns each message's ts.
    pub async fn post_markdown(&self, channel: &str, thread_ts: Option<&str>, text: &str) -> Result<Vec<String>> {
        let mut posted = Vec::new();
        for chunk in markdown_chunks(text, MARKDOWN_LIMIT) {
            let body = json!({"channel": channel, "thread_ts": thread_ts, "text": chunk,
                              "blocks": [{"type": "markdown", "text": chunk}]});
            let resp = match self.call("chat.postMessage", body).await {
                Err(e) if matches!(error_code(&e), Some("invalid_blocks" | "invalid_blocks_format")) => {
                    tracing::warn!("Slack rejected the markdown block; posting plain text");
                    self.call("chat.postMessage", json!({"channel": channel, "thread_ts": thread_ts, "text": chunk})).await?
                }
                other => other?,
            };
            posted.push(resp["ts"].as_str().unwrap_or_default().to_string());
        }
        Ok(posted)
    }

    pub async fn update(&self, channel: &str, ts: &str, text: &str) -> Result<()> {
        self.call("chat.update", json!({"channel": channel, "ts": ts, "text": text})).await.map(drop)
    }

    pub async fn ephemeral(&self, channel: &str, user: &str, text: &str) -> Result<()> {
        self.call("chat.postEphemeral", json!({"channel": channel, "user": user, "text": text})).await.map(drop)
    }

    pub async fn permalink(&self, channel: &str, ts: &str) -> Result<String> {
        let body = self.api_get("chat.getPermalink", &[("channel".into(), channel.into()), ("message_ts".into(), ts.into())]).await?;
        Ok(body["permalink"].as_str().unwrap_or_default().to_string())
    }

    pub async fn react(&self, channel: &str, ts: &str, name: &str) -> Result<()> {
        self.call("reactions.add", json!({"channel": channel, "timestamp": ts, "name": name})).await.map(drop)
    }

    pub async fn unreact(&self, channel: &str, ts: &str, name: &str) -> Result<()> {
        self.call("reactions.remove", json!({"channel": channel, "timestamp": ts, "name": name})).await.map(drop)
    }

    /// The DM channel with a person.
    pub async fn open_dm(&self, user: &str) -> Result<String> {
        Ok(self.call("conversations.open", json!({"users": user})).await?["channel"]["id"].as_str().unwrap_or_default().to_string())
    }

    pub async fn channel_name(&self, channel: &str) -> Result<String> {
        let body = self.api_get("conversations.info", &[("channel".into(), channel.into())]).await?;
        Ok(body["channel"]["name"].as_str().unwrap_or_default().to_string())
    }

    /// A person's name as Slack has it, if it has one.
    pub async fn user_name(&self, user: &str) -> Result<Option<String>> {
        let body = self.api_get("users.info", &[("user".into(), user.into())]).await?;
        let user = &body["user"];
        Ok([user["real_name"].as_str(), user["profile"]["real_name"].as_str()].into_iter().flatten()
            .find(|n| !n.is_empty()).map(str::to_string))
    }

    pub async fn replies(&self, channel: &str, ts: &str, limit: u32) -> Result<Vec<Value>> {
        let body = self.api_get("conversations.replies", &[("channel".into(), channel.into()), ("ts".into(), ts.into()),
                                                         ("limit".into(), limit.to_string())]).await?;
        Ok(body["messages"].as_array().cloned().unwrap_or_default())
    }

    /// A channel's messages from `oldest` on, newest first as Slack gives them.
    pub async fn history(&self, channel: &str, oldest: &str, limit: u32) -> Result<Vec<Value>> {
        let body = self.api_get("conversations.history", &[("channel".into(), channel.into()), ("oldest".into(), oldest.into()),
                                                          ("inclusive".into(), "1".into()), ("limit".into(), limit.to_string())]).await?;
        Ok(body["messages"].as_array().cloned().unwrap_or_default())
    }

    /// Files into a thread, through Slack's external upload: a URL per file, the bytes, then one completing call.
    pub async fn upload_files(&self, channel: &str, thread_ts: Option<&str>, comment: &str,
                              files: Vec<(String, Vec<u8>)>) -> Result<()> {
        let mut done = Vec::new();
        for (name, data) in files {
            let slot = self.form("files.getUploadURLExternal", &[("filename", name.clone()), ("length", data.len().to_string())]).await?;
            let url = slot["upload_url"].as_str().ok_or_else(|| anyhow!("Slack gave no upload URL for {name}"))?;
            self.http.post(url).body(data).send().await?.error_for_status().with_context(|| format!("uploading {name}"))?;
            done.push(json!({"id": slot["file_id"], "title": name}));
        }
        let mut form = vec![("files", Value::Array(done).to_string()), ("channel_id", channel.to_string())];
        if let Some(ts) = thread_ts {
            form.push(("thread_ts", ts.to_string()));
        }
        if !comment.is_empty() {
            form.push(("initial_comment", comment.to_string()));
        }
        self.form("files.completeUploadExternal", &form).await.map(drop)
    }

    /// A file's bytes, downloaded with the bot token (the files:read scope). Slack answers a download it won't
    /// authorise with its HTML login page, so that counts as a failure.
    pub async fn download_file(&self, file_id: &str) -> Result<Vec<u8>> {
        let info = self.api_get("files.info", &[("file".into(), file_id.into())]).await?;
        let file = &info["file"];
        let url = file["url_private_download"].as_str().or(file["url_private"].as_str())
            .ok_or_else(|| anyhow!("Slack gave no download link for {file_id}"))?;
        let resp = self.http.get(url).bearer_auth(&self.token).send().await?.error_for_status()?;
        let html = resp.headers().get("content-type").and_then(|v| v.to_str().ok()).is_some_and(|t| t.starts_with("text/html"));
        if html && !file["mimetype"].as_str().unwrap_or_default().starts_with("text/html") {
            bail!("Slack refused the download (is the files:read scope granted?)");
        }
        Ok(resp.bytes().await?.to_vec())
    }
}

/// Python's `str.splitlines(keepends=True)`.
fn lines_with_ends(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let (mut start, mut chars) = (0, text.char_indices().peekable());
    while let Some((i, c)) = chars.next() {
        if matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            let mut end = i + c.len_utf8();
            if c == '\r' && chars.next_if(|&(_, n)| n == '\n').is_some() {
                end += 1;
            }
            out.push(&text[start..end]);
            start = end;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// `text` in pieces of at most `limit` characters, split on line breaks. A cut inside a code fence closes the fence
/// and reopens it in the next piece; a line longer than a piece is cut where it must.
pub fn markdown_chunks(text: &str, limit: usize) -> Vec<String> {
    let room = limit - 2 * (FENCE.len() + 1);
    let mut pieces = Vec::new();
    for line in lines_with_ends(text) {
        let chars: Vec<char> = line.chars().collect();
        pieces.extend(chars.chunks(room).map(|c| c.iter().collect::<String>()));
    }
    let (mut chunks, mut current, mut in_fence) = (Vec::new(), String::new(), false);
    for piece in pieces {
        if !current.is_empty() && current.chars().count() + piece.chars().count() > room {
            chunks.push(if in_fence { format!("{}\n{FENCE}", current.trim_end_matches('\n')) } else { current.clone() });
            current = if in_fence { format!("{FENCE}\n") } else { String::new() };
        }
        current += &piece;
        if piece.trim_start().starts_with(FENCE) {
            in_fence = !in_fence;
        }
    }
    if !current.is_empty() {
        chunks.push(current);
    }
    chunks
}

/// Socket Mode, outbound only: each event (a message or a mention) goes to `on_event`, after its envelope is
/// acknowledged, so Slack doesn't retry it. A dropped or refreshed connection is opened again; this never returns.
pub async fn socket_mode(app_token: &str, api: &str, on_event: impl Fn(Value) + Send + Sync + 'static) {
    let opener = Slack::new(app_token, api);
    let mut backoff = 1;
    loop {
        match opener.call("apps.connections.open", json!({})).await
            .and_then(|b| b["url"].as_str().map(str::to_string).ok_or_else(|| anyhow!("no Socket Mode URL"))) {
            Ok(url) => match tokio_tungstenite::connect_async(url.as_str()).await {
                Ok((mut ws, _)) => {
                    backoff = 1;
                    while let Some(Ok(msg)) = ws.next().await {
                        let Message::Text(text) = msg else {
                            if matches!(msg, Message::Close(_)) { break } else { continue }
                        };
                        let Ok(frame) = serde_json::from_str::<Value>(text.as_str()) else { continue };
                        if let Some(id) = frame.get("envelope_id") {
                            let _ = ws.send(Message::text(json!({"envelope_id": id}).to_string())).await;
                        }
                        match frame["type"].as_str() {
                            Some("events_api") => on_event(frame["payload"]["event"].clone()),
                            Some("disconnect") => break,
                            _ => {}
                        }
                    }
                    tracing::info!("Socket Mode connection closed; opening another");
                }
                Err(e) => tracing::warn!("Socket Mode connect failed: {e}"),
            },
            Err(e) => tracing::warn!("Socket Mode open failed: {e:#}"),
        }
        tokio::time::sleep(Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    use axum::extract::State;
    use axum::routing::post;
    use futures_util::{SinkExt, StreamExt};
    use serde_json::{Value, json};

    #[test]
    fn markdown_chunks_match_python() {
        let expected: Value = serde_json::from_str(&std::fs::read_to_string("tests/fixtures/intake/expected.json").unwrap()).unwrap();
        let text = "intro line\n".to_string() + &(0..12).map(|i| format!("line {i} {}", "x".repeat(30))).collect::<Vec<_>>().join("\n")
            + "\n```\n" + &(0..10).map(|i| format!("code {i} {}", "y".repeat(30))).collect::<Vec<_>>().join("\n")
            + "\n```\nafter\n" + &"z".repeat(300) + "\nend";
        for case in expected["markdown_chunks"].as_array().unwrap() {
            let limit = case["limit"].as_u64().unwrap() as usize;
            let want: Vec<String> = serde_json::from_value(case["out"].clone()).unwrap();
            assert_eq!(markdown_chunks(&text, limit), want, "limit {limit}");
        }
    }

    /// A Slack Web API that records each call (method, body) and answers from `answer`.
    async fn fake(answer: fn(&str, &Value) -> Value) -> (String, Arc<Mutex<Vec<(String, Value)>>>) {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let state = (calls.clone(), answer);
        type S = (Arc<Mutex<Vec<(String, Value)>>>, fn(&str, &Value) -> Value);
        async fn handle(State((calls, answer)): State<S>, axum::extract::Path(method): axum::extract::Path<String>,
                        body: axum::body::Bytes) -> axum::Json<Value> {
            let body = serde_json::from_slice(&body).unwrap_or_else(|_| {
                let form: std::collections::HashMap<String, String> = serde_urlencoded::from_bytes(&body).unwrap_or_default();
                json!(form)
            });
            calls.lock().unwrap().push((method.clone(), body.clone()));
            axum::Json(answer(&method, &body))
        }
        let app = axum::Router::new().route("/api/{method}", post(handle).get(handle))
            .route("/upload/{id}", post(|State((calls, _)): State<S>, body: axum::body::Bytes| async move {
                calls.lock().unwrap().push(("upload".into(), json!(body.len())));
                "OK"
            }))
            .with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (base, calls)
    }

    #[tokio::test]
    async fn markdown_block_rejected_falls_back_to_text() {
        let (base, calls) = fake(|_, body| if body.get("blocks").is_some() { json!({"ok": false, "error": "invalid_blocks"}) }
                                           else { json!({"ok": true, "channel": "C1", "ts": "1.2"}) }).await;
        let slack = Slack::new("xoxb-test", &format!("{base}/api"));
        assert_eq!(slack.post_markdown("C1", Some("1.0"), "**hi**").await.unwrap(), ["1.2"]);
        let calls = calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].1["blocks"][0], json!({"type": "markdown", "text": "**hi**"}));
        assert_eq!((calls[1].1["thread_ts"].as_str(), calls[1].1.get("blocks")), (Some("1.0"), None));
    }

    #[tokio::test]
    async fn upload_goes_through_external_upload() {
        static BASE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
        let (base, calls) = fake(|method, _| match method {
            "files.getUploadURLExternal" =>
                json!({"ok": true, "upload_url": format!("{}/upload/F1", BASE.get().unwrap()), "file_id": "F1"}),
            _ => json!({"ok": true}),
        }).await;
        BASE.set(base.clone()).unwrap();
        let slack = Slack::new("xoxb-test", &format!("{base}/api"));
        slack.upload_files("C1", Some("1.0"), "here", vec![("a.png".into(), vec![1, 2, 3])]).await.unwrap();
        let calls = calls.lock().unwrap();
        let methods: Vec<&str> = calls.iter().map(|(m, _)| m.as_str()).collect();
        assert_eq!(methods, ["files.getUploadURLExternal", "upload", "files.completeUploadExternal"]);
        assert_eq!((calls[0].1["filename"].as_str(), calls[0].1["length"].as_str()), (Some("a.png"), Some("3")));
        let done = &calls[2].1;
        assert_eq!((done["channel_id"].as_str(), done["thread_ts"].as_str(), done["initial_comment"].as_str()),
                   (Some("C1"), Some("1.0"), Some("here")));
        assert_eq!(serde_json::from_str::<Value>(done["files"].as_str().unwrap()).unwrap(), json!([{"id": "F1", "title": "a.png"}]));
    }

    #[tokio::test]
    async fn socket_mode_acks_and_reconnects() {
        // the WebSocket end: each connection says hello, sends one event and waits for its ack, then says disconnect
        let ws = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let ws_url = format!("ws://{}/link", ws.local_addr().unwrap());
        let acks = Arc::new(Mutex::new(Vec::new()));
        let seen = acks.clone();
        tokio::spawn(async move {
            for n in 0..2 {
                let (tcp, _) = ws.accept().await.unwrap();
                let mut sock = tokio_tungstenite::accept_async(tcp).await.unwrap();
                sock.send(json!({"type": "hello"}).to_string().into()).await.unwrap();
                let envelope = json!({"envelope_id": format!("E{n}"), "type": "events_api",
                                      "payload": {"event": {"type": "message", "text": format!("m{n}")}}});
                sock.send(envelope.to_string().into()).await.unwrap();
                if let Some(Ok(msg)) = sock.next().await {
                    seen.lock().unwrap().push(serde_json::from_str::<Value>(msg.to_text().unwrap()).unwrap()["envelope_id"].clone());
                }
                sock.send(json!({"type": "disconnect", "reason": "refresh_requested"}).to_string().into()).await.unwrap();
                let _ = sock.close(None).await;
            }
        });
        static URL: std::sync::OnceLock<String> = std::sync::OnceLock::new();
        URL.set(ws_url).unwrap();
        let (base, calls) = fake(|_, _| json!({"ok": true, "url": URL.get().unwrap()})).await;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let api = format!("{base}/api");
        tokio::spawn(async move {
            socket_mode("xapp-test", &api, move |event| { let _ = tx.send(event); }).await;
        });
        let first = rx.recv().await.unwrap();
        let second = tokio::time::timeout(std::time::Duration::from_secs(10), rx.recv()).await.unwrap().unwrap();
        assert_eq!((first["text"].as_str(), second["text"].as_str()), (Some("m0"), Some("m1")));
        for _ in 0..100 {
            if acks.lock().unwrap().len() == 2 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert_eq!(*acks.lock().unwrap(), [json!("E0"), json!("E1")], "each envelope acked");
        assert_eq!(calls.lock().unwrap().iter().filter(|(m, _)| m == "apps.connections.open").count(), 2);
    }
}
