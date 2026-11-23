//! Containers against the real Docker daemon and agent image; run on the host in the live check, as root (the holder's
//! socket belongs to the agent user) with the Docker socket and the `agentwork` volume mounted at /var/agent:
//! `cargo test -p agentd --test containers_docker -- --ignored`. Settings come from AGENTD_TEST_IMAGE,
//! AGENTD_TEST_DATA_DIR, AGENTD_TEST_WORK_VOLUME and AGENTD_TEST_NETWORK.

use std::time::Duration as StdDuration;

use agentd::containers::{Containers, Docker, Engine, Host, HolderProbe, Probe};
use chrono::{Duration, Utc};
use holder_proto::{Request, Response, RUN_ROOT};

fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn setup(dir: &std::path::Path) -> Containers<Docker, HolderProbe> {
    let settings = agentd::config::Containers {
        image: env("AGENTD_TEST_IMAGE", "autonomous-agent-agent"), memory_gib: 2, cpus: 1.0, pids: 512,
        idle_stop_minutes: 60, max_age_hours: 24, keep_stopped_days: 7, min_free_gib: 3,
    };
    let host = Host {
        data_dir: env("AGENTD_TEST_DATA_DIR", "/tmp/agentd-test-data"),
        work_volume: env("AGENTD_TEST_WORK_VOLUME", "autonomous-agent_agentwork"),
        network_container: env("AGENTD_TEST_NETWORK", "agent-docker"),
        uid: 1001,
        run_root: RUN_ROOT.into(),
    };
    let db = agentd::db::Db::open(&dir.join("agent.db")).unwrap();
    Containers::new(Docker::connect().unwrap(), HolderProbe { run_root: RUN_ROOT.into() }, db, settings, host)
}

async fn container_id(name: &str) -> Option<String> {
    bollard::Docker::connect_with_local_defaults().unwrap().inspect_container(name, None).await.ok()?.id
}

#[tokio::test]
#[ignore]
async fn starts_reuses_restarts_and_deletes() {
    let (dir, conversation, name) = (tempfile::tempdir().unwrap(), "agentd-test-reuse", "agent-conv-agentd-test-reuse");
    let c = setup(dir.path());
    Docker::connect().unwrap().remove(name).await.unwrap(); // left by an earlier failed run
    let t0 = Utc::now();
    c.ensure(conversation, t0).await.unwrap();
    let first = container_id(name).await.expect("created");
    c.ensure(conversation, t0).await.unwrap();
    assert_eq!(container_id(name).await.as_ref(), Some(&first), "reused");

    c.sweep(t0 + Duration::hours(2)).await.unwrap();
    assert_eq!(Docker::connect().unwrap().inspect(name).await.unwrap(), Some(false), "idle: stopped");
    c.ensure(conversation, t0 + Duration::hours(2)).await.unwrap();
    assert_eq!(container_id(name).await.as_ref(), Some(&first), "the same container started again");
    assert!(HolderProbe { run_root: RUN_ROOT.into() }.status(conversation).await.is_some(), "its holder answers");

    c.sweep(t0 + Duration::hours(4)).await.unwrap();
    c.sweep(t0 + Duration::hours(4) + Duration::days(7)).await.unwrap();
    assert_eq!(container_id(name).await, None, "stopped 7 days: deleted");
    assert!(!std::path::Path::new(RUN_ROOT).join(conversation).exists(), "with its run folder");
}

#[tokio::test]
#[ignore]
async fn idle_stop_skips_container_with_listener() {
    let (dir, conversation, name) =
        (tempfile::tempdir().unwrap(), "agentd-test-listener", "agent-conv-agentd-test-listener");
    let c = setup(dir.path());
    let docker = Docker::connect().unwrap();
    docker.remove(name).await.unwrap();
    let t0 = Utc::now();
    c.ensure(conversation, t0).await.unwrap();
    // a turn that leaves a server behind, as a demo would
    let server = "python3 -c 'import socket,time; s=socket.socket(); s.bind((\"127.0.0.1\",0)); s.listen(); \
                  time.sleep(3600)' >/dev/null 2>&1 &";
    let socket = std::path::Path::new(RUN_ROOT).join(conversation).join("holder.sock");
    let start = Request::Start { work_item: "W-test".into(), argv: vec!["sh".into(), "-c".into(), server.into()],
                                 env: Default::default(), cwd: "/tmp".into(), stdin: vec![], tools: vec![] };
    assert!(matches!(agent_holder::client::request(&socket, &start).await.unwrap(), Response::Started { .. }));
    let probe = HolderProbe { run_root: RUN_ROOT.into() };
    let mut status = None;
    for _ in 0..100 {
        status = probe.status(conversation).await.filter(|s| !s.alive && !s.listeners.is_empty());
        if status.is_some() {
            break;
        }
        tokio::time::sleep(StdDuration::from_millis(100)).await;
    }
    assert!(status.is_some(), "the holder reports the listener after the turn ended");
    c.turn_ended(conversation, t0).await.unwrap();

    c.sweep(t0 + Duration::hours(2)).await.unwrap();
    assert_eq!(docker.inspect(name).await.unwrap(), Some(true), "serving: kept");
    c.sweep(t0 + Duration::hours(25)).await.unwrap();
    assert_eq!(docker.inspect(name).await.unwrap(), Some(false), "24 hours: stopped");
    docker.remove(name).await.unwrap();
    std::fs::remove_dir_all(std::path::Path::new(RUN_ROOT).join(conversation)).ok();
}
