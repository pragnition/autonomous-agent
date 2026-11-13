"""CLI tools run by core on the agent's behalf, starting with Azure's `az` (spec v2 §7, v1 §6.16 rule format).

The agent never gets a shell next to core's credentials: it calls a typed tool, and code decides. `deny` rules
(secret-printing commands) are refused; `confirm` rules (major infrastructure changes) wait for an admin's
click; unattended work may run only `read` rules; everything else runs on its own and is logged. Output is
stripped of secrets and capped; `save_to` hands the full output back as a file for the session's worktree.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import PurePath

from agent import events
from agent.config import CliRules
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema
from agent.memory.scrub import scrub

TIMEOUT_S = 60
RELOGIN = "The Azure login has expired."
_SAVE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def leading_words(args: list[str]) -> list[str]:
    words = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg)
    return words


def _match(words: list[str], tokens: list[str]) -> bool:
    if not tokens:
        return not words
    if tokens[0] == "*":
        return any(_match(words[i:], tokens[1:]) for i in range(len(words) + 1))
    return bool(words) and fnmatch.fnmatchcase(words[0], tokens[0]) and _match(words[1:], tokens[1:])


def matches(args: list[str], pattern: str) -> bool:
    """Word glob over the command's leading words: `*` alone matches any number of words, `credential*` one word."""
    return _match(leading_words(args), pattern.split())


def classify(rules: CliRules, args: list[str], *, unattended: bool) -> str:
    if args and args[0].startswith("-"):  # global options first would hide the command from every rule
        return "deny"
    if any(matches(args, p) for p in rules.deny):
        return "deny"
    if unattended:
        return "run" if any(matches(args, p) for p in rules.read) else "deny"
    if any(matches(args, p) for p in rules.confirm):
        return "confirm"
    return "run"


def valid_save_name(name: str | None) -> bool:
    return bool(name) and name not in (".", "..") and bool(_SAVE_NAME.match(name))


class CliRunner:
    def __init__(self, rules: CliRules, *, secret_values: list[str], exec_fn=asyncio.create_subprocess_exec):
        self.rules, self.secret_values, self.exec_fn = rules, list(secret_values), exec_fn

    async def run(self, args: list[str], *, save_to: str | None = None) -> ToolReply:
        if save_to is not None and not valid_save_name(save_to):
            return ToolReply("refused: save_to must be a plain file name such as logs.json")
        proc = await self.exec_fn(self.rules.command, *args, env={**os.environ, **self.rules.env},
                                  stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                  stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            return ToolReply(f"timed out after {TIMEOUT_S} s; narrow the query")
        full = scrub(out.decode(errors="replace"), self.secret_values)
        if any(p in full for p in self.rules.relogin_patterns):
            return ToolReply(f"{RELOGIN} An admin can log it in again (az_login).\n{full[:500]}")
        limit = self.rules.max_output_chars
        preview = full[:limit]
        if len(full) > limit:
            preview += f"\n[output cut at {limit} chars; narrow it, e.g. --query]"
        if proc.returncode:
            preview = f"exit code {proc.returncode}\n{preview}"
        if save_to:
            return ToolReply(f"{preview}\n[full output: {len(full)} chars, saved as {save_to}]", file=(save_to, full.encode()))
        return ToolReply(preview)


Propose = Callable[[ToolContext, str, list[str]], Awaitable[str]]


async def _no_proposals(ctx: ToolContext, cli: str, args: list[str]) -> str:
    return "This needs an admin's confirmation, and confirmations aren't set up yet."


def register_cli_tools(router: AgentToolRouter, *, runners: dict[str, CliRunner], propose: Propose | None = None
                       ) -> None:
    propose = propose or _no_proposals
    for cli, runner in runners.items():
        tool = PurePath(runner.rules.command).name

        async def handler(ctx: ToolContext, args: dict, cli=cli, runner=runner, tool=tool) -> ToolReply:
            argv = args.get("args")
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                return ToolReply("args must be a list of strings, e.g. [\"group\", \"list\"]")
            verdict = classify(runner.rules, argv, unattended=ctx.unattended)
            events.append(router.conn, "cli_command", task_id=ctx.task_id, actor=ctx.actor,
                          data={"cli": cli, "args": argv, "verdict": verdict, "work_item": ctx.work_item_id})
            if verdict == "deny":
                if argv and argv[0].startswith("-"):
                    return ToolReply("refused: put global options such as -o or --query after the command")
                if ctx.unattended and not any(matches(argv, p) for p in runner.rules.deny):
                    return ToolReply("refused: unattended work may only run read commands "
                                     f"({', '.join(runner.rules.read)})")
                return ToolReply(f"refused: `{tool} {' '.join(leading_words(argv))}` can print secrets or tokens")
            if verdict == "confirm":
                return ToolReply(await propose(ctx, cli, argv))
            return await runner.run(argv, save_to=args.get("save_to"))

        router.register(ToolSpec(
            tool, f"Run `{tool}` with an argument list (no shell). Reads and routine changes run straight away; "
                  "major infrastructure changes wait for an admin. Use save_to for large output: you get a preview "
                  "and the full output lands in your worktree.",
            schema({"args": "array"}, {"save_to": "string"}), "act", tuple(runner.rules.profiles)), handler)
