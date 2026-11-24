//! `agent-holder serve` against the fake Claude Code in rust/testdata.

mod common;

use std::process::Command;

use common::*;
use holder_proto::{Entry, Request, Response, ToolSpec};
use serde_json::json;

#[test]
fn start_writes_numbered_log_and_exit() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"emit": {"type": "system", "subtype": "init"}}),
                                        json!({"emit": {"type": "result", "subtype": "success"}}),
                                        json!({"exit": 0})]);
    assert!(matches!(h.request(&start(dir.path(), "W-1", &s, vec![], vec![])), Response::Started { .. }));
    let log = h.wait_exit("W-1");
    let seqs: Vec<u64> = log.iter().map(|l| l.seq).collect();
    assert_eq!(seqs, (1..=log.len() as u64).collect::<Vec<_>>());
    assert!(matches!(log[0].entry, Entry::Started { .. }));
    assert_eq!(outs(&log), vec![json!({"type": "system", "subtype": "init"}),
                                json!({"type": "result", "subtype": "success"})]);
    assert_eq!(log.last().unwrap().entry, Entry::Exit { code: Some(0), signal: None });
}

#[test]
fn send_is_logged_before_written() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"wait_stdin": 2}), json!({"exit": 0})]);
    let init = json!({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}});
    h.request(&start(dir.path(), "W-1", &s, vec![init], vec![]));
    let msg = json!({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}});
    assert_eq!(h.request(&Request::Send { line: msg, tag: Some("item-2".into()) }), Response::Ok);
    let log = h.wait_exit("W-1");
    let sent = log.iter().position(|l| matches!(&l.entry, Entry::Sent { line, tag }
                                                 if line["uuid"] == "u1" && tag.as_deref() == Some("item-2"))).unwrap();
    let echo = log.iter().position(|l| matches!(&l.entry, Entry::Out { line } if line.contains("\"u1\""))).unwrap();
    assert!(sent < echo, "Sent {sent} after echo {echo}");
}

#[test]
fn close_ends_input_and_claude_exits() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"emit": {"type": "result"}})]); // then waits for its input to close
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    wait_for(|| !outs(&h.log("W-1")).is_empty());
    assert_eq!(h.request(&Request::Close { grace_s: 5 }), Response::Ok);
    assert_eq!(h.wait_exit("W-1").last().unwrap().entry, Entry::Exit { code: Some(0), signal: None });
}

#[test]
fn close_terminates_claude_that_keeps_running() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"emit": {"type": "busy"}}), json!({"sleep_ms": 60000})]);
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    wait_for(|| !outs(&h.log("W-1")).is_empty());
    h.request(&Request::Close { grace_s: 1 });
    assert_eq!(h.wait_exit("W-1").last().unwrap().entry, Entry::Exit { code: None, signal: Some(15) });
}

#[test]
fn second_start_while_running_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"wait_stdin": 100})]);
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    assert!(matches!(h.request(&start(dir.path(), "W-2", &s, vec![], vec![])), Response::Error { .. }));
    h.request(&Request::Stop { grace_s: 1 });
    h.wait_exit("W-1");
}

#[test]
fn log_survives_holder_restart_and_numbering_continues() {
    let dir = tempfile::tempdir().unwrap();
    let s = scenario(dir.path(), "s", &[json!({"emit": {"type": "a"}}), json!({"exit": 0})]);
    {
        let h = Holder::start(dir.path());
        h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
        h.wait_exit("W-1");
    }
    let h = Holder::start(dir.path());
    let first = h.log("W-1").len();
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    wait_for(|| h.log("W-1").len() > first
             && matches!(h.log("W-1").last().unwrap().entry, Entry::Exit { .. }));
    let seqs: Vec<u64> = h.log("W-1").iter().map(|l| l.seq).collect();
    assert_eq!(seqs, (1..=seqs.len() as u64).collect::<Vec<_>>());
}

#[test]
fn long_line_kept_whole() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let big = "x".repeat(3 * 1024 * 1024);
    let s = scenario(dir.path(), "s", &[json!({"emit": {"type": "big", "data": big}}), json!({"exit": 0})]);
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    let log = h.wait_exit("W-1");
    assert_eq!(outs(&log)[0]["data"].as_str().unwrap().len(), 3 * 1024 * 1024);
}

#[test]
fn tool_call_waits_for_result() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"wait_stdin": 100})]);
    let tool = ToolSpec { name: "post_update".into(), description: "post".into(), input_schema: json!({}) };
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![tool.clone()]));
    assert_eq!(h.request(&Request::Tools), Response::Tools { tools: vec![tool] });
    let root = dir.path().to_path_buf();
    let caller = std::thread::spawn(move || {
        let h2 = Holder { child: Command::new("true").spawn().unwrap(), root };
        h2.request(&Request::Tool { name: "post_update".into(), args: json!({"text": "hi"}) })
    });
    wait_for(|| h.log("W-1").iter().any(|l| matches!(l.entry, Entry::Tool { .. })));
    let id = h.log("W-1").iter().find_map(|l| match &l.entry {
        Entry::Tool { id, name, args } => {
            assert_eq!((name.as_str(), args), ("post_update", &json!({"text": "hi"})));
            Some(*id)
        }
        _ => None,
    }).unwrap();
    assert_eq!(h.request(&Request::ToolResult { id, text: "posted".into() }), Response::Ok);
    assert_eq!(caller.join().unwrap(), Response::ToolText { text: "posted".into() });
    h.request(&Request::Stop { grace_s: 1 });
    h.wait_exit("W-1");
}

fn group_members(pgid: u32) -> Vec<u32> {
    std::fs::read_dir("/proc").unwrap().filter_map(|e| {
        let pid: u32 = e.ok()?.file_name().to_str()?.parse().ok()?;
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        let after = &stat[stat.rfind(')')? + 2..];
        let fields: Vec<&str> = after.split_whitespace().collect();
        (fields[0] != "Z" && fields[2].parse::<u32>().ok()? == pgid).then_some(pid)
    }).collect()
}

#[test]
fn stop_terminates_process_group() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"spawn": ["sleep", "300"]}), json!({"emit": {"type": "spawned"}}),
                                        json!({"wait_stdin": 100})]);
    let Response::Started { pid } = h.request(&start(dir.path(), "W-1", &s, vec![], vec![])) else { panic!() };
    wait_for(|| !outs(&h.log("W-1")).is_empty());
    assert!(group_members(pid).len() >= 2, "fake claude and its child share a group");
    assert_eq!(h.request(&Request::Stop { grace_s: 1 }), Response::Ok);
    h.wait_exit("W-1");
    wait_for(|| group_members(pid).is_empty());
}

#[test]
fn status_reports_listener_of_running_turn() {
    let dir = tempfile::tempdir().unwrap();
    let h = Holder::start(dir.path());
    let s = scenario(dir.path(), "s", &[json!({"listen": 45678}), json!({"emit": {"type": "listening"}}),
                                        json!({"wait_stdin": 100})]);
    h.request(&start(dir.path(), "W-1", &s, vec![], vec![]));
    wait_for(|| !outs(&h.log("W-1")).is_empty());
    let Response::Status(status) = h.request(&Request::Status) else { panic!() };
    assert_eq!(status.work_item.as_deref(), Some("W-1"));
    assert!(status.alive);
    assert!(status.listeners.contains(&45678), "{:?}", status.listeners);
    h.request(&Request::Stop { grace_s: 1 });
    h.wait_exit("W-1");
    let Response::Status(status) = h.request(&Request::Status) else { panic!() };
    assert!(!status.alive);
}
