//! The Host page: the store and views against Python's on the same snapshots (tests/fixtures/dashboard/make_host.py),
//! and a snapshot built from /proc files and container counters.

use std::collections::HashMap;

use agentd::host::{self, HostStore, Numbers, Reading, Sampled};
use serde_json::{Value, json};

mod common;

#[test]
fn store_and_views_match_python() {
    let e: Value = serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/host.json").unwrap()).unwrap();
    let links: HashMap<(Option<String>, Option<String>), Value> = e["links"].as_array().unwrap().iter()
        .map(|l| ((l[0].as_str().map(str::to_string), l[1].as_str().map(str::to_string)), l[2].clone())).collect();
    let session_of = |item: Option<&str>, convo: Option<&str>| {
        links.get(&(item.map(str::to_string), convo.map(str::to_string))).cloned().unwrap_or(Value::Null)
    };
    let views = |store: &HostStore, now: f64| json!({"host": host::host_view(store, now),
                                                     "consumers": host::consumers_view(store, now, session_of)});
    let mut store = HostStore::new(3600.0);
    common::assert_json(&views(&store, 0.0), &e["empty"], "empty");
    for step in e["steps"].as_array().unwrap() {
        let at = step["at"].as_f64().unwrap();
        match step.get("add") {
            Some(which) => store.add(&e["snapshots"][which.as_u64().unwrap() as usize], at),
            None => common::assert_json(&views(&store, at), &step["view"], &format!("at {at}")),
        }
    }
}

fn proc_files(dir: &std::path::Path, user: u64, idle: u64, sectors_read: u64) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(dir.join("stat"), format!(
        "cpu  {user} 0 {user} {idle} 0 0 0 0 0 0\ncpu0 {} 0 {} {} 0 0 0 0\ncpu1 {} 0 {} {} 0 0 0 0\nintr 1 2\n",
        user / 2, user / 2, idle / 2, user / 2, user / 2, idle / 2)).unwrap();
    std::fs::write(dir.join("meminfo"), "MemTotal:       32000000 kB\nMemAvailable:   20000000 kB\nSwapTotal:       4000000 kB\n\
                                         SwapFree:        3000000 kB\nHugePages_Total:       0\n").unwrap();
    std::fs::write(dir.join("loadavg"), "1.50 0.75 0.25 2/300 12345\n").unwrap();
    std::fs::write(dir.join("diskstats"), format!(
        "   8       0 sda 100 0 {sectors_read} 0 50 0 {} 0 0 0 0\n   8       1 sda1 100 0 999999 0 50 0 999999 0 0 0 0\n\
         7       0 loop0 1 0 999999 0 1 0 999999 0 0 0 0\n", sectors_read * 2)).unwrap();
}

fn reading(dir: &std::path::Path, at: f64, cpu_ns: u64, rx: u64) -> Reading {
    let container = |id: &str, name: &str, kind: Option<&'static str>, convo: Option<&str>, cpu: u64| Sampled {
        id: id.into(), name: name.into(), image: "img".into(), state: "running".into(), status: "Up".into(), kind,
        item: convo.map(|_| "W-1".to_string()), conversation: convo.map(str::to_string),
        numbers: Numbers { cpu, mem: 2 << 30, rx, tx: rx / 2, read: 0, write: rx, pids: 7 }, published: vec![],
    };
    let mut apps = container("d1", "demo-web-1", Some("container"), Some("C1"), cpu_ns / 4);
    apps.published = vec![("0.0.0.0".into(), 5173, "tcp".into())];
    Reading {
        at, cpu: host::read_cpu(dir), mem: host::read_meminfo(dir), load: host::read_loadavg(dir),
        disk: host::read_diskstats(dir, None),
        net: [("eth0".to_string(), (rx, rx / 2)), ("lo".to_string(), (rx * 10, rx * 10))].into(),
        containers: vec![container("c1", "agent-conv-C1", Some("session"), Some("C1"), cpu_ns),
                         container("r1", "agent-control", Some("runner"), None, cpu_ns / 10),
                         container("n1", "agent-docker", None, None, cpu_ns), apps],
        listeners: vec![("C1".into(), Some("W-1".into()), 3000), ("C1".into(), Some("W-1".into()), 3000)],
    }
}

#[test]
fn snapshot_from_proc_and_docker() {
    let dir = tempfile::tempdir().unwrap();
    let (a, b) = (dir.path().join("a"), dir.path().join("b"));
    proc_files(&a, 1000, 9000, 2000);
    proc_files(&b, 1400, 9200, 4000); // busy +800 of +1000 jiffies; 2000 sectors read in 5 s
    let prev = reading(&a, 10.0, 10_000_000_000, 1000);
    let now = reading(&b, 15.0, 15_000_000_000, 6000);
    let snap = host::build(&prev, &now, 1_800_000_000.0, None);
    assert_eq!(snap["cpu"]["percent"], 80.0);
    assert_eq!(snap["cpu"]["cores"], 2);
    assert_eq!(snap["cpu"]["per_core"], json!([80.0, 80.0]));
    assert_eq!(snap["cpu"]["load"], json!([1.5, 0.75, 0.25]));
    assert_eq!(snap["memory"]["used"], 12_000_000u64 * 1024);
    assert_eq!(snap["memory"]["swap_used"], 1_000_000u64 * 1024);
    assert_eq!(snap["disk_io"]["read_bps"], 2000.0 * 512.0 / 5.0, "whole disks only: partitions and loops left out");
    assert_eq!((&snap["network"]["rx_bps"], &snap["network"]["tx_bps"]), (&json!(1000.0), &json!(500.0)), "lo left out");
    let consumers = snap["consumers"].as_array().unwrap();
    assert_eq!(consumers.iter().map(|c| c["key"].as_str().unwrap()).collect::<Vec<_>>(),
               ["session:C1", "container:demo-web-1", "runner"], "most CPU first; the apps' Docker counts by its containers");
    let session = &consumers[0];
    assert_eq!((&session["name"], &session["item"], &session["processes"]), (&json!("C1"), &json!("W-1"), &json!(7)));
    assert_eq!(session["cpu_percent"], 50.0, "5 s of CPU in 5 s over 2 cores");
    assert_eq!(snap["containers"].as_array().unwrap().len(), 4, "every container listed");
    let ports = snap["ports"].as_array().unwrap();
    assert_eq!(ports.len(), 2, "a port reported twice is listed once");
    assert_eq!((&ports[0]["port"], &ports[0]["consumer"]), (&json!(3000), &json!("session:C1")));
    assert_eq!((&ports[1]["port"], &ports[1]["consumer"], &ports[1]["conversation"]),
               (&json!(5173), &json!("container:demo-web-1"), &json!("C1")));
    let mut store = HostStore::default();
    store.add(&snap, 1_800_000_000.0);
    let view = host::host_view(&store, 1_800_000_001.0);
    assert_eq!(view["history"][0]["cpu_by_kind"]["session"], 50.0);
    assert_eq!(view["history"][0]["cpu_by_kind"]["host"], 80.0 - 50.0 - 12.5 - 5.0);
}

#[test]
fn disk_space_by_conversation() {
    let dir = tempfile::tempdir().unwrap();
    let work = dir.path();
    for (path, bytes) in [("scratch/C1/big.bin", 200_000), ("scratch/C2/a.txt", 10), ("home/.npm/x", 50_000),
                          ("home/notes", 10), ("run/C1/log", 10)] {
        std::fs::create_dir_all(work.join(path).parent().unwrap()).unwrap();
        std::fs::write(work.join(path), vec![b'x'; bytes]).unwrap();
    }
    let space = host::measure_space(&[work.to_path_buf()], work, std::time::Duration::from_secs(10), 5.0);
    assert_eq!(space["complete"], true);
    assert_eq!(space["volumes"][0]["path"], work.to_string_lossy().as_ref());
    let top = space["top"].as_array().unwrap();
    let names: Vec<&str> = top.iter().map(|r| r["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["npm cache", "home (other)", "run", "C1", "C2"]);
    assert_eq!(top[3]["conversation"], "C1");
    assert!(top[3]["bytes"].as_u64().unwrap() >= 200_000);
}
