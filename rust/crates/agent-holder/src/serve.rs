//! `agent-holder serve`: the main process of a conversation's container.
//!
//! It starts Claude Code for a turn when `agentd` asks, keeps its input open, and writes everything that happens to
//! the turn's numbered log on the shared volume: each output line, each line written to its input, each tool call
//! waiting for `agentd`, and how it exited. Docker owns this process, not `agentd`, so a turn carries on while
//! `agentd` restarts; `agentd` reads the log from where it stopped.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow};
use holder_proto::{Entry, LogLine, Request, Response, Status, ToolSpec};
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::process::{ChildStdin, Command};
use tokio::sync::{Mutex, oneshot};

use crate::listeners;

/// A turn's log: append-only, one `LogLine` per line, numbered from 1 across every run of the same work item.
struct Log {
    file: Mutex<(tokio::fs::File, u64)>,
}

impl Log {
    async fn open(path: &Path) -> Result<Log> {
        let seq = match tokio::fs::read(path).await {
            Ok(data) => data.iter().filter(|&&b| b == b'\n').count() as u64,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(e).context("reading the turn log"),
        };
        let file = tokio::fs::OpenOptions::new().create(true).append(true).open(path).await?;
        Ok(Log { file: Mutex::new((file, seq)) })
    }

    async fn append(&self, entry: Entry) -> Result<u64> {
        let mut guard = self.file.lock().await;
        let (file, seq) = &mut *guard;
        *seq += 1;
        let mut line = serde_json::to_vec(&LogLine { seq: *seq, entry })?;
        line.push(b'\n');
        file.write_all(&line).await?;
        file.flush().await?;
        Ok(*seq)
    }

    async fn seq(&self) -> u64 {
        self.file.lock().await.1
    }
}

struct Turn {
    work_item: String,
    pid: u32,
    alive: bool,
    stdin: Option<ChildStdin>,
    log: Arc<Log>,
}

#[derive(Default)]
struct Inner {
    turn: Option<Turn>,
    tools: Vec<ToolSpec>,
    next_tool: u64,
    pending_tools: HashMap<u64, oneshot::Sender<String>>,
}

struct Holder {
    dir: PathBuf,
    inner: Mutex<Inner>,
}

pub async fn serve(root: PathBuf, conversation: String) -> Result<()> {
    let dir = root.join(holder_proto::component(&conversation)?);
    tokio::fs::create_dir_all(&dir).await?;
    let sock = dir.join("holder.sock");
    let _ = tokio::fs::remove_file(&sock).await;
    let listener = UnixListener::bind(&sock).with_context(|| format!("binding {}", sock.display()))?;
    let holder = Arc::new(Holder { dir, inner: Mutex::new(Inner::default()) });
    loop {
        let (stream, _) = listener.accept().await?;
        let holder = holder.clone();
        tokio::spawn(async move {
            if let Err(e) = connection(holder, stream).await {
                eprintln!("agent-holder: connection ended: {e:#}");
            }
        });
    }
}

async fn connection(holder: Arc<Holder>, stream: UnixStream) -> Result<()> {
    let (read, mut write) = stream.into_split();
    let mut lines = BufReader::new(read).lines();
    while let Some(line) = lines.next_line().await? {
        let response = match serde_json::from_str::<Request>(&line) {
            Ok(req) => holder.handle(req).await.unwrap_or_else(|e| Response::Error { message: format!("{e:#}") }),
            Err(e) => Response::Error { message: format!("bad request: {e}") },
        };
        let mut out = serde_json::to_vec(&response)?;
        out.push(b'\n');
        write.write_all(&out).await?;
    }
    Ok(())
}

impl Holder {
    async fn handle(self: &Arc<Self>, req: Request) -> Result<Response> {
        match req {
            Request::Start { work_item, argv, env, cwd, stdin, tools } => {
                self.start(work_item, argv, env, cwd, stdin, tools).await
            }
            Request::Send { line, tag } => {
                let mut inner = self.inner.lock().await;
                let turn = inner.turn.as_mut().filter(|t| t.alive).ok_or_else(|| anyhow!("no turn is running"))?;
                write_line(turn, line, tag).await?;
                Ok(Response::Ok)
            }
            Request::Close { grace_s } => {
                let mut inner = self.inner.lock().await;
                if let Some(turn) = inner.turn.as_mut().filter(|t| t.alive) {
                    turn.stdin = None;
                    let (holder, pid) = (self.clone(), turn.pid);
                    tokio::spawn(async move {
                        for signal in [Signal::SIGTERM, Signal::SIGKILL] {
                            tokio::time::sleep(Duration::from_secs(grace_s)).await;
                            if !holder.inner.lock().await.turn.as_ref().is_some_and(|t| t.alive && t.pid == pid) {
                                return;
                            }
                            let _ = nix::sys::signal::kill(Pid::from_raw(pid as i32), signal);
                        }
                    });
                }
                Ok(Response::Ok)
            }
            Request::Stop { grace_s } => {
                let inner = self.inner.lock().await;
                if let Some(turn) = inner.turn.as_ref().filter(|t| t.alive) {
                    let pgid = Pid::from_raw(turn.pid as i32);
                    let _ = killpg(pgid, Signal::SIGTERM);
                    tokio::spawn(async move {
                        tokio::time::sleep(Duration::from_secs(grace_s)).await;
                        let _ = killpg(pgid, Signal::SIGKILL);
                    });
                }
                Ok(Response::Ok)
            }
            Request::Status => Ok(Response::Status(self.status().await)),
            Request::Tools => Ok(Response::Tools { tools: self.inner.lock().await.tools.clone() }),
            Request::ToolResult { id, text } => {
                if let Some(waiting) = self.inner.lock().await.pending_tools.remove(&id) {
                    let _ = waiting.send(text);
                }
                Ok(Response::Ok)
            }
            Request::Tool { name, args } => {
                let (rx, log, id) = {
                    let mut inner = self.inner.lock().await;
                    let log = inner.turn.as_ref().filter(|t| t.alive).map(|t| t.log.clone())
                        .ok_or_else(|| anyhow!("no turn is running"))?;
                    inner.next_tool += 1;
                    let id = inner.next_tool;
                    let (tx, rx) = oneshot::channel();
                    inner.pending_tools.insert(id, tx);
                    (rx, log, id)
                };
                log.append(Entry::Tool { id, name, args }).await?;
                let text = rx.await.map_err(|_| anyhow!("the turn ended before the tool answered"))?;
                Ok(Response::ToolText { text })
            }
        }
    }

    async fn start(self: &Arc<Self>, work_item: String, argv: Vec<String>, env: std::collections::BTreeMap<String, String>,
                   cwd: String, stdin: Vec<serde_json::Value>, tools: Vec<ToolSpec>) -> Result<Response> {
        let mut inner = self.inner.lock().await;
        if inner.turn.as_ref().is_some_and(|t| t.alive) {
            return Err(anyhow!("a turn is already running"));
        }
        let path = self.dir.join(format!("{}.jsonl", holder_proto::component(&work_item)?));
        let log = Arc::new(Log::open(&path).await?);
        let (program, args) = argv.split_first().ok_or_else(|| anyhow!("empty argv"))?;
        let mut child = Command::new(program)
            .args(args)
            .envs(&env)
            .current_dir(&cwd)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .process_group(0)
            .kill_on_drop(false)
            .spawn()
            .with_context(|| format!("starting {program}"))?;
        let pid = child.id().ok_or_else(|| anyhow!("no pid"))?;
        log.append(Entry::Started { pid }).await?;
        let stdout = child.stdout.take().expect("piped");
        let reader = {
            let log = log.clone();
            tokio::spawn(async move {
                let mut out = BufReader::new(stdout);
                let mut buf = Vec::new();
                loop {
                    buf.clear();
                    match out.read_until(b'\n', &mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&buf).trim_end_matches(['\n', '\r']).to_string();
                            if !line.is_empty() {
                                let _ = log.append(Entry::Out { line }).await;
                            }
                        }
                    }
                }
            })
        };
        let mut turn = Turn { work_item: work_item.clone(), pid, alive: true, stdin: child.stdin.take(), log: log.clone() };
        for line in stdin {
            write_line(&mut turn, line, None).await?;
        }
        inner.turn = Some(turn);
        inner.tools = tools;
        let holder = self.clone();
        tokio::spawn(async move {
            let status = child.wait().await;
            let _ = reader.await;
            let (code, signal) = match status {
                Ok(s) => {
                    use std::os::unix::process::ExitStatusExt;
                    (s.code(), s.signal())
                }
                Err(_) => (None, None),
            };
            let _ = log.append(Entry::Exit { code, signal }).await;
            let mut inner = holder.inner.lock().await;
            if let Some(turn) = inner.turn.as_mut().filter(|t| t.work_item == work_item && t.pid == pid) {
                turn.alive = false;
                turn.stdin = None;
            }
            inner.pending_tools.clear();
        });
        Ok(Response::Started { pid })
    }

    async fn status(&self) -> Status {
        let inner = self.inner.lock().await;
        let turn = inner.turn.as_ref();
        let alive_pgid = turn.filter(|t| t.alive).map(|t| t.pid);
        Status {
            work_item: turn.map(|t| t.work_item.clone()),
            pid: turn.map(|t| t.pid),
            alive: alive_pgid.is_some(),
            listeners: listeners::owned_here(),
            other_processes: listeners::other_processes(std::process::id(), alive_pgid),
            log_seq: match turn {
                Some(t) => t.log.seq().await,
                None => 0,
            },
        }
    }
}

/// Log the line as `Sent`, then write it to Claude Code's input: the log always shows a line before its echo.
async fn write_line(turn: &mut Turn, line: serde_json::Value, tag: Option<String>) -> Result<()> {
    let stdin = turn.stdin.as_mut().ok_or_else(|| anyhow!("Claude Code's input is closed"))?;
    let mut bytes = serde_json::to_vec(&line)?;
    bytes.push(b'\n');
    turn.log.append(Entry::Sent { line, tag }).await?;
    stdin.write_all(&bytes).await?;
    stdin.flush().await?;
    Ok(())
}
