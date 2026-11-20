"""The agent container's runner process: one runner on core's runner token.

core writes the runner token to a file the container mounts. Every few seconds the process checks that file: it
starts the runner when a token appears, restarts it when the token changes (core replaced a revoked or expiring
one), and stops it when the file is empty or gone. A runner that exits on its own (its token stopped working)
stays down until core writes a new token, so nobody has to touch the laptop.

Alongside it, from the moment the process starts and whether or not there is a token yet, the process keeps the
agent's own services running (services.py). On SIGTERM it stops those services cleanly before it exits. Before
any of that, it makes sure the agent's home folder and browsers are in place on the volume (home.py). It also
reports what the machine is doing to core, for the dashboard's host monitor (host.py), once there is a token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from agent.runner.home import prepare as prepare_home

log = logging.getLogger(__name__)


def agent_environment(environ, agent_name: str, git_email: str = "") -> dict[str, str]:
    """What every session in the agent container gets on top of its own environment: the agent's keys from
    agent.env (GH_TOKEN, dev and test keys) and a git identity. The keys are passed explicitly because the Claude
    runner blanks secret-looking variables it would otherwise inherit. `git_email` should be the GitHub account's
    noreply address, so GitHub credits the agent's commits to that account."""
    from agent.runner.claude import SECRETISH

    email = git_email or f"{agent_name}@users.noreply.github.com"
    return {**{k: v for k, v in environ.items() if SECRETISH.search(k)},
            "GIT_AUTHOR_NAME": agent_name, "GIT_COMMITTER_NAME": agent_name,
            "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}


class RunnerKeeper:
    """Keeps one runner going on the current token."""

    def __init__(self, make_runner: Callable[[str], Any]):
        self.make_runner = make_runner
        self.token = ""
        self.task: asyncio.Task | None = None

    async def stop(self) -> None:
        task, self.task, self.token = self.task, None, ""
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def reconcile(self, token: str) -> None:
        if token == self.token:
            return  # running, or exited with a token core hasn't replaced yet
        if self.task is not None:
            log.info("stopping the runner")
            await self.stop()
        if token:
            log.info("starting the runner")
            self.token = token
            self.task = asyncio.create_task(self.make_runner(token).run_forever())


async def serve(keeper: RunnerKeeper, read_token: Callable[[], str], check_s: float, services: Coroutine,
                stopping: asyncio.Event) -> None:
    """The runner loop and the agent's services side by side, until `stopping` is set. The services start whether or
    not there is a token yet, and nothing they do can stop the runner loop."""
    task = asyncio.create_task(services, name="services")
    task.add_done_callback(_services_ended)
    try:
        while not stopping.is_set():
            await keeper.reconcile(read_token())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), check_s)
    finally:
        task.cancel()
        await asyncio.wait([task])  # the services stop before the process exits


def _services_ended(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("the services supervisor stopped", exc_info=task.exception())


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent.core.runner_tokens import read_runner_token
    from agent.proxy import ProxyClient
    from agent.runner.claude import ClaudeRunner
    from agent.runner.host import HostSampler, core_poster, report_forever
    from agent.runner.link import CoreLink
    from agent.runner.services import supervise
    from agent.runner.worker import Runner

    parser = argparse.ArgumentParser(prog="agent runner", description="Run the agent container's runner")
    parser.add_argument("--token-file", default="/run/agent/runner-token")
    parser.add_argument("--core", default="http://core:8080")
    parser.add_argument("--proxy", default="http://172.30.0.1:28091")
    parser.add_argument("--root", default="/var/agent")
    parser.add_argument("--agent-name", default="agent")
    parser.add_argument("--git-email", default="")
    parser.add_argument("--check-s", type=float, default=15.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    root = Path(args.root)
    prepare_home(root)  # the home folder and browsers on the volume, before anything runs in them
    env = agent_environment(os.environ, args.agent_name, args.git_email)
    os.environ.update(env)  # the git identity, for every commit the agent makes
    claude = ClaudeRunner(ProxyClient(args.proxy), args.proxy, root / "transcripts")
    token_file = Path(args.token_file)

    def read_token() -> str:
        return read_runner_token(token_file)
    shared = token_file.parent  # what core shares with the container: the token, linear.env, the projects' env files

    def make_runner(token: str) -> Runner:
        return Runner(CoreLink(args.core, token), claude, bundles_dir=root / "bundles",
                      claude_home=root / "claude", scratch_root=root / "scratch", extra_env=env,
                      env_files=[shared / "linear.env"], memory_dir=root / "memory")

    async def run() -> None:
        stopping = asyncio.Event()  # SIGTERM (docker stop, a redeploy): stop the services, then exit
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
        # what the machine is doing, for the dashboard's host monitor: posted to core, which keeps the history
        host = asyncio.create_task(report_forever(HostSampler(root), core_poster(args.core, read_token)), name="host")
        try:
            await serve(RunnerKeeper(make_runner), read_token, args.check_s,
                        supervise(root / "services.toml", root / "run"), stopping)
        finally:
            host.cancel()

    asyncio.run(run())
    return 0
