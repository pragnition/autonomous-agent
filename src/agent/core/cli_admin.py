"""Logging a CLI in with a device code: code-driven; the model only asks."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import PurePath

from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

LOGIN_TIMEOUT_S = 15 * 60
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"enter the code\s+([A-Za-z0-9-]+)", re.I)


def parse_device_code(output: str) -> tuple[str, str] | None:
    url, code = _URL.search(output), _CODE.search(output)
    return (url.group(0).rstrip("."), code.group(1)) if url and code else None


class DeviceCodeLogin:
    """`az login --use-device-code`: the URL and code go by DM to the admin who asked, never to a channel."""

    def __init__(self, slack, *, config, rules: dict, exec_fn=asyncio.create_subprocess_exec,
                 run_as: int | None = None):
        """run_as: the uid the login runs as, so the agent container owns and can refresh it."""
        self.slack, self.config, self.rules, self.exec_fn, self.run_as = slack, config, rules, exec_fn, run_as
        self.pending: set[asyncio.Task] = set()

    def _as_user(self) -> dict:
        return {"user": self.run_as} if self.run_as is not None else {}

    async def start(self, ctx: ToolContext, cli: str) -> str:
        rules = self.rules[cli]
        person = self.config.people.get(ctx.actor or "")
        if not rules.login or person is None:
            return "no login is configured for this CLI"
        as_agent = {"HOME": "/tmp", "USER": "agent", "LOGNAME": "agent"}  # uid run_as has no passwd entry in core
        env = {**os.environ, **rules.env, **(as_agent if self.run_as is not None else {})}
        proc = await self.exec_fn(*rules.login, env=env, stdin=asyncio.subprocess.DEVNULL,
                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **self._as_user())
        seen, found = "", None
        async with asyncio.timeout(60):
            while found is None:
                line = await proc.stdout.readline()
                if not line:
                    break
                seen += line.decode(errors="replace")
                found = parse_device_code(seen)
        if found is None:
            return f"the login didn't print a device code:\n{seen[-500:]}"
        url, code = found
        dm = (await self.slack.conversations_open(users=person.slack))["channel"]["id"]
        await self.slack.chat_postMessage(channel=dm, text=f"{PurePath(rules.command).name} login: open {url} and "
                                                           f"enter {code}. It expires in 15 minutes.")
        task = asyncio.create_task(self._finish(ctx, rules, proc, env))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return f"Sent the login code to {person.key} by DM."

    async def _finish(self, ctx: ToolContext, rules, proc, env) -> None:
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT_S):
                await proc.wait()
            check = await self.exec_fn(*(rules.login_check or []), env=env, stdin=asyncio.subprocess.DEVNULL,
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                       **self._as_user())
            out, _ = await check.communicate()
            info = json.loads(out.decode() or "{}")
            text = f"Logged in as {(info.get('user') or {}).get('name', '?')} ({info.get('name', '?')})."
        except (TimeoutError, json.JSONDecodeError) as e:
            text = f"The login didn't complete ({type(e).__name__})."
        if ctx.channel:
            await self.slack.chat_postMessage(channel=ctx.channel, thread_ts=ctx.thread_ts, text=text)


def register_login_tools(router: AgentToolRouter, login: DeviceCodeLogin) -> None:
    for cli, rules in login.rules.items():
        if not rules.login:
            continue
        tool = f"{PurePath(rules.command).name}_login"

        async def handler(ctx: ToolContext, args: dict, cli=cli) -> ToolReply:
            return ToolReply(await login.start(ctx, cli))

        router.register(ToolSpec(tool, f"Log {PurePath(rules.command).name} in with a device code; the code goes "
                                        "to you by DM.", schema(), "admin"), handler)

