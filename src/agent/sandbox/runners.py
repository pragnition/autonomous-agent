"""The open agent container's runners (spec v3 O8): one process runs one SandboxRunner per queue.

core writes `{queue: sandbox token}` to a file the container mounts. Every few seconds the process reconciles its
runners with that file: it starts a runner for a new queue, restarts one whose token changed (core replaced a
revoked or expiring token), and stops one whose queue is gone. A runner that exits on its own (its token stopped
working) stays down until core writes a new token, so nobody has to touch the laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def profile_for(queue: str) -> str:
    return "general" if queue == "general" else queue.split("-", 1)[0]


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


def read_tokens(path: Path) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


class RunnerSet:
    def __init__(self, make_runner: Callable[[str, str], Any]):
        self.make_runner = make_runner
        self.tokens: dict[str, str] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def _cancel(self, queue: str) -> None:
        task = self.tasks.pop(queue)
        self.tokens.pop(queue, None)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def reconcile(self, tokens: dict[str, str]) -> None:
        for queue in [q for q in self.tasks if q not in tokens]:
            log.info("stopping the runner for %s", queue)
            await self._cancel(queue)
        for queue, token in tokens.items():
            if queue in self.tasks:
                if self.tokens[queue] == token:
                    continue  # running, or exited with a token core hasn't replaced yet
                await self._cancel(queue)
            log.info("starting the runner for %s", queue)
            self.tokens[queue] = token
            self.tasks[queue] = asyncio.create_task(self.make_runner(queue, token).run_forever())

    async def stop(self) -> None:
        for queue in list(self.tasks):
            await self._cancel(queue)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agent.proxy import ProxyClient
    from agent.runner.claude import ClaudeRunner
    from agent.sandbox.link import CoreLink
    from agent.sandbox.runner import SandboxRunner, SessionUsers
    from agent.sandbox.worktrees import Worktrees

    parser = argparse.ArgumentParser(prog="agent runners", description="Run a runner per queue (open mode)")
    parser.add_argument("--tokens", default="/run/agent/runner-tokens.json")
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
    os.environ.update(env)  # the git identity, for setup commands and open_pr's checks in plain shells
    claude = ClaudeRunner(ProxyClient(args.proxy), args.proxy, root / "transcripts")
    users = SessionUsers()

    def make_runner(queue: str, token: str) -> SandboxRunner:
        return SandboxRunner(CoreLink(args.core, token), Worktrees(root, args.core, lambda: token), users, claude,
                             sandbox=queue, profile=profile_for(queue), bundles_dir=root / "bundles",
                             sessions_root=root / "sessions", scratch_root=root / "scratch",
                             agent_name=args.agent_name, extra_env=env,
                             env_files=[Path(args.tokens).parent / "linear.env"])

    async def run() -> None:
        runners = RunnerSet(make_runner)
        while True:
            await runners.reconcile(read_tokens(Path(args.tokens)))
            await asyncio.sleep(args.check_s)

    asyncio.run(run())
    return 0
