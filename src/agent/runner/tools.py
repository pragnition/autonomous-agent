"""core's tools as in-process SDK tools. The tool code runs in the runner's process, so the session token stays
out of the session's environment, files and command lines. Three tools do local work: work_on binds the conversation's project and makes its
checkout, open_pr runs the checks and pushes the branch, and post_files reads files from the session folder."""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import tool

from agent.runner.link import SessionRevoked

CHECK_TIMEOUT_S = 1800
SETUP_TIMEOUT_S = 1800
LOG_TAIL = 3000
SETUP_TAIL = 1500


@dataclass
class Checkout:
    """The session's checkout as it is now: set at session start when the conversation already has one, and by
    work_on partway through, so open_pr and post_files always use the current one."""
    path: Path | None
    branch: str | None
    checks: list[str]


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


KEYS_PREFIX, MARKER_PREFIX = ".test-keys-", ".setup-failed-"
PRIVATE_PREFIXES = (KEYS_PREFIX, MARKER_PREFIX)  # the runner's own files in a session folder; post_files refuses them


def setup_marker(session_dir: Path, project: str) -> Path:
    """In the session folder, outside the checkout, while a project's setup hasn't passed (it failed, or the runner
    stopped partway through it): the next work_on runs it again."""
    return session_dir / f"{MARKER_PREFIX}{project}"


def _private(path: Path) -> bool:
    """The test-keys file or a setup marker, by its own name or the name a link to it resolves to."""
    return path.name.startswith(PRIVATE_PREFIXES) or path.resolve().name.startswith(PRIVATE_PREFIXES)


async def run_setup(commands: list[str], checkout: Path, marker: Path, shell=run_shell) -> tuple[bool, str]:
    """Run a project's setup in its checkout, stopping at the first failure. `marker` is written before the first
    command and removed only once every one has passed, so setup that failed or never finished is run again.
    Returns whether setup passed and, when it didn't, the tail of its output."""
    marker.write_text("setup started\n")
    log = []
    for cmd in commands:
        code, out = await shell(cmd, checkout, SETUP_TIMEOUT_S, None)
        log.append(f"$ {cmd}\n{out}")
        if code != 0:
            marker.write_text(f"{cmd}\n")
            return False, f"`{cmd}` exited {code}:\n" + "\n".join(log)[-SETUP_TAIL:]
    marker.unlink(missing_ok=True)
    return True, ""


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)  # an older file keeps its mode through O_CREAT


def remote_sdk_tools(manifest: list[dict], link, tok: str, *, session_dir: Path, checkout: Checkout, worktrees,
                     agent_name: str = "agent", shell=run_shell, app_env_dir: Path | None = None) -> list:
    async def forward(name: str, args: dict) -> str:
        try:
            reply = await link.tool(tok, name, args)
        except SessionRevoked:
            return "This session was stopped."
        return reply.get("text", "")

    async def work_on(args: dict) -> str:
        try:
            details = await link.bind(tok, str(args.get("project") or ""), str(args.get("title") or ""))
        except SessionRevoked:
            return "This session was stopped."
        if "error" in details:
            return details["error"]
        project, branch = details["project"], details["branch"]
        try:
            path, created = await asyncio.to_thread(worktrees.prepare, session_dir / project, project, branch,
                                                    details["default_branch"])
        except Exception as e:
            return f"this conversation is now bound to {project}, but its checkout couldn't be made: {e}"
        marker = setup_marker(session_dir, project)
        if created or marker.exists():
            passed, tail = await run_setup(list(details.get("setup") or []), path, marker, shell)
            done = "setup passed" if passed else f"setup failed: {tail}"
        else:
            done = "setup not run again (it ran when this checkout was made)"
        keys = session_dir / f"{KEYS_PREFIX}{project}.env"
        _write_private(keys, "".join(f"{k}={v}\n" for k, v in sorted((details.get("test_secrets") or {}).items())))
        checkout.path, checkout.branch, checkout.checks = path, branch, list(details.get("checks") or [])
        lines = [f"checkout: {path} (branch {branch})", done,
                 f"test keys: {keys} (KEY=value lines; later turns get them as environment variables)"]
        app_env = app_env_dir / f"{project}.env" if app_env_dir is not None else None
        if app_env is not None and app_env.is_file():
            lines.append(f"app env: {app_env} (the app's own .env for running it locally)")
        return "\n".join(lines)

    async def open_pr(args: dict) -> str:
        worktree, branch = checkout.path, checkout.branch
        if worktree is None or not branch:
            return "call work_on(<project>) first"
        title = args.get("title") or "Agent changes"
        default_email = f"{agent_name}@users.noreply.github.com"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # no token: committing needs none
        for key, default in (("GIT_AUTHOR_NAME", agent_name), ("GIT_COMMITTER_NAME", agent_name),
                             ("GIT_AUTHOR_EMAIL", default_email), ("GIT_COMMITTER_EMAIL", default_email)):
            env.setdefault(key, default)  # runner.main already set these from --git-email; this is only a fallback
        await shell(f"git add -A && (git diff --cached --quiet || git commit -q -m {shlex.quote(title)})",
                    worktree, 120, env)
        log, passed = [], True
        for cmd in checkout.checks:
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

    def resolve(raw: str) -> Path:
        """A relative path is relative to the session's working folder: its checkout, or the session folder."""
        if Path(raw).is_absolute():
            return Path(raw)
        bases = [b for b in (checkout.path, session_dir) if b is not None]
        return next((b / raw for b in bases if (b / raw).is_file()), bases[0] / raw)

    async def post_files(args: dict) -> str:
        items = []
        for raw in args.get("paths") or []:
            path = resolve(raw)
            if not _inside(path, session_dir):
                return f"refused: {raw} is outside your session folder"
            if _private(path):
                return f"refused: {raw} is the runner's own file (test keys or setup state), which is never posted"
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

    local = {"work_on": work_on, "open_pr": open_pr, "post_files": post_files}
    tools = []
    for entry in manifest:
        name = entry["name"]

        async def handler(args, name=name):
            if name in local:
                return _text(await local[name](args))
            return _text(await forward(name, args))

        tools.append(tool(name, entry.get("description", ""), entry.get("input_schema") or
                          {"type": "object", "properties": {}})(handler))
    return tools
