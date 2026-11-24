"""The agent's own long-running services (a bridge, a poller, a watcher), kept going in the services container.

The agent lists them in /var/agent/services.toml:

    [[services]]
    name = "outlook-bridge"
    cmd = ["python3", "-I", "bridge.py"]
    cwd = "/var/agent/bridges/outlook-bridge"

Every 30 s the supervisor reads the file again and reconciles: it starts the services that are new, stops the ones
that are gone (SIGTERM to the service's process group, SIGKILL after 10 s) and restarts a service whose command or
folder changed. A service that exits is started again after a backoff: 5 s, doubling to 5 minutes, and back to 5 s
once a run has lasted 10 minutes. A missing file means no services. A file that can't be read or parsed keeps the
services it listed last (none right after the supervisor starts), and an invalid entry is left out; each problem is
logged once, and nothing here ever stops the supervisor.

While the supervisor runs a service it holds an exclusive lock on run/<name>.lock, so a second supervisor can't start
another copy, and keeps the service's pid in run/<name>.pid. Before a service's first start in this supervisor's life,
any process with exactly the same argv in the same folder is stopped: that takes over a copy the agent started by
hand; a process the supervisor started itself is never taken for one. A service runs in its own process group with
the container's environment, and its stdout and stderr go to run/<name>.out (rotated to .out.1 at 5 MB) along with the
supervisor's own start and exit lines. The container's log gets names, pids and exit codes only, never a service's
arguments or environment.

Every file here is opened without blocking and must be a regular file: a FIFO or a device in its place is refused,
since a blocking open on the event loop would freeze every service.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import re
import signal
import stat
import time
import tomllib
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SERVICES_FILE = Path("/var/agent/services.toml")
RUN_DIR = Path("/var/agent/run")
OUT_CAP = 5 * 1024 * 1024
MAX_FILE = 1024 * 1024  # the most of services.toml that's read
_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Service:
    name: str
    cmd: tuple[str, ...]
    cwd: str


def open_regular(path: Path, flags: int, mode: int = 0o644) -> int:
    """os.open that never blocks (on a FIFO, say) and refuses anything but a regular file."""
    try:
        fd = os.open(path, flags | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC, mode)
    except OSError as e:
        if e.errno == errno.ENXIO:  # a FIFO nobody reads, opened to write
            raise OSError(errno.EINVAL, "not a regular file", str(path)) from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_services(path: Path) -> tuple[list[Service] | None, list[str]]:
    """The services the file lists, and what's wrong with it. A missing file lists none. A file that can't be read
    or parsed gives None (keep the last good list) and says why; an invalid entry is left out and named. Never
    raises."""
    try:
        with os.fdopen(open_regular(path, os.O_RDONLY), "rb") as f:
            raw = f.read(MAX_FILE + 1)
    except FileNotFoundError:
        return [], []
    except OSError as e:
        return None, [f"can't read {path}: {e.strerror or type(e).__name__}"]
    if len(raw) > MAX_FILE:
        return None, [f"{path} is over {MAX_FILE // 1024} KB"]
    try:
        data = tomllib.loads(raw.decode())
    except UnicodeDecodeError:
        return None, [f"{path} isn't UTF-8 text"]
    except tomllib.TOMLDecodeError as e:
        return None, [f"{path} isn't valid TOML: {e}"]
    entries = data.get("services", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return None, [f"{path}: `services` must be [[services]] tables"]
    services: list[Service] = []
    problems: list[str] = []
    for i, entry in enumerate(entries, 1):
        name, cmd, cwd = entry.get("name"), entry.get("cmd"), entry.get("cwd")
        if not (isinstance(name, str) and _NAME.fullmatch(name)):
            problems.append(f"service {i}: `name` must match [A-Za-z0-9._-]+; left out")
        elif any(s.name == name for s in services):
            problems.append(f"{name}: listed more than once; only the first one runs")
        elif not (isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)):
            problems.append(f"{name}: `cmd` must be a non-empty list of strings; left out")
        elif not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
            problems.append(f"{name}: `cwd` must be an existing absolute directory; left out")
        else:
            services.append(Service(name, tuple(cmd), cwd))
    return services, problems


class Backoff:
    """The wait before each restart: `first`, doubling up to `cap`, and back to `first` after a run of at least
    `reset_after` seconds."""

    def __init__(self, first: float = 5.0, cap: float = 300.0, reset_after: float = 600.0):
        self.first, self.cap, self.reset_after = first, cap, reset_after
        self._next = first

    def after(self, uptime: float) -> float:
        if uptime >= self.reset_after:
            self._next = self.first
        delay, self._next = self._next, min(self._next * 2, self.cap)
        return delay


def take_lock(path: Path) -> int | None:
    """An exclusive flock on `path`, held until the returned fd is closed; None when someone else holds it."""
    fd = open_regular(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def find_strays(proc_root: Path, cmd: tuple[str, ...] | list[str], cwd: str,
                ours: Collection[int] = ()) -> list[int]:
    """The pids of the processes running exactly `cmd`, the whole argv and never a part of it, in the folder `cwd`:
    never this process, and never one in a process group in `ours` (the services the supervisor started)."""
    want = [os.fsencode(c) for c in cmd]
    folder = os.path.realpath(cwd)
    me = os.getpid()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if argv[-1] == b"":
                argv.pop()  # the terminating NUL
            if argv != want or os.readlink(entry / "cwd") != folder:
                continue
        except OSError:
            continue
        if int(entry.name) in ours or _process_group(proc_root, int(entry.name)) in ours:
            continue
        found.append(int(entry.name))
    return sorted(found)


def _stat_fields(proc_root: Path, pid: int) -> list[str]:
    """/proc/<pid>/stat after the command name: state, ppid, pgrp, ... ([] when it can't be read)."""
    try:
        return (proc_root / str(pid) / "stat").read_text().rpartition(")")[2].split()
    except OSError:
        return []


def _process_group(proc_root: Path, pid: int) -> int | None:
    fields = _stat_fields(proc_root, pid)
    return int(fields[2]) if len(fields) > 2 and fields[2].lstrip("-").isdigit() else None


def _alive(proc_root: Path, pid: int) -> bool:
    """Whether `pid` is still running: there, and not a zombie."""
    return _stat_fields(proc_root, pid)[:1] not in ([], ["Z"], ["X"])


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pgid, sig)


def _ended(code: int) -> str:
    if code < 0:
        with contextlib.suppress(ValueError):
            return f"was killed by {signal.Signals(-code).name}"
    return f"exited with code {code}"


class Output:
    """A service's output file: appended to, and rotated to <name>.out.1 when a write would take it past `cap`."""

    def __init__(self, path: Path, cap: int = OUT_CAP):
        self.path, self.cap = path, cap
        self._file = None
        self._line_start = True
        self._failed = False

    def write(self, data: bytes) -> None:
        if not data:
            return
        try:
            if self._file is None:
                self._file = self._open()
            size = os.fstat(self._file.fileno()).st_size
            if size and size + len(data) > self.cap:
                self._file.close()
                self._file = None
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                self._file = self._open()
            self._file.write(data)
            self._file.flush()
            self._line_start = data.endswith(b"\n")
            self._failed = False
        except OSError as e:
            if not self._failed:  # keep draining the service's output either way, so it never blocks on a write
                log.warning("can't write %s: %s", self.path, e.strerror or type(e).__name__)
                self._failed = True

    def _open(self):
        return os.fdopen(open_regular(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT), "ab")

    def note(self, text: str) -> None:
        """One line from the supervisor itself, so the file shows when the service started, exited and why."""
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write(("" if self._line_start else "\n").encode() + f"--- {stamp} supervisor: {text}\n".encode())

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


class _Pipe(asyncio.Protocol):
    """Copies what the service writes to its Output."""

    def __init__(self, out: Output):
        self.out = out
        self.closed = asyncio.get_running_loop().create_future()

    def data_received(self, data: bytes) -> None:
        self.out.write(data)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


@dataclass
class _Keeper:
    service: Service
    out: Output
    task: asyncio.Task | None = None
    reason: str = ""  # why it was stopped, for the output file


class ServiceSupervisor:
    """Keeps the services in the services file running. `sleep` is the wait between restarts and `clock` measures
    how long a run lasted; tests pass fakes. `env` None gives each service the container's own environment."""

    def __init__(self, config: Path = SERVICES_FILE, run_dir: Path = RUN_DIR, *, proc_root: Path = Path("/proc"),
                 env: dict[str, str] | None = None, check_s: float = 30.0, grace_s: float = 10.0,
                 backoff: tuple[float, float, float] = (5.0, 300.0, 600.0), out_cap: int = OUT_CAP,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep):
        self.config, self.run_dir, self.proc_root, self.env = Path(config), Path(run_dir), Path(proc_root), env
        self.check_s, self.grace_s, self.backoff, self.out_cap = check_s, grace_s, backoff, out_cap
        self._clock, self._sleep = clock, sleep
        self._keepers: dict[str, _Keeper] = {}
        self._locks: dict[str, int] = {}
        self._adopted: set[Service] = set()   # services whose hand-started copies have been looked for already
        self._held_elsewhere: set[str] = set()
        self._file_state: tuple | None = None
        self._last_good: list[Service] = []   # what the file listed when it last parsed
        self._file_error = ""                 # why it doesn't parse now, as noted in each service's output
        self._errors: dict[str, str] = {}     # the last error per service (or "" for the whole pass), logged once
        self._ours: set[int] = set()          # the pids, and so process groups, of the services running now
        self._spawning = asyncio.Lock()       # a scan for strays never sees a service mid-start

    def running(self) -> dict[str, Service]:
        return {name: k.service for name, k in self._keepers.items()}

    async def run_forever(self) -> None:
        try:
            while True:
                try:
                    await self.reconcile()
                    self._errors.pop("", None)
                except Exception as e:
                    self._log_error("", "checking the services failed", e)
                await asyncio.sleep(self.check_s)
        finally:
            try:
                await self.stop_all()
            except Exception:
                log.exception("stopping the services failed")

    async def reconcile(self) -> None:
        services, problems = read_services(self.config)
        if services is None:  # can't be read or parsed: keep what it listed last
            services, error = self._last_good, "; ".join(problems)
            if error != self._file_error:
                self._file_error = error
                for keeper in self._keepers.values():
                    keeper.out.note(f"{self.config} can't be used ({error}); keeping the services it listed last")
        else:
            self._last_good, self._file_error = services, ""
        self._note_file(services, problems)
        wanted = {s.name: s for s in services}
        stale = [name for name, k in self._keepers.items() if wanted.get(name) != k.service]
        why = f"it's no longer in {self.config}" + (f" ({'; '.join(problems)})" if problems else "")
        await asyncio.gather(*(self._stop(name, why if name not in wanted else "its cmd or cwd changed",
                                          release=name not in wanted) for name in stale))
        for name, service in wanted.items():
            try:
                self._ensure(service)
            except Exception as e:
                self._log_error(name, f"couldn't start service {name}", e)
            else:
                self._errors.pop(name, None)
        self._held_elsewhere &= wanted.keys()

    def _ensure(self, service: Service) -> None:
        name = service.name
        keeper = self._keepers.get(name)
        if keeper is not None:
            if not keeper.task.done():
                return
            # a keeper only ends on a bug of its own: say so and start it again
            self._keepers.pop(name)
            if not keeper.task.cancelled() and keeper.task.exception() is not None:
                log.error("keeping service %s failed; starting it again", name, exc_info=keeper.task.exception())
        if name not in self._locks:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            fd = take_lock(self.run_dir / f"{name}.lock")
            if fd is None:
                if name not in self._held_elsewhere:
                    log.info("service %s already runs under another supervisor (%s is locked); not starting it",
                             name, self.run_dir / f"{name}.lock")
                    self._held_elsewhere.add(name)
                return
            self._locks[name] = fd
        self._held_elsewhere.discard(name)
        keeper = _Keeper(service, Output(self.run_dir / f"{name}.out", self.out_cap))
        keeper.task = asyncio.create_task(self._keep(keeper), name=f"service {name}")
        self._keepers[name] = keeper

    async def stop_all(self) -> None:
        await asyncio.gather(*(self._stop(name, "the supervisor is stopping") for name in list(self._keepers)))
        for name in list(self._locks):
            self._release(name)

    async def _stop(self, name: str, reason: str, release: bool = True) -> None:
        keeper = self._keepers.pop(name, None)
        if keeper is not None:
            log.info("stopping service %s", name)
            keeper.reason = reason
            if not keeper.task.cancelling():
                keeper.task.cancel()
            await asyncio.wait([keeper.task])
        if release:
            self._release(name)

    def _release(self, name: str) -> None:
        fd = self._locks.pop(name, None)
        if fd is not None:
            os.close(fd)  # closing the fd releases the flock

    def _note_file(self, services: list[Service], problems: list[str]) -> None:
        state = (tuple(s.name for s in services), tuple(problems))
        if state == self._file_state:
            return
        self._file_state = state
        for problem in problems:
            log.warning("services file: %s", problem)
        log.info("services file %s lists %s", self.config, ", ".join(state[0]) or "no services")

    def _log_error(self, key: str, what: str, e: BaseException) -> None:
        text = f"{type(e).__name__}: {e}"
        if self._errors.get(key) != text:
            self._errors[key] = text
            log.error("%s: %s", what, text)

    async def _keep(self, keeper: _Keeper) -> None:
        service, out = keeper.service, keeper.out
        backoff = Backoff(*self.backoff)
        try:
            if service not in self._adopted:
                self._adopted.add(service)
                async with self._spawning:
                    strays = find_strays(self.proc_root, service.cmd, service.cwd, self._ours)
                for pid in strays:
                    await self._stop_stray(service.name, pid, out)
            runs = 0
            while True:
                uptime, ended = await self._run_once(service, out, restart=runs > 0)
                runs += 1
                delay = backoff.after(uptime)
                log.info("service %s %s; restarting it in %.0f s", service.name, ended, delay)
                out.note(f"{ended}; restarting in {delay:.0f} s")
                await self._sleep(delay)
        except asyncio.CancelledError:
            out.note(f"stopped: {keeper.reason or 'the supervisor is stopping'}")
            raise
        finally:
            out.close()

    async def _run_once(self, service: Service, out: Output, restart: bool) -> tuple[float, str]:
        """Run the service once, until it exits: how long it ran and how it ended."""
        started = self._clock()
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            async with self._spawning:
                proc = await asyncio.create_subprocess_exec(
                    *service.cmd, cwd=service.cwd, env=self.env, stdin=asyncio.subprocess.DEVNULL, stdout=write_fd,
                    stderr=write_fd, start_new_session=True)
                self._ours.add(proc.pid)
        except (OSError, ValueError) as e:
            reader.close()
            why = getattr(e, "strerror", None) or type(e).__name__
            log.warning("service %s couldn't start: %s", service.name, why)
            return 0.0, f"couldn't start ({why})"
        except BaseException:
            reader.close()
            raise
        finally:
            os.close(write_fd)
        try:
            transport, pipe = await asyncio.get_running_loop().connect_read_pipe(lambda: _Pipe(out), reader)
        except BaseException:
            reader.close()  # the file object, never the fd number: the transport may close it too
            _signal_group(proc.pid, signal.SIGKILL)
            self._ours.discard(proc.pid)
            raise
        pid_file = self.run_dir / f"{service.name}.pid"
        wrote_pid = False
        try:
            try:
                fd = open_regular(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                with os.fdopen(fd, "w") as f:
                    f.write(f"{proc.pid}\n")
                wrote_pid = True
            except OSError as e:
                log.warning("can't write %s: %s", pid_file, e.strerror or type(e).__name__)
            log.info("service %s %s (pid %d)", service.name, "restarted" if restart else "started", proc.pid)
            out.note(f"started (pid {proc.pid})")
            try:
                code = await proc.wait()
            except asyncio.CancelledError:
                await self._terminate(proc)
                raise
        finally:
            _signal_group(proc.pid, signal.SIGKILL)  # anything the service left behind in its group
            self._ours.discard(proc.pid)
            if wrote_pid:
                with contextlib.suppress(OSError):
                    pid_file.unlink(missing_ok=True)
            try:
                await asyncio.wait([pipe.closed], timeout=2)  # the rest of its output
            finally:
                transport.close()
        uptime = self._clock() - started
        return uptime, f"{_ended(code)} after {uptime:.0f} s"

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.grace_s)
        except TimeoutError:
            log.warning("service pid %d didn't stop within %.0f s of SIGTERM; killing it", proc.pid, self.grace_s)
            _signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def _stop_stray(self, name: str, pid: int, out: Output) -> None:
        """Stop a copy of the service that runs outside the supervisor: SIGTERM, then SIGKILL after the grace."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            log.warning("service %s: a copy started outside the supervisor (pid %d) isn't ours to stop", name, pid)
            return
        if not await self._gone(pid, self.grace_s):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            await self._gone(pid, 2.0)
        log.info("service %s: stopped a copy started outside the supervisor (pid %d)", name, pid)
        out.note(f"stopped a copy started outside the supervisor (pid {pid})")

    async def _gone(self, pid: int, within: float) -> bool:
        deadline = time.monotonic() + within
        while _alive(self.proc_root, pid):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True


async def supervise(config: Path = SERVICES_FILE, run_dir: Path = RUN_DIR, *, retry_s: float = 30.0,
                    **options) -> None:
    """Keep the agent's services going for as long as the container runs. Nothing in here ends the container: a failure is
    logged and the supervisor starts again."""
    while True:
        try:
            await ServiceSupervisor(config, run_dir, **options).run_forever()
        except Exception:
            log.exception("the services supervisor failed; starting it again in %.0f s", retry_s)
        await asyncio.sleep(retry_s)
