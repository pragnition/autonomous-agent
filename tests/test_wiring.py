import json
import os
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


class Writer:
    async def find_open_pr(self, repo, branch):
        return None


def make_app(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    return CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={},
                   slack=FakeSlack(), proxy=Proxy(), github_writer=Writer(), bot_user_id="UBOT")


def test_pr_stats_knows_the_agent_by_its_github_login(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace('name = "chilli"', 'name = "chilli"\ngithub = "chilli-bot"', 1))
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={}, slack=FakeSlack(), proxy=Proxy(),
                  github_reader=object())
    assert app.pr_stats.agent_login == "chilli-bot"


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def manifest_names(app, sandbox, tier="admin"):
    ctx = ToolContext(work_item_id="W", conversation_id=None, sandbox=sandbox,
                      profile=sandbox.split("-")[0] if sandbox != "general" else "general", project="sample",
                      actor="parthib", tier=tier, unattended=False, task_id=None, channel="C", thread_ts="1")
    return {t["name"] for t in app.router.manifest(ctx)}


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
    assert app.slack.posts[-1] == {"channel": "D1", "thread_ts": "5.1", "text": "Nothing else is running.",
                                   "blocks": [{"type": "markdown", "text": "Nothing else is running."}]}
    run = app.conn.execute("SELECT role, stage, turns, outcome FROM runs WHERE id = ?", (item["id"],)).fetchone()
    assert tuple(run) == ("general", "turn", 2, "done")


async def test_on_files_uploads_to_thread(app):
    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "6.1", "text": "show me"})
    item_id = app.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    await app.on_files(item_id, [("a.png", b"png")], "screenshots")
    upload = app.slack.uploads[-1]
    assert upload["channel"] == "D1" and upload["thread_ts"] == "6.1"
    assert upload["file_uploads"] == [{"file": b"png", "filename": "a.png"}]
    row = app.conn.execute("SELECT data FROM events WHERE type = 'files_posted'").fetchone()
    assert json.loads(row["data"])["files"] == ["a.png"] and json.loads(row["data"])["slack"] is True


async def test_on_files_without_a_thread_is_recorded_not_posted(app):
    app.conn.execute("INSERT INTO conversations(id, sandbox, is_primary, created_at, last_active_at) "
                     "VALUES ('V-linear', 'general', 1, 'now', 'now')")
    item_id = app.queue.enqueue("V-linear", "general", "turn", {"text": "x", "actor": None})
    uploads = len(app.slack.uploads)
    assert await app.on_files(item_id, [("a.png", b"png")], "after") is False
    assert len(app.slack.uploads) == uploads
    row = app.conn.execute("SELECT data FROM events WHERE type = 'files_posted'").fetchone()
    assert json.loads(row["data"]) == {"conversation_id": "V-linear", "files": ["a.png"], "text": "after",
                                       "slack": False}


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


async def test_open_mode_tools(app):
    member = manifest_names(app, "code-sample", tier="act")
    assert {"open_pr", "post_update", "track_task"} <= member
    assert not ({"ask_production", "az"} & member)
    assert "az_login" in manifest_names(app, "code-sample", tier="admin")


async def test_open_mode_writes_runner_tokens(app):
    tokens = app.sync_runners()
    assert json.loads(app.runner_tokens_path.read_text()) == tokens
    assert sorted(tokens) == ["code-sample", "general"]
    assert all(resolve_token(app.conn, tok).sandbox == name for name, tok in tokens.items())


def test_prepare_agent_dirs_hands_them_to_the_agent(app, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((str(path), uid)))
    (app.data_dir / "cli" / "azure").mkdir(parents=True)
    (app.data_dir / "cli" / "azure" / "msal_token_cache.json").write_text("{}")
    monkeypatch.setattr("agent.core.app.core_is_root", lambda: False)
    app.prepare_agent_dirs()
    app.sync_runners()
    assert calls == [] and (app.data_dir / "runner").is_dir()
    monkeypatch.setattr("agent.core.app.core_is_root", lambda: True)
    app.prepare_agent_dirs()
    app.sync_runners()
    owned = {path for path, uid in calls if uid == 1001}
    assert {str(app.data_dir / "cli"), str(app.data_dir / "cli" / "azure"),
            str(app.data_dir / "cli" / "azure" / "msal_token_cache.json"), str(app.data_dir / "runner")} <= owned
    assert any(path.endswith("runner-tokens.json.tmp") for path, _ in calls)


def test_project_envs_go_to_the_agent_readable_only_by_it(app, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((str(path), uid)))
    monkeypatch.setattr("agent.core.app.core_is_root", lambda: True)
    app.share_project_envs()  # no secrets/projects: nothing to do
    assert not (app.data_dir / "runner" / "projects").exists()
    source = app.data_dir / "secrets" / "projects"
    source.mkdir(parents=True)
    (source / "sample.env").write_text("A=1\n")
    (source / "notes.txt").write_text("not an env file")
    app.share_project_envs()
    shared = app.data_dir / "runner" / "projects" / "sample.env"
    assert shared.read_text() == "A=1\n" and oct(shared.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in shared.parent.iterdir()) == ["sample.env"]
    assert {str(shared.parent), str(shared.parent / ".sample.env.tmp")} <= {p for p, uid in calls if uid == 1001}


async def test_digest_uses_local_time_and_posts_once(app):
    from datetime import UTC, datetime
    app.config.observability.digest_channel = "#agent-ops"
    assert not await app.post_digest(datetime(2026, 11, 13, 0, 30, tzinfo=UTC))       # 08:30 in Singapore
    assert await app.post_digest(datetime(2026, 11, 13, 1, 10, tzinfo=UTC))
    [post] = [p for p in app.slack.posts if p["channel"] == "#agent-ops"]
    assert "last 7 days" in post["text"]
    assert not await app.post_digest(datetime(2026, 11, 13, 2, 0, tzinfo=UTC))
    again = CoreApp(config_dir=app.config_dir, data_dir=app.data_dir, secrets={}, slack=FakeSlack(), proxy=Proxy())
    again.config.observability.digest_channel = "#agent-ops"
    assert not await again.post_digest(datetime(2026, 11, 13, 3, 0, tzinfo=UTC))
    assert await again.post_digest(datetime(2026, 11, 14, 1, 5, tzinfo=UTC))


async def test_no_channel_no_digest(app):
    from datetime import UTC, datetime
    assert not await app.post_digest(datetime(2026, 11, 13, 3, 0, tzinfo=UTC))
    assert not app.slack.posts



async def test_linear_app_wiring(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace('public_url = ""', 'public_url = "https://jensen.example.ts.net"'))
    secrets = {"LINEAR_CLIENT_ID": "cid", "LINEAR_CLIENT_SECRET": "cs", "LINEAR_WEBHOOK_SECRET": "wh"}
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets=secrets, slack=FakeSlack(), proxy=Proxy())
    assert app.public_app is not None
    assert app.linear_auth.redirect_uri == "https://jensen.example.ts.net/linear/oauth/callback"
    assert app.linear_auth.token_path == app.data_dir / "runner" / "linear.env"
    assert "linear_install" in manifest_names(app, "general")
    assert await app.refresh_linear_token() is None          # not installed yet: nothing to refresh
    assert make_app(tmp_path / "plain").public_app is None


async def test_linear_session_event_becomes_a_turn(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace('public_url = ""', 'public_url = "https://jensen.example.ts.net"'))
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={"LINEAR_CLIENT_ID": "cid"},
                  slack=FakeSlack(), proxy=Proxy())
    acts = []

    async def activity(session_id, type, body):
        acts.append((session_id, type))
    app.linear_sessions.client.agent_activity = activity
    await app.on_linear_event({"type": "AgentSessionEvent", "action": "created", "promptContext": "fix it",
                               "agentSession": {"id": "S1", "issue": {"id": "i1", "identifier": "SAM-1",
                                                                      "team": {"key": "SAM"}}}})
    assert acts == [("S1", "thought")]
    row = app.conn.execute("SELECT sandbox, payload FROM work_items WHERE kind = 'turn'").fetchone()
    assert row["sandbox"] == "general" and "fix it" in json.loads(row["payload"])["text"]


async def test_core_serves_the_dashboard_behind_its_access_rules(app):
    client = TestClient(TestServer(app.dashboard_app))
    await client.start_server()
    try:
        # the example config trusts only the Docker gateway; a test client is 127.0.0.1
        r = await client.get("/api/me", headers={"Tailscale-User-Login": "parthib@example.com"})
        assert r.status == 403
    finally:
        await client.close()


async def test_permalinks_are_cached(app):
    calls = []

    async def get_permalink(**kw):
        calls.append(kw)
        return {"permalink": f"https://x.slack.com/archives/{kw['channel']}/p1"}

    app.slack.chat_getPermalink = get_permalink
    assert await app.permalink("C1", "1.0") == "https://x.slack.com/archives/C1/p1"
    assert await app.permalink("C1", "1.0") == "https://x.slack.com/archives/C1/p1"
    assert calls == [{"channel": "C1", "message_ts": "1.0"}]


async def test_failed_permalinks_are_not_retried_at_once(app):
    calls = []

    async def get_permalink(**kw):
        calls.append(kw)
        raise RuntimeError("not_in_channel")

    app.slack.chat_getPermalink = get_permalink
    assert await app.permalink("C9", "1.0") is None
    assert await app.permalink("C9", "1.0") is None
    assert len(calls) == 1


async def test_person_names_come_from_slack_and_are_cached(app):
    calls = []

    async def users_info(**kw):
        calls.append(kw)
        return {"user": {"real_name": "Parthib Das Gupta", "profile": {"real_name": "Parthib Das Gupta"}}}

    app.slack.users_info = users_info
    assert await app.person_name("parthib") == "Parthib Das Gupta"
    assert await app.person_name("parthib") == "Parthib Das Gupta"
    assert await app.person_name("nobody") is None
    assert calls == [{"user": "U00000001"}]


def test_core_reads_account_names_from_its_config_dir(app):
    assert app.account_names.path == app.config_dir / "account-names.toml"


def test_foundry_meter_is_wired_when_installed(app, monkeypatch):
    from agent.config import LlmMeterSettings, LlmUpstream
    settings = LlmMeterSettings(daily_usd=10.0, monthly_usd=100.0,
                                upstreams={"dev": LlmUpstream(base_url="https://x.invalid/openai/v1", key="DEV_KEY")})
    monkeypatch.setattr(app.config, "llm_meter", settings)
    view, set_caps = app.llm_meter()
    assert view()["upstreams"] == ["dev"]
    set_caps("ann", 5, None)
    assert view()["caps"]["daily_usd"] == 5


def test_foundry_meter_absent_is_none(app):
    assert not app.config.llm_meter.upstreams
    assert app.llm_meter() == (None, None)
