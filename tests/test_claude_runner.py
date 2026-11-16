import asyncio
import json
from pathlib import Path

from claude_agent_sdk import AssistantMessage, CLIConnectionError, ResultMessage, TextBlock, UserMessage, tool

from agent.proxy import PLACEHOLDER_TOKEN, AccountUsage, Usage
from agent.runner.base import RunSpec
from agent.runner.claude import ClaudeRunner

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

    async def whoami(self, sid):
        self.whoami_calls.append(sid)
        return self.account

    async def usage(self):
        return Usage({self.account: AccountUsage(self.status, 1, True)})


class FakeClient:
    def __init__(self, options, script):
        self.options, self.script = options, script
        self.interrupted, self.queries, self.sent = False, [], []
        self._stop, self._sent = asyncio.Event(), asyncio.Event()

    async def connect(self):
        pass

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.queries.append(prompt)
            return
        async for message in prompt:
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
                system_prompt="sys", max_turns=10, timeout_s=5, env={"TZ": "Asia/Singapore"})
    args.update(kw)
    return RunSpec(**args)


async def test_options_env_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", "ghp_secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("HARMLESS_SETTING", "ok")
    runner, clients = make_runner(tmp_path, [[result()]])
    await runner.run(spec(tmp_path, plugins=[Path("/plugins/core")]))
    o = clients[0].options
    assert o.env["ANTHROPIC_BASE_URL"] == "http://172.30.0.1:28091"
    assert o.env["CLAUDE_CODE_OAUTH_TOKEN"] == PLACEHOLDER_TOKEN
    assert o.env["TZ"] == "Asia/Singapore"
    assert o.env["GITHUB_WRITE_TOKEN"] == "" and o.env["SLACK_BOT_TOKEN"] == ""
    assert "HARMLESS_SETTING" not in o.env
    assert o.setting_sources == ["user", "project"]
    assert o.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert (o.model, o.max_turns) == ("claude-opus-5-5", 10)
    assert o.plugins == [{"type": "local", "path": "/plugins/core"}]
    assert o.session_id == "00000000-0000-4000-8000-000000000001" and o.resume is None


async def test_settings_go_to_the_session_as_inline_json(tmp_path):
    runner, clients = make_runner(tmp_path, [[result()], [result()]])
    await runner.run(spec(tmp_path, settings={"autoMemoryDirectory": "/var/agent/memory"}))
    assert json.loads(clients[0].options.settings) == {"autoMemoryDirectory": "/var/agent/memory"}
    await runner.run(spec(tmp_path, run_id="R2"))
    assert clients[1].options.settings is None


async def test_sdk_tools_become_the_agent_mcp_server(tmp_path):
    @tool("ping", "d", {"type": "object", "properties": {}})
    async def ping(args):
        return {"content": [{"type": "text", "text": "pong"}]}

    runner, clients = make_runner(tmp_path, [[result()]])
    await runner.run(spec(tmp_path, sdk_tools=[ping]))
    assert "agent" in clients[0].options.mcp_servers


async def test_no_sdk_tools_means_no_agent_server(tmp_path):
    runner, clients = make_runner(tmp_path, [[result()]])
    await runner.run(spec(tmp_path))
    assert "agent" not in clients[0].options.mcp_servers


async def test_result_mapping_success(tmp_path):
    runner, _ = make_runner(tmp_path, [[result()]])
    r = await runner.run(spec(tmp_path))
    assert (r.outcome, r.summary) == ("done", "final text")


async def test_result_mapping_max_turns(tmp_path):
    runner, _ = make_runner(tmp_path, [[result("error_max_turns")]])
    assert (await runner.run(spec(tmp_path))).outcome == "max_turns"


async def test_tokens_and_cost_copied(tmp_path):
    runner, _ = make_runner(tmp_path, [[result(cost=0.5, num_turns=7)]])
    r = await runner.run(spec(tmp_path))
    assert (r.turns, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.cost_usd_est) == \
        (7, 100, 20, 5, 7, 0.5)
    assert r.session_id == "00000000-0000-4000-8000-000000000001"


async def test_transcript_written(tmp_path):
    runner, _ = make_runner(tmp_path, [[assistant, result()]])
    await runner.run(spec(tmp_path))
    lines = (tmp_path / "R1.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "AssistantMessage"


async def test_account_code_from_whoami(tmp_path):
    proxy = FakeProxy(account="cedar")
    runner, _ = make_runner(tmp_path, [[assistant, assistant, result()]], proxy=proxy)
    r = await runner.run(spec(tmp_path))
    assert r.account_code == "cedar" and len(proxy.whoami_calls) == 1


async def test_resume_without_fork(tmp_path):
    runner, clients = make_runner(tmp_path, [[result()]])
    await runner.run(spec(tmp_path, resume_session_id="11111111-1111-4111-8111-111111111111"))
    o = clients[0].options
    assert o.resume == "11111111-1111-4111-8111-111111111111" and o.fork_session is False and o.session_id is None


async def test_fork_on_403_then_success(tmp_path):
    runner, clients = make_runner(tmp_path, [[result("error_during_execution", api_error_status=403,
                                                     errors=["account disabled: maple"])], [result()]])
    r = await runner.run(spec(tmp_path))
    assert (r.outcome, r.forks) == ("done", 1)
    second = clients[1].options
    assert second.resume == "00000000-0000-4000-8000-000000000001" and second.fork_session is True
    assert second.session_id == "00000000-0000-4000-8000-000000000002"


async def test_fork_on_429_only_when_account_rejected(tmp_path):
    err = [result("error_during_execution", api_error_status=429)]
    runner, clients = make_runner(tmp_path, [[assistant, *err], [result()]], proxy=FakeProxy(status="rejected"))
    assert (await runner.run(spec(tmp_path))).forks == 1
    runner2, clients2 = make_runner(tmp_path, [[assistant, *err]], proxy=FakeProxy(status="allowed"))
    r2 = await runner2.run(spec(tmp_path))
    assert (r2.outcome, r2.forks, len(clients2)) == ("error", 0, 1)
    assert "429" in r2.error


async def test_fork_cap_two(tmp_path):
    bad = [result("error_during_execution", api_error_status=503)]
    runner, clients = make_runner(tmp_path, [bad, bad, bad])
    r = await runner.run(spec(tmp_path))
    assert (r.outcome, r.forks, len(clients)) == ("error", 2, 3)


async def test_watchdog_interrupts_on_rejected_account(tmp_path):
    runner, clients = make_runner(tmp_path, [[assistant, HANG], [result()]], proxy=FakeProxy(status="rejected"),
                                  watchdog_s=0.01)
    r = await runner.run(spec(tmp_path))
    assert clients[0].interrupted is True
    assert (r.outcome, r.forks) == ("done", 1)


async def test_timeout_interrupts(tmp_path):
    runner, clients = make_runner(tmp_path, [[HANG]])
    r = await runner.run(spec(tmp_path, timeout_s=0.05))
    assert (r.outcome, clients[0].interrupted) == ("timeout", True)


async def test_external_interrupt(tmp_path):
    runner, clients = make_runner(tmp_path, [[HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    while not clients:
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)
    await runner.interrupt("R1")
    assert (await task).outcome == "interrupted"


async def test_mcp_servers_the_agent_adds_itself_are_loaded(tmp_path):
    runner, clients = make_runner(tmp_path, [[result()]])
    await runner.run(spec(tmp_path))
    assert clients[0].options.strict_mcp_config is False


async def test_interrupt_before_run_returns_interrupted(tmp_path):
    runner, clients = make_runner(tmp_path, [[result()]])
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
    runner, clients = make_plain_runner(tmp_path, [[result()]])
    out = await runner.run(spec(tmp_path))
    o = clients[0].options
    assert o.permission_mode == "bypassPermissions" and o.can_use_tool is None
    assert o.env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:18091"
    assert out.outcome == "done" and out.summary == "final text"


async def test_messages_forwarded(tmp_path):
    seen = []

    async def on_message(record):
        seen.append(record["type"])

    runner, _ = make_plain_runner(tmp_path, [[assistant, result()]])
    await runner.run(spec(tmp_path, on_message=on_message))
    assert seen == ["AssistantMessage", "ResultMessage"]


async def test_options_ask_the_cli_to_echo_messages(tmp_path):
    runner, _ = make_runner(tmp_path, [[]])
    assert runner.build_options(spec(tmp_path), session_id="s").extra_args == {"replay-user-messages": None}


async def test_a_message_sent_mid_turn_reaches_the_session_and_is_read(tmp_path):
    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, replay, result()]])
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
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, result(num_turns=1), replay, result(num_turns=7)]])
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

    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, WAIT_SENT, replay_first, replay_second,
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
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, replay, HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "how's it going?")
    await wait_until(lambda: runner.read_of("R1") == ["W-2"])
    assert runner.read_of("R1") == ["W-2"]
    await runner.interrupt("R1")
    assert (await task).read == ["W-2"]


async def test_a_fork_does_not_report_an_unread_message(tmp_path):
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, forbidden], [assistant, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.read == [] and out.forks == 1 and out.outcome == "done"
    assert clients[1].sent == []


async def test_a_fork_adds_its_reads_to_the_run(tmp_path):
    forbidden = result("error_during_execution", api_error_status=403, errors=["account disabled: maple"])
    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, replay, forbidden],
                                             [assistant, WAIT_SENT, replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "first")
    await wait_until(lambda: len(clients) == 2 and runner.accepting("R1"))
    assert runner.session_of("R1") == clients[1].options.session_id
    await runner.send("R1", "W-3", "second")
    out = await task
    assert out.read == ["W-2", "W-3"] and out.forks == 1


async def test_a_cancelled_turn_stops_at_its_result_and_leaves_a_sent_message_unread(tmp_path):
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, WAIT_STOP, result("error_during_execution"),
                                        replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert await runner.send("R1", "W-2", "how's it going?")
    await runner.interrupt("R1")
    assert not runner.accepting("R1") and not await runner.send("R1", "W-3", "late")
    out = await task
    assert out.read == [] and out.outcome == "interrupted"


async def test_a_message_that_cannot_be_written_is_not_waited_for(tmp_path):
    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, result(), HANG]])
    task = asyncio.create_task(runner.run(spec(tmp_path, timeout_s=2)))
    await wait_until(lambda: runner.accepting("R1"))

    async def broken(prompt):
        clients[0]._sent.set()
        raise CLIConnectionError("ProcessTransport is not ready for writing")

    clients[0].query = broken
    assert not await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert out.outcome == "done" and out.read == []


async def test_a_two_result_turn_keeps_both_answers_sums_turns_and_tokens_and_takes_the_last_cost(tmp_path):
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, result(num_turns=2, cost=0.25, text="The PR is up."),
                                        replay, result(num_turns=1, cost=0.5, text="Told Sam it's nearly done.")]])
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
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, result(text=""), replay,
                                        result("error_max_turns", text="Ran out of turns.")]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert (out.summary, out.outcome) == ("Ran out of turns.", "max_turns")
