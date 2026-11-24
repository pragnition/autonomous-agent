"""The Host page's store and views from Python, for agentd's stage 4 tests. Run from rust/crates/agentd with
PYTHONPATH=<repo>/src; writes host.json."""
import json
from pathlib import Path

from agent.dashboard import host as h

HERE = Path("tests/fixtures/dashboard")
snap_a = {
    "ts": 1000.0, "interval_s": 5.0,
    "cpu": {"percent": 40.0, "per_core": [30.0, 50.0], "cores": 2, "load": [1.0, 0.5, 0.2]},
    "memory": {"total": 32e9, "available": 20e9, "used": 12e9, "swap_total": 4e9, "swap_used": 1e9},
    "network": {"rx_bps": 1000, "tx_bps": 500, "interfaces": [{"name": "eth0", "rx_bps": 1000, "tx_bps": 500}, "junk"]},
    "disk_io": {"read_bps": 10, "write_bps": 20},
    "disk_space": {"at": 990.0, "complete": True, "volumes": [{"path": "/var/agent", "total": 100, "used": 60, "free": 40}],
                   "top": [{"name": "C1", "kind": "conversation", "bytes": 30, "conversation": "C1"},
                           {"name": "npm cache", "kind": "cache", "bytes": 5, "conversation": 7}]},
    "consumers": [
        {"key": "session:C1", "kind": "session", "name": "C1", "item": "W1", "conversation": "C1", "cpu_percent": 20.0,
         "mem_bytes": 4e9, "read_bps": 1, "write_bps": 2, "rx_bps": None, "tx_bps": None, "processes": 3},
        {"key": "runner", "kind": "runner", "name": "runner", "item": None, "conversation": None, "cpu_percent": 5.0,
         "mem_bytes": 1e9},
        {"key": "weird", "kind": None, "name": "x", "item": 5, "conversation": ["no"], "cpu_percent": "a", "mem_bytes": True},
        "not a row",
    ],
    "containers": [{"name": "demo-web-1", "image": "demo", "state": "running", "status": "Up", "conversation": "C1",
                    "cpu_percent": 3.0, "mem_bytes": 2e8, "rx_bps": 1, "tx_bps": 1, "read_bps": 0, "write_bps": 0}],
    "ports": [{"port": 3000, "address": "0.0.0.0", "proto": "tcp", "pid": None, "process": None,
               "consumer": "session:C1", "kind": "session", "name": "C1", "item": "W1", "conversation": "C1"},
              {"port": 8080, "kind": None, "item": None, "conversation": None}],
}
snap_b = {**snap_a, "ts": 1005.0, "cpu": {"percent": 10.0}, "memory": {"used": 1e9}, "network": None,
          "disk_space": None, "consumers": [], "containers": "nope", "ports": None}
now = [0.0]
store = h.HostStore(keep_s=3600, clock=lambda: now[0])
links = {("W1", "C1"): {"id": "W1", "title": "Build the demo", "state": "running"},
         (None, "C1"): {"id": "W9", "title": "Older", "state": "done"}}


def session_of(item, convo):
    return links.get((item, convo))


def views():
    return {"host": h.host_view(store), "consumers": h.consumers_view(store, session_of)}


out = {"snapshots": [snap_a, snap_b], "links": [[k[0], k[1], v] for k, v in links.items()]}
out["empty"] = views()
steps = [("add", 0, 1000.0), ("view", None, 1005.0), ("add", 1, 1007.0), ("add", 0, 4601.0), ("view", None, 4602.0),
         ("view", None, 4700.0)]
out["steps"] = []
for what, which, at in steps:
    now[0] = at
    if what == "add":
        store.add([snap_a, snap_b][which])
        out["steps"].append({"add": which, "at": at})
    else:
        out["steps"].append({"view": views(), "at": at})
(HERE / "host.json").write_text(json.dumps(out, indent=1) + "\n")
print("ok")
