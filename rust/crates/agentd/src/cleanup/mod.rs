//! Freeing disk in the conversations' folders (`<work>/scratch/<conversation>`) without losing anything the agent made.
//! A folder quiet for `[cleanup] idle_days` loses what can be rebuilt: installed dependencies and caches anywhere in it,
//! and clones whose work is all on their remote (see `scan`). Files the agent wrote and clones with work of their own
//! stay, and `CLEANUP.md` in the folder says what went and how to get each clone back. Removal is permanent.
//!
//! A folder is left alone while it is in use: a turn queued, running, parked or waiting out a usage limit, an active
//! long project, a running spawned agent, a running or kept container, a stack in the agent's Docker or one of its
//! services running from it, or any activity in the last `idle_days` (the folder's top, or any clone's git use). That
//! is checked again right before anything in it moves.

pub mod docker;
pub mod remove;
pub mod scan;

use std::collections::{BTreeMap, HashMap, HashSet};
use std::future::Future;
use std::io::Write as _;
use std::os::fd::AsRawFd;
use std::path::{Component, Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration as StdDuration, Instant};

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Duration, NaiveDateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};
use serde::Serialize;

use crate::db::Db;
use crate::keeps;
use scan::Verdict;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The note left in each folder the cleanup changed.
pub const NOTES: &str = "CLEANUP.md";
/// A pass starts nothing new after this long; the next pass carries on.
const BUDGET: StdDuration = StdDuration::from_secs(50 * 60);
/// How long the agent's Docker's answer counts before it's asked again.
const DOCKER_FRESH: StdDuration = StdDuration::from_secs(60);
/// Dependency folders named one by one in the note; the rest are counted.
const LISTED: usize = 20;
/// What `Apps::containers` prints for each container: its state, name, compose project, compose folder and mounts.
pub const PS_FORMAT: &str = "{{.State}}\t{{.Names}}\t{{.Label \"com.docker.compose.project\"}}\t\
                             {{.Label \"com.docker.compose.project.working_dir\"}}\t{{.Mounts}}";
const END: &str = "__END__";
const HEADER: &str = "# Cleanup\n\nagentd removes what can be rebuilt from conversation folders that have been quiet for a \
while: installed dependencies, caches, and clones whose work is all on their remote. Files written here and clones with \
work of their own are never removed. It also stops this folder's stacks in the agent's Docker once no session uses \
them, and removes their containers and volumes (never a database without a person's say-so) once they sit unused. Each \
section below says what it did and how to get things back.\n";

/// The agent's own Docker, where its stacks run as root with the work volume at the same path as here.
pub trait Apps: Send + Sync {
    /// `docker ps -a` in `PS_FORMAT`, one line per container, then `__END__` once the listing is complete.
    fn containers(&self) -> BoxFuture<'_, Result<String>>;
    /// Move `from` (inside a conversation's folder) to `to` (a new name in the trash) as root, for what agentd's own
    /// user can't move (files the agent's stacks made as root).
    fn move_as_root(&self, from: PathBuf, to: PathBuf) -> BoxFuture<'_, Result<()>>;
    /// Remove `path` (in the trash) as root, for what agentd's own user can't delete.
    fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>>;
    /// `docker <args>` there: its output, or an error when it fails.
    fn docker(&self, args: Vec<String>) -> BoxFuture<'_, Result<String>>;
    /// `docker inspect` of these containers (one that vanished meanwhile is left out).
    fn inspect(&self, ids: Vec<String>) -> BoxFuture<'_, Result<String>>;
    /// `docker volume inspect` of these volumes (one that vanished meanwhile is left out).
    fn volume_inspect(&self, names: Vec<String>) -> BoxFuture<'_, Result<String>>;
    /// The command lines (one process per line) in a conversation's running session container, or in the agent's
    /// services container (None).
    fn processes(&self, conversation: Option<&str>) -> BoxFuture<'_, Result<String>>;
    /// `== <pid>` then `/proc/<pid>/net/dev`, `tcp` and `tcp6` for each container process: its own network.
    fn network(&self, pids: Vec<u32>) -> BoxFuture<'_, Result<String>>;
    /// How many bytes each container's own writable layer holds.
    fn writable_sizes(&self, ids: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, u64>>>;
    /// `docker diff` of each container: the files it added or changed in its own layer, one per line.
    fn changed_files(&self, ids: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, String>>>;
    /// For each volume mountpoint: whether it holds database files, and how many bytes it takes.
    fn volume_contents(&self, mountpoints: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, (bool, u64)>>>;
}

/// Starting a turn of the agent's own, for the questions only people can answer.
pub trait Ask: Send + Sync {
    /// Queue `text` as a fresh unattended turn; returns its work item.
    fn ask(&self, text: &str) -> Result<String>;
    /// Each project's Slack channel, by project name.
    fn project_channels(&self) -> HashMap<String, String>;
}

/// The real thing: a turn in a fresh `V-cleanup-` conversation, marked as the cleanup's.
pub struct Jensen(pub Arc<crate::shared::Shared>);

impl Ask for Jensen {
    fn ask(&self, text: &str) -> Result<String> {
        crate::ask::ask(&self.0, None, "V-cleanup-", text, Some("cleanup"), None)
    }
    fn project_channels(&self) -> HashMap<String, String> {
        self.0.packs.iter().filter(|(_, p)| !p.project.slack_channel.is_empty())
            .map(|(name, p)| (name.clone(), p.project.slack_channel.clone())).collect()
    }
}

#[derive(Debug, Clone)]
pub struct Settings {
    /// The work volume: `scratch` holds the folders; the trash and the lock sit beside it.
    pub work: PathBuf,
    pub idle: Duration,
    pub budget: StdDuration,
}

impl Settings {
    pub fn new(work: &Path, idle_days: i64) -> Settings {
        Settings { work: work.to_path_buf(), idle: Duration::days(idle_days), budget: BUDGET }
    }
    pub fn scratch(&self) -> PathBuf {
        self.work.join("scratch")
    }
    pub fn trash(&self) -> PathBuf {
        self.work.join(".cleanup-trash")
    }
    fn services(&self) -> PathBuf {
        self.work.join("services.toml")
    }
    fn lock(&self) -> PathBuf {
        self.work.join(".cleanup.lock")
    }
}

#[derive(Debug, Default, Serialize)]
pub struct Report {
    pub dry_run: bool,
    /// Why the pass didn't run, when it didn't.
    pub skipped: Option<String>,
    /// Folders the pass changed (or would change), and quiet ones holding clones it kept.
    pub folders: Vec<FolderReport>,
    pub in_use: usize,
    pub recent: usize,
    /// Quiet folders already cleaned with no activity since.
    pub unchanged: usize,
    pub left_for_next_pass: usize,
    pub freed_bytes: u64,
    /// Clones kept, by reason.
    pub kept_clones: BTreeMap<String, usize>,
    pub failures: Vec<String>,
    /// The agent's Docker: stacks stopped, containers and volumes removed, databases asked about.
    pub docker: Option<docker::DockerReport>,
    /// The work volume's free space before and after the pass.
    pub free_before: u64,
    pub free_after: u64,
}

#[derive(Debug, Default, Serialize)]
pub struct FolderReport {
    pub folder: String,
    pub quiet_since: String,
    pub deps: Vec<String>,
    pub clones_removed: Vec<String>,
    pub clones_kept: Vec<String>,
    pub bytes: u64,
    #[serde(skip)]
    kept_kinds: Vec<&'static str>,
    #[serde(skip)]
    problems: Vec<String>,
}

impl Report {
    /// One line for the log.
    pub fn summary(&self) -> String {
        if let Some(why) = &self.skipped {
            return format!("cleanup skipped: {why}");
        }
        let changed = self.folders.iter().filter(|f| !f.deps.is_empty() || !f.clones_removed.is_empty()).count();
        let kept = self.kept_clones.iter().map(|(k, n)| format!("{k} {n}")).collect::<Vec<_>>().join(", ");
        let (to, gone, ask) = if self.dry_run { ("to stop", "to remove", "to ask about") } else { ("stopped", "removed", "asked about") };
        let docker = self.docker.as_ref().map(|d| format!(
            "; the agent's Docker: {} stacks {to}, {} containers, {} volumes and {} images {gone}, {} databases {ask}{}, kept {} \
             containers with data of their own and {} volumes of unknown use, build cache {}, {} problems",
            d.stopped_stacks.len(), d.removed_containers.len(), d.removed_volumes.len(), d.removed_images, d.asked_about.len(),
            if d.to_ask_later > 0 { format!(" ({} for the next daily question)", d.to_ask_later) } else { String::new() },
            d.kept_containers.len(), d.kept_volumes, d.build_cache, d.failures.len())).unwrap_or_default();
        format!("cleanup{}: {changed} folders {}, {:.1} GB {}; {} in use, {} active in the last idle period, {} already \
                 clean, {} left for the next pass; clones kept: {}; {} problems{docker}; disk free {:.1} GB -> {:.1} GB",
                if self.dry_run { " (dry run)" } else { "" }, if self.dry_run { "to clean" } else { "cleaned" },
                self.freed_bytes as f64 / 1e9, if self.dry_run { "to free" } else { "freed" }, self.in_use, self.recent,
                self.unchanged, self.left_for_next_pass, if kept.is_empty() { "none".into() } else { kept },
                self.failures.len(), self.free_before as f64 / 1e9, self.free_after as f64 / 1e9)
    }
}

/// One pass: the agent's Docker first (stacks nobody uses stop, so their folders can be cleaned), then every folder,
/// the quietest first, for at most `settings.budget`.
pub async fn pass(db: Db, apps: Arc<dyn Apps>, ask: Option<Arc<dyn Ask>>, settings: Settings, now: DateTime<Utc>,
                  dry_run: bool) -> Result<Report> {
    let _lock = if dry_run {
        None
    } else {
        match Lock::take(&settings.lock())? {
            Some(lock) => Some(lock),
            None => return Ok(Report { dry_run, skipped: Some("another pass is running".into()), ..Default::default() }),
        }
    };
    let free_before = free_space(&settings.work);
    let services = match std::fs::read_to_string(settings.services()) {
        Ok(text) => Ok(text),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(anyhow::Error::from(e).context("reading the services file")),
    };
    let docker = match services {
        Ok(services) => docker::step(&db, apps.as_ref(), ask.as_deref(), &settings, &services, now, dry_run).await,
        Err(e) => Err(e),
    };
    let docker = match docker {
        Ok(d) => d,
        Err(e) => docker::DockerReport { failures: vec![format!("the agent's Docker: {e:#}")], ..Default::default() },
    };
    let work = settings.work.clone();
    let handle = tokio::runtime::Handle::current();
    let (tx, rx) = tokio::sync::oneshot::channel();
    let (thread_apps, thread_db) = (apps.clone(), db.clone());
    std::thread::Builder::new().name("cleanup".into()).stack_size(32 << 20).spawn(move || {
        lower_priority();
        let mut in_use = InUse { apps: thread_apps, handle, services: settings.services(), at: None, busy: HashSet::new() };
        let _ = tx.send(run(&thread_db, &settings, &mut in_use, now, dry_run));
    })?;
    let (mut report, needs_root) = rx.await.map_err(|_| anyhow!("the cleanup thread stopped"))??;
    // only ever what is already in the trash
    for path in needs_root {
        if let Err(e) = apps.remove_as_root(path.clone()).await {
            report.failures.push(format!("removing {} as root: {e:#}", path.display()));
        }
    }
    report.docker = Some(docker);
    (report.free_before, report.free_after) = (free_before, free_space(&work));
    Ok(report)
}

/// The free space on the filesystem holding `path` (0 when it can't be read).
fn free_space(path: &Path) -> u64 {
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()) else { return 0 };
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: a valid C string and a statvfs we own.
    if unsafe { libc::statvfs(c.as_ptr(), &mut s) } != 0 {
        return 0;
    }
    s.f_bavail as u64 * s.f_frsize as u64
}

/// Which folders the agent's Docker's containers and the agent's services use, read again once older than
/// `DOCKER_FRESH` or when asked for a fresh answer.
struct InUse {
    apps: Arc<dyn Apps>,
    handle: tokio::runtime::Handle,
    services: PathBuf,
    at: Option<Instant>,
    busy: HashSet<String>,
}

impl InUse {
    fn busy(&mut self, db: &Db, scratch: &Path, now: DateTime<Utc>, fresh: bool) -> Result<&HashSet<String>> {
        if fresh || self.at.is_none_or(|at| at.elapsed() >= DOCKER_FRESH) {
            let listing = self.handle.block_on(self.apps.containers()).context("asking the agent's Docker what its containers use")?;
            let kept: Vec<String> = keeps::all(&db.lock())?.into_iter().filter(|k| k.kind == "stack" && k.holds(now))
                .map(|k| k.target).collect();
            let mut busy = in_use_from_ps(&listing, scratch, &kept)?;
            let services = match std::fs::read_to_string(&self.services) {
                Ok(text) => text,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
                Err(e) => return Err(e).context("reading the services file"),
            };
            busy.extend(services_in_use(&services, scratch));
            self.busy = busy;
            self.at = Some(Instant::now());
        }
        Ok(&self.busy)
    }

    fn move_as_root(&self, from: &Path, to: &Path) -> Result<()> {
        self.handle.block_on(self.apps.move_as_root(from.to_path_buf(), to.to_path_buf()))
    }
}

fn run(db: &Db, st: &Settings, in_use: &mut InUse, now: DateTime<Utc>, dry_run: bool) -> Result<(Report, Vec<PathBuf>)> {
    let started = Instant::now();
    let mut report = Report { dry_run, ..Default::default() };
    let mut root = Vec::new();
    let scratch = st.scratch();
    match std::fs::symlink_metadata(&scratch) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok((report, root)),
        Err(e) => return Err(e.into()),
        Ok(m) if m.file_type().is_symlink() || !m.is_dir() => bail!("{} isn't a plain folder", scratch.display()),
        Ok(_) => {}
    }
    if !dry_run {
        remove::plain_dir(&st.trash())?;
        empty_trash(&st.trash(), &mut root);
    }
    // no answer from the agent's Docker: no pass
    let busy = in_use.busy(db, &scratch, now, true)?.clone();
    let mut quiet = Vec::new();
    for e in std::fs::read_dir(&scratch)?.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') || !e.file_type().is_ok_and(|t| t.is_dir()) {
            continue;
        }
        if busy.contains(&name) {
            report.in_use += 1;
            continue;
        }
        let touched = modified(&e.path()); // read before the DB is locked: the disk can be slow
        let c = db.lock();
        match state(&c, &name, &e.path(), now, st.idle, touched) {
            Err(err) => {
                report.in_use += 1;
                report.failures.push(format!("{name}: {err:#}"));
            }
            Ok(State::Busy(_)) => report.in_use += 1,
            Ok(State::Recent) => report.recent += 1,
            Ok(State::Quiet(at)) => match cleaned_since(&c, &name, at) {
                Ok(true) => report.unchanged += 1,
                Ok(false) => quiet.push((at, name, e.path())),
                Err(err) => report.failures.push(format!("{name}: {err:#}")),
            },
        }
    }
    quiet.sort();
    for (i, (at, name, path)) in quiet.iter().enumerate() {
        if started.elapsed() >= st.budget {
            report.left_for_next_pass = quiet.len() - i;
            break;
        }
        match in_use.busy(db, &scratch, now, false) {
            Err(e) => {
                report.failures.push(format!("{e:#}"));
                report.left_for_next_pass = quiet.len() - i;
                break;
            }
            Ok(busy) if busy.contains(name) => {
                report.in_use += 1;
                continue;
            }
            Ok(_) => {}
        }
        match clean(db, st, in_use, name, path, *at, now, dry_run, &mut root) {
            Ok(Done::Cleaned(mut f)) => {
                report.freed_bytes += f.bytes;
                for kind in &f.kept_kinds {
                    *report.kept_clones.entry(kind.to_string()).or_default() += 1;
                }
                report.failures.extend(f.problems.drain(..).map(|p| format!("{name}: {p}")));
                report.folders.push(f);
            }
            Ok(Done::Nothing) => {}
            Ok(Done::Recent) => report.recent += 1,
            Ok(Done::InUse) => report.in_use += 1,
            Err(e) => report.failures.push(format!("{name}: {e:#}")),
        }
    }
    Ok((report, root))
}

/// What became of a quiet folder.
enum Done {
    /// Changed (or would be, in a dry run), or kept clones worth reporting.
    Cleaned(FolderReport),
    Nothing,
    /// One of its clones was used within the idle period.
    Recent,
    /// It came into use meanwhile.
    InUse,
}

/// Clean one quiet folder.
#[allow(clippy::too_many_arguments)]
fn clean(db: &Db, st: &Settings, in_use: &mut InUse, name: &str, folder: &Path, quiet_since: DateTime<Utc>,
         now: DateTime<Utc>, dry_run: bool, root: &mut Vec<PathBuf>) -> Result<Done> {
    let found = scan::scan(folder);
    // git use inside a clone (a checkout, a commit, a fetch) counts as activity, whoever did it
    if let Some(used) = found.repos.iter().filter_map(|r| scan::last_used(&r.path)).max().map(DateTime::<Utc>::from)
        && now - used < st.idle {
        return Ok(Done::Recent);
    }
    let judged: Vec<(PathBuf, Verdict)> = found.repos.iter().filter(|r| !r.worktree).map(|r| {
        let verdict = if r.path == folder {
            Verdict::Keep { kind: "folder", detail: "the conversation's folder is itself a clone".into() }
        } else if found.unreadable.iter().any(|u| u.starts_with(&r.path)) {
            Verdict::Keep { kind: "unreadable", detail: "it holds folders that can't be read".into() }
        } else {
            scan::judge(&r.path, &st.work)
        };
        (r.path.clone(), verdict)
    }).collect();
    // a clone holding a worktree or a clone that stays, stays too
    let keeper = |p: &Path| found.repos.iter().any(|r| r.path == p && r.worktree)
        || judged.iter().any(|(q, v)| q == p && matches!(v, Verdict::Keep { .. }));
    let rel = |p: &Path| p.strip_prefix(folder).unwrap_or(p).to_string_lossy().into_owned();
    let mut report = FolderReport { folder: name.to_string(), quiet_since: keeps::fmt(quiet_since), ..Default::default() };
    let mut removed: Vec<(PathBuf, String, String, String)> = Vec::new();
    for (path, verdict) in &judged {
        let verdict = match verdict {
            Verdict::Remove { .. } if found.repos.iter().any(|r| r.path != *path && r.path.starts_with(path) && keeper(&r.path)) =>
                Verdict::Keep { kind: "nested", detail: "it holds another clone or worktree with work of its own".into() },
            v => v.clone(),
        };
        match verdict {
            Verdict::Remove { url, branch, head } => removed.push((path.clone(), url, branch, head)),
            Verdict::Keep { kind, detail } => {
                report.kept_kinds.push(kind);
                report.clones_kept.push(format!("`{}`: {detail}", rel(path)));
            }
        }
    }
    let timed_out = report.kept_kinds.contains(&scan::TIMED_OUT);
    // every removed clone goes in the note; one inside another that's removed goes with it
    let clones: Vec<(String, String, String, String)> =
        removed.iter().map(|(p, url, branch, head)| (rel(p), url.clone(), branch.clone(), head.clone())).collect();
    let outer: Vec<PathBuf> = removed.iter().map(|r| r.0.clone()).collect();
    removed.retain(|r| !outer.iter().any(|o| *o != r.0 && r.0.starts_with(o)));
    let inside_removed = |p: &Path| removed.iter().any(|r| p.starts_with(&r.0));
    let mut deps: Vec<PathBuf> = found.deps.iter().filter(|d| !inside_removed(d)).cloned().collect();
    // inside a repo, a dependency folder git tracks stays
    let mut tracked = HashSet::new();
    for repo in &found.repos {
        let mine: Vec<PathBuf> = deps.iter().filter(|d| deepest_repo(&found.repos, d) == Some(repo.path.as_path())).cloned().collect();
        if !mine.is_empty() {
            tracked.extend(scan::tracked(&repo.path, &mine).unwrap_or_else(|_| mine.iter().cloned().collect()));
        }
    }
    deps.retain(|d| !tracked.contains(d));
    report.deps = deps.iter().map(|d| rel(d)).collect();
    report.clones_removed = removed.iter().map(|r| rel(&r.0)).collect();

    let targets: Vec<PathBuf> = removed.iter().map(|r| r.0.clone()).chain(deps.iter().cloned()).collect();
    if targets.is_empty() {
        if !dry_run && !timed_out {
            record(&db.lock(), name, 0, now)?;
        }
        return Ok(if report.clones_kept.is_empty() { Done::Nothing } else { Done::Cleaned(report) });
    }
    if dry_run {
        report.bytes = targets.iter().map(|t| remove::tree_bytes(t)).sum();
        return Ok(Done::Cleaned(report));
    }
    // asked again right before anything moves: the DB, the folder's top, and the agent's Docker and services
    let touched = modified(folder);
    match state(&db.lock(), name, folder, now, st.idle, touched)? {
        State::Quiet(_) => {}
        State::Recent => return Ok(Done::Recent),
        State::Busy(_) => return Ok(Done::InUse),
    }
    if in_use.busy(db, &st.scratch(), now, true)?.contains(name) {
        return Ok(Done::InUse);
    }
    // everything moves out at once, so the folder never holds anything half deleted
    let (trash, scratch) = (st.trash(), st.scratch());
    let mut moved = Vec::new();
    let mut failed_moves = false;
    for t in &targets {
        if !remove::inside(t, &scratch, 2) {
            report.problems.push(format!("not moving {}: not a plain path inside a conversation's folder", rel(t)));
            failed_moves = true;
            continue;
        }
        let dest = remove::trash_path(t, &trash);
        match std::fs::rename(t, &dest) {
            Ok(()) => moved.push(dest),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => match in_use.move_as_root(t, &dest) {
                Ok(()) => moved.push(dest),
                Err(e) => {
                    report.problems.push(format!("moving {} as root: {e:#}", rel(t)));
                    failed_moves = true;
                }
            },
            Err(e) => {
                report.problems.push(format!("moving {}: {e}", rel(t)));
                failed_moves = true;
            }
        }
    }
    if let Err(e) = write_notes(folder, quiet_since, now, &report.deps, &clones, &report.clones_kept) {
        report.problems.push(format!("writing {NOTES}: {e}"));
    }
    for m in moved {
        let d = remove::delete_tree(&m);
        report.bytes += d.bytes;
        if d.failed > 0 {
            root.push(m);
        }
    }
    // a folder git timed out on, or something in it couldn't move, is looked at again next pass
    if !timed_out && !failed_moves {
        record(&db.lock(), name, report.bytes, now)?;
    }
    Ok(Done::Cleaned(report))
}

fn deepest_repo<'a>(repos: &'a [scan::Repo], path: &Path) -> Option<&'a Path> {
    repos.iter().filter(|r| path.starts_with(&r.path)).map(|r| r.path.as_path()).max_by_key(|p| p.components().count())
}

/// Delete what an earlier pass left in the trash (checked to be a plain folder); what agentd can't delete goes to
/// `root`.
fn empty_trash(trash: &Path, root: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(trash).into_iter().flatten().flatten() {
        if remove::delete_tree(&e.path()).failed > 0 {
            root.push(e.path());
        }
    }
}

// --- whether a folder is in use --------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    /// In use, and why.
    Busy(String),
    /// Active within the idle period.
    Recent,
    /// Quiet since then.
    Quiet(DateTime<Utc>),
}

/// Whether the folder `folder` (at `path`) is in use at `now`: by its own conversation, or by any conversation working
/// inside it. `touched` is when the folder's top last changed (see `modified`).
pub fn state(c: &Connection, folder: &str, path: &Path, now: DateTime<Utc>, idle: Duration, touched: Option<DateTime<Utc>>)
             -> Result<State> {
    let p = path.to_string_lossy().into_owned();
    let mut ids: Vec<String> = c.prepare("SELECT id FROM conversations WHERE id = ?1 OR cwd = ?2 \
                                          OR substr(cwd, 1, length(?2) + 1) = ?2 || '/'")?
        .query_map(params![folder, p], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    ids.extend(c.prepare("SELECT child_conversation_id FROM spawned_agents WHERE cwd = ?1 \
                          OR substr(cwd, 1, length(?1) + 1) = ?1 || '/'")?
        .query_map(params![p], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?);
    ids.push(folder.to_string());
    ids.sort();
    ids.dedup();
    let marks = vec!["?"; ids.len()].join(", ");
    let ask = |sql: &str| -> Result<Vec<Option<String>>> {
        let n = sql.matches("{ids}").count();
        Ok(c.prepare(&sql.replace("{ids}", &marks))?
            .query_map(params_from_iter(std::iter::repeat_n(ids.iter(), n).flatten()), |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    };
    let any = |sql: &str| -> Result<bool> { Ok(!ask(sql)?.is_empty()) };
    let busy = |why: &str| Ok(State::Busy(why.to_string()));
    if any("SELECT id FROM work_items WHERE status IN ('queued', 'claimed', 'delivered') AND conversation_id IN ({ids})")? {
        return busy("a turn is queued or running");
    }
    if any("SELECT conversation_id FROM parks WHERE ended_at IS NULL AND conversation_id IN ({ids})")? {
        return busy("it is parked");
    }
    if any("SELECT conversation_id FROM limit_waits WHERE resumed_at IS NULL AND conversation_id IN ({ids})")? {
        return busy("a turn is waiting out a usage limit");
    }
    if any("SELECT name FROM projects WHERE status = 'active' AND conversation_id IN ({ids})")? {
        return busy("it runs a long project");
    }
    if any("SELECT id FROM spawned_agents WHERE status = 'running' AND (parent_conversation_id IN ({ids}) \
            OR child_conversation_id IN ({ids}))")? {
        return busy("a spawned agent is running");
    }
    if any("SELECT conversation_id FROM agentd_containers WHERE stopped_at IS NULL AND conversation_id IN ({ids})")? {
        return busy("its container is running");
    }
    for id in &ids {
        if keeps::holds_container(c, id, now)? {
            return busy("someone kept its container");
        }
    }
    // the cleanup's own changes to the folder (moving things out, its note) aren't activity
    let accounted: Option<String> = c.query_row("SELECT activity_at FROM agentd_cleanups WHERE folder = ?", [folder], |r| r.get(0))
        .optional()?;
    let accounted = accounted.as_deref().and_then(parse_time);
    let touched = touched.filter(|t| accounted.is_none_or(|a| *t > a));
    let mut times = ask("SELECT last_active_at FROM conversations WHERE id IN ({ids})")?;
    times.extend(ask("SELECT last_turn_at FROM agentd_containers WHERE conversation_id IN ({ids})")?);
    times.extend(ask("SELECT MAX(updated_at) FROM work_items WHERE conversation_id IN ({ids})")?);
    // a time that can't be read counts as now: never as long ago
    let latest = times.iter().flatten().map(|t| parse_time(t).unwrap_or(now)).chain(touched).max()
        .unwrap_or(now);
    Ok(if now - latest < idle { State::Recent } else { State::Quiet(latest) })
}

fn parse_time(s: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(s).map(|t| t.with_timezone(&Utc)).ok()
        .or_else(|| NaiveDateTime::parse_from_str(s.trim_end_matches('Z'), "%Y-%m-%dT%H:%M:%S%.f").ok().map(|t| t.and_utc()))
        .or_else(|| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f").ok().map(|t| t.and_utc()))
}

/// When the folder or anything at its top was last changed.
pub fn modified(path: &Path) -> Option<DateTime<Utc>> {
    // the cleanup's own note isn't activity
    let entries = std::fs::read_dir(path).into_iter().flatten().flatten().filter(|e| e.file_name() != NOTES).map(|e| e.path());
    std::iter::once(path.to_path_buf()).chain(entries)
        .filter_map(|p| std::fs::symlink_metadata(p).and_then(|m| m.modified()).ok())
        .map(DateTime::<Utc>::from).max()
}

/// The folders the agent's Docker's containers use: a running (or kept) container's compose folder and bind mounts.
pub fn in_use_from_ps(text: &str, scratch: &Path, kept_stacks: &[String]) -> Result<HashSet<String>> {
    // a line's trailing tab is an empty field (no mounts), so lines aren't trimmed
    let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    let Some((last, containers)) = lines.split_last().filter(|(last, _)| last.trim() == END) else {
        bail!("the listing didn't finish: {}", text.chars().take(300).collect::<String>());
    };
    let _ = last;
    let mut used = HashSet::new();
    for line in containers {
        let f: Vec<&str> = line.split('\t').collect();
        let &[state, name, project, dir, mounts] = f.as_slice() else { bail!("can't read the line {line:?}") };
        let kept = kept_stacks.iter().any(|k| k == project || k == name);
        if matches!(state, "exited" | "created" | "dead") && !kept {
            continue;
        }
        for p in std::iter::once(dir).chain(mounts.split(',')) {
            let p = Path::new(p);
            if p.is_absolute() && scratch.starts_with(p) {
                bail!("{name} uses every conversation's folder ({})", p.display());
            }
            if let Some(folder) = folder_of(p, scratch) {
                used.insert(folder);
            }
        }
    }
    Ok(used)
}

/// The conversation's folder a path lies in, if any.
fn folder_of(path: &Path, scratch: &Path) -> Option<String> {
    match path.strip_prefix(scratch).ok()?.components().next()? {
        Component::Normal(first) => Some(first.to_string_lossy().into_owned()),
        _ => None,
    }
}

/// The folders the agent's services (its services file) run in or name in their commands.
pub fn services_in_use(text: &str, scratch: &Path) -> HashSet<String> {
    let prefix = format!("{}/", scratch.display());
    let mut used = HashSet::new();
    for entry in crate::workloads::service_entries(text) {
        for word in entry.cwd.iter().chain(entry.cmd.iter()) {
            for (at, _) in word.match_indices(&prefix) {
                let rest = &word[at + prefix.len()..];
                let first = rest.split(|c: char| c == '/' || c.is_whitespace() || "'\";:=,".contains(c)).next().unwrap_or_default();
                if !first.is_empty() && first != "." && first != ".." {
                    used.insert(first.to_string());
                }
            }
        }
    }
    used
}

// --- bookkeeping ---------------------------------------------------------------------------------------------------

/// Whether the folder was cleaned after its last activity `at`.
fn cleaned_since(c: &Connection, folder: &str, at: DateTime<Utc>) -> Result<bool> {
    let done: Option<String> = c.query_row("SELECT activity_at FROM agentd_cleanups WHERE folder = ?", [folder], |r| r.get(0))
        .optional()?;
    Ok(done.and_then(|d| parse_time(&d)).is_some_and(|d| at <= d))
}

/// Note the folder as cleaned at the pass's time `now`. Activity up to then (or up to the clock, whichever is later)
/// is accounted for, so the cleanup's own changes to the folder don't count as activity.
fn record(c: &Connection, folder: &str, bytes: u64, now: DateTime<Utc>) -> Result<()> {
    c.execute("INSERT INTO agentd_cleanups(folder, cleaned_at, activity_at, freed_bytes) VALUES (?1, ?2, ?3, ?4)
               ON CONFLICT(folder) DO UPDATE SET cleaned_at = ?2, activity_at = ?3, freed_bytes = freed_bytes + ?4",
              params![folder, keeps::fmt(Utc::now()), keeps::fmt(now.max(Utc::now())), bytes as i64])?;
    Ok(())
}

fn write_notes(folder: &Path, quiet_since: DateTime<Utc>, now: DateTime<Utc>, deps: &[String],
               clones: &[(String, String, String, String)], kept: &[String]) -> std::io::Result<()> {
    let mut text = format!("This folder had been quiet since {}.\n", quiet_since.format("%Y-%m-%d %H:%M UTC"));
    if !deps.is_empty() {
        text += "\nRemoved installed dependencies and caches. The project's install command brings them back:\n";
        for d in deps.iter().take(LISTED) {
            text += &format!("- `{d}`\n");
        }
        if deps.len() > LISTED {
            text += &format!("- and {} more\n", deps.len() - LISTED);
        }
    }
    if !clones.is_empty() {
        text += "\nRemoved clones whose work is all on their remote. To get one back:\n";
        for (rel, url, branch, head) in clones {
            let at = if branch == "HEAD" { head.as_str() } else { branch.as_str() };
            text += &format!("- `{rel}` ({branch} at {}): `git clone {} {} && git -C {} checkout {}`\n",
                             &head[..head.len().min(12)], quote(url), quote(rel), quote(rel), quote(at));
        }
    }
    if !kept.is_empty() {
        text += "\nKept clones with work of their own:\n";
        for k in kept {
            text += &format!("- {k}\n");
        }
    }
    append_note(folder, now, text.trim_end())
}

/// Add a dated section to the folder's `CLEANUP.md` (made with its header when missing), leaving the folder's own
/// modification time as it was: the note isn't activity.
pub fn append_note(folder: &Path, now: DateTime<Utc>, text: &str) -> std::io::Result<()> {
    let path = folder.join(NOTES);
    let before = std::fs::metadata(folder).and_then(|m| m.modified()).ok();
    let mut out = if path.exists() { String::new() } else { HEADER.to_string() };
    out += &format!("\n## {}\n\n{text}\n", now.format("%Y-%m-%d %H:%M UTC"));
    std::fs::OpenOptions::new().create(true).append(true).open(&path)?.write_all(out.as_bytes())?;
    if let Some(t) = before {
        let _ = std::fs::File::open(folder).and_then(|f| f.set_modified(t));
    }
    Ok(())
}

/// `s` as one shell word.
fn quote(s: &str) -> String {
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "_./:@%+=,-".contains(c)) {
        s.to_string()
    } else {
        format!("'{}'", s.replace('\'', r"'\''"))
    }
}

/// The pass's thread runs at the lowest CPU priority and the lowest best-effort disk priority; the git commands it
/// starts inherit both. (Not the idle disk class: on a disk that is never quiet it would never run.)
fn lower_priority() {
    // SAFETY: plain syscalls on the calling thread with constant arguments.
    unsafe {
        let tid = libc::syscall(libc::SYS_gettid) as libc::id_t;
        libc::setpriority(libc::PRIO_PROCESS, tid, 19);
        const IOPRIO_WHO_PROCESS: libc::c_long = 1;
        const IOPRIO_CLASS_BE: libc::c_long = 2;
        libc::syscall(libc::SYS_ioprio_set, IOPRIO_WHO_PROCESS, 0 as libc::c_long, (IOPRIO_CLASS_BE << 13) | 7);
    }
}

/// An exclusive lock on a file, held until dropped.
struct Lock(std::fs::File);

impl Drop for Lock {
    /// Released outright: closing the file alone leaves it held while a process being started at that moment still
    /// has a copy of the descriptor.
    fn drop(&mut self) {
        // SAFETY: flock on a descriptor we own.
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

impl Lock {
    /// None when someone else holds it.
    fn take(path: &Path) -> Result<Option<Lock>> {
        let file = std::fs::OpenOptions::new().create(true).truncate(false).write(true).open(path)
            .with_context(|| format!("opening {}", path.display()))?;
        // SAFETY: flock on a descriptor we own.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(Some(Lock(file)));
        }
        let e = std::io::Error::last_os_error();
        if e.kind() == std::io::ErrorKind::WouldBlock { Ok(None) } else { Err(e.into()) }
    }
}

// --- the real agent's Docker -------------------------------------------------------------------------------------

/// The agent's Docker, reached by `docker exec` into its container (its API listens only inside it).
pub struct DockerApps {
    docker: bollard::Docker,
    container: String,
    /// The agent's services container (agent-services).
    services: String,
    scratch: PathBuf,
    trash: PathBuf,
}

impl DockerApps {
    pub fn new(container: &str, services: &str, settings: &Settings) -> Result<DockerApps> {
        Ok(DockerApps { docker: bollard::Docker::connect_with_local_defaults()?, container: container.to_string(),
                        services: services.to_string(),
                        scratch: settings.scratch(), trash: settings.trash() })
    }

    async fn exec(&self, cmd: Vec<String>, limit: StdDuration) -> Result<String> {
        crate::workloads::exec_output(&self.docker, &self.container, None, cmd, limit).await
    }

    /// `script` (with `args` as `$1`...) in `container`, which must print `__END__` last to count as done; returns
    /// what came before it.
    async fn finished(&self, container: &str, user: Option<&str>, script: &str, args: Vec<String>, limit: StdDuration)
                      -> Result<String> {
        let cmd = ["sh".to_string(), "-c".into(), format!("{script} && echo {END}"), "sh".into()].into_iter().chain(args).collect();
        let out = crate::workloads::exec_output(&self.docker, container, user, cmd, limit).await?;
        match out.trim_end().strip_suffix(END) {
            Some(text) => Ok(text.to_string()),
            None => bail!("{}", out.trim().chars().take(400).collect::<String>()),
        }
    }

    /// `path` is a new name right in the trash, which is a plain folder.
    fn in_trash(&self, path: &Path) -> bool {
        path.parent() == Some(self.trash.as_path()) && remove::inside(&self.trash, self.trash.parent().unwrap_or(&self.trash), 1)
            && path.file_name().is_some_and(|n| !n.is_empty() && n != "." && n != "..")
    }
}

impl Apps for DockerApps {
    fn containers(&self) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move {
            // the format goes in as an argument, so nothing in it is read by the shell
            let cmd = vec!["sh".to_string(), "-c".into(), format!("docker ps -a --no-trunc --format \"$1\" && echo {END}"),
                           "sh".into(), PS_FORMAT.into()];
            self.exec(cmd, StdDuration::from_secs(120)).await
        })
    }

    fn move_as_root(&self, from: PathBuf, to: PathBuf) -> BoxFuture<'_, Result<()>> {
        Box::pin(async move {
            if !remove::inside(&from, &self.scratch, 2) || !self.in_trash(&to) || std::fs::symlink_metadata(&to).is_ok() {
                bail!("won't move {} to {}", from.display(), to.display());
            }
            let cmd = vec!["mv".into(), "--".into(), from.to_string_lossy().into_owned(), to.to_string_lossy().into_owned()];
            let out = self.exec(cmd, StdDuration::from_secs(300)).await?;
            if std::fs::symlink_metadata(&from).is_ok() || std::fs::symlink_metadata(&to).is_err() {
                bail!("mv didn't move it: {}", out.trim());
            }
            Ok(())
        })
    }

    fn docker(&self, args: Vec<String>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move { self.finished(&self.container, None, "docker \"$@\"", args, StdDuration::from_secs(600)).await })
    }

    fn inspect(&self, ids: Vec<String>) -> BoxFuture<'_, Result<String>> {
        // one that vanished since the listing only gets an error line on stderr, which is dropped
        Box::pin(async move { self.finished(&self.container, None, "docker inspect \"$@\" 2>/dev/null; true", ids, StdDuration::from_secs(300)).await })
    }

    fn volume_inspect(&self, names: Vec<String>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move {
            self.finished(&self.container, None, "docker volume inspect \"$@\" 2>/dev/null; true", names, StdDuration::from_secs(300)).await
        })
    }

    fn processes(&self, conversation: Option<&str>) -> BoxFuture<'_, Result<String>> {
        let conversation = conversation.map(str::to_string);
        Box::pin(async move {
            let name = match &conversation {
                Some(c) => crate::containers::container_name(c)?,
                None => self.services.clone(),
            };
            let script = r#"for f in /proc/[0-9]*/cmdline; do tr '\0' ' ' < "$f" 2>/dev/null; echo; done; true"#;
            self.finished(&name, Some("1001"), script, Vec::new(), StdDuration::from_secs(60)).await
        })
    }

    fn network(&self, pids: Vec<u32>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move {
            let script = r#"for p in "$@"; do echo "== $p"; cat /proc/$p/net/dev /proc/$p/net/tcp /proc/$p/net/tcp6 2>/dev/null; done; true"#;
            self.finished(&self.container, None, script, pids.iter().map(u32::to_string).collect(), StdDuration::from_secs(120)).await
        })
    }

    fn changed_files(&self, ids: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, String>>> {
        Box::pin(async move {
            let script = r#"for id in "$@"; do echo "== $id"; timeout 120 docker diff "$id" 2>/dev/null || echo "? unreadable"; done; true"#;
            let out = self.finished(&self.container, None, script, ids, StdDuration::from_secs(1800)).await?;
            let mut files: HashMap<String, String> = HashMap::new();
            let mut current = None;
            for line in out.lines() {
                if let Some(id) = line.strip_prefix("== ") {
                    current = Some(id.trim().to_string());
                    files.entry(id.trim().to_string()).or_default();
                } else if let Some(id) = &current {
                    // a diff that can't be read counts as holding a database: the container stays
                    let line = if line.starts_with("? unreadable") { "/unreadable.db" } else { line.get(2..).unwrap_or_default() };
                    files.entry(id.clone()).or_default().push_str(&format!("{line}\n"));
                }
            }
            Ok(files)
        })
    }

    fn writable_sizes(&self, ids: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, u64>>> {
        Box::pin(async move {
            let out = self.finished(&self.container, None,
                                    "timeout 600 docker inspect --size --format '{{.Id}} {{.SizeRw}}' \"$@\" 2>/dev/null; true",
                                    ids, StdDuration::from_secs(660)).await?;
            Ok(out.lines().filter_map(|l| {
                let (id, size) = l.split_once(' ')?;
                Some((id.to_string(), size.trim().parse().ok()?))
            }).collect())
        })
    }

    fn volume_contents(&self, mountpoints: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, (bool, u64)>>> {
        Box::pin(async move {
            // a missing or unreadable mountpoint counts as a database: never removed without a person's say-so
            let script = r#"for m in "$@"; do db=0; [ -d "$m" ] && [ -r "$m" ] || db=1; [ -n "$(timeout 120 find "$m" -maxdepth 4 \( -name PG_VERSION -o -name ibdata1 -o -name 'WiredTiger*' -o -name dump.rdb -o -name 'appendonly*' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db' -o -name '*.mdb' -o -name '*.duckdb' -o -name 'neostore*' -o -name meta.properties -o -name influxd.bolt -o -name .minio.sys -o -name mnesia -o -name _state -o -path '*/member/wal' \) 2>/dev/null | head -n 1)" ] && db=1; k=$(timeout 300 du -sk "$m" 2>/dev/null | cut -f1); printf '%s\t%s\t%s\n' "$m" "$db" "${k:-0}"; done; true"#;
            let out = self.finished(&self.container, None, script, mountpoints, StdDuration::from_secs(1800)).await?;
            Ok(out.lines().filter_map(|l| {
                let mut f = l.split('\t');
                let (m, db, k) = (f.next()?, f.next()?, f.next()?);
                Some((m.to_string(), (db == "1", k.trim().parse::<u64>().unwrap_or(0) * 1024)))
            }).collect())
        })
    }

    fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>> {
        Box::pin(async move {
            if !self.in_trash(&path) {
                bail!("{} isn't in the trash", path.display());
            }
            let cmd = vec!["rm".into(), "-rf".into(), "--".into(), path.to_string_lossy().into_owned()];
            let out = self.exec(cmd, StdDuration::from_secs(1800)).await?;
            if std::fs::symlink_metadata(&path).is_ok() {
                bail!("still there after rm: {}", out.trim());
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests;
