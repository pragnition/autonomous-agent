//! One container per conversation, made from the agent image. It starts with the conversation's first turn and
//! serves every later one. It is stopped an hour after its last turn when nothing is left running in it, and 24 hours
//! after its last turn whatever is running. A stopped container is started again for the same conversation within
//! 7 days, and deleted after that with its holder's logs; the next turn gets a new one from the newest image.

use std::collections::HashMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration as StdDuration;

use anyhow::{Context, Result, bail};
use bollard::models::{ContainerCreateBody, HostConfig};
use bollard::query_parameters::{CreateContainerOptions, RemoveContainerOptions, StopContainerOptions};
use chrono::{DateTime, Duration, NaiveDateTime, Utc};
use holder_proto::{Request, Response, Status};
use rusqlite::{OptionalExtension, params};
use tokio::sync::OwnedMutexGuard;

use crate::config;
use crate::db::Db;

/// How long a started container's holder has to answer on its socket.
const READY_WITHIN: StdDuration = StdDuration::from_secs(20);
/// How long Docker waits for a container it is stopping before killing it.
const STOP_GRACE_S: i32 = 10;
const TIME_FORMAT: &str = "%Y-%m-%dT%H:%M:%SZ";

/// What a conversation's container is made with.
#[derive(Clone, Debug, PartialEq)]
pub struct ContainerSpec {
    pub name: String,
    pub image: String,
    pub cmd: Vec<String>,
    pub labels: HashMap<String, String>,
    pub user: String,
    pub network_mode: String,
    pub memory_bytes: i64,
    pub nano_cpus: i64,
    pub pids: i64,
    pub binds: Vec<String>,
}

/// The Docker calls agentd makes: `Docker` for real, a fake in tests.
pub trait Engine: Send + Sync {
    /// `None` when there is no such container, else whether it is running.
    fn inspect(&self, name: &str) -> impl Future<Output = Result<Option<bool>>> + Send;
    fn create(&self, spec: &ContainerSpec) -> impl Future<Output = Result<()>> + Send;
    fn start(&self, name: &str) -> impl Future<Output = Result<()>> + Send;
    fn stop(&self, name: &str) -> impl Future<Output = Result<()>> + Send;
    fn remove(&self, name: &str) -> impl Future<Output = Result<()>> + Send;
}

/// Asks a conversation's holder what is running in its container: `HolderProbe` for real, a fake in tests.
pub trait Probe: Send + Sync {
    /// `None` when the holder doesn't answer.
    fn status(&self, conversation: &str) -> impl Future<Output = Option<Status>> + Send;
}

/// Where the containers' mounts come from.
pub struct Host {
    /// The data folder's path on the Docker host: bind mounts are resolved there, not in agentd's container.
    pub data_dir: String,
    /// The `agentwork` volume, mounted at /var/agent.
    pub work_volume: String,
    /// The container whose network every session container joins: Jensen's Docker daemon.
    pub network_container: String,
    pub uid: u32,
    /// The holders' run folders, as agentd sees them.
    pub run_root: PathBuf,
}

pub struct Containers<E, P> {
    engine: E,
    probe: P,
    db: Db,
    settings: config::Containers,
    host: Host,
    /// One lock per conversation. Never removed: a lock taken away while someone waits on it would let a third caller
    /// in beside them.
    locks: StdMutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
}

struct Row {
    created_at: DateTime<Utc>,
    last_turn_at: Option<DateTime<Utc>>,
    stopped_at: Option<DateTime<Utc>>,
    replace: bool,
}

impl<E: Engine, P: Probe> Containers<E, P> {
    pub fn new(engine: E, probe: P, db: Db, settings: config::Containers, host: Host) -> Self {
        Containers { engine, probe, db, settings, host, locks: StdMutex::default() }
    }

    pub fn spec(&self, conversation: &str) -> Result<ContainerSpec> {
        let data = &self.host.data_dir;
        Ok(ContainerSpec {
            name: container_name(conversation)?,
            image: self.settings.image.clone(),
            cmd: ["agent-holder", "serve", "--conversation", conversation].map(String::from).to_vec(),
            labels: HashMap::from([("agent.conversation".to_string(), conversation.to_string()),
                                   ("agent.kind".to_string(), "session".to_string())]),
            user: self.host.uid.to_string(),
            network_mode: format!("container:{}", self.host.network_container),
            memory_bytes: self.settings.memory_gib << 30,
            nano_cpus: (self.settings.cpus * 1e9).round() as i64,
            pids: self.settings.pids,
            // the agent service's mounts today: the Linear token and the apps' env files, the Azure login, the
            // schedules file, the task record, and the shared volume
            binds: vec![format!("{data}/runner:/run/agent:ro"), format!("{data}/cli/azure:/data/cli/azure"),
                        format!("{data}/schedules:/data/schedules"), format!("{data}/tasks:/data/tasks:ro"),
                        format!("{}:/var/agent", self.host.work_volume)],
        })
    }

    /// Make sure the conversation's container is running and its holder answering: start it if it is stopped, or
    /// create one if there is none, or if the one there was stopped 7 days ago or marked to be replaced. Nothing else
    /// touches the container while the returned guard is held: start the turn before dropping it.
    pub async fn ensure(&self, conversation: &str, now: DateTime<Utc>) -> Result<OwnedMutexGuard<()>> {
        let spec = self.spec(conversation)?;
        let held = self.lock(conversation).lock_owned().await;
        let row = self.row(conversation).await?;
        let mut running = self.engine.inspect(&spec.name).await?;
        let keep = Duration::days(self.settings.keep_stopped_days);
        if running == Some(false)
            && row.is_some_and(|r| r.replace || r.stopped_at.is_some_and(|t| now - t >= keep)) {
            self.engine.remove(&spec.name).await?;
            running = None;
        }
        // recorded before Docker is asked, so no container agentd makes goes unrecorded
        let (id, name, image, at, new) =
            (conversation.to_string(), spec.name.clone(), spec.image.clone(), fmt(now), running.is_none());
        self.db.call(move |c| {
            c.execute("INSERT INTO agentd_containers(conversation_id, name, image_id, created_at, last_turn_at) \
                       VALUES (?1, ?2, ?3, ?4, ?4) ON CONFLICT(conversation_id) DO UPDATE SET \
                       last_turn_at = ?4, stopped_at = NULL, \
                       created_at = CASE WHEN ?5 THEN ?4 ELSE created_at END, \
                       image_id = CASE WHEN ?5 THEN ?3 ELSE image_id END, \
                       replace = CASE WHEN ?5 THEN 0 ELSE replace END",
                      params![id, name, image, at, new])?;
            Ok(())
        }).await?;
        match running {
            None => {
                self.engine.create(&spec).await?;
                self.engine.start(&spec.name).await?;
            }
            Some(false) => self.engine.start(&spec.name).await?,
            Some(true) => {}
        }
        let deadline = tokio::time::Instant::now() + READY_WITHIN;
        while self.probe.status(conversation).await.is_none() {
            if tokio::time::Instant::now() >= deadline {
                bail!("the holder in {} didn't answer within {} s", spec.name, READY_WITHIN.as_secs());
            }
            tokio::time::sleep(StdDuration::from_millis(100)).await;
        }
        Ok(held)
    }

    /// Note that a turn ended: the idle and 24-hour stops count from here.
    pub async fn turn_ended(&self, conversation: &str, now: DateTime<Utc>) -> Result<()> {
        let (id, at) = (conversation.to_string(), fmt(now));
        self.db.call(move |c| {
            c.execute("UPDATE agentd_containers SET last_turn_at = ? WHERE conversation_id = ?", params![at, id])?;
            Ok(())
        }).await
    }

    /// Mark every container to be replaced from the newest image at its next start; running ones carry on as they are.
    pub async fn mark_for_replacement(&self) -> Result<()> {
        self.db.call(|c| {
            c.execute("UPDATE agentd_containers SET replace = 1", [])?;
            Ok(())
        }).await
    }

    /// Stop idle containers, delete those stopped for 7 days with their logs, and remove holder logs untouched for
    /// 7 days. Run every minute or so.
    pub async fn sweep(&self, now: DateTime<Utc>) -> Result<()> {
        let conversations = self.db.call(|c| {
            Ok(c.prepare("SELECT conversation_id FROM agentd_containers")?
                .query_map([], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?)
        }).await?;
        for conversation in conversations {
            if let Err(e) = self.sweep_one(&conversation, now).await {
                tracing::warn!(conversation, "container sweep: {e:#}");
            }
        }
        let root = self.host.run_root.clone();
        let cutoff = now - Duration::days(self.settings.keep_stopped_days);
        tokio::task::spawn_blocking(move || remove_old_logs(&root, cutoff)).await?
    }

    async fn sweep_one(&self, conversation: &str, now: DateTime<Utc>) -> Result<()> {
        let _held = self.lock(conversation).lock_owned().await;
        let Some(row) = self.row(conversation).await? else { return Ok(()) };
        let name = container_name(conversation)?;
        match self.engine.inspect(&name).await? {
            // removed outside agentd: the next turn makes a new one
            None => self.forget(conversation).await,
            Some(false) => match row.stopped_at {
                // stopped outside agentd: a crash, a reboot
                None => self.set_stopped(conversation, now).await,
                Some(at) if now - at >= Duration::days(self.settings.keep_stopped_days) => {
                    self.engine.remove(&name).await?;
                    self.forget(conversation).await?;
                    match std::fs::remove_dir_all(self.host.run_root.join(conversation)) {
                        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
                        _ => Ok(()),
                    }
                }
                Some(_) => Ok(()),
            },
            Some(true) => {
                let unused = now - row.last_turn_at.unwrap_or(row.created_at);
                let past_max_age = unused >= Duration::hours(self.settings.max_age_hours);
                let stop = match self.probe.status(conversation).await {
                    Some(s) if s.alive => false,
                    Some(s) => past_max_age || (unused >= Duration::minutes(self.settings.idle_stop_minutes)
                                                && s.listeners.is_empty() && s.other_processes == 0),
                    // the holder isn't answering: only the 24-hour stop
                    None => past_max_age,
                };
                if !stop {
                    return Ok(());
                }
                self.engine.stop(&name).await?;
                self.set_stopped(conversation, now).await
            }
        }
    }

    /// Stop one running container with no turn running, to make room for a turn waiting for memory: one with nothing
    /// left running before one with leftover processes, one serving on a port last, the longest unused first. `false`
    /// when there is none.
    pub async fn stop_one_idle(&self, except: &str, now: DateTime<Utc>) -> Result<bool> {
        let rows = self.db.call(|c| {
            Ok(c.prepare("SELECT conversation_id, COALESCE(last_turn_at, created_at) FROM agentd_containers \
                          WHERE stopped_at IS NULL")?
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?)
        }).await?;
        let mut idle = Vec::new();
        for (conversation, last_turn) in rows.into_iter().filter(|(c, _)| c != except) {
            if let Some(s) = self.probe.status(&conversation).await.filter(|s| !s.alive) {
                let rank = if !s.listeners.is_empty() { 2 } else if s.other_processes > 0 { 1 } else { 0 };
                idle.push((rank, last_turn, conversation));
            }
        }
        idle.sort();
        for (_, _, conversation) in idle {
            let _held = self.lock(&conversation).lock_owned().await;
            let name = container_name(&conversation)?;
            // checked again under the lock: a turn may have started since
            if self.probe.status(&conversation).await.is_some_and(|s| !s.alive)
                && self.engine.inspect(&name).await? == Some(true) {
                self.engine.stop(&name).await?;
                self.set_stopped(&conversation, now).await?;
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn lock(&self, conversation: &str) -> Arc<tokio::sync::Mutex<()>> {
        self.locks.lock().unwrap_or_else(|e| e.into_inner()).entry(conversation.to_string()).or_default().clone()
    }

    async fn row(&self, conversation: &str) -> Result<Option<Row>> {
        let id = conversation.to_string();
        self.db.call(move |c| {
            Ok(c.query_row("SELECT created_at, last_turn_at, stopped_at, replace FROM agentd_containers \
                            WHERE conversation_id = ?", [id], |r| {
                Ok(Row { created_at: parse(&r.get::<_, String>(0)?), last_turn_at: r.get::<_, Option<String>>(1)?.map(|t| parse(&t)),
                         stopped_at: r.get::<_, Option<String>>(2)?.map(|t| parse(&t)), replace: r.get(3)? })
            }).optional()?)
        }).await
    }

    async fn set_stopped(&self, conversation: &str, now: DateTime<Utc>) -> Result<()> {
        let (id, at) = (conversation.to_string(), fmt(now));
        self.db.call(move |c| {
            c.execute("UPDATE agentd_containers SET stopped_at = ? WHERE conversation_id = ?", params![at, id])?;
            Ok(())
        }).await
    }

    async fn forget(&self, conversation: &str) -> Result<()> {
        let id = conversation.to_string();
        self.db.call(move |c| {
            c.execute("DELETE FROM agentd_containers WHERE conversation_id = ?", [id])?;
            Ok(())
        }).await
    }
}

/// `agent-conv-<conversation>`; the conversation id must be a valid Docker name (core's are `V-…` and `A-…`).
fn container_name(conversation: &str) -> Result<String> {
    let mut chars = conversation.chars();
    let valid = chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c));
    if !valid {
        bail!("not a conversation id: {conversation:?}");
    }
    Ok(format!("agent-conv-{conversation}"))
}

fn fmt(t: DateTime<Utc>) -> String {
    t.format(TIME_FORMAT).to_string()
}

/// A time as core writes them; an unreadable one counts as long ago.
fn parse(s: &str) -> DateTime<Utc> {
    NaiveDateTime::parse_from_str(s, TIME_FORMAT).map(|t| t.and_utc()).unwrap_or(DateTime::UNIX_EPOCH)
}

/// Remove every holder log last written before `cutoff`.
fn remove_old_logs(root: &Path, cutoff: DateTime<Utc>) -> Result<()> {
    let dirs = match std::fs::read_dir(root) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        dirs => dirs?,
    };
    for dir in dirs {
        let Ok(files) = std::fs::read_dir(dir?.path()) else { continue };
        for file in files {
            let path = file?.path();
            let old = std::fs::metadata(&path).and_then(|m| m.modified())
                .is_ok_and(|t| DateTime::<Utc>::from(t) < cutoff);
            if old && path.extension().is_some_and(|e| e == "jsonl") {
                std::fs::remove_file(&path)?;
            }
        }
    }
    Ok(())
}

/// The Docker daemon, through its API.
pub struct Docker(bollard::Docker);

impl Docker {
    pub fn connect() -> Result<Docker> {
        Ok(Docker(bollard::Docker::connect_with_local_defaults()?))
    }
}

/// Docker's answer for a container that isn't there (404), or already in the state asked for (304).
fn code(e: &bollard::errors::Error) -> Option<u16> {
    match e {
        bollard::errors::Error::DockerResponseServerError { status_code, .. } => Some(*status_code),
        _ => None,
    }
}

impl Engine for Docker {
    async fn inspect(&self, name: &str) -> Result<Option<bool>> {
        match self.0.inspect_container(name, None).await {
            Ok(c) => Ok(Some(c.state.and_then(|s| s.running).unwrap_or(false))),
            Err(e) if code(&e) == Some(404) => Ok(None),
            Err(e) => Err(e).with_context(|| format!("inspecting {name}")),
        }
    }

    async fn create(&self, spec: &ContainerSpec) -> Result<()> {
        let options = CreateContainerOptions { name: Some(spec.name.clone()), ..Default::default() };
        let body = ContainerCreateBody {
            image: Some(spec.image.clone()),
            cmd: Some(spec.cmd.clone()),
            labels: Some(spec.labels.clone()),
            user: Some(spec.user.clone()),
            host_config: Some(HostConfig {
                init: Some(true),
                network_mode: Some(spec.network_mode.clone()),
                binds: Some(spec.binds.clone()),
                memory: Some(spec.memory_bytes),
                // no swap: past its cap, a container's biggest process is killed rather than the host slowed down
                memory_swap: Some(spec.memory_bytes),
                nano_cpus: Some(spec.nano_cpus),
                pids_limit: Some(spec.pids),
                ..Default::default()
            }),
            ..Default::default()
        };
        self.0.create_container(Some(options), body).await.with_context(|| format!("creating {}", spec.name))?;
        Ok(())
    }

    async fn start(&self, name: &str) -> Result<()> {
        match self.0.start_container(name, None).await {
            Err(e) if code(&e) != Some(304) => Err(e).with_context(|| format!("starting {name}")),
            _ => Ok(()),
        }
    }

    async fn stop(&self, name: &str) -> Result<()> {
        let options = StopContainerOptions { t: Some(STOP_GRACE_S), ..Default::default() };
        match self.0.stop_container(name, Some(options)).await {
            Err(e) if !matches!(code(&e), Some(304 | 404)) => Err(e).with_context(|| format!("stopping {name}")),
            _ => Ok(()),
        }
    }

    async fn remove(&self, name: &str) -> Result<()> {
        let options = RemoveContainerOptions { force: true, ..Default::default() };
        match self.0.remove_container(name, Some(options)).await {
            Err(e) if code(&e) != Some(404) => Err(e).with_context(|| format!("removing {name}")),
            _ => Ok(()),
        }
    }
}

/// Asks the holder on its socket in the conversation's run folder.
pub struct HolderProbe {
    pub run_root: PathBuf,
}

impl Probe for HolderProbe {
    async fn status(&self, conversation: &str) -> Option<Status> {
        let socket = self.run_root.join(holder_proto::component(conversation).ok()?).join("holder.sock");
        let ask = agent_holder::client::request(&socket, &Request::Status);
        match tokio::time::timeout(StdDuration::from_secs(5), ask).await {
            Ok(Ok(Response::Status(status))) => Some(status),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex as StdMutex;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use chrono::{Duration, Utc};

    #[derive(Default)]
    struct FakeEngine {
        containers: StdMutex<HashMap<String, (ContainerSpec, bool)>>,
        creates: AtomicUsize,
        removes: AtomicUsize,
    }

    impl Engine for FakeEngine {
        async fn inspect(&self, name: &str) -> anyhow::Result<Option<bool>> {
            Ok(self.containers.lock().unwrap().get(name).map(|(_, running)| *running))
        }
        async fn create(&self, spec: &ContainerSpec) -> anyhow::Result<()> {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            self.creates.fetch_add(1, Ordering::SeqCst);
            self.containers.lock().unwrap().insert(spec.name.clone(), (spec.clone(), false));
            Ok(())
        }
        async fn start(&self, name: &str) -> anyhow::Result<()> {
            self.containers.lock().unwrap().get_mut(name).unwrap().1 = true;
            Ok(())
        }
        async fn stop(&self, name: &str) -> anyhow::Result<()> {
            self.containers.lock().unwrap().get_mut(name).unwrap().1 = false;
            Ok(())
        }
        async fn remove(&self, name: &str) -> anyhow::Result<()> {
            self.removes.fetch_add(1, Ordering::SeqCst);
            self.containers.lock().unwrap().remove(name);
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeProbe(StdMutex<HashMap<String, holder_proto::Status>>);

    impl Probe for FakeProbe {
        async fn status(&self, conversation: &str) -> Option<holder_proto::Status> {
            Some(self.0.lock().unwrap().get(conversation).cloned().unwrap_or_default())
        }
    }

    fn settings() -> crate::config::Containers {
        crate::config::Containers { image: "agent-image".into(), memory_gib: 8, cpus: 4.0, pids: 4096,
                                    idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 3 }
    }

    fn setup(dir: &std::path::Path) -> Containers<FakeEngine, FakeProbe> {
        let db = crate::db::Db::open(&dir.join("agent.db")).unwrap();
        let host = Host { data_dir: "/srv/agent/data".into(), work_volume: "agentwork-vol".into(),
                          network_container: "agent-docker".into(), uid: 1001, run_root: dir.join("run") };
        Containers::new(FakeEngine::default(), FakeProbe::default(), db, settings(), host)
    }

    fn running(c: &Containers<FakeEngine, FakeProbe>, name: &str) -> Option<bool> {
        c.engine.containers.lock().unwrap().get(name).map(|(_, r)| *r)
    }

    #[test]
    fn spec_has_limits_network_mounts_and_labels() {
        let dir = tempfile::tempdir().unwrap();
        let spec = setup(dir.path()).spec("c1").unwrap();
        assert_eq!(spec.name, "agent-conv-c1");
        assert_eq!(spec.image, "agent-image");
        assert_eq!(spec.cmd, ["agent-holder", "serve", "--conversation", "c1"]);
        assert_eq!(spec.labels["agent.conversation"], "c1");
        assert_eq!(spec.labels["agent.kind"], "session");
        assert_eq!(spec.user, "1001");
        assert_eq!(spec.network_mode, "container:agent-docker");
        assert_eq!((spec.memory_bytes, spec.nano_cpus, spec.pids), (8 << 30, 4_000_000_000, 4096));
        assert_eq!(spec.binds, ["/srv/agent/data/runner:/run/agent:ro", "/srv/agent/data/cli/azure:/data/cli/azure",
                                "/srv/agent/data/schedules:/data/schedules", "/srv/agent/data/tasks:/data/tasks:ro",
                                "agentwork-vol:/var/agent"]);
        assert!(setup(dir.path()).spec("../x").is_err());
    }

    #[tokio::test]
    async fn ensure_creates_then_reuses_and_restarts() {
        let dir = tempfile::tempdir().unwrap();
        let c = setup(dir.path());
        let now = Utc::now();
        c.ensure("c1", now).await.unwrap();
        assert_eq!((c.engine.creates.load(Ordering::SeqCst), running(&c, "agent-conv-c1")), (1, Some(true)));
        c.ensure("c1", now).await.unwrap();
        assert_eq!(c.engine.creates.load(Ordering::SeqCst), 1);
        c.engine.stop("agent-conv-c1").await.unwrap();
        c.ensure("c1", now).await.unwrap();
        assert_eq!((c.engine.creates.load(Ordering::SeqCst), running(&c, "agent-conv-c1")), (1, Some(true)));
    }

    #[tokio::test]
    async fn ensure_is_serialised_per_conversation() {
        let dir = tempfile::tempdir().unwrap();
        let c = std::sync::Arc::new(setup(dir.path()));
        let now = Utc::now();
        let (a, b) = (c.clone(), c.clone());
        let (x, y) = tokio::join!(async { a.ensure("c1", now).await.map(drop) },
                                  async { b.ensure("c1", now).await.map(drop) });
        x.unwrap();
        y.unwrap();
        assert_eq!(c.engine.creates.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn week_old_or_marked_container_is_replaced_on_next_turn() {
        let dir = tempfile::tempdir().unwrap();
        let c = setup(dir.path());
        let now = Utc::now();
        c.ensure("c1", now).await.unwrap();
        c.sweep(now + Duration::hours(25)).await.unwrap(); // stopped: 24 hours after its last turn
        assert_eq!(running(&c, "agent-conv-c1"), Some(false));
        c.ensure("c1", now + Duration::days(9)).await.unwrap(); // stopped over 7 days: a fresh container
        assert_eq!((c.engine.removes.load(Ordering::SeqCst), c.engine.creates.load(Ordering::SeqCst)), (1, 2));
        c.mark_for_replacement().await.unwrap();
        c.engine.stop("agent-conv-c1").await.unwrap();
        c.ensure("c1", now + Duration::days(9)).await.unwrap();
        assert_eq!(c.engine.creates.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn sweep_stops_idle_keeps_listeners_and_deletes_week_old_containers_and_logs() {
        let dir = tempfile::tempdir().unwrap();
        let c = setup(dir.path());
        let t0 = Utc::now();
        for convo in ["idle", "serving"] {
            c.ensure(convo, t0).await.unwrap();
            c.turn_ended(convo, t0).await.unwrap();
        }
        let serving = holder_proto::Status { listeners: vec![5174], ..Default::default() };
        c.probe.0.lock().unwrap().insert("serving".into(), serving);
        c.sweep(t0 + Duration::minutes(30)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-idle"), Some(true), "idle under an hour: kept");
        c.sweep(t0 + Duration::hours(2)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-idle"), Some(false), "idle an hour: stopped");
        assert_eq!(running(&c, "agent-conv-serving"), Some(true), "a server is listening: kept");
        c.sweep(t0 + Duration::hours(25)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-serving"), Some(false), "24 hours after its last turn: stopped");
        std::fs::create_dir_all(dir.path().join("run/idle")).unwrap();
        std::fs::write(dir.path().join("run/idle/W-1.jsonl"), "{}\n").unwrap();
        c.sweep(t0 + Duration::days(7)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-idle"), Some(false), "stopped under 7 days: kept");
        c.sweep(t0 + Duration::days(7) + Duration::hours(3)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-idle"), None, "stopped 7 days: deleted");
        assert!(!dir.path().join("run/idle").exists(), "its holder logs too");
    }

    #[tokio::test]
    async fn sweep_never_stops_a_container_with_a_turn_running_and_removes_week_old_logs() {
        let dir = tempfile::tempdir().unwrap();
        let c = setup(dir.path());
        let t0 = Utc::now();
        c.ensure("busy", t0).await.unwrap();
        c.probe.0.lock().unwrap().insert("busy".into(), holder_proto::Status { alive: true, ..Default::default() });
        let logs = dir.path().join("run/busy");
        std::fs::create_dir_all(&logs).unwrap();
        std::fs::write(logs.join("W-new.jsonl"), "{}\n").unwrap();
        std::fs::File::create(logs.join("W-old.jsonl")).unwrap()
            .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(8 * 86400)).unwrap();
        c.sweep(t0 + Duration::hours(30)).await.unwrap();
        assert_eq!(running(&c, "agent-conv-busy"), Some(true));
        assert!(logs.join("W-new.jsonl").exists() && !logs.join("W-old.jsonl").exists());
    }

    #[tokio::test]
    async fn memory_pressure_stops_the_quietest_longest_unused_container() {
        let dir = tempfile::tempdir().unwrap();
        let c = setup(dir.path());
        let t0 = Utc::now();
        for (i, convo) in ["waiting", "busy", "serving", "leftover", "quiet-new", "quiet-old"].into_iter().enumerate() {
            let at = t0 - Duration::minutes(60 - i as i64);
            c.ensure(convo, at).await.unwrap();
            c.turn_ended(convo, if convo == "quiet-old" { t0 - Duration::hours(3) } else { at }).await.unwrap();
        }
        let status = |alive, listeners: Vec<u16>, other_processes| holder_proto::Status { alive, listeners, other_processes, ..Default::default() };
        c.probe.0.lock().unwrap().extend([("busy".to_string(), status(true, vec![], 0)),
                                          ("serving".into(), status(false, vec![5174], 0)),
                                          ("leftover".into(), status(false, vec![], 2))]);
        let order = ["quiet-old", "quiet-new", "leftover", "serving"];
        for name in order {
            assert!(c.stop_one_idle("waiting", t0).await.unwrap());
            assert_eq!(running(&c, &format!("agent-conv-{name}")), Some(false), "{name} next");
        }
        assert!(!c.stop_one_idle("waiting", t0).await.unwrap(), "a running turn and the waiting one's own: kept");
        assert_eq!((running(&c, "agent-conv-busy"), running(&c, "agent-conv-waiting")), (Some(true), Some(true)));
    }
}
