"""The supervisor's watchdog: health-checks core and speaks up, independently of core."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp


class Watchdog:
    def __init__(self, targets: dict[str, str], *, alert: Callable[[str], Awaitable[None]],
                 restart: Callable[[str], Awaitable[None]], check: Callable[[str], Awaitable[bool]],
                 threshold: int = 3):
        self.targets, self.alert, self.restart, self.check, self.threshold = targets, alert, restart, check, threshold
        self.failures = {name: 0 for name in targets}
        self.down: set[str] = set()

    async def tick(self) -> None:
        for name, url in self.targets.items():
            healthy = await self.check(url)
            if healthy:
                self.failures[name] = 0
                if name in self.down:
                    self.down.discard(name)
                    await self.alert(f"{name} is back up.")
                continue
            self.failures[name] += 1
            if self.failures[name] == self.threshold and name not in self.down:
                self.down.add(name)
                await self.alert(f"{name} is down ({self.threshold} failed health checks); restarting it once.")
                await self.restart(name)


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


def webhook_alert(url: str) -> Callable[[str], Awaitable[None]]:
    async def alert(text: str) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(url, json={"text": text}) as resp:
                resp.raise_for_status()

    return alert


def health_targets(environ) -> dict[str, str]:
    """The one thing to watch: core."""
    return {"core": environ.get("CORE_HEALTH", "http://core:8080/health")}


def main() -> int:
    import asyncio
    import os

    from agent import paths
    from agent.secrets import load_secrets

    secrets = load_secrets(paths.SECRETS_FILE)
    containers = {"core": os.environ.get("CORE_CONTAINER", "agent-core")}  # the only container the watchdog restarts
    targets = health_targets(os.environ)

    async def print_alert(text: str) -> None:
        print(text, flush=True)

    alert = webhook_alert(secrets["SLACK_ALERT_WEBHOOK"]) if secrets.get("SLACK_ALERT_WEBHOOK") else print_alert

    async def restart(name: str) -> None:
        if name in containers:
            await docker_restart(containers[name])

    watchdog = Watchdog(targets, alert=alert, restart=restart, check=http_check)

    async def loop() -> None:
        while True:
            await watchdog.tick()
            await asyncio.sleep(60)

    asyncio.run(loop())
    return 0
