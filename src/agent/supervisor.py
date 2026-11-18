"""The supervisors' watchdog: health-checks core, the runner and the other supervisor, and speaks up, independently
of core.

Two supervisors run, `primary` and `standby`, and each watches core, the runner's heartbeat (as core sees it) and the
other (its peer). Each answers its own `/health`, which turns unhealthy when its watch loop stops ticking, so a hung
supervisor looks down too. Either one restarts its peer when the peer is down. Only the primary acts on core and the
runner; the standby takes them over only while the primary is down, so nothing is restarted twice and nobody gets
the same alert twice. A target is restarted once; down again within 30 minutes, it gets an alert and no restart.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

INTERVAL_S = 60
RESTART_COOLDOWN_S = 30 * 60  # down again this soon after a restart: alert only, no restart loop


class Watchdog:
    def __init__(self, targets: dict[str, str], *, alert: Callable[[str], Awaitable[None]],
                 restart: Callable[[str], Awaitable[None]], check: Callable[[str], Awaitable[bool]],
                 threshold: int = 3, defer_to: dict[str, str] | None = None,
                 depends_on: dict[str, str] | None = None, cooldown_s: float = RESTART_COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic):
        """defer_to maps a target to a peer: this watchdog acts on the target only while that peer is down.
        depends_on maps a target to the one its check goes through: while that one is failing, the target isn't
        checked (the runner's heartbeat is read from core, so a down core says nothing about the runner).
        A target is restarted once per outage. One that is down again within cooldown_s of its last restart is only
        alerted about, so a target that can't stay up isn't restarted in a loop."""
        self.targets, self.alert, self.restart, self.check, self.threshold = targets, alert, restart, check, threshold
        self.defer_to, self.depends_on = defer_to or {}, depends_on or {}
        self.cooldown_s, self.clock = cooldown_s, clock
        self.failures = {name: 0 for name in targets}
        self.down: set[str] = set()
        self.restarted_at: dict[str, float] = {}

    def _deferred(self, name: str) -> bool:
        peer = self.defer_to.get(name)
        return peer is not None and peer not in self.down

    def _order(self, name: str) -> tuple[bool, bool]:
        # peers first, so a target deferred to a peer sees whether the peer is down on this tick; a target that
        # depends on another comes after it, so it sees that one's result from this tick
        return name in self.defer_to, name in self.depends_on

    async def tick(self) -> None:
        for name in sorted(self.targets, key=self._order):
            dep = self.depends_on.get(name)
            if dep in self.failures and self.failures[dep]:
                continue
            healthy = await self.check(self.targets[name])
            if healthy:
                self.failures[name] = 0
                if name in self.down:
                    self.down.discard(name)
                    await self.alert(f"{name} is back up.")
                continue
            self.failures[name] += 1
            if self.failures[name] >= self.threshold and name not in self.down and not self._deferred(name):
                self.down.add(name)
                await self._act(name)

    async def _act(self, name: str) -> None:
        now, last = self.clock(), self.restarted_at.get(name)
        if last is not None and now - last < self.cooldown_s:
            await self.alert(f"{name} is down again ({self.failures[name]} failed health checks), less than "
                             f"{self.cooldown_s / 60:.0f} minutes after I restarted it. Not restarting it again; "
                             "it needs a person.")
            return
        self.restarted_at[name] = now
        await self.alert(f"{name} is down ({self.failures[name]} failed health checks); restarting it once.")
        await self.restart(name)


class Heartbeat:
    """When the watch loop last finished a tick; /health is healthy only while that is recent."""

    def __init__(self, max_age_s: float, clock: Callable[[], float] = time.monotonic):
        self.max_age_s, self.clock = max_age_s, clock
        self.last = clock()  # a fresh supervisor gets one window to finish its first tick

    def beat(self) -> None:
        self.last = self.clock()

    def healthy(self) -> bool:
        return self.clock() - self.last <= self.max_age_s


def health_app(heartbeat: Heartbeat) -> web.Application:
    async def health(request: web.Request) -> web.Response:
        ok = heartbeat.healthy()
        return web.json_response({"ok": ok}, status=200 if ok else 503)

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def http_check(url: str) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s, s.get(url) as resp:
            return resp.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


async def docker_restart(name: str, socket: str = "/var/run/docker.sock") -> None:
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket)) as s:
        async with s.post(f"http://docker/containers/{name}/restart") as resp:
            resp.raise_for_status()


def webhook_alert(url: str, who: str) -> Callable[[str], Awaitable[None]]:
    async def alert(text: str) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(url, json={"text": f"[{who}] {text}"}) as resp:
                resp.raise_for_status()

    return alert


def health_targets(environ) -> dict[str, str]:
    """What to watch: core, the runner's heartbeat as core sees it, and the other supervisor when PEER_HEALTH is
    set."""
    targets = {"core": environ.get("CORE_HEALTH", "http://core:8080/health"),
               "runner": environ.get("RUNNER_HEALTH", "http://core:8080/health/runner")}
    if environ.get("PEER_HEALTH"):
        targets["peer"] = environ["PEER_HEALTH"]
    return targets


def restartable(environ) -> dict[str, str]:
    """Target name to container: the only containers a supervisor restarts."""
    containers = {"core": environ.get("CORE_CONTAINER", "agent-core"),
                  "runner": environ.get("RUNNER_CONTAINER", "agent-runner")}
    if environ.get("PEER_HEALTH") and environ.get("PEER_CONTAINER"):
        containers["peer"] = environ["PEER_CONTAINER"]
    return containers


DEPENDS_ON = {"runner": "core"}  # the runner's heartbeat is read from core


def deferrals(environ, targets: dict[str, str]) -> dict[str, str]:
    """The standby leaves core and the runner to the primary while the primary (its peer) is up."""
    if environ.get("SUPERVISOR_ROLE", "primary") == "standby" and "peer" in targets:
        return {name: "peer" for name in ("core", "runner") if name in targets}
    return {}


def main() -> int:
    import asyncio
    import os

    from agent import paths
    from agent.secrets import load_secrets

    secrets = load_secrets(paths.SECRETS_FILE)
    role = os.environ.get("SUPERVISOR_ROLE", "primary")
    targets = health_targets(os.environ)
    containers = restartable(os.environ)
    port = int(os.environ.get("SUPERVISOR_PORT", "8090"))

    async def print_alert(text: str) -> None:
        print(f"[supervisor {role}] {text}", flush=True)

    hook = secrets.get("SLACK_ALERT_WEBHOOK")
    alert = webhook_alert(hook, f"supervisor {role}") if hook else print_alert

    async def restart(name: str) -> None:
        if name in containers:
            await docker_restart(containers[name])

    watchdog = Watchdog(targets, alert=alert, restart=restart, check=http_check,
                        defer_to=deferrals(os.environ, targets), depends_on=DEPENDS_ON)
    heartbeat = Heartbeat(max_age_s=3 * INTERVAL_S)

    async def loop() -> None:
        runner = web.AppRunner(health_app(heartbeat))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        while True:
            await watchdog.tick()  # if it raises, the process exits and Docker restarts the container
            heartbeat.beat()
            await asyncio.sleep(INTERVAL_S)

    asyncio.run(loop())
    return 0
