import asyncio
import io
import json
import tarfile

import httpx
import pytest

from agent.runner.base import RunResult
from agent.runner.claude import ClaudeRunner
from agent.runner.handoff import handoff_note
from agent.runner.link import CoreLink, SessionRevoked
from agent.runner.tools import remote_sdk_tools
from agent.runner.worker import FRESH_NOTE, MIDTURN_NOTE, RESTARTED_NOTE, Runner, memory_block
from test_claude_runner import HANG, FakeClient, FakeProxy, echo_prompt, result, sids


async def wait_until(cond):
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


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
        self.beats, self.messages = [], []  # messages: one list of messages per beat, consumed in order
        self.posted = True
        self.slack_files = {}  # file id -> bytes core serves; any other id fails like a 404

    async def slack_file(self, tok, file_id):
        if file_id not in self.slack_files:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "http://core"),
                                        response=httpx.Response(404))
        return self.slack_files[file_id]

    async def heartbeat(self, tok, beat):
        self.beats.append(beat)
        if self.revoked:
            raise SessionRevoked()
        return {"cancel": self.cancel, "messages": self.messages.pop(0) if self.messages else []}

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
        self.read, self.sent = [], []  # read: what a beat (or the result) should report; sent: (run_id, item_id, text)
        self._active: set[str] = set()
        self._stop = asyncio.Event()
        self.resumable, self.resume_checks = True, []  # what can_resume answers, and what it was asked

    async def run(self, spec):
        self.specs.append(spec)
        self._active.add(spec.run_id)
        try:
            if self.hang:
                await asyncio.wait_for(self._stop.wait(), 5)
                return RunResult(spec.run_id, "sess-1", "interrupted", error="interrupted", read=list(self.read))
            return RunResult(spec.run_id, "sess-1", "done", summary="all done", turns=3, cost_usd_est=0.5,
                             read=list(self.read))
        finally:
            self._active.discard(spec.run_id)

    def accepting(self, run_id):
        return run_id in self._active

    def session_of(self, run_id):
        return "live-1"

    def account_of(self, run_id):
        return "maple"

    async def can_resume(self, session_id, account):
        self.resume_checks.append((session_id, account))
        return self.resumable

    def read_of(self, run_id):
        return list(self.read)

    async def send(self, run_id, item_id, text):
        self.sent.append((run_id, item_id, text))
        return True

    async def interrupt(self, run_id):
        self.interrupted.append(run_id)
        self._stop.set()

    def release(self, run_id):
        pass


def item(conversation_id="V-1", **payload_over):
    payload = {"text": "fix the export", "actor": "bob", "unattended": False,
               "model": "claude-opus-5-5", "system_prompt": "SYS", "max_turns": 400, "timeout_s": 5400,
               "resume_session_id": None, "test_secrets": {"SAMPLE_TEST_USER": "tester@example.com"},
               "project": "sample"}
    payload.update(payload_over)
    return {"id": f"W-{conversation_id}", "conversation_id": conversation_id, "kind": "turn", "payload": payload}


def make_runner(tmp_path, link, claude, **kw):
    return Runner(link, claude, bundles_dir=tmp_path / "bundles", claude_home=tmp_path / "claude",
                  scratch_root=tmp_path / "scratch", heartbeat_s=0.05, **kw)


async def test_handle_builds_bypass_spec_and_reports(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, link, claude).handle(item(), "tok", [])
    spec = claude.specs[0]
    assert spec.model == "claude-opus-5-5"
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
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


def test_memory_block_carries_the_rules_and_the_index(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [Reply style](people-ivan-replies.md) — short replies\n")
    text = memory_block(tmp_path)
    assert str(tmp_path) in text and "One fact per file" in text and "people-ivan-replies.md" in text


def test_an_empty_memory_says_so(tmp_path):
    assert "(empty)" in memory_block(tmp_path)


def test_a_long_index_is_cut_and_says_how_much(tmp_path):
    (tmp_path / "MEMORY.md").write_text("".join(f"- line {i}\n" for i in range(250)))
    text = memory_block(tmp_path, max_lines=200)
    assert "- line 199" in text and "- line 200" not in text and "50 more lines" in text


def test_invalid_utf8_in_the_index_does_not_crash_and_keeps_the_good_line(tmp_path):
    """A concurrent write (or a hand edit) can leave MEMORY.md with invalid UTF-8 bytes mid-file; memory_block must
    not raise, and should still surface the lines that do read cleanly."""
    (tmp_path / "MEMORY.md").write_bytes(b"- good line\n\xff\xfe bad bytes\n- another good line\n")
    text = memory_block(tmp_path)
    assert "- good line" in text and "- another good line" in text


async def test_every_session_gets_the_memory_block(tmp_path):
    claude = FakeClaude()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("- [Reply style](people-ivan-replies.md) — short replies\n")
    runner = make_runner(tmp_path, FakeLink(), claude, memory_dir=memory_dir)
    await runner.handle(item(), "tok", [])
    prompt = claude.specs[0].system_prompt
    assert prompt.startswith("SYS") and "people-ivan-replies.md" in prompt and "One fact per file" in prompt


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

        def release(self, run_id):
            pass

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


async def test_a_turn_stopped_once_runs_when_this_runner_claims_it_again(tmp_path):
    """A stop belongs to the claim it came with. When a turn's lease runs out and core hands it to the same runner
    again, the new attempt runs instead of returning "interrupted" before it starts."""
    clients, scripts = [], [[HANG], [echo_prompt, result()]]

    def factory(options):
        clients.append(FakeClient(options, scripts[len(clients)]))
        return clients[-1]

    claude = ClaudeRunner(FakeProxy(), "http://172.30.0.1:28091", tmp_path, client_factory=factory,
                          uuid_factory=sids().__next__)
    link = FakeLink(cancel=True)
    runner = make_runner(tmp_path, link, claude)
    await runner.handle(item(), "tok-1", [])
    link.cancel = False
    await runner.handle(item(), "tok-2", [])
    assert [r["outcome"] for r in link.results] == ["interrupted", "done"]


async def test_heartbeat_reports_the_live_session_and_what_was_read(tmp_path):
    link, claude = FakeLink(), FakeClaude(hang=True)
    claude.read = ["W-2"]
    runner = make_runner(tmp_path, link, claude)
    task = asyncio.create_task(runner.handle(item(), "tok", []))
    await wait_until(lambda: link.beats)
    assert link.beats[0] == {"session_id": "live-1", "account": "maple", "read": ["W-2"],
                             "accepts_messages": True}
    await claude.interrupt("W-V-1")
    await asyncio.wait_for(task, 2)


async def test_messages_from_the_heartbeat_go_into_the_session(tmp_path):
    link, claude = FakeLink(), FakeClaude(hang=True)
    link.messages = [[{"id": "W-2", "payload": {"text": "how's it going?",
                                                "origin": "[from Sam (sam) · DM · <link>]"}}]]
    runner = make_runner(tmp_path, link, claude)
    task = asyncio.create_task(runner.handle(item(), "tok", []))
    await wait_until(lambda: claude.sent)
    assert claude.sent == [("W-V-1", "W-2", "[from Sam (sam) · DM · <link>]\n" + MIDTURN_NOTE + "how's it going?")]
    await claude.interrupt("W-V-1")
    await asyncio.wait_for(task, 2)


async def test_attached_files_are_saved_and_named_in_the_prompt(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    link.slack_files = {"F1": b"png-bytes"}
    files = [{"id": "F1", "name": "../shot.png", "mimetype": "image/png", "size": 2048},
             {"id": "F2", "name": "gone.pdf", "mimetype": "application/pdf", "size": 10}]
    await make_runner(tmp_path, link, claude).handle(item(text="what's this?", files=files), "tok", [])
    saved = tmp_path / "scratch" / "V-1" / "slack-files" / "F1" / "shot.png"
    assert saved.read_bytes() == b"png-bytes"
    prompt = claude.specs[0].prompt
    assert prompt.startswith("what's this?\n\n(The message came with these files attached")
    assert f"- {saved} (image/png, 2 KB)" in prompt
    assert "- gone.pdf: couldn't be fetched (HTTPStatusError" in prompt


async def test_a_message_without_files_gets_no_attachment_note(tmp_path):
    claude = FakeClaude()
    await make_runner(tmp_path, FakeLink(), claude).handle(item(), "tok", [])
    assert claude.specs[0].prompt == "fix the export"


async def test_files_on_a_message_arriving_midturn_are_saved_too(tmp_path):
    link, claude = FakeLink(), FakeClaude(hang=True)
    link.slack_files = {"F9": b"img"}
    link.messages = [[{"id": "W-2", "payload": {"text": "", "files": [{"id": "F9", "name": "a.png"}]}}]]
    runner = make_runner(tmp_path, link, claude)
    task = asyncio.create_task(runner.handle(item(), "tok", []))
    await wait_until(lambda: claude.sent)
    saved = tmp_path / "scratch" / "V-1" / "slack-files" / "F9" / "a.png"
    assert saved.read_bytes() == b"img" and f"- {saved}" in claude.sent[0][2]
    await claude.interrupt("W-V-1")
    await asyncio.wait_for(task, 2)


async def test_result_reports_every_message_the_session_read(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    claude.read = ["W-2", "W-3"]
    await make_runner(tmp_path, link, claude).handle(item(), "tok", [])
    assert link.results[-1]["read"] == ["W-2", "W-3"]


async def test_a_restarted_turn_is_told_and_resumes(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    it = item(resume_session_id="live-1", origin="[from bob · DM]")
    it["attempts"] = 1
    await make_runner(tmp_path, link, claude).handle(it, "tok", [])
    assert claude.specs[0].prompt == "[from bob · DM]\n" + RESTARTED_NOTE + "fix the export"
    assert claude.specs[0].resume_session_id == "live-1"


async def test_a_session_resumes_when_it_stays_on_its_account(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, link, claude).handle(item(resume_session_id="sess-1", resume_account="maple"),
                                                     "tok", [])
    assert claude.resume_checks == [("sess-1", "maple")]
    assert claude.specs[0].resume_session_id == "sess-1" and claude.specs[0].prompt == "fix the export"


async def test_a_session_that_would_land_on_another_account_hands_off_to_a_fresh_one(tmp_path):
    origin = "[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]"
    log = tmp_path / "claude" / "projects" / "-var-agent-scratch-V-1" / "sess-1.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")
    link, claude = FakeLink(), FakeClaude()
    claude.resumable = False
    it = item(resume_session_id="sess-1", resume_account="maple", origin=origin)
    it["attempts"] = 1  # a restarted turn: the fresh session doesn't pick up where it stopped, so no restart note
    await make_runner(tmp_path, link, claude).handle(it, "tok", [])
    assert claude.resume_checks == [("sess-1", "maple")]
    assert len(claude.specs) == 1 and claude.specs[0].resume_session_id is None  # never resumed elsewhere
    prompt = claude.specs[0].prompt
    assert prompt == f"{origin}\n{handoff_note('sess-1', tmp_path / 'claude')}fix the export"
    assert prompt.splitlines()[0] == origin  # the who/where/link line stays first
    assert "sess-1" in prompt and str(log) in prompt and "read_thread" in prompt and RESTARTED_NOTE not in prompt


async def test_the_handoff_inside_a_run_keeps_the_origin_first(tmp_path):
    origin = "[from bob · DM]"
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, link, claude).handle(item(origin=origin), "tok", [])
    transcript = tmp_path / "transcripts" / "W-V-1.jsonl"
    prompt = claude.specs[0].handoff("sess-1", transcript)
    assert prompt == f"{origin}\n{handoff_note('sess-1', tmp_path / 'claude', transcript)}fix the export"
    assert str(transcript) in prompt


async def test_a_first_attempt_gets_no_restart_note(tmp_path):
    link, claude = FakeLink(), FakeClaude()
    it = item(resume_session_id="live-1")
    it["attempts"] = 0
    await make_runner(tmp_path, link, claude).handle(it, "tok", [])
    assert claude.specs[0].prompt == "fix the export"


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
    posted = {}

    def handler(request):
        if request.url.path == "/agent/pull":
            return httpx.Response(204)
        if request.url.path == "/agent/heartbeat":
            posted["beat"] = json.loads(request.content)
            return httpx.Response(200, json={"cancel": False, "messages": []})
        return httpx.Response(401, json={"error": "unauthorized"})

    link = CoreLink("http://core.test", "runner-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await link.pull() is None
    beat = {"session_id": None, "read": [], "accepts_messages": False}
    assert await link.heartbeat("tok", beat) == {"cancel": False, "messages": []}
    assert posted["beat"] == beat
    with pytest.raises(SessionRevoked):
        await link.event("tok", {})


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


async def test_a_restarted_turn_whose_session_is_also_dead_gets_only_the_fresh_note(tmp_path):
    """A restarted attempt's prompt carries RESTARTED_NOTE. If its resumed session also turns out to be gone, the
    fresh fallback must drop that note, not stack it under FRESH_NOTE: the two make contradictory claims."""
    origin = "[from bob · DM]"
    link, claude = FakeLink(), ResumeFailsClaude()
    it = item(resume_session_id="sess-gone", origin=origin)
    it["attempts"] = 1
    await make_runner(tmp_path, link, claude).handle(it, "tok", [])
    assert claude.specs[0].prompt == f"{origin}\n{RESTARTED_NOTE}fix the export"
    assert claude.specs[1].prompt == f"{origin}\n{FRESH_NOTE}fix the export"
    assert RESTARTED_NOTE not in claude.specs[1].prompt


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


async def test_every_conversation_shares_one_claude_home(tmp_path):
    """What the agent installs in its Claude home (skills, plugins, MCP servers) is there in every later session."""
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, link, claude)
    await runner.handle(item(), "tok", [])
    await runner.handle(item(conversation_id="V-2"), "tok", [])
    assert [s.env["CLAUDE_CONFIG_DIR"] for s in claude.specs] == [str(tmp_path / "claude")] * 2
    assert (tmp_path / "claude").is_dir()


async def test_forced_env_keys_cannot_be_overridden(tmp_path):
    """CLAUDE_CONFIG_DIR and the function-hooks flag are forced last: neither the container's extra env nor a
    pack's test secrets can override them, even when both try to."""
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, link, claude, extra_env={"CLAUDE_CONFIG_DIR": "/from-extra-env",
                                                            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"})
    await runner.handle(item(test_secrets={"CLAUDE_CONFIG_DIR": "/from-test-secrets",
                                           "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"}), "tok", [])
    spec = claude.specs[0]
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
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
