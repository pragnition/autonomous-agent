//! What `agentd` and `agent-holder` say to each other: requests on the holder's socket, and the numbered log the
//! holder writes for each turn on the shared volume.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Where every conversation's run directory lives, on the volume both `agentd` and the containers mount.
pub const RUN_ROOT: &str = "/var/agent/run/containers";

/// A tool Claude Code may call through the holder's MCP relay.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
}

/// One request on the holder's socket, one JSON object per line.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    /// Start Claude Code for a turn and write `stdin` to it.
    Start {
        work_item: String,
        argv: Vec<String>,
        env: BTreeMap<String, String>,
        cwd: String,
        stdin: Vec<Value>,
        tools: Vec<ToolSpec>,
    },
    /// Write one line to Claude Code's input (logged as `Sent` first, with `tag`: the work item a message carries).
    Send {
        line: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tag: Option<String>,
    },
    /// End the running Claude Code: its process group gets SIGTERM, then SIGKILL after `grace_s`.
    Stop { grace_s: u64 },
    /// Close Claude Code's input so it finishes and exits, as the Agent SDK ends a session: SIGTERM after `grace_s`
    /// if it is still running, then SIGKILL after `grace_s` more.
    Close { grace_s: u64 },
    Status,
    /// `agentd`'s answer to a `Tool` entry in the log.
    ToolResult { id: u64, text: String },
    /// The tools of the running turn, for the MCP relay.
    Tools,
    /// A tool call from the MCP relay; answered with `ToolText` once `agentd` sends `ToolResult`.
    Tool { name: String, args: Value },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Response {
    Ok,
    Started { pid: u32 },
    Status(Status),
    Tools { tools: Vec<ToolSpec> },
    ToolText { text: String },
    Error { message: String },
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Status {
    pub work_item: Option<String>,
    pub pid: Option<u32>,
    pub alive: bool,
    /// TCP ports a process in this container is listening on.
    pub listeners: Vec<u16>,
    /// Processes in this container besides the holder and the running Claude Code.
    pub other_processes: u32,
    /// The last sequence number written to the current turn's log.
    pub log_seq: u64,
}

/// One entry of a turn's log.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Entry {
    Started { pid: u32 },
    /// A line Claude Code printed, as it printed it.
    Out { line: String },
    /// A line written to Claude Code's input.
    Sent {
        line: Value,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tag: Option<String>,
    },
    /// A tool call from the MCP relay, waiting for `agentd`.
    Tool { id: u64, name: String, args: Value },
    Exit { code: Option<i32>, signal: Option<i32> },
}

/// `{"seq": N, "kind": ..., ...}`: one line of a turn's log.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LogLine {
    pub seq: u64,
    #[serde(flatten)]
    pub entry: Entry,
}

#[derive(Debug, PartialEq, Eq)]
pub struct BadName(pub String);

impl std::fmt::Display for BadName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?} can't be used as a file name", self.0)
    }
}

impl std::error::Error for BadName {}

/// A conversation or work item id as one path component. Ids come from the database, but are never trusted as paths.
pub fn component(name: &str) -> Result<&str, BadName> {
    if name.is_empty() || name == "." || name == ".." || name.contains(['/', '\\', '\0']) {
        return Err(BadName(name.to_string()));
    }
    Ok(name)
}

pub fn run_dir(conversation: &str) -> Result<PathBuf, BadName> {
    Ok(PathBuf::from(RUN_ROOT).join(component(conversation)?))
}

pub fn log_path(conversation: &str, work_item: &str) -> Result<PathBuf, BadName> {
    Ok(run_dir(conversation)?.join(format!("{}.jsonl", component(work_item)?)))
}

pub fn socket_path(conversation: &str) -> Result<PathBuf, BadName> {
    Ok(run_dir(conversation)?.join("holder.sock"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn request_round_trips_as_tagged_json() {
        let req = Request::Send { line: json!({"type": "user", "uuid": "u1"}), tag: None };
        let text = serde_json::to_string(&req).unwrap();
        assert_eq!(serde_json::from_str::<serde_json::Value>(&text).unwrap(),
                   json!({"op": "send", "line": {"type": "user", "uuid": "u1"}}));
        assert_eq!(serde_json::from_str::<Request>(&text).unwrap(), req);
        let status: Request = serde_json::from_str(r#"{"op":"status"}"#).unwrap();
        assert_eq!(status, Request::Status);
    }

    #[test]
    fn log_line_round_trips() {
        let entries = vec![
            Entry::Started { pid: 7 },
            Entry::Out { line: r#"{"type":"result"}"#.into() },
            Entry::Sent { line: json!({"type": "user"}), tag: Some("item-2".into()) },
            Entry::Tool { id: 3, name: "post_update".into(), args: json!({"text": "hi"}) },
            Entry::Exit { code: Some(0), signal: None },
        ];
        for (i, entry) in entries.into_iter().enumerate() {
            let line = LogLine { seq: i as u64 + 1, entry };
            let text = serde_json::to_string(&line).unwrap();
            let value: serde_json::Value = serde_json::from_str(&text).unwrap();
            assert_eq!(value["seq"], json!(i + 1));
            assert!(value["kind"].is_string());
            assert_eq!(serde_json::from_str::<LogLine>(&text).unwrap(), line);
        }
    }

    #[test]
    fn run_dir_rejects_path_separators() {
        assert_eq!(run_dir("c-1").unwrap(), std::path::PathBuf::from("/var/agent/run/containers/c-1"));
        assert_eq!(log_path("c-1", "W-1").unwrap(),
                   std::path::PathBuf::from("/var/agent/run/containers/c-1/W-1.jsonl"));
        assert_eq!(socket_path("c-1").unwrap(),
                   std::path::PathBuf::from("/var/agent/run/containers/c-1/holder.sock"));
        for bad in ["", ".", "..", "a/b", "a\\b", "../x"] {
            assert!(run_dir(bad).is_err(), "{bad:?} accepted");
            assert!(log_path("c-1", bad).is_err(), "{bad:?} accepted as a work item");
        }
    }
}
