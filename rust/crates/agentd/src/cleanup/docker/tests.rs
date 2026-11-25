use super::*;

#[test]
fn parse_containers_reads_stack_folders_volumes_ports_addresses_and_times() {
    let json = r#"[{"Id": "a1", "Name": "/rr-x-app-1", "Created": "2026-11-20T00:00:00Z", "Image": "sha256:abc",
        "State": {"Status": "running", "Pid": 4242, "StartedAt": "2026-11-21T00:00:00.5Z", "FinishedAt": "0001-01-01T00:00:00Z"},
        "Config": {"Image": "postgres:18", "Labels": {"com.docker.compose.project": "rr-x",
                   "com.docker.compose.project.working_dir": "/var/agent/scratch/V-1/app"}},
        "Mounts": [{"Type": "bind", "Source": "/var/agent/scratch/V-2/data"}, {"Type": "volume", "Name": "rr-x_pg", "Destination": "/var/lib/postgresql"},
                   {"Type": "bind", "Source": "/var/run/docker.sock"}],
        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18096"}], "9229/tcp": null},
                            "Networks": {"rr-x_default": {"IPAddress": "10.201.3.2", "GlobalIPv6Address": ""}}}},
        {"Id": "b2", "Name": "/loose", "Created": "2026-11-19T00:00:00Z", "Image": "sha256:def",
         "State": {"Status": "exited", "Pid": 0, "StartedAt": "2026-11-19T00:00:00Z", "FinishedAt": "2026-11-22T00:00:00Z"},
         "Config": {"Image": "node:22", "Labels": null}, "Mounts": [], "NetworkSettings": {"Ports": {}, "Networks": {}}}]"#;
    let got = parse_containers(json, Path::new("/var/agent/scratch")).unwrap();
    let a = &got[0];
    assert_eq!((a.stack.as_str(), a.running, a.pid, a.image.as_str(), a.image_id.as_str()), ("rr-x", true, 4242, "postgres:18", "sha256:abc"));
    assert_eq!(a.folders, BTreeSet::from(["V-1".to_string(), "V-2".to_string()]));
    assert_eq!(a.volumes, BTreeMap::from([("rr-x_pg".to_string(), "/var/lib/postgresql".to_string())]));
    assert_eq!(a.ports, BTreeSet::from([18096]));
    assert_eq!(a.ips, BTreeSet::from(["10.201.3.2".parse::<IpAddr>().unwrap()]));
    assert_eq!(keeps::fmt(a.since), "2026-11-21T00:00:00Z");
    assert_eq!((got[1].stack.as_str(), got[1].running, got[1].pid, keeps::fmt(got[1].since)), ("loose", false, 0, "2026-11-22T00:00:00Z".into()));
    assert!(parse_containers("Error: No such object", Path::new("/s")).is_err());
}

#[test]
fn parse_net_sums_traffic_off_loopback_and_lists_far_ends() {
    let text = "== 12\nInter-|   Receive                                                |  Transmit\n \
                face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n    \
                lo:  500 5 0 0 0 0 0 0  500 5 0 0 0 0 0 0\n  eth0: 1000 9 0 0 0 0 0 0 2000 9 0 0 0 0 0 0\n  \
                sl  local_address rem_address   st tx_queue\n   \
                0: 0201C90A:1F90 0101C90A:C350 01 00000000:00000000\n   \
                1: 0100007F:1538 0100007F:9C40 01 00000000:00000000\n   \
                2: 00000000:1F90 00000000:0000 0A 00000000:00000000\n   \
                0: 0000000000000000FFFF00000100007F:1F90 0000000000000000FFFF00000100007F:C351 01 0:0\n\
                == 13\n  eth0: 7 0 0 0 0 0 0 0 3 0\n";
    let got = parse_net(text);
    assert_eq!(got[&12], Net { bytes: 3000, peers: BTreeSet::from(["10.201.1.1".parse().unwrap()]) });
    assert_eq!(got[&13].bytes, 10);
}

#[test]
fn hex_ip_reads_both_families() {
    assert_eq!(hex_ip("0100007F:1F90"), Some("127.0.0.1".parse().unwrap()));
    assert_eq!(hex_ip("0201C90A:0050"), Some("10.201.1.2".parse().unwrap()));
    assert_eq!(hex_ip("0000000000000000FFFF00000100007F:1F90"), Some("::ffff:127.0.0.1".parse().unwrap()));
    assert_eq!(hex_ip("zz:1"), None);
    assert_eq!(hex_ip("0000000000000000FFFF00001800C90A:1F90").map(|ip| ip.to_canonical()), Some("10.201.0.24".parse().unwrap()));
}

#[test]
fn a_session_references_a_stack_by_name_folder_or_port() {
    let app = AppContainer { name: "rr-x-app-1".into(), stack: "rr-x".into(), ports: BTreeSet::from([18096]),
                             folders: BTreeSet::from(["V-1".to_string()]), ..Default::default() };
    let scratch = Path::new("/var/agent/scratch");
    for line in ["curl -s http://localhost:18096/api", "docker compose -p rr-x logs", "npm test --prefix /var/agent/scratch/V-1/app",
                 "docker exec rr-x-app-1 sh"] {
        assert!(referenced(line, "rr-x", &[&app], scratch), "{line}");
    }
    for line in ["curl http://localhost:180960", "sleep 118096", "node server.js",
                 "claude --system-prompt use ports 18090-18099 like 18096 --output-format stream-json"] {
        assert!(!referenced(line, "rr-x", &[&app], scratch), "{line}");
    }
}

fn vol(name: &str) -> AppVolume {
    AppVolume { name: name.into(), project: None, created: Utc::now(), mountpoint: format!("/v/{name}") }
}

#[test]
fn kind_of_needs_a_sign_to_remove_and_any_sign_of_a_database_to_ask() {
    let anon = "ab".repeat(32);
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    assert_eq!(kind_of(&vol("rr-x_api_node_modules"), &[], &[], None), Kind::Rebuildable);
    assert_eq!(kind_of(&vol("rr-s_search_cache"), &[], &[], Some(false)), Kind::Unknown, "a cache by name alone isn't enough");
    assert_eq!(kind_of(&vol("rr-s_build_cache"), &s(&["/root/.cache"]), &s(&["node:22"]), Some(false)), Kind::Rebuildable);
    assert_eq!(kind_of(&vol(&anon), &s(&["/app/node_modules"]), &s(&["node:22"]), Some(false)), Kind::Rebuildable);
    assert_eq!(kind_of(&vol("rr-x_postgres"), &[], &[], None), Kind::Database);
    assert_eq!(kind_of(&vol(&anon), &s(&["/var/lib/postgresql"]), &[], None), Kind::Database, "postgres 18's mount point");
    assert_eq!(kind_of(&vol(&anon), &s(&["/usr/share/elasticsearch/data"]), &[], None), Kind::Database);
    assert_eq!(kind_of(&vol(&anon), &s(&["/app/node_modules"]), &s(&["mcr.microsoft.com/mssql/server:2022"]), None), Kind::Database);
    assert_eq!(kind_of(&vol("cache"), &[], &[], Some(true)), Kind::Database, "a database's files win over a cache's name");
    assert_eq!(kind_of(&vol(&anon), &s(&["/srv/uploads"]), &s(&["node:22"]), Some(false)), Kind::Unknown);
    assert_eq!(kind_of(&vol(&anon), &[], &[], Some(false)), Kind::Unknown, "an orphan nobody explains stays");
    let anon_db = "0".repeat(30) + "db" + &"0".repeat(32);
    assert_eq!(kind_of(&vol(&anon_db), &[], &[], Some(false)), Kind::Unknown, "hex that reads 'db' means nothing");
}
