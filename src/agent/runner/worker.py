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
from agent.runner.handoff import handoff_note
from agent.runner.link import SessionRevoked
from agent.runner.tools import remote_sdk_tools

log = logging.getLogger(__name__)
FRESH_NOTE = ("(Your earlier session in this conversation couldn't be resumed. Call read_thread to catch up on "
              "the conversation so far before you answer.)\n\n")
MIDTURN_NOTE = ("(A new message arrived while you're working. Answer it now with post_update, or a Linear thought in a "
                "Linear session, because your final reply only posts when this whole turn ends. Then carry on, and "
                "change course if it asks you to.)\n\n")
RESTARTED_NOTE = ("(Your machine restarted partway through this turn. Your session picks up where it stopped: check "
                  "what you had already done, then carry on. Background agents or tasks you had running were stopped "
                  "by the restart: relaunch the ones you still need and tell the thread they were restarted. The "
                  "message you were working on:)\n\n")
MEMORY_RULES = """# Memory

Your memory is the folder {dir}, shared by all your sessions. Its index, MEMORY.md, is below; read a fact's file when its line matters to what you're doing.

- One fact per file, named for it (`people-ivan-replies.md`, `project-roster-demo.md`), starting with:
  ---
  name: <short-kebab-name>
  description: <one line: what it is and when it matters>
  ---
  then the fact, with who said it, when and the link.
- Add one line for it to MEMORY.md: `- [Title](file.md) — hook`. The index holds pointers, never the facts.
- Before you save, look for a file that already covers it and update that one. When a fact turns out wrong, fix or delete its file and its line.
- Save what you'd want next time: what people want and how, decisions, conventions, gotchas, where things are. Not what the code, git or the task record already says.
- Never write what you learn into CLAUDE.md.

## MEMORY.md
"""

_VAR = re.compile(r"\$\{(\w+)\}")


def origin_of(payload: dict) -> str:
    """The prompt's first line, who sent the message, where and its link, or "" for a turn without one."""
    return f"{payload['origin']}\n" if payload.get("origin") else ""


SLACK_FILES_DIR = "slack-files"  # in the session folder: each attachment at slack-files/<file id>/<name>


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


async def save_slack_files(link, tok: str, session: Path, files: list[dict]) -> str:
    """Save a message's attachments into the session folder and return the note that tells the session where they
    are ("" when the message has none). A file that can't be fetched is named in the note with the reason, so the
    session can say so rather than answer as if it had seen it."""
    if not files:
        return ""
    lines = []
    for f in files:
        file_id = str(f.get("id") or "")
        name = Path(str(f.get("name") or "")).name or file_id  # never a path out of its folder
        if not file_id or "/" in file_id or file_id in (".", ".."):
            continue
        kind = ", ".join(p for p in (f.get("mimetype") or "", _size(int(f.get("size") or 0)) if f.get("size") else "")
                         if p)
        path = session / SLACK_FILES_DIR / file_id / name
        try:
            if not path.is_file():  # already saved when a restarted turn runs again
                data = await link.slack_file(tok, file_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            lines.append(f"- {path}" + (f" ({kind})" if kind else ""))
        except SessionRevoked:
            raise
        except Exception as e:
            log.warning("couldn't fetch Slack file %s", file_id, exc_info=True)
            lines.append(f"- {name}: couldn't be fetched ({type(e).__name__}: {e})")
    if not lines:
        return ""
    return ("\n\n(The message came with these files attached, saved in your session folder. Read them before you "
            "answer:\n" + "\n".join(lines) + ")")


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


def memory_block(memory_dir: Path, max_lines: int = 200) -> str:
    """MEMORY_RULES with the folder filled in, followed by its index (memory_dir/MEMORY.md) cut to max_lines."""
    try:
        # errors="replace": a concurrent write can leave invalid UTF-8 mid-file; keep the readable lines rather
        # than raising and failing every session's turn until someone repairs the file.
        lines = (memory_dir / "MEMORY.md").read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    if not lines:
        index = "(empty)"
    else:
        index = "\n".join(lines[:max_lines])
        hidden = len(lines) - max_lines
        if hidden > 0:
            index += f"\n(… {hidden} more lines not shown: the index is too long, so tidy it)"
    return MEMORY_RULES.format(dir=memory_dir) + index


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
        self.memory_dir = memory_dir  # one folder of fact files for every session; see memory_block

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

    async def _heartbeat(self, tok: str, item_id: str, convo: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                beat = {"session_id": self.claude.session_of(item_id), "account": self.claude.account_of(item_id),
                        "read": self.claude.read_of(item_id), "accepts_messages": self.claude.accepting(item_id)}
                reply = await self.link.heartbeat(tok, beat)
            except SessionRevoked:
                reply = {"cancel": True}
            except Exception:
                continue
            if reply.get("cancel"):
                await self.claude.interrupt(item_id)
                return
            for m in reply.get("messages") or []:
                try:
                    attached = await save_slack_files(self.link, tok, self.scratch_root / convo,
                                                      m["payload"].get("files") or [])
                except SessionRevoked:
                    await self.claude.interrupt(item_id)
                    return
                await self.claude.send(item_id, m["id"],
                                       origin_of(m["payload"]) + MIDTURN_NOTE + m["payload"]["text"] + attached)

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

    def _handoff_prompt(self, prompt: str, origin: str, earlier: str, transcript: Path | None = None) -> str:
        """The prompt for a fresh session that picks up from the earlier one: the origin line stays first, the
        handoff note after it. A restart note is dropped: the fresh session doesn't pick up where it stopped."""
        text = prompt.removeprefix(origin).removeprefix(RESTARTED_NOTE)
        return origin + handoff_note(earlier, self.claude_home, transcript) + text

    async def _run_or_start_fresh(self, spec: RunSpec, origin: str) -> RunResult:
        earlier = spec.resume_session_id
        if earlier and not await self._can_resume(earlier, spec.resume_account):
            # Resuming would put the session on another account (or on one we can't confirm): never do that. A fresh
            # session starts with a short digest of the earlier one instead, never its full history.
            log.info("not resuming %s: it last ran on %s and the proxy won't keep it there; starting a fresh session",
                     earlier, spec.resume_account or "an unknown account")
            spec = dataclasses.replace(spec, resume_session_id=None, prompt=spec.handoff(earlier, None))
        result = await self._run(spec)
        if result.outcome == "error" and spec.resume_session_id and not result.turns:
            # The saved session is gone (the agent container was rebuilt, or its files were lost); the thread has
            # the history, so start a fresh session that reads it. The origin line stays first, the note after it.
            # A restart note, if this attempt had one, is dropped: "your session picks up where it stopped" would
            # contradict "your earlier session couldn't be resumed" right above it.
            log.warning("resuming %s failed (%s); starting a fresh session", spec.resume_session_id, result.error)
            text = spec.prompt.removeprefix(origin).removeprefix(RESTARTED_NOTE)
            prompt = origin + FRESH_NOTE + text
            result = await self._run(dataclasses.replace(spec, resume_session_id=None, prompt=prompt))
        return result

    async def _can_resume(self, session_id: str, account: str | None) -> bool:
        try:
            return await self.claude.can_resume(session_id, account)
        except Exception:
            log.exception("couldn't check where %s would resume", session_id)
            return False

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
        beat = asyncio.create_task(self._heartbeat(tok, item_id, convo))
        try:
            result = await self._run_or_start_fresh(spec, origin_of(item["payload"]))
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            self.claude.release(item_id)  # a stop belongs to this claim; a later claim of the item runs afresh
        out = {"outcome": result.outcome, "text": result.summary, "session_id": result.session_id,
               "turns": result.turns, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
               "cache_read_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens,
               "cost_usd_est": result.cost_usd_est, "account_code": result.account_code, "forks": result.forks,
               "error": result.error, "read": result.read, "lost_tasks": result.lost_tasks}
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
        system_prompt = payload["system_prompt"]
        if self.memory_dir is not None:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            settings["autoMemoryDirectory"] = str(self.memory_dir)
            system_prompt += "\n\n" + memory_block(self.memory_dir)
        # the message's origin (who, where, link) comes first, then the restart note (if this attempt is resuming
        # a turn the machine restarted mid-way through), then the message
        note = RESTARTED_NOTE if item.get("attempts") and payload.get("resume_session_id") else ""
        attached = await save_slack_files(self.link, tok, session, payload.get("files") or [])
        origin, prompt = origin_of(payload), origin_of(payload) + note + payload["text"] + attached
        spec = RunSpec(run_id=item_id, prompt=prompt, cwd=session,
                       model=payload["model"], system_prompt=system_prompt, max_turns=payload["max_turns"],
                       timeout_s=payload["timeout_s"],
                       # CLAUDE_CONFIG_DIR and the function-hooks flag (which loads the effortless plugin's code,
                       # plugins/effortless/hooks) are forced last, so a pack's test secret or the container's
                       # extra env can never override them
                       env={**env, **(payload.get("test_secrets") or {}), "CLAUDE_CONFIG_DIR": str(self.claude_home),
                            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"},
                       mcp_servers=expand_mcp(payload.get("mcp") or {}, env),
                       plugins=plugins, sdk_tools=tools, settings=settings,
                       resume_session_id=payload.get("resume_session_id"),
                       resume_account=payload.get("resume_account"), on_message=self._forwarder(tok),
                       handoff=lambda earlier, transcript: self._handoff_prompt(prompt, origin, earlier, transcript))
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
