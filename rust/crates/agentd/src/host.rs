//! The Host page: what the machine is doing, sampled every 5 seconds and kept for an hour (the store and its views
//! are a port of dashboard/host.py).
//!
//! The runner's process sampler is gone with the runner: each conversation now runs in its own container, so a
//! consumer is a container. CPU, memory, load and disk I/O are the host's own, from /proc (not namespaced). Each
//! conversation's container is a "session" consumer, linked to its running turn; agent-control is "runner"; the
//! services container "service"; the container that shares its network with the conversations and runs the apps'
//! Docker is "container", or, when its Docker API is reachable, each app container is. Network numbers are that
//! shared network's. Listening ports come from the conversations' holders and the apps' published ports. Folder
//! sizes take a while to add up, so disk space is measured every 10 minutes. Nothing here can stop agentd: a sample
//! that fails is logged and skipped.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use regex::Regex;
use serde_json::{Value, json};

use crate::containers::{HolderProbe, Probe};
use crate::shared::Shared;

const KEEP_S: f64 = 3600.0;
/// Samples come every 5 s; older than this and the page says nothing is reporting.
const STALE_S: f64 = 30.0;
const KINDS: [&str; 7] = ["session", "browser", "service", "container", "runner", "other", "host"];
pub const SAMPLE_EVERY: Duration = Duration::from_secs(5);
const SPACE_EVERY: Duration = Duration::from_secs(600);
const SPACE_BUDGET: Duration = Duration::from_secs(300);
const MAX_ROWS: usize = 200;
const MAX_INTERFACES: usize = 12;
const TOP_CONVERSATIONS: usize = 20;
const CACHES: [(&str, &str); 6] = [(".npm", "npm cache"), (".cache/pip", "pip cache"), (".cache/uv", "uv cache"),
                                   (".cache/ms-playwright", "playwright cache"), (".bun/install/cache", "bun cache"),
                                   (".cache/go-build", "go cache")];
static SKIP_DISKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(loop|ram|zram|dm-|md|sr|fd)").unwrap());
static PARTITION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(sd|vd|xvd|hd)[a-z]+\d+$|^(nvme\d+n\d+|mmcblk\d+)p\d+$").unwrap());
// bridges and the like carry the same bytes as the interface they lead to, so the totals leave them out
static VIRTUAL_NET: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(lo|br-|veth|docker\d|virbr|cni|flannel|cali|tailscale|tun|tap)").unwrap());

// --- the store and its views ---------------------------------------------------------------------------------------

fn num(v: &Value) -> f64 {
    v.as_f64().unwrap_or(0.0)
}

fn rows(v: &Value) -> Vec<Value> {
    v.as_array().into_iter().flatten().filter(|r| r.is_object()).cloned().collect()
}

fn obj_or_null(v: &Value) -> Value {
    if v.as_object().is_some_and(|m| !m.is_empty()) { v.clone() } else { Value::Null }
}

/// The snapshot with every part the shape the views expect, so an odd one can't break the dashboard: objects are
/// objects, lists are lists of objects, and the ids rows link by are strings.
pub fn clean(snap: &Value) -> Value {
    let mut out = snap.as_object().cloned().unwrap_or_default();
    for k in ["cpu", "memory", "network", "disk_io"] {
        out.insert(k.into(), obj_or_null(&snap[k]));
    }
    if out["network"].is_object() {
        let interfaces = json!(rows(&out["network"]["interfaces"]));
        out.get_mut("network").and_then(Value::as_object_mut).map(|n| n.insert("interfaces".into(), interfaces));
    }
    let space = &snap["disk_space"];
    out.insert("disk_space".into(), if space.as_object().is_some_and(|m| !m.is_empty()) {
        let mut s = space.as_object().cloned().unwrap_or_default();
        s.insert("volumes".into(), json!(rows(&space["volumes"])));
        s.insert("top".into(), json!(rows(&space["top"]).into_iter().map(|mut r| {
            let c = r["conversation"].as_str().map_or(Value::Null, |c| json!(c));
            r["conversation"] = c;
            r
        }).collect::<Vec<_>>()));
        Value::Object(s)
    } else { Value::Null });
    for key in ["consumers", "containers", "ports"] {
        out.insert(key.into(), json!(rows(&snap[key]).into_iter().map(|mut r| {
            for id in ["item", "conversation"] {
                let v = r[id].as_str().map_or(Value::Null, |s| json!(s));
                r[id] = v;
            }
            let kind = r["kind"].as_str().filter(|k| !k.is_empty()).unwrap_or("other").to_string();
            r["kind"] = json!(kind);
            r
        }).collect::<Vec<_>>()));
    }
    Value::Object(out)
}

pub struct HostStore {
    keep_s: f64,
    points: VecDeque<Value>,
    latest: Option<Value>,
    received_at: Option<f64>,
}

impl Default for HostStore {
    fn default() -> HostStore {
        HostStore::new(KEEP_S)
    }
}

impl HostStore {
    pub fn new(keep_s: f64) -> HostStore {
        HostStore { keep_s, points: VecDeque::new(), latest: None, received_at: None }
    }

    pub fn add(&mut self, snap: &Value, now: f64) {
        let snap = clean(snap);
        let mut cpu: BTreeMap<&str, f64> = KINDS.iter().map(|k| (*k, 0.0)).collect();
        let mut mem = cpu.clone();
        for c in snap["consumers"].as_array().into_iter().flatten() {
            if let Some(k) = c["kind"].as_str().and_then(|k| KINDS.iter().find(|x| **x == k)) {
                *cpu.get_mut(k).unwrap() += num(&c["cpu_percent"]);
                *mem.get_mut(k).unwrap() += num(&c["mem_bytes"]);
            }
        }
        // what the host total holds beyond the containers agentd sees: everything else on the machine
        cpu.insert("host", (num(&snap["cpu"]["percent"]) - cpu.values().sum::<f64>()).max(0.0));
        mem.insert("host", (num(&snap["memory"]["used"]) - mem.values().sum::<f64>()).max(0.0));
        let by = |m: &BTreeMap<&str, f64>, round: bool| Value::Object(KINDS.iter().map(|k| {
            let v = m[k];
            (k.to_string(), json!(if round { (v * 100.0).round() / 100.0 } else { v }))
        }).collect());
        self.points.push_back(json!({
            "ts": now, "cpu": num(&snap["cpu"]["percent"]), "mem_used": num(&snap["memory"]["used"]),
            "swap_used": num(&snap["memory"]["swap_used"]),
            "rx_bps": num(&snap["network"]["rx_bps"]), "tx_bps": num(&snap["network"]["tx_bps"]),
            "read_bps": num(&snap["disk_io"]["read_bps"]), "write_bps": num(&snap["disk_io"]["write_bps"]),
            "containers": snap["containers"].as_array().map_or(0, Vec::len), "ports": snap["ports"].as_array().map_or(0, Vec::len),
            "cpu_by_kind": by(&cpu, true), "mem_by_kind": by(&mem, false),
        }));
        self.latest = Some(snap);
        self.received_at = Some(now);
        while self.points.front().is_some_and(|p| num(&p["ts"]) < now - self.keep_s) {
            self.points.pop_front();
        }
    }

    pub fn status(&self, now: f64) -> Value {
        let age = self.received_at.map(|at| (now - at).max(0.0));
        json!({"available": self.latest.is_some(), "age_s": age.map(|a| a.round_ties_even() as i64),
               "stale": age.is_none_or(|a| a > STALE_S)})
    }
}

/// The host's totals now and over the last hour.
pub fn host_view(store: &HostStore, now: f64) -> Value {
    let empty = json!({});
    let snap = store.latest.as_ref().unwrap_or(&empty);
    let space = &snap["disk_space"];
    let mut out = store.status(now).as_object().cloned().unwrap_or_default();
    let get = |k: &str| snap.get(k).cloned().unwrap_or(Value::Null);
    out.extend(json!({
        "ts": get("ts"), "cpu": get("cpu"), "memory": get("memory"), "network": get("network"), "disk_io": get("disk_io"),
        "disk_space": if space.as_object().is_some_and(|m| !m.is_empty()) {
            json!({"volumes": space.get("volumes").filter(|v| crate::records::truthy(Some(v))).cloned().unwrap_or(json!([])),
                   "at": space.get("at").cloned().unwrap_or(Value::Null),
                   "complete": space.get("complete").cloned().unwrap_or(json!(true))})
        } else { Value::Null },
        "containers": snap["containers"].as_array().map_or(0, Vec::len), "ports": snap["ports"].as_array().map_or(0, Vec::len),
        "history": store.points.iter().cloned().collect::<Vec<_>>(),
    }).as_object().cloned().unwrap_or_default());
    Value::Object(out)
}

/// Who uses what, from the newest sample: the consumers (with a row for what the host spends outside them), the
/// containers, the listening ports and what takes the disk. `session_of(item, conversation)` is the dashboard session
/// to link a row to: {id, title, state}, or null.
pub fn consumers_view(store: &HostStore, now: f64, mut session_of: impl FnMut(Option<&str>, Option<&str>) -> Value) -> Value {
    let empty = json!({});
    let snap = store.latest.as_ref().unwrap_or(&empty);
    let mut cache: HashMap<(Option<String>, Option<String>), Value> = HashMap::new();
    let mut link = |row: &Value| -> Value {
        let key = (row["item"].as_str().map(str::to_string), row["conversation"].as_str().map(str::to_string));
        let session = if key == (None, None) { Value::Null } else {
            cache.entry(key.clone()).or_insert_with(|| session_of(key.0.as_deref(), key.1.as_deref())).clone()
        };
        let mut r = row.clone();
        r["session"] = session;
        r
    };
    let mut consumers: Vec<Value> = rows(&snap["consumers"]).iter().map(&mut link).collect();
    let seen_cpu: f64 = consumers.iter().map(|c| num(&c["cpu_percent"])).sum();
    let seen_mem: f64 = consumers.iter().map(|c| num(&c["mem_bytes"])).sum();
    if snap.as_object().is_some_and(|m| !m.is_empty()) {
        consumers.push(json!({"key": "host", "kind": "host", "name": "outside the agent container", "item": null,
                              "conversation": null,
                              "cpu_percent": ((num(&snap["cpu"]["percent"]) - seen_cpu).max(0.0) * 100.0).round() / 100.0,
                              "mem_bytes": (num(&snap["memory"]["used"]) - seen_mem).max(0.0),
                              "read_bps": null, "write_bps": null, "rx_bps": null, "tx_bps": null, "processes": null,
                              "session": null}));
    }
    let mut out = store.status(now).as_object().cloned().unwrap_or_default();
    out.extend(json!({
        "ts": snap.get("ts").cloned().unwrap_or(Value::Null), "consumers": consumers,
        "containers": rows(&snap["containers"]).iter().map(&mut link).collect::<Vec<_>>(),
        "ports": rows(&snap["ports"]).iter().map(&mut link).collect::<Vec<_>>(),
        "disk": rows(&snap["disk_space"]["top"]).iter().map(&mut link).collect::<Vec<_>>(),
        "interfaces": snap["network"].get("interfaces").cloned().unwrap_or(json!([])),
    }).as_object().cloned().unwrap_or_default());
    Value::Object(out)
}

// --- /proc readers: each takes the /proc root, so tests can hand them a fake one -----------------------------------

/// Per line of /proc/stat ("cpu", "cpu0", ...): (busy, total) jiffies.
pub fn read_cpu(proc: &Path) -> BTreeMap<String, (u64, u64)> {
    let text = std::fs::read_to_string(proc.join("stat")).unwrap_or_default();
    text.lines().filter(|l| l.starts_with("cpu")).filter_map(|l| {
        let mut f = l.split_whitespace();
        let name = f.next()?.to_string();
        let nums: Vec<u64> = f.take(8).filter_map(|v| v.parse().ok()).collect();
        let total: u64 = nums.iter().sum();
        let idle = nums.get(3).copied().unwrap_or(0) + nums.get(4).copied().unwrap_or(0);
        Some((name, (total - idle, total)))
    }).collect()
}

pub fn read_meminfo(proc: &Path) -> HashMap<String, u64> {
    let text = std::fs::read_to_string(proc.join("meminfo")).unwrap_or_default();
    text.lines().filter_map(|l| {
        let (key, rest) = l.split_once(':')?;
        let parts: Vec<&str> = rest.split_whitespace().collect();
        let n: u64 = parts.first()?.parse().ok()?;
        Some((key.to_string(), n * if parts.len() > 1 { 1024 } else { 1 }))
    }).collect()
}

pub fn read_loadavg(proc: &Path) -> Vec<f64> {
    std::fs::read_to_string(proc.join("loadavg")).unwrap_or_default().split_whitespace().take(3)
        .filter_map(|v| v.parse().ok()).collect()
}

pub fn whole_disks(sys_block: &Path) -> Option<HashSet<String>> {
    Some(std::fs::read_dir(sys_block).ok()?.flatten().filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| !SKIP_DISKS.is_match(n)).collect())
}

/// Bytes read and written on the host's whole disks (partitions and the devices layered on them would count the
/// same bytes twice).
pub fn read_diskstats(proc: &Path, disks: Option<&HashSet<String>>) -> (u64, u64) {
    let text = std::fs::read_to_string(proc.join("diskstats")).unwrap_or_default();
    let (mut read, mut written) = (0, 0);
    for line in text.lines() {
        let p: Vec<&str> = line.split_whitespace().collect();
        if p.len() < 10 {
            continue;
        }
        let name = p[2];
        match disks {
            Some(d) if !d.contains(name) => continue,
            None if SKIP_DISKS.is_match(name) || PARTITION.is_match(name) => continue,
            _ => {}
        }
        read += p[5].parse::<u64>().unwrap_or(0) * 512;
        written += p[9].parse::<u64>().unwrap_or(0) * 512;
    }
    (read, written)
}

/// cpu ns, memory bytes, network and block I/O byte counters and the process count from one container's stats.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Numbers {
    pub cpu: u64,
    pub mem: u64,
    pub rx: u64,
    pub tx: u64,
    pub read: u64,
    pub write: u64,
    pub pids: u64,
}

pub fn container_numbers(stats: &Value) -> Numbers {
    let u = |v: &Value| v.as_u64().unwrap_or(0);
    let inner = &stats["memory_stats"]["stats"];
    let cache = inner.get("inactive_file").or(inner.get("total_inactive_file")).map_or(0, u);
    let nets = stats["networks"].as_object();
    let (mut read, mut write) = (0, 0);
    for e in stats["blkio_stats"]["io_service_bytes_recursive"].as_array().into_iter().flatten() {
        match e["op"].as_str().unwrap_or_default().to_lowercase().as_str() {
            "read" => read += u(&e["value"]),
            "write" => write += u(&e["value"]),
            _ => {}
        }
    }
    Numbers { cpu: u(&stats["cpu_stats"]["cpu_usage"]["total_usage"]),
              mem: u(&stats["memory_stats"]["usage"]).saturating_sub(cache),
              rx: nets.into_iter().flatten().map(|(_, n)| u(&n["rx_bytes"])).sum(),
              tx: nets.into_iter().flatten().map(|(_, n)| u(&n["tx_bytes"])).sum(),
              read, write, pids: u(&stats["pids_stats"]["current"]) }
}

// --- one reading and the snapshot two of them make -----------------------------------------------------------------

/// One container as sampled: what it is for the page, and its counters.
#[derive(Clone, Debug, Default)]
pub struct Sampled {
    pub id: String,
    pub name: String,
    pub image: String,
    pub state: String,
    pub status: String,
    /// session, runner, service, container or other; None: listed but not a consumer (its parts are).
    pub kind: Option<&'static str>,
    pub item: Option<String>,
    pub conversation: Option<String>,
    pub numbers: Numbers,
    /// (address, port, protocol) it publishes.
    pub published: Vec<(String, u16, String)>,
}

#[derive(Clone, Debug, Default)]
pub struct Reading {
    /// Seconds on a steady clock.
    pub at: f64,
    pub cpu: BTreeMap<String, (u64, u64)>,
    pub mem: HashMap<String, u64>,
    pub load: Vec<f64>,
    pub disk: (u64, u64),
    /// The shared network's interfaces: (rx, tx) bytes.
    pub net: BTreeMap<String, (u64, u64)>,
    pub containers: Vec<Sampled>,
    /// (conversation, its running work item, port) a holder reports.
    pub listeners: Vec<(String, Option<String>, u16)>,
}

fn rate(now: u64, before: Option<u64>, dt: f64) -> f64 {
    match before {
        Some(b) if dt > 0.0 => now.saturating_sub(b) as f64 / dt,
        _ => 0.0,
    }
}

fn key_of(c: &Sampled) -> String {
    match c.kind {
        Some("session") => format!("session:{}", c.conversation.as_deref().unwrap_or(&c.name)),
        Some("service") => format!("service:{}", c.name),
        Some(k @ ("runner" | "other")) => k.to_string(),
        _ => format!("container:{}", c.name),
    }
}

/// The snapshot from two readings `dt` seconds apart, as the dashboard reads it; `wall` is the time it is stamped
/// with and `space` the last disk-space measurement.
pub fn build(prev: &Reading, now: &Reading, wall: f64, space: Option<&Value>) -> Value {
    let dt = now.at - prev.at;
    let cores: Vec<&String> = {
        let mut c: Vec<&String> = now.cpu.keys().filter(|k| *k != "cpu").collect();
        c.sort_by_key(|k| k[3..].parse::<u32>().unwrap_or(0));
        c
    };
    let ncpu = cores.len().max(1) as f64;
    let pct = |name: &str| {
        let (busy, total) = now.cpu.get(name).copied().unwrap_or((0, 0));
        let (b0, t0) = prev.cpu.get(name).copied().unwrap_or((busy, total));
        if total > t0 { (1000.0 * (busy - b0) as f64 / (total - t0) as f64).round() / 10.0 } else { 0.0 }
    };
    let m = |k: &str| now.mem.get(k).copied().unwrap_or(0);
    let mut ifaces: Vec<Value> = now.net.iter().map(|(name, (rx, tx))| {
        let p = prev.net.get(name);
        json!({"name": name, "rx_bps": rate(*rx, p.map(|p| p.0), dt).round(), "tx_bps": rate(*tx, p.map(|p| p.1), dt).round()})
    }).collect();
    let real = |i: &&Value| !VIRTUAL_NET.is_match(i["name"].as_str().unwrap_or_default());
    let (rx, tx) = (ifaces.iter().filter(real).map(|i| num(&i["rx_bps"])).sum::<f64>(),
                    ifaces.iter().filter(real).map(|i| num(&i["tx_bps"])).sum::<f64>());
    ifaces.sort_by(|a, b| (num(&b["rx_bps"]) + num(&b["tx_bps"])).total_cmp(&(num(&a["rx_bps"]) + num(&a["tx_bps"]))));
    ifaces.truncate(MAX_INTERFACES);

    let before: HashMap<&str, &Numbers> = prev.containers.iter().map(|c| (c.id.as_str(), &c.numbers)).collect();
    let (mut consumers, mut rows, mut published): (Vec<(f64, f64, Value)>, Vec<Value>, HashMap<u16, String>) =
        (Vec::new(), Vec::new(), HashMap::new());
    for c in &now.containers {
        let (n, b) = (&c.numbers, before.get(c.id.as_str()));
        let cpu = b.map_or(0.0, |b| 100.0 * n.cpu.saturating_sub(b.cpu) as f64 / 1e9 / dt / ncpu);
        let (read, write) = (rate(n.read, b.map(|b| b.read), dt), rate(n.write, b.map(|b| b.write), dt));
        let (crx, ctx) = (rate(n.rx, b.map(|b| b.rx), dt), rate(n.tx, b.map(|b| b.tx), dt));
        let row = json!({"name": c.name, "image": c.image, "state": c.state, "status": c.status, "conversation": c.conversation,
                         "cpu_percent": (cpu * 100.0).round() / 100.0, "mem_bytes": n.mem, "rx_bps": crx.round(),
                         "tx_bps": ctx.round(), "read_bps": read.round(), "write_bps": write.round()});
        rows.push(row);
        let Some(kind) = c.kind else { continue };
        let key = key_of(c);
        for (_, port, _) in &c.published {
            published.insert(*port, key.clone());
        }
        let name = if kind == "session" { c.conversation.clone().unwrap_or_else(|| c.name.clone()) } else { c.name.clone() };
        consumers.push((cpu, n.mem as f64, json!({
            "key": key, "kind": kind, "name": name, "item": c.item, "conversation": c.conversation,
            "cpu_percent": (cpu * 100.0).round() / 100.0, "mem_bytes": n.mem, "read_bps": read.round(),
            "write_bps": write.round(), "rx_bps": crx.round(), "tx_bps": ctx.round(), "processes": n.pids})));
    }
    let consumer = |key: &str| consumers.iter().find(|(_, _, c)| c["key"] == key).map(|(_, _, c)| c.clone());
    let mut ports: Vec<Value> = Vec::new();
    let mut seen = HashSet::new();
    for (convo, item, port) in &now.listeners {
        if seen.insert((*port, convo.clone())) {
            ports.push(json!({"port": port, "address": null, "proto": "tcp", "pid": null, "process": null,
                              "consumer": format!("session:{convo}"), "kind": "session", "name": convo, "item": item,
                              "conversation": convo}));
        }
    }
    for c in &now.containers {
        for (address, port, proto) in &c.published {
            let key = published.get(port).cloned().unwrap_or_else(|| "host".into());
            let owner = consumer(&key);
            ports.push(json!({"port": port, "address": address, "proto": proto, "pid": null, "process": "docker-proxy",
                              "consumer": key, "kind": owner.as_ref().map_or(json!("host"), |o| o["kind"].clone()),
                              "name": owner.as_ref().map_or(json!("outside the agent container"), |o| o["name"].clone()),
                              "item": owner.as_ref().map_or(Value::Null, |o| o["item"].clone()),
                              "conversation": owner.as_ref().map_or(Value::Null, |o| o["conversation"].clone())}));
        }
    }
    ports.sort_by_key(|p| p["port"].as_u64().unwrap_or(0));
    consumers.sort_by(|a, b| b.0.total_cmp(&a.0).then(b.1.total_cmp(&a.1)));
    let space = space.map(|s| {
        let mut s = s.clone();
        if let Some(top) = s["top"].as_array_mut() {
            top.sort_by(|a, b| num(&b["bytes"]).total_cmp(&num(&a["bytes"])));
        }
        s
    });
    json!({
        "ts": wall, "interval_s": (dt * 100.0).round() / 100.0,
        "cpu": {"percent": pct("cpu"), "per_core": cores.iter().map(|k| pct(k)).collect::<Vec<_>>(), "cores": ncpu as u64,
                "load": now.load},
        "memory": {"total": m("MemTotal"), "available": m("MemAvailable"), "used": m("MemTotal").saturating_sub(m("MemAvailable")),
                   "swap_total": m("SwapTotal"), "swap_used": m("SwapTotal").saturating_sub(m("SwapFree"))},
        "network": {"rx_bps": rx, "tx_bps": tx, "interfaces": ifaces},
        "disk_io": {"read_bps": rate(now.disk.0, Some(prev.disk.0), dt).round(), "write_bps": rate(now.disk.1, Some(prev.disk.1), dt).round()},
        "disk_space": space,
        "containers": rows.into_iter().take(MAX_ROWS).collect::<Vec<_>>(),
        "ports": ports.into_iter().take(MAX_ROWS).collect::<Vec<_>>(),
        "consumers": consumers.into_iter().take(MAX_ROWS).map(|(_, _, c)| c).collect::<Vec<_>>(),
    })
}

// --- disk space ----------------------------------------------------------------------------------------------------

/// The bytes the files under `path` take on disk, without following links or leaving its filesystem, and whether
/// the walk finished before `deadline`. A file with several hard links counts once.
fn tree_bytes(path: &Path, deadline: Instant) -> (u64, bool) {
    use std::os::unix::fs::MetadataExt;
    let Ok(meta) = std::fs::symlink_metadata(path) else { return (0, true) };
    let (mut total, dev) = (meta.blocks() * 512, meta.dev());
    if !meta.is_dir() {
        return (total, true);
    }
    let (mut stack, mut linked) = (vec![path.to_path_buf()], HashSet::new());
    while let Some(dir) = stack.pop() {
        if Instant::now() > deadline {
            return (total, false);
        }
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for e in entries.flatten() {
            let Ok(m) = e.metadata() else { continue };
            if m.nlink() > 1 && !m.is_dir() && !linked.insert((m.dev(), m.ino())) {
                continue;
            }
            total += m.blocks() * 512;
            if m.is_dir() && m.dev() == dev {
                stack.push(e.path());
            }
        }
    }
    (total, true)
}

fn volume(path: &Path) -> Option<Value> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: `c` is a valid C string and `st` a writable statvfs for the call to fill
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return None;
    }
    let frsize = st.f_frsize as u64;
    let total = st.f_blocks as u64 * frsize;
    Some(json!({"path": path.to_string_lossy(), "total": total, "used": total - st.f_bfree as u64 * frsize,
                "free": st.f_bavail as u64 * frsize}))
}

/// The volumes' use and what takes the space: each conversation's folder, the caches, the other folders.
pub fn measure_space(roots: &[PathBuf], work: &Path, budget: Duration, wall: f64) -> Value {
    let deadline = Instant::now() + budget;
    let mut complete = true;
    let mut size = |p: &Path| {
        let (n, done) = tree_bytes(p, deadline);
        complete &= done;
        n
    };
    let (mut top, mut convos): (Vec<Value>, Vec<Value>) = (Vec::new(), Vec::new());
    let mut children: Vec<PathBuf> = std::fs::read_dir(work).map(|d| d.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    children.sort();
    for child in children {
        let Ok(meta) = std::fs::symlink_metadata(&child) else { continue };
        if meta.file_type().is_symlink() {
            continue;
        }
        let name = child.file_name().unwrap_or_default().to_string_lossy().to_string();
        if name == "scratch" && meta.is_dir() {
            let mut folders: Vec<PathBuf> = std::fs::read_dir(&child).map(|d| d.flatten().map(|e| e.path()).collect()).unwrap_or_default();
            folders.sort();
            for c in folders.into_iter().filter(|c| c.is_dir() && !c.is_symlink()) {
                let convo = c.file_name().unwrap_or_default().to_string_lossy().to_string();
                convos.push(json!({"name": convo, "kind": "conversation", "bytes": size(&c), "conversation": convo}));
            }
        } else if name == "home" && meta.is_dir() {
            let mut whole = size(&child) as i64;
            for (rel, label) in CACHES {
                if child.join(rel).is_dir() {
                    let n = size(&child.join(rel));
                    whole -= n as i64;
                    top.push(json!({"name": label, "kind": "cache", "bytes": n}));
                }
            }
            top.push(json!({"name": "home (other)", "kind": "folder", "bytes": whole.max(0)}));
        } else if name == "ms-playwright" {
            top.push(json!({"name": "playwright browsers", "kind": "cache", "bytes": size(&child)}));
        } else {
            top.push(json!({"name": name, "kind": "folder", "bytes": size(&child)}));
        }
    }
    convos.sort_by(|a, b| num(&b["bytes"]).total_cmp(&num(&a["bytes"])));
    if convos.len() > TOP_CONVERSATIONS {
        let rest: Vec<Value> = convos.split_off(TOP_CONVERSATIONS);
        convos.push(json!({"name": format!("{} other conversations", rest.len()), "kind": "conversation",
                           "bytes": rest.iter().map(|r| num(&r["bytes"])).sum::<f64>()}));
    }
    top.extend(convos);
    json!({"at": wall, "complete": complete, "volumes": roots.iter().filter_map(|r| volume(r)).collect::<Vec<_>>(), "top": top})
}

fn docker_space(df: &Value) -> Vec<Value> {
    let sum = |list: &Value, f: &dyn Fn(&Value) -> f64| list.as_array().into_iter().flatten().map(f).sum::<f64>();
    vec![json!({"name": "docker images", "kind": "docker", "bytes": num(&df["LayersSize"])}),
         json!({"name": "docker containers", "kind": "docker", "bytes": sum(&df["Containers"], &|c| num(&c["SizeRw"]))}),
         json!({"name": "docker volumes", "kind": "docker",
                "bytes": sum(&df["Volumes"], &|v| num(&v["UsageData"]["Size"]).max(0.0))}),
         json!({"name": "docker build cache", "kind": "docker", "bytes": sum(&df["BuildCache"], &|b| num(&b["Size"]))})]
}

// --- the sampler ---------------------------------------------------------------------------------------------------

pub struct SamplerSettings {
    pub proc: PathBuf,
    pub sys_block: PathBuf,
    /// The shared volume (/var/agent) and the data directory: the volumes shown, and where conversations' folders are.
    pub work: PathBuf,
    pub data: PathBuf,
    /// The compose project agent-control belongs to: its containers are the stack's.
    pub project: String,
    /// The container whose network the conversations share and whose Docker runs the apps.
    pub network_container: String,
    /// That Docker's API, when agentd can reach it (each app container then counts on its own).
    pub apps_docker: Option<String>,
}

pub struct Sampler {
    settings: SamplerSettings,
    docker: bollard::Docker,
    apps: Option<bollard::Docker>,
    disks: Option<HashSet<String>>,
    started: Instant,
    space: Arc<Mutex<Option<Value>>>,
    space_at: Option<Instant>,
}

fn summary_json(c: &impl serde::Serialize) -> Value {
    serde_json::to_value(c).unwrap_or(Value::Null)
}

impl Sampler {
    pub fn new(settings: SamplerSettings) -> anyhow::Result<Sampler> {
        let docker = bollard::Docker::connect_with_local_defaults()?;
        let apps = match &settings.apps_docker {
            Some(url) => Some(bollard::Docker::connect_with_http(url, 10, bollard::API_DEFAULT_VERSION)?),
            None => None,
        };
        let disks = whole_disks(&settings.sys_block);
        Ok(Sampler { settings, docker, apps, disks, started: Instant::now(), space: Arc::default(), space_at: None })
    }

    async fn stats(docker: &bollard::Docker, id: &str) -> Option<Value> {
        use futures_util::StreamExt;
        let options = bollard::query_parameters::StatsOptions { stream: false, one_shot: true };
        let stats = docker.stats(id, Some(options)).next().await?.ok()?;
        Some(summary_json(&stats))
    }

    async fn listed(docker: &bollard::Docker, label: Option<String>) -> Vec<Value> {
        let filters = label.map(|l| HashMap::from([("label".to_string(), vec![l])]));
        let options = bollard::query_parameters::ListContainersOptions { filters, ..Default::default() };
        docker.list_containers(Some(options)).await.map(|l| l.iter().map(summary_json).collect()).unwrap_or_default()
    }

    async fn read(&mut self, s: &Shared) -> Reading {
        let p = self.settings.proc.clone();
        let mut r = Reading { at: self.started.elapsed().as_secs_f64(), cpu: read_cpu(&p), mem: read_meminfo(&p),
                              load: read_loadavg(&p), disk: read_diskstats(&p, self.disks.as_ref()), ..Default::default() };
        let mut found = Self::listed(&self.docker, Some("agent.conversation".into())).await;
        found.extend(Self::listed(&self.docker, Some(format!("com.docker.compose.project={}", self.settings.project))).await);
        let mut ids = HashSet::new();
        for c in found {
            let id = c["Id"].as_str().unwrap_or_default().to_string();
            if !ids.insert(id.clone()) {
                continue;
            }
            let labels = &c["Labels"];
            let name = c["Names"][0].as_str().unwrap_or("/?").trim_start_matches('/').to_string();
            let service = labels["com.docker.compose.service"].as_str().unwrap_or_default();
            let conversation = labels["agent.conversation"].as_str().map(str::to_string);
            let kind = if conversation.is_some() {
                Some("session")
            } else if name == self.settings.network_container {
                if self.apps.is_some() { None } else { Some("container") }
            } else if service == "agent-control" {
                Some("runner")
            } else if service == "services" || name == "agent-services" {
                Some("service")
            } else {
                Some("other")
            };
            let item = match &conversation {
                Some(convo) => s.db.lock().query_row(
                    "SELECT id FROM work_items WHERE conversation_id = ? AND status = 'claimed' LIMIT 1", [convo],
                    |row| row.get(0)).ok(),
                None => None,
            };
            let Some(stats) = Self::stats(&self.docker, &id).await else { continue };
            if name == self.settings.network_container {
                r.net = stats["networks"].as_object().into_iter().flatten()
                    .map(|(k, n)| (k.clone(), (n["rx_bytes"].as_u64().unwrap_or(0), n["tx_bytes"].as_u64().unwrap_or(0)))).collect();
            }
            let numbers = container_numbers(&stats);
            r.containers.push(Sampled { id, name, image: c["Image"].as_str().unwrap_or_default().into(),
                                        state: c["State"].as_str().unwrap_or_default().into(),
                                        status: c["Status"].as_str().unwrap_or_default().into(), kind, item, conversation,
                                        numbers, published: Vec::new() });
        }
        if let Some(apps) = &self.apps {
            let scratch = self.settings.work.join("scratch");
            for c in Self::listed(apps, None).await {
                let id = c["Id"].as_str().unwrap_or_default().to_string();
                let Some(numbers) = Self::stats(apps, &id).await.map(|v| container_numbers(&v)) else { continue };
                let conversation = c["Labels"]["com.docker.compose.project.working_dir"].as_str()
                    .and_then(|d| Path::new(d).strip_prefix(&scratch).ok()?.components().next())
                    .map(|p| p.as_os_str().to_string_lossy().to_string());
                let published = c["Ports"].as_array().into_iter().flatten()
                    .filter_map(|p| Some((p["IP"].as_str().unwrap_or_default().to_string(), p["PublicPort"].as_u64()? as u16,
                                          p["Type"].as_str().unwrap_or("tcp").to_string()))).collect();
                r.containers.push(Sampled { id, name: c["Names"][0].as_str().unwrap_or("/?").trim_start_matches('/').into(),
                                            image: c["Image"].as_str().unwrap_or_default().into(),
                                            state: c["State"].as_str().unwrap_or_default().into(),
                                            status: c["Status"].as_str().unwrap_or_default().into(), kind: Some("container"),
                                            item: None, conversation, numbers, published });
            }
        }
        let probe = HolderProbe { run_root: holder_proto::RUN_ROOT.into() };
        for c in r.containers.iter().filter(|c| c.kind == Some("session")) {
            let Some(convo) = &c.conversation else { continue };
            if let Some(status) = probe.status(convo).await {
                r.listeners.extend(status.listeners.iter().map(|p| (convo.clone(), c.item.clone(), *p)));
            }
        }
        r
    }

    fn start_space(&mut self) {
        if self.space_at.is_some_and(|at| at.elapsed() < SPACE_EVERY) {
            return;
        }
        self.space_at = Some(Instant::now());
        let (roots, work, space, apps) = (vec![self.settings.work.clone(), self.settings.data.clone()],
                                          self.settings.work.clone(), self.space.clone(), self.apps.clone());
        tokio::spawn(async move {
            let wall = chrono::Utc::now().timestamp_millis() as f64 / 1000.0;
            let Ok(mut measured) = tokio::task::spawn_blocking(move || measure_space(&roots, &work, SPACE_BUDGET, wall)).await
            else { return };
            if let Some(apps) = apps && let Ok(df) = apps.df(None::<bollard::query_parameters::DataUsageOptions>).await
                && let Some(top) = measured["top"].as_array_mut() {
                top.extend(docker_space(&summary_json(&df)));
            }
            *space.lock().unwrap_or_else(|e| e.into_inner()) = Some(measured);
        });
    }

    /// Sample every 5 seconds into `store`.
    pub async fn run(mut self, s: Arc<Shared>, store: Arc<Mutex<HostStore>>) {
        let mut prev: Option<Reading> = None;
        loop {
            self.start_space();
            let reading = self.read(&s).await;
            if let Some(before) = &prev {
                let wall = chrono::Utc::now().timestamp_millis() as f64 / 1000.0;
                let space = self.space.lock().unwrap_or_else(|e| e.into_inner()).clone();
                let snap = build(before, &reading, wall, space.as_ref());
                store.lock().unwrap_or_else(|e| e.into_inner()).add(&snap, wall);
            }
            prev = Some(reading);
            tokio::time::sleep(SAMPLE_EVERY).await;
        }
    }
}
