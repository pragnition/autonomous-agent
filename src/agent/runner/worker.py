"""The runner inside the agent container: pull work from core, run a Claude Code session with full permissions in
the conversation's own folder, and report the result. Limits are enforced by core's tools, not the OS."""

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
from agent.runner.link import SessionRevoked
from agent.runner.tools import remote_sdk_tools

log = logging.getLogger(__name__)
FRESH_NOTE = ("(Your earlier session in this conversation couldn't be resumed. Call read_thread to catch up on "
              "the conversation so far before you answer.)\n\n")

_VAR = re.compile(r"\$\{(\w+)\}")


def origin_of(payload: dict) -> str:
    """The prompt's first line, who sent the message, where and its link, or "" for a turn without one."""
    return f"{payload['origin']}\n" if payload.get("origin") else ""


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


class Runner:
    def __init__(self, link, claude, *, bundles_dir: Path, claude_home: Path, scratch_root: Path,
                 heartbeat_s: float = 15, max_parallel: int = 32, extra_env: dict[str, str] | None = None,
                 env_files: list[Path] = (), memory_dir: Path | None = None):
        self.link, self.claude = link, claude
        self.bundles_dir, self.scratch_root = Path(bundles_dir), Path(scratch_root)
        # the agent's own Claude home, shared by every session: the skills, plugins and MCP servers it installs stay
        self.claude_home = Path(claude_home)
        self.heartbeat_s, self.max_parallel = heartbeat_s, max_parallel  # core's Gate is the real limit; this is a safety bound
        self.extra_env = dict(extra_env or {})  # agent.env and a git identity for every session
        self.env_files = [Path(f) for f in env_files]  # re-read per session: core rotates LINEAR_APP_TOKEN there
        self.memory_dir = memory_dir  # Claude Code's auto memory, one folder for every session

    async def _bundle(self) -> list[Path]:
        version, data = await self.link.bundle()
        dest = self.bundles_dir / version
        if not dest.exists():
            tmp = self.bundles_dir / f".{version}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            tmp.rename(dest)
        return [dest / "plugins" / "core", dest / "plugins" / "effortless",
                *sorted((dest / "plugins" / "packs").glob("*"))]

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

    async def _run_or_start_fresh(self, spec: RunSpec, origin: str) -> RunResult:
        result = await self._run(spec)
        if result.outcome == "error" and spec.resume_session_id and not result.turns:
            # The saved session is gone (the agent container was rebuilt, or its files were lost); the thread has
            # the history, so start a fresh session that reads it. The origin line stays first, the note after it.
            log.warning("resuming %s failed (%s); starting a fresh session", spec.resume_session_id, result.error)
            prompt = origin + FRESH_NOTE + spec.prompt.removeprefix(origin)
            result = await self._run(dataclasses.replace(spec, resume_session_id=None, prompt=prompt))
        return result

    async def handle(self, item: dict, tok: str, manifest: list[dict]) -> None:
        item_id = item["id"]
        convo = item.get("conversation_id") or item_id
        try:
            spec = await self._prepare(item, tok, manifest, convo)
        except Exception as e:
            log.exception("preparing %s failed", item_id)
            with contextlib.suppress(SessionRevoked):
                await self.link.result(tok, {"outcome": "error", "text": "", "session_id": None, "turns": 0,
                                             "error": f"preparing the session failed: {type(e).__name__}: {e}"})
            return
        beat = asyncio.create_task(self._heartbeat(tok, item_id))
        try:
            result = await self._run_or_start_fresh(spec, origin_of(item["payload"]))
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        out = {"outcome": result.outcome, "text": result.summary, "session_id": result.session_id,
               "turns": result.turns, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
               "cache_read_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens,
               "cost_usd_est": result.cost_usd_est, "account_code": result.account_code, "forks": result.forks,
               "error": result.error}
        with contextlib.suppress(SessionRevoked):
            await self.link.result(tok, out)

    async def _prepare(self, item: dict, tok: str, manifest: list[dict], convo: str) -> RunSpec:
        payload, item_id = item["payload"], item["id"]
        plugins = await self._bundle()
        session = self.scratch_root / convo  # the conversation's own folder, the same every turn; its clones live here
        session.mkdir(parents=True, exist_ok=True)
        self.claude_home.mkdir(parents=True, exist_ok=True)
        tools = remote_sdk_tools(manifest, self.link, tok, session_dir=session)
        env = {**self.extra_env, **read_env_files(self.env_files)}
        settings = {}
        if self.memory_dir is not None:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            settings["autoMemoryDirectory"] = str(self.memory_dir)
        # the message's origin (who, where, link) comes first, then the message
        spec = RunSpec(run_id=item_id, prompt=origin_of(payload) + payload["text"], cwd=session,
                       model=payload["model"], system_prompt=payload["system_prompt"], max_turns=payload["max_turns"],
                       timeout_s=payload["timeout_s"],
                       # CLAUDE_CONFIG_DIR and the function-hooks flag (which loads the effortless plugin's code,
                       # plugins/effortless/hooks) are forced last, so a pack's test secret or the container's
                       # extra env can never override them
                       env={**env, **(payload.get("test_secrets") or {}), "CLAUDE_CONFIG_DIR": str(self.claude_home),
                            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"},
                       mcp_servers=expand_mcp(payload.get("mcp") or {}, env),
                       plugins=plugins, sdk_tools=tools, settings=settings,
                       resume_session_id=payload.get("resume_session_id"), on_message=self._forwarder(tok))
        return spec

    async def run_forever(self) -> None:
        running: set[asyncio.Task] = set()
        backoff = 1.0
        while True:
            if len(running) >= self.max_parallel:
                await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                got = await self.link.pull()
                backoff = 1.0
            except SessionRevoked:
                # The runner token expired or was replaced: exit, and the process starts a runner on the new one
                log.error("core refused the runner token; exiting")
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
