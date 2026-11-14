"""The agent container's runner process: one runner on core's runner token.

core writes the runner token to a file the container mounts. Every few seconds the process checks that file: it
starts the runner when a token appears, restarts it when the token changes (core replaced a revoked or expiring
one), and stops it when the file is empty or gone. A runner that exits on its own (its token stopped working)
stays down until core writes a new token, so nobody has to touch the laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent.core.runner_tokens import read_runner_token
    from agent.proxy import ProxyClient
    from agent.runner.claude import ClaudeRunner
    from agent.runner.link import CoreLink
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
    env = agent_environment(os.environ, args.agent_name, args.git_email)
    os.environ.update(env)  # the git identity, for every commit the agent makes
    claude = ClaudeRunner(ProxyClient(args.proxy), args.proxy, root / "transcripts")
    token_file = Path(args.token_file)
    shared = token_file.parent  # what core shares with the container: the token, linear.env, the projects' env files

    def make_runner(token: str) -> Runner:
        return Runner(CoreLink(args.core, token), claude, bundles_dir=root / "bundles",
                      sessions_root=root / "sessions", scratch_root=root / "scratch", extra_env=env,
                      env_files=[shared / "linear.env"], memory_dir=root / "memory")

    async def run() -> None:
        keeper = RunnerKeeper(make_runner)
        while True:
            await keeper.reconcile(read_runner_token(token_file))
            await asyncio.sleep(args.check_s)

    asyncio.run(run())
    return 0
