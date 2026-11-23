#!/usr/bin/env python3
"""Spike: print `port pid cmd` for each TCP listener owned by a process visible in this container.

The agent image has no `ss`, so this reads /proc/net/tcp* for listening sockets (state 0A) and matches their
inodes against the socket file descriptors of the processes this container can see.
"""
import glob
import os


def listening() -> dict[str, int]:
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(path).read().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            f = row.split()
            if f[3] == "0A":
                out[f[9]] = int(f[1].rsplit(":", 1)[1], 16)
    return out


def owners(inodes: dict[str, int]) -> list[tuple[int, int, str]]:
    found = set()
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        inode = target[8:-1] if target.startswith("socket:[") else None
        if inode in inodes:
            pid = fd.split("/")[2]
            try:
                cmd = open(f"/proc/{pid}/cmdline").read().replace("\0", " ").strip()
            except OSError:
                cmd = "?"
            found.add((inodes[inode], int(pid), cmd))
    return sorted(found)


if __name__ == "__main__":
    for port, pid, cmd in owners(listening()):
        print(port, pid, cmd)
