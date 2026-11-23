//! The note that starts a fresh session in place of one that can't be resumed on the account it would run on
//! (a port of runner/handoff.py).

//!
//! A session is never resumed on a different account from the one it ran on, and its full history never reaches the
//! fresh session by any other route either. The note carries a short digest: the session id, the text of its last few
//! assistant messages (capped) and the files it edited or wrote, and says where its record is for looking up one
//! detail.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};

use serde_json::Value;

/// The earlier session's last assistant messages put in the digest.
const LAST_MESSAGES: usize = 3;
/// Their text in total, at most.
const MAX_DIGEST_CHARS: usize = 4000;
/// The files it edited or wrote, at most (the latest ones).
const MAX_FILES: usize = 40;
const EDIT_TOOLS: [&str; 4] = ["Edit", "MultiEdit", "Write", "NotebookEdit"];

/// Where Claude Code keeps the session's record, `<home>/projects/<project folder>/<sid>.jsonl`: the real path when
/// it's there, otherwise the pattern to look under.
pub fn session_file(claude_home: Option<&Path>, sid: &str) -> String {
    let Some(home) = claude_home else { return format!("<CLAUDE_CONFIG_DIR>/projects/*/{sid}.jsonl") };
    let projects = home.join("projects");
    let mut found: Vec<PathBuf> = std::fs::read_dir(&projects).into_iter().flatten().flatten()
        .map(|e| e.path().join(format!("{sid}.jsonl"))).filter(|p| p.is_file()).collect();
    found.sort();
    found.first().unwrap_or(&projects.join("*").join(format!("{sid}.jsonl"))).display().to_string()
}

pub fn handoff_note(sid: &str, claude_home: Option<&Path>, transcript: Option<&Path>) -> String {
    let mut records: Vec<String> = transcript.map(|t| t.display().to_string()).into_iter().collect();
    records.push(session_file(claude_home, sid));
    let (texts, files) = digest(sid, &records);
    let mut summary = if texts.is_empty() {
        "There is no digest of its messages (its record wasn't found or had none).\n\n".to_string()
    } else {
        let last: Vec<String> = texts.iter().enumerate().map(|(i, t)| format!("[{}] {t}", i + 1)).collect();
        format!("Its last messages, oldest first:\n\n{}\n\n", last.join("\n\n"))
    };
    if !files.is_empty() {
        summary += &format!("Files it edited or wrote: {}\n\n", files.join(", "));
    }
    format!("(This is a fresh session. Your earlier session in this conversation, {sid}, ran on another Claude account, \
             and a session is never resumed on a different account, so you don't have its history. Here is a short \
             digest of it.\n\n{summary}Catch up with read_thread and from the conversation folder's own state (git \
             status, your notes there), then carry on with the task. Don't resume the earlier session (no /resume, no \
             --resume) and don't read or load its record whole. If you need one specific detail from it, look it up in \
             {} with grep or tail.)\n\n", records.join(" or "))
}

/// The digest of the earlier session from the first of its records that has any of its messages.
fn digest(sid: &str, records: &[String]) -> (Vec<String>, Vec<String>) {
    for path in records.iter().map(Path::new).filter(|p| p.is_file()) {
        let Ok(data) = std::fs::read(path) else { continue };
        let (texts, files) = read_record(&String::from_utf8_lossy(&data), sid);
        if !texts.is_empty() || !files.is_empty() {
            return (cap(texts), files);
        }
    }
    (Vec::new(), Vec::new())
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// The content blocks of one of the session's own assistant messages, from either record: agentd's transcript (an
/// AssistantMessage, tagged with its session) or Claude Code's session log (type "assistant"). None for anything
/// else, a helper's messages included.
fn assistant_blocks<'a>(record: &'a Value, sid: &str) -> Option<&'a Vec<Value>> {
    match record.get("type")?.as_str()? {
        "AssistantMessage" => {
            let other_session = record.get("session_id").is_some_and(|s| !s.is_null() && s.as_str() != Some(sid));
            if truthy(record.get("parent_tool_use_id")) || other_session {
                return None;
            }
            record.get("content")?.as_array()
        }
        "assistant" if !truthy(record.get("isSidechain")) => record.get("message")?.get("content")?.as_array(),
        _ => None,
    }
}

/// The text of the session's last assistant messages and the files it edited or wrote.
fn read_record(text: &str, sid: &str) -> (Vec<String>, Vec<String>) {
    let mut texts = VecDeque::new();
    let mut files: Vec<String> = Vec::new();
    for line in text.lines() {
        let Ok(record) = serde_json::from_str::<Value>(line) else { continue };
        let Some(blocks) = assistant_blocks(&record, sid).filter(|b| !b.is_empty()) else { continue };
        let joined: Vec<&str> = blocks.iter()
            .filter(|b| b.get("type").is_none_or(|t| t == "text"))
            .filter_map(|b| b.get("text")?.as_str()).collect();
        let joined = joined.join("\n").trim().to_string();
        if !joined.is_empty() {
            if texts.len() == LAST_MESSAGES {
                texts.pop_front();
            }
            texts.push_back(joined);
        }
        for b in blocks {
            let edit = b.get("name").and_then(Value::as_str).is_some_and(|n| EDIT_TOOLS.contains(&n));
            let Some(input) = b.get("input").filter(|i| edit && i.is_object()) else { continue };
            let target = if truthy(input.get("file_path")) { input.get("file_path") } else { input.get("notebook_path") };
            if let Some(target) = target.and_then(Value::as_str).filter(|t| !t.is_empty()) {
                files.retain(|f| f != target);
                files.push(target.to_string());
            }
        }
    }
    let skip = files.len().saturating_sub(MAX_FILES);
    (texts.into(), files.split_off(skip))
}

/// Each message cut to its share of MAX_DIGEST_CHARS, so the digest stays short however long they were.
fn cap(texts: Vec<String>) -> Vec<String> {
    let share = MAX_DIGEST_CHARS / texts.len().max(1);
    texts.into_iter().map(|t| {
        if t.chars().count() <= share {
            t
        } else {
            t.chars().take(share - 1).collect::<String>().trim_end().to_string() + "…"
        }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn handoff_note_matches_python() {
        let cases: Vec<serde_json::Value> =
            serde_json::from_str(&std::fs::read_to_string("tests/fixtures/handoff/expected.json").unwrap()).unwrap();
        for c in cases {
            let case = &c["case"];
            let note = handoff_note(case[0].as_str().unwrap(), case[1].as_str().map(Path::new),
                                    case[2].as_str().map(Path::new));
            assert_eq!(note, c["note"].as_str().unwrap(), "{case}");
        }
    }
}
