//! Helpers shared by the holder's integration tests: a real `agent-holder serve` on a temp dir, driven against the
//! fake Claude Code in rust/testdata.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use holder_proto::{Entry, LogLine, Request, Response, ToolSpec};
use serde_json::Value;

pub const FAKE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../testdata/fake_claude.py");
pub const CONVO: &str = "c1";

pub struct Holder {
    pub child: Child,
    pub root: PathBuf,
}

impl Holder {
    pub fn start(root: &Path) -> Holder {
        let _ = std::fs::remove_file(root.join(CONVO).join("holder.sock"));
        let child = Command::new(env!("CARGO_BIN_EXE_agent-holder"))
            .args(["serve", "--conversation", CONVO, "--root"])
            .arg(root)
            .spawn()
            .unwrap();
        let sock = root.join(CONVO).join("holder.sock");
        wait_for(|| UnixStream::connect(&sock).is_ok());
        Holder { child, root: root.to_path_buf() }
    }

    pub fn request(&self, req: &Request) -> Response {
        let mut stream = UnixStream::connect(self.root.join(CONVO).join("holder.sock")).unwrap();
        writeln!(stream, "{}", serde_json::to_string(req).unwrap()).unwrap();
        let mut line = String::new();
        BufReader::new(stream).read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap()
    }

    pub fn log(&self, work_item: &str) -> Vec<LogLine> {
        let path = self.root.join(CONVO).join(format!("{work_item}.jsonl"));
        std::fs::read_to_string(path)
            .unwrap_or_default()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    pub fn wait_exit(&self, work_item: &str) -> Vec<LogLine> {
        wait_for(|| self.log(work_item).iter().any(|l| matches!(l.entry, Entry::Exit { .. })));
        self.log(work_item)
    }
}

impl Drop for Holder {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn wait_for(mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !cond() {
        assert!(Instant::now() < deadline, "timed out");
        std::thread::sleep(Duration::from_millis(20));
    }
}

pub fn scenario(root: &Path, name: &str, steps: &[Value]) -> PathBuf {
    let path = root.join(format!("{name}.scenario"));
    let text: String = steps.iter().map(|s| format!("{s}\n")).collect();
    std::fs::write(&path, text).unwrap();
    path
}

pub fn start(root: &Path, work_item: &str, scenario: &Path, stdin: Vec<Value>, tools: Vec<ToolSpec>) -> Request {
    Request::Start {
        work_item: work_item.into(),
        argv: vec!["python3".into(), FAKE.into(), scenario.to_string_lossy().into()],
        env: BTreeMap::new(),
        cwd: root.to_string_lossy().into(),
        stdin,
        tools,
    }
}

pub fn outs(log: &[LogLine]) -> Vec<Value> {
    log.iter()
        .filter_map(|l| match &l.entry {
            Entry::Out { line } => Some(serde_json::from_str(line).unwrap()),
            _ => None,
        })
        .collect()
}

