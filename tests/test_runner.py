import asyncio
import io
import tarfile

import httpx
import pytest

from agent.runner.base import RunResult
from agent.runner.link import CoreLink, SessionRevoked
from agent.runner.tools import remote_sdk_tools
from agent.runner.worker import FRESH_NOTE, Runner


def bundle_bytes() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name in ("plugins/core/.claude-plugin/plugin.json", "plugins/effortless/.claude-plugin/plugin.json",
                     "plugins/packs/a/.claude-plugin/plugin.json", "plugins/packs/b/.claude-plugin/plugin.json"):
            data = b"{}"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


class FakeLink:
    def __init__(self, cancel=False, revoked=False):
        self.cancel, self.revoked = cancel, revoked
        self.tool_calls, self.files_sent, self.results, self.events = [], [], [], []
        self.posted = True

    async def heartbeat(self, tok):
        if self.revoked:
            raise SessionRevoked()
        return self.cancel

    async def event(self, tok, ev):
        self.events.append(ev)
        return False

    async def tool(self, tok, name, args):
        self.tool_calls.append((name, args))
        return {"text": f"core ran {name}"}

    async def files(self, tok, items, text):
        self.files_sent.append((items, text))
        return self.posted

    async def result(self, tok, result):
        self.results.append(result)

    async def bundle(self):
        return "b1", bundle_bytes()


class FakeClaude:
    def __init__(self, hang=False):
        self.specs, self.interrupted, self.hang = [], [], hang
        self._stop = asyncio.Event()

    async def run(self, spec):
        self.specs.append(spec)
        if self.hang:
            await asyncio.wait_for(self._stop.wait(), 5)
            return RunResult(spec.run_id, "sess-1", "interrupted", error="interrupted")
        return RunResult(spec.run_id, "sess-1", "done", summary="all done", turns=3, cost_usd_est=0.5)

    async def interrupt(self, run_id):
        self.interrupted.append(run_id)
        self._stop.set()


def item(conversation_id="V-1", **payload_over):
    payload = {"text": "fix the export", "actor": "bob", "unattended": False,
               "model": "claude-opus-5-5", "system_prompt": "SYS", "max_turns": 400, "timeout_s": 5400,
               "resume_session_id": None, "test_secrets": {"SAMPLE_TEST_USER": "tester@example.com"},
               "project": "sample"}
    payload.update(payload_over)
    return {"id": f"W-{conversation_id}", "conversation_id": conversation_id, "kind": "turn", "payload": payload}


def make_runner(tmp_path, link, claude, **kw):
    return Runner(link, claude, bundles_dir=tmp_path / "bundles", sessions_root=tmp_path / "sessions",
                  scratch_root=tmp_path / "scratch", heartbeat_s=0.05, **kw)


async def test_handle_builds_bypass_spec_and_reports(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, link, claude).handle(item(), "tok", [])
    spec = claude.specs[0]
    assert spec.model == "claude-opus-5-5"
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "V-1")
    assert spec.env["SAMPLE_TEST_USER"] == "tester@example.com"
    assert spec.env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1"
    assert [p.name for p in spec.plugins] == ["core", "effortless", "a", "b"] and spec.system_prompt == "SYS"
    assert link.results[-1]["text"] == "all done" and link.results[-1]["session_id"] == "sess-1"
    assert link.results[-1]["outcome"] == "done" and link.results[-1]["cost_usd_est"] == 0.5


async def test_every_turn_runs_in_its_conversations_own_folder(tmp_path):
    """A project conversation gets no checkout made for it: the session clones what it needs into its folder, which
    is the same one on every turn."""
    claude = FakeClaude()
    runner = make_runner(tmp_path, FakeLink(), claude)
    await runner.handle(item("V-1"), "tok", [])
    (tmp_path / "scratch" / "V-1" / "chilliclaw").mkdir()     # what the session cloned on its first turn
    await runner.handle(item("V-1", resume_session_id="sess-1"), "tok", [])
    await runner.handle(item("V-2", project=None), "tok", [])
    assert [s.cwd for s in claude.specs] == [tmp_path / "scratch" / "V-1"] * 2 + [tmp_path / "scratch" / "V-2"]
    assert (tmp_path / "scratch" / "V-1" / "chilliclaw").is_dir()
    assert not (tmp_path / "scratch" / "V-1" / ".git").exists()


async def test_every_session_shares_one_claude_code_memory(tmp_path):
    claude = FakeClaude()
    runner = make_runner(tmp_path, FakeLink(), claude, memory_dir=tmp_path / "memory")
    await runner.handle(item("V-1"), "tok", [])
    await runner.handle(item("V-2", project=None), "tok", [])
    assert [s.settings for s in claude.specs] == [{"autoMemoryDirectory": str(tmp_path / "memory")}] * 2
    assert (tmp_path / "memory").is_dir()


async def test_prompt_starts_with_the_messages_origin(tmp_path):
    claude = FakeClaude()
    runner = make_runner(tmp_path, FakeLink(), claude)
    await runner.handle(item("V-1", origin="[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]"),
                        "tok", [])
    await runner.handle(item("V-2"), "tok", [])
    assert claude.specs[0].prompt == "[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]\nfix the export"
    assert claude.specs[1].prompt == "fix the export"


async def test_runner_loads_core_effortless_and_all_pack_plugins(tmp_path):
    runner = make_runner(tmp_path, FakeLink(), FakeClaude())
    plugins = await runner._bundle()
    assert [p.name for p in plugins] == ["core", "effortless", "a", "b"]


async def test_runner_runs_ten_conversations_at_once(tmp_path):
    """The runner runs as many conversations at once as core hands it (10 by default). 10 fake items block inside
    claude.run() until released, and all 10 must be in flight together."""
    n = 10
    entered: list[str] = []
    all_in = asyncio.Event()
    release = asyncio.Event()

    class BlockingClaude:
        async def run(self, spec):
            entered.append(spec.run_id)
            if len(entered) == n:
                all_in.set()
            await release.wait()
            return RunResult(spec.run_id, "sess", "done", summary="ok")

        async def interrupt(self, run_id):
            release.set()

    runner = make_runner(tmp_path, FakeLink(), BlockingClaude())
    items = [item(conversation_id=f"V-{i}") for i in range(n)]
    handles = asyncio.gather(*(runner.handle(it, f"tok-{i}", []) for i, it in enumerate(items)))
    await asyncio.wait_for(all_in.wait(), 2)
    assert len(entered) == n
    release.set()
    await asyncio.wait_for(handles, 2)


async def test_cancel_via_heartbeat_interrupts(tmp_path):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, FakeLink(cancel=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


async def test_revoked_token_interrupts(tmp_path):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, FakeLink(revoked=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


MANIFEST = [{"name": "post_files", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "status", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


async def call(tools, name, args):
    tool = next(t for t in tools if t.name == name)
    return (await tool.handler(args))["content"][0]["text"]


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "scratch" / "V-9"
    path.mkdir(parents=True)
    return path


async def test_generic_tool_forwards(session):
    link = FakeLink()
    assert await call(remote_sdk_tools(MANIFEST, link, "tok", session_dir=session), "status", {}) == "core ran status"
    assert link.tool_calls == [("status", {})]


async def test_post_files_outside_the_session_folder_refused(session):
    link = FakeLink()
    tools = remote_sdk_tools(MANIFEST, link, "tok", session_dir=session)
    assert "outside" in await call(tools, "post_files", {"paths": ["/etc/passwd"], "text": ""})
    assert "outside" in await call(tools, "post_files", {"paths": ["../../../etc/passwd"], "text": ""})
    assert link.files_sent == []


async def test_post_files_sends_files_from_the_session_folder_and_its_clones(session):
    link = FakeLink()
    (session / "chilliclaw").mkdir()
    (session / "chilliclaw" / "shot.png").write_bytes(b"png")
    tools = remote_sdk_tools(MANIFEST, link, "tok", session_dir=session)
    await call(tools, "post_files", {"paths": ["chilliclaw/shot.png"], "text": "after the fix"})
    await call(tools, "post_files", {"paths": [str(session / "chilliclaw" / "shot.png")], "text": "again"})
    assert link.files_sent == [([("shot.png", b"png")], "after the fix"), ([("shot.png", b"png")], "again")]


async def test_post_files_says_when_nothing_reached_slack(session):
    link = FakeLink()
    link.posted = False
    (session / "shot.png").write_bytes(b"png")
    reply = await call(remote_sdk_tools(MANIFEST, link, "tok", session_dir=session), "post_files",
                       {"paths": ["shot.png"], "text": "t"})
    assert "no Slack thread" in reply


async def test_link_maps_401_and_204():
    def handler(request):
        if request.url.path == "/agent/pull":
            return httpx.Response(204)
        return httpx.Response(401, json={"error": "unauthorized"})

    link = CoreLink("http://core.test", "runner-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await link.pull() is None
    with pytest.raises(SessionRevoked):
        await link.heartbeat("tok")


class ResumeFailsClaude(FakeClaude):
    async def run(self, spec):
        self.specs.append(spec)
        if spec.resume_session_id:
            return RunResult(spec.run_id, None, "error", error="No conversation found with session ID")
        return RunResult(spec.run_id, "sess-new", "done", summary="caught up")


async def test_dead_session_falls_back_to_a_fresh_one(tmp_path):
    link, claude = FakeLink(), ResumeFailsClaude()
    await make_runner(tmp_path, link, claude).handle(item(resume_session_id="sess-gone"), "tok", [])
    assert [s.resume_session_id for s in claude.specs] == ["sess-gone", None]
    assert "read_thread" in claude.specs[1].prompt and claude.specs[1].prompt.endswith("fix the export")
    assert link.results[-1]["outcome"] == "done" and link.results[-1]["session_id"] == "sess-new"


async def test_fresh_session_prompt_still_starts_with_the_origin(tmp_path):
    """The fallback's note goes after the who/where/link line, which stays the prompt's first line."""
    origin = "[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]"
    link, claude = FakeLink(), ResumeFailsClaude()
    await make_runner(tmp_path, link, claude).handle(item(resume_session_id="sess-gone", origin=origin), "tok", [])
    assert claude.specs[1].prompt == f"{origin}\n{FRESH_NOTE}fix the export"


async def test_preparation_failure_reports_the_error(tmp_path):
    class NoBundleLink(FakeLink):
        async def bundle(self):
            raise httpx.ConnectError("core is restarting")

    link = NoBundleLink()
    await make_runner(tmp_path, link, FakeClaude()).handle(item(), "tok", [])
    assert link.results[-1]["outcome"] == "error" and "core is restarting" in link.results[-1]["error"]


async def test_runner_exits_when_its_token_is_revoked(tmp_path):
    class RevokedLink(FakeLink):
        async def pull(self):
            raise SessionRevoked()

    runner = make_runner(tmp_path, RevokedLink(), FakeClaude())
    await asyncio.wait_for(runner.run_forever(), 2)


async def test_extra_env_reaches_the_session(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, link, claude, extra_env={"GH_TOKEN": "ghp_abc"})
    await runner.handle(item(), "tok", [])
    assert claude.specs[0].env["GH_TOKEN"] == "ghp_abc"
    assert claude.specs[0].env["SAMPLE_TEST_USER"] == "tester@example.com"


async def test_forced_env_keys_cannot_be_overridden(tmp_path):
    """CLAUDE_CONFIG_DIR and the function-hooks flag are forced last: neither the container's extra env nor a
    pack's test secrets can override them, even when both try to."""
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, link, claude, extra_env={"CLAUDE_CONFIG_DIR": "/from-extra-env",
                                                            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"})
    await runner.handle(item(test_secrets={"CLAUDE_CONFIG_DIR": "/from-test-secrets",
                                           "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"}), "tok", [])
    spec = claude.specs[0]
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "V-1")
    assert spec.env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1"


LINEAR_MCP = {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                         "headers": {"Authorization": "Bearer ${LINEAR_APP_TOKEN}"}}}


async def test_mcp_headers_use_current_linear_token(tmp_path):
    claude, env_file = FakeClaude(), tmp_path / "linear.env"
    runner = make_runner(tmp_path, FakeLink(), claude, env_files=[env_file])
    for n, token in enumerate(("tok-1", "tok-2")):
        env_file.write_text(f"LINEAR_APP_TOKEN={token}\n")
        await runner.handle(item(conversation_id=f"V-{n}", mcp=LINEAR_MCP), "tok", [])
    assert [s.mcp_servers["linear"]["headers"]["Authorization"] for s in claude.specs] == \
        ["Bearer tok-1", "Bearer tok-2"]
    assert claude.specs[1].mcp_servers["linear"]["url"] == "https://mcp.linear.app/mcp"
    assert claude.specs[1].env["LINEAR_APP_TOKEN"] == "tok-2"   # for agent activities via the API


async def test_unset_variable_drops_the_server(tmp_path, caplog):
    claude = FakeClaude()
    await make_runner(tmp_path, FakeLink(), claude, env_files=[tmp_path / "missing.env"]).handle(
        item(mcp=LINEAR_MCP), "tok", [])
    assert claude.specs[0].mcp_servers == {}
    assert "LINEAR_APP_TOKEN" in caplog.text
