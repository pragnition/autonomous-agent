"""core's tools as in-process SDK tools (spec v2 §6). The tool code runs in the runner's process, so the session
token never reaches the session's OS user. Two tools do local work first: open_pr runs the checks and pushes
the branch, and post_files reads files from the worktree."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from claude_agent_sdk import tool

from agent.sandbox.link import SessionRevoked

CHECK_TIMEOUT_S = 1800
LOG_TAIL = 3000


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


async def run_shell(cmd: str, cwd: Path, timeout_s: int = CHECK_TIMEOUT_S, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("bash", "-lc", cmd, cwd=str(cwd), env=env,
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        return 124, f"`{cmd}` timed out after {timeout_s} s"
    return proc.returncode or 0, out.decode(errors="replace")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def remote_sdk_tools(manifest: list[dict], link, tok: str, *, worktree: Path | None,
                     checks: list[str], branch: str | None, worktrees=None, agent_name: str = "agent",
                     structured_sink: dict | None = None, structured_schema: dict | None = None,
                     shell=run_shell) -> list:
    async def forward(name: str, args: dict) -> str:
        try:
            reply = await link.tool(tok, name, args)
        except SessionRevoked:
            return "This session was stopped."
        return reply.get("text", "")

    async def open_pr(args: dict) -> str:
        if worktree is None or not branch or worktrees is None:
            return "open_pr needs a coding worktree; this session doesn't have one."
        title = args.get("title") or "Agent changes"
        ident = {"GIT_AUTHOR_NAME": agent_name, "GIT_COMMITTER_NAME": agent_name,
                 "GIT_AUTHOR_EMAIL": f"{agent_name}@users.noreply.github.com",
                 "GIT_COMMITTER_EMAIL": f"{agent_name}@users.noreply.github.com"}
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **ident}  # no token: the session user runs this
        await shell(f"git add -A && (git diff --cached --quiet || git commit -q -m {shlex.quote(title)})",
                    worktree, 120, env)
        log, passed = [], True
        for cmd in checks:
            code, out = await shell(cmd, worktree, CHECK_TIMEOUT_S, None)
            log.append(f"$ {cmd}\n{out}")
            if code != 0:
                passed = False
                break
        try:
            sha = await asyncio.to_thread(worktrees.push, worktree, branch) if passed else \
                await asyncio.to_thread(worktrees.head, worktree)
        except Exception as e:
            return f"couldn't push {branch}: {e}"
        return await forward("open_pr", {**args, "branch": branch, "sha": sha,
                                         "checks": {"passed": passed, "log_tail": "\n".join(log)[-LOG_TAIL:]}})

    async def post_files(args: dict) -> str:
        if worktree is None:
            return "post_files needs a worktree; this session doesn't have one."
        items = []
        for raw in args.get("paths") or []:
            path = Path(raw) if Path(raw).is_absolute() else worktree / raw
            if not _inside(path, worktree):
                return f"refused: {raw} is outside your worktree"
            if not path.is_file():
                return f"no such file: {raw}"
            items.append((path.name, path.read_bytes()))
        try:
            posted = await link.files(tok, items, args.get("text", ""))
        except SessionRevoked:
            return "This session was stopped."
        if posted is False:
            return (f"recorded {len(items)} file(s) as evidence, but this conversation has no Slack thread, so nobody "
                    "saw them. Attach them where the request came from (e.g. the Linear issue) if they matter.")
        return f"posted {len(items)} file(s)"

    local = {"open_pr": open_pr, "post_files": post_files}
    tools = []
    for entry in manifest:
        name = entry["name"]

        async def handler(args, name=name):
            if name in local:
                return _text(await local[name](args))
            return _text(await forward(name, args))

        tools.append(tool(name, entry.get("description", ""), entry.get("input_schema") or
                          {"type": "object", "properties": {}})(handler))
    if structured_sink is not None:
        @tool("submit_result", "Return your structured answer. Call it once, last.",
              structured_schema or {"type": "object", "properties": {}})
        async def submit_result(args):
            structured_sink["output"] = args
            return _text("recorded")
        tools.append(submit_result)
    return tools
