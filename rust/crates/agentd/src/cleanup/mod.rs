//! Freeing disk in the conversations' folders (`<work>/scratch/<conversation>`) without losing anything the agent made.
//! A folder quiet for `[cleanup] idle_days` loses what can be rebuilt: installed dependencies and caches anywhere in it,
//! and clones whose work is all on their remote (see `scan`). Files the agent wrote and clones with work of their own
//! stay, and `CLEANUP.md` in the folder says what went and how to get each clone back. Removal is permanent.
//!
//! A folder is left alone while it is in use: a turn queued or running, a park, an active long project, a running
//! spawned agent, a running or kept container, a stack in the agent's Docker running from it, or any activity in the
//! last `idle_days`. That is checked again right before anything in it moves.

pub mod remove;
pub mod scan;

use std::collections::{BTreeMap, HashSet};
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
work of their own are never removed. Each section below says what went and how to get it back.\n";

/// The agent's own Docker, where its stacks run as root with the work volume at the same path as here.
pub trait Apps: Send + Sync {
    /// `docker ps -a` in `PS_FORMAT`, one line per container, then `__END__` once the listing is complete.
    fn containers(&self) -> BoxFuture<'_, Result<String>>;
    /// Remove `path` as root, for what agentd's own user can't delete.
    fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>>;
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
        format!("cleanup{}: {changed} folders {}, {:.1} GB {}; {} in use, {} active in the last idle period, {} already \
                 clean, {} left for the next pass; clones kept: {}; {} problems",
                if self.dry_run { " (dry run)" } else { "" }, if self.dry_run { "to clean" } else { "cleaned" },
                self.freed_bytes as f64 / 1e9, if self.dry_run { "to free" } else { "freed" }, self.in_use, self.recent,
                self.unchanged, self.left_for_next_pass, if kept.is_empty() { "none".into() } else { kept },
                self.failures.len())
    }
}

/// One pass over every folder: the quietest first, for at most `settings.budget`.
pub async fn pass(db: Db, apps: Arc<dyn Apps>, settings: Settings, now: DateTime<Utc>, dry_run: bool) -> Result<Report> {
    let _lock = if dry_run {
        None
    } else {
        match Lock::take(&settings.lock())? {
            Some(lock) => Some(lock),
            None => return Ok(Report { dry_run, skipped: Some("another pass is running".into()), ..Default::default() }),
        }
    };
    let handle = tokio::runtime::Handle::current();
    let (tx, rx) = tokio::sync::oneshot::channel();
    let (thread_apps, thread_db) = (apps.clone(), db.clone());
    std::thread::Builder::new().name("cleanup".into()).stack_size(32 << 20).spawn(move || {
        lower_priority();
        let mut docker = DockerView { apps: thread_apps, handle, at: None, busy: HashSet::new() };
        let _ = tx.send(run(&thread_db, &settings, &mut docker, now, dry_run));
    })?;
    let (mut report, needs_root) = rx.await.map_err(|_| anyhow!("the cleanup thread stopped"))??;
    for path in needs_root {
        if let Err(e) = apps.remove_as_root(path.clone()).await {
            report.failures.push(format!("removing {} as root: {e:#}", path.display()));
        }
    }
    Ok(report)
}

/// The agent's Docker's view of which folders its containers use, asked again once it's older than `DOCKER_FRESH`.
struct DockerView {
    apps: Arc<dyn Apps>,
    handle: tokio::runtime::Handle,
    at: Option<Instant>,
    busy: HashSet<String>,
}

impl DockerView {
    fn busy(&mut self, db: &Db, scratch: &Path, now: DateTime<Utc>) -> Result<&HashSet<String>> {
        if self.at.is_none_or(|at| at.elapsed() >= DOCKER_FRESH) {
            let listing = self.handle.block_on(self.apps.containers()).context("asking the agent's Docker what its containers use")?;
            let kept: Vec<String> = keeps::all(&db.lock())?.into_iter().filter(|k| k.kind == "stack" && k.holds(now))
                .map(|k| k.target).collect();
            self.busy = in_use_from_ps(&listing, scratch, &kept)?;
            self.at = Some(Instant::now());
        }
        Ok(&self.busy)
    }
}

fn run(db: &Db, st: &Settings, docker: &mut DockerView, now: DateTime<Utc>, dry_run: bool) -> Result<(Report, Vec<PathBuf>)> {
    let started = Instant::now();
    let mut report = Report { dry_run, ..Default::default() };
    let mut root = Vec::new();
    if !dry_run {
        empty_trash(&st.trash(), &mut root);
    }
    let scratch = st.scratch();
    let entries = match std::fs::read_dir(&scratch) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok((report, root)),
        Err(e) => return Err(e.into()),
    };
    let busy = docker.busy(db, &scratch, now)?.clone();
    let mut quiet = Vec::new();
    for e in entries.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') || !e.file_type().is_ok_and(|t| t.is_dir()) {
            continue;
        }
        if busy.contains(&name) {
            report.in_use += 1;
            continue;
        }
        let c = db.lock();
        match state(&c, &name, &e.path(), now, st.idle) {
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
        // asked again right before anything moves; no answer from the agent's Docker stops the pass
        if docker.busy(db, &scratch, now)?.contains(name) {
            report.in_use += 1;
            continue;
        }
        match clean(db, st, name, path, *at, now, dry_run, &mut root) {
            Ok(Some(mut f)) => {
                report.freed_bytes += f.bytes;
                for kind in &f.kept_kinds {
                    *report.kept_clones.entry(kind.to_string()).or_default() += 1;
                }
                report.failures.extend(f.problems.drain(..).map(|p| format!("{name}: {p}")));
                report.folders.push(f);
            }
            Ok(None) => {}
            Err(e) => report.failures.push(format!("{name}: {e:#}")),
        }
    }
    Ok((report, root))
}

/// Clean one quiet folder. None when there was nothing to report or it came into use meanwhile.
#[allow(clippy::too_many_arguments)]
fn clean(db: &Db, st: &Settings, name: &str, folder: &Path, quiet_since: DateTime<Utc>, now: DateTime<Utc>, dry_run: bool,
         root: &mut Vec<PathBuf>) -> Result<Option<FolderReport>> {
    let found = scan::scan(folder);
    let judged: Vec<(PathBuf, Verdict)> = found.repos.iter().filter(|r| !r.worktree)
        .map(|r| (r.path.clone(), scan::judge(&r.path))).collect();
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
    // a clone inside one being removed goes with it
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
        if !dry_run {
            record(&db.lock(), name, 0, now)?;
        }
        return Ok((!report.clones_kept.is_empty()).then_some(report));
    }
    if dry_run {
        report.bytes = targets.iter().map(|t| remove::tree_bytes(t)).sum();
        return Ok(Some(report));
    }
    if !matches!(state(&db.lock(), name, folder, now, st.idle)?, State::Quiet(_)) {
        return Ok(None);
    }
    // everything moves out at once, so the folder never holds anything half deleted
    let mut moved = Vec::new();
    for t in &targets {
        match remove::to_trash(t, &st.trash()) {
            Ok(p) => moved.push(p),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => root.push(t.clone()),
            Err(e) => report.problems.push(format!("moving {}: {e}", rel(t))),
        }
    }
    let clones: Vec<(String, String, String, String)> =
        removed.iter().map(|(p, url, branch, head)| (rel(p), url.clone(), branch.clone(), head.clone())).collect();
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
    record(&db.lock(), name, report.bytes, now)?;
    Ok(Some(report))
}

fn deepest_repo<'a>(repos: &'a [scan::Repo], path: &Path) -> Option<&'a Path> {
    repos.iter().filter(|r| path.starts_with(&r.path)).map(|r| r.path.as_path()).max_by_key(|p| p.components().count())
}

/// Delete what an earlier pass left in the trash; what agentd can't delete goes to `root`.
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
/// inside it.
pub fn state(c: &Connection, folder: &str, path: &Path, now: DateTime<Utc>, idle: Duration) -> Result<State> {
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
    let mut times = ask("SELECT last_active_at FROM conversations WHERE id IN ({ids})")?;
    times.extend(ask("SELECT last_turn_at FROM agentd_containers WHERE conversation_id IN ({ids})")?);
    times.extend(ask("SELECT MAX(updated_at) FROM work_items WHERE conversation_id IN ({ids})")?);
    // a time that can't be read counts as now: never as long ago
    let latest = times.iter().flatten().map(|t| parse_time(t).unwrap_or(now)).chain(modified(path)).max()
        .unwrap_or(now);
    Ok(if now - latest < idle { State::Recent } else { State::Quiet(latest) })
}

fn parse_time(s: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(s).map(|t| t.with_timezone(&Utc)).ok()
        .or_else(|| NaiveDateTime::parse_from_str(s.trim_end_matches('Z'), "%Y-%m-%dT%H:%M:%S%.f").ok().map(|t| t.and_utc()))
        .or_else(|| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f").ok().map(|t| t.and_utc()))
}

/// When the folder or anything at its top was last changed.
fn modified(path: &Path) -> Option<DateTime<Utc>> {
    let entries = std::fs::read_dir(path).into_iter().flatten().flatten().map(|e| e.path());
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
            if let Ok(rest) = Path::new(p).strip_prefix(scratch)
                && let Some(Component::Normal(first)) = rest.components().next() {
                used.insert(first.to_string_lossy().into_owned());
            }
        }
    }
    Ok(used)
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
    let path = folder.join(NOTES);
    let mut text = if path.exists() { String::new() } else { HEADER.to_string() };
    text += &format!("\n## {}\n\nThis folder had been quiet since {}.\n", now.format("%Y-%m-%d %H:%M UTC"),
                     quiet_since.format("%Y-%m-%d %H:%M UTC"));
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
    std::fs::OpenOptions::new().create(true).append(true).open(path)?.write_all(text.as_bytes())
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
    roots: Vec<PathBuf>,
}

impl DockerApps {
    pub fn new(container: &str, settings: &Settings) -> Result<DockerApps> {
        Ok(DockerApps { docker: bollard::Docker::connect_with_local_defaults()?, container: container.to_string(),
                        roots: vec![settings.scratch(), settings.trash()] })
    }
}

impl Apps for DockerApps {
    fn containers(&self) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move {
            // the format goes in as an argument, so nothing in it is read by the shell
            let cmd = vec!["sh".to_string(), "-c".into(), format!("docker ps -a --no-trunc --format \"$1\" && echo {END}"),
                           "sh".into(), PS_FORMAT.into()];
            crate::workloads::exec_output(&self.docker, &self.container, None, cmd, StdDuration::from_secs(120)).await
        })
    }

    fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>> {
        Box::pin(async move {
            let roots: Vec<&Path> = self.roots.iter().map(PathBuf::as_path).collect();
            if !remove::under(&path, &roots) {
                bail!("{} isn't in the scratch folders or the trash", path.display());
            }
            let cmd = vec!["rm".into(), "-rf".into(), "--".into(), path.to_string_lossy().into_owned()];
            let out = crate::workloads::exec_output(&self.docker, &self.container, None, cmd, StdDuration::from_secs(1800)).await?;
            if std::fs::symlink_metadata(&path).is_ok() {
                bail!("still there after rm: {}", out.trim());
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests;
