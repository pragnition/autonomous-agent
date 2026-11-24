//! `agent-holder mcp`: the stdio MCP server Claude Code starts for `agentd`'s tools. It lists the running turn's tools
//! and relays each call through the holder, which logs it for `agentd` and waits for the answer.

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use holder_proto::{Request, Response};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::Mutex;

use crate::client;

pub async fn run(socket: PathBuf) -> Result<()> {
    let out = Arc::new(Mutex::new(tokio::io::stdout()));
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = lines.next_line().await? {
        let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
        let Some(id) = msg.get("id").cloned() else { continue }; // a notification: nothing to answer
        let (socket, out) = (socket.clone(), out.clone());
        tokio::spawn(async move {
            let reply = match answer(&socket, &msg).await {
                Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
                Err((code, message)) => json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}),
            };
            let mut bytes = reply.to_string().into_bytes();
            bytes.push(b'\n');
            let mut out = out.lock().await;
            let _ = out.write_all(&bytes).await;
            let _ = out.flush().await;
        });
    }
    Ok(())
}

async fn answer(socket: &std::path::Path, msg: &Value) -> Result<Value, (i64, String)> {
    let params = msg.get("params").cloned().unwrap_or(json!({}));
    match msg.get("method").and_then(Value::as_str).unwrap_or("") {
        "initialize" => Ok(json!({
            "protocolVersion": params.get("protocolVersion").cloned().unwrap_or(json!("2025-06-18")),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent", "version": env!("CARGO_PKG_VERSION")},
        })),
        "ping" => Ok(json!({})),
        "tools/list" => match client::request(socket, &Request::Tools).await {
            Ok(Response::Tools { tools }) => Ok(json!({"tools": tools.into_iter().map(|t| json!({
                "name": t.name, "description": t.description, "inputSchema": t.input_schema,
            })).collect::<Vec<_>>()})),
            other => Err((-32603, format!("couldn't list the tools: {other:?}"))),
        },
        "tools/call" => {
            let name = params.get("name").and_then(Value::as_str).unwrap_or("").to_string();
            let args = params.get("arguments").cloned().unwrap_or(json!({}));
            match client::request(socket, &Request::Tool { name, args }).await {
                Ok(Response::ToolText { text }) => Ok(json!({"content": [{"type": "text", "text": text}]})),
                Ok(Response::Error { message }) => {
                    Ok(json!({"content": [{"type": "text", "text": message}], "isError": true}))
                }
                other => Ok(json!({"content": [{"type": "text", "text": format!("{other:?}")}], "isError": true})),
            }
        }
        method => Err((-32601, format!("method not found: {method}"))),
    }
}
