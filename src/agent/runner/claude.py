"""ClaudeRunner: one run = one Agent SDK session, through the host's account proxy.

The proxy pins each session to one account and returns that account's limits to it. When the account is rejected
or disabled (seen by the watchdog, or as a 403/503, or a 429 while the proxy reports the account rejected), the run
is resumed as a fork: a new session id with the conversation intact, which the proxy pins to a fresh account.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, create_sdk_mcp_server

from agent.proxy import PLACEHOLDER_TOKEN, ProxyClient, ProxyUnavailable
from agent.runner.base import RunResult, RunSpec
from agent.runner.outcome import ACCOUNT_TROUBLE, FORK_STATUSES, map_result, usage_tokens

# The SDK hands the parent's whole environment to the CLI, so anything secret-looking is overridden with "".
SECRETISH = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|_KEY)$|^(GH|GITHUB|SLACK|LINEAR|AWS)_", re.I)
PROXY_VARS = {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"}


@dataclass
class _Attempt:
    result: RunResult
    fork_wanted: bool


class ClaudeRunner:
    def __init__(self, proxy: ProxyClient, base_url: str, transcripts_dir: Path, *,
                 client_factory: Callable | None = None, watchdog_s: float = 60,
                 uuid_factory: Callable[[], str] | None = None, max_forks: int = 2):
        self.proxy, self.base_url = proxy, base_url
        self.transcripts_dir = Path(transcripts_dir)
        self.client_factory = client_factory or (lambda options: ClaudeSDKClient(options=options))
        self.watchdog_s, self.max_forks = watchdog_s, max_forks
        self.uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self._active: dict[str, tuple[object, dict]] = {}
        self._stopped: set[str] = set()

    def _env(self, spec: RunSpec) -> dict[str, str]:
        redacted = {k: "" for k in os.environ if SECRETISH.search(k) and k not in PROXY_VARS}
        # The proxy signs each session in as some teammate's seat, so that account's claude.ai connectors (their
        # mail, their drive) aren't the agent's; the MCP servers the agent adds itself still load
        return {**redacted, **spec.env, "ANTHROPIC_BASE_URL": self.base_url,
                "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER_TOKEN, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

    def build_options(self, spec: RunSpec, *, session_id: str | None, resume: str | None = None,
                      fork: bool = False) -> ClaudeAgentOptions:
        tools = [*spec.sdk_tools]
        servers = {**spec.mcp_servers, **({"agent": create_sdk_mcp_server("agent", tools=tools)} if tools else {})}
        return ClaudeAgentOptions(
            model=spec.model, max_turns=spec.max_turns, cwd=str(spec.cwd),
            system_prompt=spec.system_prompt, env=self._env(spec),
            # "user" is the agent's own Claude home (CLAUDE_CONFIG_DIR): the skills, plugins and MCP servers it adds
            setting_sources=["user", "project"], plugins=[{"type": "local", "path": str(p)} for p in spec.plugins],
            mcp_servers=servers, can_use_tool=None, permission_mode="bypassPermissions",
            session_id=session_id, resume=resume, fork_session=fork,
            settings=json.dumps(spec.settings) if spec.settings else None)

    async def interrupt(self, run_id: str) -> None:
        self._stopped.add(run_id)
        active = self._active.get(run_id)
        if active:
            client, state = active
            state["reason"] = state["reason"] or "external"
            await client.interrupt()

    async def run(self, spec: RunSpec) -> RunResult:
        forks, resume, fork = 0, spec.resume_session_id, False
        if spec.run_id in self._stopped:  # cancelled before (or between) its sessions: don't start another
            return RunResult(run_id=spec.run_id, session_id=resume, outcome="interrupted", error="interrupted")
        while True:
            attempt = await self._attempt(spec, resume=resume, fork=fork)
            if attempt.fork_wanted and forks < self.max_forks and spec.run_id not in self._stopped:
                forks, resume, fork = forks + 1, attempt.result.session_id, True
                continue
            attempt.result.forks = forks
            return attempt.result

    async def _watch(self, client, state: dict) -> None:
        while True:
            await asyncio.sleep(self.watchdog_s)
            if not state.get("account"):
                continue
            try:
                status = (await self.proxy.usage()).status_of(state["account"])
            except ProxyUnavailable:
                continue
            if status in ACCOUNT_TROUBLE:
                state["reason"], state["trouble"] = "watchdog", status
                await client.interrupt()
                return

    async def _account_status(self, state: dict) -> str | None:
        try:
            if not state.get("account"):
                state["account"] = await self.proxy.whoami(state["sid"])
            return (await self.proxy.usage()).status_of(state["account"])
        except ProxyUnavailable:
            return None

    def _append(self, path: Path, msg) -> dict:
        record = {"type": type(msg).__name__,
                  **(dataclasses.asdict(msg) if dataclasses.is_dataclass(msg) else {"repr": repr(msg)})}
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record

    async def _attempt(self, spec: RunSpec, *, resume: str | None, fork: bool) -> _Attempt:
        session_id = None if (resume and not fork) else self.uuid_factory()
        client = self.client_factory(self.build_options(spec, session_id=session_id, resume=resume, fork=fork))
        state = {"reason": None, "account": None, "sid": session_id or resume, "asked": False}
        self._active[spec.run_id] = (client, state)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        transcript = self.transcripts_dir / f"{spec.run_id}.jsonl"
        final: ResultMessage | None = None
        watchdog = asyncio.create_task(self._watch(client, state))
        try:
            async with asyncio.timeout(spec.timeout_s):
                await client.connect()
                await client.query(spec.prompt)
                async for msg in client.receive_response():
                    record = self._append(transcript, msg)
                    if spec.on_message is not None:
                        with contextlib.suppress(Exception):
                            await spec.on_message(json.loads(json.dumps(record, default=str)))
                    if getattr(msg, "session_id", None):
                        state["sid"] = msg.session_id
                    if isinstance(msg, AssistantMessage) and not state["asked"]:
                        state["asked"] = True
                        with contextlib.suppress(ProxyUnavailable):
                            state["account"] = await self.proxy.whoami(state["sid"])
                    if isinstance(msg, ResultMessage):
                        final = msg
        except TimeoutError:
            state["reason"] = "timeout"
            await client.interrupt()
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._active.pop(spec.run_id, None)

        result = RunResult(run_id=spec.run_id, session_id=state["sid"], outcome="error",
                           account_code=state["account"])
        fork_wanted = False
        if final is not None:
            result.turns = final.num_turns
            (result.input_tokens, result.output_tokens, result.cache_read_tokens,
             result.cache_write_tokens) = usage_tokens(final.usage)
            result.cost_usd_est = final.total_cost_usd or 0.0
            result.summary = final.result or ""
        if state["reason"] == "timeout":
            result.outcome, result.error = "timeout", f"no result within {spec.timeout_s} s"
        elif state["reason"] == "external":
            result.outcome, result.error = "interrupted", "interrupted"
        elif state["reason"] == "watchdog":
            result.error = f"account {state['account']} is {state.get('trouble')}"
            fork_wanted = True
        elif final is None:
            result.error = "the session ended without a result"
        else:
            result.outcome, result.error = map_result(final)
            if result.outcome == "error":
                if final.api_error_status in FORK_STATUSES:
                    fork_wanted = True
                elif final.api_error_status == 429:
                    fork_wanted = await self._account_status(state) in ACCOUNT_TROUBLE
        return _Attempt(result, fork_wanted)
