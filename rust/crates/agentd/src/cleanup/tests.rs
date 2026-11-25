use super::*;
use super::scan::tests::{pushed_clone, sh};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::Mutex;

/// The agent's Docker, faked: a fixed listing (or none), root moves and removals done as ourselves, and answers for
/// the Docker step that a test can change between passes (`docker` calls are recorded).
#[derive(Default)]
struct Fake {
    listing: Option<String>,
    moved_as_root: Mutex<Vec<PathBuf>>,
    removed_as_root: Mutex<Vec<PathBuf>>,
    /// `docker inspect` of every container and `docker volume inspect` of every volume.
    containers_json: Mutex<String>,
    volumes_json: Mutex<String>,
    /// `docker images` lines: id TAB repo:tag.
    images: Mutex<String>,
    /// Command lines by session ("" for the agent's services); a session in `blind` can't be read.
    processes: Mutex<HashMap<String, String>>,
    blind: Mutex<HashSet<String>>,
    /// What `network` prints.
    network: Mutex<String>,
    sizes: Mutex<HashMap<String, u64>>,
    contents: Mutex<HashMap<String, (bool, u64)>>,
    calls: Mutex<Vec<String>>,
}

impl Fake {
    fn new(listing: &str) -> Arc<Fake> {
        let fake = Fake { listing: Some(listing.to_string()), ..Default::default() };
        *fake.containers_json.lock().unwrap() = "[]".into();
        *fake.volumes_json.lock().unwrap() = "[]".into();
        Arc::new(fake)
    }
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

fn ids_in(json: &str, key: &str) -> String {
    serde_json::from_str::<serde_json::Value>(json).unwrap().as_array().unwrap().iter()
        .map(|c| c[key].as_str().unwrap().to_string()).collect::<Vec<_>>().join("\n")
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
    fn docker(&self, args: Vec<String>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move {
            let line = args.join(" ");
            let out = match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
                ["ps", ..] => ids_in(&self.containers_json.lock().unwrap(), "Id"),
                ["volume", "ls", ..] => ids_in(&self.volumes_json.lock().unwrap(), "Name"),
                ["images", ..] => self.images.lock().unwrap().clone(),
                _ => "Total reclaimed space: 0B".into(),
            };
            self.calls.lock().unwrap().push(line);
            Ok(out)
        })
    }
    fn inspect(&self, _: Vec<String>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move { Ok(self.containers_json.lock().unwrap().clone()) })
    }
    fn volume_inspect(&self, _: Vec<String>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move { Ok(self.volumes_json.lock().unwrap().clone()) })
    }
    fn processes(&self, conversation: Option<&str>) -> BoxFuture<'_, Result<String>> {
        let key = conversation.unwrap_or_default().to_string();
        Box::pin(async move {
            if self.blind.lock().unwrap().contains(&key) {
                bail!("no such container");
            }
            Ok(self.processes.lock().unwrap().get(&key).cloned().unwrap_or_default())
        })
    }
    fn network(&self, _: Vec<u32>) -> BoxFuture<'_, Result<String>> {
        Box::pin(async move { Ok(self.network.lock().unwrap().clone()) })
    }
    fn writable_sizes(&self, ids: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, u64>>> {
        Box::pin(async move {
            let sizes = self.sizes.lock().unwrap();
            Ok(ids.into_iter().map(|id| { let b = sizes.get(&id).copied().unwrap_or(1000); (id, b) }).collect())
        })
    }
    fn volume_contents(&self, mountpoints: Vec<String>) -> BoxFuture<'_, Result<HashMap<String, (bool, u64)>>> {
        Box::pin(async move {
            let contents = self.contents.lock().unwrap();
            Ok(mountpoints.into_iter().map(|m| { let c = contents.get(&m).copied().unwrap_or((false, 1000)); (m, c) }).collect())
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
        pass(self.db.clone(), apps, None, self.settings.clone(), self.now, dry_run).await
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
    let apps = Arc::new(Fake { listing: None, ..Default::default() });
    *apps.containers_json.lock().unwrap() = "[]".into();
    *apps.volumes_json.lock().unwrap() = "[]".into();
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

// --- the agent's Docker ----------------------------------------------------------------------------------------------

/// The cleanup's questions to the agent, recorded.
#[derive(Default)]
struct Asker {
    asked: Mutex<Vec<String>>,
}

impl Ask for Asker {
    fn ask(&self, text: &str) -> Result<String> {
        self.asked.lock().unwrap().push(text.to_string());
        Ok("w-ask".into())
    }
    fn project_channels(&self) -> HashMap<String, String> {
        HashMap::from([("rosterrolo".to_string(), "C-RR".to_string())])
    }
}

/// A container for `docker inspect`: started `days` before the pass when running (pid `pid`), stopped then when not.
struct Box_ {
    id: &'static str,
    project: &'static str,
    folder: Option<&'static str>,
    running: bool,
    days: f64,
    volumes: Vec<(&'static str, &'static str)>,
    image: &'static str,
    pid: u32,
}

impl Default for Box_ {
    fn default() -> Self {
        Box_ { id: "", project: "", folder: None, running: true, days: 1.0, volumes: vec![], image: "node:22", pid: 0 }
    }
}

fn inspect(s: &Setup, boxes: &[Box_]) -> String {
    serde_json::Value::Array(boxes.iter().map(|b| {
        let when = keeps::fmt(s.now - Duration::minutes((b.days * 1440.0) as i64));
        let mut mounts: Vec<serde_json::Value> = b.volumes.iter()
            .map(|(v, at)| serde_json::json!({"Type": "volume", "Name": v, "Destination": at})).collect();
        let mut labels = serde_json::json!({"com.docker.compose.project": b.project});
        if let Some(f) = b.folder {
            let dir = format!("{}/{f}/app", s.settings.scratch().display());
            labels["com.docker.compose.project.working_dir"] = serde_json::json!(dir);
            mounts.push(serde_json::json!({"Type": "bind", "Source": dir}));
        }
        serde_json::json!({"Id": b.id, "Name": format!("/{}", b.id), "Created": when, "Image": format!("sha256:{}", b.image),
            "State": {"Status": if b.running { "running" } else { "exited" }, "Pid": b.pid, "StartedAt": when,
                      "FinishedAt": if b.running { "0001-01-01T00:00:00Z".to_string() } else { when.clone() }},
            "Config": {"Labels": labels, "Image": b.image}, "Mounts": mounts,
            "NetworkSettings": {"Ports": {}, "Networks": {"x": {"IPAddress": format!("10.201.0.{}", b.pid % 250 + 2)}}}})
    }).collect()).to_string()
}

fn volumes(s: &Setup, vols: &[(&str, &str, i64)]) -> String {
    serde_json::Value::Array(vols.iter().map(|(name, project, days)| serde_json::json!({
        "Name": name, "Labels": if project.is_empty() { serde_json::json!(null) } else { serde_json::json!({"com.docker.compose.project": project}) },
        "CreatedAt": keeps::fmt(s.now - Duration::days(*days)), "Mountpoint": format!("/v/{name}")})).collect()).to_string()
}

/// `/proc/<pid>/net/dev` with `bytes` received on eth0, and established connections from `peers` (hex addresses).
fn net(entries: &[(u32, u64, &[&str])]) -> String {
    entries.iter().map(|(pid, bytes, peers)| {
        let tcp: String = peers.iter().map(|p| format!("   0: 0201C90A:1F90 {p}:C350 01 0:0\n")).collect();
        format!("== {pid}\nInter-|   Receive\n face |bytes\n    lo: 999999 0 0 0 0 0 0 0 999999 0\n  eth0: {bytes} 0 0 0 0 0 0 0 0 0\n{tcp}")
    }).collect()
}

async fn pass_at(s: &Setup, apps: &Arc<Fake>, ask: Option<Arc<Asker>>, at: DateTime<Utc>) -> docker::DockerReport {
    let ask = ask.map(|a| a as Arc<dyn Ask>);
    let report = pass(s.db.clone(), apps.clone(), ask, s.settings.clone(), at, false).await.unwrap();
    report.docker.unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stack_stops_only_after_an_hour_seen_unused() {
    let s = setup();
    s.folder("V-stale");
    s.conversation("V-stale", 5, None);
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-stale-app", project: "rr-stale", folder: Some("V-stale"), pid: 11, ..Default::default() }]);
    *apps.network.lock().unwrap() = net(&[(11, 1000, &[])]);
    let first = pass_at(&s, &apps, None, s.now).await;
    assert!(first.stopped_stacks.is_empty(), "first sight: counted as used");
    let soon = pass_at(&s, &apps, None, s.now + Duration::minutes(30)).await;
    assert!(soon.stopped_stacks.is_empty(), "unused for only half an hour");
    let later = pass_at(&s, &apps, None, s.now + Duration::minutes(61)).await;
    assert_eq!(later.stopped_stacks, ["rr-stale"]);
    assert!(apps.calls().contains(&"stop rr-stale-app".to_string()));
    assert!(fs::read_to_string(s.settings.scratch().join("V-stale").join(NOTES)).unwrap().contains("`docker start rr-stale-app`"));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stack_in_use_keeps_running() {
    // each case: something uses rr-x (folder V-x, pid 21), checked over passes two hours apart
    let cases: [(&str, &dyn Fn(&Setup, &Arc<Fake>)); 9] = [
        ("its conversation's session is up", &|s, _| s.exec("INSERT INTO agentd_containers(conversation_id, name, created_at) VALUES ('V-x', 'n', 'x')")),
        ("its conversation was active half an hour ago",
         &|s, _| s.exec(&format!("UPDATE conversations SET last_active_at = '{}' WHERE id = 'V-x'", keeps::fmt(s.now + Duration::minutes(90))))),
        ("a session's process names it", &|s, a| {
            s.exec("INSERT INTO agentd_containers(conversation_id, name, created_at) VALUES ('V-other', 'n', 'x')");
            a.processes.lock().unwrap().insert("V-other".into(), "docker compose -p rr-x logs\n".into());
        }),
        ("a service's process names it", &|_, a| { a.processes.lock().unwrap().insert("".into(), "psql -h rr-x-db-1\n".into()); }),
        ("a service runs in its folder", &|s, _| {
            fs::write(s.settings.work.join("services.toml"),
                      format!("[[services]]\nname = \"demo\"\ncmd = [\"node\", \"a.js\"]\ncwd = \"{}/V-x/demo\"\n", s.settings.scratch().display())).unwrap();
        }),
        ("someone outside the stack is connected", &|_, a| { *a.network.lock().unwrap() = net(&[(21, 1000, &["0101C90A"])]); }),
        ("traffic moved since the last pass", &|_, a| { *a.network.lock().unwrap() = net(&[(21, 1000 + 64 * 1024, &[])]); }),
        ("someone kept it", &|s, _| s.exec("INSERT INTO agentd_keeps(kind, target, until, set_by, set_at) VALUES ('stack', 'rr-x', NULL, 'sam', 'x')")),
        ("someone kept its container by name", &|s, _| s.exec("INSERT INTO agentd_keeps(kind, target, until, set_by, set_at) VALUES ('stack', 'rr-x-app', NULL, 'sam', 'x')")),
    ];
    for (why, setup_use) in cases {
        let s = setup();
        s.folder("V-x");
        s.conversation("V-x", 5, None);
        let apps = Fake::new("__END__");
        *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-x-app", project: "rr-x", folder: Some("V-x"), pid: 21, ..Default::default() }]);
        *apps.network.lock().unwrap() = net(&[(21, 1000, &[])]);
        pass_at(&s, &apps, None, s.now).await;
        setup_use(&s, &apps);
        let report = pass_at(&s, &apps, None, s.now + Duration::hours(2)).await;
        assert!(report.stopped_stacks.is_empty(), "{why}: {:?}", report.stopped_stacks);
        assert!(!apps.calls().iter().any(|c| c.starts_with("stop")), "{why}");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn connections_inside_the_stack_and_loopback_do_not_count() {
    let s = setup();
    s.folder("V-x");
    s.conversation("V-x", 5, None);
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-x-db", project: "rr-x", folder: Some("V-x"), pid: 21, ..Default::default() },
                                                          Box_ { id: "rr-x-app", project: "rr-x", folder: Some("V-x"), pid: 22, ..Default::default() }]);
    // the app (10.201.0.24) holds a connection to the database (10.201.0.23); both talk to themselves on loopback
    *apps.network.lock().unwrap() = net(&[(21, 1000, &["1800C90A", "0100007F"]), (22, 500, &["1700C90A"])]);
    pass_at(&s, &apps, None, s.now).await;
    let report = pass_at(&s, &apps, None, s.now + Duration::hours(2)).await;
    assert_eq!(report.stopped_stacks, ["rr-x"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_session_whose_processes_cannot_be_read_stops_nothing() {
    let s = setup();
    s.folder("V-stale");
    s.conversation("V-stale", 5, None);
    s.exec("INSERT INTO agentd_containers(conversation_id, name, created_at) VALUES ('V-other', 'n', 'x')");
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-stale-app", project: "rr-stale", folder: Some("V-stale"), pid: 11, ..Default::default() }]);
    *apps.network.lock().unwrap() = net(&[(11, 1000, &[])]);
    pass_at(&s, &apps, None, s.now).await;
    apps.blind.lock().unwrap().insert("V-other".into());
    let report = pass_at(&s, &apps, None, s.now + Duration::hours(2)).await;
    assert!(report.stopped_stacks.is_empty() && !report.failures.is_empty());
    assert!(!apps.calls().iter().any(|c| c.starts_with("stop")));
}

#[tokio::test(flavor = "multi_thread")]
async fn volumes_go_only_when_rebuildable_and_unused_long_enough_and_databases_are_asked_about() {
    let s = setup();
    s.folder("V-old");
    s.conversation("V-old", 9, None);
    s.exec("UPDATE conversations SET channel = 'C-OLD', thread_ts = '111.222' WHERE id = 'V-old'");
    let apps = Fake::new("__END__");
    // a stopped stack: its database (postgres 18 keeps data deeper), its node_modules volume, and a volume of unknown use
    *apps.containers_json.lock().unwrap() = inspect(&s, &[
        Box_ { id: "rr-old-db", project: "rr-old", folder: Some("V-old"), running: false, days: 9.0, image: "postgres:18",
               volumes: vec![("rr-old_pg", "/var/lib/postgresql")], ..Default::default() },
        Box_ { id: "rr-old-app", project: "rr-old", folder: Some("V-old"), running: false, days: 9.0,
               volumes: vec![("rr-old_api_node_modules", "/app/node_modules"), ("a".repeat(64).leak(), "/srv/uploads")], ..Default::default() }]);
    *apps.volumes_json.lock().unwrap() = volumes(&s, &[("rr-old_pg", "rr-old", 9), ("rr-old_api_node_modules", "rr-old", 9), (&"a".repeat(64), "", 9)]);
    let asker = Arc::new(Asker::default());

    // the first pass removes the containers; their volumes are unused from now on
    let first = pass_at(&s, &apps, Some(asker.clone()), s.now).await;
    assert_eq!(first.removed_containers.len(), 2, "{first:?}");
    assert!(first.removed_volumes.is_empty() && first.asked_about.is_empty(), "unused for no time yet: {first:?}");
    *apps.containers_json.lock().unwrap() = "[]".into();
    let two_days = pass_at(&s, &apps, Some(asker.clone()), s.now + Duration::days(2)).await;
    assert!(two_days.removed_volumes.is_empty() && two_days.asked_about.is_empty());

    let later = pass_at(&s, &apps, Some(asker.clone()), s.now + Duration::days(4)).await;
    assert_eq!(later.removed_volumes, ["rr-old_api_node_modules"]);
    assert_eq!(later.asked_about, ["rr-old_pg"]);
    assert_eq!(later.kept_volumes, 1, "the volume of unknown use stays");
    let calls = apps.calls();
    assert!(calls.contains(&"volume rm rr-old_api_node_modules".to_string()));
    assert!(!calls.iter().any(|c| c.starts_with("volume rm") && (c.contains("rr-old_pg") || c.contains("aaaa"))), "{calls:?}");
    let asked = asker.asked.lock().unwrap().clone();
    assert_eq!(asked.len(), 1);
    assert!(asked[0].contains("`rr-old_pg`") && asked[0].contains("channel C-OLD") && asked[0].contains("C-OLD/111.222")
            && asked[0].contains("postgres:18") && asked[0].contains("only the volumes named"), "{}", asked[0]);
    assert!(fs::read_to_string(s.settings.scratch().join("V-old").join(NOTES)).unwrap().contains("volume `rr-old_api_node_modules`"));

    // asked once; asked again only if that turn failed
    pass_at(&s, &apps, Some(asker.clone()), s.now + Duration::days(6)).await;
    assert_eq!(asker.asked.lock().unwrap().len(), 1);
    s.exec("INSERT INTO work_items(id, kind, payload, status, created_at, updated_at) VALUES ('w-ask', 'turn', '{}', 'failed', 'x', 'x')");
    let again = pass_at(&s, &apps, Some(asker.clone()), s.now + Duration::days(8)).await;
    assert_eq!(again.asked_about, ["rr-old_pg"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_volume_unmounted_just_now_is_not_unused_however_old() {
    // a busy conversation ran `docker compose down`: its 9-day-old volumes lost their containers a minute ago
    let s = setup();
    s.folder("V-busy");
    s.conversation("V-busy", 0, None);
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-b-app", project: "rr-b", folder: Some("V-busy"), pid: 5,
        volumes: vec![("rr-b_cache", "/root/.cache")], ..Default::default() }]);
    *apps.volumes_json.lock().unwrap() = volumes(&s, &[("rr-b_cache", "rr-b", 9)]);
    pass_at(&s, &apps, None, s.now).await;
    *apps.containers_json.lock().unwrap() = "[]".into();
    let report = pass_at(&s, &apps, None, s.now + Duration::minutes(5)).await;
    assert!(report.removed_volumes.is_empty());
    // three quiet days later it goes
    s.exec(&format!("UPDATE conversations SET last_active_at = '{}' WHERE id = 'V-busy'", keeps::fmt(s.now)));
    let later = pass_at(&s, &apps, None, s.now + Duration::days(4)).await;
    assert_eq!(later.removed_volumes, ["rr-b_cache"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stopped_container_with_data_of_its_own_stays() {
    let s = setup();
    s.folder("V-old");
    s.conversation("V-old", 9, None);
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[
        Box_ { id: "rr-o-search", project: "rr-o", folder: Some("V-old"), running: false, days: 9.0, image: "elasticsearch:8", ..Default::default() },
        Box_ { id: "rr-o-app", project: "rr-o", folder: Some("V-old"), running: false, days: 9.0, ..Default::default() },
        Box_ { id: "rr-o-big", project: "rr-o", folder: Some("V-old"), running: false, days: 9.0, ..Default::default() }]);
    apps.sizes.lock().unwrap().insert("rr-o-big".into(), 500 * 1024 * 1024);
    let report = pass_at(&s, &apps, None, s.now).await;
    assert_eq!(report.removed_containers, ["rr-o-app"]);
    let mut kept = report.kept_containers.clone();
    kept.sort();
    assert_eq!(kept, ["rr-o-big", "rr-o-search"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn images_go_once_unused_for_the_idle_period() {
    let s = setup();
    let apps = Fake::new("__END__");
    *apps.images.lock().unwrap() = "sha256:used\tnode:22\nsha256:old\tpostgres:17\nsha256:dangling\t<none>:<none>\n".into();
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "c", project: "p", running: false, days: 1.0, image: "used", ..Default::default() }]);
    let first = pass_at(&s, &apps, None, s.now).await;
    assert_eq!(first.removed_images, 0, "first sight starts the clock");
    let later = pass_at(&s, &apps, None, s.now + Duration::days(4)).await;
    assert_eq!(later.removed_images, 2);
    let calls = apps.calls();
    assert!(calls.contains(&"image rm postgres:17".to_string()) && calls.contains(&"image rm sha256:dangling".to_string()), "{calls:?}");
    assert!(!calls.iter().any(|c| c.contains("node:22") && c.starts_with("image rm")));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_dry_run_of_the_docker_step_changes_and_remembers_nothing() {
    let s = setup();
    s.folder("V-stale");
    s.conversation("V-stale", 5, None);
    let apps = Fake::new("__END__");
    *apps.containers_json.lock().unwrap() = inspect(&s, &[Box_ { id: "rr-stale-app", project: "rr-stale", folder: Some("V-stale"), pid: 11, ..Default::default() }]);
    *apps.network.lock().unwrap() = net(&[(11, 1000, &[])]);
    pass(s.db.clone(), apps.clone(), Some(Arc::new(Asker::default())), s.settings.clone(), s.now, true).await.unwrap();
    assert!(apps.calls().iter().all(|c| c.starts_with("ps") || c.starts_with("volume ls") || c.starts_with("images")), "{:?}", apps.calls());
    let remembered: i64 = s.db.lock().query_row("SELECT COUNT(*) FROM agentd_cleanup_stacks", [], |r| r.get(0)).unwrap();
    assert_eq!(remembered, 0);
    assert!(!s.settings.scratch().join("V-stale").join(NOTES).exists());
}
