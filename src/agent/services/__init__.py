"""`python -m agent services`: the agent's own long-running services (a bridge, a poller: /var/agent/services.toml),
in a container of their own beside agentd's conversation containers. First the agent's home folder and browsers on
the volume (home.py), then the services with the agent's git identity (supervise.py); on SIGTERM the services stop
cleanly before the process exits."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Coroutine
from pathlib import Path

from agent.services.home import prepare as prepare_home

log = logging.getLogger(__name__)

def git_identity(agent_name: str, git_email: str = "") -> dict[str, str]:
    """The agent's git identity for the services. `git_email` should be the GitHub account's noreply address, so
    GitHub credits the agent's commits to that account."""
    email = git_email or f"{agent_name}@users.noreply.github.com"
    return {"GIT_AUTHOR_NAME": agent_name, "GIT_COMMITTER_NAME": agent_name,
            "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}


async def keep_services(services: Coroutine, stopping: asyncio.Event) -> None:
    """The agent's services until `stopping` is set; they stop before this returns."""
    task = asyncio.create_task(services, name="services")
    task.add_done_callback(_services_ended)
    try:
        await stopping.wait()
    finally:
        task.cancel()
        await asyncio.wait([task])


def services_main(argv: list[str] | None = None) -> int:
    """`agent services`: the home folder and browsers on the volume first, then the services with the agent's git
    identity until SIGTERM."""
    import argparse

    from agent.services.supervise import supervise

    parser = argparse.ArgumentParser(prog="agent services", description="Keep the agent's own services running")
    parser.add_argument("--root", default="/var/agent")
    parser.add_argument("--agent-name", default="agent")
    parser.add_argument("--git-email", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    root = Path(args.root)
    prepare_home(root)
    os.environ.update(git_identity(args.agent_name, args.git_email))

    async def run() -> None:
        stopping = asyncio.Event()
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
        await keep_services(supervise(root / "services.toml", root / "run"), stopping)

    asyncio.run(run())
    return 0


def _services_ended(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("the services supervisor stopped", exc_info=task.exception())
