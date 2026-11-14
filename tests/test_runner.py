import asyncio
import io
import json
import stat
import subprocess
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from agent import db
from agent.core.conversations import bind_project, create_conversation
from agent.core.payloads import Payloads
from agent.gitutil import GitError, branch_name
from agent.runner.base import RunResult
from agent.runner.link import CoreLink, SessionRevoked
from agent.runner.tools import Checkout, remote_sdk_tools, run_setup, setup_marker
from agent.runner.worker import FRESH_NOTE, Runner
from agent.runner.worktrees import CheckoutRefused, Worktrees
from agent_fixtures import make_config, make_pack


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
    return Worktrees(tmp_path / "var", server[0], token_for_git=lambda: "runner-token")


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


BOUND_BRANCH = "agent/V-9-run-sample"


class FakeLink:
    def __init__(self, cancel=False, revoked=False):
        self.cancel, self.revoked = cancel, revoked
        self.tool_calls, self.files_sent, self.results, self.events, self.binds = [], [], [], [], []
        self.posted = True
        # what core's bind answers for `sample` (Payloads.checkout plus the pack's test keys)
        self.bound = {"project": "sample", "default_branch": "main", "branch": BOUND_BRANCH,
                      "setup": ["echo ran >> .setup-count"], "checks": ["true"],
                      "test_secrets": {"SAMPLE_TEST_USER": "tester@example.com"}}

    async def bind(self, tok, project, title):
        self.binds.append((project, title))
        if project != "sample":
            return {"error": f"unknown project '{project}' (known: sample)"}
        return dict(self.bound)

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
               "project": "sample", "workdir": "checkout",
               "worktree": {"project": "sample", "default_branch": "main", "branch": "agent/T-0001-fix-export",
                            "setup": ["echo ran >> .setup-count"], "checks": ["true"]}}
    payload.update(payload_over)
    return {"id": f"W-{conversation_id}", "conversation_id": conversation_id, "kind": "turn", "payload": payload}


def make_runner(tmp_path, worktrees, link, claude):
    return Runner(link, worktrees, claude, bundles_dir=tmp_path / "bundles", sessions_root=tmp_path / "sessions",
                  scratch_root=tmp_path / "scratch", heartbeat_s=0.05)


async def test_handle_builds_bypass_spec_and_reports(tmp_path, worktrees):
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(item(), "tok", [])
    spec = claude.specs[0]
    assert spec.model == "claude-opus-5-5"
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "V-1")
    assert spec.env["SAMPLE_TEST_USER"] == "tester@example.com"
    assert git(spec.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T-0001-fix-export"
    assert spec.env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1"
    assert [p.name for p in spec.plugins] == ["core", "effortless", "a", "b"] and spec.system_prompt == "SYS"
    assert link.results[-1]["text"] == "all done" and link.results[-1]["session_id"] == "sess-1"
    assert link.results[-1]["outcome"] == "done" and link.results[-1]["cost_usd_est"] == 0.5


async def test_every_session_shares_one_claude_code_memory(tmp_path, worktrees):
    claude = FakeClaude()
    runner = Runner(FakeLink(), worktrees, claude, bundles_dir=tmp_path / "bundles",
                    sessions_root=tmp_path / "sessions", scratch_root=tmp_path / "scratch", heartbeat_s=0.05,
                    memory_dir=tmp_path / "memory")
    await runner.handle(item("V-1"), "tok", [])
    await runner.handle(item("V-2", worktree=None, workdir="session"), "tok", [])
    assert [s.settings for s in claude.specs] == [{"autoMemoryDirectory": str(tmp_path / "memory")}] * 2
    assert (tmp_path / "memory").is_dir()


async def test_prompt_starts_with_the_messages_origin(tmp_path, worktrees):
    claude = FakeClaude()
    runner = make_runner(tmp_path, worktrees, FakeLink(), claude)
    await runner.handle(item("V-1", worktree=None, workdir="session",
                             origin="[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]"), "tok", [])
    await runner.handle(item("V-2", worktree=None, workdir="session"), "tok", [])
    assert claude.specs[0].prompt == "[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]\nfix the export"
    assert claude.specs[1].prompt == "fix the export"


async def test_runner_loads_core_effortless_and_all_pack_plugins(tmp_path, worktrees):
    runner = make_runner(tmp_path, worktrees, FakeLink(), FakeClaude())
    plugins = await runner._bundle()
    assert [p.name for p in plugins] == ["core", "effortless", "a", "b"]


async def test_runner_runs_ten_conversations_at_once(tmp_path, worktrees):
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

    runner = make_runner(tmp_path, worktrees, FakeLink(), BlockingClaude())
    items = [item(conversation_id=f"V-{i}", worktree=None) for i in range(n)]
    handles = asyncio.gather(*(runner.handle(it, f"tok-{i}", []) for i, it in enumerate(items)))
    await asyncio.wait_for(all_in.wait(), 2)
    assert len(entered) == n
    release.set()
    await asyncio.wait_for(handles, 2)


async def test_setup_runs_once_per_worktree(tmp_path, worktrees):
    runner = make_runner(tmp_path, worktrees, FakeLink(), FakeClaude())
    await runner.handle(item(), "tok", [])
    await runner.handle(item(), "tok", [])
    wt = tmp_path / "scratch" / "V-1" / "sample"
    assert (wt / ".setup-count").read_text().count("ran") == 1


async def test_session_workdir_checkout_lives_inside_session_folder(tmp_path, worktrees):
    runner = make_runner(tmp_path, worktrees, FakeLink(), FakeClaude())
    cwd, checkout, note = await runner._workdir(tmp_path / "scratch" / "V-1", item(workdir="session")["payload"])
    assert cwd == tmp_path / "scratch" / "V-1" and note == ""
    assert checkout == tmp_path / "scratch" / "V-1" / "sample"
    assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T-0001-fix-export"
    assert (checkout / ".setup-count").read_text().count("ran") == 1


async def test_checkout_workdir_runs_in_checkout(tmp_path, worktrees):
    claude = FakeClaude()
    await make_runner(tmp_path, worktrees, FakeLink(), claude).handle(item(workdir="checkout"), "tok", [])
    assert claude.specs[0].cwd == tmp_path / "scratch" / "V-1" / "sample"


async def test_no_checkout_runs_in_session_folder(tmp_path, worktrees):
    cwd, checkout, note = await make_runner(tmp_path, worktrees, FakeLink(), FakeClaude())._workdir(
        tmp_path / "scratch" / "V-1", item(worktree=None, workdir="session")["payload"])
    assert (cwd, checkout, note) == (tmp_path / "scratch" / "V-1", None, "") and cwd.is_dir()


async def test_switched_branch_checkout_is_reused_next_turn(tmp_path, worktrees):
    """The checkout is the agent's to work in: a branch it switched to doesn't stop the next turn using it."""
    claude = FakeClaude()
    runner = make_runner(tmp_path, worktrees, FakeLink(), claude)
    await runner.handle(item(workdir="checkout"), "tok", [])
    checkout = tmp_path / "scratch" / "V-1" / "sample"
    git(checkout, "checkout", "-q", "-b", "other")
    await runner.handle(item(workdir="checkout"), "tok", [])
    assert [s.cwd for s in claude.specs] == [checkout, checkout]
    assert claude.specs[1].prompt == "fix the export"
    assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "other"
    assert (checkout / ".setup-count").read_text().count("ran") == 1


async def test_foreign_clone_is_refused_and_the_turn_runs_without_it(tmp_path, worktrees, server):
    """A repo at the checkout's path that prepare didn't make is left alone, even a clone of the right repo on the
    right branch. The turn still runs, in the session folder, and the prompt says why there is no checkout."""
    session = tmp_path / "scratch" / "V-1"
    path = session / "sample"
    path.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "-b", "main", f"{server[0]}/git/sample.git", str(path))
    git(path, "checkout", "-q", "-b", "agent/T-0001-fix-export")
    (path / "mine.txt").write_text("keep me")
    link, claude = FakeLink(), FakeClaude()
    original = claude.run

    async def run_and_open_pr(spec):
        claude.open_pr = await call(spec.sdk_tools, "open_pr", {"title": "t", "body": "b"})
        link.bound["branch"] = "agent/T-0001-fix-export"
        claude.work_on = await call(spec.sdk_tools, "work_on", {"project": "sample"})
        return await original(spec)

    claude.run = run_and_open_pr
    await make_runner(tmp_path, worktrees, link, claude).handle(item(workdir="checkout"), "tok", MANIFEST)
    spec = claude.specs[0]
    assert link.results[-1]["outcome"] == "done" and spec.cwd == session
    note, text = spec.prompt.split("\n\n", 1)
    assert str(path) in note and "work_on(sample)" in note and text == "fix the export"
    assert claude.open_pr == "call work_on(<project>) first"
    assert "couldn't be made" in claude.work_on and "didn't make" in claude.work_on
    assert (path / "mine.txt").read_text() == "keep me" and not (path / ".setup-count").exists()
    assert git(path, "status", "--porcelain") == "?? mine.txt"


async def test_failed_upfront_setup_is_rerun_by_work_on(tmp_path, worktrees):
    """Setup that fails before the first turn of a conversation bound up front leaves the same marker work_on
    leaves, so the agent's work_on runs setup again."""
    fixed, marker = tmp_path / "fixed", tmp_path / "scratch" / "V-1" / ".setup-failed-sample"
    setup = [f"if [ -e {fixed} ]; then echo ran >> .setup-count; else touch {fixed}; echo broken; exit 1; fi"]
    link = FakeLink()
    link.bound.update(branch="agent/T-0001-fix-export", setup=setup)
    seen = {}

    class WorkOnClaude(FakeClaude):
        async def run(self, spec):
            seen["marker before work_on"] = marker.exists()
            seen["reply"] = await call(spec.sdk_tools, "work_on", {"project": "sample"})
            return await super().run(spec)

    up_front = item()
    up_front["payload"]["worktree"]["setup"] = setup
    await make_runner(tmp_path, worktrees, link, WorkOnClaude()).handle(up_front, "tok", MANIFEST)
    assert seen["marker before work_on"] is True
    assert "setup passed" in seen["reply"] and not marker.exists()
    assert (tmp_path / "scratch" / "V-1" / "sample" / ".setup-count").read_text().count("ran") == 1


async def test_failed_upfront_setup_is_told_at_the_top_of_the_prompt(tmp_path, worktrees):
    """The session still starts in its checkout, but its prompt opens with what failed and how to run it again."""
    up_front = item()
    up_front["payload"]["worktree"]["setup"] = ["echo 'pip: no matching distribution for leftpad'; exit 1"]
    link, claude = FakeLink(), FakeClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(up_front, "tok", [])
    spec, checkout = claude.specs[0], tmp_path / "scratch" / "V-1" / "sample"
    assert link.results[-1]["outcome"] == "done" and spec.cwd == checkout
    assert spec.prompt.startswith(f"(Setup failed in {checkout}")
    note, text = spec.prompt.rsplit("\n\n", 1)
    assert "no matching distribution for leftpad" in note and "work_on(sample)" in note and text == "fix the export"
    assert (tmp_path / "scratch" / "V-1" / ".setup-failed-sample").exists()


async def test_setup_marker_is_there_while_setup_runs_and_gone_once_it_passes(tmp_path, worktrees):
    """A runner that dies partway through setup leaves the marker behind; one that finishes it removes it."""
    up_front = item()
    up_front["payload"]["worktree"]["setup"] = ["test -e ../.setup-failed-sample && echo ran >> .setup-count"]
    claude = FakeClaude()
    await make_runner(tmp_path, worktrees, FakeLink(), claude).handle(up_front, "tok", [])
    checkout = tmp_path / "scratch" / "V-1" / "sample"
    assert (checkout / ".setup-count").read_text() == "ran\n"
    assert not (tmp_path / "scratch" / "V-1" / ".setup-failed-sample").exists()
    assert claude.specs[0].prompt == "fix the export"


async def test_interrupted_setup_is_rerun_by_work_on(worktrees, session):
    """Setup that never finished (the runner stopped partway) is run again by the next work_on."""
    path, _ = worktrees.prepare(session / "sample", "sample", BOUND_BRANCH, "main")
    marker = setup_marker(session, "sample")

    async def stops(cmd, cwd, timeout_s, env):
        raise RuntimeError("the runner stopped")

    with pytest.raises(RuntimeError):
        await run_setup(["echo ran >> .setup-count"], path, marker, shell=stops)
    assert marker.exists()
    tools, _ = unbound_tools(FakeLink(), worktrees, session)
    reply = await call(tools, "work_on", {"project": "sample"})
    assert "setup passed" in reply and not marker.exists()
    assert (path / ".setup-count").read_text().count("ran") == 1


async def test_cancel_via_heartbeat_interrupts(tmp_path, worktrees):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, worktrees, FakeLink(cancel=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


async def test_revoked_token_interrupts(tmp_path, worktrees):
    claude = FakeClaude(hang=True)
    await make_runner(tmp_path, worktrees, FakeLink(revoked=True), claude).handle(item(), "tok", [])
    assert claude.interrupted == ["W-V-1"]


MANIFEST = [{"name": "open_pr", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "work_on", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "post_files", "description": "d", "input_schema": {"type": "object", "properties": {}}},
            {"name": "status", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


async def call(tools, name, args):
    tool = next(t for t in tools if t.name == name)
    return (await tool.handler(args))["content"][0]["text"]


@pytest.fixture
def worktree(worktrees, tmp_path):
    path, _ = worktrees.prepare(tmp_path / "scratch" / "V-9" / "sample", "sample", "agent/T-0009-x", "main")
    return path


def tools_for(link, worktrees, worktree, checks=("true",)):
    return remote_sdk_tools(MANIFEST, link, "tok", session_dir=worktree.parent,
                            checkout=Checkout(worktree, "agent/T-0009-x", list(checks)), worktrees=worktrees)


@pytest.fixture
def session(tmp_path):
    """A DM conversation's session folder, with no checkout yet."""
    path = tmp_path / "scratch" / "V-9"
    path.mkdir(parents=True)
    return path


def unbound_tools(link, worktrees, session, **kw):
    checkout = Checkout(None, None, [])
    return remote_sdk_tools(MANIFEST, link, "tok", session_dir=session, checkout=checkout, worktrees=worktrees,
                            **kw), checkout


async def test_work_on_makes_checkout_runs_setup_writes_keys(worktrees, session, tmp_path):
    app_envs = tmp_path / "run" / "projects"
    app_envs.mkdir(parents=True)
    (app_envs / "sample.env").write_text("APP_KEY=x\n")
    link = FakeLink()
    tools, checkout = unbound_tools(link, worktrees, session, app_env_dir=app_envs)
    reply = await call(tools, "work_on", {"project": "sample", "title": "run sample"})
    path, keys = session / "sample", session / ".test-keys-sample.env"
    assert link.binds == [("sample", "run sample")]
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == BOUND_BRANCH
    assert (path / ".setup-count").read_text().count("ran") == 1
    assert keys.read_text() == "SAMPLE_TEST_USER=tester@example.com\n"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert checkout == Checkout(path, BOUND_BRANCH, ["true"])
    for part in (str(path), BOUND_BRANCH, "setup passed", str(keys), str(app_envs / "sample.env")):
        assert part in reply
    again = await call(tools, "work_on", {"project": "sample"})
    assert str(path) in again and (path / ".setup-count").read_text().count("ran") == 1
    assert "tester@example.com" not in reply and "tester@example.com" not in again  # the file's path, never a key


async def test_work_on_without_an_app_env_file_says_nothing_about_one(worktrees, session, tmp_path):
    tools, _ = unbound_tools(FakeLink(), worktrees, session, app_env_dir=tmp_path / "run" / "projects")
    assert "app env" not in await call(tools, "work_on", {"project": "sample"})


async def test_work_on_refused_by_core_changes_nothing(worktrees, session):
    tools, checkout = unbound_tools(FakeLink(), worktrees, session)
    assert await call(tools, "work_on", {"project": "nope"}) == "unknown project 'nope' (known: sample)"
    assert checkout == Checkout(None, None, []) and list(session.iterdir()) == []


async def test_work_on_reruns_failed_setup(worktrees, session, tmp_path):
    fixed = tmp_path / "fixed"
    link = FakeLink()
    link.bound["setup"] = [f"if [ -e {fixed} ]; then echo installed; else touch {fixed}; "
                           "echo 'missing dependency'; exit 1; fi"]
    tools, _ = unbound_tools(link, worktrees, session)
    first = await call(tools, "work_on", {"project": "sample"})
    assert "setup failed" in first and "missing dependency" in first
    assert (session / ".setup-failed-sample").exists()
    second = await call(tools, "work_on", {"project": "sample"})
    assert "setup passed" in second and not (session / ".setup-failed-sample").exists()
    third = await call(tools, "work_on", {"project": "sample"})
    assert "setup passed" not in third and "setup failed" not in third


async def test_open_pr_after_work_on_uses_new_checkout(worktrees, session, server):
    link = FakeLink()
    tools, _ = unbound_tools(link, worktrees, session)
    assert await call(tools, "open_pr", {"title": "t", "body": "b"}) == "call work_on(<project>) first"
    await call(tools, "work_on", {"project": "sample"})
    (session / "sample" / "fix.py").write_text("x = 1\n")
    await call(tools, "open_pr", {"title": "Fix export", "body": "b"})
    name, args = link.tool_calls[-1]
    assert name == "open_pr" and args["branch"] == BOUND_BRANCH and args["checks"]["passed"] is True
    assert "$ true" in args["checks"]["log_tail"]  # the bound project's checks ran, not an empty list
    assert args["sha"] == git(server[1], "rev-parse", f"refs/heads/{BOUND_BRANCH}")
    (session / "sample" / "shot.png").write_bytes(b"png")
    (session / "notes.txt").write_text("n")
    assert await call(tools, "post_files", {"paths": [str(session / "sample" / "shot.png"), "notes.txt"],
                                            "text": "after"}) == "posted 2 file(s)"
    assert link.files_sent[-1] == ([("shot.png", b"png"), ("notes.txt", b"n")], "after")


async def test_next_turn_after_work_on_resumes_in_session_folder(tmp_path, worktrees):
    """A DM conversation calls work_on in its first turn. Its next turn is bound: it gets the checkout and the test
    keys, and it runs in the same session folder as the first turn, so --resume finds the session."""
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    packs = {"sample": make_pack(build_setup=["echo ran >> .setup-count"], build_checks=["true"])}
    payloads = Payloads(conn, config=make_config(tmp_path), packs=packs, persona_text="",
                        config_dir=tmp_path / "config",
                        secrets={"SAMPLE_TEST_USER": "tester@example.com", "SLACK_BOT_TOKEN": "xoxb-nope"})
    cid = create_conversation(conn, agent_name="agent", channel="D-ann", thread_ts="5.1", project=None, title="hi")

    def convo():
        return conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()

    class CoreBindLink(FakeLink):
        async def bind(self, tok, project, title):  # what core's /agent/bind does
            row = bind_project(conn, cid, project, packs=packs, agent_name="agent", title=title)
            return {**payloads.checkout(row), "test_secrets": payloads.test_secrets(project)}

    class WorkOnClaude(FakeClaude):
        async def run(self, spec):
            if not self.specs:
                await call(spec.sdk_tools, "work_on", {"project": "sample", "title": "run sample locally"})
            return await super().run(spec)

    claude = WorkOnClaude()
    runner = make_runner(tmp_path, worktrees, CoreBindLink(), claude)
    session, sent = tmp_path / "scratch" / cid, []
    for text in ("run sample locally", "now add a test"):
        sent.append(payloads.turn(convo(), text, "ann"))
        await runner.handle({"id": f"W-{len(sent)}", "conversation_id": cid, "kind": "turn", "payload": sent[-1]},
                            "tok", MANIFEST)
    first, second = sent
    assert first["worktree"] is None and first["test_secrets"] == {}
    assert convo()["branch"] == branch_name("agent", cid, "run sample locally") and convo()["workdir"] == "session"
    assert second["worktree"]["branch"] == convo()["branch"] and second["workdir"] == "session"
    assert second["test_secrets"] == {"SAMPLE_TEST_USER": "tester@example.com"}
    cwd, checkout, note = await runner._workdir(session, second)
    assert (cwd, checkout, note) == (session, session / "sample", "")
    assert [s.cwd for s in claude.specs] == [session, session]
    assert claude.specs[1].env["SAMPLE_TEST_USER"] == "tester@example.com"
    assert (checkout / ".setup-count").read_text().count("ran") == 1


def test_prepare_refuses_a_foreign_repo_and_leaves_it(worktrees, tmp_path, server):
    """Only a repo prepare made is trusted, so even a clone of core's repo on the conversation's branch is refused."""
    path = tmp_path / "scratch" / "V-7" / "sample"
    path.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", f"{server[0]}/git/sample.git", str(path))
    git(path, "checkout", "-q", "-b", "agent/T-7-x")
    (path / "mine.txt").write_text("keep me")
    with pytest.raises(CheckoutRefused, match="didn't make"):
        worktrees.prepare(path, "sample", "agent/T-7-x", "main")
    assert (path / "mine.txt").read_text() == "keep me"
    assert git(path, "status", "--porcelain") == "?? mine.txt"
    assert not (path / ".git" / "agent-checkout").exists()


def test_prepare_trusts_its_checkout_after_a_branch_switch_or_a_new_core_url(worktrees, tmp_path, server):
    path, _ = worktrees.prepare(tmp_path / "scratch" / "V-7" / "sample", "sample", "agent/T-7-x", "main")
    git(path, "checkout", "-q", "-b", "experiment")
    assert worktrees.prepare(path, "sample", "agent/T-7-x", "main") == (path, False)
    moved = Worktrees(tmp_path / "var", "http://core.elsewhere:8080", token_for_git=lambda: "runner-token")
    assert moved.prepare(path, "sample", "agent/T-7-x", "main") == (path, False)
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "experiment"


def test_prepare_refuses_a_folder_that_isnt_a_repo(worktrees, tmp_path):
    path = tmp_path / "scratch" / "V-7" / "sample"
    path.mkdir(parents=True)
    (path / "notes.md").write_text("the agent's notes")
    with pytest.raises(CheckoutRefused, match="isn't a checkout"):
        worktrees.prepare(path, "sample", "agent/T-7-x", "main")
    assert [p.name for p in path.iterdir()] == ["notes.md"] and (path / "notes.md").read_text() == "the agent's notes"


def test_prepare_refuses_a_link_even_to_its_own_checkout(worktrees, tmp_path):
    real, _ = worktrees.prepare(tmp_path / "scratch" / "V-7" / "sample", "sample", "agent/T-7-x", "main")
    link = tmp_path / "scratch" / "V-8" / "sample"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    with pytest.raises(CheckoutRefused, match="is a link"):
        worktrees.prepare(link, "sample", "agent/T-8-x", "main")
    assert link.is_symlink() and git(real, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T-7-x"


def test_prepare_uses_an_empty_folder(worktrees, tmp_path):
    path = tmp_path / "scratch" / "V-7" / "sample"
    path.mkdir(parents=True)
    assert worktrees.prepare(path, "sample", "agent/T-7-x", "main") == (path, True)
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T-7-x"


def test_failed_clone_leaves_nothing_behind(worktrees, tmp_path):
    session = tmp_path / "scratch" / "V-7"
    session.mkdir(parents=True)
    with pytest.raises(GitError, match="no-such-branch"):
        worktrees.prepare(session / "sample", "sample", "agent/T-7-x", "no-such-branch")
    assert list(session.iterdir()) == []


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


async def test_post_files_refuses_the_runners_own_files(worktrees, session):
    """The test-keys file work_on writes, and the setup marker, sit in the session folder but never leave it, not
    even through a link with another name."""
    link = FakeLink()
    tools, _ = unbound_tools(link, worktrees, session)
    await call(tools, "work_on", {"project": "sample"})
    (session / ".setup-failed-sample").write_text("x\n")
    (session / "keys.txt").symlink_to(session / ".test-keys-sample.env")
    (session / "shot.png").write_bytes(b"png")
    for raw in (".test-keys-sample.env", str(session / ".test-keys-sample.env"), ".setup-failed-sample",
                "keys.txt"):
        reply = await call(tools, "post_files", {"paths": ["shot.png", raw], "text": "t"})
        assert reply.startswith("refused:") and raw in reply
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


async def test_session_workdir_open_pr_pushes_the_checkout(tmp_path, worktrees, server):
    """A session running in its session folder still opens its PR from the checkout inside it, on the
    conversation's branch."""
    link, claude = FakeLink(), FakeClaude()
    original = claude.run

    async def run_and_open_pr(spec):
        (tmp_path / "scratch" / "V-1" / "sample" / "fix.py").write_text("x = 1\n")
        await call(spec.sdk_tools, "open_pr", {"title": "Fix export", "body": "b"})
        return await original(spec)

    claude.run = run_and_open_pr
    await make_runner(tmp_path, worktrees, link, claude).handle(item(workdir="session"), "tok", MANIFEST)
    name, args = link.tool_calls[-1]
    assert name == "open_pr" and args["branch"] == "agent/T-0001-fix-export"
    assert args["sha"] == git(server[1], "rev-parse", "refs/heads/agent/T-0001-fix-export")


async def test_link_maps_401_and_204():
    def handler(request):
        if request.url.path == "/agent/pull":
            return httpx.Response(204)
        return httpx.Response(401, json={"error": "unauthorized"})

    link = CoreLink("http://core.test", "runner-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await link.pull() is None
    with pytest.raises(SessionRevoked):
        await link.heartbeat("tok")


async def test_link_bind_returns_cores_refusal():
    def handler(request):
        if request.headers["Authorization"] != "Bearer tok":
            return httpx.Response(401, json={"error": "unauthorized"})
        if b'"nope"' in request.content:
            return httpx.Response(409, json={"error": "unknown project 'nope' (known: sample)"})
        return httpx.Response(200, json={"project": "sample", "test_secrets": {"K": "v"}})

    link = CoreLink("http://core.test", "runner-token", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await link.bind("tok", "sample", "t") == {"project": "sample", "test_secrets": {"K": "v"}}
    assert await link.bind("tok", "nope", "t") == {"error": "unknown project 'nope' (known: sample)"}
    with pytest.raises(SessionRevoked):
        await link.bind("gone", "sample", "t")


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


async def test_fresh_session_prompt_still_starts_with_the_origin(tmp_path, worktrees):
    """The fallback's note goes after the who/where/link line, which stays the prompt's first line."""
    origin = "[from Ann Lee (ann) · DM · https://team.slack.com/archives/D1/p51]"
    link, claude = FakeLink(), ResumeFailsClaude()
    await make_runner(tmp_path, worktrees, link, claude).handle(
        item(resume_session_id="sess-gone", origin=origin), "tok", [])
    assert claude.specs[1].prompt == f"{origin}\n{FRESH_NOTE}fix the export"


async def test_preparation_failure_reports_an_error(tmp_path, worktrees):
    link = FakeLink()
    bad = item()
    bad["payload"]["worktree"]["default_branch"] = "no-such-branch"
    await make_runner(tmp_path, worktrees, link, FakeClaude()).handle(bad, "tok", [])
    assert link.results[-1]["outcome"] == "error" and "no-such-branch" in link.results[-1]["error"]


async def test_runner_exits_when_its_token_is_revoked(tmp_path, worktrees):
    class RevokedLink(FakeLink):
        async def pull(self):
            raise SessionRevoked()

    runner = make_runner(tmp_path, worktrees, RevokedLink(), FakeClaude())
    await asyncio.wait_for(runner.run_forever(), 2)


def test_conversation_gets_its_own_repo_with_shared_objects(worktrees, tmp_path):
    path, created = worktrees.prepare(tmp_path / "scratch" / "V-5" / "sample", "sample", "agent/T-5-x", "main")
    assert created and path == tmp_path / "scratch" / "V-5" / "sample" and (path / ".git").is_dir()
    assert (path / ".git" / "objects" / "info" / "alternates").exists()
    assert git(path, "remote", "get-url", "origin").endswith("/git/sample.git")
    assert json.loads((path / ".git" / "agent-checkout").read_text()) == {"project": "sample",
                                                                           "branch": "agent/T-5-x"}
    assert worktrees.prepare(path, "sample", "agent/T-5-x", "main") == (path, False)


def test_parallel_prepare_same_project(worktrees, tmp_path):
    """Two conversations on one project prepare their checkouts at the same moment: the shared clone is made
    once, and each gets its own repo on its own branch."""
    start = threading.Barrier(2)

    def prepare(convo):
        start.wait()
        return worktrees.prepare(tmp_path / "scratch" / convo / "sample", "sample", f"agent/{convo}-x", "main")

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(prepare, ["V-1", "V-2"]))
    for convo, (path, created) in zip(["V-1", "V-2"], results, strict=True):
        assert created and path == tmp_path / "scratch" / convo / "sample"
        assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == f"agent/{convo}-x"
        assert git(path, "status", "--porcelain") == ""


def test_scratch_files_are_never_committed(worktrees, tmp_path):
    path, _ = worktrees.prepare(tmp_path / "scratch" / "V-6" / "sample", "sample", "agent/T-6-x", "main")
    (path / ".agent-progress.md").write_text("notes")
    (path / ".agent-out").mkdir()
    (path / ".agent-out" / "logs.json").write_text("{}")
    assert git(path, "status", "--porcelain") == ""


async def test_open_pr_commit_uses_configured_email(worktrees, worktree, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "bot@example.com")
    seen = []

    async def shell(cmd, cwd, timeout_s=0, env=None):
        seen.append((cmd, env))
        return 0, ""

    tools = remote_sdk_tools(MANIFEST, FakeLink(), "tok", session_dir=worktree.parent,
                             checkout=Checkout(worktree, "agent/T-0009-x", []), worktrees=worktrees, shell=shell)
    await call(tools, "open_pr", {"title": "t", "body": "b"})
    commit_env = seen[0][1]
    assert commit_env["GIT_AUTHOR_EMAIL"] == "bot@example.com"


async def test_commit_step_never_sees_the_runner_token(worktrees, worktree):
    seen = []

    async def shell(cmd, cwd, timeout_s=0, env=None):
        seen.append((cmd, env))
        return 0, ""

    tools = remote_sdk_tools(MANIFEST, FakeLink(), "tok", session_dir=worktree.parent,
                             checkout=Checkout(worktree, "agent/T-0009-x", []), worktrees=worktrees, shell=shell)
    await call(tools, "open_pr", {"title": "t", "body": "b"})
    commit_env = seen[0][1]
    assert "commit" in seen[0][0] and not any("runner-token" in str(v) for v in commit_env.values())


async def test_extra_env_reaches_the_session(tmp_path, worktrees):
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, worktrees, link, claude)
    runner.extra_env = {"GH_TOKEN": "ghp_abc"}
    await runner.handle(item(), "tok", [])
    assert claude.specs[0].env["GH_TOKEN"] == "ghp_abc"
    assert claude.specs[0].env["SAMPLE_TEST_USER"] == "tester@example.com"


async def test_forced_env_keys_cannot_be_overridden(tmp_path, worktrees):
    """CLAUDE_CONFIG_DIR and the function-hooks flag are forced last: neither the container's extra env nor a
    pack's test secrets can override them, even when both try to."""
    link, claude = FakeLink(), FakeClaude()
    runner = make_runner(tmp_path, worktrees, link, claude)
    runner.extra_env = {"CLAUDE_CONFIG_DIR": "/from-extra-env", "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"}
    await runner.handle(item(test_secrets={"CLAUDE_CONFIG_DIR": "/from-test-secrets",
                                           "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"}), "tok", [])
    spec = claude.specs[0]
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "sessions" / "V-1")
    assert spec.env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1"


LINEAR_MCP = {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                         "headers": {"Authorization": "Bearer ${LINEAR_APP_TOKEN}"}}}


def env_runner(tmp_path, worktrees, claude, env_file):
    return Runner(FakeLink(), worktrees, claude, bundles_dir=tmp_path / "bundles",
                  sessions_root=tmp_path / "sessions", scratch_root=tmp_path / "scratch", heartbeat_s=0.05,
                  env_files=[env_file])


async def test_mcp_headers_use_current_linear_token(tmp_path, worktrees):
    claude, env_file = FakeClaude(), tmp_path / "linear.env"
    runner = env_runner(tmp_path, worktrees, claude, env_file)
    for n, token in enumerate(("tok-1", "tok-2")):
        env_file.write_text(f"LINEAR_APP_TOKEN={token}\n")
        await runner.handle(item(conversation_id=f"V-{n}", worktree=None, mcp=LINEAR_MCP), "tok", [])
    assert [s.mcp_servers["linear"]["headers"]["Authorization"] for s in claude.specs] == \
        ["Bearer tok-1", "Bearer tok-2"]
    assert claude.specs[1].mcp_servers["linear"]["url"] == "https://mcp.linear.app/mcp"
    assert claude.specs[1].env["LINEAR_APP_TOKEN"] == "tok-2"   # for agent activities via the API


async def test_unset_variable_drops_the_server(tmp_path, worktrees, caplog):
    claude = FakeClaude()
    await env_runner(tmp_path, worktrees, claude, tmp_path / "missing.env").handle(
        item(worktree=None, mcp=LINEAR_MCP), "tok", [])
    assert claude.specs[0].mcp_servers == {}
    assert "LINEAR_APP_TOKEN" in caplog.text
