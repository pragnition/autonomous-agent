use super::*;
use super::scan::tests::{pushed_clone, sh};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::Mutex;

/// The agent's Docker, faked: a fixed listing (or none), and root moves and removals done as ourselves.
struct Fake {
    listing: Option<String>,
    moved_as_root: Mutex<Vec<PathBuf>>,
    removed_as_root: Mutex<Vec<PathBuf>>,
}

impl Fake {
    fn new(listing: &str) -> Arc<Fake> {
        Arc::new(Fake { listing: Some(listing.to_string()), moved_as_root: Mutex::default(), removed_as_root: Mutex::default() })
    }
}

impl Apps for Fake {
    fn containers(&self) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move { self.listing.clone().ok_or_else(|| anyhow!("Cannot connect to the Docker daemon")) })
    }
    fn move_as_root(&self, from: PathBuf, to: PathBuf) -> BoxFuture<'_, Result<()>> {
        Box::pin(async move {
            if let Some(parent) = from.parent() {
                fs::set_permissions(parent, fs::Permissions::from_mode(0o755))?;
            }
            fs::rename(&from, &to)?;
            self.moved_as_root.lock().unwrap().push(from);
            Ok(())
        })
    }
    fn remove_as_root(&self, path: PathBuf) -> BoxFuture<'_, Result<()>> {
        Box::pin(async move {
            if let Some(parent) = path.parent() {
                fs::set_permissions(parent, fs::Permissions::from_mode(0o755))?;
            }
            fs::remove_dir_all(&path)?;
            self.removed_as_root.lock().unwrap().push(path);
            Ok(())
        })
    }
}

struct Setup {
    _dir: tempfile::TempDir,
    db: Db,
    settings: Settings,
    /// The pass's time: ten days on, so files made by the test look old.
    now: DateTime<Utc>,
}

fn setup() -> Setup {
    let dir = tempfile::tempdir().unwrap();
    let db = Db::open(&dir.path().join("agent.db")).unwrap();
    let settings = Settings::new(&dir.path().join("work"), 3);
    fs::create_dir_all(settings.scratch()).unwrap();
    Setup { _dir: dir, db, settings, now: Utc::now() + Duration::days(10) }
}

impl Setup {
    fn folder(&self, name: &str) -> PathBuf {
        let f = self.settings.scratch().join(name);
        fs::create_dir_all(&f).unwrap();
        f
    }
    fn conversation(&self, id: &str, active_days_ago: i64, cwd: Option<&Path>) {
        let at = keeps::fmt(self.now - Duration::days(active_days_ago));
        self.db.lock().execute("INSERT INTO conversations(id, cwd, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                               params![id, cwd.map(|p| p.to_string_lossy().into_owned()), at, at]).unwrap();
    }
    fn exec(&self, sql: &str) {
        self.db.lock().execute_batch(sql).unwrap();
    }
    fn state(&self, name: &str) -> State {
        let path = self.settings.scratch().join(name);
        state(&self.db.lock(), name, &path, self.now, self.settings.idle, modified(&path)).unwrap()
    }
    async fn pass(&self, apps: Arc<Fake>, dry_run: bool) -> Result<Report> {
        pass(self.db.clone(), apps, self.settings.clone(), self.now, dry_run).await
    }
}

fn busy(s: State) -> bool {
    matches!(s, State::Busy(_))
}

/// A folder like the live ones: notes, a Slack attachment, loose dependencies, a pushed clone and a clone with an
/// unpushed commit, both with their dependencies installed.
fn typical(s: &Setup, name: &str) -> PathBuf {
    let f = s.folder(name);
    fs::write(f.join("notes.md"), "what I worked out").unwrap();
    fs::create_dir_all(f.join("slack-files")).unwrap();
    fs::write(f.join("slack-files/spec.pdf"), "pdf").unwrap();
    fs::create_dir_all(f.join("tools/node_modules/x")).unwrap();
    fs::write(f.join("tools/node_modules/x/index.js"), vec![b'x'; 50_000]).unwrap();
    let (_, app) = pushed_clone(&f, "app");
    fs::create_dir_all(app.join("node_modules/y")).unwrap();
    fs::write(app.join("node_modules/y/index.js"), vec![b'y'; 50_000]).unwrap();
    let (_, wip) = pushed_clone(&f, "wip");
    fs::write(wip.join("b.js"), "1").unwrap();
    sh(&wip, &["add", "-A"]);
    sh(&wip, &["commit", "-qm", "not pushed"]);
    fs::create_dir_all(wip.join("node_modules/z")).unwrap();
    f
}

// --- whether a folder is in use ----------------------------------------------------------------------------------------

#[test]
fn quiet_after_idle_days_and_recent_before() {
    let s = setup();
    s.folder("V-old");
    s.folder("V-new");
    s.conversation("V-old", 4, None);
    s.conversation("V-new", 2, None);
    assert!(matches!(s.state("V-old"), State::Quiet(_)));
    assert_eq!(s.state("V-new"), State::Recent);
}

#[test]
fn a_folder_without_a_conversation_goes_by_when_it_changed() {
    let s = setup();
    s.folder("V-loose");
    assert!(matches!(s.state("V-loose"), State::Quiet(_)), "made ten days before the pass");
    let path = s.settings.scratch().join("V-loose");
    let in_a_day = state(&s.db.lock(), "V-loose", &path, Utc::now() + Duration::days(1), s.settings.idle, modified(&path)).unwrap();
    assert_eq!(in_a_day, State::Recent);
}

#[test]
fn busy_while_anything_runs_in_it() {
    let cases = [
        "INSERT INTO work_items(id, conversation_id, kind, payload, status, created_at, updated_at) VALUES ('w', 'V-1', 'turn', '{}', 'queued', 'x', 'x')",
        "INSERT INTO work_items(id, conversation_id, kind, payload, status, created_at, updated_at) VALUES ('w', 'V-1', 'turn', '{}', 'claimed', 'x', 'x')",
        "INSERT INTO parks(conversation_id, work_item_id, until_at, created_at) VALUES ('V-1', 'w', 'x', 'x')",
        "INSERT INTO limit_waits(work_item_id, conversation_id, created_at) VALUES ('w', 'V-1', 'x')",
        "INSERT INTO projects(name, repo, conversation_id, status, created_at, updated_at) VALUES ('p', 'o/r', 'V-1', 'active', 'x', 'x')",
        "INSERT INTO spawned_agents(id, parent_conversation_id, child_conversation_id, prompt, created_at) VALUES ('A-1', 'V-1', 'V-2', 'go', 'x')",
        "INSERT INTO agentd_containers(conversation_id, name, created_at) VALUES ('V-1', 'agent-conv-V-1', 'x')",
        "INSERT INTO agentd_keeps(kind, target, until, set_by, set_at) VALUES ('container', 'V-1', NULL, 'sam', 'x')",
    ];
    for sql in cases {
        let s = setup();
        s.folder("V-1");
        s.conversation("V-1", 9, None);
        s.exec(sql);
        assert!(busy(s.state("V-1")), "{sql}");
    }
}

#[test]
fn finished_work_does_not_hold_a_folder() {
    let s = setup();
    s.folder("V-1");
    s.conversation("V-1", 9, None);
    let old = keeps::fmt(s.now - Duration::days(9));
    s.exec(&format!("
        INSERT INTO work_items(id, conversation_id, kind, payload, status, created_at, updated_at) VALUES ('w', 'V-1', 'turn', '{{}}', 'done', '{old}', '{old}');
        INSERT INTO parks(conversation_id, work_item_id, until_at, created_at, ended_at) VALUES ('V-1', 'w', 'x', 'x', 'x');
        INSERT INTO projects(name, repo, conversation_id, status, created_at, updated_at) VALUES ('p', 'o/r', 'V-1', 'done', 'x', 'x');
        INSERT INTO agentd_containers(conversation_id, name, created_at, last_turn_at, stopped_at) VALUES ('V-1', 'n', '{old}', '{old}', '{old}');
        INSERT INTO agentd_keeps(kind, target, until, set_by, set_at) VALUES ('container', 'V-1', '2000-01-01T00:00:00Z', 'sam', 'x');"));
    assert!(matches!(s.state("V-1"), State::Quiet(_)));
}

#[test]
fn a_conversation_working_inside_the_folder_counts() {
    let s = setup();
    let f = s.folder("V-parent");
    s.conversation("V-parent", 9, None);
    s.conversation("V-child", 1, Some(&f.join("lanes/l1")));
    assert_eq!(s.state("V-parent"), State::Recent, "the child worked in it yesterday");
    s.exec("INSERT INTO work_items(id, conversation_id, kind, payload, status, created_at, updated_at) VALUES ('w', 'V-other', 'turn', '{}', 'queued', 'x', 'x')");
    let lookalike = s.folder("V-parent-2");
    s.conversation("V-other", 9, Some(&lookalike));
    assert!(matches!(s.state("V-parent-2"), State::Busy(_)));
    s.exec("UPDATE conversations SET last_active_at = '2000-01-01T00:00:00Z' WHERE id = 'V-child'");
    assert!(matches!(s.state("V-parent"), State::Quiet(_)), "a folder whose name starts the same isn't inside it");
}

#[test]
fn a_time_that_cannot_be_read_counts_as_now() {
    let s = setup();
    s.folder("V-1");
    s.conversation("V-1", 9, None);
    s.exec("UPDATE conversations SET last_active_at = 'garbage' WHERE id = 'V-1'");
    assert_eq!(s.state("V-1"), State::Recent);
}

#[test]
fn in_use_from_ps_reads_running_folders_and_kept_stacks() {
    let scratch = Path::new("/var/agent/scratch");
    let listing = "running\trr-a-app-1\trr-a\t/var/agent/scratch/V-a/app\t/var/agent/scratch/V-a/app,rr-a_nm\n\
                   running\tmock\t\t\t/var/agent/scratch/V-b/RosterRolo,rr-nm\n\
                   exited\trr-c-db-1\trr-c\t/var/agent/scratch/V-c/x\trr-c_pg\n\
                   exited\trr-d-db-1\trr-d\t/var/agent/scratch/V-d/x\t\n\
                   restarting\tloop\t\t\t/var/agent/scratch/V-e\n\
                   running\tother\t\t\t/var/agent/memory,/tmp/x,/var/run/docker.sock\n__END__\n";
    let used = in_use_from_ps(listing, scratch, &["rr-d".into()]).unwrap();
    assert_eq!(used, HashSet::from(["V-a", "V-b", "V-d", "V-e"].map(String::from)));
    assert!(in_use_from_ps("running\tx\t\t\t/var/agent/scratch/V-a\n", scratch, &[]).is_err(), "no end: an error");
    assert!(in_use_from_ps("Cannot connect to the Docker daemon\n", scratch, &[]).is_err());
    assert!(in_use_from_ps("__END__\n", scratch, &[]).unwrap().is_empty());
    let everything = "running\tbig\t\t\t/var/agent\n__END__\n";
    assert!(in_use_from_ps(everything, scratch, &[]).is_err(), "a container using every folder stops the pass");
}

#[test]
fn services_in_use_reads_their_folders_and_commands() {
    let scratch = Path::new("/var/agent/scratch");
    let file = "[[services]]\nname = \"proxy\"\ncmd = [\"node\", \"proxy.mjs\"]\ncwd = \"/var/agent/scratch/V-1/proxy\"\n\n\
                [[services]]\nname = \"api\"\ncmd = [\"sh\", \"-c\", \"cd /var/agent/scratch/V-2/api && npm start\"]\n\n\
                [[services]]\nname = \"other\"\ncmd = [\"x\"]\ncwd = \"/var/agent/home\"\n";
    assert_eq!(services_in_use(file, scratch), HashSet::from(["V-1", "V-2"].map(String::from)));
}

// --- a pass ----------------------------------------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_pass_removes_what_can_be_rebuilt_and_keeps_the_rest() {
    let s = setup();
    let f = typical(&s, "V-old");
    s.conversation("V-old", 4, None);
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert!(report.failures.is_empty(), "{:?}", report.failures);

    assert!(!f.join("app").exists(), "the pushed clone went");
    assert!(!f.join("tools/node_modules").exists() && !f.join("wip/node_modules").exists(), "dependencies went");
    assert!(f.join("notes.md").exists() && f.join("slack-files/spec.pdf").exists() && f.join("tools").is_dir());
    assert!(f.join("wip/b.js").exists(), "the clone with an unpushed commit stays");
    let notes = fs::read_to_string(f.join(NOTES)).unwrap();
    assert!(notes.contains("git clone ") && notes.contains(&f.join("app-remote.git").to_string_lossy().into_owned()), "{notes}");
    assert!(notes.contains("`wip`: 1 commits not on origin") && notes.contains("`tools/node_modules`"), "{notes}");

    assert_eq!(report.folders.len(), 1);
    assert!(report.freed_bytes >= 100_000, "{}", report.freed_bytes);
    assert_eq!(report.kept_clones, BTreeMap::from([("unpushed".to_string(), 1)]));
    assert_eq!(fs::read_dir(s.settings.trash()).unwrap().count(), 0, "the trash is empty again");
    let freed: i64 = s.db.lock().query_row("SELECT freed_bytes FROM agentd_cleanups WHERE folder = 'V-old'", [], |r| r.get(0)).unwrap();
    assert_eq!(freed as u64, report.freed_bytes);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_clone_holding_a_clone_with_work_of_its_own_stays() {
    let s = setup();
    let f = s.folder("V-old");
    s.conversation("V-old", 4, None);
    let (_, outer) = pushed_clone(&f, "outer");
    let (_, inner) = pushed_clone(&f, "inner");
    fs::write(inner.join("b.js"), "1").unwrap();
    sh(&inner, &["add", "-A"]);
    sh(&inner, &["commit", "-qm", "not pushed"]);
    fs::create_dir_all(outer.join("dist")).unwrap();
    fs::rename(&inner, outer.join("dist/inner")).unwrap(); // dist/ is ignored and generated: git alone would let it go
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert!(outer.join("dist/inner/b.js").exists());
    assert_eq!(report.kept_clones, BTreeMap::from([("nested".to_string(), 1), ("unpushed".to_string(), 1)]));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cleaned_folder_is_not_walked_again_until_it_is_used() {
    let s = setup();
    typical(&s, "V-old");
    s.conversation("V-old", 4, None);
    let first = s.pass(Fake::new("__END__"), false).await.unwrap();
    let again = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert_eq!((again.unchanged, again.folders.len()), (1, 0), "first: {first:?}\nagain: {again:?}");
    s.exec(&format!("UPDATE conversations SET last_active_at = '{}' WHERE id = 'V-old'", keeps::fmt(s.now)));
    assert_eq!(s.pass(Fake::new("__END__"), false).await.unwrap().recent, 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_dry_run_changes_nothing() {
    let s = setup();
    let f = typical(&s, "V-old");
    s.conversation("V-old", 4, None);
    let report = s.pass(Fake::new("__END__"), true).await.unwrap();
    assert!(f.join("app/node_modules").exists() && f.join("tools/node_modules").exists() && !f.join(NOTES).exists());
    assert!(!s.settings.trash().exists());
    let folder = &report.folders[0];
    assert_eq!(folder.clones_removed, ["app"]);
    let mut deps = folder.deps.clone();
    deps.sort();
    assert_eq!(deps, ["tools/node_modules", "wip/node_modules"]);
    assert!(report.freed_bytes >= 100_000);
    let recorded: i64 = s.db.lock().query_row("SELECT COUNT(*) FROM agentd_cleanups", [], |r| r.get(0)).unwrap();
    assert_eq!(recorded, 0);
}

#[tokio::test(flavor = "multi_thread")]
async fn folders_in_use_are_untouched() {
    let s = setup();
    let queued = typical(&s, "V-queued");
    s.conversation("V-queued", 4, None);
    s.exec("INSERT INTO work_items(id, conversation_id, kind, payload, status, created_at, updated_at) VALUES ('w', 'V-queued', 'turn', '{}', 'queued', 'x', 'x')");
    let stack = typical(&s, "V-stack");
    s.conversation("V-stack", 4, None);
    let recent = typical(&s, "V-recent");
    s.conversation("V-recent", 1, None);
    let listing = format!("running\trr-x-app-1\trr-x\t{}/app\t\n__END__\n", stack.display());
    let report = s.pass(Fake::new(&listing), false).await.unwrap();
    for f in [&queued, &stack, &recent] {
        assert!(f.join("app/node_modules").exists() && !f.join(NOTES).exists(), "{}", f.display());
    }
    assert_eq!((report.in_use, report.recent, report.folders.len()), (2, 1, 0));
}

#[tokio::test(flavor = "multi_thread")]
async fn no_answer_from_the_agents_docker_means_no_pass() {
    let s = setup();
    let f = typical(&s, "V-old");
    s.conversation("V-old", 4, None);
    let apps = Arc::new(Fake { listing: None, moved_as_root: Mutex::default(), removed_as_root: Mutex::default() });
    assert!(s.pass(apps, false).await.is_err());
    assert!(f.join("app/node_modules").exists() && f.join("tools/node_modules").exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn a_service_running_from_a_folder_holds_it() {
    let s = setup();
    let f = typical(&s, "V-svc");
    s.conversation("V-svc", 4, None);
    fs::write(s.settings.work.join("services.toml"),
              format!("[[services]]\nname = \"proxy\"\ncmd = [\"node\", \"p.mjs\"]\ncwd = \"{}/tools\"\n", f.display())).unwrap();
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert!(f.join("tools/node_modules").exists() && f.join("app").exists());
    assert_eq!(report.in_use, 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn git_use_inside_a_clone_counts_as_activity() {
    let s = setup();
    let f = typical(&s, "V-old");
    s.conversation("V-old", 4, None);
    // another conversation checked something out in it yesterday
    let yesterday = std::time::SystemTime::from(s.now - Duration::days(1));
    fs::File::options().write(true).open(f.join("app/.git/HEAD")).unwrap().set_modified(yesterday).unwrap();
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert!(f.join("app/node_modules").exists() && f.join("tools/node_modules").exists());
    assert_eq!(report.recent, 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_folder_that_is_itself_a_clone_stays() {
    let s = setup();
    let scratch = s.settings.scratch();
    let (_, clone) = pushed_clone(&scratch, "V-clone");
    fs::create_dir_all(clone.join("node_modules/x")).unwrap();
    s.conversation("V-clone", 4, None);
    s.conversation("V-clone-remote.git", 4, None); // the test's remote sits beside it: keep it out of the way
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert!(report.failures.is_empty(), "{:?}", report.failures);
    assert!(clone.join("app.js").exists() && clone.join(NOTES).exists());
    assert!(!clone.join("node_modules").exists(), "its dependencies still go");
    assert_eq!(report.kept_clones, BTreeMap::from([("folder".to_string(), 1)]));
}

#[tokio::test(flavor = "multi_thread")]
async fn what_agentd_cannot_move_is_moved_as_root() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        return; // root can move anything: nothing to fall back from
    }
    let s = setup();
    let f = s.folder("V-old");
    s.conversation("V-old", 4, None);
    fs::create_dir_all(f.join("locked/node_modules/x")).unwrap();
    fs::set_permissions(f.join("locked"), fs::Permissions::from_mode(0o555)).unwrap();
    let apps = Fake::new("__END__");
    let report = s.pass(apps.clone(), false).await.unwrap();
    assert!(report.failures.is_empty(), "{:?}", report.failures);
    assert_eq!(*apps.moved_as_root.lock().unwrap(), [f.join("locked/node_modules")], "moved at the check, not later");
    assert!(apps.removed_as_root.lock().unwrap().is_empty());
    assert!(!f.join("locked/node_modules").exists());
    assert_eq!(fs::read_dir(s.settings.trash()).unwrap().count(), 0);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_pass_waits_for_the_first() {
    let s = setup();
    let _held = Lock::take(&s.settings.lock()).unwrap().unwrap();
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert_eq!(report.skipped.as_deref(), Some("another pass is running"));
}

#[tokio::test(flavor = "multi_thread")]
async fn the_budget_leaves_the_rest_for_the_next_pass() {
    let mut s = setup();
    for name in ["V-a", "V-b"] {
        fs::create_dir_all(s.folder(name).join("node_modules")).unwrap();
        s.conversation(name, 4, None);
    }
    s.settings.budget = StdDuration::ZERO;
    let report = s.pass(Fake::new("__END__"), false).await.unwrap();
    assert_eq!((report.left_for_next_pass, report.folders.len()), (2, 0));
}

#[test]
fn quote_makes_one_shell_word() {
    assert_eq!(quote("https://github.com/o/r.git"), "https://github.com/o/r.git");
    assert_eq!(quote("my dir"), "'my dir'");
    assert_eq!(quote("it's"), r"'it'\''s'");
}
