//! Everything that runs for the agent, for the dashboard's Containers page: each conversation's container with the
//! processes and servers in it, the agent's services (agent-services, from /var/agent/services.toml), the stacks the agent
//! runs in its own Docker (agent-docker, reached through `docker exec`, since its API listens only inside that
//! container), and the stack's own containers. Each can be inspected, its logs followed, stopped, and given an expiry
//! (see `keeps`); a conversation's container, agent-services and a stack's container can be opened in a shell.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration as StdDuration, Instant};

use anyhow::{Context, Result, anyhow, bail};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use axum::body::Bytes;
use chrono::{DateTime, Duration, Utc};
use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};

use crate::containers::{Containers, ContainerRow, Docker, HolderProbe, Probe, container_name};
use crate::host::HostStore;
use crate::keeps::{self, Keep};
use crate::shared::Shared;

const INSPECT: &str = include_str!("workloads_inspect.py");
/// The services file the agent edits, and where the services supervisor keeps each service's pid and output.
const SERVICES_FILE: &str = "services.toml";
const RUN_DIR: &str = "run";
const AGENT_UID: &str = "1001";
const SERVICES_FRESH: StdDuration = StdDuration::from_secs(5);
const FOLDER_FRESH: StdDuration = StdDuration::from_secs(600);
/// A follow (logs) ends after this long; the page reconnects.
const FOLLOW_S: &str = "1800";
const LOG_TAIL: &str = "300";

pub type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes>> + Send>>;

pub struct Settings {
    /// the agent's own Docker, whose network the conversations share: agent-docker.
    pub apps: String,
    /// The container that runs the agent's services: agent-services.
    pub services: String,
    /// The compose project of the stack's own containers.
    pub project: String,
}

pub struct Workloads {
    s: Arc<Shared>,
    docker: bollard::Docker,
    containers: Arc<Containers<Docker, HolderProbe>>,
    probe: HolderProbe,
    host: Arc<Mutex<HostStore>>,
    settings: Settings,
    stacks: tokio::sync::Mutex<Option<(Instant, Value)>>,
    services_ps: tokio::sync::Mutex<Option<(Instant, Value)>>,
    folders: Arc<Mutex<HashMap<String, (Instant, Option<u64>)>>>,
    /// When the page last asked: the stacks are read in the background only while someone is looking.
    watched: Mutex<Option<Instant>>,
}

/// A target a person can act on, from the page's request.
#[derive(Debug, Clone, PartialEq)]
pub enum Target {
    /// A conversation's container.
    Container(String),
    /// agent-services.
    Services,
    /// A container in the agent's own Docker, by name.
    App(String),
    /// One of the stack's own containers (agent-control, agent-docker, ...), by name: read only.
    System(String),
}

/// A name Docker or the services file would accept: letters, digits, `_.-`, not starting with a dot or dash.
pub fn plain_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c)) && name.len() <= 200
}

impl Target {
    pub fn parse(kind: &str, name: &str) -> Result<Target> {
        if !plain_name(name) && kind != "services" {
            bail!("not a name: {name:?}");
        }
        Ok(match kind {
            "container" => {
                container_name(name)?;
                Target::Container(name.to_string())
            }
            "services" => Target::Services,
            "app" => Target::App(name.to_string()),
            "system" => Target::System(name.to_string()),
            _ => bail!("no such kind: {kind}"),
        })
    }
}

/// Run a command in a container to its end and return what it printed (stdout and stderr together).
pub(crate) async fn exec_output(docker: &bollard::Docker, container: &str, user: Option<&str>, cmd: Vec<String>,
                                limit: StdDuration) -> Result<String> {
    let options = CreateExecOptions::<String> {
        attach_stdout: Some(true), attach_stderr: Some(true), cmd: Some(cmd), user: user.map(str::to_string),
        ..Default::default()
    };
    let id = docker.create_exec(container, options).await.with_context(|| format!("exec in {container}"))?.id;
    let started = docker.start_exec(&id, Some(StartExecOptions { detach: false, tty: false, output_capacity: Some(1 << 20) }))
        .await.with_context(|| format!("exec in {container}"))?;
    let StartExecResults::Attached { mut output, .. } = started else { bail!("exec detached") };
    let mut text = Vec::new();
    let read = async {
        while let Some(chunk) = output.next().await {
            text.extend_from_slice(&chunk?.into_bytes());
        }
        Ok::<_, anyhow::Error>(())
    };
    tokio::time::timeout(limit, read).await.map_err(|_| anyhow!("{container} didn't answer in {} s", limit.as_secs()))??;
    Ok(String::from_utf8_lossy(&text).into_owned())
}

impl Workloads {
    pub fn new(s: Arc<Shared>, containers: Arc<Containers<Docker, HolderProbe>>, host: Arc<Mutex<HostStore>>,
               settings: Settings) -> Result<Workloads> {
        Ok(Workloads { s, docker: bollard::Docker::connect_with_local_defaults()?, containers,
                       probe: HolderProbe { run_root: holder_proto::RUN_ROOT.into() }, host, settings,
                       stacks: Default::default(), services_ps: Default::default(), folders: Default::default(),
                       watched: Mutex::new(None) })
    }

    fn services_file(&self) -> PathBuf {
        self.s.paths.work.join(SERVICES_FILE)
    }

    // --- running commands in containers ------------------------------------------------------------------------

    async fn start_exec(&self, container: &str, user: Option<&str>, cmd: Vec<String>, tty: bool, stdin: bool,
                        workdir: Option<String>) -> Result<(String, StartExecResults)> {
        let options = CreateExecOptions::<String> {
            attach_stdout: Some(true), attach_stderr: Some(true), attach_stdin: Some(stdin), tty: Some(tty), cmd: Some(cmd),
            user: user.map(str::to_string), working_dir: workdir,
            env: tty.then(|| vec!["TERM=xterm-256color".to_string(), "COLORTERM=truecolor".to_string()]),
            ..Default::default()
        };
        let id = self.docker.create_exec(container, options).await.with_context(|| format!("exec in {container}"))?.id;
        let started = self.docker.start_exec(&id, Some(StartExecOptions { detach: false, tty, output_capacity: Some(1 << 20) }))
            .await.with_context(|| format!("exec in {container}"))?;
        Ok((id, started))
    }

    /// Run a command to its end and return what it printed (stdout and stderr together).
    async fn run(&self, container: &str, user: Option<&str>, cmd: Vec<String>, limit: StdDuration) -> Result<String> {
        exec_output(&self.docker, container, user, cmd, limit).await
    }

    /// The process inspector's answer, in a container from the agent image.
    async fn inspect(&self, container: &str, args: &[&str]) -> Result<Value> {
        let cmd = ["python3", "-c", INSPECT].iter().chain(args).map(|a| a.to_string()).collect();
        let text = self.run(container, Some(AGENT_UID), cmd, StdDuration::from_secs(20)).await?;
        let value: Value = serde_json::from_str(text.trim()).with_context(|| format!("the inspector in {container}: {}",
                                                                                     text.chars().take(300).collect::<String>()))?;
        if let Some(e) = value["error"].as_str() {
            bail!("{e}");
        }
        Ok(value)
    }

    /// `docker` in the agent's own Docker.
    async fn apps_docker(&self, args: &[&str]) -> Result<String> {
        let cmd = std::iter::once("docker").chain(args.iter().copied()).map(str::to_string).collect();
        self.run(&self.settings.apps, None, cmd, StdDuration::from_secs(30)).await
    }

    /// The container name `docker exec` takes for a target, and the user to run as there.
    fn exec_target(&self, t: &Target) -> Result<(String, Option<&'static str>)> {
        Ok(match t {
            Target::Container(c) => (container_name(c)?, Some(AGENT_UID)),
            Target::Services => (self.settings.services.clone(), Some(AGENT_UID)),
            Target::App(_) | Target::System(_) => bail!("not a container from the agent image"),
        })
    }

    // --- the overview ------------------------------------------------------------------------------------------

    /// Everything, for the page's list: the host's totals, the conversations' containers, the services, the stacks in
    /// the agent's Docker and the stack's own containers.
    pub async fn overview(&self, now: DateTime<Utc>) -> Result<Value> {
        let (host, rows, stats) = self.host_numbers();
        let keeps = keeps::all(&self.s.db.lock())?;
        let docker_state = self.docker_states().await;
        let mut conversations = Vec::new();
        for row in self.containers.rows().await? {
            let state = docker_state.get(&row.name).cloned();
            conversations.push(self.conversation_row(&row, state, &stats, &rows, &keeps, now).await);
        }
        conversations.sort_by(|a, b| sort_key(b).cmp(&sort_key(a)));
        *self.watched.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
        let services = self.services(&keeps).await;
        let (stacks, stacks_at) = match self.stacks(false).await {
            Ok((v, at)) => (v, Some(at)),
            Err(e) => (json!({"error": format!("{e:#}")}), None),
        };
        let system = self.system(&docker_state, &rows, &stats);
        let limits = self.containers.settings();
        Ok(json!({
            "now": keeps::fmt(now), "host": host,
            "rules": {"memory_bytes": limits.memory_gib << 30, "cpus": limits.cpus, "pids": limits.pids,
                      "idle_stop_minutes": limits.idle_stop_minutes, "max_age_hours": limits.max_age_hours,
                      "keep_stopped_days": limits.keep_stopped_days, "min_free_bytes": limits.min_free_gib << 30,
                      "max_sessions": self.s.config.limits.max_concurrent_sessions},
            "conversations": conversations, "services": services, "stacks": stacks,
            "stacks_at": stacks_at.map(keeps::fmt), "system": system,
        }))
    }

    /// The host sampler's newest numbers: the host's totals, its per-container rows by name, and the processes per
    /// consumer key.
    fn host_numbers(&self) -> (Value, HashMap<String, Value>, HashMap<String, Value>) {
        let store = self.host.lock().unwrap_or_else(|e| e.into_inner());
        let view = crate::host::host_view(&store, Utc::now().timestamp_millis() as f64 / 1000.0);
        let consumers = crate::host::consumers_view(&store, 0.0, |_, _| Value::Null);
        let rows = consumers["containers"].as_array().into_iter().flatten()
            .filter_map(|r| Some((r["name"].as_str()?.to_string(), r.clone()))).collect();
        let stats = consumers["consumers"].as_array().into_iter().flatten()
            .filter_map(|r| Some((r["key"].as_str()?.to_string(), r.clone()))).collect();
        let host = json!({"cpu": view["cpu"], "memory": view["memory"], "disk": view["disk_space"],
                          "stale": view["stale"], "age_s": view["age_s"]});
        (host, rows, stats)
    }

    /// Every container on the host Docker that agentd shows: name -> {state, status, created, image, labels}.
    async fn docker_states(&self) -> HashMap<String, Value> {
        let options = bollard::query_parameters::ListContainersOptions { all: true, ..Default::default() };
        let listed = self.docker.list_containers(Some(options)).await.unwrap_or_default();
        listed.into_iter().filter_map(|c| {
            let v = serde_json::to_value(&c).ok()?;
            let name = v["Names"][0].as_str()?.trim_start_matches('/').to_string();
            Some((name, json!({"state": v["State"], "status": v["Status"], "created": v["Created"], "image": v["Image"],
                               "labels": v["Labels"]})))
        }).collect()
    }

    async fn conversation_row(&self, row: &ContainerRow, state: Option<Value>, stats: &HashMap<String, Value>,
                              rows: &HashMap<String, Value>, keeps: &[Keep], now: DateTime<Utc>) -> Value {
        let running = state.as_ref().is_some_and(|s| s["state"] == "running");
        let status = if running { self.probe.status(&row.conversation).await } else { None };
        let own = keeps.iter().find(|k| k.kind == "container" && k.target == row.conversation);
        let servers: Vec<(u16, &Keep)> = keeps.iter().filter(|k| k.kind == "server").filter_map(|k| {
            let (c, p) = k.target.rsplit_once(':')?;
            (c == row.conversation).then_some((p.parse().ok()?, k))
        }).collect();
        let limits = self.containers.settings();
        let expires = container_expiry(row, running, status.as_ref(), own, &servers, limits, now);
        let numbers = rows.get(&row.name).cloned().unwrap_or(Value::Null);
        let consumer = stats.get(&format!("session:{}", row.conversation)).cloned().unwrap_or(Value::Null);
        let session = latest_session(&self.s, &row.conversation);
        json!({
            "conversation": row.conversation, "name": row.name,
            "state": state.as_ref().map_or(json!("missing"), |s| s["state"].clone()),
            "status": state.as_ref().map_or(Value::Null, |s| s["status"].clone()),
            "created_at": keeps::fmt(row.created_at), "last_turn_at": row.last_turn_at.map(keeps::fmt),
            "stopped_at": row.stopped_at.map(keeps::fmt),
            "turn_running": status.as_ref().is_some_and(|s| s.alive), "work_item": status.as_ref().and_then(|s| s.work_item.clone()),
            "listeners": status.as_ref().map_or(vec![], |s| s.listeners.clone()),
            "other_processes": status.as_ref().map(|s| s.other_processes),
            "cpu_percent": numbers["cpu_percent"], "mem_bytes": numbers["mem_bytes"], "mem_limit": limits.memory_gib << 30,
            "rx_bps": numbers["rx_bps"], "tx_bps": numbers["tx_bps"], "read_bps": numbers["read_bps"],
            "write_bps": numbers["write_bps"], "processes": consumer["processes"],
            "keep": own, "servers_kept": servers.iter().map(|(p, k)| json!({"port": p, "keep": k})).collect::<Vec<_>>(),
            "expires": expires, "session": session,
        })
    }

    fn system(&self, states: &HashMap<String, Value>, rows: &HashMap<String, Value>, stats: &HashMap<String, Value>) -> Vec<Value> {
        // a conversation's container carries the image's compose labels too: it is listed with the conversations
        let mut out: Vec<Value> = states.iter().filter(|(_, s)| s["labels"]["com.docker.compose.project"] == self.settings.project.as_str()
                                                       && s["labels"]["agent.conversation"].is_null())
            .map(|(name, s)| {
                let numbers = rows.get(name).cloned().unwrap_or(Value::Null);
                let processes = stats.values().find(|c| c["name"] == name.as_str()).map_or(Value::Null, |c| c["processes"].clone());
                json!({"name": name, "service": s["labels"]["com.docker.compose.service"], "state": s["state"],
                       "status": s["status"], "image": s["image"], "cpu_percent": numbers["cpu_percent"],
                       "mem_bytes": numbers["mem_bytes"], "processes": processes,
                       "shell": *name == self.settings.services,
                       "note": if *name == self.settings.apps { "The agent's own Docker: includes every stack below" } else { "" }})
            }).collect();
        out.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        out
    }

    // --- services ----------------------------------------------------------------------------------------------

    async fn services_ps(&self) -> Result<Value> {
        let mut cached = self.services_ps.lock().await;
        if let Some((at, v)) = cached.as_ref() && at.elapsed() < SERVICES_FRESH {
            return Ok(v.clone());
        }
        let v = self.inspect(&self.settings.services, &["ps", "0.3"]).await?;
        *cached = Some((Instant::now(), v.clone()));
        Ok(v)
    }

    async fn services(&self, keeps: &[Keep]) -> Value {
        let text = std::fs::read_to_string(self.services_file()).unwrap_or_default();
        let entries = service_entries(&text);
        let ps = self.services_ps().await;
        let procs = ps.as_ref().map(|v| v["processes"].as_array().cloned().unwrap_or_default()).unwrap_or_default();
        let run = self.s.paths.work.join(RUN_DIR);
        let rows: Vec<Value> = entries.iter().map(|e| {
            let pid: Option<u64> = std::fs::read_to_string(run.join(format!("{}.pid", e.name))).ok()
                .and_then(|p| p.trim().parse().ok());
            let tree = pid.map(|p| subtree(&procs, p)).unwrap_or_default();
            let alive = pid.is_some_and(|p| procs.iter().any(|x| x["pid"] == p));
            let out = run.join(format!("{}.out", e.name));
            let keep = keeps.iter().find(|k| k.kind == "service" && k.target == e.name);
            let expires = match keep {
                Some(k) => json!({"at": k.until.map(keeps::fmt), "why": k.until.map_or("kept until changed".into(),
                                  |_| format!("removed from the services file then (set by {})", k.set_by.as_deref().unwrap_or("someone")))}),
                None => json!({"at": null, "why": "runs until removed from the services file"}),
            };
            json!({"name": e.name, "cmd": self.s.scrub(&e.cmd.join(" ")), "cwd": e.cwd, "note": e.note,
                   "pid": pid, "running": alive,
                   "started": pid.and_then(|p| procs.iter().find(|x| x["pid"] == p)).map(|x| x["started"].clone()),
                   "cpu_percent": round1(tree.iter().map(|x| x["cpu_percent"].as_f64().unwrap_or(0.0)).sum()),
                   "mem_bytes": tree.iter().map(|x| x["pss"].as_u64().unwrap_or(0)).sum::<u64>(),
                   "ports": tree.iter().flat_map(|x| x["ports"].as_array().cloned().unwrap_or_default()).collect::<Vec<_>>(),
                   "processes": tree.len(), "log_bytes": std::fs::metadata(&out).map(|m| m.len()).ok(),
                   "keep": keep, "expires": expires})
        }).collect();
        json!({"file": self.services_file().display().to_string(), "items": rows,
               "error": ps.err().map(|e| format!("{e:#}")),
               "cgroup": Value::Null})
    }

    pub async fn service_detail(&self, name: &str) -> Result<Value> {
        let text = std::fs::read_to_string(self.services_file()).unwrap_or_default();
        let entry = service_entries(&text).into_iter().find(|e| e.name == name).ok_or_else(|| anyhow!("no service {name}"))?;
        let ps = self.inspect(&self.settings.services, &["ps", "0.5"]).await?;
        let procs = ps["processes"].as_array().cloned().unwrap_or_default();
        let pid: Option<u64> = std::fs::read_to_string(self.s.paths.work.join(RUN_DIR).join(format!("{name}.pid"))).ok()
            .and_then(|p| p.trim().parse().ok());
        let tree = pid.map(|p| subtree(&procs, p)).unwrap_or_default();
        Ok(json!({"name": name, "cmd": self.s.scrub(&entry.cmd.join(" ")), "cwd": entry.cwd, "note": entry.note,
                  "pid": pid, "processes": self.tag(tree, None)}))
    }

    // --- the agent's own Docker -----------------------------------------------------------------------------------

    /// Every 10 s while the page is open: the stacks read again in the background (`docker stats` takes seconds).
    pub async fn refresh_stacks(&self) -> Result<()> {
        let watched = self.watched.lock().unwrap_or_else(|e| e.into_inner()).is_some_and(|at| at.elapsed() < StdDuration::from_secs(120));
        if watched {
            self.stacks(true).await?;
        }
        Ok(())
    }

    /// The stacks in the agent's Docker, grouped by compose project (a container started on its own is its own group),
    /// with each container's numbers: the last reading (see `refresh_stacks`), or a new one when there is none or
    /// `fresh` asks for it.
    pub async fn stacks(&self, fresh: bool) -> Result<(Value, DateTime<Utc>)> {
        let mut cached = self.stacks.lock().await;
        if !fresh && let Some((at, v)) = cached.as_ref() {
            let age = Duration::from_std(at.elapsed()).unwrap_or_default();
            return Ok((v.clone(), Utc::now() - age));
        }
        let listed = self.apps_docker(&["ps", "-a", "--no-trunc", "--format", "{{json .}}"]).await?;
        let stats = self.apps_docker(&["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"]).await.unwrap_or_default();
        let keeps = keeps::all(&self.s.db.lock())?;
        let v = group_stacks(&listed, &stats, &self.s.paths.work.join("scratch"), &keeps, |convo| latest_session(&self.s, convo));
        *cached = Some((Instant::now(), v.clone()));
        Ok((v, Utc::now()))
    }

    /// The containers of one stack (or one container started on its own).
    async fn stack_containers(&self, stack: &str) -> Result<Vec<String>> {
        let (v, _) = self.stacks(true).await?;
        let found = v.as_array().into_iter().flatten().find(|s| s["name"] == stack)
            .ok_or_else(|| anyhow!("no stack {stack} in the agent's Docker"))?;
        Ok(found["containers"].as_array().into_iter().flatten().filter_map(|c| c["name"].as_str().map(str::to_string)).collect())
    }

    pub async fn app_detail(&self, name: &str) -> Result<Value> {
        let top = self.apps_docker(&["top", name, "-eo", "pid,ppid,pcpu,rss,etime,args"]).await.unwrap_or_default();
        let inspect: Value = serde_json::from_str(&self.apps_docker(&["inspect", "--size", name]).await?).unwrap_or(Value::Null);
        let i = &inspect[0];
        Ok(json!({
            "name": name, "image": i["Config"]["Image"], "created": i["Created"], "started_at": i["State"]["StartedAt"],
            "state": i["State"]["Status"], "restart_count": i["RestartCount"], "oom_killed": i["State"]["OOMKilled"],
            "exit_code": i["State"]["ExitCode"], "size_rw": i["SizeRw"], "size_root_fs": i["SizeRootFs"],
            "memory_limit": i["HostConfig"]["Memory"], "nano_cpus": i["HostConfig"]["NanoCpus"],
            "mounts": i["Mounts"].as_array().into_iter().flatten().map(|m| json!({"type": m["Type"], "source": m["Source"],
                "name": m["Name"], "destination": m["Destination"]})).collect::<Vec<_>>(),
            "processes": parse_top(&self.s.scrub(&top)),
        }))
    }

    // --- one conversation's container --------------------------------------------------------------------------

    pub async fn container_detail(&self, conversation: &str, now: DateTime<Utc>) -> Result<Value> {
        let row = self.containers.rows().await?.into_iter().find(|r| r.conversation == conversation)
            .ok_or_else(|| anyhow!("no container for {conversation}"))?;
        let (_, rows, stats) = self.host_numbers();
        let keeps = keeps::all(&self.s.db.lock())?;
        let state = self.docker_states().await.get(&row.name).cloned();
        let summary = self.conversation_row(&row, state.clone(), &stats, &rows, &keeps, now).await;
        let inspected = self.docker.inspect_container(&row.name,
            Some(bollard::query_parameters::InspectContainerOptionsBuilder::default().size(true).build())).await
            .ok().and_then(|i| serde_json::to_value(i).ok()).unwrap_or(Value::Null);
        let running = summary["state"] == "running";
        let ps = if running { self.inspect(&row.name, &["ps", "0.5"]).await.map_err(|e| format!("{e:#}")) } else { Err("stopped".into()) };
        let (processes, cgroup, ps_error) = match ps {
            Ok(v) => (self.tag(v["processes"].as_array().cloned().unwrap_or_default(), Some(&summary)), v["cgroup"].clone(), None),
            Err(e) => (vec![], Value::Null, Some(e)),
        };
        let folder = self.s.conversation_dir(conversation);
        let sessions = recent_sessions(&self.s, conversation, 15);
        Ok(json!({
            "summary": summary,
            "container": {"id": inspected["Id"].as_str().map(|i| i.chars().take(12).collect::<String>()),
                          "image": inspected["Config"]["Image"], "image_id": inspected["Image"].as_str().map(|i| i.trim_start_matches("sha256:").chars().take(12).collect::<String>()),
                          "created": inspected["Created"], "started_at": inspected["State"]["StartedAt"],
                          "finished_at": inspected["State"]["FinishedAt"], "oom_killed": inspected["State"]["OOMKilled"],
                          "exit_code": inspected["State"]["ExitCode"], "restart_count": inspected["RestartCount"],
                          "size_rw": inspected["SizeRw"], "size_root_fs": inspected["SizeRootFs"],
                          "memory_limit": inspected["HostConfig"]["Memory"], "nano_cpus": inspected["HostConfig"]["NanoCpus"],
                          "pids_limit": inspected["HostConfig"]["PidsLimit"]},
            "cgroup": cgroup, "processes": processes, "processes_error": ps_error,
            "folder": {"path": folder.display().to_string(), "bytes": self.folder_size(conversation, &folder)},
            "sessions": sessions,
        }))
    }

    /// Each process with what it is: the container's init, the holder, Claude Code (and what it runs during a turn),
    /// a server (listening on a port), or something left running. Its command is scrubbed of secrets.
    fn tag(&self, procs: Vec<Value>, summary: Option<&Value>) -> Vec<Value> {
        let claude: HashSet<u64> = procs.iter().filter(|p| is_claude(p)).filter_map(|p| p["pid"].as_u64()).collect();
        let parents: HashMap<u64, u64> = procs.iter().filter_map(|p| Some((p["pid"].as_u64()?, p["ppid"].as_u64()?))).collect();
        let in_turn = |pid: u64| {
            let mut p = pid;
            for _ in 0..64 {
                if claude.contains(&p) {
                    return true;
                }
                match parents.get(&p) {
                    Some(&pp) if pp > 1 => p = pp,
                    _ => return false,
                }
            }
            false
        };
        let kept: HashSet<u64> = summary.map(|s| s["servers_kept"].as_array().into_iter().flatten()
            .filter_map(|k| k["port"].as_u64()).collect()).unwrap_or_default();
        procs.into_iter().map(|mut p| {
            let pid = p["pid"].as_u64().unwrap_or(0);
            let cmd = p["cmd"].as_str().unwrap_or_default().to_string();
            let kind = if pid == 1 { "init" } else if cmd.contains("agent-holder serve") || cmd.contains("-m agent services") {
                "holder"
            } else if is_claude(&p) { "claude" } else if p["ports"].as_array().is_some_and(|a| !a.is_empty()) { "server" } else { "process" };
            p["kind"] = json!(kind);
            p["in_turn"] = json!(kind != "claude" && in_turn(pid));
            p["cmd"] = json!(self.s.scrub(&cmd));
            p["kept"] = json!(p["ports"].as_array().is_some_and(|a| a.iter().any(|x| x.as_u64().is_some_and(|x| kept.contains(&x)))));
            p
        }).collect()
    }

    /// The conversation's folder on the shared volume, measured in the background (it can hold big checkouts).
    fn folder_size(&self, conversation: &str, folder: &Path) -> Option<u64> {
        let mut cache = self.folders.lock().unwrap_or_else(|e| e.into_inner());
        let entry = cache.get(conversation).cloned();
        if entry.as_ref().is_none_or(|(at, _)| at.elapsed() > FOLDER_FRESH) {
            cache.insert(conversation.to_string(), (Instant::now(), entry.as_ref().and_then(|e| e.1)));
            let (folders, key, folder) = (self.folders.clone(), conversation.to_string(), folder.to_path_buf());
            tokio::task::spawn_blocking(move || {
                let size = du(&folder, Instant::now() + StdDuration::from_secs(60));
                folders.lock().unwrap_or_else(|e| e.into_inner()).insert(key, (Instant::now(), size));
            });
        }
        entry.and_then(|e| e.1)
    }

    // --- logs --------------------------------------------------------------------------------------------------

    /// A log followed as it grows: the container's own output, one process's log file, a service's output, or a
    /// container in the agent's Docker. Each chunk is scrubbed of secrets by the caller.
    pub async fn follow(&self, source: &str, target: &Target, pid: Option<u64>, service: Option<&str>) -> Result<ByteStream> {
        match (source, target) {
            ("container", Target::Container(c)) => Ok(self.docker_logs(&container_name(c)?)),
            ("container", Target::Services) => Ok(self.docker_logs(&self.settings.services)),
            ("container", Target::System(name)) => Ok(self.docker_logs(name)),
            ("container", Target::App(name)) => {
                let cmd = ["timeout", FOLLOW_S, "docker", "logs", "-f", "--tail", LOG_TAIL, name].map(str::to_string).to_vec();
                self.exec_stream(&self.settings.apps, None, cmd).await
            }
            ("process", t @ (Target::Container(_) | Target::Services)) => {
                let pid = pid.ok_or_else(|| anyhow!("which process?"))?;
                let (container, user) = self.exec_target(t)?;
                let ps = self.inspect(&container, &["ps", "0"]).await?;
                let p = ps["processes"].as_array().into_iter().flatten().find(|p| p["pid"] == pid)
                    .ok_or_else(|| anyhow!("process {pid} isn't running"))?;
                let path = p["log"].as_str().or(p["err"].as_str())
                    .ok_or_else(|| anyhow!("process {pid} doesn't write its output to a file"))?.to_string();
                let cmd = ["timeout", FOLLOW_S, "tail", "-n", LOG_TAIL, "-F", "--", &path].map(str::to_string).to_vec();
                self.exec_stream(&container, user, cmd).await
            }
            ("service", Target::Services) => {
                let name = service.filter(|n| plain_name(n)).ok_or_else(|| anyhow!("which service?"))?;
                let path = format!("/var/agent/{RUN_DIR}/{name}.out");
                let cmd = ["timeout", FOLLOW_S, "tail", "-n", LOG_TAIL, "-F", "--", &path].map(str::to_string).to_vec();
                self.exec_stream(&self.settings.services, Some(AGENT_UID), cmd).await
            }
            _ => bail!("no such log"),
        }
    }

    fn docker_logs(&self, name: &str) -> ByteStream {
        let options = bollard::query_parameters::LogsOptionsBuilder::default()
            .follow(true).stdout(true).stderr(true).tail(LOG_TAIL).build();
        Box::pin(self.docker.logs(name, Some(options)).map(|c| c.map(|o| o.into_bytes()).map_err(anyhow::Error::from)))
    }

    async fn exec_stream(&self, container: &str, user: Option<&str>, cmd: Vec<String>) -> Result<ByteStream> {
        let (_, started) = self.start_exec(container, user, cmd, false, false, None).await?;
        let StartExecResults::Attached { output, .. } = started else { bail!("exec detached") };
        Ok(Box::pin(output.map(|c| c.map(|o| o.into_bytes()).map_err(anyhow::Error::from))))
    }

    // --- a shell -----------------------------------------------------------------------------------------------

    /// An interactive shell: its exec id (for resizing), its output and its input.
    pub async fn shell(&self, target: &Target) -> Result<(String, ByteStream, Pin<Box<dyn tokio::io::AsyncWrite + Send>>)> {
        let login = "command -v bash >/dev/null && exec bash -l || exec sh -l";
        let (container, user, cmd, workdir) = match target {
            Target::Container(c) => {
                let folder = self.s.conversation_dir(c);
                (container_name(c)?, Some(AGENT_UID), vec!["sh".into(), "-c".into(), login.into()],
                 folder.is_dir().then(|| folder.display().to_string()))
            }
            Target::Services => (self.settings.services.clone(), Some(AGENT_UID), vec!["sh".into(), "-c".into(), login.into()],
                                 Some("/var/agent".into())),
            Target::App(name) => (self.settings.apps.clone(), None,
                                  ["docker", "exec", "-it", "-e", "TERM=xterm-256color", name, "sh", "-c",
                                   "command -v bash >/dev/null && exec bash || exec sh"].map(str::to_string).to_vec(), None),
            Target::System(_) => bail!("no shell in the stack's own containers here"),
        };
        let (id, started) = self.start_exec(&container, user, cmd, true, true, workdir).await?;
        let StartExecResults::Attached { output, input } = started else { bail!("exec detached") };
        Ok((id, Box::pin(output.map(|c| c.map(|o| o.into_bytes()).map_err(anyhow::Error::from))), input))
    }

    pub async fn resize(&self, exec_id: &str, cols: u16, rows: u16) -> Result<()> {
        self.docker.resize_exec(exec_id, bollard::exec::ResizeExecOptions { width: cols, height: rows }).await?;
        Ok(())
    }

    // --- actions -----------------------------------------------------------------------------------------------

    /// One action from the page, by `by`. Returns a short note of what was done.
    pub async fn act(&self, a: &Value, by: &str, now: DateTime<Utc>) -> Result<String> {
        let action = a["action"].as_str().unwrap_or_default();
        let kind = a["kind"].as_str().unwrap_or_default();
        let name = a["target"].as_str().unwrap_or_default();
        let until = || -> Result<Option<Option<DateTime<Utc>>>> {
            if a["clear"].as_bool() == Some(true) {
                return Ok(None);
            }
            if a["forever"].as_bool() == Some(true) {
                return Ok(Some(None));
            }
            let hours = a["hours"].as_f64().filter(|h| *h > 0.0 && *h <= 24.0 * 365.0)
                .ok_or_else(|| anyhow!("give hours (more than 0, at most a year), forever, or clear"))?;
            Ok(Some(Some(now + Duration::seconds((hours * 3600.0) as i64))))
        };
        let set = |kind: &str, target: &str, until: Option<Option<DateTime<Utc>>>| -> Result<String> {
            let c = self.s.db.lock();
            match until {
                None => {
                    keeps::clear(&c, kind, target)?;
                    Ok("back to the usual rules".into())
                }
                Some(u) => {
                    keeps::set(&c, kind, target, u, by, now)?;
                    Ok(u.map_or("kept until changed".into(), |t| format!("kept until {}", keeps::fmt(t))))
                }
            }
        };
        let done = match (kind, action) {
            ("container", "stop") => {
                self.containers.stop_by_hand(conversation(name)?, now).await?;
                "stopped".to_string()
            }
            ("container", "start") => {
                self.containers.start_by_hand(conversation(name)?, by, now).await?;
                "started (kept an hour unless kept longer)".into()
            }
            ("container", "remove") => {
                self.containers.remove_by_hand(conversation(name)?).await?;
                "deleted: the next message gets a new container".into()
            }
            ("container", "keep") => set("container", conversation(name)?, until()?)?,
            ("server", "keep") => {
                let port = port_of(a)?;
                set("server", &keeps::server_target(conversation(name)?, port), until()?)?
            }
            ("server", "stop") => {
                let port = port_of(a)?.to_string();
                let (container, _) = self.exec_target(&Target::parse("container", name)?)?;
                let v = self.inspect(&container, &["killport", &port, "TERM"]).await?;
                self.s.db.lock().execute("DELETE FROM agentd_keeps WHERE kind = 'server' AND target = ?",
                                         [keeps::server_target(name, port.parse()?)])?;
                format!("stopped {} process(es)", v["signalled"].as_array().map_or(0, Vec::len))
            }
            ("process", "kill") => {
                let pid = a["pid"].as_u64().ok_or_else(|| anyhow!("which process?"))?.to_string();
                let signal = match a["signal"].as_str().unwrap_or("TERM") { "KILL" => "KILL", _ => "TERM" };
                let t = Target::parse(if name == "agent-services" { "services" } else { "container" }, name)?;
                if let Target::Container(c) = &t && self.probe.status(c).await.is_some_and(|s| s.alive && s.pid.map(|p| p.to_string()) == Some(pid.clone())) {
                    bail!("that's the running turn's Claude Code: cancel the turn instead");
                }
                let (container, _) = self.exec_target(&t)?;
                let v = self.inspect(&container, &["kill", &pid, signal]).await?;
                format!("sent {signal} to {} process(es)", v["signalled"].as_array().map_or(0, Vec::len))
            }
            ("service", "remove") => {
                remove_service(&self.services_file(), name)?;
                self.s.db.lock().execute("DELETE FROM agentd_keeps WHERE kind = 'service' AND target = ?", [name])?;
                "removed from the services file: it stops within 30 s".into()
            }
            ("service", "restart") => {
                let pid = std::fs::read_to_string(self.s.paths.work.join(RUN_DIR).join(format!("{name}.pid")))
                    .ok().and_then(|p| p.trim().parse::<u64>().ok()).ok_or_else(|| anyhow!("{name} isn't running"))?;
                self.inspect(&self.settings.services, &["kill", &pid.to_string(), "TERM"]).await?;
                "stopped: the services supervisor starts it again in about 5 s".into()
            }
            ("service", "keep") => {
                if !service_entries(&std::fs::read_to_string(self.services_file()).unwrap_or_default()).iter().any(|e| e.name == name) {
                    bail!("no service {name}");
                }
                set("service", name, until()?)?
            }
            ("stack", "stop" | "start" | "restart" | "remove") => {
                let names = self.stack_containers(name).await?;
                if names.is_empty() {
                    bail!("{name} has no containers");
                }
                let verb = if action == "remove" { "rm" } else { action };
                let mut args = vec![verb];
                if action == "remove" {
                    args.push("-f");
                }
                args.extend(names.iter().map(String::as_str));
                let out = self.apps_docker(&args).await?;
                if action == "remove" {
                    self.s.db.lock().execute("DELETE FROM agentd_keeps WHERE kind = 'stack' AND target = ?", [name])?;
                }
                self.stacks(true).await.ok();
                format!("{action}: {}", out.split_whitespace().count())
            }
            ("stack", "keep") => set("stack", name, until()?)?,
            ("app", "stop" | "start" | "restart") => {
                if !plain_name(name) {
                    bail!("not a name: {name}");
                }
                self.apps_docker(&[action, name]).await?;
                self.stacks(true).await.ok();
                format!("{action}: {name}")
            }
            _ => bail!("can't {action} a {kind}"),
        };
        let event = json!({"kind": kind, "target": name, "action": action, "done": done,
                           "hours": a["hours"], "forever": a["forever"], "pid": a["pid"], "port": a["port"]});
        crate::db::append_event(&self.s.db.lock(), "workload_action", None, Some(by), Some(&event))?;
        Ok(done)
    }

    // --- the expiry sweep --------------------------------------------------------------------------------------

    /// Every minute: what has passed its expiry stops (a service, a stack, a kept server), a kept server whose
    /// process is gone lets its container go, and a container held only by its kept servers past its usual stop has
    /// everything else in it stopped. Containers' own expiries are the container sweep's.
    pub async fn sweep(&self, now: DateTime<Utc>) -> Result<()> {
        let all = keeps::all(&self.s.db.lock())?;
        for k in all.iter().filter(|k| !k.holds(now)) {
            let done = match k.kind.as_str() {
                "service" => remove_service(&self.services_file(), &k.target).map(|_| ()),
                "stack" => match self.stack_containers(&k.target).await {
                    Ok(names) if !names.is_empty() => {
                        let mut args = vec!["stop"];
                        args.extend(names.iter().map(String::as_str));
                        self.apps_docker(&args).await.map(drop)
                    }
                    _ => Ok(()),
                },
                "server" => match k.target.rsplit_once(':') {
                    Some((convo, port)) => match container_name(convo) {
                        Ok(name) => self.inspect(&name, &["killport", port, "TERM"]).await.map(drop).or(Ok(())),
                        Err(e) => Err(e),
                    },
                    None => Ok(()),
                },
                _ => continue,
            };
            match done {
                Ok(()) => {
                    keeps::clear(&self.s.db.lock(), &k.kind, &k.target)?;
                    let event = json!({"kind": k.kind, "target": k.target, "action": "expired", "set_by": k.set_by});
                    crate::db::append_event(&self.s.db.lock(), "workload_action", None, Some("agentd"), Some(&event))?;
                }
                Err(e) => tracing::warn!("{} {} expired, but stopping it failed: {e:#}", k.kind, k.target),
            }
        }
        // kept servers: gone (the process ended) or holding a container past its usual stop
        let limits = self.containers.settings().clone();
        let rows = self.containers.rows().await?;
        let mut by_convo: BTreeMap<String, Vec<u16>> = BTreeMap::new();
        for k in all.iter().filter(|k| k.kind == "server" && k.holds(now)) {
            if let Some((c, p)) = k.target.rsplit_once(':') && let Ok(p) = p.parse() {
                by_convo.entry(c.to_string()).or_default().push(p);
            }
        }
        for (convo, ports) in by_convo {
            let Some(row) = rows.iter().find(|r| r.conversation == convo) else { continue };
            let status = if row.stopped_at.is_none() { self.probe.status(&convo).await } else { None };
            let Some(status) = status else {
                // its container is stopped or gone: so are the servers
                for p in &ports {
                    keeps::clear(&self.s.db.lock(), "server", &keeps::server_target(&convo, *p))?;
                }
                continue;
            };
            let set_long_ago = |p: u16| all.iter().find(|k| k.kind == "server" && k.target == keeps::server_target(&convo, p))
                .is_some_and(|k| now - k.set_at > Duration::minutes(2));
            let gone: Vec<u16> = ports.iter().copied().filter(|p| !status.listeners.contains(p) && set_long_ago(*p)).collect();
            for p in &gone {
                keeps::clear(&self.s.db.lock(), "server", &keeps::server_target(&convo, *p))?;
            }
            let held: Vec<String> = ports.iter().filter(|p| !gone.contains(p)).map(u16::to_string).collect();
            let own_holds = keeps::get(&self.s.db.lock(), "container", &convo)?.is_some_and(|k| k.holds(now));
            let past = now - row.last_turn_at.unwrap_or(row.created_at) >= Duration::hours(limits.max_age_hours);
            if !held.is_empty() && !own_holds && past && !status.alive && status.other_processes > 0 {
                let mut args = vec!["trim"];
                args.extend(held.iter().map(String::as_str));
                if let Err(e) = self.inspect(&row.name, &args).await {
                    tracing::warn!(convo, "trimming around its kept servers: {e:#}");
                }
            }
        }
        Ok(())
    }
}

fn conversation(name: &str) -> Result<&str> {
    container_name(name)?;
    Ok(name)
}

fn port_of(a: &Value) -> Result<u16> {
    a["port"].as_u64().and_then(|p| u16::try_from(p).ok()).filter(|p| *p > 0).ok_or_else(|| anyhow!("which port?"))
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}

fn is_claude(p: &Value) -> bool {
    let cmd = p["cmd"].as_str().unwrap_or_default();
    p["comm"] == "claude" || cmd.starts_with("claude ") || cmd.contains("/claude --output-format")
}

/// The process and everything it started.
fn subtree(procs: &[Value], root: u64) -> Vec<Value> {
    let mut keep: HashSet<u64> = HashSet::from([root]);
    loop {
        let more: Vec<u64> = procs.iter().filter(|p| p["ppid"].as_u64().is_some_and(|pp| keep.contains(&pp)))
            .filter_map(|p| p["pid"].as_u64()).filter(|p| !keep.contains(p)).collect();
        if more.is_empty() {
            break;
        }
        keep.extend(more);
    }
    procs.iter().filter(|p| p["pid"].as_u64().is_some_and(|x| keep.contains(&x))).cloned().collect()
}

/// Running first, then by last activity.
fn sort_key(v: &Value) -> (bool, bool, String) {
    (v["turn_running"] == true, v["state"] == "running",
     v["last_turn_at"].as_str().or(v["created_at"].as_str()).unwrap_or_default().to_string())
}

/// When a conversation's container stops (or is deleted, once stopped), and why.
pub fn container_expiry(row: &ContainerRow, running: bool, status: Option<&holder_proto::Status>, own: Option<&Keep>,
                        servers: &[(u16, &Keep)], limits: &crate::config::Containers, now: DateTime<Utc>) -> Value {
    let by = |k: &Keep| k.set_by.clone().unwrap_or_else(|| "someone".into());
    if !running {
        let at = row.stopped_at.map(|t| t + Duration::days(limits.keep_stopped_days));
        return json!({"at": at.map(keeps::fmt), "why": format!("stopped; deleted {} days after it stopped", limits.keep_stopped_days),
                      "rule": "deleted"});
    }
    if status.is_some_and(|s| s.alive) {
        return json!({"at": null, "why": "a turn is running", "rule": "turn"});
    }
    if let Some(k) = own.filter(|k| k.holds(now)) {
        return match k.until {
            None => json!({"at": null, "why": format!("kept until changed, by {}", by(k)), "rule": "kept"}),
            Some(t) => json!({"at": keeps::fmt(t), "why": format!("kept by {}", by(k)), "rule": "kept"}),
        };
    }
    let holding: Vec<&(u16, &Keep)> = servers.iter().filter(|(_, k)| k.holds(now)).collect();
    if !holding.is_empty() {
        let ports = holding.iter().map(|(p, _)| p.to_string()).collect::<Vec<_>>().join(", ");
        let at = if holding.iter().any(|(_, k)| k.until.is_none()) { None } else { holding.iter().filter_map(|(_, k)| k.until).max() };
        return json!({"at": at.map(keeps::fmt), "why": format!("held by its kept server on {ports}"), "rule": "server"});
    }
    if own.is_some() {
        return json!({"at": keeps::fmt(now), "why": "its expiry has passed: stopping", "rule": "kept"});
    }
    let last = row.last_turn_at.unwrap_or(row.created_at);
    let max_age = last + Duration::hours(limits.max_age_hours);
    let busy = status.is_none_or(|s| !s.listeners.is_empty() || s.other_processes > 0);
    if busy {
        json!({"at": keeps::fmt(max_age), "why": format!("{} h after its last message (something still runs in it)", limits.max_age_hours),
               "rule": "max_age"})
    } else {
        let idle = (last + Duration::minutes(limits.idle_stop_minutes)).max(now);
        json!({"at": keeps::fmt(idle.min(max_age)), "why": format!("idle: {} minutes after its last message", limits.idle_stop_minutes),
               "rule": "idle"})
    }
}

/// The dashboard session a row links to: the conversation's newest turn.
fn latest_session(s: &Shared, conversation: &str) -> Value {
    recent_sessions(s, conversation, 1).into_iter().next().unwrap_or(Value::Null)
}

fn recent_sessions(s: &Shared, conversation: &str, n: i64) -> Vec<Value> {
    let c = s.db.lock();
    let ids: Vec<String> = c.prepare("SELECT id FROM work_items WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?")
        .and_then(|mut st| st.query_map(rusqlite::params![conversation, n], |r| r.get(0))?.collect())
        .unwrap_or_default();
    ids.iter().filter_map(|id| crate::dashboard::queries::get_session(&c, id).ok().flatten()).map(|r| json!({
        "id": r["id"], "title": r["title"], "state": r["state"], "requester": r["requester"], "source": r["source"],
        "project": r["project"], "created_at": r["created_at"], "ended_at": r["ended_at"], "links": r["links"],
    })).collect()
}

/// A folder's size, giving up (None) at `deadline`.
fn du(root: &Path, deadline: Instant) -> Option<u64> {
    let mut total = 0;
    let mut todo = vec![root.to_path_buf()];
    while let Some(dir) = todo.pop() {
        if Instant::now() > deadline {
            return None;
        }
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for e in entries.flatten() {
            let Ok(meta) = e.metadata() else { continue };
            if meta.is_dir() {
                todo.push(e.path());
            } else {
                use std::os::unix::fs::MetadataExt;
                total += meta.blocks() * 512;
            }
        }
    }
    Some(total)
}

// --- the services file ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct ServiceEntry {
    pub name: String,
    pub cmd: Vec<String>,
    pub cwd: Option<String>,
    /// The comment lines right above its `[[services]]` line.
    pub note: String,
    /// Its lines in the file, comment included: [start, end).
    lines: (usize, usize),
}

/// The services in the agent's services file, in order, with the comment above each.
pub fn service_entries(text: &str) -> Vec<ServiceEntry> {
    let lines: Vec<&str> = text.lines().collect();
    let headers: Vec<usize> = lines.iter().enumerate().filter(|(_, l)| l.trim() == "[[services]]").map(|(i, _)| i).collect();
    let mut out = Vec::new();
    for (n, &h) in headers.iter().enumerate() {
        let mut start = h;
        while start > 0 && lines[start - 1].trim_start().starts_with('#') {
            start -= 1;
        }
        let mut end = headers.get(n + 1).copied().unwrap_or(lines.len());
        // the next entry's comment belongs to it, and blank lines before it to neither
        while end > h + 1 && (lines[end - 1].trim_start().starts_with('#') || lines[end - 1].trim().is_empty()) {
            end -= 1;
        }
        let body = lines[h..end].join("\n");
        let Ok(parsed) = toml::from_str::<toml::Value>(&body.replacen("[[services]]", "", 1)) else { continue };
        let Some(name) = parsed.get("name").and_then(|v| v.as_str()) else { continue };
        let note = lines[start..h].iter().map(|l| l.trim_start().trim_start_matches('#').trim()).collect::<Vec<_>>().join(" ");
        out.push(ServiceEntry {
            name: name.to_string(),
            cmd: parsed.get("cmd").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
                .unwrap_or_default(),
            cwd: parsed.get("cwd").and_then(|v| v.as_str()).map(str::to_string),
            note, lines: (start, end),
        });
    }
    out
}

/// Take a service (and the comment above it) out of the services file; the supervisor stops it on its next read.
pub fn remove_service(path: &Path, name: &str) -> Result<()> {
    let text = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let entry = service_entries(&text).into_iter().find(|e| e.name == name).ok_or_else(|| anyhow!("no service {name}"))?;
    let lines: Vec<&str> = text.lines().collect();
    let mut kept: Vec<&str> = lines[..entry.lines.0].to_vec();
    let mut rest = &lines[entry.lines.1..];
    while kept.last().is_some_and(|l| l.trim().is_empty()) && rest.first().is_some_and(|l| l.trim().is_empty()) {
        rest = &rest[1..];
    }
    kept.extend_from_slice(rest);
    let mut out = kept.join("\n");
    if text.ends_with('\n') {
        out.push('\n');
    }
    toml::from_str::<toml::Value>(&out).context("the services file wouldn't parse without it")?;
    let tmp = path.with_extension("toml.tmp");
    std::fs::write(&tmp, out)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

// --- the agent's own Docker -------------------------------------------------------------------------------------------

/// Docker's human sizes ("1.2GiB", "300MB", "0B") in bytes.
pub fn parse_size(text: &str) -> u64 {
    let t = text.trim();
    let split = t.find(|c: char| c.is_ascii_alphabetic()).unwrap_or(t.len());
    let (n, unit) = t.split_at(split);
    let n: f64 = n.trim().parse().unwrap_or(0.0);
    let mult = match unit.trim() {
        "KiB" => 1024.0, "MiB" => 1024.0 * 1024.0, "GiB" => 1024.0 * 1024.0 * 1024.0, "TiB" => 1024f64.powi(4),
        "kB" | "KB" => 1e3, "MB" => 1e6, "GB" => 1e9, "TB" => 1e12,
        _ => 1.0,
    };
    (n * mult) as u64
}

/// "a / b" pairs (MemUsage, NetIO, BlockIO) in bytes.
fn pair(text: &str) -> (u64, u64) {
    let (a, b) = text.split_once('/').unwrap_or((text, ""));
    (parse_size(a), parse_size(b))
}

/// Docker's label list ("a=b,c=d").
fn labels(text: &str) -> HashMap<String, String> {
    text.split(',').filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

/// `docker ps` and `docker stats` (one JSON object per line) grouped into stacks, with each stack's thread (from the
/// compose file's folder under the conversations' scratch folder) and expiry.
pub fn group_stacks(listed: &str, stats: &str, scratch: &Path, keeps: &[Keep], session: impl Fn(&str) -> Value) -> Value {
    let stats: HashMap<String, Value> = stats.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter_map(|v| Some((v["Name"].as_str()?.to_string(), v))).collect();
    let mut stacks: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    let mut folders: HashMap<String, String> = HashMap::new();
    for v in listed.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok()) {
        let name = v["Names"].as_str().unwrap_or_default().to_string();
        let l = labels(v["Labels"].as_str().unwrap_or_default());
        let stack = l.get("com.docker.compose.project").cloned().unwrap_or_else(|| name.clone());
        if let Some(dir) = l.get("com.docker.compose.project.working_dir") {
            folders.entry(stack.clone()).or_insert_with(|| dir.clone());
        }
        let s = stats.get(&name).cloned().unwrap_or(Value::Null);
        let (mem, mem_limit) = pair(s["MemUsage"].as_str().unwrap_or_default());
        let (rx, tx) = pair(s["NetIO"].as_str().unwrap_or_default());
        let (read, write) = pair(s["BlockIO"].as_str().unwrap_or_default());
        let running = v["State"] == "running";
        stacks.entry(stack).or_default().push(json!({
            "name": name, "service": l.get("com.docker.compose.service"), "image": v["Image"], "state": v["State"],
            "status": v["Status"], "ports": v["Ports"], "created": v["CreatedAt"], "running_for": v["RunningFor"],
            "cpu_percent": if running { json!(s["CPUPerc"].as_str().unwrap_or("0").trim_end_matches('%').parse::<f64>().unwrap_or(0.0)) } else { json!(0.0) },
            "mem_bytes": if running { mem } else { 0 }, "mem_limit": mem_limit,
            "net_rx": rx, "net_tx": tx, "block_read": read, "block_write": write,
            "pids": s["PIDs"].as_str().and_then(|p| p.parse::<u64>().ok()),
        }));
    }
    let out: Vec<Value> = stacks.into_iter().map(|(name, containers)| {
        let folder = folders.get(&name).cloned();
        let conversation = folder.as_ref().and_then(|d| Path::new(d).strip_prefix(scratch).ok()?.components().next())
            .map(|c| c.as_os_str().to_string_lossy().to_string());
        let running = containers.iter().filter(|c| c["state"] == "running").count();
        let keep = keeps.iter().find(|k| k.kind == "stack" && k.target == name);
        let ports: Vec<u64> = containers.iter().flat_map(|c| published(c["ports"].as_str().unwrap_or_default())).collect();
        json!({
            "name": name, "folder": folder, "conversation": conversation,
            "session": conversation.as_deref().map(&session).unwrap_or(Value::Null),
            "running": running, "total": containers.len(),
            "cpu_percent": round1(containers.iter().map(|c| c["cpu_percent"].as_f64().unwrap_or(0.0)).sum()),
            "mem_bytes": containers.iter().map(|c| c["mem_bytes"].as_u64().unwrap_or(0)).sum::<u64>(),
            "ports": ports, "keep": keep,
            "expires": match keep {
                Some(k) => json!({"at": k.until.map(keeps::fmt), "why": format!("stops then (set by {})", k.set_by.as_deref().unwrap_or("someone"))}),
                None => json!({"at": null, "why": "runs until someone stops it"}),
            },
            "containers": containers,
        })
    }).collect();
    json!(out)
}

/// The host ports a container publishes, from `docker ps`'s Ports ("0.0.0.0:18096->8080/tcp, [::]:18096->8080/tcp").
fn published(ports: &str) -> Vec<u64> {
    let mut out: Vec<u64> = ports.split(',').filter_map(|p| p.split_once("->")?.0.rsplit(':').next()?.parse().ok()).collect();
    out.sort();
    out.dedup();
    out
}

/// `docker top ... -eo pid,ppid,pcpu,rss,etime,args` as rows.
fn parse_top(text: &str) -> Vec<Value> {
    text.lines().skip(1).filter_map(|l| {
        let f: Vec<&str> = l.split_whitespace().collect();
        if f.len() < 6 {
            return None;
        }
        Some(json!({"pid": f[0].parse::<u64>().ok(), "ppid": f[1].parse::<u64>().ok(), "cpu_percent": f[2].parse::<f64>().ok(),
                    "rss": f[3].parse::<u64>().ok().map(|k| k * 1024), "elapsed": f[4], "cmd": f[5..].join(" ")}))
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const FILE: &str = "# Processes the runner keeps running.\n[[services]]\nname = \"bridge\"\ncmd = [\"python3\", \"-I\", \"bridge.py\"]\n\
cwd = \"/var/agent/bridges/b\"\n\n\n# A preview for sam (2026-11-20). Remove after.\n[[services]]\nname = \"preview\"\n\
cmd = [\"node\", \"proxy.mjs\"]\ncwd = \"/var/agent/scratch/V-1/proxy\"\n\n[[services]]\nname = \"last\"\ncmd = [\"x\"]\n";

    #[test]
    fn services_are_read_with_the_comment_above_each() {
        let e = service_entries(FILE);
        assert_eq!(e.iter().map(|e| e.name.as_str()).collect::<Vec<_>>(), ["bridge", "preview", "last"]);
        assert_eq!(e[0].note, "Processes the runner keeps running.");
        assert_eq!(e[1].note, "A preview for sam (2026-11-20). Remove after.");
        assert_eq!(e[1].cmd, ["node", "proxy.mjs"]);
        assert_eq!(e[1].cwd.as_deref(), Some("/var/agent/scratch/V-1/proxy"));
        assert_eq!(e[2].note, "");
    }

    #[test]
    fn removing_a_service_takes_its_comment_and_leaves_the_rest() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("services.toml");
        std::fs::write(&path, FILE).unwrap();
        remove_service(&path, "preview").unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(!text.contains("preview") && !text.contains("Remove after"), "{text}");
        assert_eq!(service_entries(&text).iter().map(|e| e.name.clone()).collect::<Vec<_>>(), ["bridge", "last"]);
        assert!(text.starts_with("# Processes the runner keeps running.\n[[services]]\nname = \"bridge\""));
        remove_service(&path, "last").unwrap();
        remove_service(&path, "bridge").unwrap();
        assert!(service_entries(&std::fs::read_to_string(&path).unwrap()).is_empty());
        assert!(remove_service(&path, "bridge").is_err());
    }

    #[test]
    fn docker_sizes_and_ports_are_read() {
        assert_eq!(parse_size("1.5GiB"), 1_610_612_736);
        assert_eq!(parse_size("300MB"), 300_000_000);
        assert_eq!(parse_size("0B"), 0);
        assert_eq!(pair("100MiB / 31.1GiB").0, 104_857_600);
        assert_eq!(published("0.0.0.0:18096->8080/tcp, [::]:18096->8080/tcp, 5432/tcp"), [18096]);
    }

    #[test]
    fn stacks_group_by_project_and_find_their_thread() {
        let listed = [
            r#"{"Names":"rr-a-app-1","Labels":"com.docker.compose.project=rr-a,com.docker.compose.service=app,com.docker.compose.project.working_dir=/var/agent/scratch/V-1/rr","State":"running","Status":"Up 4 hours","Ports":"0.0.0.0:18096->8080/tcp","Image":"rr-app"}"#,
            r#"{"Names":"rr-a-db-1","Labels":"com.docker.compose.project=rr-a,com.docker.compose.service=db","State":"exited","Status":"Exited (0)","Ports":"","Image":"postgres"}"#,
            r#"{"Names":"lonely","Labels":"","State":"running","Status":"Up","Ports":"","Image":"redis"}"#,
        ].join("\n");
        let stats = r#"{"Name":"rr-a-app-1","CPUPerc":"2.50%","MemUsage":"512MiB / 30GiB","NetIO":"1kB / 2kB","BlockIO":"0B / 0B","PIDs":"12"}"#;
        let keep = Keep { kind: "stack".into(), target: "rr-a".into(), until: None, set_by: Some("sam".into()), set_at: Utc::now() };
        let v = group_stacks(&listed, stats, Path::new("/var/agent/scratch"), &[keep], |c| json!({"id": c}));
        let rr = &v[1];
        assert_eq!(v[0]["name"], "lonely");
        assert_eq!((rr["name"].as_str(), rr["running"].as_u64(), rr["total"].as_u64()), (Some("rr-a"), Some(1), Some(2)));
        assert_eq!(rr["conversation"], "V-1");
        assert_eq!(rr["session"]["id"], "V-1");
        assert_eq!(rr["mem_bytes"], 512 * 1024 * 1024);
        assert_eq!(rr["cpu_percent"], 2.5);
        assert_eq!(rr["ports"], json!([18096]));
        assert_eq!(rr["keep"]["set_by"], "sam");
    }

    fn row(last: DateTime<Utc>) -> ContainerRow {
        ContainerRow { conversation: "V-1".into(), name: "agent-conv-V-1".into(), created_at: last, last_turn_at: Some(last),
                       stopped_at: None }
    }

    #[test]
    fn a_containers_expiry_says_when_and_why() {
        let limits = crate::config::Containers { image: "i".into(), memory_gib: 8, cpus: 4.0, pids: 4096, idle_stop_minutes: 60,
                                                 max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 3 };
        let t0 = DateTime::parse_from_rfc3339("2026-11-24T00:00:00Z").unwrap().with_timezone(&Utc);
        let quiet = holder_proto::Status::default();
        let serving = holder_proto::Status { listeners: vec![18098], ..Default::default() };
        let busy = holder_proto::Status { alive: true, ..Default::default() };
        let r = row(t0);
        assert_eq!(container_expiry(&r, true, Some(&busy), None, &[], &limits, t0)["rule"], "turn");
        assert_eq!(container_expiry(&r, true, Some(&quiet), None, &[], &limits, t0)["at"], "2026-11-24T01:00:00Z");
        assert_eq!(container_expiry(&r, true, Some(&serving), None, &[], &limits, t0)["at"], "2026-11-25T00:00:00Z");
        let kept = Keep { kind: "container".into(), target: "V-1".into(), until: Some(t0 + Duration::hours(72)),
                          set_by: Some("sam".into()), set_at: t0 };
        let e = container_expiry(&r, true, Some(&serving), Some(&kept), &[], &limits, t0);
        assert_eq!((e["at"].as_str(), e["rule"].as_str()), (Some("2026-11-27T00:00:00Z"), Some("kept")));
        let server = Keep { kind: "server".into(), target: "V-1:18098".into(), until: None, set_by: None, set_at: t0 };
        let e = container_expiry(&r, true, Some(&serving), None, &[(18098, &server)], &limits, t0);
        assert_eq!((e["at"].clone(), e["rule"].as_str()), (Value::Null, Some("server")));
        let stopped = ContainerRow { stopped_at: Some(t0), ..row(t0) };
        assert_eq!(container_expiry(&stopped, false, None, None, &[], &limits, t0)["at"], "2026-12-01T00:00:00Z");
    }
}
