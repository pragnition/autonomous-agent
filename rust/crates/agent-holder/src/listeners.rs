//! What is running in this container, read from /proc (the agent image has no `ss`). In a container, /proc shows only
//! the container's own processes, while /proc/net/tcp shows every listener on the network it shares: a listener
//! counts as ours only when one of our processes holds its socket.

use std::collections::{BTreeSet, HashMap};
use std::fs;

fn listening_sockets() -> HashMap<String, u16> {
    let mut out = HashMap::new();
    for path in ["/proc/net/tcp", "/proc/net/tcp6"] {
        let Ok(text) = fs::read_to_string(path) else { continue };
        for row in text.lines().skip(1) {
            let f: Vec<&str> = row.split_whitespace().collect();
            if f.len() > 9 && f[3] == "0A" {
                if let Some(port) = f[1].rsplit(':').next().and_then(|p| u16::from_str_radix(p, 16).ok()) {
                    out.insert(f[9].to_string(), port);
                }
            }
        }
    }
    out
}

fn pids() -> impl Iterator<Item = u32> {
    fs::read_dir("/proc").into_iter().flatten().flatten()
        .filter_map(|e| e.file_name().to_str()?.parse().ok())
}

/// TCP ports a process of this container is listening on.
pub fn owned_here() -> Vec<u16> {
    let sockets = listening_sockets();
    let mut ports = BTreeSet::new();
    for pid in pids() {
        let Ok(fds) = fs::read_dir(format!("/proc/{pid}/fd")) else { continue };
        for fd in fds.flatten() {
            let Ok(target) = fs::read_link(fd.path()) else { continue };
            let target = target.to_string_lossy();
            if let Some(inode) = target.strip_prefix("socket:[").and_then(|t| t.strip_suffix(']')) {
                if let Some(port) = sockets.get(inode) {
                    ports.insert(*port);
                }
            }
        }
    }
    ports.into_iter().collect()
}

/// Live processes besides PID 1, the holder itself and the running Claude Code's process group.
pub fn other_processes(holder: u32, claude_group: Option<u32>) -> u32 {
    pids().filter(|&pid| pid != 1 && pid != holder).filter(|&pid| {
        let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) else { return false };
        let Some(close) = stat.rfind(')') else { return false };
        let fields: Vec<&str> = stat[close + 2..].split_whitespace().collect();
        let (state, ppid, pgid) = (fields.first().copied(), fields.get(1), fields.get(2));
        let zombie = state == Some("Z");
        let ours = ppid.and_then(|p| p.parse::<u32>().ok()) == Some(holder) && claude_group.is_none();
        let claude = pgid.and_then(|p| p.parse::<u32>().ok()).is_some_and(|g| Some(g) == claude_group);
        !zombie && !claude && !ours
    }).count() as u32
}
