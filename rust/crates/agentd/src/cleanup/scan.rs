//! What in a conversation's folder can be rebuilt: dependency and cache folders anywhere in it, and clones whose every
//! change is on their remote. Nothing here changes a file, and git runs without the network.

use std::collections::HashSet;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{Result, anyhow, bail};

/// Folders that tools install or fill, removed wherever they are. A `.venv` or `venv` counts only with its
/// `pyvenv.cfg`, and any folder with a cache tag (`CACHEDIR.TAG`) counts too.
pub const DEPENDENCIES: &[&str] = &["node_modules", "__pycache__", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".parcel-cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".pnpm-store", ".venv", "venv"];
/// Generated folders a clone may hold (ignored by git) without that keeping the clone.
const GENERATED: &[&str] = &["dist", "build", "out", "coverage", "target", ".cache", "test-results", "playwright-report",
    "blob-report", ".angular", "storybook-static"];
/// Generated files likewise: by ending, and by name.
const GENERATED_ENDINGS: &[&str] = &[".log", ".pyc", ".tsbuildinfo"];
const GENERATED_NAMES: &[&str] = &[".eslintcache", ".DS_Store"];
const CACHE_TAG: &[u8] = b"Signature: 8a477f597d28d172789f06886806bc55";
const GIT_TIMEOUT: Duration = Duration::from_secs(60);

/// What a folder holds that may go.
#[derive(Debug, Default)]
pub struct Found {
    pub deps: Vec<PathBuf>,
    pub repos: Vec<Repo>,
    /// Folders that couldn't be read: a clone holding one stays, since nothing can say what's in it.
    pub unreadable: Vec<PathBuf>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Repo {
    pub path: PathBuf,
    /// A linked worktree or submodule (`.git` is a file): never removed whole.
    pub worktree: bool,
}

/// Whether a clone can go, and if not, why not.
#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// Everything in it is on `url`; `branch` is "HEAD" when detached.
    Remove { url: String, branch: String, head: String },
    /// `kind` is a short label for counting; `detail` says what was found.
    Keep { kind: &'static str, detail: String },
}

/// git took too long: the clone stays, and the folder is looked at again next time.
pub const TIMED_OUT: &str = "git timed out";

fn keep(kind: &'static str, detail: impl Into<String>) -> Result<Verdict> {
    Ok(Verdict::Keep { kind, detail: detail.into() })
}

/// The dependency folders and repos under `folder`, without following symlinks, entering `.git` or entering a
/// dependency folder.
pub fn scan(folder: &Path) -> Found {
    let mut found = Found::default();
    walk(folder, &mut found);
    found
}

fn walk(dir: &Path, found: &mut Found) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        found.unreadable.push(dir.to_path_buf());
        return;
    };
    let mut subdirs = Vec::new();
    for e in entries {
        let Ok(e) = e else {
            found.unreadable.push(dir.to_path_buf());
            continue;
        };
        let Ok(kind) = e.file_type() else { continue };
        if e.file_name() == ".git" {
            if kind.is_dir() || kind.is_file() {
                found.repos.push(Repo { path: dir.to_path_buf(), worktree: kind.is_file() });
            }
        } else if kind.is_dir() {
            subdirs.push(e.path());
        }
    }
    for path in subdirs {
        if is_dependency(&path) {
            found.deps.push(path);
        } else {
            walk(&path, found);
        }
    }
}

/// A dependency folder, unless a repo is in it (an editable install's checkout, a fixture with a cache tag): that is
/// walked like any other folder instead.
fn is_dependency(dir: &Path) -> bool {
    let name = dir.file_name().unwrap_or_default().to_string_lossy();
    let named = match name.as_ref() {
        ".venv" | "venv" => dir.join("pyvenv.cfg").is_file(),
        n if DEPENDENCIES.contains(&n) => true,
        _ => cache_tagged(dir),
    };
    named && !holds_repo(dir)
}

fn cache_tagged(dir: &Path) -> bool {
    let mut head = [0u8; 43];
    std::fs::File::open(dir.join("CACHEDIR.TAG")).and_then(|mut f| f.read_exact(&mut head)).is_ok() && head == CACHE_TAG
}

/// Whether `dir` or anything under it is a repo (symlinks not followed). A folder that can't be read counts as one.
fn holds_repo(dir: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else { return true };
    let mut subdirs = Vec::new();
    for e in entries {
        let Ok(e) = e else { return true };
        if e.file_name() == ".git" {
            return true;
        }
        if e.file_type().is_ok_and(|t| t.is_dir()) {
            subdirs.push(e.path());
        }
    }
    subdirs.iter().any(|d| holds_repo(d))
}

/// A path `git status --ignored=matching` reports as ignored that can be rebuilt: a folder an ignore rule names and
/// that is a dependency or generated folder by its own name, or a generated file. A file inside some other folder
/// (`build/.env` in a tracked `build`) is judged by its own name.
fn generated(entry: &str) -> bool {
    if let Some(dir) = entry.strip_suffix('/') {
        let name = dir.rsplit('/').next().unwrap_or(dir);
        DEPENDENCIES.contains(&name) || GENERATED.contains(&name)
    } else {
        let name = entry.rsplit('/').next().unwrap_or(entry);
        GENERATED_NAMES.contains(&name) || GENERATED_ENDINGS.iter().any(|e| name.ends_with(e))
    }
}

/// git in one repo, run so that nothing in the repo can make it start a program: no system or global config, no
/// looking above the repo, no fsmonitor, hooks or signature checks, and every filter the repo's config names turned
/// into a plain copy. No locks are taken, nothing prompts, and nothing uses the network.
pub struct Git {
    repo: PathBuf,
    overrides: Vec<String>,
}

impl Git {
    /// For commands that may run filters (status).
    pub fn new(repo: &Path) -> Result<Git> {
        let plain = Git::plain(repo);
        let (code, out, err) = plain.run(&["config", "--get-regexp", r"^filter\."])?;
        if code != 0 && code != 1 {
            bail!("git config: {}", err.trim());
        }
        let mut overrides = Vec::new();
        for key in out.lines().filter_map(|l| l.split_whitespace().next()) {
            let Some((name, _)) = key.strip_prefix("filter.").and_then(|k| k.rsplit_once('.')) else { continue };
            if name.contains('=') {
                bail!("a filter named {name:?}");
            }
            for setting in ["clean=cat", "smudge=cat", "process=", "required=false"] {
                overrides.push(format!("filter.{name}.{setting}"));
            }
        }
        Ok(Git { repo: repo.to_path_buf(), overrides })
    }

    /// For commands that run no filters.
    pub fn plain(repo: &Path) -> Git {
        Git { repo: repo.to_path_buf(), overrides: Vec::new() }
    }

    /// Its output; an error when it fails.
    pub fn out(&self, args: &[&str]) -> Result<String> {
        let (code, out, err) = self.run(args)?;
        if code != 0 {
            bail!("git {}: {}", args.join(" "), err.trim());
        }
        Ok(out)
    }

    /// (exit code, stdout, stderr); an error when it can't run or takes over a minute.
    pub fn run(&self, args: &[&str]) -> Result<(i32, String, String)> {
        let mut cmd = Command::new("git");
        cmd.arg("-C").arg(&self.repo);
        for c in ["core.fsmonitor=false", "core.hooksPath=/dev/null", "log.showSignature=false"].iter().map(|s| s.to_string())
            .chain(self.overrides.iter().cloned()) {
            cmd.args(["-c", &c]);
        }
        let ceiling = self.repo.parent().unwrap_or(&self.repo);
        let mut child = cmd.args(args)
            .env("GIT_OPTIONAL_LOCKS", "0").env("GIT_TERMINAL_PROMPT", "0").env("LC_ALL", "C")
            .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", "/dev/null").env("GIT_CEILING_DIRECTORIES", ceiling)
            .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE")
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
        let drain = |mut r: Box<dyn Read + Send>| std::thread::spawn(move || {
            let mut b = Vec::new();
            let _ = r.read_to_end(&mut b);
            b
        });
        let out = drain(Box::new(child.stdout.take().ok_or_else(|| anyhow!("no stdout"))?));
        let err = drain(Box::new(child.stderr.take().ok_or_else(|| anyhow!("no stderr"))?));
        let deadline = Instant::now() + GIT_TIMEOUT;
        let status = loop {
            if let Some(status) = child.try_wait()? {
                break status;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                bail!("{TIMED_OUT}: git {} took over {} s", args.join(" "), GIT_TIMEOUT.as_secs());
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        let out = out.join().map_err(|_| anyhow!("reading git's output"))?;
        let err = err.join().map_err(|_| anyhow!("reading git's errors"))?;
        Ok((status.code().unwrap_or(-1), String::from_utf8_lossy(&out).into_owned(), String::from_utf8_lossy(&err).into_owned()))
    }
}

/// Whether the clone at `repo` can be removed whole. A remote that is a local folder counts only inside `work` (the
/// work volume), where it lasts. Any git error keeps the clone.
pub fn judge(repo: &Path, work: &Path) -> Verdict {
    try_judge(repo, work).unwrap_or_else(|e| {
        let text = format!("{e:#}");
        Verdict::Keep { kind: if text.starts_with(TIMED_OUT) { TIMED_OUT } else { "git error" },
                        detail: text.lines().next().unwrap_or_default().chars().take(200).collect() }
    })
}

fn try_judge(repo: &Path, work: &Path) -> Result<Verdict> {
    let dot_git = repo.join(".git");
    if ["rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"]
        .iter().any(|m| dot_git.join(m).exists()) {
        return keep("in progress", "a merge, rebase, cherry-pick or bisect is in progress");
    }
    if repo.join(".gitmodules").exists() || dot_git.join("modules").is_dir() {
        return keep("submodules", "it has submodules");
    }
    if std::fs::read_dir(dot_git.join("lfs/objects")).is_ok_and(|mut d| d.next().is_some()) {
        return keep("lfs", "it holds Git LFS files that may not be on its remote");
    }
    let git = Git::new(repo)?;
    if git.out(&["worktree", "list", "--porcelain"])?.lines().filter(|l| l.starts_with("worktree ")).count() > 1 {
        return keep("worktrees", "it has linked worktrees");
    }
    let remotes = git.out(&["remote"])?;
    let Some(remote) = remotes.lines().find(|r| *r == "origin").or_else(|| remotes.lines().next()) else {
        return keep("no remote", "it has no remote");
    };
    let configured = git.out(&["remote", "get-url", remote])?.trim().to_string();
    if let Some(local) = local_path(&configured, repo) {
        let lasts = std::fs::canonicalize(&local).ok().zip(std::fs::canonicalize(work).ok())
            .is_some_and(|(l, w)| l.starts_with(&w) && l != w);
        if !lasts {
            return keep("local remote", format!("its remote is a folder that may not last: {configured}"));
        }
    }
    let url = strip_credentials(&configured);
    if !git.out(&["stash", "list"])?.trim().is_empty() {
        return keep("stash", "it has a stash");
    }
    // every commit HEAD or a local branch holds must be on this clone's own remote. Tags aren't counted: without the
    // network a tag fetched from the remote (an archived branch, say) can't be told from one made here.
    let unpushed = git.out(&["rev-list", "--count", "HEAD", "--branches", "--not", &format!("--remotes={remote}")])?;
    if unpushed.trim() != "0" {
        return keep("unpushed", format!("{} commits not on {remote}", unpushed.trim()));
    }
    let (code, status, err) = git.run(&["status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=normal"])?;
    if code != 0 {
        bail!("git status: {}", err.trim());
    }
    if !err.trim().is_empty() {
        return keep("unreadable", format!("git couldn't read all of it: {}", err.lines().next().unwrap_or_default()));
    }
    let (mut changed, mut untracked, mut ignored) = (Vec::new(), Vec::new(), Vec::new());
    let mut entries = status.split('\0').filter(|e| !e.is_empty());
    while let Some(e) = entries.next() {
        let (code, path) = (e.get(..2).unwrap_or_default(), e.get(3..).unwrap_or_default().to_string());
        match code {
            "!!" if !generated(&path) => ignored.push(path),
            "!!" => {}
            "??" => untracked.push(path),
            _ => {
                if code.starts_with('R') || code.starts_with('C') {
                    entries.next(); // a rename or copy is followed by where it came from
                }
                changed.push(path);
            }
        }
    }
    let few = |v: &[String]| v.iter().take(5).cloned().collect::<Vec<_>>().join(", ") + if v.len() > 5 { ", ..." } else { "" };
    if !changed.is_empty() {
        return keep("uncommitted", format!("uncommitted changes: {}", few(&changed)));
    }
    if !untracked.is_empty() {
        return keep("untracked", format!("untracked files: {}", few(&untracked)));
    }
    if !ignored.is_empty() {
        return keep("ignored", format!("ignored files that can't be rebuilt: {}", few(&ignored)));
    }
    // changes git status doesn't show: files marked assume-unchanged, or skip-worktree files present on disk
    let hidden: Vec<String> = git.out(&["ls-files", "-v", "-z"])?.split('\0').filter_map(|e| {
        let (tag, path) = (e.chars().next()?, e.get(2..)?);
        (tag.is_ascii_lowercase() || (tag == 'S' && std::fs::symlink_metadata(repo.join(path)).is_ok())).then(|| path.to_string())
    }).collect();
    if !hidden.is_empty() {
        return keep("hidden changes", format!("files git is told not to look at: {}", few(&hidden)));
    }
    let branch = git.out(&["rev-parse", "--abbrev-ref", "HEAD"])?.trim().to_string();
    let head = git.out(&["rev-parse", "HEAD"])?.trim().to_string();
    Ok(Verdict::Remove { url, branch, head })
}

/// The folder a remote URL names, when it is a local one (a path or `file://`), resolved against the repo.
fn local_path(url: &str, repo: &Path) -> Option<PathBuf> {
    if let Some((scheme, rest)) = url.split_once("://") {
        return (scheme == "file").then(|| PathBuf::from(rest));
    }
    // `host:path` or `user@host:path`
    if let Some((host, _)) = url.split_once(':')
        && !host.is_empty() && !host.contains('/') {
        return None;
    }
    let path = Path::new(url);
    Some(if path.is_absolute() { path.to_path_buf() } else { repo.join(path) })
}

/// When a repo was last used: its HEAD, index, reflog or last fetch changed (a linked worktree's in its own git folder).
pub fn last_used(repo: &Path) -> Option<std::time::SystemTime> {
    let dot_git = repo.join(".git");
    let dir = if dot_git.is_file() {
        let text = std::fs::read_to_string(&dot_git).ok()?;
        let target = text.trim().strip_prefix("gitdir:")?.trim().to_string();
        let target = Path::new(&target);
        if target.is_absolute() { target.to_path_buf() } else { repo.join(target) }
    } else {
        dot_git
    };
    ["HEAD", "index", "logs/HEAD", "FETCH_HEAD", "ORIG_HEAD"].iter()
        .filter_map(|f| std::fs::symlink_metadata(dir.join(f)).and_then(|m| m.modified()).ok()).max()
}

/// Of `dirs` (inside `repo`), the ones holding files git tracks.
pub fn tracked(repo: &Path, dirs: &[PathBuf]) -> Result<HashSet<PathBuf>> {
    let git = Git::plain(repo);
    let mut found = HashSet::new();
    let rels: Vec<(PathBuf, String)> = dirs.iter()
        .filter_map(|d| Some((d.clone(), d.strip_prefix(repo).ok()?.to_string_lossy().into_owned()))).collect();
    for chunk in rels.chunks(100) {
        let mut args = vec!["ls-files".to_string(), "-z".into(), "--".into()];
        args.extend(chunk.iter().map(|(_, rel)| format!(":(literal){rel}")));
        let out = git.out(&args.iter().map(String::as_str).collect::<Vec<_>>())?;
        for file in out.split('\0').filter(|f| !f.is_empty()) {
            if let Some((dir, _)) = chunk.iter().find(|(_, rel)| file.starts_with(&format!("{rel}/"))) {
                found.insert(dir.clone());
            }
        }
    }
    Ok(found)
}

/// A remote URL without any user name or token in it.
pub fn strip_credentials(url: &str) -> String {
    if let Some((scheme, rest)) = url.split_once("://") {
        let host_end = rest.find('/').unwrap_or(rest.len());
        if let Some(at) = rest[..host_end].rfind('@') {
            return format!("{scheme}://{}", &rest[at + 1..]);
        }
    }
    url.to_string()
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::fs;

    /// `git` in a test, with an identity and no global config.
    pub fn sh(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git").arg("-C").arg(dir)
            .args(["-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"])
            .args(args).env("GIT_CONFIG_GLOBAL", "/dev/null").output().unwrap();
        assert!(out.status.success(), "git {args:?}: {}", String::from_utf8_lossy(&out.stderr));
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    /// A bare remote and a clone of it with one pushed commit on main: (remote, clone).
    pub fn pushed_clone(root: &Path, name: &str) -> (PathBuf, PathBuf) {
        let remote = root.join(format!("{name}-remote.git"));
        let clone = root.join(name);
        fs::create_dir_all(&remote).unwrap();
        sh(&remote, &["init", "--bare", "-q"]);
        sh(root, &["clone", "-q", remote.to_str().unwrap(), clone.to_str().unwrap()]);
        fs::write(clone.join(".gitignore"), "node_modules/\ndist/\n*.log\n.env\nruns/\nvendor/\n").unwrap();
        fs::write(clone.join("app.js"), "1").unwrap();
        sh(&clone, &["add", "-A"]);
        sh(&clone, &["commit", "-qm", "first"]);
        sh(&clone, &["push", "-q", "origin", "main"]);
        (remote, clone)
    }

    fn kind(v: Verdict) -> &'static str {
        match v {
            Verdict::Keep { kind, .. } => kind,
            Verdict::Remove { .. } => "remove",
        }
    }

    #[test]
    fn scan_finds_dependency_folders_and_stops_there() {
        let t = tempfile::tempdir().unwrap();
        let f = t.path();
        for d in ["a/node_modules/x/node_modules", "b/.venv", "c/venv", "d/target/debug", "e/notes", ".git/node_modules"] {
            fs::create_dir_all(f.join(d)).unwrap();
        }
        fs::write(f.join("b/.venv/pyvenv.cfg"), "home = /usr").unwrap();
        fs::write(f.join("d/target/CACHEDIR.TAG"), "Signature: 8a477f597d28d172789f06886806bc55\n# cargo").unwrap();
        fs::create_dir_all(t.path().join("outside/node_modules")).unwrap();
        std::os::unix::fs::symlink(f.join("outside"), f.join("link")).unwrap();
        let mut deps: Vec<_> = scan(f).deps.iter().map(|d| d.strip_prefix(f).unwrap().to_string_lossy().into_owned()).collect();
        deps.sort();
        assert_eq!(deps, ["a/node_modules", "b/.venv", "d/target", "outside/node_modules"]);
    }

    #[test]
    fn scan_does_not_follow_symlinks_or_enter_git() {
        let t = tempfile::tempdir().unwrap();
        let (f, other) = (t.path().join("f"), t.path().join("other"));
        fs::create_dir_all(f.join(".git/node_modules")).unwrap();
        fs::create_dir_all(other.join("node_modules")).unwrap();
        std::os::unix::fs::symlink(&other, f.join("link")).unwrap();
        let found = scan(&f);
        assert!(found.deps.is_empty(), "{:?}", found.deps);
        assert_eq!(found.repos, [Repo { path: f.clone(), worktree: false }]);
    }

    #[test]
    fn scan_tells_worktrees_from_clones() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        sh(&clone, &["worktree", "add", "-q", t.path().join("r/lanes/w").to_str().unwrap()]);
        let mut repos = scan(&clone).repos;
        repos.sort_by(|a, b| a.path.cmp(&b.path));
        assert_eq!(repos, [Repo { path: clone.clone(), worktree: false }, Repo { path: clone.join("lanes/w"), worktree: true }]);
    }

    #[test]
    fn a_pushed_clean_clone_can_go() {
        let t = tempfile::tempdir().unwrap();
        let (remote, clone) = pushed_clone(t.path(), "r");
        fs::create_dir_all(clone.join("node_modules/x")).unwrap();
        fs::create_dir_all(clone.join("dist")).unwrap();
        fs::write(clone.join("dist/app.js"), "1").unwrap();
        fs::write(clone.join("server.log"), "1").unwrap();
        let head = sh(&clone, &["rev-parse", "HEAD"]).trim().to_string();
        assert_eq!(judge(&clone, t.path()), Verdict::Remove { url: remote.to_string_lossy().into_owned(), branch: "main".into(), head });
    }

    /// A name, a change to a pushed clone, and the reason the clone is then kept.
    type Case<'a> = (&'a str, &'a dyn Fn(&Path), &'a str);

    #[test]
    fn keeps_a_clone_with_work_of_its_own() {
        let cases: [Case; 7] = [
            ("untracked", &|c| fs::write(c.join("notes.md"), "mine").unwrap(), "untracked"),
            ("uncommitted", &|c| fs::write(c.join("app.js"), "2").unwrap(), "uncommitted"),
            ("unpushed", &|c| { fs::write(c.join("b.js"), "1").unwrap(); sh(c, &["add", "-A"]); sh(c, &["commit", "-qm", "b"]); }, "unpushed"),
            ("stash", &|c| { fs::write(c.join("app.js"), "3").unwrap(); sh(c, &["stash", "-q"]); }, "stash"),
            ("env", &|c| fs::write(c.join(".env"), "TOKEN=x").unwrap(), "ignored"),
            ("runs", &|c| { fs::create_dir_all(c.join("runs")).unwrap(); fs::write(c.join("runs/out.csv"), "1").unwrap(); }, "ignored"),
            ("no remote", &|c| { sh(c, &["remote", "remove", "origin"]); }, "no remote"),
        ];
        for (name, change, want) in cases {
            let t = tempfile::tempdir().unwrap();
            let (_, clone) = pushed_clone(t.path(), "r");
            change(&clone);
            assert_eq!(kind(judge(&clone, t.path())), want, "{name}");
        }
    }

    #[test]
    fn keeps_a_clone_with_a_linked_worktree_or_another_clone_inside() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        sh(&clone, &["worktree", "add", "-q", t.path().join("w").to_str().unwrap()]);
        assert_eq!(kind(judge(&clone, t.path())), "worktrees");

        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        let (_, inner) = pushed_clone(t.path(), "inner");
        fs::create_dir_all(clone.join("vendor")).unwrap();
        fs::rename(&inner, clone.join("vendor/inner")).unwrap();
        assert_eq!(kind(judge(&clone, t.path())), "ignored", "a clone in an ignored folder is not something git can rebuild");
    }

    #[test]
    fn a_repo_without_commits_is_kept() {
        let t = tempfile::tempdir().unwrap();
        sh(t.path(), &["init", "-q"]);
        sh(t.path(), &["remote", "add", "origin", "https://example.com/r.git"]);
        assert_eq!(kind(judge(t.path(), t.path())), "git error");
    }

    #[test]
    fn tracked_finds_dependency_folders_git_tracks() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        fs::create_dir_all(clone.join("vendored/node_modules/x")).unwrap();
        fs::write(clone.join("vendored/node_modules/x/i.js"), "1").unwrap();
        sh(&clone, &["add", "-f", "vendored"]);
        fs::create_dir_all(clone.join("node_modules/y")).unwrap();
        let dirs = [clone.join("vendored/node_modules"), clone.join("node_modules")];
        assert_eq!(tracked(&clone, &dirs).unwrap(), HashSet::from([clone.join("vendored/node_modules")]));
    }

    #[test]
    fn generated_paths() {
        for p in ["node_modules/", "web/node_modules/", "dist/", "a/b.log", "x.pyc", ".DS_Store", "app/.venv/"] {
            assert!(generated(p), "{p}");
        }
        for p in [".env", "runs/", "vendor/inner/", "notes.md", "build/.env", "out/a.csv", "dist/x.env"] {
            assert!(!generated(p), "{p}");
        }
    }

    #[test]
    fn keeps_a_clone_whose_ignored_files_only_look_generated() {
        // a tracked build/ holding an ignored .env, and an untracked out/ holding only ignored data
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        fs::create_dir_all(clone.join("build")).unwrap();
        fs::write(clone.join("build/keep.txt"), "1").unwrap();
        sh(&clone, &["add", "build/keep.txt"]);
        sh(&clone, &["commit", "-qm", "build"]);
        sh(&clone, &["push", "-q", "origin", "main"]);
        fs::write(clone.join("build/.env"), "TOKEN=x").unwrap();
        assert_eq!(kind(judge(&clone, t.path())), "ignored");

        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        fs::write(clone.join(".gitignore"), "node_modules/\ndist/\n*.log\n*.csv\n").unwrap();
        sh(&clone, &["commit", "-qam", "csv"]);
        sh(&clone, &["push", "-q", "origin", "main"]);
        fs::create_dir_all(clone.join("out")).unwrap();
        fs::write(clone.join("out/results.csv"), "1").unwrap();
        assert_eq!(kind(judge(&clone, t.path())), "ignored");
    }

    #[test]
    fn keeps_a_clone_git_cannot_fully_read() {
        // SAFETY: geteuid has no preconditions.
        if unsafe { libc::geteuid() } == 0 {
            return; // root reads everything
        }
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        fs::create_dir_all(clone.join("pgdata")).unwrap();
        fs::write(clone.join("pgdata/only-copy.sql"), "1").unwrap();
        fs::set_permissions(clone.join("pgdata"), std::os::unix::fs::PermissionsExt::from_mode(0o000)).unwrap();
        let verdict = kind(judge(&clone, t.path()));
        let found = scan(&clone);
        fs::set_permissions(clone.join("pgdata"), std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        assert_eq!(verdict, "unreadable");
        assert_eq!(found.unreadable, [clone.join("pgdata")]);
    }

    #[test]
    fn keeps_a_clone_with_changes_git_status_hides() {
        for flag in ["--skip-worktree", "--assume-unchanged"] {
            let t = tempfile::tempdir().unwrap();
            let (_, clone) = pushed_clone(t.path(), "r");
            sh(&clone, &["update-index", flag, "app.js"]);
            fs::write(clone.join("app.js"), "changed").unwrap();
            assert_eq!(kind(judge(&clone, t.path())), "hidden changes", "{flag}");
        }
    }

    #[test]
    fn commits_count_only_on_the_clones_own_remote() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        let other = t.path().join("other.git");
        fs::create_dir_all(&other).unwrap();
        sh(&other, &["init", "--bare", "-q"]);
        sh(&clone, &["remote", "add", "other", other.to_str().unwrap()]);
        sh(&clone, &["checkout", "-qb", "feature"]);
        fs::write(clone.join("f.js"), "1").unwrap();
        sh(&clone, &["add", "-A"]);
        sh(&clone, &["commit", "-qm", "feature"]);
        sh(&clone, &["push", "-q", "other", "feature"]);
        assert_eq!(kind(judge(&clone, t.path())), "unpushed", "pushed only to another remote");

        // a tag the remote has, on a commit no branch holds (an archived branch), doesn't keep the clone
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        sh(&clone, &["checkout", "-qb", "old"]);
        fs::write(clone.join("t.js"), "1").unwrap();
        sh(&clone, &["add", "-A"]);
        sh(&clone, &["commit", "-qm", "archived"]);
        sh(&clone, &["tag", "-a", "-m", "v", "archive/old"]);
        sh(&clone, &["push", "-q", "origin", "archive/old"]);
        sh(&clone, &["checkout", "-q", "main"]);
        sh(&clone, &["branch", "-qD", "old"]);
        assert_eq!(kind(judge(&clone, t.path())), "remove");
    }

    #[test]
    fn a_local_remote_counts_only_inside_the_work_volume() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        let work = t.path().join("work");
        fs::create_dir_all(&work).unwrap();
        assert_eq!(kind(judge(&clone, &work)), "local remote", "the remote is outside the work volume");
        sh(&clone, &["remote", "set-url", "origin", t.path().join("gone.git").to_str().unwrap()]);
        assert_eq!(kind(judge(&clone, t.path())), "local remote", "the remote is gone");
        assert_eq!(local_path("git@github.com:o/r.git", &clone), None);
        assert_eq!(local_path("https://github.com/o/r.git", &clone), None);
        assert_eq!(local_path("file:///srv/r.git", &clone), Some(PathBuf::from("/srv/r.git")));
        assert_eq!(local_path("../r.git", &clone), Some(clone.join("../r.git")));
    }

    #[test]
    fn judging_runs_nothing_the_repo_names() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        let marker = t.path().join("ran");
        sh(&clone, &["config", "filter.x.clean", &format!("touch {}; cat", marker.display())]);
        sh(&clone, &["config", "core.fsmonitor", &format!("touch {}", marker.display())]);
        fs::write(clone.join(".git/info/attributes"), "* filter=x\n").unwrap();
        fs::write(clone.join("app.js"), "1").unwrap(); // same content, new time: git looks at it again
        let _ = judge(&clone, t.path());
        assert!(!marker.exists(), "a program the repo named ran");
    }

    #[test]
    fn a_folder_holding_a_repo_is_not_a_dependency() {
        let t = tempfile::tempdir().unwrap();
        let f = t.path();
        fs::create_dir_all(f.join(".venv/src/lib/.git")).unwrap();
        fs::write(f.join(".venv/pyvenv.cfg"), "home = /usr").unwrap();
        fs::create_dir_all(f.join("fixture/.git")).unwrap();
        fs::write(f.join("fixture/CACHEDIR.TAG"), "Signature: 8a477f597d28d172789f06886806bc55").unwrap();
        let found = scan(f);
        assert!(found.deps.is_empty(), "{:?}", found.deps);
        let mut repos: Vec<_> = found.repos.iter().map(|r| r.path.strip_prefix(f).unwrap().to_path_buf()).collect();
        repos.sort();
        assert_eq!(repos, [PathBuf::from(".venv/src/lib"), PathBuf::from("fixture")]);
    }

    #[test]
    fn last_used_reads_the_git_folder() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        sh(&clone, &["worktree", "add", "-q", t.path().join("w").to_str().unwrap()]);
        assert!(last_used(&clone).is_some() && last_used(&t.path().join("w")).is_some());
        assert!(last_used(t.path()).is_none());
    }

    #[test]
    fn strip_credentials_drops_user_and_token() {
        assert_eq!(strip_credentials("https://x-access-token:ghp_abc@github.com/o/r.git"), "https://github.com/o/r.git");
        assert_eq!(strip_credentials("https://github.com/o/r.git"), "https://github.com/o/r.git");
        assert_eq!(strip_credentials("git@github.com:o/r.git"), "git@github.com:o/r.git");
        assert_eq!(strip_credentials("https://github.com/o/r@v1.git"), "https://github.com/o/r@v1.git");
    }
}
