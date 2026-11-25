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
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let mut subdirs = Vec::new();
    for e in entries.flatten() {
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

fn is_dependency(dir: &Path) -> bool {
    let name = dir.file_name().unwrap_or_default().to_string_lossy();
    match name.as_ref() {
        ".venv" | "venv" => dir.join("pyvenv.cfg").is_file(),
        n if DEPENDENCIES.contains(&n) => true,
        _ => cache_tagged(dir),
    }
}

fn cache_tagged(dir: &Path) -> bool {
    let mut head = [0u8; 43];
    std::fs::File::open(dir.join("CACHEDIR.TAG")).and_then(|mut f| f.read_exact(&mut head)).is_ok() && head == CACHE_TAG
}

/// A path git reports as ignored that can be rebuilt: inside a dependency or generated folder, or a generated file.
fn generated(path: &str) -> bool {
    let path = path.trim_end_matches('/');
    let name = path.rsplit('/').next().unwrap_or(path);
    path.split('/').any(|c| DEPENDENCIES.contains(&c) || GENERATED.contains(&c))
        || GENERATED_NAMES.contains(&name) || GENERATED_ENDINGS.iter().any(|e| name.ends_with(e))
}

/// `git -C repo args...`: its output, or an error when it fails or takes over a minute. No locks taken, no prompts.
pub fn git(repo: &Path, args: &[&str]) -> Result<String> {
    let mut child = Command::new("git").arg("-C").arg(repo).args(["-c", "core.fsmonitor=false"]).args(args)
        .env("GIT_OPTIONAL_LOCKS", "0").env("GIT_TERMINAL_PROMPT", "0").env("LC_ALL", "C")
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
            bail!("git {} took over {} s", args.join(" "), GIT_TIMEOUT.as_secs());
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let out = out.join().map_err(|_| anyhow!("reading git's output"))?;
    let err = err.join().map_err(|_| anyhow!("reading git's errors"))?;
    if !status.success() {
        bail!("git {}: {}", args.join(" "), String::from_utf8_lossy(&err).trim());
    }
    Ok(String::from_utf8_lossy(&out).into_owned())
}

/// Whether the clone at `repo` can be removed whole. Any git error keeps it.
pub fn judge(repo: &Path) -> Verdict {
    try_judge(repo).unwrap_or_else(|e| Verdict::Keep {
        kind: "git error", detail: format!("{e:#}").lines().next().unwrap_or_default().chars().take(200).collect() })
}

fn try_judge(repo: &Path) -> Result<Verdict> {
    let dot_git = repo.join(".git");
    if ["rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"]
        .iter().any(|m| dot_git.join(m).exists()) {
        return keep("in progress", "a merge, rebase, cherry-pick or bisect is in progress");
    }
    if repo.join(".gitmodules").exists() {
        return keep("submodules", "it has submodules");
    }
    if git(repo, &["worktree", "list", "--porcelain"])?.lines().filter(|l| l.starts_with("worktree ")).count() > 1 {
        return keep("worktrees", "it has linked worktrees");
    }
    let remotes = git(repo, &["remote"])?;
    let Some(remote) = remotes.lines().find(|r| *r == "origin").or_else(|| remotes.lines().next()) else {
        return keep("no remote", "it has no remote");
    };
    let url = strip_credentials(git(repo, &["remote", "get-url", remote])?.trim());
    let unpushed = git(repo, &["rev-list", "--count", "HEAD", "--branches", "--not", "--remotes"])?;
    if unpushed.trim() != "0" {
        return keep("unpushed", format!("{} commits not on its remote", unpushed.trim()));
    }
    if !git(repo, &["stash", "list"])?.trim().is_empty() {
        return keep("stash", "it has a stash");
    }
    let status = git(repo, &["status", "--porcelain=v1", "-z", "--ignored=traditional", "--untracked-files=normal"])?;
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
    let branch = git(repo, &["rev-parse", "--abbrev-ref", "HEAD"])?.trim().to_string();
    let head = git(repo, &["rev-parse", "HEAD"])?.trim().to_string();
    Ok(Verdict::Remove { url, branch, head })
}

/// Of `dirs` (inside `repo`), the ones holding files git tracks.
pub fn tracked(repo: &Path, dirs: &[PathBuf]) -> Result<HashSet<PathBuf>> {
    let mut found = HashSet::new();
    let rels: Vec<(PathBuf, String)> = dirs.iter()
        .filter_map(|d| Some((d.clone(), d.strip_prefix(repo).ok()?.to_string_lossy().into_owned()))).collect();
    for chunk in rels.chunks(100) {
        let mut args = vec!["ls-files".to_string(), "-z".into(), "--".into()];
        args.extend(chunk.iter().map(|(_, rel)| format!(":(literal){rel}")));
        let out = git(repo, &args.iter().map(String::as_str).collect::<Vec<_>>())?;
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
        assert_eq!(judge(&clone), Verdict::Remove { url: remote.to_string_lossy().into_owned(), branch: "main".into(), head });
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
            assert_eq!(kind(judge(&clone)), want, "{name}");
        }
    }

    #[test]
    fn keeps_a_clone_with_a_linked_worktree_or_another_clone_inside() {
        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        sh(&clone, &["worktree", "add", "-q", t.path().join("w").to_str().unwrap()]);
        assert_eq!(kind(judge(&clone)), "worktrees");

        let t = tempfile::tempdir().unwrap();
        let (_, clone) = pushed_clone(t.path(), "r");
        let (_, inner) = pushed_clone(t.path(), "inner");
        fs::create_dir_all(clone.join("vendor")).unwrap();
        fs::rename(&inner, clone.join("vendor/inner")).unwrap();
        assert_eq!(kind(judge(&clone)), "ignored", "a clone in an ignored folder is not something git can rebuild");
    }

    #[test]
    fn a_repo_without_commits_is_kept() {
        let t = tempfile::tempdir().unwrap();
        sh(t.path(), &["init", "-q"]);
        sh(t.path(), &["remote", "add", "origin", "https://example.com/r.git"]);
        assert_eq!(kind(judge(t.path())), "git error");
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
        for p in ["node_modules/", "web/node_modules/", "dist/", "a/b.log", "x.pyc", ".DS_Store", "build/x.env"] {
            assert!(generated(p), "{p}");
        }
        for p in [".env", "runs/", "vendor/inner/", "notes.md"] {
            assert!(!generated(p), "{p}");
        }
    }

    #[test]
    fn strip_credentials_drops_user_and_token() {
        assert_eq!(strip_credentials("https://x-access-token:ghp_abc@github.com/o/r.git"), "https://github.com/o/r.git");
        assert_eq!(strip_credentials("https://github.com/o/r.git"), "https://github.com/o/r.git");
        assert_eq!(strip_credentials("git@github.com:o/r.git"), "git@github.com:o/r.git");
        assert_eq!(strip_credentials("https://github.com/o/r@v1.git"), "https://github.com/o/r@v1.git");
    }
}
