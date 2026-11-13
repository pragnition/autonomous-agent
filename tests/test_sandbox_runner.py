import asyncio
import io
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from agent.runner.base import RunResult
from agent.sandbox.link import CoreLink, SessionRevoked
from agent.sandbox.remote_tools import remote_sdk_tools
from agent.sandbox.runner import SandboxRunner, SessionUsers
from agent.sandbox.worktrees import Worktrees


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def server(tmp_path, git_env):
    """A bare repo standing in for core's git endpoint, plus the file:// base the runner clones from."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "app.py").write_text("print('hi')\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "init")
    bare = tmp_path / "srv" / "git" / "sample.git"
    bare.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    return f"file://{tmp_path / 'srv'}", bare


@pytest.fixture
def worktrees(tmp_path, server):
    return Worktrees(tmp_path / "var", server[0], token_for_git=lambda: "sandbox-token")


def bundle_bytes() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name in ("system.md", "plugins/core/.claude-plugin/plugin.json", "plugins/pack/.claude-plugin/plugin.json"):
            data = b"{}"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


class FakeLink:
    def __init__(self, cancel=False, revoked=False, file=None):
        self.cancel, self.revoked, self.file = cancel, revoked, file
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
        return {"text": f"core ran {name}", "file": self.file}

    async def files(self, tok, items, text):
        self.files_sent.append((items, text))
        return self.posted

    async def result(self, tok, result):
        self.results.append(result)

    async def bundle(self, profile, project):
        return "v1", bundle_bytes()


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


def item(profile="code", conversation_id="V-1", **payload_over):
    payload = {"text": "fix the export", "actor": "bob", "unattended": False, "profile": profile,
               "model": "claude-opus-5-5", "system_prompt": "SYS", "max_turns": 400, "timeout_s": 5400,
               "resume_session_id": None, "test_secrets": {"SAMPLE_TEST_USER": "tester@example.com"},
               "project": "sample",
               "worktree": {"project": "sample", "default_branch": "main", "branch_hint": "agent/T-0001-fix-export",
                            "setup": ["echo ran >> .setup-count"], "checks": ["true"]}}
    payload.update(payload_over)
    return {"id": f"W-{conversation_id}", "conversation_id": conversation_id, "kind": "turn", "payload": payload}


def make_runner(tmp_path, worktrees, link, claude, profile="code"):
    return SandboxRunner(link, worktrees, SessionUsers(), claude, sandbox=f"{profile}-sample", profile=profile,
                         bundles_dir=tmp_path / "bundles", sessions_root=tmp_path / "sessions",
                         scratch_root=tmp_path / "scratch", heartbeat_s=0.05, run_as_root=False)


async def test_handle_builds_bypass_spec_and_reports(tmp_path, worktrees):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(item(), "tok", [])
    spec = claude.specs[0]
    assert spec.permission_mode == "bypass" and spec.model == "claude-opus-5-5"
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "V-1")
    assert spec.env["SAMPLE_TEST_USER"] == "tester@example.com"
    assert git(spec.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T-0001-fix-export"
    assert [p.name for p in spec.plugins] == ["core", "pack"] and spec.system_prompt == "SYS"
    assert link.results[-1]["text"] == "all done" and link.results[-1]["session_id"] == "sess-1"
    assert link.results[-1]["outcome"] == "done" and link.results[-1]["cost_usd_est"] == 0.5


async def test_setup_runs_once_per_worktree(tmp_path, worktrees):
    runner = make_runner(tmp_path, worktrees, FakeLink(), FakeClaude())
    await runner.handle(item(), "tok", [])
    await runner.handle(item(), "tok", [])
    wt = tmp_path / "var" / "work" / "V-1"
    assert (wt / ".setup-count").read_text().count("ran") == 1


async def test_debug_gets_scratch_dir_with_read_only_repo(tmp_path, worktrees):
    claude = FakeClaude()
    payload = {"worktree": {"project": "sample", "default_branch": "main", "read_only": True}, "test_secrets": {}}
    await make_runner(tmp_path, worktrees, FakeLink(), claude, profile="debug").handle(item("debug", **payload), "t", [])
    cwd = claude.specs[0].cwd
    assert cwd == tmp_path / "scratch" / "V-1" and (cwd / "repo" / "app.py").exists()


async def test_cancel_via_heartbeat_interrupts(tmp_path, worktrees):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, worktrees, FakeLink(cancel=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


async def test_revoked_token_interrupts(tmp_path, worktrees):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, worktrees, FakeLink(revoked=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


def test_session_users_sticky_and_distinct():
    users = SessionUsers(("s1", "s2"))
    assert users.acquire("a") == "s1" and users.acquire("a") == "s1" and users.acquire("b") == "s2"
    with pytest.raises(RuntimeError):
        users.acquire("c")
    users.release("a")
    assert users.acquire("c") == "s1"


MANIFEST = [{"name": "open_pr", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "post_files", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "az", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "status", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


async def call(tools, name, args):
    tool = next(t for t in tools if t.name == name)
    return (await tool.handler(args))["content"][0]["text"]


@pytest.fixture
def worktree(worktrees):
    path, _ = worktrees.prepare("V-9", "sample", "agent/T-0009-x", "main")
    return path


def tools_for(link, worktrees, worktree, checks=("true",)):
    return remote_sdk_tools(MANIFEST, link, "tok", worktree=worktree, user=None, checks=list(checks),
                            branch="agent/T-0009-x", worktrees=worktrees)


async def test_generic_tool_forwards(worktrees, worktree):
    link = FakeLink()
    assert await call(tools_for(link, worktrees, worktree), "status", {}) == "core ran status"
    assert link.tool_calls == [("status", {})]


async def test_post_files_outside_worktree_refused(worktrees, worktree):
    link = FakeLink()
    assert "outside" in await call(tools_for(link, worktrees, worktree), "post_files",
                                   {"paths": ["/etc/passwd"], "text": ""})
    assert "outside" in await call(tools_for(link, worktrees, worktree), "post_files",
                                   {"paths": ["../../../etc/passwd"], "text": ""})
    assert link.files_sent == []


async def test_post_files_sends_worktree_files(worktrees, worktree):
    link = FakeLink()
    (worktree / "shot.png").write_bytes(b"png")
    await call(tools_for(link, worktrees, worktree), "post_files", {"paths": ["shot.png"], "text": "after the fix"})
    assert link.files_sent == [([("shot.png", b"png")], "after the fix")]


async def test_post_files_says_when_nothing_reached_slack(worktrees, worktree):
    link = FakeLink()
    link.posted = False
    (worktree / "shot.png").write_bytes(b"png")
    reply = await call(tools_for(link, worktrees, worktree), "post_files", {"paths": ["shot.png"], "text": "t"})
    assert "no Slack thread" in reply


async def test_open_pr_blocks_on_failed_checks(worktrees, worktree, server):
    link = FakeLink()
    await call(tools_for(link, worktrees, worktree, checks=["false"]), "open_pr", {"title": "t", "body": "b"})
    name, args = link.tool_calls[-1]
    assert name == "open_pr" and args["checks"]["passed"] is False
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/heads/agent/T-0009-x"], cwd=server[1],
                          capture_output=True).returncode != 0


async def test_open_pr_commits_pushes_and_forwards_sha(worktrees, worktree, server):
    link = FakeLink()
    (worktree / "fix.py").write_text("x = 1\n")
    await call(tools_for(link, worktrees, worktree), "open_pr", {"title": "Fix export", "body": "b"})
    _, args = link.tool_calls[-1]
    assert args["checks"]["passed"] is True and args["branch"] == "agent/T-0009-x"
    assert args["sha"] == git(server[1], "rev-parse", "refs/heads/agent/T-0009-x")
    assert git(worktree, "log", "-1", "--format=%s") == "Fix export"


async def test_saved_file_lands_in_agent_out(worktrees, worktree):
    link = FakeLink(file={"name": "logs.json", "content_b64": "eyJhIjogMX0="})
    text = await call(tools_for(link, worktrees, worktree), "az", {"args": ["x"], "save_to": "logs.json"})
    assert (worktree / ".agent-out" / "logs.json").read_text() == '{"a": 1}'
    assert "saved to .agent-out/logs.json" in text


async def test_saved_file_with_bad_name_refused(worktrees, worktree):
    link = FakeLink(file={"name": "../../x", "content_b64": "eA=="})
    text = await call(tools_for(link, worktrees, worktree), "az", {"args": ["x"]})
    assert "refused" in text and not (worktree.parent / "x").exists()


async def test_structured_submit_captured(tmp_path, worktrees):
    claude = FakeClaude()
    payload = {"structured": {"type": "object", "properties": {"episode": {"type": "object"}}}, "worktree": None,
               "test_secrets": {}}
    runner = make_runner(tmp_path, worktrees, FakeLink(), claude, profile="general")
    original = claude.run

    async def run_and_submit(spec):
        submit = next(t for t in spec.sdk_tools if t.name == "submit_result")
        await submit.handler({"episode": {"summary": "s"}})
        return await original(spec)

    claude.run = run_and_submit
    link = runner.link
    await runner.handle(item("general", **payload), "tok", [])
    assert link.results[-1]["structured"] == {"episode": {"summary": "s"}}


async def test_link_maps_401_and_204():
    def handler(request):
        if request.url.path == "/agent/pull":
            return httpx.Response(204)
        return httpx.Response(401, json={"error": "unauthorized"})

    link = CoreLink("http://core.test", "sbx-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await link.pull("code-sample") is None
    with pytest.raises(SessionRevoked):
        await link.heartbeat("tok")


class ResumeFailsClaude(FakeClaude):
    async def run(self, spec):
        self.specs.append(spec)
        if spec.resume_session_id:
            return RunResult(spec.run_id, None, "error", error="No conversation found with session ID")
        return RunResult(spec.run_id, "sess-new", "done", summary="caught up")


async def test_dead_session_falls_back_to_a_fresh_one(tmp_path, worktrees):
    link, claude = FakeLink(), ResumeFailsClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(item(resume_session_id="sess-gone"), "tok", [])
    assert [s.resume_session_id for s in claude.specs] == ["sess-gone", None]
    assert "read_thread" in claude.specs[1].prompt and claude.specs[1].prompt.endswith("fix the export")
    assert link.results[-1]["outcome"] == "done" and link.results[-1]["session_id"] == "sess-new"


async def test_preparation_failure_reports_an_error(tmp_path, worktrees):
    link = FakeLink()
    bad = item()
    bad["payload"]["worktree"]["default_branch"] = "no-such-branch"
    await make_runner(tmp_path, worktrees, link, FakeClaude()).handle(bad, "tok", [])
    assert link.results[-1]["outcome"] == "error" and "no-such-branch" in link.results[-1]["error"]


async def test_runner_exits_when_its_token_is_revoked(tmp_path, worktrees):
    class RevokedLink(FakeLink):
        async def pull(self, sandbox):
            raise SessionRevoked()

    runner = make_runner(tmp_path, worktrees, RevokedLink(), FakeClaude())
    await asyncio.wait_for(runner.run_forever(), 2)


def test_conversation_gets_its_own_repo_with_shared_objects(worktrees, tmp_path):
    path, created = worktrees.prepare("V-5", "sample", "agent/T-5-x", "main")
    assert created and (path / ".git").is_dir()
    assert (path / ".git" / "objects" / "info" / "alternates").exists()
    assert git(path, "remote", "get-url", "origin").endswith("/git/sample.git")


def test_scratch_files_are_never_committed(worktrees):
    path, _ = worktrees.prepare("V-6", "sample", "agent/T-6-x", "main")
    (path / ".agent-progress.md").write_text("notes")
    (path / ".agent-out").mkdir()
    (path / ".agent-out" / "logs.json").write_text("{}")
    assert git(path, "status", "--porcelain") == ""


async def test_commit_step_never_sees_the_sandbox_token(worktrees, worktree):
    seen = []

    async def shell(cmd, cwd, user, timeout_s=0, env=None):
        seen.append((cmd, env))
        return 0, ""

    tools = remote_sdk_tools(MANIFEST, FakeLink(), "tok", worktree=worktree, user=None, checks=[],
                             branch="agent/T-0009-x", worktrees=worktrees, shell=shell)
    await call(tools, "open_pr", {"title": "t", "body": "b"})
    commit_env = seen[0][1]
    assert "commit" in seen[0][0] and not any("sandbox-token" in str(v) for v in commit_env.values())


async def test_open_runner_runs_sessions_as_itself(tmp_path, worktrees):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(item(), "tok", [])
    assert claude.specs[0].os_user is None


async def test_extra_env_reaches_the_session(tmp_path, worktrees):
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, worktrees, link, claude)
    runner.extra_env = {"GH_TOKEN": "ghp_abc"}
    await runner.handle(item(), "tok", [])
    assert claude.specs[0].env["GH_TOKEN"] == "ghp_abc"
    assert claude.specs[0].env["SAMPLE_TEST_USER"] == "tester@example.com"


LINEAR_MCP = {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                         "headers": {"Authorization": "Bearer ${LINEAR_APP_TOKEN}"}}}


def env_runner(tmp_path, worktrees, claude, env_file):
    return SandboxRunner(FakeLink(), worktrees, SessionUsers(), claude, sandbox="general", profile="general",
                         bundles_dir=tmp_path / "bundles", sessions_root=tmp_path / "sessions",
                         scratch_root=tmp_path / "scratch", heartbeat_s=0.05, run_as_root=False,
                         env_files=[env_file])


async def test_mcp_headers_use_current_linear_token(tmp_path, worktrees):
    claude, env_file = FakeClaude(), tmp_path / "linear.env"
    runner = env_runner(tmp_path, worktrees, claude, env_file)
    for n, token in enumerate(("tok-1", "tok-2")):
        env_file.write_text(f"LINEAR_APP_TOKEN={token}\n")
        await runner.handle(item("general", conversation_id=f"V-{n}", worktree=None, mcp=LINEAR_MCP), "tok", [])
    assert [s.mcp_servers["linear"]["headers"]["Authorization"] for s in claude.specs] == \
        ["Bearer tok-1", "Bearer tok-2"]
    assert claude.specs[1].mcp_servers["linear"]["url"] == "https://mcp.linear.app/mcp"
    assert claude.specs[1].env["LINEAR_APP_TOKEN"] == "tok-2"   # for agent activities via the API


async def test_unset_variable_drops_the_server(tmp_path, worktrees, caplog):
    claude = FakeClaude()
    await env_runner(tmp_path, worktrees, claude, tmp_path / "missing.env").handle(
        item("general", worktree=None, mcp=LINEAR_MCP), "tok", [])
    assert claude.specs[0].mcp_servers == {}
    assert "LINEAR_APP_TOKEN" in caplog.text
