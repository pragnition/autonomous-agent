//! What a turn's sessions used, counted from Claude Code's own session logs (a port of runner/usage.py).

//!
//! A result reports the main thread's usage only, a turn that stops without one reports nothing, and a background
//! helper's replies never reach the stream at all. Claude Code logs every reply it gets:
//! `<home>/projects/<project folder>/<sid>.jsonl` for the main thread and `<sid>/subagents/*.jsonl` for each helper,
//! one record per content block, each with the reply's id and usage, so each reply counts once, at its largest
//! numbers. A resumed session's log holds its earlier turns too: only replies from when this turn took it up count.
//! Each poll reads only what the logs gained since the last.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use serde_json::Value;

/// The counts, in the order agentd reports them: input, output, cache read, cache write.
pub const USAGE_FIELDS: [&str; 4] =
    ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"];

pub struct SessionUsage {
    projects: PathBuf,
    /// Session id -> when this turn took it up (epoch seconds, to the millisecond, as Claude Code stamps records).
    since: BTreeMap<String, f64>,
    /// Log -> bytes read so far.
    read: HashMap<PathBuf, usize>,
    replies: HashMap<String, [u64; 4]>,
}

impl SessionUsage {
    pub fn new(claude_home: &Path) -> SessionUsage {
        SessionUsage { projects: claude_home.join("projects"), since: BTreeMap::new(), read: HashMap::new(),
                       replies: HashMap::new() }
    }

    /// Count the session's replies from `since` on; a session already watched keeps its first start.
    pub fn watch(&mut self, sid: &str, since: f64) {
        self.since.entry(sid.to_string()).or_insert((since * 1000.0).trunc() / 1000.0);
    }

    /// The totals so far; None while no session has a log.
    pub fn poll(&mut self) -> Option<[u64; 4]> {
        let projects: Vec<PathBuf> = std::fs::read_dir(&self.projects).into_iter().flatten().flatten()
            .map(|e| e.path()).collect();
        let since: Vec<(String, f64)> = self.since.iter().map(|(s, t)| (s.clone(), *t)).collect();
        for (sid, since) in since {
            for project in &projects {
                let mut logs = vec![project.join(format!("{sid}.jsonl"))];
                if let Ok(helpers) = std::fs::read_dir(project.join(&sid).join("subagents")) {
                    logs.extend(helpers.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "jsonl")));
                }
                for log in logs.into_iter().filter(|p| p.is_file()) {
                    self.read_on(log, since);
                }
            }
        }
        if self.read.is_empty() {
            return None;
        }
        Some(self.replies.values().fold([0; 4], |a, r| std::array::from_fn(|i| a[i] + r[i])))
    }

    fn read_on(&mut self, path: PathBuf, since: f64) {
        let start = self.read.get(&path).copied().unwrap_or(0);
        let Ok(data) = std::fs::read(&path) else { return };
        let data = data.get(start..).unwrap_or_default();
        // a last line without its newline is still being written: the next poll reads it
        let end = data.iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
        self.read.insert(path, start + end);
        for line in data[..end].split(|&b| b == b'\n') {
            if let Some((id, counts)) = reply(line, since) {
                let seen = self.replies.entry(id).or_insert(counts);
                *seen = std::array::from_fn(|i| seen[i].max(counts[i]));
            }
        }
    }
}

/// (reply id, its counts) for an assistant record from `since` on.
fn reply(line: &[u8], since: f64) -> Option<(String, [u64; 4])> {
    if !line.windows(7).any(|w| w == b"\"usage\"") {
        return None;
    }
    let record: Value = serde_json::from_slice(line).ok()?;
    let at = chrono::DateTime::parse_from_rfc3339(record.get("timestamp")?.as_str()?).ok()?;
    let at = at.timestamp() as f64 + f64::from(at.timestamp_subsec_millis()) / 1000.0;
    let message = record.get("message")?;
    if record.get("type")?.as_str()? != "assistant" || at < since {
        return None;
    }
    let usage = message.get("usage")?.as_object()?;
    let id = message.get("id")?.as_str()?;
    Some((id.to_string(), USAGE_FIELDS.map(|k| usage.get(k).and_then(Value::as_u64).unwrap_or(0))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn reply(id: &str, at: &str, counts: [u64; 4]) -> String {
        serde_json::json!({"type": "assistant", "timestamp": at, "message": {"id": id, "usage": {
            "input_tokens": counts[0], "output_tokens": counts[1], "cache_read_input_tokens": counts[2],
            "cache_creation_input_tokens": counts[3]}}}).to_string()
    }

    #[test]
    fn usage_counts_each_reply_once_from_since() {
        let home = tempfile::tempdir().unwrap();
        let dir = home.path().join("projects/-var-agent-scratch-V-1");
        std::fs::create_dir_all(dir.join("S1/subagents")).unwrap();
        let mut usage = SessionUsage::new(home.path());
        assert_eq!(usage.poll(), None, "no session watched");
        let since = chrono::DateTime::parse_from_rfc3339("2026-11-23T10:00:01Z").unwrap().timestamp() as f64;
        usage.watch("S1", since);
        assert_eq!(usage.poll(), None, "no log yet");
        let main = [reply("m0", "2026-11-23T10:00:00.999Z", [1000, 1000, 1000, 1000]), // before the turn took it up
                    reply("m1", "2026-11-23T10:00:02.000Z", [10, 1, 100, 5]),
                    reply("m1", "2026-11-23T10:00:02.500Z", [10, 7, 100, 5]),         // the same reply, another block
                    r#"{"type":"user","timestamp":"2026-11-23T10:00:03Z","message":{"id":"u","usage":{"input_tokens":9}}}"#.into()];
        std::fs::write(dir.join("S1.jsonl"), main.join("\n") + "\n" + &reply("m2", "2026-11-23T10:00:04Z", [3, 0, 0, 0])).unwrap();
        std::fs::write(dir.join("S1/subagents/agent-a.jsonl"), reply("m3", "2026-11-23T10:00:05Z", [2, 2, 0, 0]) + "\n").unwrap();
        assert_eq!(usage.poll(), Some([12, 9, 100, 5]), "a line still being written isn't counted yet");
        std::fs::OpenOptions::new().append(true).open(dir.join("S1.jsonl")).unwrap().write_all(b"\n").unwrap();
        assert_eq!(usage.poll(), Some([15, 9, 100, 5]));
        assert_eq!(usage.poll(), Some([15, 9, 100, 5]), "nothing new: the same");
    }
}
