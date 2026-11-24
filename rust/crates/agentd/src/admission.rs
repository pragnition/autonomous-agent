//! Whether there is room for one more turn: fewer turns running than `[limits] max_concurrent_sessions`, and at
//! least `[containers] min_free_gib` of memory available on the host.

use std::path::PathBuf;

pub struct Admission {
    pub max_sessions: usize,
    pub min_free_gib: u64,
    /// The host's /proc/meminfo (a container reads the host's).
    pub meminfo: PathBuf,
}

impl Admission {
    pub fn room(&self, running: usize) -> bool {
        running < self.max_sessions && self.available_kib().is_none_or(|kib| kib >= self.min_free_gib << 20)
    }

    /// `MemAvailable` in KiB; `None` when it can't be read.
    fn available_kib(&self) -> Option<u64> {
        let text = std::fs::read_to_string(&self.meminfo).ok()?;
        let line = text.lines().find_map(|l| l.strip_prefix("MemAvailable:"))?;
        line.split_whitespace().next()?.parse().ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admission_refuses_when_memory_low() {
        let dir = tempfile::tempdir().unwrap();
        let meminfo = dir.path().join("meminfo");
        let admission = Admission { max_sessions: 2, min_free_gib: 3, meminfo: meminfo.clone() };
        std::fs::write(&meminfo, "MemTotal:       32000000 kB\nMemFree:  100000 kB\nMemAvailable:    3000000 kB\n").unwrap();
        assert!(!admission.room(0), "under 3 GiB available");
        std::fs::write(&meminfo, "MemTotal:       32000000 kB\nMemAvailable:    3200000 kB\n").unwrap();
        assert!(admission.room(1));
        assert!(!admission.room(2), "at the session cap");
        std::fs::remove_file(&meminfo).unwrap();
        assert!(admission.room(0), "memory not known: only the cap counts");
    }
}
