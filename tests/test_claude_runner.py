import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (TERMINAL_TASK_STATUSES, AssistantMessage, CLIConnectionError, ResultMessage, TextBlock,
                              UserMessage, tool)
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk._internal.query import DEFERRING_TASK_TYPES

from agent.proxy import PLACEHOLDER_TOKEN, AccountUsage, ProxyUnavailable, SoftCap, Usage
from agent.runner.base import RunSpec
from agent.runner import claude
from agent.runner.claude import ClaudeRunner
from agent.runner.idle_wait import block_idle_waits

HANG = "HANG"
WAIT_SENT = "WAIT_SENT"  # wait until the runner writes a message into the session
WAIT_STOP = "WAIT_STOP"  # wait until the session is interrupted, then carry on with the script


def sids():
    n = 0
    while True:
        n += 1
        yield f"00000000-0000-4000-8000-{n:012d}"


class FakeProxy:
    def __init__(self, account="maple", status="allowed"):
        self.account, self.status, self.whoami_calls = account, status, []
        self.down = False

    async def whoami(self, sid):
        if self.down:
            raise ProxyUnavailable("proxy unreachable")
        self.whoami_calls.append(sid)
        return self.account

    async def usage(self):
        return Usage({self.account: AccountUsage(self.status, 1, True)})


class FakeClient:
    def __init__(self, options, script):
        self.options, self.script = options, script
        self.interrupted, self.prompt, self.sent = False, None, []
        self._stop, self._sent = asyncio.Event(), asyncio.Event()

    async def connect(self):
        pass

    async def query(self, prompt):
        async for message in prompt:  # the turn's own prompt comes first, then each message written mid-turn
            if self.prompt is None:
                self.prompt = message
                continue
            self.sent.append(message)
            self._sent.set()

    async def receive_messages(self):
        for item in self.script:
            if item == HANG:
                await self._stop.wait()
                return
            if item == WAIT_SENT:
                await self._sent.wait()
                self._sent.clear()
                continue
            if item == WAIT_STOP:
                await self._stop.wait()
                continue
            if isinstance(item, float):  # the session works this long before its next message
                await asyncio.sleep(item)
                continue
            yield item(self) if callable(item) else item

    async def interrupt(self):
        self.interrupted = True
        self._stop.set()

    async def disconnect(self):
        pass


def assistant(c):
    return AssistantMessage(content=[TextBlock("working")], model="m", session_id=c.options.session_id)


def result(subtype="success", **kw):
    def make(c):
        return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=subtype != "success",
                             num_turns=kw.pop("num_turns", 3), session_id=c.options.session_id or c.options.resume,
                             total_cost_usd=kw.pop("cost", 0.12),
                             usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5,
                                    "cache_creation_input_tokens": 7}, result=kw.pop("text", "final text"), **kw)
    return make


def echo_prompt(c):
    """The CLI's echo of the turn's own prompt once it reads it: the same text, the same uuid."""
    return UserMessage(content=c.prompt["message"]["content"], uuid=c.prompt["uuid"])


def replay(c, i=-1):
    """The CLI's echo of a message the runner wrote: the same text, the same uuid."""
    return UserMessage(content=c.sent[i]["message"]["content"], uuid=c.sent[i]["uuid"])


async def wait_until(cond):
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


def make_runner(tmp_path, attempts, proxy=None, watchdog_s=60):
    clients = []

    def factory(options):
        clients.append(FakeClient(options, attempts[len(clients)]))
        return clients[-1]

    runner = ClaudeRunner(proxy or FakeProxy(), "http://172.30.0.1:28091", tmp_path, client_factory=factory,
                          watchdog_s=watchdog_s, uuid_factory=sids().__next__)
    return runner, clients


def spec(tmp_path, **kw):
    args = dict(run_id="R1", prompt="build it", cwd=tmp_path, model="claude-opus-5-5",
                system_prompt="sys", idle_s=5, env={"TZ": "Asia/Singapore"})
    args.update(kw)
    return RunSpec(**args)


async def test_options_env_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", "ghp_secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("HARMLESS_SETTING", "ok")
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path, plugins=[Path("/plugins/core")]))
    o = clients[0].options
    assert o.env["ANTHROPIC_BASE_URL"] == "http://172.30.0.1:28091"
    assert o.env["CLAUDE_CODE_OAUTH_TOKEN"] == PLACEHOLDER_TOKEN
    assert o.env["TZ"] == "Asia/Singapore"
    assert o.env["GITHUB_WRITE_TOKEN"] == "" and o.env["SLACK_BOT_TOKEN"] == ""
    assert "HARMLESS_SETTING" not in o.env
    assert o.setting_sources == ["user", "project"]
    assert o.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert (o.model, o.max_turns) == ("claude-opus-5-5", None)  # no step limit: a turn runs until it's done
    assert o.plugins == [{"type": "local", "path": "/plugins/core"}]
    assert o.session_id == "00000000-0000-4000-8000-000000000001" and o.resume is None


async def test_settings_go_to_the_session_as_inline_json(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()], [echo_prompt, result()]])
    await runner.run(spec(tmp_path, settings={"autoMemoryDirectory": "/var/agent/memory"}))
    assert json.loads(clients[0].options.settings) == {"autoMemoryDirectory": "/var/agent/memory"}
    await runner.run(spec(tmp_path, run_id="R2"))
    assert clients[1].options.settings is None


async def test_sdk_tools_become_the_agent_mcp_server(tmp_path):
    @tool("ping", "d", {"type": "object", "properties": {}})
    async def ping(args):
        return {"content": [{"type": "text", "text": "pong"}]}

    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path, sdk_tools=[ping]))
    assert "agent" in clients[0].options.mcp_servers


async def test_no_sdk_tools_means_no_agent_server(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path))
    assert "agent" not in clients[0].options.mcp_servers


async def test_result_mapping_success(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, result()]])
    r = await runner.run(spec(tmp_path))
    assert (r.outcome, r.summary) == ("done", "final text")


async def test_tokens_and_cost_copied(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, result(cost=0.5, num_turns=7)]])
    r = await runner.run(spec(tmp_path))
    assert (r.turns, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.cost_usd_est) == \
        (7, 100, 20, 5, 7, 0.5)
    assert r.session_id == "00000000-0000-4000-8000-000000000001"


async def test_transcript_written(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, result()]])
    await runner.run(spec(tmp_path))
    lines = (tmp_path / "R1.jsonl").read_text().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["UserMessage", "AssistantMessage", "ResultMessage"]


async def test_account_code_from_whoami(tmp_path):
    proxy = FakeProxy(account="cedar")
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, assistant, result()]], proxy=proxy)
    r = await runner.run(spec(tmp_path))
    assert r.account_code == "cedar" and len(proxy.whoami_calls) == 1


async def test_resume_keeps_the_session_id(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path, resume_session_id="11111111-1111-4111-8111-111111111111"))
    o = clients[0].options
    assert o.resume == "11111111-1111-4111-8111-111111111111" and o.fork_session is False and o.session_id is None


async def test_a_rejected_account_moves_to_a_fresh_session_never_a_resume(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result("error_during_execution", api_error_status=403,
                                                                  errors=["account disabled: maple"])],
                                             [echo_prompt, result()]])
    home = tmp_path / "claude"
    r = await runner.run(spec(tmp_path, env={"CLAUDE_CONFIG_DIR": str(home)}))
    assert (r.outcome, r.forks) == ("done", 1)
    second = clients[1].options
    assert second.resume is None and second.fork_session is False  # no history carried onto the new account
    assert second.session_id == "00000000-0000-4000-8000-000000000003"  # 002 was the first attempt's prompt
    handoff = clients[1].prompt["message"]["content"]
    assert handoff.endswith("build it") and "fresh session" in handoff and "read_thread" in handoff
    assert "don't read or load its record whole" in handoff and "--resume" in handoff
    earlier = "00000000-0000-4000-8000-000000000001"  # the earlier session, to read from inside the chat
    assert str(home / "projects" / "*" / (earlier + ".jsonl")) in handoff
    assert str(tmp_path / "R1.jsonl") in handoff  # the runner's transcript of it
    assert clients[1].prompt["uuid"] != clients[0].prompt["uuid"]


async def test_a_resumed_session_on_a_rejected_account_hands_off_without_resuming(tmp_path):
    unavailable = result("error_during_execution", api_error_status=503)
    runner, clients = make_runner(tmp_path, [[echo_prompt, unavailable], [echo_prompt, result()]])
    r = await runner.run(spec(tmp_path, resume_session_id="11111111-1111-4111-8111-111111111111",
                              handoff=lambda sid, transcript: "HANDOFF " + sid + " " + transcript.name))
    assert clients[0].options.resume == "11111111-1111-4111-8111-111111111111"
    assert clients[1].options.resume is None and clients[1].options.fork_session is False
    assert clients[1].prompt["message"]["content"] == "HANDOFF 11111111-1111-4111-8111-111111111111 R1.jsonl"
    assert r.outcome == "done" and r.session_id == clients[1].options.session_id


async def test_can_resume_only_on_the_account_the_session_last_ran_on(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    proxy = FakeProxy(account="maple")
    runner, _ = make_runner(tmp_path, [], proxy=proxy)
    assert await runner.can_resume(sid, "maple") is True
    assert proxy.whoami_calls == [sid]
    assert await runner.can_resume(sid, "cedar") is False  # the proxy has it pinned elsewhere now
    assert await runner.can_resume(sid, None) is False  # the last account isn't known: don't guess
    proxy.account = None  # the pin expired: the proxy would pick an account on the first request
    assert await runner.can_resume(sid, "maple") is False
    proxy.account, proxy.status = "maple", "rejected"  # still pinned, but the proxy would move it
    assert await runner.can_resume(sid, "maple") is False
    proxy.status, proxy.down = "allowed", True
    assert await runner.can_resume(sid, "maple") is False


async def test_account_of_reports_the_live_sessions_account(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, HANG]], proxy=FakeProxy(account="cedar"))
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.account_of("R1") == "cedar")
    await runner.interrupt("R1")
    await task
    assert runner.account_of("R1") is None


async def test_fork_on_429_only_when_account_rejected(tmp_path):
    err = [result("error_during_execution", api_error_status=429)]
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, *err], [echo_prompt, result()]],
                                  proxy=FakeProxy(status="rejected"))
    assert (await runner.run(spec(tmp_path))).forks == 1
    runner2, clients2 = make_runner(tmp_path, [[echo_prompt, assistant, *err]], proxy=FakeProxy(status="allowed"))
    r2 = await runner2.run(spec(tmp_path))
    assert (r2.outcome, r2.forks, len(clients2)) == ("error", 0, 1)
    assert "429" in r2.error


async def test_fork_cap_two(tmp_path):
    bad = [echo_prompt, result("error_during_execution", api_error_status=503)]
    runner, clients = make_runner(tmp_path, [bad, bad, bad])
    r = await runner.run(spec(tmp_path))
    assert (r.outcome, r.forks, len(clients)) == ("error", 2, 3)


async def test_watchdog_interrupts_on_rejected_account(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, HANG], [echo_prompt, result()]],
                                  proxy=FakeProxy(status="rejected"), watchdog_s=0.01)
    r = await runner.run(spec(tmp_path))
    assert clients[0].interrupted is True
    assert (r.outcome, r.forks) == ("done", 1)


NOW, RESET_IN_3H = 1794560000.0, 1794560000.0 + 3 * 3600


class CapProxy(FakeProxy):
    """The session is on maple; each account is (rank, 5-hour window used), with 3h to the reset."""

    def __init__(self, accounts):
        super().__init__(account="maple")
        self.accounts = accounts

    async def usage(self):
        return Usage({code: AccountUsage("allowed", rank, True, used, RESET_IN_3H)
                      for code, (rank, used) in self.accounts.items()}, soft_cap=SoftCap(), now=NOW)


def test_handoff_target_is_the_top_ranked_account_under_its_cap():
    def target(accounts):
        return Usage({c: AccountUsage("allowed", r, True, u, RESET_IN_3H) for c, (r, u) in accounts.items()},
                     soft_cap=SoftCap(), now=NOW).handoff_target("maple")
    assert target({"maple": (2, 0.7), "oak": (1, 0.1)}) == "oak"
    assert target({"maple": (2, 0.7), "oak": (1, 0.8)}) is None   # the top-ranked account is capped too
    assert target({"maple": (1, 0.7), "oak": (2, 0.1)}) is None   # a fresh session would land on maple again
    capped = Usage({"maple": AccountUsage("allowed", 1, True, 0.9, RESET_IN_3H)}, soft_cap=SoftCap(), now=NOW)
    assert capped.over_cap("maple") and capped.has_capacity()      # capped still has room: people's requests start


async def test_watchdog_hands_off_an_account_over_its_soft_cap(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, HANG], [echo_prompt, result()]],
                                  proxy=CapProxy({"maple": (2, 0.66), "oak": (1, 0.1)}), watchdog_s=0.01)
    r = await runner.run(spec(tmp_path, handoff=lambda sid, transcript: "HANDOFF " + sid))
    assert clients[0].interrupted is True and clients[1].options.resume is None   # a fresh session, never --resume
    assert clients[1].prompt["message"]["content"] == "HANDOFF " + clients[0].options.session_id
    assert (r.outcome, r.forks) == ("done", 1)


async def test_watchdog_keeps_running_over_the_soft_cap_with_no_account_free(tmp_path):
    for accounts in ({"maple": (1, 0.66), "oak": (2, 0.8)},    # every account capped
                     {"maple": (1, 0.66), "oak": (2, 0.1)}):   # the proxy would put a fresh session on maple again
        runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, 0.1, result()]],
                                      proxy=CapProxy(accounts), watchdog_s=0.01)
        r = await runner.run(spec(tmp_path))
        assert clients[0].interrupted is False and (r.outcome, r.forks) == ("done", 0)


async def test_watchdog_keeps_running_over_the_soft_cap_with_no_fork_left(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, 0.1, result()]],
                                  proxy=CapProxy({"maple": (2, 0.66), "oak": (1, 0.1)}), watchdog_s=0.01)
    runner.max_forks = 0
    r = await runner.run(spec(tmp_path))
    assert clients[0].interrupted is False and (r.outcome, r.forks) == ("done", 0)


async def test_soft_cap_lifted_keeps_the_session_where_it_is(tmp_path):
    proxy = CapProxy({"maple": (2, 0.9), "oak": (1, 0.1)})
    lifted = proxy.usage

    async def usage():
        u = await lifted()
        return Usage(u.accounts, soft_cap=u.soft_cap, now=RESET_IN_3H - 3600)   # 1h to the reset
    proxy.usage = usage
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, 0.1, result()]], proxy=proxy, watchdog_s=0.01)
    r = await runner.run(spec(tmp_path))
    assert clients[0].interrupted is False and (r.outcome, r.forks) == ("done", 0)


async def test_a_session_that_goes_quiet_is_stopped_as_stalled(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, HANG]])
    r = await runner.run(spec(tmp_path, idle_s=0.05))
    assert (r.outcome, r.error, clients[0].interrupted) == ("stalled", "no progress for 0.05 s", True)


async def test_a_session_that_keeps_working_runs_on_past_its_idle_window(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, *[0.1, assistant] * 4, result()]])
    r = await runner.run(spec(tmp_path, idle_s=0.2))
    assert (r.outcome, clients[0].interrupted) == ("done", False)


async def test_a_run_with_a_time_budget_stops_at_it_however_busy(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, *[0.02, assistant] * 50, result()]])
    r = await runner.run(spec(tmp_path, timeout_s=0.1))
    assert (r.outcome, r.error, clients[0].interrupted) == ("timeout", "no result within 0.1 s", True)


async def test_a_time_budget_spans_the_whole_run_not_each_session(tmp_path):
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, clients = make_runner(tmp_path, [[echo_prompt, 0.16, forbidden],
                                             [echo_prompt, *[0.06, assistant] * 3, result()]])
    r = await runner.run(spec(tmp_path, timeout_s=0.24))
    assert (r.outcome, r.forks, clients[1].interrupted) == ("timeout", 1, True)


async def test_external_interrupt(tmp_path):
    runner, clients = make_runner(tmp_path, [[HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    while not clients:
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)
    await runner.interrupt("R1")
    assert (await task).outcome == "interrupted"


async def test_mcp_servers_the_agent_adds_itself_are_loaded(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path))
    assert clients[0].options.strict_mcp_config is False


async def test_interrupt_before_run_returns_interrupted(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.interrupt("R1")
    r = await runner.run(spec(tmp_path))
    assert r.outcome == "interrupted" and clients == []


def make_plain_runner(tmp_path, attempts):
    clients = []

    def factory(options):
        clients.append(FakeClient(options, attempts[len(clients)]))
        return clients[-1]

    runner = ClaudeRunner(FakeProxy(), "http://host.docker.internal:18091", tmp_path, client_factory=factory,
                          uuid_factory=sids().__next__)
    return runner, clients


async def test_always_bypasses_permissions(tmp_path):
    runner, clients = make_plain_runner(tmp_path, [[echo_prompt, result()]])
    out = await runner.run(spec(tmp_path))
    o = clients[0].options
    assert o.permission_mode == "bypassPermissions" and o.can_use_tool is None
    assert o.env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:18091"
    assert out.outcome == "done" and out.summary == "final text"


async def test_messages_forwarded(tmp_path):
    seen = []

    async def on_message(record):
        seen.append(record["type"])

    runner, _ = make_plain_runner(tmp_path, [[echo_prompt, assistant, result()]])
    await runner.run(spec(tmp_path, on_message=on_message))
    assert seen == ["UserMessage", "AssistantMessage", "ResultMessage"]


async def test_options_ask_the_cli_to_echo_messages(tmp_path):
    runner, _ = make_runner(tmp_path, [[]])
    assert runner.build_options(spec(tmp_path), session_id="s").extra_args == {"replay-user-messages": None}


async def test_options_let_a_large_image_through(tmp_path):
    # Issue #3: a 1.08 MB PNG from Slack is ~1.44 MB as base64, over the SDK's 1 MiB default, and ended the session
    runner, _ = make_runner(tmp_path, [[]])
    image_message = len(json.dumps({"type": "image", "data": base64.b64encode(b"\0" * 1_080_000).decode()}))
    assert runner.build_options(spec(tmp_path), session_id="s").max_buffer_size > 10 * image_message


async def test_a_message_sent_mid_turn_reaches_the_session_and_is_read(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert runner.session_of("R1") == "00000000-0000-4000-8000-000000000001"
    assert await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert clients[0].sent[0]["message"]["content"] == "how's it going?" and clients[0].sent[0]["uuid"]
    assert clients[0].sent[0]["type"] == "user" and clients[0].sent[0]["parent_tool_use_id"] is None
    assert out.read == ["W-2"] and out.outcome == "done"
    assert not runner.accepting("R1") and not await runner.send("R1", "W-3", "late")
    assert runner.session_of("R1") is None and runner.read_of("R1") == []


async def test_the_turn_keeps_reading_until_every_sent_message_is_read(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, result(num_turns=1), replay,
                                        result(num_turns=7)]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "also say hi")
    out = await task
    assert out.read == ["W-2"] and out.turns == 8


async def test_two_messages_are_both_read_in_order(tmp_path):
    def replay_first(c):
        return replay(c, 0)

    def replay_second(c):
        return replay(c, 1)

    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, WAIT_SENT, replay_first, replay_second,
                                              result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert await runner.send("R1", "W-2", "first")
    await wait_until(lambda: not clients[0]._sent.is_set())  # the session took the first before the second
    assert await runner.send("R1", "W-3", "second")
    out = await task
    assert [m["message"]["content"] for m in clients[0].sent] == ["first", "second"]
    assert clients[0].sent[0]["uuid"] != clients[0].sent[1]["uuid"]
    assert out.read == ["W-2", "W-3"]


async def test_read_of_reports_what_was_read_so_far_without_draining(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, replay, HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "how's it going?")
    await wait_until(lambda: runner.read_of("R1") == ["W-2"])
    assert runner.read_of("R1") == ["W-2"]
    await runner.interrupt("R1")
    assert (await task).read == ["W-2"]


async def test_a_fork_does_not_report_an_unread_message(tmp_path):
    """A 403/503 result ends the attempt right there, even with a message pending: the stream keeps going past
    it (its echo, then a clean result) to prove that tail is never consumed by this attempt — it would read on,
    marking the message read and erasing the fork, if the forking status didn't end the attempt on its own."""
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, forbidden, replay, result()],
                                             [echo_prompt, assistant, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.read == [] and out.forks == 1 and out.outcome == "done"
    assert clients[1].sent == []


async def test_a_fork_adds_its_reads_to_the_run(tmp_path):
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, replay, forbidden],
                                             [echo_prompt, assistant, WAIT_SENT, replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "first")
    await wait_until(lambda: len(clients) == 2 and runner.accepting("R1"))
    assert runner.session_of("R1") == clients[1].options.session_id
    await runner.send("R1", "W-3", "second")
    out = await task
    assert out.read == ["W-2", "W-3"] and out.forks == 1


async def test_a_cancelled_turn_stops_at_its_result_and_leaves_a_sent_message_unread(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, WAIT_STOP, result("error_during_execution"),
                                        replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert await runner.send("R1", "W-2", "how's it going?")
    await runner.interrupt("R1")
    assert not runner.accepting("R1") and not await runner.send("R1", "W-3", "late")
    out = await task
    assert out.read == [] and out.outcome == "interrupted"


async def test_a_message_that_cannot_be_written_is_not_waited_for(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, result(), HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path, idle_s=2)))
    await wait_until(lambda: runner.accepting("R1"))

    async def broken(prompt):
        clients[0]._sent.set()
        raise CLIConnectionError("ProcessTransport is not ready for writing")

    clients[0].query = broken
    assert not await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.outcome == "done" and out.read == []


async def test_a_two_result_turn_keeps_both_answers_sums_turns_and_tokens_and_takes_the_last_cost(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT,
                                        result(num_turns=2, cost=0.25, text="The PR is up."), replay,
                                        result(num_turns=1, cost=0.5, text="Told Sam it's nearly done.")]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.summary == "The PR is up.\n\nTold Sam it's nearly done."
    assert (out.turns, out.input_tokens, out.output_tokens, out.cache_read_tokens, out.cache_write_tokens) == \
        (3, 200, 40, 10, 14)
    assert out.cost_usd_est == 0.5  # the CLI reports the session-wide cost so far, so the last result has it all
    assert out.outcome == "done" and out.read == ["W-2"]


async def test_a_two_result_turn_skips_an_empty_answer_and_takes_its_outcome_from_the_last_result(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, assistant, WAIT_SENT, result(text=""), replay,
                                        result("error_during_execution", text="Ran into an error.")]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert (out.summary, out.outcome) == ("Ran into an error.", "error")


async def test_a_result_before_the_prompt_is_read_does_not_end_the_turn(tmp_path):
    """A resumed session whose background task finished between turns reports that first, with an empty result of
    its own, and only then reads this turn's prompt: the turn ends at the prompt's answer, and only that counts."""
    runner, clients = make_runner(tmp_path, [[result(num_turns=0, text=""), echo_prompt, assistant,
                                              result(num_turns=3)]])
    out = await runner.run(spec(tmp_path, resume_session_id="11111111-1111-4111-8111-111111111111"))
    assert (out.outcome, out.summary, out.turns, out.input_tokens) == ("done", "final text", 3, 100)
    assert clients[0].prompt == {"type": "user", "uuid": "00000000-0000-4000-8000-000000000001",
                                 "message": {"role": "user", "content": "build it"}, "parent_tool_use_id": None}
    assert out.read == []  # the prompt is the turn's own, not a work item it read


async def test_a_forking_result_before_the_prompt_is_read_still_forks(tmp_path):
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, _ = make_runner(tmp_path, [[forbidden, echo_prompt, result()], [echo_prompt, result()]])
    out = await runner.run(spec(tmp_path))
    assert (out.outcome, out.forks, out.read) == ("done", 1, [])


def task_frame(subtype, task_id, **fields):
    """A task lifecycle frame as the CLI sends it, parsed by the SDK's own parser into its typed message."""
    def make(c):
        return parse_message({"type": "system", "subtype": subtype, "task_id": task_id, "uuid": f"{subtype}-{task_id}",
                              "session_id": c.options.session_id, **fields})
    return make


def task_started(task_id, task_type="local_agent"):
    return task_frame("task_started", task_id, description=f"lane {task_id}", task_type=task_type)


def task_done(task_id, status="completed"):
    return task_frame("task_notification", task_id, status=status, output_file="", summary="done")


def task_killed(task_id):  # a stopped task can report its end only here, with no notification
    return task_frame("task_updated", task_id, patch={"status": "killed"})


def task_progress(task_id):
    return task_frame("task_progress", task_id, description=f"lane {task_id}", last_tool_name="Bash",
                      usage={"total_tokens": 10, "tool_uses": 1, "duration_ms": 5})


async def test_the_bash_hook_turning_away_idle_waits_is_on_every_session(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, result()]])
    await runner.run(spec(tmp_path))
    (matcher,) = clients[0].options.hooks["PreToolUse"]
    assert matcher.matcher == "Bash" and matcher.hooks == [block_idle_waits]


async def test_a_turn_ended_while_background_agents_run_is_interim(tmp_path):
    # The session launches two agents and ends its turn; the run reads on, and finishes only on the result after the
    # last agent is done (one by notification, one killed and reported only by task_updated)
    runner, clients = make_runner(tmp_path, [[echo_prompt, task_started("a1"), task_started("a2"),
                                              result(text="lanes running", num_turns=2), task_done("a1"),
                                              result(text="a1 in", num_turns=1), task_killed("a2"),
                                              result(text="all done", num_turns=3), HANG]])
    out = await runner.run(spec(tmp_path))
    assert out.outcome == "done" and out.turns == 6
    assert out.summary == "lanes running\n\na1 in\n\nall done"
    assert not clients[0].interrupted


async def test_a_message_sent_while_agents_run_is_read_before_they_finish(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, task_started("a1"), result(text="lane running"),
                                              WAIT_SENT, replay, result(text="answered"),
                                              task_done("a1"), result(text="lane done"), HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await asyncio.sleep(0.05)  # the interim result has come and gone: the run is still open
    assert runner.accepting("R1")
    assert await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.read == ["W-2"] and out.summary == "lane running\n\nanswered\n\nlane done"


async def test_a_background_shell_does_not_hold_the_run_open(tmp_path):
    # a dev server started with run_in_background runs until it's stopped: the turn's result still ends the run
    runner, _ = make_runner(tmp_path, [[echo_prompt, task_started("b1", "local_bash"), result(), HANG]])
    out = await asyncio.wait_for(runner.run(spec(tmp_path)), 2)
    assert out.outcome == "done"


async def test_a_workflow_holds_the_run_open_like_an_agent(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, task_started("w1", "local_workflow"), result(text="flow running"),
                                        task_done("w1"), result(text="flow done"), HANG]])
    out = await asyncio.wait_for(runner.run(spec(tmp_path)), 2)
    assert out.summary == "flow running\n\nflow done"


async def test_a_remote_agent_does_not_hold_the_run_open(tmp_path):
    # the CLI doesn't wait on a remote agent (it can be a long-running monitor), so neither does the run
    runner, _ = make_runner(tmp_path, [[echo_prompt, task_started("r1", "remote_agent"), result(), HANG]])
    out = await asyncio.wait_for(runner.run(spec(tmp_path)), 2)
    assert out.outcome == "done"


async def test_an_agent_that_failed_or_was_stopped_no_longer_holds_the_run_open(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, task_started("a1"), task_started("a2"), result(text="running"),
                                        task_done("a1", "failed"), result(text="a1 failed"),
                                        task_done("a2", "stopped"), result(text="a2 stopped"), HANG]])
    out = await asyncio.wait_for(runner.run(spec(tmp_path)), 2)
    assert out.summary == "running\n\na1 failed\n\na2 stopped"


def test_the_tasks_waited_on_are_the_sdks_own_sets():
    assert claude.WAITED_TASK_TYPES is DEFERRING_TASK_TYPES and claude.TASK_ENDED is TERMINAL_TASK_STATUSES
    assert "remote_agent" not in claude.WAITED_TASK_TYPES


async def test_a_stop_while_agents_run_between_turns_ends_the_run_at_once(tmp_path):
    # Between turns the CLI has no turn to interrupt, so no result comes: the run must not wait for one
    runner, clients = make_runner(tmp_path, [[echo_prompt, task_started("a1"), result(text="lanes running"), 30.0]])
    seen = []

    async def on_message(record):
        seen.append(record["type"])
    task = asyncio.create_task(runner.run(spec(tmp_path, on_message=on_message)))
    await wait_until(lambda: "ResultMessage" in seen)
    await runner.interrupt("R1")
    out = await asyncio.wait_for(task, 1)
    # nothing to interrupt between turns: the session is closed, with no control request left waiting on it
    assert (out.outcome, out.summary, clients[0].interrupted) == ("interrupted", "lanes running", False)


async def test_a_soft_cap_handoff_while_agents_run_between_turns_moves_at_once(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, assistant, task_started("a1"), result(text="lanes"), 30.0],
                                             [echo_prompt, result()]],
                                  proxy=CapProxy({"maple": (2, 0.66), "oak": (1, 0.1)}), watchdog_s=0.05)
    out = await asyncio.wait_for(runner.run(spec(tmp_path, handoff=lambda sid, transcript: "HANDOFF " + sid)), 1)
    assert (out.outcome, out.forks, clients[0].interrupted) == ("done", 1, False)


async def test_a_stop_while_a_woken_turn_runs_still_waits_for_its_result(tmp_path):
    # an agent finished and woke the session: that turn is running, so the stop ends the run at its result
    runner, _ = make_runner(tmp_path, [[echo_prompt, task_started("a1"), result(text="lanes running"), task_done("a1"),
                                        assistant, WAIT_STOP, result(text="stopped here"), 30.0]])
    seen = []

    async def on_message(record):
        seen.append(record["type"])
    task = asyncio.create_task(runner.run(spec(tmp_path, on_message=on_message)))
    await wait_until(lambda: "AssistantMessage" in seen)
    await runner.interrupt("R1")
    out = await asyncio.wait_for(task, 1)
    assert (out.outcome, out.summary) == ("interrupted", "lanes running\n\nstopped here")


async def test_progress_from_running_agents_keeps_a_waiting_session_from_stalling(tmp_path):
    # The turn ended with an agent running; the session then sends only the agent's frames, for longer than the
    # idle window in all, never longer than it between two: that's progress, not a stuck session
    runner, clients = make_runner(tmp_path, [[echo_prompt, task_started("a1"), result(text="lane running"),
                                              0.12, task_progress("a1"), 0.12, task_progress("a1"), 0.12,
                                              task_done("a1"), 0.12, result(text="lane done"), HANG]])
    out = await runner.run(spec(tmp_path, idle_s=0.2))
    assert (out.outcome, out.summary, clients[0].interrupted) == ("done", "lane running\n\nlane done", False)


async def test_a_session_waiting_on_agents_that_hears_nothing_is_stalled(tmp_path):
    runner, clients = make_runner(tmp_path, [[echo_prompt, task_started("a1"), result(text="lane running"), HANG]])
    out = await asyncio.wait_for(runner.run(spec(tmp_path, idle_s=0.1)), 2)
    assert (out.outcome, out.error, clients[0].interrupted) == ("stalled", "no progress for 0.1 s", True)


# What a run used: every API call its sessions' CLI logs show, main thread and subagents alike

def logged(mid, agent=None, read=1000):
    """The session's next API reply, written to the CLI's own log the way the CLI does (a subagent's to the agent's
    log, which never reaches the SDK stream when it runs in the background); the main thread's also streams."""
    def make(c):
        sid = c.options.session_id or c.options.resume
        home = Path(c.options.env["CLAUDE_CONFIG_DIR"]) / "projects" / "-var-agent-scratch-V-1"
        path = home / sid / "subagents" / f"agent-{agent}.jsonl" if agent else home / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with path.open("a") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": at, "sessionId": sid, "message": {
                "id": mid, "content": [], "usage": {"input_tokens": 1, "output_tokens": 10,
                                                    "cache_read_input_tokens": read,
                                                    "cache_creation_input_tokens": 50}}}) + "\n")
        return assistant(c)
    return make


def home_spec(tmp_path, **kw):
    return spec(tmp_path, env={"TZ": "Asia/Singapore", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}, **kw)


async def test_a_run_reports_every_call_its_session_made_its_agents_included(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, logged("m1"), logged("a1", agent="a1", read=4000),
                                        logged("m2"), result()]])
    out = await runner.run(home_spec(tmp_path))
    # not the result's own usage (100, 20, 5, 7), which covers the main thread only
    assert (out.input_tokens, out.output_tokens, out.cache_read_tokens, out.cache_write_tokens) == (3, 30, 6000, 150)


async def test_a_stalled_run_still_reports_the_calls_it_made(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, logged("m1"), HANG]])
    out = await runner.run(home_spec(tmp_path, idle_s=0.05))
    assert (out.outcome, out.input_tokens, out.cache_read_tokens) == ("stalled", 1, 1000)


async def test_a_run_that_moves_to_another_account_counts_the_session_it_left(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, logged("m1"), result("error_during_execution",
                                                                          api_error_status=503)],
                                       [echo_prompt, logged("m2", read=3000), result()]])
    out = await runner.run(home_spec(tmp_path))
    assert (out.forks, out.input_tokens, out.cache_read_tokens) == (1, 2, 4000)


async def test_a_resumed_session_counts_only_this_run(tmp_path):
    resumed = "11111111-1111-4111-8111-111111111111"
    earlier = tmp_path / "claude" / "projects" / "-var-agent-scratch-V-1" / f"{resumed}.jsonl"
    earlier.parent.mkdir(parents=True)
    earlier.write_text(json.dumps({"type": "assistant", "timestamp": "2026-11-01T00:00:00.000Z", "message": {
        "id": "old", "usage": {"input_tokens": 9, "output_tokens": 9, "cache_read_input_tokens": 9_000_000}}}) + "\n")
    runner, _ = make_runner(tmp_path, [[echo_prompt, logged("m1"), result()]])
    out = await runner.run(home_spec(tmp_path, resume_session_id=resumed, resume_account="maple"))
    assert (out.input_tokens, out.cache_read_tokens) == (1, 1000)


async def test_without_a_log_the_results_of_every_session_count(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, result("error_during_execution", api_error_status=503)],
                                       [echo_prompt, result()]])
    out = await runner.run(spec(tmp_path))
    assert (out.forks, out.input_tokens, out.output_tokens, out.cache_read_tokens, out.cache_write_tokens) == \
        (1, 200, 40, 10, 14)


async def test_a_running_session_sends_what_it_has_used_so_far(tmp_path):
    runner, _ = make_runner(tmp_path, [[echo_prompt, logged("m1"), logged("a1", agent="a1"), 0.3, result()]])
    runner.usage_every_s = 0.02
    seen = []

    async def on_message(record):
        seen.append(record)
    await runner.run(home_spec(tmp_path, on_message=on_message))
    counts = [r["usage"] for r in seen if r["type"] == "RunUsage"]
    assert counts and counts[-1] == {"input_tokens": 2, "output_tokens": 20, "cache_read_input_tokens": 2000,
                                     "cache_creation_input_tokens": 100}
    assert len(counts) == len({json.dumps(c) for c in counts})  # sent when it changes, not every tick
