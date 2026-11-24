# What runs in this container, for the dashboard. agentd runs it with `python3 -c` through `docker exec` as the
# agent user, in a conversation's container or in agent-services; it only reads /proc and the cgroup files, except
# `kill` and `trim`, which signal processes of this container (never PID 1 or the holder).
#
#   ps SECONDS          every process, CPU measured over SECONDS, and the container's cgroup numbers, as JSON
#   kill PID SIGNAL     PID and everything it started
#   killport PORT SIG   the process listening on PORT and everything it started
#   trim PORT...        every process except the ones listening on PORT... with their parents and children
import json, os, signal, sys, time

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path, default=""):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return default


def pids():
    return sorted(int(p) for p in os.listdir("/proc") if p.isdigit())


def stat(pid):
    text = read(f"/proc/{pid}/stat")
    close = text.rfind(")")
    if close < 0:
        return None
    f = text[close + 2:].split()
    return {"comm": text[text.find("(") + 1:close], "state": f[0], "ppid": int(f[1]), "pgid": int(f[2]),
            "ticks": int(f[11]) + int(f[12]), "threads": int(f[17]), "start": int(f[19])}


def listening():
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in read(path).splitlines()[1:]:
            f = row.split()
            if len(f) > 9 and f[3] == "0A":
                out[f[9]] = int(f[1].rsplit(":", 1)[1], 16)
    return out


def ports_of(pid, sockets):
    ports = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return []
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in sockets:
            ports.add(sockets[target[8:-1]])
    return sorted(ports)


def output(pid, fd):
    """The file a process writes fd to, when it is a regular file (a log), else None."""
    try:
        target = os.readlink(f"/proc/{pid}/fd/{fd}")
        st = os.stat(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return None
    import stat as s
    return target if s.S_ISREG(st.st_mode) else None


def memory(pid):
    rss = pss = swap = 0
    for line in read(f"/proc/{pid}/smaps_rollup").splitlines():
        k, _, v = line.partition(":")
        if k in ("Rss", "Pss", "Swap"):
            n = int(v.split()[0]) * 1024
            rss, pss, swap = (n, pss, swap) if k == "Rss" else (rss, n, swap) if k == "Pss" else (rss, pss, n)
    if not rss:
        f = read(f"/proc/{pid}/statm").split()
        rss = int(f[1]) * PAGE if len(f) > 1 else 0
    return rss, pss or rss, swap


def io(pid):
    out = {}
    for line in read(f"/proc/{pid}/io").splitlines():
        k, _, v = line.partition(":")
        if k in ("read_bytes", "write_bytes"):
            out[k] = int(v)
    return out


def cgroup():
    def num(name):
        v = read(f"/sys/fs/cgroup/{name}").strip()
        return None if v in ("", "max") else int(v.split()[0])
    mem = dict((k, int(v)) for k, v in (l.split() for l in read("/sys/fs/cgroup/memory.stat").splitlines() if l.count(" ") == 1))
    events = dict((k, int(v)) for k, v in (l.split() for l in read("/sys/fs/cgroup/memory.events").splitlines() if l.count(" ") == 1))
    cpu = dict((k, int(v)) for k, v in (l.split() for l in read("/sys/fs/cgroup/cpu.stat").splitlines() if l.count(" ") == 1))
    quota = read("/sys/fs/cgroup/cpu.max").split()
    return {"memory_current": num("memory.current"), "memory_max": num("memory.max"), "memory_peak": num("memory.peak"),
            "anon": mem.get("anon"), "file": mem.get("file"), "kernel": mem.get("kernel"), "shmem": mem.get("shmem"),
            "oom_kills": events.get("oom_kill"), "oom_events": events.get("oom"), "high_events": events.get("high"),
            "cpu_usage_us": cpu.get("usage_usec"), "throttled_us": cpu.get("throttled_usec"),
            "cpus": (int(quota[0]) / int(quota[1])) if len(quota) == 2 and quota[0] != "max" else None,
            "pids_current": num("pids.current"), "pids_max": num("pids.max")}


def ps(seconds):
    before = {p: (stat(p) or {}).get("ticks", 0) for p in pids()}
    t0 = time.monotonic()
    time.sleep(seconds)
    wall = max(time.monotonic() - t0, 0.001)
    sockets = listening()
    boot = float(read("/proc/stat").split("btime")[1].split()[0]) if "btime" in read("/proc/stat") else 0
    own, out = set(), []
    for pid in pids():
        st = stat(pid)
        if not st or pid == os.getpid():
            continue
        argv = read(f"/proc/{pid}/cmdline").split("\0")
        cmd = " ".join(a for a in argv if a)[:2000]
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = None
        rss, pss, swap = memory(pid)
        ports = ports_of(pid, sockets)
        own.update(ports)
        log = output(pid, 1)
        err = output(pid, 2)
        out.append({"pid": pid, "ppid": st["ppid"], "pgid": st["pgid"], "comm": st["comm"], "state": st["state"],
                    "cmd": cmd or f"[{st['comm']}]", "cwd": cwd, "threads": st["threads"],
                    "started": boot + st["start"] / CLK if boot else None,
                    "cpu_percent": round(max(0, st["ticks"] - before.get(pid, st["ticks"])) / CLK / wall * 100, 1),
                    "cpu_seconds": round(st["ticks"] / CLK, 1), "rss": rss, "pss": pss, "swap": swap,
                    "ports": ports, "log": log, "err": err if err != log else None, **io(pid),
                    "uid": os.stat(f"/proc/{pid}").st_uid if os.path.exists(f"/proc/{pid}") else None})
    shared = sorted(set(sockets.values()) - own)
    return {"now": time.time(), "processes": out, "cgroup": cgroup(), "other_listeners": shared}


def tree(root, procs):
    kids, out, todo = {}, set(), [root]
    for p in procs:
        kids.setdefault(p["ppid"], []).append(p["pid"])
    while todo:
        pid = todo.pop()
        if pid not in out:
            out.add(pid)
            todo.extend(kids.get(pid, []))
    return out


def protected(procs):
    return {1, os.getpid()} | {p["pid"] for p in procs if "agent-holder serve" in p["cmd"] or "-m agent services" in p["cmd"]}


def signal_all(targets, sig, keep):
    done = []
    for pid in sorted(targets - keep, reverse=True):
        try:
            os.kill(pid, sig)
            done.append(pid)
        except OSError:
            pass
    return done


def owner(port, procs):
    return next((p["pid"] for p in procs if port in p["ports"]), None)


def main(args):
    cmd = args[0]
    if cmd == "ps":
        print(json.dumps(ps(float(args[1]))))
        return
    procs = ps(0)["processes"]
    keep = protected(procs)
    if cmd in ("kill", "killport"):
        pid = int(args[1]) if cmd == "kill" else owner(int(args[1]), procs)
        if pid is None or pid in keep:
            print(json.dumps({"error": "nothing to stop" if pid is None else "that process runs the container"}))
            return
        print(json.dumps({"signalled": signal_all(tree(pid, procs), getattr(signal, "SIG" + args[2]), keep)}))
    elif cmd == "trim":
        by = {p["pid"]: p for p in procs}
        for port in map(int, args[1:]):
            pid = owner(port, procs)
            if pid is None:
                continue
            keep |= tree(pid, procs)
            parent = by[pid]["ppid"]
            while parent in by and parent > 1:  # what started it: a shell, npm
                keep.add(parent)
                parent = by[parent]["ppid"]
        print(json.dumps({"signalled": signal_all({p["pid"] for p in procs}, signal.SIGTERM, keep)}))


main(sys.argv[1:])
