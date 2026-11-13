import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.core.agent_tools import ToolContext
from agent.core.app import CoreApp
from agent.core.sessions import mint_token, resolve_token
from agent.proxy import AccountUsage, Usage
from agent_fixtures import FakeSlack

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples" / "config"


class Proxy:
    async def usage(self):
        return Usage({"maple": AccountUsage("maple", "allowed", 1, True, 0.1, 0.1, 1)}, "on")


class Sandboxd:
    def __init__(self, states=None):
        self.states, self.ensured = states or {}, []

    async def status(self):
        return self.states

    async def ensure(self, name, profile, project, sandbox_token, inject):
        self.ensured.append((name, profile, project, sandbox_token, inject))


class Writer:
    async def find_open_pr(self, repo, branch):
        return None


@pytest.fixture
def app(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    return CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={"SANDBOXD_TOKEN": "sbxd"},
                   slack=FakeSlack(), proxy=Proxy(), sandboxd=Sandboxd(), github_writer=Writer(), bot_user_id="UBOT")


@pytest.fixture
async def api(app):
    client = TestClient(TestServer(app.agent_app))
    await client.start_server()
    yield client
    await client.close()


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


async def test_health(api):
    assert (await api.get("/health")).status == 200


async def test_dm_turn_end_to_end(app, api):
    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "5.1", "text": "what's running?"})
    tok = mint_token(app.conn, "sandbox", sandbox="general", work_item_id=None, ttl_s=60)
    body = await (await api.post("/agent/pull", json={"sandbox": "general"}, headers=bearer(tok))).json()
    item, session = body["work_item"], body["session_token"]
    assert item["payload"]["profile"] == "general" and "status" in {t["name"] for t in body["tools"]}
    r = await api.post("/agent/tool", json={"name": "status", "args": {}}, headers=bearer(session))
    assert "running: 1" in (await r.json())["text"]
    await api.post("/agent/result", json={"result": {"outcome": "done", "text": "Nothing else is running.",
                                                     "session_id": "sess-1", "turns": 2, "cost_usd_est": 0.1}},
                   headers=bearer(session))
    assert app.slack.posts[-1] == {"channel": "D1", "thread_ts": "5.1", "text": "Nothing else is running."}
    run = app.conn.execute("SELECT role, stage, turns, outcome FROM runs WHERE id = ?", (item["id"],)).fetchone()
    assert tuple(run) == ("general", "turn", 2, "done")


async def test_tools_by_profile(app):
    def names(sandbox):
        ctx = ToolContext(work_item_id="W", conversation_id=None, sandbox=sandbox,
                          profile=sandbox.split("-")[0] if sandbox != "general" else "general", project="sample",
                          actor="parthib", tier="admin", unattended=False, task_id=None, channel="C", thread_ts="1")
        return {t["name"] for t in app.router.manifest(ctx)}

    code, debug = names("code-sample"), names("debug-sample")
    assert {"open_pr", "ask_production", "track_task", "az_login", "rebuild_sandbox", "sandbox_exec"} <= code
    assert "az" not in code
    assert {"az", "az_login"} <= debug and not ({"open_pr", "ask_production"} & debug)


async def test_ensure_sandboxes_mints_tokens_for_waiting_work(app):
    app.queue.enqueue("V-1", "code-sample", "turn", {})
    app.queue.enqueue(None, "general", "chore", {})
    app.sandboxd.states = {"general": {"state": "running"}}
    await app.ensure_sandboxes()
    [(name, profile, project, token, inject)] = app.sandboxd.ensured
    assert (name, profile, project, inject) == ("code-sample", "code", "sample", {})
    info = resolve_token(app.conn, token)
    assert (info.kind, info.sandbox) == ("sandbox", "code-sample")


async def test_on_files_uploads_to_thread(app):
    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "6.1", "text": "show me"})
    item_id = app.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    await app.on_files(item_id, [("a.png", b"png")], "screenshots")
    upload = app.slack.uploads[-1]
    assert upload["channel"] == "D1" and upload["thread_ts"] == "6.1"
    assert upload["file_uploads"] == [{"file": b"png", "filename": "a.png"}]


async def test_git_endpoint_needs_a_token(api):
    assert (await api.get("/git/sample.git/info/refs?service=git-upload-pack")).status == 401


def test_no_v1_pipeline_left():
    out = subprocess.run(["grep", "-rlE", "--include=*.py", r"agent\.worker|agent\.roles|core\.dispatch|core\.chat|core\.prompts",
                          str(ROOT / "src")], capture_output=True, text=True)
    assert out.stdout == ""
    for gone in ("worker", "roles.py", "core/dispatch.py", "core/chat.py", "core/tools.py", "core/internal_api.py"):
        assert not (ROOT / "src" / "agent" / gone).exists(), gone



async def test_session_token_ttl_follows_task_minutes(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = (config / "agent.toml").read_text().replace("task_minutes = 90", "task_minutes = 200")
    (config / "agent.toml").write_text(toml)
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={}, slack=FakeSlack(), proxy=Proxy())
    client = TestClient(TestServer(app.agent_app))
    await client.start_server()
    try:
        app.queue.enqueue("V-1", "general", "chore", {})
        tok = mint_token(app.conn, "sandbox", sandbox="general", work_item_id=None, ttl_s=60)
        body = await (await client.post("/agent/pull", json={"sandbox": "general"}, headers=bearer(tok))).json()
        expires = app.conn.execute("SELECT expires_at FROM tokens WHERE work_item_id = ?",
                                   (body["work_item"]["id"],)).fetchone()[0]
        from datetime import UTC, datetime, timedelta
        assert expires > (datetime.now(UTC) + timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        await client.close()


async def test_ensure_is_not_repeated_while_booting_but_silent_runners_are_reensured(app):
    app.queue.enqueue("V-1", "code-sample", "turn", {})
    await app.ensure_sandboxes()
    await app.ensure_sandboxes()
    assert len(app.sandboxd.ensured) == 1
    app.sandboxd.states = {"code-sample": {"state": "running"}}
    app._ensured["code-sample"] -= 1000
    app.pulls["code-sample"] = __import__("time").time() - 1000          # its runner went quiet long ago
    await app.ensure_sandboxes()
    assert len(app.sandboxd.ensured) == 2
    app.pulls["code-sample"] = __import__("time").time()
    app._ensured["code-sample"] -= 1000
    await app.ensure_sandboxes()
    assert len(app.sandboxd.ensured) == 2
