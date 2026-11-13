"""The two moments a person steps in for a CLI (spec v2 §7): confirming a major infrastructure change, and
logging the CLI in with a device code. Both are code-driven; the model only asks."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePath

from agent import events
from agent.core.agent_tools import ALL_PROFILES, AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema
from agent.core.sessions import WorkQueue

PROPOSAL_TTL = timedelta(hours=24)
LOGIN_TIMEOUT_S = 15 * 60
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"enter the code\s+([A-Za-z0-9-]+)", re.I)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def command_text(command: str, args: list[str]) -> str:
    return " ".join([PurePath(command).name, *(shlex.quote(a) for a in args)])


class Proposals:
    """A Confirm / Cancel button for a `confirm`-rule command. Only an admin's click runs it."""

    def __init__(self, conn: sqlite3.Connection, slack, *, config, runner_for: Callable[[str], object],
                 queue: WorkQueue, turn_payload: Callable[[str, str, str | None, bool], dict] | None = None):
        self.conn, self.slack, self.config, self.runner_for, self.queue = conn, slack, config, runner_for, queue
        self.turn_payload = turn_payload or (lambda convo_id, text, actor, unattended:
                                             {"text": text, "actor": actor, "unattended": unattended})

    def _target(self, ctx: ToolContext) -> str | None:
        """Where the result goes: the session that asked for production work, or this conversation."""
        row = self.conn.execute("SELECT payload FROM work_items WHERE id = ?", (ctx.work_item_id,)).fetchone()
        payload = json.loads(row["payload"]) if row else {}
        return payload.get("caller_conversation_id") or ctx.conversation_id

    async def propose(self, ctx: ToolContext, cli: str, args: list[str]) -> str:
        runner = self.runner_for(cli)
        text = command_text(runner.rules.command, args)
        pid, now = f"P-{uuid.uuid4().hex[:10]}", datetime.now(UTC)
        target = self._target(ctx)
        channel, thread_ts = ctx.channel, ctx.thread_ts
        if not channel and target:
            row = self.conn.execute("SELECT channel, thread_ts FROM conversations WHERE id = ?", (target,)).fetchone()
            channel, thread_ts = (row["channel"], row["thread_ts"]) if row else (None, None)
        payload = {"cli": cli, "args": args, "actor": ctx.actor, "target": target,
                   "channel": channel, "thread_ts": thread_ts}
        self.conn.execute("INSERT INTO proposals(id, conversation_id, kind, payload, created_at, expires_at) "
                          "VALUES (?, ?, 'cli', ?, ?, ?)",
                          (pid, ctx.conversation_id, json.dumps(payload), _iso(now), _iso(now + PROPOSAL_TTL)))
        if channel:
            message = f"`{text}` is a major change and needs an admin's confirmation (asked by {ctx.actor or 'the agent'})."
            await self.slack.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=message,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}},
                        {"type": "actions", "elements": [
                            {"type": "button", "action_id": f"cli_confirm:{pid}", "style": "primary",
                             "text": {"type": "plain_text", "text": "Confirm"}},
                            {"type": "button", "action_id": f"cli_cancel:{pid}",
                             "text": {"type": "plain_text", "text": "Cancel"}}]}])
        events.append(self.conn, "cli_proposed", task_id=ctx.task_id, actor=ctx.actor, data={"id": pid, "command": text})
        return "Waiting for an admin to confirm. The result will arrive as your next message; end this turn now."

    async def on_action(self, action_id: str, slack_user: str) -> str:
        kind, _, pid = action_id.partition(":")
        row = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return "Unknown request."
        person = self.config.person_by_slack(slack_user)
        if person is None or person.role != "admin":
            return "Only an admin can confirm this."
        if row["status"] != "pending":
            return "Already handled."
        if row["expires_at"] < _iso(datetime.now(UTC)):
            self.conn.execute("UPDATE proposals SET status = 'expired' WHERE id = ?", (pid,))
            return "This request expired."
        payload = json.loads(row["payload"])
        runner = self.runner_for(payload["cli"])
        text = command_text(runner.rules.command, payload["args"])
        if kind == "cli_cancel":
            self.conn.execute("UPDATE proposals SET status = 'cancelled' WHERE id = ?", (pid,))
            await self._post(payload, f"`{text}` was cancelled by {person.key}.")
            await self._follow_up(payload, f"An admin cancelled the command `{text}`; it did not run.")
            return "Cancelled."
        self.conn.execute("UPDATE proposals SET status = 'confirmed' WHERE id = ?", (pid,))
        events.append(self.conn, "cli_confirmed", task_id=None, actor=person.key, data={"id": pid, "command": text})
        reply = await runner.run(payload["args"])
        await self._post(payload, f"Ran `{text}` (confirmed by {person.key}):\n```{reply.text[:3000]}```")
        await self._follow_up(payload, f"Result of the confirmed command `{text}`:\n{reply.text}")
        return "Confirmed."

    async def _post(self, payload: dict, text: str) -> None:
        if payload.get("channel"):
            await self.slack.chat_postMessage(channel=payload["channel"], thread_ts=payload.get("thread_ts"), text=text)

    async def _follow_up(self, payload: dict, text: str) -> None:
        target = payload.get("target")
        row = self.conn.execute("SELECT sandbox FROM conversations WHERE id = ?", (target,)).fetchone() if target else None
        if row is not None:
            self.queue.enqueue(target, row["sandbox"], "turn",
                               self.turn_payload(target, text, payload.get("actor"), False))


def parse_device_code(output: str) -> tuple[str, str] | None:
    url, code = _URL.search(output), _CODE.search(output)
    return (url.group(0).rstrip("."), code.group(1)) if url and code else None


class DeviceCodeLogin:
    """`az login --use-device-code`: the URL and code go by DM to the admin who asked, never to a channel."""

    def __init__(self, slack, *, config, rules: dict, exec_fn=asyncio.create_subprocess_exec,
                 run_as: int | None = None):
        """run_as: the uid the login runs as, so the agent container (open mode) owns and can refresh it."""
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
                                        "to you by DM.", schema(), "admin", ALL_PROFILES), handler)

