"""ClaudeRunner: one run = one Agent SDK session, through the host's account proxy.

The proxy pins each session to one account and returns that account's limits to it. When the account is rejected
or disabled (seen by the watchdog, or as a 403/503, or a 429 while the proxy reports the account rejected), the run
moves to a fresh session (new id, empty history), which the proxy pins to another account, with a note naming the
earlier session and where its record is. The watchdog does the same when the account goes over its soft cap on the
5-hour window (proxy.SoftCap), but only while the proxy's top-ranked account is another one under its own cap and the
run has a fork left; otherwise the run carries on where it is. A session is never resumed on a different account
from the one it ran on: not here, and not on a later turn (can_resume).

Each attempt's prompt, and every message that arrives while the run is going, is written into the live session with its
own uuid. The CLI echoes every message it reads, so a run knows which ones were read, and it reads on past a result
until each one has been: a result from before the prompt was read (a background task from an earlier turn finishing)
doesn't answer this turn. It also reads on past a result while the session has background agents running: that turn
end is interim, and each agent wakes the session again when it finishes. A stop then (a cancel, stop_all, a handoff
off an account over its cap) ends the run at once, since no turn is running to send a result. A Bash call that only
waits is turned away (idle_wait), since nothing sent into the session is read until the call returns.

A run has no time or step limit of its own. A session that sends nothing for `idle_s` is stuck, and is stopped as
stalled. A run with a time budget (`timeout_s`, which a scheduled run has) is stopped when it runs out, however busy
it is; the budget covers the whole run, every session it moves through.
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

from claude_agent_sdk import (TERMINAL_TASK_STATUSES, AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              HookMatcher, ResultMessage, SystemMessage, UserMessage, create_sdk_mcp_server)
# The task types the SDK keeps the CLI's stdin open for past a result (it doesn't export the set)
from claude_agent_sdk._internal.query import DEFERRING_TASK_TYPES

from agent.proxy import PLACEHOLDER_TOKEN, ProxyClient, ProxyUnavailable
from agent.runner.base import RunResult, RunSpec
from agent.runner.handoff import handoff_note
from agent.runner.idle_wait import block_idle_waits
from agent.runner.outcome import ACCOUNT_TROUBLE, FORK_STATUSES, map_result, usage_tokens

# The SDK hands the parent's whole environment to the CLI, so anything secret-looking is overridden with "".
SECRETISH = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|_KEY)$|^(GH|GITHUB|SLACK|LINEAR|AWS)_", re.I)
PROXY_VARS = {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"}
# The most the SDK reads of one message from the CLI. Its default, 1 MiB, ends the session on an image of ~750 KB
# (a tool result carries it as base64, a third larger): a Slack screenshot or a full-page Playwright one.
MAX_BUFFER_SIZE = 20 * 1024 * 1024
# The background tasks a run stays open for: the SDK's own sets. Delegated agent work (local agents and workflows),
# which finishes by itself and wakes the session when it does; not background shells or monitors (a dev server runs
# until it's stopped) and not remote agents, which can be long-running monitors the CLI won't wait on either. A task
# has ended on a terminal status from a task_notification ("stopped") or a task_updated ("killed").
WAITED_TASK_TYPES, TASK_ENDED = DEFERRING_TASK_TYPES, TERMINAL_TASK_STATUSES


def track_task(tasks: dict[str, str], msg: SystemMessage) -> None:
    """Keep tasks (task id -> description) to the agent tasks still running, from the CLI's task frames, read the way
    the SDK's own ledger reads them: task_started adds one of WAITED_TASK_TYPES; any task_notification, or a
    task_updated whose patch has a terminal status, ends it (not every task that ends sends a notification)."""
    data = msg.data if isinstance(msg.data, dict) else {}
    task_id = data.get("task_id")
    if not task_id:
        return
    if msg.subtype == "task_started":
        if data.get("task_type") in WAITED_TASK_TYPES:
            tasks[task_id] = data.get("description") or task_id
    elif msg.subtype == "task_notification":
        tasks.pop(task_id, None)
    elif msg.subtype == "task_updated":
        patch = data.get("patch")
        if isinstance(patch, dict) and patch.get("status") in TASK_ENDED:
            tasks.pop(task_id, None)


async def _user_message(message_uuid: str, text: str):
    """One stream-json user message; the CLI echoes it back with this uuid once it reads it."""
    yield {"type": "user", "uuid": message_uuid, "message": {"role": "user", "content": text},
           "parent_tool_use_id": None}


@dataclass
class _Attempt:
    result: RunResult
    switch_wanted: bool  # the account is in trouble: carry on in a fresh session on another one


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
        self._read: dict[str, list[str]] = {}  # per run: the ids of the messages its sessions read, in order

    def _env(self, spec: RunSpec) -> dict[str, str]:
        redacted = {k: "" for k in os.environ if SECRETISH.search(k) and k not in PROXY_VARS}
        # The proxy signs each session in as some teammate's seat, so that account's claude.ai connectors (their
        # mail, their drive) aren't the agent's; the MCP servers the agent adds itself still load
        return {**redacted, **spec.env, "ANTHROPIC_BASE_URL": self.base_url,
                "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER_TOKEN, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

    def build_options(self, spec: RunSpec, *, session_id: str | None, resume: str | None = None) -> ClaudeAgentOptions:
        tools = [*spec.sdk_tools]
        servers = {**spec.mcp_servers, **({"agent": create_sdk_mcp_server("agent", tools=tools)} if tools else {})}
        return ClaudeAgentOptions(
            model=spec.model, cwd=str(spec.cwd),
            system_prompt=spec.system_prompt, env=self._env(spec),
            # "user" is the agent's own Claude home (CLAUDE_CONFIG_DIR): the skills, plugins and MCP servers it adds
            setting_sources=["user", "project"], plugins=[{"type": "local", "path": str(p)} for p in spec.plugins],
            mcp_servers=servers, can_use_tool=None, permission_mode="bypassPermissions",
            session_id=session_id, resume=resume,
            settings=json.dumps(spec.settings) if spec.settings else None, max_buffer_size=MAX_BUFFER_SIZE,
            # a foreground Bash call that only waits holds the turn, and no message reaches the session meanwhile
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[block_idle_waits])]},
            # the CLI echoes each user message it reads, with the uuid it was sent with
            extra_args={"replay-user-messages": None})

    def accepting(self, run_id: str) -> bool:
        """True while an attempt of the run is reading and hasn't finished or been told to stop."""
        active = self._active.get(run_id)
        return bool(active and active[1]["accepting"] and not active[1]["reason"])

    def session_of(self, run_id: str) -> str | None:
        active = self._active.get(run_id)
        return active[1]["sid"] if active else None

    def account_of(self, run_id: str) -> str | None:
        """The account the run's live session is on, once the proxy has said."""
        active = self._active.get(run_id)
        return active[1]["account"] if active else None

    async def can_resume(self, session_id: str, account: str | None) -> bool:
        """Whether resuming the session keeps it on the account it last ran on: the proxy has it pinned to that
        account, and that account isn't rejected or disabled (the proxy would move it). Anything not known, the
        last account, the pin, or the proxy itself, means no: a fresh session is the safe side."""
        if not account:
            return False
        try:
            pinned = await self.proxy.whoami(session_id)
            if pinned != account:
                return False
            return (await self.proxy.usage()).status_of(pinned) not in ACCOUNT_TROUBLE
        except ProxyUnavailable:
            return False

    def _handoff(self, spec: RunSpec, earlier: str, transcript: Path) -> str:
        if spec.handoff is not None:
            return spec.handoff(earlier, transcript)
        return handoff_note(earlier, spec.env.get("CLAUDE_CONFIG_DIR"), transcript) + spec.prompt

    def read_of(self, run_id: str) -> list[str]:
        return list(self._read.get(run_id, []))

    async def send(self, run_id: str, item_id: str, text: str) -> bool:
        """Write a message into the run's live session. It counts as read once the CLI echoes it back."""
        if not self.accepting(run_id):
            return False
        client, state = self._active[run_id]
        message_uuid = self.uuid_factory()
        # Pending from before the write, with no await since the check: the receive loop either sees it at its next
        # result and reads on, or had already stopped accepting
        state["pending"][message_uuid] = item_id
        try:
            await client.query(_user_message(message_uuid, text))
        except Exception:
            state["pending"].pop(message_uuid, None)
            return False
        return True

    async def interrupt(self, run_id: str) -> None:
        self._stopped.add(run_id)
        active = self._active.get(run_id)
        if active:
            client, state = active
            state["reason"] = state["reason"] or "external"
            self._end_between_turns(state)
            await client.interrupt()

    def release(self, run_id: str) -> None:
        """Forget a stop once the claim it came with is over: core may hand the same item to this runner again."""
        self._stopped.discard(run_id)

    async def run(self, spec: RunSpec) -> RunResult:
        forks, resume, prompt = 0, spec.resume_session_id, spec.prompt
        if spec.run_id in self._stopped:  # cancelled before (or between) its sessions: don't start another
            return RunResult(run_id=spec.run_id, session_id=resume, outcome="interrupted", error="interrupted")
        self._read[spec.run_id] = []
        end = asyncio.get_running_loop().time() + spec.timeout_s if spec.timeout_s else None
        try:
            while True:
                attempt = await self._attempt(spec, resume=resume, prompt=prompt, can_fork=forks < self.max_forks,
                                              end=end)
                if attempt.switch_wanted and forks < self.max_forks and spec.run_id not in self._stopped:
                    # Never resume (or fork) the session onto another account: start a fresh one, which the proxy
                    # pins to a working account, and tell it which session to pick up from
                    earlier = attempt.result.session_id or resume
                    forks, resume = forks + 1, None
                    if earlier:
                        prompt = self._handoff(spec, earlier, self.transcripts_dir / f"{spec.run_id}.jsonl")
                    continue
                attempt.result.forks, attempt.result.read = forks, self._read[spec.run_id]
                return attempt.result
        finally:
            self._read.pop(spec.run_id, None)

    async def _watch(self, client, state: dict) -> None:
        while True:
            await asyncio.sleep(self.watchdog_s)
            if not state.get("account"):
                continue
            try:
                usage = await self.proxy.usage()
            except ProxyUnavailable:
                continue
            status = usage.status_of(state["account"])
            if status in ACCOUNT_TROUBLE:
                state["reason"], state["trouble"] = "watchdog", status
                self._end_between_turns(state)
                await client.interrupt()
                return
            # Over the soft cap: hand off like a rejected account, but only when the fresh session would land on an
            # account under its cap and the run may still fork; otherwise keep going here rather than end the run
            if state["can_fork"] and usage.over_cap(state["account"]) and usage.handoff_target(state["account"]):
                state["reason"], state["trouble"] = "watchdog", "over its 5-hour soft cap"
                self._end_between_turns(state)
                await client.interrupt()
                return

    @staticmethod
    def _end_between_turns(state: dict) -> None:
        """A stop ends the run at the result the CLI sends for the turn it interrupts. Between turns (the session
        waiting on its background agents) there is no turn to interrupt and no result coming, so the attempt's window
        closes now instead (see deadline in _attempt)."""
        window = state.get("window")
        if window is not None and not state["turn"]:
            with contextlib.suppress(RuntimeError):  # the window has already closed: the attempt is ending anyway
                window.reschedule(asyncio.get_running_loop().time())

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

    async def _attempt(self, spec: RunSpec, *, resume: str | None, prompt: str, can_fork: bool = True,
                       end: float | None = None) -> _Attempt:
        """One session of the run. `end` is the loop time the run's time budget runs out at, None when it has none."""
        loop = asyncio.get_running_loop()

        def deadline() -> float:  # the idle window from now, cut short by the run's time budget
            if state["reason"] and not state["turn"]:  # stopped between turns: no result is coming (_end_between_turns)
                return loop.time()
            idle = loop.time() + spec.idle_s
            return idle if end is None else min(idle, end)

        session_id = None if resume else self.uuid_factory()
        prompt_uuid = self.uuid_factory()
        client = self.client_factory(self.build_options(spec, session_id=session_id, resume=resume))
        # pending: the uuid of each message sent into this session and not yet echoed -> its item id (None for the
        # attempt's own prompt, which is no work item)
        # tasks: the background agents the session has running (id -> description); a result while any is running is
        # an interim end of turn. turn: a turn is under way (from its prompt or its first main-thread message to its
        # result), so a stop gets a result to end at; between turns it doesn't (_end_between_turns)
        state = {"reason": None, "account": None, "sid": session_id or resume, "asked": False, "can_fork": can_fork,
                 "accepting": False, "pending": {prompt_uuid: None}, "tasks": {}, "turn": True, "window": None}
        self._active[spec.run_id] = (client, state)
        read = self._read.setdefault(spec.run_id, [])
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        transcript = self.transcripts_dir / f"{spec.run_id}.jsonl"
        final: ResultMessage | None = None
        # the results from once the prompt was read: more than one when a message sent late ran as a next turn
        results: list[ResultMessage] = []
        watchdog = asyncio.create_task(self._watch(client, state))
        try:
            async with asyncio.timeout_at(deadline()) as window:
                state["window"] = window
                await client.connect()
                await client.query(_user_message(prompt_uuid, prompt))
                state["accepting"] = True
                async for msg in client.receive_messages():
                    window.reschedule(deadline())  # any message is progress
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
                    if isinstance(msg, UserMessage) and msg.uuid in state["pending"]:
                        state["turn"] = True  # the CLI read a message: a turn answers it
                        if (item_id := state["pending"].pop(msg.uuid)) is not None:
                            read.append(item_id)
                    elif isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                        state["turn"] = True  # the main thread is working (a finished agent woke it, say)
                    if isinstance(msg, SystemMessage):
                        track_task(state["tasks"], msg)
                    if isinstance(msg, ResultMessage):
                        state["turn"] = False
                        switching = msg.is_error and msg.api_error_status in FORK_STATUSES
                        # A result from before the prompt was read answers something else (a background task from
                        # an earlier turn finishing): it doesn't count, unless it moves the run to another account
                        if prompt_uuid not in state["pending"] or switching:
                            final = msg
                            results.append(msg)
                        # A message sent but not yet read runs as a next turn in this process, so read on to its
                        # result. An interrupted session stops here: what it didn't read goes back to the queue.
                        # So does a result that moves the run to another account: the session it would read on into
                        # is the one about to be abandoned, not the one the pending message should land in.
                        # Background agents still running make this an interim end of turn: ending the run here
                        # would post it as the final reply and close the CLI, and the agents with it. Read on: each
                        # agent wakes the session when it finishes, and messages sent meanwhile are read at once.
                        if (not state["pending"] and not state["tasks"]) or state["reason"] or switching:
                            state["accepting"] = False
                            break
        except TimeoutError:
            if state["reason"] is None:  # the idle window or the time budget ran out; a stop has its own reason
                state["reason"] = "timeout" if end is not None and window.when() == end else "stalled"
                await client.interrupt()
        finally:
            state["accepting"] = False
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._active.pop(spec.run_id, None)

        result = RunResult(run_id=spec.run_id, session_id=state["sid"], outcome="error",
                           account_code=state["account"])
        switch_wanted = False
        if results:  # every answer the model gave is kept; the outcome comes from the last result
            result.turns = sum(r.num_turns or 0 for r in results)
            (result.input_tokens, result.output_tokens, result.cache_read_tokens,
             result.cache_write_tokens) = (sum(n) for n in zip(*(usage_tokens(r.usage) for r in results)))
            # not summed: the CLI's total_cost_usd is its session-wide ledger, so the last result already has it all
            result.cost_usd_est = final.total_cost_usd or 0.0
            result.summary = "\n\n".join(r.result for r in results if r.result)
        if state["reason"] == "timeout":
            result.outcome, result.error = "timeout", f"no result within {spec.timeout_s} s"
        elif state["reason"] == "stalled":
            result.outcome, result.error = "stalled", f"no progress for {spec.idle_s} s"
        elif state["reason"] == "external":
            result.outcome, result.error = "interrupted", "interrupted"
        elif state["reason"] == "watchdog":
            result.error = f"account {state['account']} is {state.get('trouble')}"
            switch_wanted = True
        elif final is None:
            result.error = "the session ended without a result"
        else:
            result.outcome, result.error = map_result(final)
            if result.outcome == "error":
                if final.api_error_status in FORK_STATUSES:
                    switch_wanted = True
                elif final.api_error_status == 429:
                    switch_wanted = await self._account_status(state) in ACCOUNT_TROUBLE
        return _Attempt(result, switch_wanted)
