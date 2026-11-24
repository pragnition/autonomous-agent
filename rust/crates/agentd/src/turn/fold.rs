//! One attempt of a turn (one Claude Code process), rebuilt from its log entries: the messages sent and read, the
//! helpers and moved-aside shells still running, the tool calls in flight, and the results. The same entries always
//! give the same state, so agentd rebuilds it after a restart by reading the log again.

use std::collections::{BTreeMap, BTreeSet};

use holder_proto::Entry;
use serde_json::Value;

/// The helpers a turn stays open for (the Agent SDK's set): delegated agents and workflows, which finish by
/// themselves and wake the session when they do. Not background shells (a dev server runs until it is stopped).
const WAITED_TASK_TYPES: [&str; 2] = ["local_agent", "local_workflow"];
/// A task has ended on one of these, from a task_notification or a task_updated.
const TASK_ENDED: [&str; 4] = ["completed", "failed", "stopped", "killed"];
/// A result with one of these moves the turn to another account.
pub const FORK_STATUSES: [u64; 2] = [403, 503];

#[derive(Debug, Default)]
pub struct Attempt {
    /// The uuid of the attempt's first message, its prompt.
    pub prompt: Option<String>,
    /// Each message sent and not yet echoed back -> the work item it carries (None for the prompt).
    pub pending: BTreeMap<String, Option<String>>,
    /// The work items of the messages Claude Code read, in order.
    pub read: Vec<String>,
    pub helpers: BTreeSet<String>,
    /// Shells a person's message moved to the background; the turn waits for them as for helpers.
    pub moved: BTreeSet<String>,
    /// Tool calls of the main thread without a result yet.
    pub open_tools: BTreeSet<String>,
    /// A turn is under way: from the prompt, a message read or the main thread's first message, to its result.
    pub turn: bool,
    pub session_id: Option<String>,
    pub saw_assistant: bool,
    /// The results that answer this turn, from once the prompt was read.
    pub results: Vec<Value>,
    /// The last result ends the turn by the turn-end rule, and no message has been sent since.
    pub result_end: bool,
    /// Results after this are not counted: agentd has decided to end the attempt.
    pub frozen: bool,
    pub exit: Option<(Option<i32>, Option<i32>)>,
}

/// What an entry meant for the runner.
pub enum Seen {
    Nothing,
    /// A frame from Claude Code; `first_assistant` the first assistant message of the attempt.
    Frame { frame: Value, first_assistant: bool },
    /// A tool call through the holder, waiting for its result.
    Tool { id: u64, name: String, args: Value },
    Exit,
}

impl Attempt {
    pub fn new() -> Attempt {
        Attempt { turn: true, ..Default::default() }
    }

    /// Whether a result now would end the attempt, given whether agentd is stopping it.
    fn ends(&self, stopping: bool, switching: bool) -> bool {
        (self.pending.is_empty() && self.helpers.is_empty() && self.moved.is_empty()) || stopping || switching
    }

    /// Nothing running but Claude Code thinking or writing: no tool call, helper or moved shell.
    pub fn quiet(&self) -> bool {
        self.turn && self.open_tools.is_empty() && self.helpers.is_empty() && self.moved.is_empty()
    }

    /// The last counted result moves the turn to another account (a rejected or unavailable account).
    pub fn switching(&self) -> bool {
        self.results.last().is_some_and(switching)
    }

    pub fn apply(&mut self, entry: &Entry, stopping: bool) -> Seen {
        match entry {
            Entry::Started { .. } => Seen::Nothing,
            Entry::Sent { line, tag } => {
                if let Some(uuid) = line.get("uuid").and_then(Value::as_str).filter(|_| line["type"] == "user") {
                    if self.prompt.is_none() {
                        self.prompt = Some(uuid.to_string());
                        self.pending.insert(uuid.to_string(), None);
                    } else {
                        self.pending.insert(uuid.to_string(), tag.clone());
                        self.result_end = false; // it runs as a next turn in this process
                    }
                }
                Seen::Nothing
            }
            Entry::Out { line } => match serde_json::from_str::<Value>(line) {
                Ok(frame) => self.frame(frame, stopping),
                Err(_) => Seen::Nothing,
            },
            Entry::Tool { id, name, args } => Seen::Tool { id: *id, name: name.clone(), args: args.clone() },
            Entry::Exit { code, signal } => {
                self.exit = Some((*code, *signal));
                Seen::Exit
            }
        }
    }

    fn frame(&mut self, frame: Value, stopping: bool) -> Seen {
        if let Some(sid) = frame.get("session_id").and_then(Value::as_str).filter(|s| !s.is_empty()) {
            self.session_id = Some(sid.to_string());
        }
        let main_thread = frame.get("parent_tool_use_id").is_none_or(Value::is_null);
        let blocks = frame.pointer("/message/content").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut first_assistant = false;
        match frame.get("type").and_then(Value::as_str).unwrap_or("") {
            "user" => {
                let echoed = frame.get("uuid").and_then(Value::as_str).and_then(|u| self.pending.remove(u));
                if let Some(item) = echoed {
                    self.turn = true;
                    self.read.extend(item);
                }
                for block in blocks.iter().filter(|b| b["type"] == "tool_result") {
                    if let Some(id) = block.get("tool_use_id").and_then(Value::as_str) {
                        self.open_tools.remove(id);
                    }
                }
                let moved = frame.get("tool_use_result").filter(|r| r["backgroundedToDeliverMessage"] == true);
                if let Some(task) = moved.and_then(|r| r.get("backgroundTaskId")).and_then(Value::as_str) {
                    self.moved.insert(task.to_string());
                }
            }
            "assistant" => {
                first_assistant = !self.saw_assistant;
                self.saw_assistant = true;
                if main_thread {
                    self.turn = true;
                    for block in blocks.iter().filter(|b| b["type"] == "tool_use") {
                        if let Some(id) = block.get("id").and_then(Value::as_str) {
                            self.open_tools.insert(id.to_string());
                        }
                    }
                }
            }
            "system" => {
                let task = frame.get("task_id").and_then(Value::as_str).unwrap_or_default().to_string();
                let ended = match frame.get("subtype").and_then(Value::as_str) {
                    Some("task_started") => {
                        let kind = frame.get("task_type").and_then(Value::as_str).unwrap_or_default();
                        if !task.is_empty() && WAITED_TASK_TYPES.contains(&kind) {
                            self.helpers.insert(task.clone());
                        }
                        false
                    }
                    Some("task_notification") => true,
                    Some("task_updated") => frame.pointer("/patch/status").and_then(Value::as_str)
                        .is_some_and(|s| TASK_ENDED.contains(&s)),
                    _ => false,
                };
                if ended {
                    self.helpers.remove(&task);
                    self.moved.remove(&task);
                }
            }
            "result" => {
                self.turn = false;
                self.open_tools.clear();
                let switching = switching(&frame);
                // a result from before the prompt was read answers something else (a background task from an earlier
                // turn finishing): it doesn't count, unless it moves the turn to another account
                let prompt_read = self.prompt.as_ref().is_some_and(|p| !self.pending.contains_key(p));
                if !self.frozen {
                    if prompt_read || switching {
                        self.results.push(frame.clone());
                    }
                    self.result_end = self.ends(stopping, switching);
                }
            }
            _ => {}
        }
        Seen::Frame { frame, first_assistant }
    }
}

fn switching(result: &Value) -> bool {
    result["is_error"] == true && result.get("api_error_status").and_then(Value::as_u64).is_some_and(|s| FORK_STATUSES.contains(&s))
}
