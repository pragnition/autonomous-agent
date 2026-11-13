"""The runner inside the agent container (spec v3 §6): pull work from core, prepare the worktree, run a Claude
Code session with full permissions, and report the result. Limits are enforced by core's tools, not the OS."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import logging
import re
import shutil
import tarfile
from pathlib import Path

from agent.runner.base import RunResult, RunSpec
from agent.sandbox.link import SessionRevoked
from agent.sandbox.remote_tools import remote_sdk_tools, run_shell

log = logging.getLogger(__name__)
SETUP_TIMEOUT_S = 1800
FRESH_NOTE = ("(Your earlier session in this conversation couldn't be resumed. Call read_thread to catch up on "
              "the conversation so far before you answer.)\n\n")


class SessionUsers:
    """One OS user per running conversation (s1..s8), sticky while it runs, so sessions can't read each other."""

    def __init__(self, names: tuple[str, ...] = tuple(f"s{i}" for i in range(1, 9))):
        self.names, self.taken = names, {}

    def acquire(self, conversation_id: str) -> str:
        if conversation_id in self.taken:
            return self.taken[conversation_id]
        free = [n for n in self.names if n not in self.taken.values()]
        if not free:
            raise RuntimeError("no free session user")
        self.taken[conversation_id] = free[0]
        return free[0]

    def release(self, conversation_id: str) -> None:
        self.taken.pop(conversation_id, None)


_VAR = re.compile(r"\$\{(\w+)\}")


def read_env_files(paths: list[Path]) -> dict[str, str]:
    env = {}
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                env[key] = value
    return env


def expand_mcp(servers: dict, env: dict[str, str]) -> dict:
    """MCP servers with ${VAR} in their headers filled from env; a server with an unset variable is left out."""
    out = {}
    for name, server in servers.items():
        missing = {v for h in server.get("headers", {}).values() for v in _VAR.findall(h) if not env.get(v)}
        if missing:
            log.warning("leaving out MCP server %s: %s not set yet", name, ", ".join(sorted(missing)))
            continue
        out[name] = {**server, "headers": {k: _VAR.sub(lambda m: env[m.group(1)], h)
                                           for k, h in server.get("headers", {}).items()}}
    return out


class SandboxRunner:
    def __init__(self, link, worktrees, users: SessionUsers, claude, *, sandbox: str, profile: str,
                 bundles_dir: Path, sessions_root: Path, scratch_root: Path, agent_name: str = "agent",
                 heartbeat_s: float = 15, max_parallel: int = 3,
                 extra_env: dict[str, str] | None = None, env_files: list[Path] = ()):
        self.link, self.worktrees, self.users, self.claude = link, worktrees, users, claude
        self.sandbox, self.profile, self.agent_name = sandbox, profile, agent_name
        self.bundles_dir, self.sessions_root, self.scratch_root = Path(bundles_dir), Path(sessions_root), Path(scratch_root)
        self.heartbeat_s, self.max_parallel = heartbeat_s, max_parallel
        self.extra_env = dict(extra_env or {})  # agent.env and a git identity for every session
        self.env_files = [Path(f) for f in env_files]  # re-read per session: core rotates LINEAR_APP_TOKEN there

    async def _bundle(self, project: str | None) -> list[Path]:
        version, data = await self.link.bundle(self.profile, project)
        dest = self.bundles_dir / version
        if not dest.exists():
            tmp = self.bundles_dir / f".{version}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            tmp.rename(dest)
        return [p for p in (dest / "plugins" / "core", dest / "plugins" / "pack") if p.is_dir()]

    async def _workdir(self, convo: str, payload: dict) -> tuple[Path, bool]:
        wt = payload.get("worktree")
        if self.profile == "code" and wt and wt.get("branch_hint"):
            path, created = await asyncio.to_thread(self.worktrees.prepare, convo, wt["project"], wt["branch_hint"],
                                                    wt["default_branch"])
            if created:
                for cmd in wt.get("setup") or []:
                    code, out = await run_shell(cmd, path, SETUP_TIMEOUT_S)
                    if code:
                        log.warning("setup %r failed in %s: %s", cmd, path, out[-500:])
            return path, True
        scratch = self.scratch_root / convo
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch, False

    async def _heartbeat(self, tok: str, item_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                cancel = await self.link.heartbeat(tok)
            except SessionRevoked:
                cancel = True
            except Exception:
                continue
            if cancel:
                await self.claude.interrupt(item_id)
                return

    def _forwarder(self, tok: str):
        async def forward(record: dict) -> None:
            with contextlib.suppress(Exception):
                text = json.dumps(record, default=str)
                await self.link.event(tok, record if len(text) <= 20000 else {"type": record.get("type"),
                                                                              "truncated": text[:20000]})
        return forward

    async def _run(self, spec: RunSpec) -> RunResult:
        try:
            return await self.claude.run(spec)
        except Exception as e:
            log.exception("session %s failed", spec.run_id)
            return RunResult(spec.run_id, None, "error", error=f"{type(e).__name__}: {e}")

    async def _run_or_start_fresh(self, spec: RunSpec) -> RunResult:
        result = await self._run(spec)
        if result.outcome == "error" and spec.resume_session_id and not result.turns:
            # The saved session is gone (the sandbox was rebuilt, or its files were lost); the thread still has
            # the history, so start a fresh session that reads it
            log.warning("resuming %s failed (%s); starting a fresh session", spec.resume_session_id, result.error)
            result = await self._run(dataclasses.replace(spec, resume_session_id=None, prompt=FRESH_NOTE + spec.prompt))
        return result

    async def handle(self, item: dict, tok: str, manifest: list[dict]) -> None:
        payload, item_id = item["payload"], item["id"]
        convo = item.get("conversation_id") or item_id
        self.users.acquire(convo)
        try:
            try:
                spec, sink = await self._prepare(item, tok, manifest, convo)
            except Exception as e:
                log.exception("preparing %s failed", item_id)
                with contextlib.suppress(SessionRevoked):
                    await self.link.result(tok, {"outcome": "error", "text": "", "session_id": None, "turns": 0,
                                                 "error": f"preparing the session failed: {type(e).__name__}: {e}"})
                return
            beat = asyncio.create_task(self._heartbeat(tok, item_id))
            try:
                result = await self._run_or_start_fresh(spec)
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
            out = {"outcome": result.outcome, "text": result.summary, "session_id": result.session_id,
                   "turns": result.turns, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                   "cache_read_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens,
                   "cost_usd_est": result.cost_usd_est, "account_code": result.account_code, "forks": result.forks,
                   "error": result.error}
            if sink is not None:
                out["structured"] = sink.get("output")
            with contextlib.suppress(SessionRevoked):
                await self.link.result(tok, out)
        finally:
            self.users.release(convo)

    async def _prepare(self, item: dict, tok: str, manifest: list[dict], convo: str) -> tuple[RunSpec, dict | None]:
        payload, item_id = item["payload"], item["id"]
        plugins = await self._bundle(payload.get("project"))
        cwd, is_worktree = await self._workdir(convo, payload)
        sessions = self.sessions_root / convo
        sessions.mkdir(parents=True, exist_ok=True)
        sink = {} if payload.get("structured") else None
        wt = payload.get("worktree") or {}
        tools = remote_sdk_tools(manifest, self.link, tok, worktree=cwd, checks=list(wt.get("checks") or []),
                                 branch=wt.get("branch_hint") if is_worktree else None, worktrees=self.worktrees,
                                 agent_name=self.agent_name, structured_sink=sink,
                                 structured_schema=payload.get("structured"))
        env = {**self.extra_env, **read_env_files(self.env_files)}
        spec = RunSpec(run_id=item_id, prompt=payload["text"], cwd=cwd, model=payload["model"],
                       system_prompt=payload["system_prompt"], max_turns=payload["max_turns"],
                       timeout_s=payload["timeout_s"],
                       env={"CLAUDE_CONFIG_DIR": str(sessions), **env, **(payload.get("test_secrets") or {})},
                       mcp_servers=expand_mcp(payload.get("mcp") or {}, env),
                       plugins=plugins, sdk_tools=tools, resume_session_id=payload.get("resume_session_id"),
                       on_message=self._forwarder(tok))
        return spec, sink

    async def run_forever(self) -> None:
        running: set[asyncio.Task] = set()
        backoff = 1.0
        while True:
            if len(running) >= self.max_parallel:
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                got = await self.link.pull(self.sandbox)
                backoff = 1.0
            except SessionRevoked:
                # Our sandbox token expired or was replaced: exit, and the next reconcile starts a runner with the
                # new one
                log.error("core no longer accepts this sandbox's token; exiting")
                if running:
                    await asyncio.wait(running)
                return
            except Exception as e:
                log.warning("pull failed (%s); retrying in %.0f s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if got is None:
                continue
            task = asyncio.create_task(self.handle(got["work_item"], got["session_token"], got.get("tools") or []))
            running.add(task)
            task.add_done_callback(running.discard)
