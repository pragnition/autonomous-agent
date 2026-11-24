//! The Containers page's endpoints: everything that runs (`GET /api/workloads`), one container, service or app in
//! detail, its logs followed live (server-sent events), actions (stop, start, delete, kill, expiry), and a shell in a
//! container over a websocket, for admins only: whoever holds it has all of the agent's credentials.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path as UrlPath, Query, Request, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;

use super::{D, error, ok, viewer};
use crate::workloads::{ByteStream, Target, Workloads};

/// Lines sent at once, and how long the stream gathers them.
const BATCH_LINES: usize = 500;
const BATCH_WAIT: Duration = Duration::from_millis(250);
const PING: Duration = Duration::from_secs(15);
const MAX_LINE: usize = 8000;

fn need(d: &super::Dash) -> Result<Arc<Workloads>, Response> {
    d.workloads.clone().ok_or_else(|| error(StatusCode::SERVICE_UNAVAILABLE, "agentd can't reach Docker"))
}

fn bad(e: anyhow::Error) -> Response {
    error(StatusCode::BAD_REQUEST, &format!("{e:#}"))
}

pub(super) async fn overview(State(d): D) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    match w.overview(Utc::now()).await {
        Ok(v) => ok(v),
        Err(e) => super::internal(e),
    }
}

pub(super) async fn container(State(d): D, UrlPath(id): UrlPath<String>) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    if let Err(e) = Target::parse("container", &id) {
        return bad(e);
    }
    match w.container_detail(&id, Utc::now()).await {
        Ok(v) => ok(v),
        Err(e) => error(StatusCode::NOT_FOUND, &format!("{e:#}")),
    }
}

pub(super) async fn service(State(d): D, UrlPath(name): UrlPath<String>) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    if !crate::workloads::plain_name(&name) {
        return error(StatusCode::BAD_REQUEST, "not a name");
    }
    match w.service_detail(&name).await {
        Ok(v) => ok(v),
        Err(e) => error(StatusCode::NOT_FOUND, &format!("{e:#}")),
    }
}

pub(super) async fn app(State(d): D, UrlPath(name): UrlPath<String>) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    if !crate::workloads::plain_name(&name) {
        return error(StatusCode::BAD_REQUEST, "not a name");
    }
    match w.app_detail(&name).await {
        Ok(v) => ok(v),
        Err(e) => error(StatusCode::NOT_FOUND, &format!("{e:#}")),
    }
}

pub(super) async fn act(State(d): D, req: Request) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    let v = viewer(req.extensions());
    let body = match axum::body::to_bytes(req.into_body(), 64 * 1024).await {
        Ok(b) => b,
        Err(_) => return error(StatusCode::BAD_REQUEST, "body too large"),
    };
    let Ok(a) = serde_json::from_slice::<Value>(&body) else { return error(StatusCode::BAD_REQUEST, "expected JSON") };
    match w.act(&a, &v.actor(), Utc::now()).await {
        Ok(done) => ok(json!({"ok": true, "done": done})),
        Err(e) => error(StatusCode::CONFLICT, &format!("{e:#}")),
    }
}

/// Complete lines out of `partial`, scrubbed, cut when very long, with terminal colours and carriage returns left
/// for the page.
fn take_lines(partial: &mut Vec<u8>, d: &super::Dash, out: &mut Vec<String>, flush: bool) {
    while let Some(end) = partial.iter().position(|&b| b == b'\n') {
        let line: Vec<u8> = partial.drain(..=end).collect();
        out.push(clean(&line[..line.len() - 1], d));
    }
    if flush && !partial.is_empty() {
        out.push(clean(partial, d));
        partial.clear();
    }
}

fn clean(line: &[u8], d: &super::Dash) -> String {
    let mut text = String::from_utf8_lossy(line).trim_end_matches('\r').to_string();
    if text.len() > MAX_LINE {
        let mut cut = MAX_LINE;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
        text.push_str(" …");
    }
    d.s.scrub(&text)
}

pub(super) async fn logs(State(d): D, Query(q): Query<HashMap<String, String>>) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    let get = |k: &str| q.get(k).map(String::as_str).unwrap_or_default();
    let target = match Target::parse(get("kind"), get("target")) { Ok(t) => t, Err(e) => return bad(e) };
    let pid = q.get("pid").and_then(|p| p.parse().ok());
    let inner = match w.follow(get("source"), &target, pid, q.get("service").map(String::as_str)).await {
        Ok(s) => s,
        Err(e) => return error(StatusCode::NOT_FOUND, &format!("{e:#}")),
    };
    struct Follow {
        d: Arc<super::Dash>,
        inner: ByteStream,
        partial: Vec<u8>,
        pinged: Instant,
        done: bool,
    }
    let follow = Follow { d: d.clone(), inner, partial: Vec::new(), pinged: Instant::now(), done: false };
    let events = futures_util::stream::unfold(follow, |mut f| async move {
        if f.done {
            return None;
        }
        loop {
            let mut lines = Vec::new();
            let deadline = tokio::time::Instant::now() + BATCH_WAIT;
            let mut ended = None;
            while lines.len() < BATCH_LINES {
                match tokio::time::timeout_at(deadline, f.inner.next()).await {
                    Ok(Some(Ok(chunk))) => {
                        f.partial.extend_from_slice(&chunk);
                        take_lines(&mut f.partial, &f.d, &mut lines, false);
                    }
                    Ok(Some(Err(e))) => {
                        ended = Some(format!("{e:#}"));
                        break;
                    }
                    Ok(None) => {
                        ended = Some(String::new());
                        break;
                    }
                    Err(_) => break,
                }
            }
            if let Some(why) = ended {
                take_lines(&mut f.partial, &f.d, &mut lines, true);
                f.done = true;
                let mut event = String::new();
                if !lines.is_empty() {
                    event.push_str(&format!("event: lines\ndata: {}\n\n", json!({"lines": lines})));
                }
                event.push_str(&format!("event: end\ndata: {}\n\n", json!({"error": (!why.is_empty()).then_some(why)})));
                return Some((Ok::<Bytes, std::io::Error>(Bytes::from(event)), f));
            }
            if !lines.is_empty() {
                let event = format!("event: lines\ndata: {}\n\n", json!({"lines": lines}));
                return Some((Ok(Bytes::from(event)), f));
            }
            if f.pinged.elapsed() >= PING {
                f.pinged = Instant::now();
                return Some((Ok(Bytes::from_static(b": ping\n\n")), f));
            }
        }
    });
    ([(header::CONTENT_TYPE, "text/event-stream"), (header::CACHE_CONTROL, "no-cache"),
      (header::HeaderName::from_static("x-accel-buffering"), "no")], Body::from_stream(events)).into_response()
}

/// A shell in a container, for admins: the page's terminal sends `{"i": text}` for input and `{"r": [cols, rows]}`
/// when it resizes; the shell's output comes back as binary frames.
pub(super) async fn shell(State(d): D, headers: HeaderMap, Query(q): Query<HashMap<String, String>>, ws: WebSocketUpgrade,
                   req: Request) -> Response {
    let w = match need(&d) { Ok(w) => w, Err(r) => return r };
    let v = viewer(req.extensions());
    if !v.can_resume() {
        return error(StatusCode::FORBIDDEN, "only an admin can open a shell");
    }
    // a websocket isn't held to the same origin as a fetch: only the dashboard's own page may open one
    let text = |k: header::HeaderName| headers.get(k).and_then(|h| h.to_str().ok()).unwrap_or_default().to_string();
    let origin = text(header::ORIGIN);
    let host = headers.get("x-forwarded-host").and_then(|h| h.to_str().ok()).map(str::to_string).unwrap_or_else(|| text(header::HOST));
    let netloc = origin.split_once("://").map_or("", |(_, rest)| rest).split('/').next().unwrap_or_default().to_string();
    if origin.is_empty() || netloc != host {
        return error(StatusCode::FORBIDDEN, "cross-site shell");
    }
    let get = |k: &str| q.get(k).map(String::as_str).unwrap_or_default();
    let target = match Target::parse(get("kind"), get("target")) { Ok(t) => t, Err(e) => return bad(e) };
    let actor = v.actor();
    let event = json!({"kind": get("kind"), "target": get("target"), "action": "shell"});
    if let Err(e) = crate::db::append_event(&d.s.db.lock(), "workload_action", None, Some(&actor), Some(&event)) {
        return super::internal(e);
    }
    ws.on_upgrade(move |socket| relay(socket, w, target))
}

async fn relay(socket: WebSocket, w: Arc<Workloads>, target: Target) {
    let (mut tx, mut rx) = socket.split();
    let (id, mut output, mut input) = match w.shell(&target).await {
        Ok(s) => s,
        Err(e) => {
            let _ = tx.send(Message::Binary(Bytes::from(format!("\r\ncan't open a shell: {e:#}\r\n")))).await;
            let _ = tx.send(Message::Close(None)).await;
            return;
        }
    };
    let out = async {
        while let Some(Ok(chunk)) = output.next().await {
            if tx.send(Message::Binary(chunk)).await.is_err() {
                return;
            }
        }
        let _ = tx.send(Message::Binary(Bytes::from_static(b"\r\n[the shell ended]\r\n"))).await;
        let _ = tx.send(Message::Close(None)).await;
    };
    let inp = async {
        while let Some(Ok(m)) = rx.next().await {
            match m {
                Message::Text(t) => {
                    let Ok(v) = serde_json::from_str::<Value>(t.as_str()) else { continue };
                    if let Some(i) = v["i"].as_str() {
                        if input.write_all(i.as_bytes()).await.is_err() {
                            return;
                        }
                        let _ = input.flush().await;
                    } else if let (Some(c), Some(r)) = (v["r"][0].as_u64(), v["r"][1].as_u64()) {
                        let _ = w.resize(&id, c.clamp(10, 1000) as u16, r.clamp(2, 500) as u16).await;
                    }
                }
                Message::Binary(b) => {
                    if input.write_all(&b).await.is_err() {
                        return;
                    }
                }
                Message::Close(_) => return,
                _ => {}
            }
        }
    };
    tokio::select! {
        _ = out => {}
        _ = inp => {}
    }
    // the shell's input closes with this: it ends on its own
    let _ = input.shutdown().await;
}
