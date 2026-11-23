//! A session's activity, read from runs/<work item>.jsonl (a port of dashboard/transcript.py).
//!
//! Each line is one record as the turn runner wrote it (the Python SDK's message classes by name: SystemMessage,
//! AssistantMessage, UserMessage, RateLimitEvent, ResultMessage), {"type", "truncated"} for one over 20 kB, or a
//! RunUsage with every API call the run has made so far; each carries "_ts". Lines become timeline entries; nothing
//! here fails on a strange line.

use std::collections::{HashMap, VecDeque};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use regex::Regex;
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::records::{py_dumps, py_lines, truthy};

const INPUT_CHARS: usize = 2000;
const RESULT_CHARS: usize = 4000;
const RAW_CHARS: usize = 500;
const SUMMARY_KEYS: [&str; 8] = ["command", "file_path", "path", "pattern", "url", "query", "description", "prompt"];
const MIN_SECRET: usize = 8;
const MAX_FILES: usize = 256;

/// Replaces the control plane's secret values (8 characters or more) with "[secret]", longest first.
#[derive(Clone)]
pub struct Scrubber(Option<Regex>);

impl Scrubber {
    pub fn new(secrets: &[String]) -> Scrubber {
        let mut values: Vec<&String> = secrets.iter().filter(|s| s.chars().count() >= MIN_SECRET).collect();
        values.sort_by_key(|s| std::cmp::Reverse(s.chars().count()));
        values.dedup();
        if values.is_empty() {
            return Scrubber(None);
        }
        let pattern = values.iter().map(|v| regex::escape(v)).collect::<Vec<_>>().join("|");
        Scrubber(Regex::new(&pattern).ok())
    }

    pub fn scrub(&self, text: &str) -> String {
        match &self.0 {
            Some(re) => re.replace_all(text, "[secret]").into_owned(),
            None => text.to_string(),
        }
    }
}

fn tokens_of(usage: &Value) -> Value {
    let n = |k: &str| usage[k].as_i64().filter(|_| usage[k].is_i64() || usage[k].is_u64()).unwrap_or(0);
    json!({"in": n("input_tokens"), "out": n("output_tokens"), "cache_read": n("cache_read_input_tokens"),
           "cache_write": n("cache_creation_input_tokens")})
}

fn cut(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit { text.into() } else { text.chars().take(limit - 1).collect::<String>() + "…" }
}

fn clean_input(value: &Value, scrub: &Scrubber) -> Value {
    match value {
        // scrub before cutting: a cut must never land inside a secret
        Value::String(s) => json!(cut(&scrub.scrub(s), INPUT_CHARS)),
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), clean_input(v, scrub))).collect()),
        Value::Array(a) => Value::Array(a.iter().map(|v| clean_input(v, scrub)).collect()),
        other => other.clone(),
    }
}

fn summary(name: &str, args: &Value) -> String {
    for key in SUMMARY_KEYS {
        if let Some(v) = args[key].as_str().map(str::trim).filter(|v| !v.is_empty()) {
            return py_lines(v).next().unwrap_or_default().chars().take(300).collect();
        }
    }
    name.into()
}

/// Python's str() of a value.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        other => py_dumps(other),
    }
}

fn flatten(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(blocks) => blocks.iter().map(|b| {
            if let Some(t) = b["text"].as_str() && b.is_object() {
                t.to_string()
            } else if b["type"] == "tool_reference" {
                format!("[{}]", b.get("tool_name").map_or("tool".to_string(), py_str))
            } else if b["type"] == "image" {
                "[image]".into()
            } else {
                py_dumps(b)
            }
        }).collect::<Vec<_>>().join("\n"),
        Value::Null => String::new(),
        other => py_dumps(other),
    }
}

/// Timeline entries for one record on line `line`.
pub fn parse_record(record: &Value, line: usize, scrub: &Scrubber) -> Vec<Value> {
    let ts = record["_ts"].as_str().map_or(Value::Null, |t| json!(t));
    let mut out: Vec<Value> = Vec::new();
    let add = |out: &mut Vec<Value>, kind: &str, fields: Value| {
        let mut m = Map::new();
        m.insert("id".into(), json!(format!("{line}:{}", out.len())));
        m.insert("ts".into(), ts.clone());
        m.insert("kind".into(), json!(kind));
        if let Value::Object(f) = fields {
            m.extend(f);
        }
        out.push(Value::Object(m));
    };
    let raw = || {
        let mut out = Vec::new();
        add(&mut out, "raw", json!({"text": cut(&scrub.scrub(&py_dumps(record)), RAW_CHARS)}));
        out
    };
    if record.get("truncated").is_some() {
        return raw();
    }
    let content = &record["content"];
    match record["type"].as_str() {
        Some("SystemMessage" | "RunUsage") => {}
        Some("AssistantMessage") => {
            let Some(blocks) = content.as_array() else { return raw() };
            for b in blocks.iter().filter(|b| b.is_object() && b.get("thinking").is_none()) {
                if let Some(text) = b["text"].as_str() {
                    add(&mut out, "text", json!({"role": "assistant", "text": scrub.scrub(text)}));
                } else if b.get("name").is_some() && b.get("id").is_some() {
                    let input = if b["input"].is_object() { &b["input"] } else { &json!({}) };
                    let args = clean_input(input, scrub);
                    let name = py_str(&b["name"]);
                    add(&mut out, "tool_call", json!({"tool": name, "tool_use_id": py_str(&b["id"]),
                                                      "summary": summary(&name, &args), "input": args}));
                }
            }
        }
        Some("UserMessage") => match content {
            Value::String(s) => add(&mut out, "text", json!({"role": "user", "text": scrub.scrub(s)})),
            Value::Array(blocks) => for b in blocks.iter().filter(|b| b.is_object()) {
                if b.get("tool_use_id").is_some() {
                    add(&mut out, "tool_result", json!({"tool_use_id": py_str(&b["tool_use_id"]),
                                                        "is_error": truthy(b.get("is_error")),
                                                        "content": cut(&scrub.scrub(&flatten(&b["content"])), RESULT_CHARS)}));
                } else if let Some(text) = b["text"].as_str() {
                    add(&mut out, "text", json!({"role": "user", "text": scrub.scrub(text)}));
                }
            },
            _ => return raw(),
        },
        Some("ResultMessage") => {
            let result = if truthy(record.get("result")) { py_str(&record["result"]) } else { String::new() };
            add(&mut out, "result", json!({"subtype": record.get("subtype").cloned().unwrap_or(Value::Null),
                                           "turns": record.get("num_turns").cloned().unwrap_or(Value::Null),
                                           "usage": tokens_of(&record["usage"]), "is_error": truthy(record.get("is_error")),
                                           "text": cut(&scrub.scrub(&result), RESULT_CHARS)}));
        }
        Some("RateLimitEvent") => {
            let info = if record["rate_limit_info"].is_object() { &record["rate_limit_info"] } else { &json!({}) };
            let get = |k: &str, d: &str| info.get(k).map_or(d.to_string(), py_str);
            add(&mut out, "rate_limit", json!({"text": format!("{}: {}", get("rate_limit_type", "limit"), get("status", "?"))}));
        }
        _ => return raw(),
    }
    out
}

/// Python's bytes.splitlines(): \n, \r\n and \r end a line.
fn byte_lines(data: &[u8]) -> Vec<&[u8]> {
    let (mut out, mut start, mut i) = (Vec::new(), 0, 0);
    while i < data.len() {
        match data[i] {
            b'\n' => { out.push(&data[start..i]); start = i + 1; }
            b'\r' => {
                out.push(&data[start..i]);
                if data.get(i + 1) == Some(&b'\n') { i += 1; }
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    if start < data.len() {
        out.push(&data[start..]);
    }
    out
}

fn record_of(raw_line: &[u8]) -> Value {
    match serde_json::from_slice::<Value>(raw_line) {
        Ok(v) if v.is_object() => v,
        _ => json!({"type": "unreadable",
                    "truncated": String::from_utf8_lossy(&raw_line[..raw_line.len().min(RAW_CHARS)]).into_owned()}),
    }
}

fn read_tail(path: &Path, offset: u64) -> Option<Vec<u8>> {
    let mut f = std::fs::File::open(path).ok()?;
    f.seek(SeekFrom::Start(offset)).ok()?;
    let mut data = Vec::new();
    f.read_to_end(&mut data).ok()?;
    Some(data)
}

/// Entries for the complete lines from byte `offset`, which is the start of line number `line`, and the cursor
/// (offset, line) where the next read starts. A last line without its newline is still being written: it is left
/// for the next read. Only the new bytes are read, so a stream that follows a long transcript stays cheap.
pub fn read_from(path: &Path, offset: u64, line: usize, scrub: &Scrubber) -> (Vec<Value>, u64, usize) {
    let Some(data) = read_tail(path, offset) else { return (Vec::new(), offset, line) };
    let end = data.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
    let (mut entries, mut line) = (Vec::new(), line);
    for raw_line in byte_lines(&data[..end]) {
        entries.extend(parse_record(&record_of(raw_line), line, scrub));
        line += 1;
    }
    (entries, offset + end as u64, line)
}

pub fn lines_before(path: &Path, offset: u64) -> usize {
    let Ok(mut f) = std::fs::File::open(path) else { return 0 };
    let mut data = Vec::new();
    if (&mut f).take(offset).read_to_end(&mut data).is_err() {
        return 0;
    }
    data.iter().filter(|b| **b == b'\n').count()
}

#[derive(Clone, Debug, Serialize)]
pub struct LiveStats {
    pub session_id: Option<String>,
    pub tokens: Value,
    pub turns: i64,
    pub done: bool,
}

/// What a session has used so far, fed one record at a time. The SDK splits one reply into several messages that
/// carry the same usage, so usage counts once per message id. The stream misses background agents' calls and a
/// ResultMessage covers the main thread only: the latest RunUsage has every call, so its tokens stand whenever there
/// is one.
#[derive(Default)]
struct Totals {
    session_id: Option<String>,
    by_message: Vec<(String, Value)>,
    result: Option<(Value, i64)>,
    counted: Option<Value>,
}

impl Totals {
    fn feed(&mut self, record: &Value) {
        match record["type"].as_str() {
            Some("SystemMessage") if record["subtype"] == "init" => {
                if let Some(sid) = record["data"]["session_id"].as_str() {
                    self.session_id = Some(sid.into());
                }
            }
            Some("AssistantMessage") if truthy(record.get("message_id")) => {
                let id = py_str(&record["message_id"]);
                let usage = tokens_of(&record["usage"]);
                match self.by_message.iter_mut().find(|(k, _)| *k == id) {
                    Some((_, u)) => *u = usage,
                    None => self.by_message.push((id, usage)),
                }
            }
            Some("ResultMessage") => {
                let turns = record["num_turns"].as_f64().map_or(0, |t| t as i64);
                self.result = Some((tokens_of(&record["usage"]), turns));
            }
            Some("RunUsage") => self.counted = Some(tokens_of(&record["usage"])),
            _ => {}
        }
    }

    fn stats(&self) -> LiveStats {
        let mut stats = match &self.result {
            Some((tokens, turns)) => LiveStats { session_id: self.session_id.clone(), tokens: tokens.clone(), turns: *turns, done: true },
            None => {
                let mut sum = [0i64; 4];
                for (_, u) in &self.by_message {
                    for (i, k) in ["in", "out", "cache_read", "cache_write"].iter().enumerate() {
                        sum[i] += u[*k].as_i64().unwrap_or(0);
                    }
                }
                LiveStats { session_id: self.session_id.clone(), turns: self.by_message.len() as i64, done: false,
                            tokens: json!({"in": sum[0], "out": sum[1], "cache_read": sum[2], "cache_write": sum[3]}) }
            }
        };
        if let Some(counted) = &self.counted {
            stats.tokens = counted.clone();
        }
        stats
    }
}

#[derive(Default)]
struct Followed {
    offset: u64,
    line: usize,
    totals: Totals,
    recent: VecDeque<Value>,
}

/// Live numbers and the last few entries of the transcripts being watched, read incrementally: each call reads only
/// what was appended since the last, so polling a long session costs what it added, not its whole length.
pub struct TranscriptCache {
    scrub: Scrubber,
    keep: usize,
    files: Mutex<(HashMap<PathBuf, Followed>, VecDeque<PathBuf>)>,
}

impl TranscriptCache {
    pub fn new(scrub: Scrubber, recent: usize) -> TranscriptCache {
        TranscriptCache { scrub, keep: recent, files: Mutex::default() }
    }

    pub fn follow(&self, path: &Path) -> (LiveStats, Vec<Value>) {
        let mut guard = self.files.lock().unwrap_or_else(|e| e.into_inner());
        let (files, order) = &mut *guard;
        order.retain(|p| p != path);
        order.push_back(path.to_path_buf()); // most recently followed last
        while order.len() > MAX_FILES {
            if let Some(old) = order.pop_front() {
                files.remove(&old);
            }
        }
        let state = files.entry(path.to_path_buf()).or_default();
        if let Some(data) = read_tail(path, state.offset) {
            let end = data.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
            for raw_line in byte_lines(&data[..end]) {
                let record = record_of(raw_line);
                state.totals.feed(&record);
                for e in parse_record(&record, state.line, &self.scrub) {
                    if state.recent.len() == self.keep {
                        state.recent.pop_front();
                    }
                    state.recent.push_back(e);
                }
                state.line += 1;
            }
            state.offset += end as u64;
        }
        (state.totals.stats(), state.recent.iter().cloned().collect())
    }
}
