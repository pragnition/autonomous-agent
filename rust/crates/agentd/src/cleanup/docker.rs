//! Jensen's own Docker (agent-docker), where its test stacks run. Every pass records what it sees, and decides from
//! that history rather than from one snapshot:
//!
//! - a running stack is stopped only once it has been seen unused for `STACK_GRACE`: no conversation working in its
//!   folder in use or active, no service of the agent's in its folder, no session's or service's process naming it, no
//!   connection into its containers from outside the stack, and no traffic on their network since the last pass;
//! - a stopped container goes once its stack has sat unused for the idle period, unless it holds data of its own;
//! - an image goes once no container has used it for the idle period, and build cache older than that goes;
//! - a volume goes only when it is known to be rebuildable (installed dependencies, caches) and has sat unmounted for
//!   the idle period. One that holds a database is never deleted here: a Jensen turn asks people in the project's
//!   channel, and Jensen deletes only what a person says is safe. One that is neither is kept.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::{Component, Path};
use std::sync::LazyLock;

use anyhow::{Context, Result, anyhow};
use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use serde_json::Value;

use super::{Apps, Ask, Settings, State, append_note, modified, services_in_use, state};
use crate::db::Db;
use crate::keeps;

/// A stack is stopped only after it has been seen unused for this long.
pub const STACK_GRACE: Duration = Duration::hours(1);
/// Database questions go out at most this often.
const ASK_EVERY: Duration = Duration::hours(24);
/// Traffic on a stack's network below this between two passes is background noise (ARP, neighbour discovery).
const QUIET_BYTES: u64 = 16 * 1024;
/// A stopped container whose own writable layer holds more than this keeps it: that may be data.
const OWN_DATA: u64 = 100 * 1024 * 1024;
/// Volumes whose files are looked at per pass (reading them takes a while, so it is bounded).
const CONTENT_CHECKS: usize = 100;
/// Named after a database (named volumes only: an anonymous one's name is random hex).
static DB_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)postgres|pg|mysql|maria|mongo|redis|db|data|sql").unwrap());
/// A database's image.
static DB_IMAGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?i)postgres|postgis|timescale|mysql|mariadb|mongo|redis|valkey|elastic|opensearch|mssql|sqlserver|neo4j|rabbitmq|minio|clickhouse|cassandra|scylla|influx|couchdb|cockroach|surreal|qdrant|weaviate|milvus|meilisearch|typesense|etcd|zookeeper|kafka").unwrap());
/// Where a database keeps its files.
static DB_PATH: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?i)^/var/lib/(postgresql|mysql|mongodb|redis|neo4j|rabbitmq|clickhouse|elasticsearch|influxdb|cassandra)|^/data(/db)?/?$|^/var/opt/mssql|^/usr/share/(elasticsearch|opensearch)/data|^/bitnami|^/opt/couchdb/data").unwrap());
/// Named after installed dependencies. ("cache" alone isn't enough: a search index's volume can be called that.)
static REBUILD_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)node_modules|_nm$|-nm$|\.venv|venv$|pnpm-store").unwrap());
/// A file a database keeps, as `docker diff` lists it.
static DB_FILE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?i)(/PG_VERSION|/ibdata1|/WiredTiger[^/]*|/dump\.rdb|/appendonly[^/]*|\.sqlite3?|\.db|\.mdb|\.duckdb|/neostore[^/]*|/meta\.properties|/influxd\.bolt|/\.minio\.sys|/mnesia|/member/wal|/_state)$").unwrap());
/// Mounted where installed dependencies or caches go.
static REBUILD_PATH: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?i)(/node_modules|/\.venv|/venv|/\.cache|/cache|/\.next|/\.turbo|/\.pnpm-store|/__pycache__|/\.gradle)/?$").unwrap());

/// A container in the agent's Docker.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AppContainer {
    pub id: String,
    pub name: String,
    pub image: String,
    pub image_id: String,
    /// Running, restarting or paused.
    pub running: bool,
    /// Its main process in the agent's Docker (0 when not running).
    pub pid: u32,
    /// Started with `--rm`: stopping it deletes it, and its anonymous volumes with it.
    pub auto_remove: bool,
    /// A `docker exec` is running in it right now.
    pub exec_running: bool,
    /// Its compose project, else its own name.
    pub stack: String,
    /// The conversations' folders it runs from (compose folder) or mounts.
    pub folders: BTreeSet<String>,
    /// The volumes it mounts: name -> where.
    pub volumes: BTreeMap<String, String>,
    /// Its published ports.
    pub ports: BTreeSet<u16>,
    /// Its addresses on its networks.
    pub ips: BTreeSet<IpAddr>,
    /// When it started (running), stopped, or was made (never started).
    pub since: DateTime<Utc>,
}

/// A volume in the agent's Docker.
#[derive(Debug, Clone, PartialEq)]
pub struct AppVolume {
    pub name: String,
    pub project: Option<String>,
    pub created: DateTime<Utc>,
    pub mountpoint: String,
}

impl AppVolume {
    fn anonymous(&self) -> bool {
        self.name.len() == 64 && self.name.chars().all(|c| c.is_ascii_hexdigit())
    }
}

#[derive(Debug, Default, Serialize)]
pub struct DockerReport {
    pub stopped_stacks: Vec<String>,
    /// Unused stacks left running because stopping would delete them (`--rm`).
    pub kept_running: Vec<String>,
    pub removed_containers: Vec<String>,
    /// Stopped containers kept because they may hold data of their own.
    pub kept_containers: Vec<String>,
    pub removed_volumes: Vec<String>,
    /// Unused volumes kept because nothing says they can be rebuilt (and nothing says they are databases).
    pub kept_volumes: usize,
    /// Database volumes a Jensen turn was asked to check with people (or, in a dry run, would be).
    pub asked_about: Vec<String>,
    /// Database volumes waiting for the next daily question.
    pub to_ask_later: usize,
    pub removed_images: usize,
    pub build_cache: String,
    pub failures: Vec<String>,
}

fn time(v: &Value) -> Option<DateTime<Utc>> {
    let t = DateTime::parse_from_rfc3339(v.as_str()?).ok()?.with_timezone(&Utc);
    (t.timestamp() > 0).then_some(t)
}

fn folder_of(path: &str, scratch: &Path) -> Option<String> {
    match Path::new(path).strip_prefix(scratch).ok()?.components().next()? {
        Component::Normal(first) => Some(first.to_string_lossy().into_owned()),
        _ => None,
    }
}

// --- reading ---------------------------------------------------------------------------------------------------------

/// The containers from `docker inspect` of every container.
pub fn parse_containers(json: &str, scratch: &Path) -> Result<Vec<AppContainer>> {
    let all: Value = serde_json::from_str(json).context("reading docker inspect")?;
    let mut out = Vec::new();
    for c in all.as_array().ok_or_else(|| anyhow!("docker inspect didn't give a list"))? {
        let labels = &c["Config"]["Labels"];
        let name = c["Name"].as_str().unwrap_or_default().trim_start_matches('/').to_string();
        let status = c["State"]["Status"].as_str().unwrap_or_default();
        let running = matches!(status, "running" | "restarting" | "paused");
        let mut folders = BTreeSet::new();
        let mut volumes = BTreeMap::new();
        if let Some(dir) = labels["com.docker.compose.project.working_dir"].as_str() {
            folders.extend(folder_of(dir, scratch));
        }
        for m in c["Mounts"].as_array().into_iter().flatten() {
            match m["Type"].as_str() {
                Some("bind") => folders.extend(m["Source"].as_str().and_then(|s| folder_of(s, scratch))),
                Some("volume") => {
                    if let Some(n) = m["Name"].as_str() {
                        volumes.insert(n.to_string(), m["Destination"].as_str().unwrap_or_default().to_string());
                    }
                }
                _ => {}
            }
        }
        let ports = c["NetworkSettings"]["Ports"].as_object().into_iter().flatten()
            .flat_map(|(_, binds)| binds.as_array().into_iter().flatten())
            .filter_map(|b| b["HostPort"].as_str()?.parse().ok()).collect();
        let ips = c["NetworkSettings"]["Networks"].as_object().into_iter().flatten()
            .flat_map(|(_, n)| [n["IPAddress"].as_str(), n["GlobalIPv6Address"].as_str()])
            .filter_map(|ip| ip?.parse::<IpAddr>().ok()).map(|ip| ip.to_canonical()).collect();
        let since = if running { time(&c["State"]["StartedAt"]) } else { time(&c["State"]["FinishedAt"]) }
            .or_else(|| time(&c["Created"])).unwrap_or_else(Utc::now);
        out.push(AppContainer {
            id: c["Id"].as_str().unwrap_or_default().to_string(),
            image: c["Config"]["Image"].as_str().unwrap_or_default().to_string(),
            image_id: c["Image"].as_str().unwrap_or_default().to_string(),
            stack: labels["com.docker.compose.project"].as_str().filter(|p| !p.is_empty()).unwrap_or(&name).to_string(),
            pid: if running { c["State"]["Pid"].as_u64().unwrap_or(0) as u32 } else { 0 },
            auto_remove: c["HostConfig"]["AutoRemove"].as_bool().unwrap_or(false),
            exec_running: c["ExecIDs"].as_array().is_some_and(|e| !e.is_empty()),
            name, running, folders, volumes, ports, ips, since,
        });
    }
    Ok(out)
}

/// The volumes from `docker volume inspect` of every volume.
pub fn parse_volumes(json: &str) -> Result<Vec<AppVolume>> {
    let all: Value = serde_json::from_str(json).context("reading docker volume inspect")?;
    Ok(all.as_array().ok_or_else(|| anyhow!("docker volume inspect didn't give a list"))?.iter().map(|v| AppVolume {
        name: v["Name"].as_str().unwrap_or_default().to_string(),
        project: v["Labels"]["com.docker.compose.project"].as_str().map(str::to_string),
        // a time that can't be read counts as now: never as long ago
        created: time(&v["CreatedAt"]).unwrap_or_else(Utc::now),
        mountpoint: v["Mountpoint"].as_str().unwrap_or_default().to_string(),
    }).collect())
}

/// One running container's network as seen from inside it: bytes moved on its interfaces other than loopback, and
/// the far ends of its established TCP connections (loopback left out).
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Net {
    pub bytes: u64,
    pub peers: BTreeSet<IpAddr>,
}

/// `== <pid>` then that process's `/proc/<pid>/net/dev`, `tcp` and `tcp6`, for each pid.
pub fn parse_net(text: &str) -> HashMap<u32, Net> {
    let mut out: HashMap<u32, Net> = HashMap::new();
    let mut pid = None;
    for line in text.lines() {
        if let Some(p) = line.strip_prefix("== ") {
            pid = p.trim().parse().ok();
            continue;
        }
        let Some(pid) = pid else { continue };
        let Some((head, rest)) = line.trim().split_once(':') else { continue };
        let head = head.trim();
        let net = out.entry(pid).or_default();
        if !head.is_empty() && head.chars().all(|c| c.is_ascii_digit()) {
            // a TCP socket: "sl: local remote st ..."
            let f: Vec<&str> = rest.split_whitespace().collect();
            if f.len() > 2 && f[2] == "01"
                && let Some(ip) = hex_ip(f[1]).map(|ip| ip.to_canonical()).filter(|ip| !ip.is_loopback()) {
                net.peers.insert(ip);
            }
        } else if head != "lo" && !head.contains(' ') && !head.contains('|') {
            // an interface: "name: rx_bytes (7 more) tx_bytes ..."
            let n: Vec<u64> = rest.split_whitespace().filter_map(|x| x.parse().ok()).collect();
            if n.len() >= 9 {
                net.bytes += n[0] + n[8];
            }
        }
    }
    out
}

/// An address as `/proc/net/tcp*` writes it (`0100007F:1F90`): 32-bit words in little-endian order.
fn hex_ip(addr: &str) -> Option<IpAddr> {
    let (ip, _) = addr.split_once(':')?;
    if ip.len() % 8 != 0 {
        return None;
    }
    let words: Vec<u32> = (0..ip.len() / 8).map(|i| u32::from_str_radix(&ip[i * 8..i * 8 + 8], 16).ok()).collect::<Option<_>>()?;
    match words.as_slice() {
        [w] => Some(IpAddr::V4(Ipv4Addr::from(w.to_le_bytes()))),
        [a, b, c, d] => {
            let bytes: Vec<u8> = [a, b, c, d].iter().flat_map(|w| w.to_le_bytes()).collect();
            Some(IpAddr::V6(Ipv6Addr::from(<[u8; 16]>::try_from(bytes).ok()?)))
        }
        _ => None,
    }
}

/// Whether command lines (one process per line) name the stack, one of its containers, one of its folders or one of
/// its published ports. Claude Code's own command line (its system prompt mentions ports) doesn't count.
pub fn referenced(commands: &str, stack: &str, containers: &[&AppContainer], scratch: &Path) -> bool {
    let words: Vec<String> = std::iter::once(stack.to_string())
        .chain(containers.iter().map(|c| c.name.clone()))
        .chain(containers.iter().flat_map(|c| c.folders.iter().map(|f| format!("{}/{f}", scratch.display()))))
        .filter(|w| w.len() >= 4).collect();
    let ports: BTreeSet<u16> = containers.iter().flat_map(|c| c.ports.iter().copied()).collect();
    commands.lines()
        .filter(|l| !l.contains("--system-prompt") && !l.contains("--output-format stream-json"))
        .any(|l| words.iter().any(|w| l.contains(w.as_str())) || ports.iter().any(|p| mentions_number(l, *p)))
}

fn mentions_number(line: &str, n: u16) -> bool {
    let n = n.to_string();
    line.match_indices(&n).any(|(at, _)| {
        let before = line[..at].chars().next_back();
        let after = line[at + n.len()..].chars().next();
        !before.is_some_and(|c| c.is_ascii_digit()) && !after.is_some_and(|c| c.is_ascii_digit())
    })
}

// --- what kind of volume ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum Kind {
    /// Installed dependencies or a cache: removed once unused for the idle period.
    Rebuildable,
    /// Holds a database: people are asked.
    Database,
    /// Neither is known: kept.
    Unknown,
}

impl Kind {
    fn as_str(self) -> &'static str {
        match self {
            Kind::Rebuildable => "rebuildable",
            Kind::Database => "database",
            Kind::Unknown => "unknown",
        }
    }
    fn parse(s: &str) -> Option<Kind> {
        Some(match s {
            "rebuildable" => Kind::Rebuildable,
            "database" => Kind::Database,
            "unknown" => Kind::Unknown,
            _ => return None,
        })
    }
}

/// What a volume is, from its name, where it was mounted, the images that mounted it, and (once looked at) whether
/// its files are a database's. Any sign of a database wins; rebuildable needs a positive sign and none of those.
pub fn kind_of(v: &AppVolume, destinations: &[String], images: &[String], db_files: Option<bool>) -> Kind {
    let named = !v.anonymous();
    if db_files == Some(true) || destinations.iter().any(|d| DB_PATH.is_match(d)) || images.iter().any(|i| DB_IMAGE.is_match(i))
        || (named && DB_NAME.is_match(&v.name)) {
        return Kind::Database;
    }
    let by_path = !destinations.is_empty() && destinations.iter().all(|d| REBUILD_PATH.is_match(d));
    if (named && REBUILD_NAME.is_match(&v.name)) || by_path { Kind::Rebuildable } else { Kind::Unknown }
}

// --- what the passes remember ----------------------------------------------------------------------------------------

/// A stack as passes have seen it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StackSeen {
    pub last_used: Option<DateTime<Utc>>,
    /// Container id -> bytes on its network at the last pass.
    pub counters: BTreeMap<String, u64>,
}

/// A volume as passes have seen it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct VolumeSeen {
    pub unused_since: Option<DateTime<Utc>>,
    pub folders: BTreeSet<String>,
    pub stacks: BTreeSet<String>,
    pub destinations: BTreeSet<String>,
    pub images: BTreeSet<String>,
    pub kind: Option<Kind>,
    /// The creation time it was seen with: a new volume of the same name starts over.
    pub created: String,
}

fn json_set(s: Option<String>) -> BTreeSet<String> {
    s.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

fn parse_t(s: Option<String>) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(&s?).ok().map(|t| t.with_timezone(&Utc))
}

fn stacks_seen(c: &Connection) -> Result<HashMap<String, StackSeen>> {
    Ok(c.prepare("SELECT stack, last_used_at, counters FROM agentd_cleanup_stacks")?.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, StackSeen { last_used: parse_t(r.get(1)?),
            counters: r.get::<_, Option<String>>(2)?.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default() }))
    })?.collect::<rusqlite::Result<_>>()?)
}

fn volumes_seen(c: &Connection) -> Result<HashMap<String, VolumeSeen>> {
    Ok(c.prepare("SELECT volume, unused_since, folders, stacks, destinations, images, kind, created_at FROM agentd_cleanup_volumes")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, VolumeSeen {
            unused_since: parse_t(r.get(1)?), folders: json_set(r.get(2)?), stacks: json_set(r.get(3)?),
            destinations: json_set(r.get(4)?), images: json_set(r.get(5)?),
            kind: r.get::<_, Option<String>>(6)?.as_deref().and_then(Kind::parse), created: r.get(7)?,
        })))?.collect::<rusqlite::Result<_>>()?)
}

fn images_seen(c: &Connection) -> Result<HashMap<String, DateTime<Utc>>> {
    Ok(c.prepare("SELECT image, last_used_at FROM agentd_cleanup_images")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)))?
        .filter_map(|r| r.ok().and_then(|(i, t)| Some((i, parse_t(t)?)))).collect())
}

fn save_stack(c: &Connection, stack: &str, seen: &StackSeen) -> Result<()> {
    c.execute("INSERT INTO agentd_cleanup_stacks(stack, last_used_at, counters) VALUES (?1, ?2, ?3) ON CONFLICT(stack) \
               DO UPDATE SET last_used_at = ?2, counters = ?3",
              params![stack, seen.last_used.map(keeps::fmt), serde_json::to_string(&seen.counters)?])?;
    Ok(())
}

fn save_volume(c: &Connection, name: &str, seen: &VolumeSeen) -> Result<()> {
    let j = |s: &BTreeSet<String>| serde_json::to_string(s).unwrap_or_default();
    c.execute("INSERT INTO agentd_cleanup_volumes(volume, created_at, unused_since, folders, stacks, destinations, images, kind) \
               VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8) ON CONFLICT(volume) DO UPDATE SET created_at = ?2, unused_since = ?3, \
               folders = ?4, stacks = ?5, destinations = ?6, images = ?7, kind = ?8",
              params![name, seen.created, seen.unused_since.map(keeps::fmt), j(&seen.folders), j(&seen.stacks),
                      j(&seen.destinations), j(&seen.images), seen.kind.map(Kind::as_str)])?;
    Ok(())
}

// --- the step --------------------------------------------------------------------------------------------------------

/// How a set of conversation folders stands: (in use: any busy or active within `STACK_GRACE`; quiet: all quiet for
/// the idle period). No folders: neither in use nor anything but quiet.
fn folders_stand(db: &Db, scratch: &Path, folders: &BTreeSet<String>, now: DateTime<Utc>, idle: Duration) -> Result<(bool, bool)> {
    let (mut in_use, mut quiet) = (false, true);
    for folder in folders {
        let path = scratch.join(folder);
        if !path.is_dir() {
            continue; // a folder that's gone holds nothing to protect
        }
        let touched = modified(&path); // read before the DB is locked: the disk can be slow
        let c = db.lock();
        in_use |= !matches!(state(&c, folder, &path, now, STACK_GRACE, touched)?, State::Quiet(_));
        quiet &= matches!(state(&c, folder, &path, now, idle, touched)?, State::Quiet(_));
    }
    Ok((in_use, quiet))
}

/// `docker <args>`, its failure kept in the report instead of ending the step.
async fn act(apps: &dyn Apps, args: Vec<String>, report: &mut DockerReport) -> bool {
    match apps.docker(args.clone()).await {
        Ok(_) => true,
        Err(e) => {
            report.failures.push(format!("docker {}: {e:#}", args.iter().take(3).cloned().collect::<Vec<_>>().join(" ")));
            false
        }
    }
}

fn folders_of(members: &[&AppContainer]) -> BTreeSet<String> {
    members.iter().flat_map(|c| c.folders.iter().cloned()).collect()
}

/// One Docker step: read, remember, decide, act. A dry run only reports, and remembers nothing either.
pub async fn step(db: &Db, apps: &dyn Apps, ask: Option<&dyn Ask>, st: &Settings, services: &str, now: DateTime<Utc>,
                  dry_run: bool) -> Result<DockerReport> {
    let scratch = st.scratch();
    let mut report = DockerReport::default();

    // what is there
    let ids: Vec<String> = apps.docker(vec!["ps".into(), "-aq".into(), "--no-trunc".into()]).await?
        .split_whitespace().map(str::to_string).collect();
    let containers = if ids.is_empty() { Vec::new() } else { parse_containers(&apps.inspect(ids).await?, &scratch)? };
    let names: Vec<String> = apps.docker(vec!["volume".into(), "ls".into(), "-q".into()]).await?
        .split_whitespace().map(str::to_string).collect();
    let volumes = if names.is_empty() { Vec::new() } else { parse_volumes(&apps.volume_inspect(names).await?)? };
    let mut image_refs: BTreeMap<String, Vec<String>> = BTreeMap::new();
    // `-a`: with the containerd image store an image left untagged by a rebuild only shows with it
    let listed = apps.docker(vec!["images".into(), "-a".into(), "--no-trunc".into(), "--format".into(), "{{.ID}}\t{{.Repository}}:{{.Tag}}".into()]).await?;
    for line in listed.lines() {
        if let Some((id, reference)) = line.split_once('\t') {
            let refs = image_refs.entry(id.to_string()).or_default();
            if !reference.contains("<none>") {
                refs.push(reference.to_string());
            }
        }
    }

    // who uses what: sessions' and services' processes, the services' folders, and the stacks' own network
    let sessions: Vec<String> = db.call(|c| Ok(c.prepare("SELECT conversation_id FROM agentd_containers WHERE stopped_at IS NULL")?
        .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)).await?;
    let mut commands = apps.processes(None).await.context("reading the agent's services' processes")?;
    for convo in &sessions {
        // a session whose processes can't be read might use anything: stop nothing this time
        commands += &apps.processes(Some(convo.as_str())).await.with_context(|| format!("reading the processes of {convo}'s session"))?;
    }
    let service_folders = services_in_use(services, &scratch);
    // a stack with no conversation folder can't be tied to a session: it counts as used while any session is up or
    // any turn is queued or running
    let anything_going = !sessions.is_empty() || db.call(|c| Ok(c.query_row(
        "SELECT EXISTS(SELECT 1 FROM work_items WHERE status IN ('queued', 'claimed', 'delivered'))", [], |r| r.get::<_, bool>(0))?)).await?;
    let pids: Vec<u32> = containers.iter().filter(|c| c.pid > 0).map(|c| c.pid).collect();
    let net = if pids.is_empty() { HashMap::new() } else { parse_net(&apps.network(pids).await?) };
    let kept: HashSet<String> = keeps::all(&db.lock())?.into_iter().filter(|k| k.kind == "stack" && k.holds(now))
        .map(|k| k.target).collect();

    let mut stacks: BTreeMap<&str, Vec<&AppContainer>> = BTreeMap::new();
    for c in &containers {
        stacks.entry(c.stack.as_str()).or_default().push(c);
    }
    let held = |stack: &str, members: &[&AppContainer]| kept.contains(stack) || members.iter().any(|c| kept.contains(&c.name))
        || folders_of(members).iter().any(|f| service_folders.contains(f));
    let (mut seen_stacks, mut seen_volumes, seen_images) = {
        let c = db.lock();
        (stacks_seen(&c)?, volumes_seen(&c)?, images_seen(&c)?)
    };

    // running stacks: used now, or stopped once seen unused for STACK_GRACE
    let mut stop: Vec<(String, Vec<&AppContainer>)> = Vec::new();
    for (&stack, members) in &stacks {
        let running: Vec<&AppContainer> = members.iter().copied().filter(|c| c.running).collect();
        if running.is_empty() {
            continue;
        }
        let (folder_in_use, _) = folders_stand(db, &scratch, &folders_of(members), now, st.idle)?;
        let ips: BTreeSet<IpAddr> = members.iter().flat_map(|c| c.ips.iter().copied()).collect();
        let before = seen_stacks.get(stack).cloned().unwrap_or_default();
        let (mut counters, mut traffic, mut outside) = (BTreeMap::new(), false, false);
        for c in &running {
            let n = net.get(&c.pid);
            outside |= n.is_some_and(|n| n.peers.iter().any(|p| !ips.contains(p)));
            match (n.map(|n| n.bytes), before.counters.get(&c.id)) {
                (Some(bytes), Some(&was)) => {
                    traffic |= bytes < was || bytes - was >= QUIET_BYTES;
                    counters.insert(c.id.clone(), bytes);
                }
                // first sight of this container: counts as used, and the count starts here
                (Some(bytes), None) => {
                    traffic = true;
                    counters.insert(c.id.clone(), bytes);
                }
                // its network couldn't be read: counts as used
                (None, _) => traffic = true,
            }
        }
        let used = folder_in_use || held(stack, members) || referenced(&commands, stack, members, &scratch) || outside || traffic
            || running.iter().any(|c| now - c.since < STACK_GRACE || c.exec_running)
            || (folders_of(members).is_empty() && anything_going);
        let last_used = if used { now } else { before.last_used.unwrap_or(now) };
        if !used && now - last_used >= STACK_GRACE {
            if running.iter().any(|c| c.auto_remove) {
                report.kept_running.push(stack.to_string()); // stopping would delete it and its anonymous volumes
            } else {
                stop.push((stack.to_string(), running));
            }
        }
        seen_stacks.insert(stack.to_string(), StackSeen { last_used: Some(last_used), counters });
    }
    // a stack seen now without running containers keeps the last time it was used
    for (&stack, _) in stacks.iter().filter(|(_, m)| m.iter().all(|c| !c.running)) {
        seen_stacks.entry(stack.to_string()).or_insert(StackSeen { last_used: None, counters: BTreeMap::new() });
    }
    if !dry_run {
        let c = db.lock();
        for (stack, seen) in seen_stacks.iter().filter(|(s, _)| stacks.contains_key(s.as_str())) {
            save_stack(&c, stack, seen)?;
        }
        c.execute("DELETE FROM agentd_cleanup_stacks WHERE stack NOT IN (SELECT value FROM json_each(?))",
                  [serde_json::to_string(&stacks.keys().collect::<Vec<_>>())?])?;
    }
    for (stack, running) in &stop {
        let folders = folders_of(&stacks[stack.as_str()]);
        // asked again right before it stops
        match folders_stand(db, &scratch, &folders, now, st.idle) {
            Ok((false, _)) => {}
            Ok(_) => continue,
            Err(e) => {
                report.failures.push(format!("checking {stack} again: {e:#}"));
                continue;
            }
        }
        if !dry_run {
            if !act(apps, [vec!["stop".to_string()], running.iter().map(|c| c.id.clone()).collect()].concat(), &mut report).await {
                continue;
            }
            let names: Vec<String> = running.iter().map(|c| c.name.clone()).collect();
            for folder in &folders {
                note(&scratch.join(folder), now, &format!(
                    "Stopped the stack `{stack}` in the agent's Docker: nothing had used it for over an hour. `docker start {}` \
                     starts it again (reinstall the folder's dependencies first if they were cleaned up since).", names.join(" ")),
                    &mut report);
            }
        }
        report.stopped_stacks.push(stack.clone());
    }
    let stopped_now: HashSet<&str> = report.stopped_stacks.iter().map(String::as_str).collect();

    // stopped containers: gone once their stack has sat unused for the idle period, unless they may hold data of their own
    let mut candidates: Vec<&AppContainer> = Vec::new();
    for (&stack, members) in &stacks {
        if members.iter().any(|c| c.running) || stopped_now.contains(stack) || held(stack, members) {
            continue;
        }
        let Ok((in_use, quiet)) = folders_stand(db, &scratch, &folders_of(members), now, st.idle) else { continue };
        let recently = seen_stacks.get(stack).and_then(|s| s.last_used).is_some_and(|t| now - t < st.idle);
        if !in_use && quiet && !recently {
            candidates.extend(members.iter().copied().filter(|c| now - c.since >= st.idle));
        }
    }
    let ids: Vec<String> = candidates.iter().map(|c| c.id.clone()).collect();
    let (sizes, files) = if ids.is_empty() { (HashMap::new(), HashMap::new()) } else {
        match (apps.writable_sizes(ids.clone()).await, apps.changed_files(ids).await) {
            (Ok(s), Ok(f)) => (s, f),
            (Err(e), _) | (_, Err(e)) => {
                report.failures.push(format!("reading containers' own files: {e:#}"));
                (HashMap::new(), HashMap::new()) // then none is removed
            }
        }
    };
    let mut removed_ids = HashSet::new();
    let mut by_folder: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for c in candidates {
        // a database's files or data folder in its own layer (Elasticsearch and MSSQL declare no volume), or a database
        // image with no volume at all: its data may be in there
        let own_database = files.get(&c.id).is_none_or(|f| f.lines().any(|l| DB_FILE.is_match(l.trim()) || DB_PATH.is_match(l.trim())))
            || (DB_IMAGE.is_match(&c.image) && c.volumes.is_empty());
        if sizes.get(&c.id).is_none_or(|&b| b > OWN_DATA) || own_database {
            report.kept_containers.push(c.name.clone());
            continue;
        }
        if !dry_run && !act(apps, vec!["rm".into(), c.id.clone()], &mut report).await {
            continue;
        }
        removed_ids.insert(c.id.clone());
        report.removed_containers.push(c.name.clone());
        for f in &c.folders {
            by_folder.entry(f.clone()).or_default().push(format!("container `{}`", c.name));
        }
    }

    // volumes: remember who mounted each and since when nothing has; rebuildable ones go, databases are asked about
    let mut candidates: Vec<&AppVolume> = Vec::new();
    for v in &volumes {
        let created = keeps::fmt(v.created);
        let mut seen = seen_volumes.get(&v.name).filter(|s| s.created == created).cloned()
            .unwrap_or(VolumeSeen { created, ..Default::default() });
        let mounting: Vec<&AppContainer> = containers.iter().filter(|c| c.volumes.contains_key(&v.name)).collect();
        for c in &mounting {
            seen.folders.extend(c.folders.iter().cloned());
            seen.stacks.insert(c.stack.clone());
            seen.destinations.extend(c.volumes.get(&v.name).cloned());
            seen.images.insert(c.image.clone());
        }
        seen.stacks.extend(v.project.clone());
        if mounting.iter().any(|c| !removed_ids.contains(&c.id)) {
            seen.unused_since = None;
        } else if seen.unused_since.is_none() {
            seen.unused_since = Some(now);
        }
        if kind_of(v, &seen.destinations.iter().cloned().collect::<Vec<_>>(), &seen.images.iter().cloned().collect::<Vec<_>>(), None)
            == Kind::Database {
            seen.kind = Some(Kind::Database);
        }
        let quiet_long = seen.unused_since.is_some_and(|t| now - t >= st.idle) && now - v.created >= st.idle;
        let owners_fine = seen.stacks.iter().all(|s| {
            let members = stacks.get(s.as_str()).cloned().unwrap_or_default();
            !kept.contains(s) && !members.iter().any(|c| c.running) && !(!members.is_empty() && held(s, &members))
        });
        if quiet_long && owners_fine && !seen.folders.iter().any(|f| service_folders.contains(f)) {
            match folders_stand(db, &scratch, &seen.folders, now, st.idle) {
                Ok((false, true)) => candidates.push(v),
                Ok(_) => {}
                Err(e) => report.failures.push(format!("checking volume {}: {e:#}", v.name)),
            }
        }
        seen_volumes.insert(v.name.clone(), seen);
    }
    // their files decide what the signs don't (a database's files win over a rebuildable name)
    let unchecked: Vec<String> = candidates.iter().filter(|v| seen_volumes[&v.name].kind.is_none())
        .take(CONTENT_CHECKS).map(|v| v.mountpoint.clone()).collect();
    let contents = if unchecked.is_empty() { HashMap::new() } else {
        match apps.volume_contents(unchecked).await {
            Ok(c) => c,
            Err(e) => {
                report.failures.push(format!("looking at volumes' files: {e:#}"));
                HashMap::new() // then they're looked at next pass
            }
        }
    };
    let mut to_ask = Vec::new();
    for v in candidates {
        let seen = seen_volumes.get_mut(&v.name).expect("seen above");
        if seen.kind.is_none() {
            let Some(&(db_files, _)) = contents.get(&v.mountpoint) else { continue };
            let destinations: Vec<String> = seen.destinations.iter().cloned().collect();
            let images: Vec<String> = seen.images.iter().cloned().collect();
            seen.kind = Some(kind_of(v, &destinations, &images, Some(db_files)));
        }
        match seen.kind {
            Some(Kind::Rebuildable) => {
                if !dry_run && !act(apps, vec!["volume".into(), "rm".into(), v.name.clone()], &mut report).await {
                    continue;
                }
                report.removed_volumes.push(v.name.clone());
                for f in &seen.folders {
                    by_folder.entry(f.clone()).or_default().push(format!("volume `{}`", v.name));
                }
            }
            Some(Kind::Database) => to_ask.push((v.clone(), seen.clone(), contents.get(&v.mountpoint).map(|c| c.1))),
            _ => report.kept_volumes += 1,
        }
    }
    if !dry_run {
        let c = db.lock();
        let alive: Vec<&String> = volumes.iter().map(|v| &v.name).filter(|n| !report.removed_volumes.contains(n)).collect();
        for name in &alive {
            save_volume(&c, name, &seen_volumes[*name])?;
        }
        c.execute("DELETE FROM agentd_cleanup_volumes WHERE volume NOT IN (SELECT value FROM json_each(?))",
                  [serde_json::to_string(&alive)?])?;
        drop(c);
        for (folder, gone) in &by_folder {
            note(&scratch.join(folder), now, &format!(
                "Removed from the agent's Docker after {} quiet days (nothing used them, and the stack's own setup makes them \
                 again): {}.", st.idle.num_days(), gone.join(", ")), &mut report);
        }
    }
    // each database asked about is measured, so people know what keeping it costs
    let unmeasured: Vec<String> = to_ask.iter().filter(|(_, _, b)| b.is_none()).map(|(v, ..)| v.mountpoint.clone()).collect();
    let measured = if unmeasured.is_empty() { HashMap::new() } else { apps.volume_contents(unmeasured).await.unwrap_or_default() };
    let to_ask: Vec<(AppVolume, VolumeSeen, u64)> = to_ask.into_iter()
        .map(|(v, seen, b)| { let bytes = b.or_else(|| measured.get(&v.mountpoint).map(|m| m.1)).unwrap_or(0); (v, seen, bytes) }).collect();
    if let Err(e) = ask_about(db, ask, &to_ask, &scratch, now, st.idle, dry_run, &mut report) {
        report.failures.push(format!("asking about databases: {e:#}"));
    }

    // images no container has used for the idle period, then old build cache
    // an image counts as used through the pass its last container goes in
    let in_use: HashSet<&str> = containers.iter().flat_map(|c| [c.image_id.as_str(), c.image.as_str()]).collect();
    let mut keep_images = Vec::new();
    for (id, refs) in &image_refs {
        let used = in_use.contains(id.as_str()) || refs.iter().any(|r| in_use.contains(r.as_str()));
        let last = if used { now } else { seen_images.get(id).copied().unwrap_or(now) };
        if now - last >= st.idle {
            let args = if refs.is_empty() { vec![id.clone()] } else { refs.clone() };
            if dry_run || act(apps, [vec!["image".to_string(), "rm".into()], args].concat(), &mut report).await {
                report.removed_images += 1;
                continue;
            }
        }
        keep_images.push((id.clone(), last));
    }
    if !dry_run {
        let c = db.lock();
        c.execute("DELETE FROM agentd_cleanup_images", [])?;
        for (id, last) in &keep_images {
            c.execute("INSERT INTO agentd_cleanup_images(image, last_used_at) VALUES (?, ?)", params![id, keeps::fmt(*last)])?;
        }
    }
    report.build_cache = if dry_run {
        "not pruned in a dry run".into()
    } else {
        match apps.docker(vec!["builder".into(), "prune".into(), "-f".into(), "--filter".into(), format!("until={}h", st.idle.num_hours())]).await {
            Ok(text) => text.lines().find(|l| l.starts_with("Total")).unwrap_or("nothing").trim().to_string(),
            Err(e) => format!("failed: {e:#}"),
        }
    };
    Ok(report)
}

fn note(folder: &Path, now: DateTime<Utc>, text: &str, report: &mut DockerReport) {
    if folder.is_dir() && let Err(e) = append_note(folder, now, text) {
        report.failures.push(format!("writing a note in {}: {e}", folder.display()));
    }
}

// --- asking about databases ------------------------------------------------------------------------------------------

/// Where a question about a conversation's databases goes: (channel, its thread, project). The project's channel
/// first, else the conversation's own channel, else its parent's (a spawned agent).
type Place = (Option<String>, Option<(String, String)>, Option<String>);

fn where_to_ask(c: &Connection, conversation: &str, project_channels: &HashMap<String, String>) -> Result<Place> {
    let row: Option<(Option<String>, Option<String>, Option<String>)> = c.query_row(
        "SELECT channel, thread_ts, project FROM conversations WHERE id = ?", [conversation],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional()?;
    let (mut channel, mut thread_ts, project) = row.unwrap_or_default();
    if channel.is_none() {
        let parent: Option<(Option<String>, Option<String>)> = c.query_row(
            "SELECT p.channel, p.thread_ts FROM spawned_agents s JOIN conversations p ON p.id = s.parent_conversation_id \
             WHERE s.child_conversation_id = ?", [conversation], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
        (channel, thread_ts) = parent.unwrap_or_default();
    }
    let thread = channel.clone().zip(thread_ts);
    let channel = project.as_ref().and_then(|p| project_channels.get(p).cloned()).or(channel);
    Ok((channel, thread, project))
}

/// Start one Jensen turn asking people about the database volumes not asked about yet, at most once a day. A volume
/// whose question's turn failed is asked about again.
#[allow(clippy::too_many_arguments)]
fn ask_about(db: &Db, ask: Option<&dyn Ask>, volumes: &[(AppVolume, VolumeSeen, u64)], scratch: &Path, now: DateTime<Utc>,
             idle: Duration, dry_run: bool, report: &mut DockerReport) -> Result<()> {
    let channels: HashMap<String, String> = ask.map(|a| a.project_channels()).unwrap_or_default();
    let (fresh, lines) = {
        let c = db.lock();
        if !dry_run {
            // asked again (its creation time no longer matches), but its time still counts toward once a day
            c.execute("UPDATE agentd_cleanup_asks SET created_at = '' WHERE work_item IN \
                       (SELECT id FROM work_items WHERE status IN ('failed', 'cancelled'))", [])?;
        }
        let mut fresh = Vec::new();
        for (v, seen, bytes) in volumes {
            let asked: Option<String> = c.query_row("SELECT created_at FROM agentd_cleanup_asks WHERE volume = ?", [&v.name],
                                                    |r| r.get(0)).optional()?;
            if asked.as_deref() != Some(keeps::fmt(v.created).as_str()) {
                fresh.push((v, seen, *bytes));
            }
        }
        if fresh.is_empty() {
            return Ok(());
        }
        let last: Option<String> = c.query_row("SELECT MAX(asked_at) FROM agentd_cleanup_asks", [], |r| r.get(0))?;
        if parse_t(last).is_some_and(|t| now - t < ASK_EVERY) {
            report.to_ask_later = fresh.len();
            return Ok(());
        }
        let mut lines = Vec::new();
        for (v, seen, bytes) in &fresh {
            let mut place = Vec::new();
            for folder in &seen.folders {
                let (channel, thread, project) = where_to_ask(&c, folder, &channels)?;
                place.push(format!("conversation {folder} (folder {}){}{}{}", scratch.join(folder).display(),
                    channel.map(|ch| format!(", ask in channel {ch}")).unwrap_or_else(|| ", no channel known".into()),
                    thread.map(|(ch, ts)| format!(", its thread {ch}/{ts}")).unwrap_or_default(),
                    project.map(|p| format!(", project {p}")).unwrap_or_default()));
            }
            lines.push(format!("- `{}` ({:.2} GB, stack {}, image {}, mounted at {}, created {}, unused since {}): {}", v.name,
                *bytes as f64 / 1e9, join(&seen.stacks), join(&seen.images), join(&seen.destinations), keeps::fmt(v.created),
                seen.unused_since.map(keeps::fmt).unwrap_or_default(),
                if place.is_empty() { "no conversation known".into() } else { place.join("; ") }));
        }
        (fresh, lines)
    };
    report.asked_about.extend(fresh.iter().map(|(v, ..)| v.name.clone()));
    let Some(ask) = ask.filter(|_| !dry_run) else { return Ok(()) };
    let text = format!(
"Disk cleanup needs a decision about test databases. These volumes in your Docker hold databases, and no container has \
used them for at least {} days (their stacks and conversations were quiet that long too):

{}

Ask whether each can be deleted, one message per channel listing that channel's volumes: in the project's channel \
when one is named, else the channel the conversation came from (link its thread for context). Where no channel is \
known, work out the project from the stack's name and ask in its channel; if you can't tell, leave that volume alone. \
Write the message so it stands on its own, since whoever acts on a reply sees only the thread: each volume's exact \
name and creation time, what its database was for (read the thread or the folder if you need to), that keeping it \
costs disk, and that only the volumes named there will be deleted, each only once a person says it's safe and only if \
it is still the same volume, unused. When someone says so: check `docker volume inspect -f '{{{{.CreatedAt}}}}' <name>` \
still shows the creation time in the message and `docker ps -a --filter volume=<name>` prints nothing, then remove \
that one volume with `docker volume rm <name>`. If either check fails, the volume changed: keep it and say so. Keep \
any nobody answers about or wants kept, and delete nothing else: no prune, no other volume.", idle.num_days(), lines.join("\n"));
    // queued with the DB unlocked: queueing locks it too
    let item = ask.ask(&text)?;
    let c = db.lock();
    for (v, ..) in &fresh {
        c.execute("INSERT INTO agentd_cleanup_asks(volume, created_at, asked_at, work_item) VALUES (?, ?, ?, ?) \
                   ON CONFLICT(volume) DO UPDATE SET created_at = excluded.created_at, asked_at = excluded.asked_at, \
                   work_item = excluded.work_item",
                  params![v.name, keeps::fmt(v.created), keeps::fmt(now), item])?;
    }
    Ok(())
}

fn join(s: &BTreeSet<String>) -> String {
    if s.is_empty() { "unknown".into() } else { s.iter().cloned().collect::<Vec<_>>().join(", ") }
}

#[cfg(test)]
mod tests;
