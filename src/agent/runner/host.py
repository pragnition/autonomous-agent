"""What the machine the agent runs on is doing: CPU, memory, network, disk I/O, disk space, the agent's Docker
containers and the ports something listens on. The dashboard shows it (see agent.dashboard.host).

The sampler runs in the runner process, because only the agent container sees the sessions, the services and the
browsers, and only it reaches the agent's own Docker daemon (on 127.0.0.1:2375, the network it shares with it).
/proc/stat, /proc/meminfo and /proc/loadavg are the host's own, so the totals are the host's; the processes are the
agent container's. Every few seconds it reads /proc and the Docker API and posts one snapshot to core with the
runner token, and core keeps the last hour in memory, so the history outlives a runner restart.

Each process is counted for one consumer:
- a service the runner keeps (services.py): any process in the service's process session (run/<name>.pid);
- a session: the runner sets AGENT_WORK_ITEM and AGENT_CONVERSATION in every session's environment, so the
  processes a session starts carry them, even a server it left running; a process without them takes its nearest
  ancestor's consumer (the process tree from the session's root);
- a browser (Chromium, Firefox, WebKit) counts on its own, still linked to the session that started it;
- the runner itself (and the container's init); anything else is "other".
A container is a consumer of its own, linked to the conversation whose folder its compose project is in.

Folder sizes take a while to add up, so disk space is measured every 10 minutes in a thread, and a process's PSS
(its fair share of memory, which the kernel works out by walking its page tables) once a minute; everything else is
cheap enough for every sample, a few tens of milliseconds for a hundred processes. Nothing here can stop the runner: a sample that fails is logged and skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

ITEM_VAR, CONVO_VAR = "AGENT_WORK_ITEM", "AGENT_CONVERSATION"
SAMPLE_S = 5.0
SPACE_EVERY_S = 600.0
PSS_EVERY_S = 60.0
SPACE_BUDGET_S = 300.0   # the most one disk-space measurement walks folders for; past it the sizes are partial
DOCKER_URL = "http://127.0.0.1:2375"
MAX_ROWS = 200
TOP_CONVERSATIONS = 20
BROWSER = re.compile(r"chrom|headless_shell|firefox|webkit|msedge", re.I)
SKIP_DISKS = re.compile(r"^(loop|ram|zram|dm-|md|sr|fd)")
VIRTUAL_NET = re.compile(r"^(lo|br-|veth|docker\d|virbr|cni|flannel|cali|tailscale|tun|tap)")  # bridges and the like
#   carry the same bytes as the interface they lead to, so the host's totals leave them out
MAX_INTERFACES = 12
PARTITION = re.compile(r"^(sd|vd|xvd|hd)[a-z]+\d+$|^(nvme\d+n\d+|mmcblk\d+)p\d+$")
CACHES = {".npm": "npm cache", ".cache/pip": "pip cache", ".cache/uv": "uv cache",
          ".cache/ms-playwright": "playwright cache", ".bun/install/cache": "bun cache", ".cache/go-build": "go cache"}


# --- /proc readers: each takes the /proc root, so tests can hand them a fake one -----------------------------------

def read_cpu(proc: Path) -> dict[str, tuple[int, int]]:
    """Per line of /proc/stat ("cpu", "cpu0", ...): (busy, total) jiffies."""
    out = {}
    for line in (proc / "stat").read_text().splitlines():
        if not line.startswith("cpu"):
            continue
        name, *vals = line.split()
        nums = [int(v) for v in vals[:8]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        out[name] = (total - idle, total)
    return out


def read_meminfo(proc: Path) -> dict[str, int]:
    out = {}
    for line in (proc / "meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return out


def read_loadavg(proc: Path) -> list[float]:
    return [float(x) for x in (proc / "loadavg").read_text().split()[:3]]


def read_net_dev(proc: Path) -> dict[str, tuple[int, int]]:
    """Per interface: (rx bytes, tx bytes)."""
    out = {}
    for line in (proc / "net" / "dev").read_text().splitlines()[2:]:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) >= 9:
            out[name.strip()] = (int(parts[0]), int(parts[8]))
    return out


def whole_disks(sys_block: Path) -> set[str] | None:
    try:
        return {p.name for p in sys_block.iterdir() if not SKIP_DISKS.match(p.name)}
    except OSError:
        return None


def read_diskstats(proc: Path, disks: set[str] | None) -> tuple[int, int]:
    """Bytes read and written on the host's whole disks (partitions and the devices layered on them would count
    the same bytes twice)."""
    read = written = 0
    for line in (proc / "diskstats").read_text().splitlines():
        parts = line.split()
        if len(parts) < 10:
            continue
        name = parts[2]
        if disks is not None and name not in disks:
            continue
        if disks is None and (SKIP_DISKS.match(name) or PARTITION.search(name)):
            continue
        read += int(parts[5]) * 512
        written += int(parts[9]) * 512
    return read, written


@dataclass
class Proc:
    pid: int
    ppid: int
    sid: int
    start: int
    comm: str
    ticks: int
    mem: int = 0
    io: tuple[int, int] = (0, 0)
    tag: tuple[str, str] | None = None  # (work item, conversation)
    argv0: str = ""


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_proc(proc: Path, pid: int) -> Proc | None:
    raw = _read(proc / str(pid) / "stat")
    if raw is None:
        return None
    text = raw.decode(errors="replace")
    head, _, tail = text.rpartition(")")
    comm = head.partition("(")[2]
    f = tail.split()
    if len(f) < 20 or f[0] in ("Z", "X"):
        return None
    # after the command: state ppid pgrp session ... utime(11) stime(12) ... starttime(19)
    p = Proc(pid=pid, ppid=int(f[1]), sid=int(f[3]), start=int(f[19]), comm=comm, ticks=int(f[11]) + int(f[12]))
    status = _read(proc / str(pid) / "status")
    m = re.search(rb"^VmRSS:\s+(\d+) kB", status or b"", re.M)
    p.mem = int(m.group(1)) * 1024 if m else 0
    io = _read(proc / str(pid) / "io")
    if io:
        r = re.search(rb"^read_bytes:\s+(\d+)", io, re.M)
        w = re.search(rb"^write_bytes:\s+(\d+)", io, re.M)
        p.io = (int(r.group(1)) if r else 0, int(w.group(1)) if w else 0)
    return p


def read_pss(proc: Path, pid: int) -> int | None:
    """A process's proportional share of memory (shared pages split among the processes that map them), which
    adds up across processes where RSS counts shared pages over and over. The kernel walks the process's page
    tables for it, so it costs far more than RSS: the sampler reads it once a minute per process."""
    m = re.search(rb"^Pss:\s+(\d+) kB", _read(proc / str(pid) / "smaps_rollup") or b"", re.M)
    return int(m.group(1)) * 1024 if m else None


def read_tag(proc: Path, pid: int) -> tuple[tuple[str, str] | None, str]:
    """The session a process was started for, from its environment, and its argv[0]."""
    tag = None
    env = _read(proc / str(pid) / "environ")
    if env:
        vals = dict(kv.partition(b"=")[::2] for kv in env.split(b"\0") if b"=" in kv)
        item = vals.get(ITEM_VAR.encode())
        if item:
            tag = (item.decode(errors="replace"), vals.get(CONVO_VAR.encode(), b"").decode(errors="replace"))
    cmd = _read(proc / str(pid) / "cmdline") or b""
    return tag, os.path.basename(cmd.split(b"\0", 1)[0].decode(errors="replace"))


def read_services(run_dir: Path) -> dict[int, str]:
    """The services' process sessions: the pid each service was started as -> its name."""
    out = {}
    try:
        files = list(run_dir.glob("*.pid"))
    except OSError:
        return out
    for f in files:
        with contextlib.suppress(OSError, ValueError):
            out[int(f.read_text().strip())] = f.stem
    return out


def _addr(hexaddr: str) -> str:
    ip, _, port = hexaddr.partition(":")
    raw = bytes.fromhex(ip)
    # each 32-bit word is in the host's (little-endian) byte order
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    if len(words) == 4:
        host = ".".join(str(b) for b in words)
    else:
        import ipaddress
        host = str(ipaddress.IPv6Address(words))
    return f"{host}:{int(port, 16)}"


def read_listening(proc: Path) -> list[tuple[str, int, str, int]]:
    """TCP sockets in LISTEN: (address, port, protocol, inode)."""
    out = []
    for proto in ("tcp", "tcp6"):
        raw = _read(proc / "net" / proto)
        for line in (raw or b"").decode().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            addr = _addr(parts[1])
            host, _, port = addr.rpartition(":")
            out.append((host, int(port), proto, int(parts[9])))
    return out


def socket_owners(proc: Path, pids: list[int], inodes: set[int]) -> dict[int, int]:
    """inode -> the pid holding it, for the sockets asked about."""
    owners: dict[int, int] = {}
    for pid in pids:
        if len(owners) == len(inodes):
            break
        try:
            fds = os.scandir(proc / str(pid) / "fd")
        except OSError:
            continue
        with fds:
            for fd in fds:
                try:
                    target = os.readlink(fd.path)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    inode = int(target[8:-1])
                    if inode in inodes:
                        owners.setdefault(inode, pid)
    return owners


# --- consumers -------------------------------------------------------------------------------------------------------

@dataclass
class Consumer:
    key: str
    kind: str              # session, browser, service, container, runner, other
    name: str
    item: str | None = None
    conversation: str | None = None
    cpu: float = 0.0       # percent of the whole host
    mem: int = 0
    read_bps: float = 0.0
    write_bps: float = 0.0
    rx_bps: float | None = None
    tx_bps: float | None = None
    processes: int = 0

    def row(self) -> dict:
        return {"key": self.key, "kind": self.kind, "name": self.name, "item": self.item,
                "conversation": self.conversation, "cpu_percent": round(self.cpu, 2), "mem_bytes": self.mem,
                "read_bps": round(self.read_bps), "write_bps": round(self.write_bps),
                "rx_bps": None if self.rx_bps is None else round(self.rx_bps),
                "tx_bps": None if self.tx_bps is None else round(self.tx_bps), "processes": self.processes}


def classify(procs: dict[int, Proc], services: dict[int, str], me: int) -> dict[int, tuple[str, str, str | None,
                                                                                              str | None]]:
    """pid -> (kind, name, work item, conversation)."""
    own: dict[int, tuple] = {}
    for p in procs.values():
        if p.sid in services:
            own[p.pid] = ("service", services[p.sid], None, None)
        elif p.tag:
            own[p.pid] = ("session", p.tag[0], p.tag[0], p.tag[1] or None)
        elif p.pid in (me, 1):
            own[p.pid] = ("runner", "runner", None, None)
    out = {}
    for p in procs.values():
        found, seen, cur = own.get(p.pid), set(), p
        while found is None and cur is not None and cur.ppid not in seen and cur.ppid != 0:
            seen.add(cur.ppid)
            found = own.get(cur.ppid)
            cur = procs.get(cur.ppid)
        kind, name, item, convo = found or ("other", "other", None, None)
        if kind != "service" and (BROWSER.search(p.argv0) or BROWSER.search(p.comm)):
            kind, name = "browser", item or "browser"
        out[p.pid] = (kind, name, item, convo)
    return out


def consumer_key(kind: str, name: str, item: str | None) -> str:
    if kind in ("service", "session"):
        return f"{kind}:{name}"
    return f"browser:{item or '-'}" if kind == "browser" else kind


def conversation_of(path: str | None, scratch: Path) -> str | None:
    """The conversation whose folder (scratch/<conversation>) holds `path`."""
    if not path:
        return None
    try:
        rel = Path(path).relative_to(scratch)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


# --- disk space ----------------------------------------------------------------------------------------------------

def tree_bytes(path: Path, deadline: float) -> tuple[int, bool]:
    """The bytes the files under `path` take on disk, without following links or leaving its filesystem, and
    whether the walk finished before `deadline`."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, True
    total, dev, stack = st.st_blocks * 512, st.st_dev, [str(path)]
    if not os.path.isdir(path) or os.path.islink(path):
        return total, True
    while stack:
        if time.monotonic() > deadline:
            return total, False
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for e in entries:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += s.st_blocks * 512
                if e.is_dir(follow_symlinks=False) and s.st_dev == dev:
                    stack.append(e.path)
    return total, True


def volume(path: Path) -> dict | None:
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    total, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
    return {"path": str(path), "total": total, "used": total - s.f_bfree * s.f_frsize, "free": free}


def measure_space(root: Path, tmp: Path, budget_s: float = SPACE_BUDGET_S) -> dict:
    """The volumes' use and what takes the space: each conversation's folder, the caches, the other folders."""
    deadline = time.monotonic() + budget_s
    complete = True
    top: list[dict] = []

    def size(p: Path) -> int:
        nonlocal complete
        n, done = tree_bytes(p, deadline)
        complete = complete and done
        return n

    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    convos: list[dict] = []
    for child in children:
        if child.is_symlink():
            continue
        if child.name == "scratch" and child.is_dir():
            try:
                folders = sorted(child.iterdir())
            except OSError:
                folders = []
            for c in folders:
                if c.is_dir() and not c.is_symlink():
                    convos.append({"name": c.name, "kind": "conversation", "bytes": size(c), "conversation": c.name})
        elif child.name == "home" and child.is_dir():
            whole = size(child)
            for rel, label in CACHES.items():
                if (child / rel).is_dir():
                    n = size(child / rel)
                    whole -= n
                    top.append({"name": label, "kind": "cache", "bytes": n})
            top.append({"name": "home (other)", "kind": "folder", "bytes": max(whole, 0)})
        elif child.name == "ms-playwright":
            top.append({"name": "playwright browsers", "kind": "cache", "bytes": size(child)})
        else:
            top.append({"name": child.name, "kind": "folder", "bytes": size(child)})
    convos.sort(key=lambda r: -r["bytes"])
    if len(convos) > TOP_CONVERSATIONS:
        rest = convos[TOP_CONVERSATIONS:]
        convos = convos[:TOP_CONVERSATIONS] + [{"name": f"{len(rest)} other conversations", "kind": "conversation",
                                                "bytes": sum(r["bytes"] for r in rest)}]
    if tmp.is_dir():
        top.append({"name": str(tmp), "kind": "tmp", "bytes": size(tmp)})
    volumes = [volume(root)]
    with contextlib.suppress(OSError):
        # /tmp on the same filesystem (an overlay on the same disk, say) would just repeat it
        if os.statvfs(tmp).f_fsid != os.statvfs(root).f_fsid:
            volumes.append(volume(tmp))
    return {"at": time.time(), "complete": complete, "volumes": [v for v in volumes if v], "top": top + convos}


def docker_space(df: dict) -> list[dict]:
    """The agent's Docker images, containers, volumes and build cache, from /system/df."""
    vols = sum(max((v.get("UsageData") or {}).get("Size", 0), 0) for v in df.get("Volumes") or [])
    return [{"name": "docker images", "kind": "docker", "bytes": df.get("LayersSize") or 0},
            {"name": "docker containers", "kind": "docker",
             "bytes": sum(c.get("SizeRw") or 0 for c in df.get("Containers") or [])},
            {"name": "docker volumes", "kind": "docker", "bytes": vols},
            {"name": "docker build cache", "kind": "docker",
             "bytes": sum(b.get("Size") or 0 for b in df.get("BuildCache") or [])}]


# --- containers ----------------------------------------------------------------------------------------------------

def container_numbers(stats: dict) -> dict:
    """cpu ns, memory bytes, network and block I/O byte counters from one /containers/<id>/stats."""
    cpu = ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage") or 0
    mem_stats = stats.get("memory_stats") or {}
    inner = mem_stats.get("stats") or {}
    cache = inner.get("inactive_file", inner.get("total_inactive_file", 0)) or 0
    mem = max((mem_stats.get("usage") or 0) - cache, 0)
    nets = (stats.get("networks") or {}).values()
    rx, tx = sum(n.get("rx_bytes", 0) for n in nets), sum(n.get("tx_bytes", 0) for n in nets)
    rd = wr = 0
    for e in ((stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
        op = (e.get("op") or "").lower()
        if op == "read":
            rd += e.get("value", 0)
        elif op == "write":
            wr += e.get("value", 0)
    return {"cpu": cpu, "mem": mem, "rx": rx, "tx": tx, "read": rd, "write": wr}


# --- the sampler ---------------------------------------------------------------------------------------------------

def _rate(now: int, before: int | None, dt: float) -> float:
    return max(now - before, 0) / dt if before is not None and dt > 0 else 0.0


@dataclass
class _Prev:
    at: float | None = None
    cpu: dict = field(default_factory=dict)
    net: dict = field(default_factory=dict)
    disk: tuple[int, int] | None = None
    procs: dict = field(default_factory=dict)        # (pid, start) -> (ticks, read, write)
    containers: dict = field(default_factory=dict)   # id -> container_numbers


class HostSampler:
    def __init__(self, root: Path = Path("/var/agent"), *, proc: Path = Path("/proc"),
                 sys_block: Path = Path("/sys/block"), tmp: Path = Path("/tmp"), docker_url: str = DOCKER_URL,
                 http: httpx.AsyncClient | None = None, me: int | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.root, self.proc, self.tmp = Path(root), Path(proc), Path(tmp)
        self.run_dir, self.scratch = self.root / "run", self.root / "scratch"
        self.disks = whole_disks(Path(sys_block))
        self.docker_url = docker_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=2))
        self.me = os.getpid() if me is None else me
        self.clock, self.wall = clock, wall
        self.clk = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.prev = _Prev()
        self.tags: dict[tuple[int, int], tuple] = {}  # (pid, start) -> (tag, argv0): a process's env never changes
        self.pss: dict[tuple[int, int], tuple] = {}   # (pid, start) -> (read at, PSS bytes or None)
        self.space: dict | None = None
        self.docker_df: list[dict] = []
        self._space_task: asyncio.Task | None = None
        self._space_at = -SPACE_EVERY_S

    # the /proc part, run in a thread
    def _procs(self) -> dict[int, Proc]:
        procs, now = {}, self.clock()
        for entry in os.scandir(self.proc):
            if not entry.name.isdigit():
                continue
            p = read_proc(self.proc, int(entry.name))
            if p is None:
                continue
            key = (p.pid, p.start)
            if key not in self.tags:
                self.tags[key] = read_tag(self.proc, p.pid)
            p.tag, p.argv0 = self.tags[key]
            at, pss = self.pss.get(key, (None, None))
            if at is None or now - at >= PSS_EVERY_S:
                at, pss = now, read_pss(self.proc, p.pid)
                self.pss[key] = (at, pss)
            if pss is not None:
                p.mem = pss
            procs[p.pid] = p
        alive = {(p.pid, p.start) for p in procs.values()}
        for cache in (self.tags, self.pss):
            for key in [k for k in cache if k not in alive]:
                del cache[key]
        return procs

    def _read_host(self) -> dict:
        return {"cpu": read_cpu(self.proc), "mem": read_meminfo(self.proc), "load": read_loadavg(self.proc),
                "net": read_net_dev(self.proc), "disk": read_diskstats(self.proc, self.disks),
                "procs": self._procs(), "services": read_services(self.run_dir),
                "listening": read_listening(self.proc)}

    async def _docker(self) -> list[dict]:
        try:
            listing = (await self.http.get(f"{self.docker_url}/containers/json")).json()
        except Exception:
            return []

        async def one(c: dict) -> dict | None:
            try:
                resp = await self.http.get(f"{self.docker_url}/containers/{c['Id']}/stats",
                                           params={"stream": "false", "one-shot": "true"})
                return {**c, "_n": container_numbers(resp.json())}
            except Exception:
                return None
        return [c for c in await asyncio.gather(*(one(c) for c in listing)) if c]

    def _start_space(self, now: float) -> None:
        if self._space_task is not None and not self._space_task.done():
            return
        if now - self._space_at < SPACE_EVERY_S:
            return
        self._space_at = now

        async def measure() -> None:
            try:
                self.space = await asyncio.to_thread(measure_space, self.root, self.tmp)
            except Exception:
                log.warning("measuring disk space failed", exc_info=True)
            try:
                resp = await self.http.get(f"{self.docker_url}/system/df", timeout=120)
                self.docker_df = docker_space(resp.json())
            except Exception:
                log.debug("docker system df failed", exc_info=True)
        self._space_task = asyncio.create_task(measure())

    async def sample(self) -> dict | None:
        """One snapshot, or None for the first call (rates need two readings)."""
        now = self.clock()
        self._start_space(now)
        host, containers = await asyncio.gather(asyncio.to_thread(self._read_host), self._docker())
        snap = None if self.prev.at is None else self._build(host, containers, now - self.prev.at)
        self.prev = _Prev(at=now, cpu=host["cpu"], net=host["net"], disk=host["disk"],
                          procs={(p.pid, p.start): (p.ticks, *p.io) for p in host["procs"].values()},
                          containers={c["Id"]: c["_n"] for c in containers})
        return snap

    def _build(self, host: dict, containers: list[dict], dt: float) -> dict:
        prev = self.prev
        cores = sorted((k for k in host["cpu"] if k != "cpu"), key=lambda k: int(k[3:]))
        ncpu = max(len(cores), 1)

        def pct(name: str) -> float:
            busy, total = host["cpu"][name]
            b0, t0 = prev.cpu.get(name, (busy, total))
            return round(100 * (busy - b0) / (total - t0), 1) if total > t0 else 0.0

        mem = host["mem"]
        total_mem, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        ifaces = []
        for name, (rx, tx) in sorted(host["net"].items()):
            p = prev.net.get(name)
            ifaces.append({"name": name, "rx_bps": round(_rate(rx, p and p[0], dt)),
                           "tx_bps": round(_rate(tx, p and p[1], dt))})
        rd, wr = host["disk"]
        p_rd, p_wr = prev.disk or (None, None)

        # processes -> consumers
        kinds = classify(host["procs"], host["services"], self.me)
        consumers: dict[str, Consumer] = {}
        for p in host["procs"].values():
            kind, name, item, convo = kinds[p.pid]
            key = consumer_key(kind, name, item)
            c = consumers.get(key) or consumers.setdefault(key, Consumer(key, kind, name, item, convo))
            before = prev.procs.get((p.pid, p.start))
            c.processes += 1
            c.mem += p.mem
            if before is not None:
                c.cpu += 100 * max(p.ticks - before[0], 0) / self.clk / dt / ncpu
                c.read_bps += _rate(p.io[0], before[1], dt)
                c.write_bps += _rate(p.io[1], before[2], dt)

        # containers
        rows, published = [], {}
        for ct in containers:
            n, b = ct["_n"], prev.containers.get(ct["Id"])
            labels = ct.get("Labels") or {}
            name = ((ct.get("Names") or ["/?"])[0]).lstrip("/")
            convo = conversation_of(labels.get("com.docker.compose.project.working_dir"), self.scratch)
            c = Consumer(f"container:{name}", "container", name, None, convo, processes=0, mem=n["mem"])
            if b is not None:
                c.cpu = 100 * max(n["cpu"] - b["cpu"], 0) / 1e9 / dt / ncpu
                c.read_bps, c.write_bps = _rate(n["read"], b["read"], dt), _rate(n["write"], b["write"], dt)
            c.rx_bps, c.tx_bps = (_rate(n["rx"], b and b["rx"], dt), _rate(n["tx"], b and b["tx"], dt))
            consumers[c.key] = c
            for port in ct.get("Ports") or []:
                if port.get("PublicPort"):
                    published[int(port["PublicPort"])] = c.key
            rows.append({"name": name, "image": ct.get("Image"), "state": ct.get("State"),
                         "status": ct.get("Status"), "conversation": convo, **{k: v for k, v in c.row().items()
                         if k in ("cpu_percent", "mem_bytes", "rx_bps", "tx_bps", "read_bps", "write_bps")}})

        # ports
        listening = host["listening"]
        owners = socket_owners(self.proc, list(host["procs"]), {inode for *_, inode in listening})
        ports, seen = [], set()
        for addr, port, proto, inode in listening:
            pid = owners.get(inode)
            if (port, pid) in seen:
                continue
            seen.add((port, pid))
            p = host["procs"].get(pid) if pid else None
            if p is not None:
                kind, name, item, convo = kinds[p.pid]
                key = consumer_key(kind, name, item)
                process = p.argv0 or p.comm
            else:  # a container's published port, or a socket held outside the agent container (its Docker daemon)
                key, process = published.get(port, "host"), ("docker-proxy" if port in published else None)
            c = consumers.get(key)
            ports.append({"port": port, "address": addr, "proto": proto, "pid": pid, "process": process,
                          "consumer": key, "kind": c.kind if c else "host",
                          "name": c.name if c else "outside the agent container",
                          "item": c.item if c else None, "conversation": c.conversation if c else None})
        ports.sort(key=lambda r: r["port"])

        ordered = sorted(consumers.values(), key=lambda c: (-c.cpu, -c.mem))[:MAX_ROWS]
        space = dict(self.space) if self.space else None
        if space is not None:
            space["top"] = sorted(space["top"] + self.docker_df, key=lambda r: -r["bytes"])
        return {
            "ts": self.wall(), "interval_s": round(dt, 2),
            "cpu": {"percent": pct("cpu"), "per_core": [pct(k) for k in cores], "cores": ncpu,
                    "load": host["load"]},
            "memory": {"total": total_mem, "available": avail, "used": total_mem - avail,
                       "swap_total": mem.get("SwapTotal", 0),
                       "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)},
            "network": {"rx_bps": sum(i["rx_bps"] for i in ifaces if not VIRTUAL_NET.match(i["name"])),
                        "tx_bps": sum(i["tx_bps"] for i in ifaces if not VIRTUAL_NET.match(i["name"])),
                        "interfaces": sorted(ifaces, key=lambda i: -(i["rx_bps"] + i["tx_bps"]))[:MAX_INTERFACES]},
            "disk_io": {"read_bps": round(_rate(rd, p_rd, dt)), "write_bps": round(_rate(wr, p_wr, dt))},
            "disk_space": space,
            "containers": rows[:MAX_ROWS],
            "ports": ports[:MAX_ROWS],
            "consumers": [c.row() for c in ordered],
        }


async def report_forever(sampler: HostSampler, post: Callable[[dict], Awaitable[None]],
                         every_s: float = SAMPLE_S) -> None:
    """Sample and post every `every_s` until cancelled. A failed sample or post is logged once in a row."""
    failing = False
    while True:
        started = time.monotonic()
        try:
            snap = await sampler.sample()
            if snap is not None:
                await post(snap)
            failing = False
        except asyncio.CancelledError:
            raise
        except Exception:
            if not failing:
                log.warning("the host sample failed", exc_info=True)
            failing = True
        await asyncio.sleep(max(every_s - (time.monotonic() - started), every_s / 10))


def core_poster(core: str, read_token: Callable[[], str],
                http: httpx.AsyncClient | None = None) -> Callable[[dict], Awaitable[None]]:
    """Posts a snapshot to core's /agent/host with the current runner token (none yet: nothing to post)."""
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))

    async def post(snap: dict) -> None:
        token = read_token()
        if not token:
            return
        resp = await client.post(f"{core.rstrip('/')}/agent/host", json=snap,
                                 headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    return post
