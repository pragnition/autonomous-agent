# Scratch Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agentd frees the rebuildable parts of quiet conversation folders (dependencies, fully pushed clones) every
hour, permanently, without losing anything Jensen made.

**Architecture:** A new `cleanup` module in agentd. `scan` walks a folder and judges its clones with git. `remove`
moves targets into a trash folder on the same volume and deletes them, falling back to root through Jensen's Docker.
`mod.rs` decides which folders are quiet (DB + Jensen's Docker), runs the pass on a low-priority thread, writes
`CLEANUP.md` and records each folder. `agentd serve` runs it hourly and `agentd cleanup [--dry-run]` runs it once.

**Tech Stack:** Rust (tokio, rusqlite, bollard, libc), git CLI.

**Spec:** docs/superpowers/specs/2026-11-25-scratch-cleanup-design.md

## Global Constraints

- `[cleanup] idle_days`: non-negative integer, default 3; 0 turns the job off.
- Trash: `<work>/.cleanup-trash`; lock: `<work>/.cleanup.lock`; notes file: `CLEANUP.md` at the folder's top.
- Pass budget 50 minutes; hourly from serve.
- Dependency names, generated names and the clone rules exactly as listed in the spec.
- No network calls from git; any git error keeps the clone.
- Plain words in comments and logs; no attribution lines in commits.

## Review Focus

- A folder in use only through a spawned agent's `cwd` inside it: must count as in use.
- A clone with an ignored `.env` or an ignored run-output folder: must be kept.
- A nested clone inside an ignored folder of a clean clone: the outer clone must be kept.
- A root-owned folder agentd can't rename or delete: must go through the root fallback, never left half-deleted.
- Jensen's Docker unreachable: the pass must do nothing.

---

### Task 1: Scan and judge clones (`cleanup/scan.rs`)

**Files:** Create `rust/crates/agentd/src/cleanup/scan.rs`; tests in the same file.

**Interfaces:**
- Produces: `pub struct Found { pub deps: Vec<PathBuf>, pub repos: Vec<Repo> }`,
  `pub struct Repo { pub path: PathBuf, pub worktree: bool }`, `pub fn scan(folder: &Path) -> Found`,
  `pub enum Verdict { Remove { url: String, branch: String, head: String }, Keep(String) }`,
  `pub fn judge(repo: &Path) -> Verdict`, `pub fn tracked(repo: &Path, dirs: &[PathBuf]) -> Result<HashSet<PathBuf>>`,
  `pub fn strip_credentials(url: &str) -> String`, `pub fn git(repo: &Path, args: &[&str]) -> Result<String>`
  (60 s timeout, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`).

- [ ] Tests: `scan_finds_dependency_folders_and_stops_there` (node_modules, .venv with pyvenv.cfg, venv without it
  not found, CACHEDIR.TAG folder, nothing under .git, symlinked folder not followed);
  `a_pushed_clean_clone_can_go` (bare remote, clone, commit, push; ignored node_modules + dist + x.log → Remove with
  url/branch/head); `keeps_a_clone_with` one test each for untracked file, uncommitted change, unpushed commit, stash,
  ignored `.env`, no remote, linked worktree, nested clone in an ignored folder;
  `strip_credentials_drops_user_and_token`.
- [ ] Run `cargo test -p agentd cleanup::scan` → fail, implement, → pass.

### Task 2: Remove through trash (`cleanup/remove.rs`)

**Files:** Create `rust/crates/agentd/src/cleanup/remove.rs`.

**Interfaces:**
- Produces: `pub fn to_trash(target: &Path, trash: &Path) -> std::io::Result<PathBuf>` (unique name,
  `rename`), `pub fn delete_tree(path: &Path) -> (u64, Vec<PathBuf>)` (bytes freed from `st_blocks`, paths it
  couldn't delete; never follows symlinks), `pub fn tree_bytes(path: &Path) -> u64` (for dry runs),
  `pub fn under(path: &Path, roots: &[&Path]) -> bool` (lexical, rejects `..`).
- [ ] Tests: `to_trash_moves_and_names_uniquely`, `delete_tree_counts_bytes_and_skips_symlink_targets`,
  `delete_tree_reports_what_it_cannot_delete` (read-only folder; skipped when run as root), `under_rejects_dot_dot`.
- [ ] Run tests → fail, implement, → pass.

### Task 3: Quiet folders, the pass, bookkeeping (`cleanup/mod.rs`, `db.rs`, `config.rs`)

**Files:** Create `rust/crates/agentd/src/cleanup/mod.rs`; modify `db.rs` (AGENTD_SCHEMA),
`config.rs` (`pub struct Cleanup { pub idle_days: i64 }`, `Config.cleanup`), `lib.rs`.

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: `pub trait Apps: Send + Sync { fn folders_in_use(&self) -> BoxFuture<'_, Result<HashSet<String>>>;
  fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>>; }`,
  `pub struct Settings { pub work: PathBuf, pub idle: chrono::Duration, pub budget: std::time::Duration }`,
  `pub async fn pass(db: Db, apps: Arc<dyn Apps>, settings: Settings, now: DateTime<Utc>, dry_run: bool) -> Result<Report>`,
  `pub fn busy(c: &Connection, folder: &str, folder_path: &Path, now, idle) -> Result<Option<String>>`
  (Some(reason) when in use), `pub fn in_use_from_inspect(text: &str, scratch: &Path, kept_stacks: &[String]) -> HashSet<String>`,
  `Report` (serde): folders cleaned/skipped, bytes freed, kept clones with reasons, leftovers.
- Table: `agentd_cleanups(folder TEXT PRIMARY KEY, cleaned_at TEXT NOT NULL, activity_at TEXT NOT NULL, freed_bytes INTEGER NOT NULL DEFAULT 0)`.
- [ ] Tests: `busy_when` queued work item / open park / active project / running spawned agent / running container /
  kept container / recent activity / spawned cwd inside folder; `quiet_after_idle_days`;
  `in_use_from_inspect_reads_working_dirs_and_binds`; `a_pass_removes_rebuildable_and_keeps_the_rest` (notes kept,
  pushed clone gone, unpushed clone kept with its node_modules gone, CLEANUP.md names the clone URL, record written,
  freed > 0); `a_second_pass_skips_a_cleaned_folder`; `a_dry_run_changes_nothing`; `a_busy_folder_is_untouched`;
  `docker_unreachable_means_no_pass`; `root_owned_leftovers_go_through_apps`; `config_reads_cleanup_idle_days`.
- [ ] Run tests → fail, implement, → pass.

### Task 4: Wire it in (`serve.rs`, `main.rs`, `workloads.rs`)

**Files:** Modify `serve.rs` (hourly `every`), `main.rs` (`agentd cleanup [--dry-run]`, USAGE),
`workloads.rs` (extract `pub(crate) async fn exec_output(docker, container, user, cmd, limit)`), and add the real
`Apps` (`DockerApps { docker, container }` in `cleanup/mod.rs`): `folders_in_use` runs `sh -c` with `docker inspect`
over `docker ps -aq` in agent-docker; `remove_as_root` checks `under()` then runs `rm -rf --` there and verifies the
path is gone.

- [ ] `cargo build -p agentd`, `cargo test -p agentd`, `cargo clippy -p agentd -- -D warnings` all clean.
- [ ] Commit.

### Task 5: Live dry run

- [ ] Build agentd for the host's image and run `agentd cleanup --dry-run` as uid 1001 against the live volume and a
  copy of the DB; check the numbers and keep reasons look right before the PR.
