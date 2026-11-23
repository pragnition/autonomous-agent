//! Claude Code's output frames as transcript records, exactly as the Python runner wrote them: the Python SDK's message
//! class name and its fields (`{"type": "AssistantMessage", "content": [...], ...}`). The dashboard's transcript view,
//! improve-jensen and the handoff note all read these records. Frames the SDK skips (unknown types, control
//! responses) or can't parse (a required field missing) have no record.

use serde_json::{Map, Value, json};

/// A record longer than this (as Python's json.dumps writes it) goes to core's event stream cut down to its type and
/// the first part of its text, as the Python runner sent it.
pub const MAX_EVENT_CHARS: usize = 20_000;

fn get(v: &Value, key: &str) -> Value {
    v.get(key).cloned().unwrap_or(Value::Null)
}

fn need(v: &Value, key: &str) -> Option<Value> {
    v.get(key).cloned()
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
    }
}

/// `data["origin"]` when it is an object with a string `kind`, passed through whole; otherwise absent.
fn origin(v: &Value) -> Value {
    match v.get("origin") {
        Some(o @ Value::Object(m)) if m.get("kind").is_some_and(Value::is_string) => o.clone(),
        _ => Value::Null,
    }
}

fn record(kind: &str, fields: Value) -> Value {
    let mut out = Map::new();
    out.insert("type".into(), Value::String(kind.into()));
    if let Value::Object(f) = fields {
        out.extend(f);
    }
    Value::Object(out)
}

/// One content block as the SDK's dataclass has it (no `type` key); None for a block it doesn't model. `assistant`
/// selects the blocks only assistant messages carry.
fn block(b: &Value, assistant: bool) -> Result<Option<Value>, ()> {
    let Value::Object(_) = b else { return Err(()) };
    let kind = b.get("type").and_then(Value::as_str).ok_or(())?;
    Ok(Some(match (kind, assistant) {
        ("text", _) => json!({"text": need(b, "text").ok_or(())?}),
        ("thinking", true) => json!({"thinking": need(b, "thinking").ok_or(())?, "signature": need(b, "signature").ok_or(())?}),
        ("tool_use", _) | ("server_tool_use", true) => {
            json!({"id": need(b, "id").ok_or(())?, "name": need(b, "name").ok_or(())?, "input": need(b, "input").ok_or(())?})
        }
        ("tool_result", _) => json!({"tool_use_id": need(b, "tool_use_id").ok_or(())?, "content": get(b, "content"),
                                     "is_error": get(b, "is_error")}),
        ("advisor_tool_result", true) => {
            json!({"tool_use_id": need(b, "tool_use_id").ok_or(())?, "content": need(b, "content").ok_or(())?})
        }
        _ => return Ok(None),
    }))
}

fn blocks(content: &[Value], assistant: bool) -> Option<Vec<Value>> {
    let mut out = Vec::new();
    for b in content {
        if let Some(b) = block(b, assistant).ok()? {
            out.push(b);
        }
    }
    Some(out)
}

pub fn to_record(f: &Value) -> Option<Value> {
    let kind = f.get("type").and_then(Value::as_str).unwrap_or("");
    let subtype = f.get("subtype").and_then(Value::as_str);
    if kind == "system" && matches!(subtype, Some("hook_started" | "hook_response")) {
        let name = ["hook_event", "hook_name", "hook_event_name"].iter()
            .map(|k| f.get(*k)).find(|v| truthy(*v)).flatten().cloned().unwrap_or(json!(""));
        return Some(record("HookEventMessage", json!({"subtype": get(f, "subtype"), "hook_event_name": name,
            "data": f, "session_id": get(f, "session_id"), "uuid": get(f, "uuid")})));
    }
    match kind {
        "user" => {
            let content = f.get("message")?.get("content")?;
            let content = match content {
                Value::Array(items) => Value::Array(blocks(items, false)?),
                other => other.clone(),
            };
            Some(record("UserMessage", json!({"content": content, "uuid": get(f, "uuid"),
                "parent_tool_use_id": get(f, "parent_tool_use_id"), "tool_use_result": get(f, "tool_use_result"),
                "origin": origin(f)})))
        }
        "assistant" => {
            let message = f.get("message")?;
            let Value::Array(items) = message.get("content")? else { return None };
            Some(record("AssistantMessage", json!({"content": blocks(items, true)?, "model": need(message, "model")?,
                "parent_tool_use_id": get(f, "parent_tool_use_id"), "error": get(f, "error"),
                "usage": get(message, "usage"), "message_id": get(message, "id"),
                "stop_reason": get(message, "stop_reason"), "session_id": get(f, "session_id"), "uuid": get(f, "uuid")})))
        }
        "system" => {
            let subtype = need(f, "subtype")?;
            Some(match subtype.as_str() {
                Some("task_started") => record("TaskStartedMessage", json!({"subtype": subtype, "data": f,
                    "task_id": need(f, "task_id")?, "description": need(f, "description")?, "uuid": need(f, "uuid")?,
                    "session_id": need(f, "session_id")?, "tool_use_id": get(f, "tool_use_id"),
                    "task_type": get(f, "task_type")})),
                Some("task_progress") => record("TaskProgressMessage", json!({"subtype": subtype, "data": f,
                    "task_id": need(f, "task_id")?, "description": need(f, "description")?, "usage": need(f, "usage")?,
                    "uuid": need(f, "uuid")?, "session_id": need(f, "session_id")?,
                    "tool_use_id": get(f, "tool_use_id"), "last_tool_name": get(f, "last_tool_name")})),
                Some("task_notification") => record("TaskNotificationMessage", json!({"subtype": subtype, "data": f,
                    "task_id": need(f, "task_id")?, "status": need(f, "status")?, "output_file": need(f, "output_file")?,
                    "summary": need(f, "summary")?, "uuid": need(f, "uuid")?, "session_id": need(f, "session_id")?,
                    "tool_use_id": get(f, "tool_use_id"), "usage": get(f, "usage")})),
                Some("task_updated") => {
                    let patch = match f.get("patch") {
                        Some(p @ Value::Object(_)) => p.clone(),
                        _ => json!({}),
                    };
                    record("TaskUpdatedMessage", json!({"subtype": subtype, "data": f,
                        "task_id": f.get("task_id").cloned().unwrap_or(json!("")), "status": get(&patch, "status"),
                        "patch": patch, "session_id": get(f, "session_id"), "uuid": get(f, "uuid")}))
                }
                Some("mirror_error") => record("MirrorErrorMessage", json!({"subtype": subtype, "data": f,
                    "key": get(f, "key"), "error": f.get("error").cloned().unwrap_or(json!(""))})),
                _ => record("SystemMessage", json!({"subtype": subtype, "data": f})),
            })
        }
        "result" => {
            let deferred = match f.get("deferred_tool_use") {
                d if truthy(d) => {
                    let d = d.unwrap();
                    json!({"id": need(d, "id")?, "name": need(d, "name")?, "input": need(d, "input")?})
                }
                _ => Value::Null,
            };
            Some(record("ResultMessage", json!({"subtype": need(f, "subtype")?, "duration_ms": need(f, "duration_ms")?,
                "duration_api_ms": need(f, "duration_api_ms")?, "is_error": need(f, "is_error")?,
                "num_turns": need(f, "num_turns")?, "session_id": need(f, "session_id")?,
                "stop_reason": get(f, "stop_reason"), "total_cost_usd": get(f, "total_cost_usd"),
                "usage": get(f, "usage"), "result": get(f, "result"), "structured_output": get(f, "structured_output"),
                "model_usage": get(f, "modelUsage"), "permission_denials": get(f, "permission_denials"),
                "deferred_tool_use": deferred, "errors": get(f, "errors"), "api_error_status": get(f, "api_error_status"),
                "uuid": get(f, "uuid"), "terminal_reason": get(f, "terminal_reason"), "origin": origin(f)})))
        }
        "stream_event" => Some(record("StreamEvent", json!({"uuid": need(f, "uuid")?, "session_id": need(f, "session_id")?,
            "event": need(f, "event")?, "parent_tool_use_id": get(f, "parent_tool_use_id")}))),
        "rate_limit_event" => {
            let info = need(f, "rate_limit_info")?;
            Some(record("RateLimitEvent", json!({"rate_limit_info": {"status": need(&info, "status")?,
                "resets_at": get(&info, "resetsAt"), "rate_limit_type": get(&info, "rateLimitType"),
                "utilization": get(&info, "utilization"), "overage_status": get(&info, "overageStatus"),
                "overage_resets_at": get(&info, "overageResetsAt"),
                "overage_disabled_reason": get(&info, "overageDisabledReason"), "raw": info},
                "uuid": need(f, "uuid")?, "session_id": need(f, "session_id")?})))
        }
        "conversation_reset" => Some(record("ConversationResetMessage", json!({
            "new_conversation_id": need(f, "new_conversation_id")?, "uuid": need(f, "uuid")?,
            "session_id": need(f, "session_id")?}))),
        _ => None,
    }
}

/// Python's `json.dumps` text (", " and ": " separators, non-ASCII escaped), which the Python runner measured.
pub fn py_dumps(v: &Value) -> String {
    fn string(s: &str, out: &mut String) {
        out.push('"');
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                    let mut buf = [0u16; 2];
                    for unit in c.encode_utf16(&mut buf) {
                        out.push_str(&format!("\\u{:04x}", unit));
                    }
                }
                c => out.push(c),
            }
        }
        out.push('"');
    }
    fn walk(v: &Value, out: &mut String) {
        match v {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => out.push_str(&n.to_string()),
            Value::String(s) => string(s, out),
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    walk(item, out);
                }
                out.push(']');
            }
            Value::Object(map) => {
                out.push('{');
                for (i, (k, item)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    string(k, out);
                    out.push_str(": ");
                    walk(item, out);
                }
                out.push('}');
            }
        }
    }
    let mut out = String::new();
    walk(v, &mut out);
    out
}

/// What goes to the event stream for a record: the record itself, or its type and the first MAX_EVENT_CHARS of its
/// text when it is longer.
pub fn truncate_for_event(record: Value) -> Value {
    let text = py_dumps(&record);
    if text.chars().count() <= MAX_EVENT_CHARS {
        return record;
    }
    json!({"type": get(&record, "type"), "truncated": text.chars().take(MAX_EVENT_CHARS).collect::<String>()})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_match_python_sdk() {
        let input = std::fs::read_to_string("tests/fixtures/records/input.jsonl").unwrap();
        let expected = std::fs::read_to_string("tests/fixtures/records/expected.jsonl").unwrap();
        for (i, (frame, want)) in input.lines().zip(expected.lines()).enumerate() {
            let frame: Value = serde_json::from_str(frame).unwrap();
            let want: Value = serde_json::from_str(want).unwrap();
            let got = to_record(&frame).unwrap_or(Value::Null);
            assert_eq!(got, want, "line {}: {}", i + 1, frame["type"]);
        }
    }

    #[test]
    fn py_dumps_matches_python_text() {
        let v = serde_json::json!({"a": "é\n\"q\"", "b": [1, 2.5, null, true], "c": "😀"});
        assert_eq!(py_dumps(&v), r#"{"a": "\u00e9\n\"q\"", "b": [1, 2.5, null, true], "c": "\ud83d\ude00"}"#);
    }

    #[test]
    fn long_records_are_cut_for_events() {
        let big = serde_json::json!({"type": "UserMessage", "content": "x".repeat(30_000)});
        let cut = truncate_for_event(big);
        assert_eq!(cut["type"], "UserMessage");
        assert_eq!(cut["truncated"].as_str().unwrap().len(), 20_000);
        let small = serde_json::json!({"type": "UserMessage", "content": "x"});
        assert_eq!(truncate_for_event(small.clone()), small);
    }
}
