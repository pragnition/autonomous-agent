//! `agent-holder hook idle-wait` and `agent-holder mcp`.

mod common;

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use common::*;
use holder_proto::{Entry, Request, Response, ToolSpec};
use serde_json::{Value, json};

fn hook(event: Value) -> String {
    let mut child = Command::new(env!("CARGO_BIN_EXE_agent-holder"))
        .args(["hook", "idle-wait"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(event.to_string().as_bytes()).unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success());
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn hook_denies_waiting_bash_and_allows_others() {
    let out = hook(json!({"tool_name": "Bash", "tool_input": {"command": "timeout 290 tail -f /dev/null"}}));
    let decision: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(decision["hookSpecificOutput"]["hookEventName"], "PreToolUse");
    assert_eq!(decision["hookSpecificOutput"]["permissionDecision"], "deny");
    assert_eq!(decision["hookSpecificOutput"]["permissionDecisionReason"], agent_holder::idle_wait::IDLE_WAIT_REASON);
    assert_eq!(hook(json!({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})).trim(), "");
    assert_eq!(hook(json!({"tool_name": "Bash", "tool_input": {"command": "sleep 300", "run_in_background": true}}))
                   .trim(), "");
    assert_eq!(hook(json!({"tool_name": "Read", "tool_input": {"file_path": "/x"}})).trim(), "");
}

#[test]
fn mcp_lists_tools_and_relays_call() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"wait_stdin": 100})]);
    let tool = ToolSpec { name: "post_update".into(), description: "Post a progress update.".into(),
                          input_schema: json!({"type": "object", "properties": {"text": {"type": "string"}}}) };
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![tool]));
    let mut mcp = Command::new(env!("CARGO_BIN_EXE_agent-holder"))
        .args(["mcp", "--conversation", CONVO, "--root"])
        .arg(dir.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = mcp.stdin.take().unwrap();
    let mut stdout = BufReader::new(mcp.stdout.take().unwrap());
    let mut call = |msg: Value| -> Value {
        writeln!(stdin, "{msg}").unwrap();
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap()
    };
    let init = call(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}}));
    assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(init["result"]["capabilities"]["tools"], json!({}));
    let listed = call(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}));
    assert_eq!(listed["result"]["tools"][0]["name"], "post_update");
    assert_eq!(listed["result"]["tools"][0]["inputSchema"]["properties"]["text"]["type"], "string");
    let root = dir.path().to_path_buf();
    let answer = std::thread::spawn(move || {
        wait_for(|| std::fs::read_to_string(root.join(CONVO).join("W-1.jsonl")).unwrap_or_default().contains("\"tool\""));
        let log = std::fs::read_to_string(root.join(CONVO).join("W-1.jsonl")).unwrap();
        let id = log.lines().filter_map(|l| serde_json::from_str::<holder_proto::LogLine>(l).ok())
            .find_map(|l| match l.entry { Entry::Tool { id, .. } => Some(id), _ => None }).unwrap();
        let h2 = Holder { child: Command::new("true").spawn().unwrap(), root };
        assert_eq!(h2.request(&Request::ToolResult { id, text: "posted".into() }), Response::Ok);
    });
    let result = call(json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "post_update", "arguments": {"text": "hi"}}}));
    answer.join().unwrap();
    assert_eq!(result["id"], 3);
    assert_eq!(result["result"]["content"], json!([{"type": "text", "text": "posted"}]));
    let _ = mcp.kill();
    h.request(&Request::Stop { grace_s: 1 });
    h.wait_exit("W-1");
}
