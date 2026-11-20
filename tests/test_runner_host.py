import asyncio
import json
import time

import httpx
import pytest

from agent.dashboard.host import HostStore
from agent.runner import host as h


def stat_line(pid, comm, ppid, sid, ticks, start=100, cticks=0):
    # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime
    # ... starttime
    fields = ["S", ppid, sid, sid, 0, 0, 0, 0, 0, 0, 0, ticks, 0, cticks, 0, 20, 0, 1, 0, start]
    return f"{pid} ({comm}) " + " ".join(str(f) for f in fields) + " 0 0\n"


class FakeProc:
    """A /proc with the files the sampler reads."""

    def __init__(self, root):
        self.root = root
        (root / "net").mkdir(parents=True)
        self.set_host(cpu=(100, 1000), rx=0, disk=0)
        (root / "net" / "tcp6").write_text("  sl  local_address\n")

    def set_host(self, cpu, rx, disk):
        busy, total = cpu
        (self.root / "stat").write_text(f"cpu  {busy} 0 0 {total - busy} 0 0 0 0\n"
                                        f"cpu0 {busy} 0 0 {total - busy} 0 0 0 0\nintr 1\n")
        (self.root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 100 kB\n"
                                           "SwapFree: 60 kB\n")
        (self.root / "loadavg").write_text("0.5 0.4 0.3 1/100 999\n")
        (self.root / "net" / "dev").write_text(
            "Inter-|\n face |\n"
            f"    lo: {rx} 0 0 0 0 0 0 0 {rx} 0 0 0 0 0 0 0\n"
            f"  eth0: {rx} 0 0 0 0 0 0 0 {2 * rx} 0 0 0 0 0 0 0\n"
            f"br-abc: {rx} 0 0 0 0 0 0 0 {rx} 0 0 0 0 0 0 0\n")
        (self.root / "diskstats").write_text(
            f" 259 0 nvme0n1 1 0 {disk} 0 1 0 {disk} 0 0 0 0\n"
            f" 259 1 nvme0n1p1 1 0 {disk} 0 1 0 {disk} 0 0 0 0\n"
            f"   7 0 loop0 1 0 {disk} 0 1 0 {disk} 0 0 0 0\n")
        (self.root / "net" / "tcp").write_text(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:1435 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001 0 555 1\n"
            "   1: 00000000:3E80 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001 0 777 1\n")

    def add(self, pid, comm, ppid, sid, ticks, *, env=None, argv0=None, pss_kb=1, io=(0, 0), sockets=(), start=100,
            cticks=0, cmdline=None):
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(stat_line(pid, comm, ppid, sid, ticks, start, cticks))
        (d / "smaps_rollup").write_text(f"00 [rollup]\nRss: {pss_kb * 2} kB\nPss: {pss_kb} kB\n")
        (d / "status").write_text(f"Name: {comm}\nVmRSS: {pss_kb * 2} kB\n")
        (d / "io").write_text(f"rchar: 1\nread_bytes: {io[0]}\nwrite_bytes: {io[1]}\n")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()))
        (d / "cmdline").write_bytes(cmdline if cmdline is not None else f"/usr/bin/{argv0 or comm}\0--flag".encode())
        fd = d / "fd"
        fd.mkdir(exist_ok=True)
        for i, inode in enumerate(sockets):
            (fd / str(i)).unlink(missing_ok=True)
            (fd / str(i)).symlink_to(f"socket:[{inode}]")


def session_env(item, convo):
    return {"PATH": "/bin", h.ITEM_VAR: item, h.CONVO_VAR: convo}


def test_parsers(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    assert h.read_cpu(proc.root)["cpu"] == (100, 1000)
    assert h.read_meminfo(proc.root)["MemAvailable"] == 400 * 1024
    assert h.read_net_dev(proc.root)["eth0"] == (0, 0)
    proc.set_host(cpu=(0, 1), rx=0, disk=10)
    assert h.read_diskstats(proc.root, {"nvme0n1"}) == (5120, 5120)
    assert h.read_diskstats(proc.root, None) == (5120, 5120)   # partitions and loops left out by name
    assert h.read_listening(proc.root) == [("127.0.0.1", 5173, "tcp", 555), ("0.0.0.0", 16000, "tcp", 777)]


def test_ipv6_address():
    assert h._addr("00000000000000000000000001000000:1F90") == "::1:8080"


def test_classify_sessions_services_browsers_and_ancestry(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(1, "docker-init", 0, 1, 0)
    proc.add(10, "python", 1, 1, 0)                                            # the runner
    proc.add(20, "claude", 10, 1, 0, env=session_env("W-1", "V-1"))
    proc.add(21, "node", 20, 1, 0, env={"PATH": "/bin"})                       # env cleared: its parent's session
    proc.add(22, "chrome", 20, 1, 0, env=session_env("W-1", "V-1"))
    proc.add(30, "python3", 10, 30, 0)                                         # a service, its own process session
    proc.add(31, "worker", 30, 30, 0)
    proc.add(40, "sleep", 0, 40, 0)                                            # nobody's
    procs = {pid: h.read_proc(proc.root, pid) for pid in (1, 10, 20, 21, 22, 30, 31, 40)}
    for p in procs.values():
        p.tag, p.argv0 = h.read_tag(proc.root, p.pid)
    kinds = h.classify(procs, {30: "bridge"}, me=10)
    assert kinds[1][0] == kinds[10][0] == "runner"
    assert kinds[20] == kinds[21] == ("session", "W-1", "W-1", "V-1")
    assert kinds[22] == ("browser", "W-1", "W-1", "V-1")
    assert kinds[30] == kinds[31] == ("service", "bridge", None, None)
    assert kinds[40][0] == "other"


def test_container_numbers_and_conversation(tmp_path):
    n = h.container_numbers({"cpu_stats": {"cpu_usage": {"total_usage": 5}},
                             "memory_stats": {"usage": 100, "stats": {"inactive_file": 30}},
                             "networks": {"eth0": {"rx_bytes": 1, "tx_bytes": 2}, "eth1": {"rx_bytes": 3,
                                                                                         "tx_bytes": 4}},
                             "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": 7},
                                                                            {"op": "write", "value": 9}]}})
    assert n == {"cpu": 5, "mem": 70, "rx": 4, "tx": 6, "read": 7, "write": 9}
    assert h.container_numbers({"blkio_stats": {"io_service_bytes_recursive": None}})["read"] == 0
    scratch = tmp_path / "scratch"
    assert h.conversation_of(str(scratch / "V-1" / "repo"), scratch) == "V-1"
    assert h.conversation_of("/elsewhere", scratch) is None and h.conversation_of(None, scratch) is None


def test_measure_space(tmp_path):
    root = tmp_path / "agent"
    (root / "scratch" / "V-1").mkdir(parents=True)
    (root / "scratch" / "V-1" / "big").write_bytes(b"x" * 200_000)
    (root / "home" / ".npm").mkdir(parents=True)
    (root / "home" / ".npm" / "pkg").write_bytes(b"x" * 50_000)
    (root / "transcripts").mkdir()
    (root / "scratch" / "V-1" / "loop").symlink_to(root)   # never followed
    space = h.measure_space(root, tmp_path / "no-tmp")
    top = {r["name"]: r for r in space["top"]}
    assert space["complete"] and space["volumes"][0]["path"] == str(root)
    assert top["V-1"]["kind"] == "conversation" and top["V-1"]["bytes"] >= 200_000
    assert top["npm cache"]["bytes"] >= 50_000 and top["npm cache"]["kind"] == "cache"
    assert top["home (other)"]["bytes"] < top["npm cache"]["bytes"]
    assert h.measure_space(root, tmp_path, budget_s=-1)["complete"] is False


def docker_transport(cpu_ns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[{"Id": "c1", "Names": ["/app-db-1"], "Image": "postgres",
                                              "State": "running", "Status": "Up",
                                              "Labels": {"com.docker.compose.project.working_dir":
                                                         "/agent/scratch/V-1/repo"},
                                              "Ports": [{"PrivatePort": 5432, "PublicPort": 16000}]}])
        if request.url.path == "/containers/c1/stats":
            return httpx.Response(200, json={"cpu_stats": {"cpu_usage": {"total_usage": cpu_ns[0]}},
                                             "memory_stats": {"usage": 4096},
                                             "networks": {"eth0": {"rx_bytes": cpu_ns[0], "tx_bytes": 0}}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_sampler_attributes_use_to_consumers(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(10, "python", 1, 1, 0, pss_kb=10)
    proc.add(20, "claude", 10, 1, 0, env=session_env("W-1", "V-1"), pss_kb=100, sockets=[555])
    proc.add(22, "chrome", 20, 1, 0, env=session_env("W-1", "V-1"), pss_kb=300)
    (tmp_path / "agent" / "run").mkdir(parents=True)
    cpu_ns = [0]
    now = [100.0]
    sampler = h.HostSampler(tmp_path / "agent", proc=proc.root, sys_block=tmp_path / "none", tmp=tmp_path / "t",
                            http=httpx.AsyncClient(transport=docker_transport(cpu_ns)), me=10,
                            clock=lambda: now[0], wall=lambda: 1234.0)
    sampler.clk = 100
    sampler._space_at = now[0]   # no folder walk in this test
    assert await sampler.sample() is None   # rates need two readings

    now[0] += 10
    proc.set_host(cpu=(600, 2000), rx=1000, disk=20)
    proc.add(20, "claude", 10, 1, 500, env=session_env("W-1", "V-1"), pss_kb=100, io=(0, 4000), sockets=[555])
    proc.add(22, "chrome", 20, 1, 100, env=session_env("W-1", "V-1"), pss_kb=300)
    cpu_ns[0] = 2 * 10**9
    snap = await sampler.sample()
    json.dumps(snap)  # what's posted to core

    assert snap["cpu"]["percent"] == 50.0 and snap["cpu"]["cores"] == 1
    assert snap["memory"]["used"] == 600 * 1024 and snap["memory"]["swap_used"] == 40 * 1024
    assert snap["network"]["rx_bps"] == 100 and snap["network"]["tx_bps"] == 200  # eth0 only, not lo or bridges
    assert snap["disk_io"] == {"read_bps": 1024, "write_bps": 1024}
    rows = {c["key"]: c for c in snap["consumers"]}
    assert rows["session:W-1"]["cpu_percent"] == 50.0 and rows["session:W-1"]["mem_bytes"] == 100 * 1024
    assert rows["session:W-1"]["write_bps"] == 400 and rows["session:W-1"]["conversation"] == "V-1"
    assert rows["browser:W-1"]["cpu_percent"] == 10.0 and rows["browser:W-1"]["item"] == "W-1"
    assert rows["runner"]["mem_bytes"] == 10 * 1024
    # PSS is read once a minute; meanwhile a process keeps the PSS it had
    proc.add(10, "python", 1, 1, 0, pss_kb=50)
    now[0] += 10
    assert {c["key"]: c for c in (await sampler.sample())["consumers"]}["runner"]["mem_bytes"] == 10 * 1024
    now[0] += h.PSS_EVERY_S
    assert {c["key"]: c for c in (await sampler.sample())["consumers"]}["runner"]["mem_bytes"] == 50 * 1024
    db = rows["container:app-db-1"]
    assert db["cpu_percent"] == 20.0 and db["mem_bytes"] == 4096 and db["rx_bps"] == 2 * 10**8
    assert snap["containers"][0]["conversation"] is None   # its folder isn't under this root's scratch
    ports = {p["port"]: p for p in snap["ports"]}
    assert ports[5173]["consumer"] == "session:W-1" and ports[5173]["process"] == "claude"
    assert ports[16000]["consumer"] == "container:app-db-1" and ports[16000]["process"] == "docker-proxy"

    store = HostStore(clock=lambda: 0)
    store.add(snap)   # core takes what the runner posts as it is
    assert store.points[0]["cpu_by_kind"]["browser"] == 10.0


async def test_sampler_survives_no_docker(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(10, "python", 1, 1, 0)

    def down(request):
        raise httpx.ConnectError("refused")
    sampler = h.HostSampler(tmp_path, proc=proc.root, http=httpx.AsyncClient(transport=httpx.MockTransport(down)),
                            me=10)
    sampler._space_at = time.monotonic()
    await sampler.sample()
    snap = await sampler.sample()
    assert snap["containers"] == [] and snap["consumers"][0]["key"] == "runner"


async def test_report_forever_keeps_going_after_a_failure():
    posted, calls = [], [0]

    class Sampler:
        async def sample(self):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("boom")
            return {"n": calls[0]}

    async def post(snap):
        posted.append(snap)
    task = asyncio.create_task(h.report_forever(Sampler(), post, every_s=0.01))
    while len(posted) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert posted[:2] == [{"n": 2}, {"n": 3}]


async def test_core_poster_uses_the_current_token():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})
    tokens = iter(["", "tok"])
    post = h.core_poster("http://core:8080/", lambda: next(tokens),
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await post({"ts": 1})   # no token yet: nothing posted
    await post({"ts": 2})
    assert seen == [("/agent/host", "Bearer tok")]


def test_browser_children_and_retitled_processes_count_as_browser(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(20, "claude", 1, 1, 0, env=session_env("W-1", "V-1"))
    proc.add(21, "firefox", 20, 1, 0, env=session_env("W-1", "V-1"))
    proc.add(22, "Web Content", 21, 1, 0, env=session_env("W-1", "V-1"),
             cmdline=b"/usr/lib/firefox/firefox -contentproc 8 tab")
    proc.add(23, "Isolated Web Co", 21, 1, 0, env=session_env("W-1", "V-1"), cmdline=b"browser 8 tab")
    procs = {pid: h.read_proc(proc.root, pid) for pid in (20, 21, 22, 23)}
    for p in procs.values():
        p.tag, p.argv0 = h.read_tag(proc.root, p.pid)
    assert procs[23].argv0 == "browser" and procs[22].argv0 == "firefox"
    kinds = h.classify(procs, {}, me=10)
    assert kinds[20][0] == "session"
    assert kinds[21][0] == kinds[22][0] == kinds[23][0] == "browser"


async def test_new_and_ended_processes_are_counted(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    (proc.root / "uptime").write_text("10.00 5.00\n")          # 1000 ticks since boot
    proc.add(20, "cargo", 1, 1, 0, env=session_env("W-1", "V-1"))
    proc.add(21, "rustc", 20, 1, 100, env=session_env("W-1", "V-1"), start=900)
    now = [100.0]
    sampler = h.HostSampler(tmp_path / "agent", proc=proc.root, sys_block=tmp_path / "none",
                            http=httpx.AsyncClient(transport=httpx.MockTransport(
                                lambda r: httpx.Response(200, json=[]))), me=10, clock=lambda: now[0])
    sampler.clk, sampler._space_at = 100, now[0]
    await sampler.sample()
    now[0] += 10
    (proc.root / "uptime").write_text("20.00 5.00\n")
    proc.set_host(cpu=(0, 1), rx=0, disk=0)
    # rustc ended after 300 ticks in all (100 counted already), cargo waited for it; a new rustc started since
    import shutil
    shutil.rmtree(proc.root / "21")
    proc.add(20, "cargo", 1, 1, 0, env=session_env("W-1", "V-1"), cticks=300)
    proc.add(22, "rustc", 20, 1, 400, env=session_env("W-1", "V-1"), start=1500)
    proc.add(23, "old", 1, 1, 999, start=500)                 # older than the last sample but never seen: not counted
    snap = await sampler.sample()
    rows = {c["key"]: c for c in snap["consumers"]}
    # (300 - 100 already counted) + 400 new = 600 ticks over 10 s at 100 ticks/s on one core
    assert rows["session:W-1"]["cpu_percent"] == 60.0
    assert rows["other"]["cpu_percent"] == 0.0


def test_exec_reads_the_tag_again(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(20, "python", 1, 1, 0)
    sampler = h.HostSampler(tmp_path, proc=proc.root, me=10)
    assert sampler._procs()[20].tag is None
    proc.add(20, "claude", 1, 1, 0, env=session_env("W-1", "V-1"))   # the same process after exec
    assert sampler._procs()[20].tag == ("W-1", "V-1")
    assert len(sampler.tags) == 1


def test_tree_bytes_stops_early_and_counts_hard_links_once(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_bytes(b"x" * 100_000)
    (d / "b").hardlink_to(d / "a")
    alone, done = h.tree_bytes(d, time.monotonic() + 60)
    assert done and alone < 2 * 100_000
    assert h.tree_bytes(d, time.monotonic() + 60, stop=lambda: True)[1] is False
