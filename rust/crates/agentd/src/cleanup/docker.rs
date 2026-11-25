//! Jensen's own Docker (agent-docker), where its test stacks run. A stack no running Claude Code session uses is
//! stopped; what has sat stopped or unused for the idle period goes (containers, images, build cache, volumes that hold
//! no database); and a database volume is never deleted here: a Jensen turn asks in the project's channel, and Jensen
//! deletes only what a person says is safe.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Component, Path};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};

use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use serde_json::Value;

use super::{Apps, Ask, Settings, State, append_note, modified, state};
use crate::db::Db;
use crate::keeps;

/// A stack keeps running this long after its conversation was last active, even when no session is using it.
pub const STACK_GRACE: Duration = Duration::hours(1);
/// Database questions go out at most this often.
const ASK_EVERY: Duration = Duration::hours(24);
static DB_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)postgres|pg|mysql|maria|mongo|redis|db|data|sql").unwrap());

/// A container in the agent's Docker.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AppContainer {
    pub id: String,
    pub name: String,
    /// Running, restarting or paused.
    pub running: bool,
    /// Its compose project, else its own name.
    pub stack: String,
    /// The conversations' folders it runs from (compose folder) or mounts.
    pub folders: BTreeSet<String>,
    /// The volumes it mounts.
    pub volumes: BTreeSet<String>,
    /// Its published ports on the agent's Docker's host side.
    pub ports: BTreeSet<u16>,
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

#[derive(Debug, Default, Serialize)]
pub struct DockerReport {
    pub stopped_stacks: Vec<String>,
    pub removed_containers: Vec<String>,
    pub removed_volumes: Vec<String>,
    /// Database volumes a Jensen turn was asked to check with people (or, in a dry run, would be).
    pub asked_about: Vec<String>,
    /// Database volumes waiting for the next daily question.
    pub to_ask_later: usize,
    pub images: String,
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

/// The containers from `docker inspect` of every container.
pub fn parse_containers(json: &str, scratch: &Path) -> Result<Vec<AppContainer>> {
    let all: Value = serde_json::from_str(json).context("reading docker inspect")?;
    let mut out = Vec::new();
    for c in all.as_array().into_iter().flatten() {
        let labels = &c["Config"]["Labels"];
        let name = c["Name"].as_str().unwrap_or_default().trim_start_matches('/').to_string();
        let status = c["State"]["Status"].as_str().unwrap_or_default();
        let running = matches!(status, "running" | "restarting" | "paused");
        let mut folders = BTreeSet::new();
        let mut volumes = BTreeSet::new();
        if let Some(dir) = labels["com.docker.compose.project.working_dir"].as_str() {
            folders.extend(folder_of(dir, scratch));
        }
        for m in c["Mounts"].as_array().into_iter().flatten() {
            match m["Type"].as_str() {
                Some("bind") => folders.extend(m["Source"].as_str().and_then(|s| folder_of(s, scratch))),
                Some("volume") => volumes.extend(m["Name"].as_str().map(str::to_string)),
                _ => {}
            }
        }
        let ports = c["NetworkSettings"]["Ports"].as_object().into_iter().flatten()
            .flat_map(|(_, binds)| binds.as_array().into_iter().flatten())
            .filter_map(|b| b["HostPort"].as_str()?.parse().ok()).collect();
        let since = if running { time(&c["State"]["StartedAt"]) } else { time(&c["State"]["FinishedAt"]) }
            .or_else(|| time(&c["Created"])).unwrap_or_else(Utc::now);
        out.push(AppContainer {
            id: c["Id"].as_str().unwrap_or_default().to_string(),
            stack: labels["com.docker.compose.project"].as_str().filter(|p| !p.is_empty()).unwrap_or(&name).to_string(),
            name, running, folders, volumes, ports, since,
        });
    }
    Ok(out)
}

/// The volumes from `docker volume inspect` of every volume.
pub fn parse_volumes(json: &str) -> Result<Vec<AppVolume>> {
    let all: Value = serde_json::from_str(json).context("reading docker volume inspect")?;
    Ok(all.as_array().into_iter().flatten().map(|v| AppVolume {
        name: v["Name"].as_str().unwrap_or_default().to_string(),
        project: v["Labels"]["com.docker.compose.project"].as_str().map(str::to_string),
        // a time that can't be read counts as now: never as long ago
        created: time(&v["CreatedAt"]).unwrap_or_else(Utc::now),
        mountpoint: v["Mountpoint"].as_str().unwrap_or_default().to_string(),
    }).collect())
}

/// The ports of every established TCP connection in `/proc/net/tcp` and `/proc/net/tcp6` text, either end.
pub fn connected_ports(proc_net: &str) -> HashSet<u16> {
    let port = |addr: &str| addr.rsplit(':').next().and_then(|p| u16::from_str_radix(p, 16).ok());
    proc_net.lines().filter_map(|l| {
        let f: Vec<&str> = l.split_whitespace().collect();
        (f.len() > 3 && f[3] == "01").then(|| [port(f[1]), port(f[2])])
    }).flatten().flatten().collect()
}

/// Whether a running session's command lines (one process per line) name the stack, one of its containers, one of
/// its folders or one of its ports. Claude Code's own command line (its system prompt mentions ports) doesn't count.
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

/// How a stack's conversations' folders stand: `in_use` when any is busy or was active within `STACK_GRACE`; `quiet`
/// when all have been quiet for the idle period (a stack with no folder is neither).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Folders {
    pub in_use: bool,
    pub quiet: bool,
    pub known: bool,
}

/// What the step will do, decided from what it read.
#[derive(Debug, Default, PartialEq)]
pub struct Plan {
    /// Stack -> its running containers' ids.
    pub stop: BTreeMap<String, Vec<String>>,
    /// (id, name, stack) of each stopped container to remove.
    pub remove_containers: Vec<(String, String, String)>,
    /// Volumes nothing will use, old and quiet enough: removed, or asked about when they hold a database.
    pub volume_candidates: Vec<AppVolume>,
}

/// Decide what stops and what goes. `folders` says how each stack's folders stand, `used` lists the stacks a running
/// session uses, `kept` the stacks someone kept.
pub fn plan(containers: &[AppContainer], volumes: &[AppVolume], folders: &HashMap<String, Folders>, used: &HashSet<String>,
            kept: &HashSet<String>, now: DateTime<Utc>, idle: Duration) -> Plan {
    let mut stacks: BTreeMap<&str, Vec<&AppContainer>> = BTreeMap::new();
    for c in containers {
        stacks.entry(c.stack.as_str()).or_default().push(c);
    }
    let standing = |s: &str| folders.get(s).copied().unwrap_or_default();
    let mut plan = Plan::default();
    for (&stack, members) in &stacks {
        if kept.contains(stack) || used.contains(stack) {
            continue;
        }
        let f = standing(stack);
        let running: Vec<&&AppContainer> = members.iter().filter(|c| c.running).collect();
        if !running.is_empty() {
            let old = running.iter().all(|c| now - c.since >= idle);
            if !f.in_use && (f.known || old) {
                plan.stop.insert(stack.to_string(), running.iter().map(|c| c.id.clone()).collect());
            }
            continue; // stopped now; its containers go once they have been stopped for the idle period
        }
        if f.in_use || (f.known && !f.quiet) {
            continue;
        }
        for c in members.iter().filter(|c| now - c.since >= idle) {
            plan.remove_containers.push((c.id.clone(), c.name.clone(), stack.to_string()));
        }
    }
    let removed: HashSet<&str> = plan.remove_containers.iter().map(|(id, ..)| id.as_str()).collect();
    let mounted: HashSet<&str> = containers.iter().filter(|c| !removed.contains(c.id.as_str()))
        .flat_map(|c| c.volumes.iter().map(String::as_str)).collect();
    for v in volumes {
        if mounted.contains(v.name.as_str()) || now - v.created < idle {
            continue;
        }
        // the stacks it belongs to: its compose project, and those of the containers that mounted it
        let owners: BTreeSet<&str> = v.project.iter().map(String::as_str)
            .chain(containers.iter().filter(|c| c.volumes.contains(&v.name)).map(|c| c.stack.as_str())).collect();
        let fine = owners.iter().all(|o| !kept.contains(*o) && !used.contains(*o) && {
            let f = standing(o);
            !f.in_use && (!f.known || f.quiet)
        });
        if fine {
            plan.volume_candidates.push(v.clone());
        }
    }
    plan
}

/// One Docker step: read, decide, act (or, in a dry run, only report).
pub async fn step(db: &Db, apps: &dyn Apps, ask: Option<&dyn Ask>, st: &Settings, now: DateTime<Utc>, dry_run: bool)
                  -> Result<DockerReport> {
    let scratch = st.scratch();
    let mut report = DockerReport::default();
    let ids = apps.docker(vec!["ps".into(), "-aq".into(), "--no-trunc".into()]).await?;
    let ids: Vec<String> = ids.split_whitespace().map(str::to_string).collect();
    let containers = if ids.is_empty() {
        Vec::new()
    } else {
        parse_containers(&apps.docker([vec!["inspect".to_string()], ids].concat()).await?, &scratch)?
    };
    let names: Vec<String> = apps.docker(vec!["volume".into(), "ls".into(), "-q".into()]).await?
        .split_whitespace().map(str::to_string).collect();
    let volumes = if names.is_empty() {
        Vec::new()
    } else {
        parse_volumes(&apps.docker([vec!["volume".to_string(), "inspect".into()], names].concat()).await?)?
    };

    // which stacks a running session uses
    let sessions: Vec<String> = db.call(|c| Ok(c.prepare("SELECT conversation_id FROM agentd_containers WHERE stopped_at IS NULL")?
        .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)).await?;
    let mut commands = String::new();
    for convo in &sessions {
        match apps.processes(convo).await {
            Ok(text) => commands += &text,
            // a session whose processes can't be read might use anything: stop nothing this time
            Err(e) => bail!("reading the processes of {convo}'s session: {e:#}"),
        }
    }
    let connected = connected_ports(&apps.connections().await?);
    let mut stacks: BTreeMap<&str, Vec<&AppContainer>> = BTreeMap::new();
    for c in &containers {
        stacks.entry(c.stack.as_str()).or_default().push(c);
    }
    let used: HashSet<String> = stacks.iter().filter(|(stack, members)| {
        referenced(&commands, stack, members, &scratch) || members.iter().any(|c| c.ports.iter().any(|p| connected.contains(p)))
    }).map(|(s, _)| s.to_string()).collect();
    let kept: HashSet<String> = keeps::all(&db.lock())?.into_iter().filter(|k| k.kind == "stack" && k.holds(now))
        .map(|k| k.target).collect();

    // how each stack's folders stand (each folder's top is read before the DB is locked: the disk can be slow)
    let mut folders = HashMap::new();
    for (&stack, members) in &stacks {
        let mine: BTreeSet<&String> = members.iter().flat_map(|c| c.folders.iter()).collect();
        let mut f = Folders { in_use: false, quiet: true, known: !mine.is_empty() };
        for folder in mine {
            let path = scratch.join(folder);
            let touched = modified(&path);
            let c = db.lock();
            f.in_use |= !matches!(state(&c, folder, &path, now, STACK_GRACE, touched)?, State::Quiet(_));
            f.quiet &= matches!(state(&c, folder, &path, now, st.idle, touched)?, State::Quiet(_));
        }
        folders.insert(stack.to_string(), Folders { quiet: f.quiet && f.known, ..f });
    }
    let plan = plan(&containers, &volumes, &folders, &used, &kept, now, st.idle);
    let stack_folders = |stack: &str| -> BTreeSet<String> {
        stacks.get(stack).into_iter().flatten().flat_map(|c| c.folders.iter().cloned()).collect()
    };

    for (stack, ids) in &plan.stop {
        if !dry_run {
            if let Err(e) = apps.docker([vec!["stop".to_string()], ids.clone()].concat()).await {
                report.failures.push(format!("stopping {stack}: {e:#}"));
                continue;
            }
            for folder in stack_folders(stack) {
                note(&scratch.join(&folder), now, &format!(
                    "Stopped the stack `{stack}` in the agent's Docker: no running session was using it. `docker start {}` \
                     starts it again as it was.", ids_to_names(&containers, ids).join(" ")), &mut report);
            }
        }
        report.stopped_stacks.push(stack.clone());
    }
    let mut by_folder: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for chunk in plan.remove_containers.chunks(50) {
        let ids: Vec<String> = chunk.iter().map(|(id, ..)| id.clone()).collect();
        if !dry_run && let Err(e) = apps.docker([vec!["rm".to_string()], ids].concat()).await {
            report.failures.push(format!("removing stopped containers: {e:#}"));
            continue;
        }
        for (_, name, stack) in chunk {
            report.removed_containers.push(name.clone());
            for folder in stack_folders(stack) {
                by_folder.entry(folder).or_default().push(format!("container `{name}`"));
            }
        }
    }

    // volumes: a database is asked about, anything else goes
    let mounts: Vec<String> = plan.volume_candidates.iter().map(|v| v.mountpoint.clone()).collect();
    let databases = if mounts.is_empty() { HashMap::new() } else { apps.volume_contents(mounts).await? };
    let mut to_ask = Vec::new();
    for v in &plan.volume_candidates {
        let (has_db, bytes) = databases.get(&v.mountpoint).copied().unwrap_or((true, 0));
        // an anonymous volume's name is random hex ("...db..." means nothing): only its files count
        let anonymous = v.name.len() == 64 && v.name.chars().all(|c| c.is_ascii_hexdigit());
        if has_db || (!anonymous && DB_NAME.is_match(&v.name)) {
            to_ask.push((v.clone(), bytes));
            continue;
        }
        if !dry_run && let Err(e) = apps.docker(vec!["volume".into(), "rm".into(), v.name.clone()]).await {
            report.failures.push(format!("removing volume {}: {e:#}", v.name));
            continue;
        }
        report.removed_volumes.push(v.name.clone());
        let owners: BTreeSet<String> = v.project.iter().cloned()
            .chain(containers.iter().filter(|c| c.volumes.contains(&v.name)).map(|c| c.stack.clone())).collect();
        for folder in owners.iter().flat_map(|o| stack_folders(o)) {
            by_folder.entry(folder).or_default().push(format!("volume `{}`", v.name));
        }
    }
    if !dry_run {
        for (folder, gone) in &by_folder {
            note(&scratch.join(folder), now, &format!(
                "Removed from the agent's Docker after {} quiet days (nothing used them, and the stack's own setup makes them \
                 again): {}.", st.idle.num_days(), gone.join(", ")), &mut report);
        }
    }
    ask_about(db, ask, &containers, &to_ask, &st.scratch(), now, st.idle, dry_run, &mut report)?;

    let until = format!("until={}h", st.idle.num_hours());
    if dry_run {
        report.images = "not pruned in a dry run".into();
        report.build_cache = "not pruned in a dry run".into();
    } else {
        report.images = reclaimed(apps.docker(vec!["image".into(), "prune".into(), "-a".into(), "-f".into(), "--filter".into(), until.clone()]).await);
        report.build_cache = reclaimed(apps.docker(vec!["builder".into(), "prune".into(), "-f".into(), "--filter".into(), until]).await);
    }
    Ok(report)
}

fn ids_to_names(containers: &[AppContainer], ids: &[String]) -> Vec<String> {
    ids.iter().filter_map(|id| containers.iter().find(|c| &c.id == id).map(|c| c.name.clone())).collect()
}

fn note(folder: &Path, now: DateTime<Utc>, text: &str, report: &mut DockerReport) {
    if let Err(e) = append_note(folder, now, text) {
        report.failures.push(format!("writing a note in {}: {e}", folder.display()));
    }
}

/// Docker's "Total reclaimed space" line, or the error.
fn reclaimed(out: Result<String>) -> String {
    match out {
        Ok(text) => text.lines().find(|l| l.starts_with("Total")).unwrap_or("nothing").trim().to_string(),
        Err(e) => format!("failed: {e:#}"),
    }
}

/// Where a question about a conversation's databases goes: its project's channel, else its own channel (or its
/// parent's, for a spawned agent), with its thread for context.
#[derive(Debug, Clone, PartialEq)]
struct Asked {
    conversation: String,
    channel: Option<String>,
    thread: Option<(String, String)>,
    project: Option<String>,
}

fn where_to_ask(c: &Connection, conversation: &str, project_channels: &HashMap<String, String>) -> Result<Asked> {
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
    Ok(Asked { conversation: conversation.to_string(), channel, thread, project })
}

/// Start one Jensen turn asking people about the database volumes not asked about yet, at most once a day.
#[allow(clippy::too_many_arguments)]
fn ask_about(db: &Db, ask: Option<&dyn Ask>, containers: &[AppContainer], volumes: &[(AppVolume, u64)], scratch: &Path,
             now: DateTime<Utc>, idle: Duration, dry_run: bool, report: &mut DockerReport) -> Result<()> {
    let channels: HashMap<String, String> = ask.map(|a| a.project_channels()).unwrap_or_default();
    let (fresh, lines) = {
        let c = db.lock();
        let mut fresh = Vec::new();
        for (v, bytes) in volumes {
            let asked: Option<String> = c.query_row("SELECT created_at FROM agentd_cleanup_asks WHERE volume = ?", [&v.name],
                                                    |r| r.get(0)).optional()?;
            if asked.as_deref() != Some(keeps::fmt(v.created).as_str()) {
                fresh.push((v, *bytes));
            }
        }
        if fresh.is_empty() {
            return Ok(());
        }
        let last: Option<String> = c.query_row("SELECT MAX(asked_at) FROM agentd_cleanup_asks", [], |r| r.get(0))?;
        if last.and_then(|t| DateTime::parse_from_rfc3339(&t).ok()).is_some_and(|t| now - t.with_timezone(&Utc) < ASK_EVERY) {
            report.to_ask_later = fresh.len();
            return Ok(());
        }
        let mut lines = Vec::new();
        for (v, bytes) in &fresh {
            let owners: BTreeSet<&str> = v.project.iter().map(String::as_str)
                .chain(containers.iter().filter(|k| k.volumes.contains(&v.name)).map(|k| k.stack.as_str())).collect();
            let folders: BTreeSet<&String> = containers.iter().filter(|k| owners.contains(k.stack.as_str()))
                .flat_map(|k| k.folders.iter()).collect();
            let mut place = Vec::new();
            for folder in &folders {
                let a = where_to_ask(&c, folder, &channels)?;
                place.push(format!("conversation {} (folder {}){}{}{}", a.conversation, scratch.join(folder).display(),
                    a.channel.map(|ch| format!(", ask in channel {ch}")).unwrap_or_else(|| ", no channel known".into()),
                    a.thread.map(|(ch, ts)| format!(", its thread {ch}/{ts}")).unwrap_or_default(),
                    a.project.map(|p| format!(", project {p}")).unwrap_or_default()));
            }
            lines.push(format!("- `{}` ({:.1} GB, stack {}, made {}): {}", v.name, *bytes as f64 / 1e9,
                owners.iter().copied().collect::<Vec<_>>().join(", "), v.created.format("%Y-%m-%d"),
                if place.is_empty() { "no conversation folder known".into() } else { place.join("; ") }));
        }
        (fresh, lines)
    };
    report.asked_about.extend(fresh.iter().map(|(v, _)| v.name.clone()));
    let Some(ask) = ask.filter(|_| !dry_run) else { return Ok(()) };
    let text = format!(
"Disk cleanup needs a decision about test databases. These volumes in your Docker hold databases of test stacks that \
have been stopped, with no session using them, for at least {} days; nothing uses them now:

{}

Ask whether they can be deleted, one message per channel listing that channel's databases: in the project's channel \
when one is named, else the channel the conversation came from (link its thread for context). Say what each database \
was for (read the thread or the folder if you need to) and that keeping it costs disk. When a person says one is safe \
to delete, remove it with `docker volume rm <name>`; keep any nobody answers about or wants kept. Delete nothing else.",
        idle.num_days(), lines.join("\n"));
    // queued with the DB unlocked: queueing locks it too
    let item = ask.ask(&text)?;
    let c = db.lock();
    for (v, _) in &fresh {
        c.execute("INSERT INTO agentd_cleanup_asks(volume, created_at, asked_at, work_item) VALUES (?, ?, ?, ?) \
                   ON CONFLICT(volume) DO UPDATE SET created_at = excluded.created_at, asked_at = excluded.asked_at, \
                   work_item = excluded.work_item",
                  params![v.name, keeps::fmt(v.created), keeps::fmt(now), item])?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_containers_reads_stack_folders_volumes_ports_and_times() {
        let json = r#"[{"Id": "a1", "Name": "/rr-x-app-1", "Created": "2026-11-20T00:00:00Z",
            "State": {"Status": "running", "StartedAt": "2026-11-21T00:00:00.5Z", "FinishedAt": "0001-01-01T00:00:00Z"},
            "Config": {"Labels": {"com.docker.compose.project": "rr-x",
                                  "com.docker.compose.project.working_dir": "/var/agent/scratch/V-1/app"}},
            "Mounts": [{"Type": "bind", "Source": "/var/agent/scratch/V-2/data"}, {"Type": "volume", "Name": "rr-x_pg"},
                       {"Type": "bind", "Source": "/var/run/docker.sock"}],
            "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18096"}], "9229/tcp": null}}},
            {"Id": "b2", "Name": "/loose", "Created": "2026-11-19T00:00:00Z",
             "State": {"Status": "exited", "StartedAt": "2026-11-19T00:00:00Z", "FinishedAt": "2026-11-22T00:00:00Z"},
             "Config": {"Labels": null}, "Mounts": [], "NetworkSettings": {"Ports": {}}}]"#;
        let got = parse_containers(json, Path::new("/var/agent/scratch")).unwrap();
        assert_eq!(got[0].stack, "rr-x");
        assert!(got[0].running);
        assert_eq!(got[0].folders, BTreeSet::from(["V-1".to_string(), "V-2".to_string()]));
        assert_eq!(got[0].volumes, BTreeSet::from(["rr-x_pg".to_string()]));
        assert_eq!(got[0].ports, BTreeSet::from([18096]));
        assert_eq!(keeps::fmt(got[0].since), "2026-11-21T00:00:00Z");
        assert_eq!((got[1].stack.as_str(), got[1].running, keeps::fmt(got[1].since)), ("loose", false, "2026-11-22T00:00:00Z".into()));
    }

    #[test]
    fn connected_ports_reads_established_connections_only() {
        let text = "  sl  local_address rem_address   st tx_queue rx_queue\n\
                    0: 0100007F:46B0 0100007F:C350 01 00000000:00000000\n\
                    1: 00000000:46B3 00000000:0000 0A 00000000:00000000\n\
                    0: 00000000000000000000000001000000:9C40 00000000000000000000000001000000:46B1 01 0:0\n";
        assert_eq!(connected_ports(text), HashSet::from([18096, 50000, 40000, 18097]));
    }

    #[test]
    fn a_session_references_a_stack_by_name_folder_or_port() {
        let app = AppContainer { name: "rr-x-app-1".into(), stack: "rr-x".into(), ports: BTreeSet::from([18096]),
                                 folders: BTreeSet::from(["V-1".to_string()]), ..Default::default() };
        let scratch = Path::new("/var/agent/scratch");
        let yes = ["curl -s http://localhost:18096/api", "docker compose -p rr-x logs", "npm test --prefix /var/agent/scratch/V-1/app",
                   "docker exec rr-x-app-1 sh"];
        for line in yes {
            assert!(referenced(line, "rr-x", &[&app], scratch), "{line}");
        }
        let no = ["curl http://localhost:180960", "sleep 118096", "node server.js",
                  "claude --system-prompt use ports 18090-18099 like 18096 --output-format stream-json"];
        for line in no {
            assert!(!referenced(line, "rr-x", &[&app], scratch), "{line}");
        }
    }

    fn at(days: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-12-01T00:00:00Z").unwrap().with_timezone(&Utc) - Duration::days(days)
    }

    fn app(stack: &str, running: bool, days: i64, volumes: &[&str]) -> AppContainer {
        AppContainer { id: format!("{stack}-id"), name: format!("{stack}-1"), running, stack: stack.into(),
                       volumes: volumes.iter().map(|v| v.to_string()).collect(), since: at(days), ..Default::default() }
    }

    #[test]
    fn plan_stops_unused_stacks_and_removes_what_sat_unused() {
        let folders = HashMap::from([
            ("idle".to_string(), Folders { in_use: false, quiet: true, known: true }),
            ("busy".to_string(), Folders { in_use: true, quiet: false, known: true }),
            ("old".to_string(), Folders { in_use: false, quiet: true, known: true }),
            ("lately".to_string(), Folders { in_use: false, quiet: false, known: true }),
        ]);
        let containers = [app("idle", true, 1, &[]), app("busy", true, 9, &[]), app("used", true, 9, &[]), app("kept", true, 9, &[]),
                          app("loose-old", true, 9, &[]), app("loose-new", true, 1, &[]),
                          app("old", false, 5, &["old_pg", "old_nm"]), app("lately", false, 5, &["lately_nm"]),
                          app("just-stopped", false, 1, &[])];
        let volumes = [AppVolume { name: "old_pg".into(), project: Some("old".into()), created: at(9), mountpoint: "/v/old_pg".into() },
                       AppVolume { name: "old_nm".into(), project: Some("old".into()), created: at(9), mountpoint: "/v/old_nm".into() },
                       AppVolume { name: "lately_nm".into(), project: Some("lately".into()), created: at(9), mountpoint: "/v/l".into() },
                       AppVolume { name: "orphan".into(), project: None, created: at(9), mountpoint: "/v/o".into() },
                       AppVolume { name: "new-orphan".into(), project: None, created: at(1), mountpoint: "/v/n".into() }];
        let p = plan(&containers, &volumes, &folders, &HashSet::from(["used".to_string()]), &HashSet::from(["kept".to_string()]),
                     at(0), Duration::days(3));
        assert_eq!(p.stop.keys().cloned().collect::<Vec<_>>(), ["idle", "loose-old"]);
        assert_eq!(p.remove_containers, [("old-id".to_string(), "old-1".to_string(), "old".to_string())]);
        assert_eq!(p.volume_candidates.iter().map(|v| v.name.as_str()).collect::<Vec<_>>(), ["old_pg", "old_nm", "orphan"]);
    }
}
