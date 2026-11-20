"""The dashboard's host monitor: the runner's samples of what the machine is doing (agent.runner.host), kept here
for an hour so the page can draw recent history without a store of its own. They live in core, so a runner restart
loses nothing but the samples it didn't take; a core restart starts the hour again.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

KEEP_S = 3600
STALE_S = 30         # samples come every 5 s; older than this and the page says the runner isn't reporting
KINDS = ("session", "browser", "service", "container", "runner", "other", "host")  # host: outside the agent container


def _num(x, default: float = 0.0) -> float:
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _get(d, *path, default=None):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    return default if d is None else d


class HostStore:
    def __init__(self, keep_s: float = KEEP_S, clock: Callable[[], float] = time.time):
        self.keep_s, self.clock = keep_s, clock
        self.points: deque[dict] = deque()
        self.latest: dict | None = None
        self.received_at: float | None = None

    def add(self, snap: dict) -> None:
        now = self.clock()
        self.latest, self.received_at = snap, now
        by_kind: dict[str, dict[str, float]] = {k: {"cpu": 0.0, "mem": 0.0} for k in KINDS}
        for c in snap.get("consumers") or []:
            if isinstance(c, dict) and c.get("kind") in by_kind:
                by_kind[c["kind"]]["cpu"] += _num(c.get("cpu_percent"))
                by_kind[c["kind"]]["mem"] += _num(c.get("mem_bytes"))
        # what the host total holds beyond what the agent container sees: core, its Docker daemon, everything else
        by_kind["host"]["cpu"] = max(_num(_get(snap, "cpu", "percent")) - sum(v["cpu"] for v in by_kind.values()), 0)
        by_kind["host"]["mem"] = max(_num(_get(snap, "memory", "used")) - sum(v["mem"] for v in by_kind.values()), 0)
        self.points.append({
            "ts": now,
            "cpu": _num(_get(snap, "cpu", "percent")),
            "mem_used": _num(_get(snap, "memory", "used")),
            "swap_used": _num(_get(snap, "memory", "swap_used")),
            "rx_bps": _num(_get(snap, "network", "rx_bps")), "tx_bps": _num(_get(snap, "network", "tx_bps")),
            "read_bps": _num(_get(snap, "disk_io", "read_bps")),
            "write_bps": _num(_get(snap, "disk_io", "write_bps")),
            "containers": len(snap.get("containers") or []), "ports": len(snap.get("ports") or []),
            "cpu_by_kind": {k: round(v["cpu"], 2) for k, v in by_kind.items()},
            "mem_by_kind": {k: v["mem"] for k, v in by_kind.items()},
        })
        while self.points and self.points[0]["ts"] < now - self.keep_s:
            self.points.popleft()

    def status(self) -> dict:
        age = None if self.received_at is None else max(self.clock() - self.received_at, 0)
        return {"available": self.latest is not None, "age_s": None if age is None else round(age),
                "stale": age is None or age > STALE_S}


def host_view(store: HostStore) -> dict:
    """The host's totals now and over the last hour."""
    snap = store.latest or {}
    space = snap.get("disk_space") or {}
    return {**store.status(), "ts": snap.get("ts"),
            "cpu": snap.get("cpu"), "memory": snap.get("memory"), "network": snap.get("network"),
            "disk_io": snap.get("disk_io"),
            "disk_space": {"volumes": space.get("volumes") or [], "at": space.get("at"),
                           "complete": space.get("complete", True)} if space else None,
            "containers": len(snap.get("containers") or []), "ports": len(snap.get("ports") or []),
            "history": list(store.points)}


def consumers_view(store: HostStore, session_of: Callable[[str | None, str | None], dict | None]) -> dict:
    """Who uses what, from the newest sample: the consumers (with a row for what the host spends outside the agent
    container), the containers, the listening ports and what takes the disk. `session_of(item, conversation)` is the
    dashboard session to link a row to: {id, title, state}, or None."""
    snap = store.latest or {}
    cache: dict[tuple, dict | None] = {}

    def link(row: dict) -> dict:
        key = (row.get("item"), row.get("conversation"))
        if key == (None, None):
            return {**row, "session": None}
        if key not in cache:
            cache[key] = session_of(*key)
        return {**row, "session": cache[key]}

    consumers = [link(c) for c in snap.get("consumers") or [] if isinstance(c, dict)]
    # what the agent container can't see (core, the supervisors, the agent's Docker daemon, everything else on the
    # machine) is whatever the host total holds beyond the consumers it can
    seen_cpu = sum(_num(c.get("cpu_percent")) for c in consumers)
    seen_mem = sum(_num(c.get("mem_bytes")) for c in consumers)
    outside_cpu = max(_num(_get(snap, "cpu", "percent")) - seen_cpu, 0)
    outside_mem = max(_num(_get(snap, "memory", "used")) - seen_mem, 0)
    if snap:
        consumers.append({"key": "host", "kind": "host", "name": "outside the agent container", "item": None,
                          "conversation": None, "cpu_percent": round(outside_cpu, 2), "mem_bytes": outside_mem,
                          "read_bps": None, "write_bps": None, "rx_bps": None, "tx_bps": None, "processes": None,
                          "session": None})
    space = snap.get("disk_space") or {}
    return {**store.status(), "ts": snap.get("ts"), "consumers": consumers,
            "containers": [link(c) for c in snap.get("containers") or [] if isinstance(c, dict)],
            "ports": [link(p) for p in snap.get("ports") or [] if isinstance(p, dict)],
            "disk": [link(r) for r in space.get("top") or [] if isinstance(r, dict)],
            "interfaces": _get(snap, "network", "interfaces", default=[])}
