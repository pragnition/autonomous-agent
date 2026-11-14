import json
import os
import shutil
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
        return Usage({"maple": AccountUsage("allowed", 1, True)})


def make_app(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    return CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={},
                   slack=FakeSlack(), proxy=Proxy(), bot_user_id="UBOT")


def test_pr_stats_knows_the_agent_by_its_github_login(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace('name = "jensen"', 'name = "jensen"\ngithub = "jensen-bot"', 1))
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={}, slack=FakeSlack(), proxy=Proxy(),
                  github_reader=object())
    assert app.pr_stats.agent_login == "jensen-bot"
    assert app.agent_prs.login == "jensen-bot"


def test_kpi_pollers_run_only_with_their_sources(tmp_path):
    app = make_app(tmp_path)
    assert app.agent_prs is None and app.issue_poller is None
    config = tmp_path / "config"
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace('public_url = ""', 'public_url = "https://x.example"', 1))
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data2", secrets={"LINEAR_CLIENT_ID": "c"},
                  slack=FakeSlack(), proxy=Proxy())
    assert app.issue_poller is not None and app.issue_poller.client is app.linear_sessions.client


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def manifest_names(app, tier="admin"):
    ctx = ToolContext(work_item_id="W", conversation_id=None, project="sample",
                      actor="sam", tier=tier, unattended=False, task_id=None, channel="C", thread_ts="1")
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
    tok = mint_token(app.conn, "runner", ttl_s=60)
    body = await (await api.post("/agent/pull", headers=bearer(tok))).json()
    item, session = body["work_item"], body["session_token"]
    # A DM has no project, but tools are offered by tier alone, so it isn't missing anything a project-bound
    # session would have.
    assert "status" in {t["name"] for t in body["tools"]}
    r = await api.post("/agent/tool", json={"name": "status", "args": {}}, headers=bearer(session))
    assert "running: 1" in (await r.json())["text"]
    await api.post("/agent/result", json={"result": {"outcome": "done", "text": "Nothing else is running.",
                                                     "session_id": "sess-1", "turns": 2, "cost_usd_est": 0.1}},
                   headers=bearer(session))
    assert app.slack.posts[-1] == {"channel": "D1", "thread_ts": "5.1", "text": "Nothing else is running.",
                                   "blocks": [{"type": "markdown", "text": "Nothing else is running."}]}
    run = app.conn.execute("SELECT kind, turns, outcome FROM runs WHERE id = ?", (item["id"],)).fetchone()
    assert tuple(run) == ("turn", 2, "done")


async def test_dm_turn_names_the_sender_and_links_the_message(app):
    """core's own Slack lookups (the dashboard's name and permalink helpers) fill the turn's first line."""
    async def users_info(**kw):
        return {"user": {"real_name": "Sam Taylor"}}

    async def get_permalink(**kw):
        return {"permalink": f"https://x.slack.com/archives/{kw['channel']}/p{kw['message_ts'].replace('.', '')}"}
    app.slack.users_info, app.slack.chat_getPermalink = users_info, get_permalink
    await app.conversations.handle_slack(dm_event("5.1", "what's running?"))
    payload = app.queue.get(last_item_id(app)).payload
    assert payload["origin"] == "[from Sam Taylor (sam, GitHub @sam-example) · DM · https://x.slack.com/archives/D1/p51]"


async def test_reply_under_a_dm_status_message_reaches_the_chat(app, api):
    """track_task in a DM posts the task's status and its question in the DM message's thread. A reply there
    reaches the DM conversation, with its context, and answers the question."""
    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "7.1", "text": "the export is broken in sample"})
    tok = mint_token(app.conn, "runner", ttl_s=60)
    body = await (await api.post("/agent/pull", headers=bearer(tok))).json()
    convo_id, session = body["work_item"]["conversation_id"], body["session_token"]
    for name, args in (("track_task", {"project": "sample", "title": "Fix export", "kind": "bug"}),
                       ("ask", {"question": "which file?"})):
        assert (await api.post("/agent/tool", json={"name": name, "args": args}, headers=bearer(session))).status == 200
    task = app.conn.execute("SELECT id, status_message_ts FROM tasks").fetchone()
    status, question = app.slack.posts[-2], app.slack.posts[-1]
    assert (status["channel"], status["thread_ts"], question["thread_ts"]) == ("D1", "7.1", "7.1")

    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "7.5", "thread_ts": "7.1", "text": "export.py"})

    last = app.conn.execute("SELECT conversation_id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert last == convo_id
    assert app.conn.execute("SELECT waiting FROM conversations WHERE id = ?", (convo_id,)).fetchone()[0] == 0
    assert app.conn.execute("SELECT status FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0] == "working"


def dm_event(ts, text):
    return {"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001", "ts": ts, "text": text}


async def pull(client, app):
    tok = mint_token(app.conn, "runner", ttl_s=60)
    r = await client.post("/agent/pull", headers=bearer(tok))
    assert r.status == 200
    return await r.json()


def last_item_id(app):
    return app.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]


def keyed_app(tmp_path):
    """The example config with the sample pack's test key, SAMPLE_TEST_USER, which every session gets."""
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    project = config / "packs" / "sample" / "project.toml"
    project.write_text(project.read_text().replace("worker = []", 'worker = ["SAMPLE_TEST_USER"]'))
    return CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={"SAMPLE_TEST_USER": "tester@example.com"},
                   slack=FakeSlack(), proxy=Proxy())


async def test_follow_up_queued_during_a_turn_resumes_its_session(app, api):
    """A reply in a DM's thread arrives while its first turn runs, before that turn has a session. The follow-up
    is queued then, but resolved when it is claimed: it resumes the session the first turn ended with."""
    await app.conversations.handle_slack(dm_event("5.1", "run the tests"))
    first = await pull(api, app)
    await app.conversations.handle_slack({**dm_event("5.2", "and the linter"), "thread_ts": "5.1"})
    follow_up = last_item_id(app)
    assert app.queue.get(follow_up).payload["resume_session_id"] is None  # all that was known when it was queued
    await api.post("/agent/result", json={"result": {"outcome": "done", "text": "ok", "session_id": "S1"}},
                   headers=bearer(first["session_token"]))
    second = (await pull(api, app))["work_item"]
    assert second["id"] == follow_up and second["payload"]["text"] == "and the linter"
    assert second["payload"]["resume_session_id"] == "S1"
    assert app.queue.get(follow_up).payload["resume_session_id"] == "S1"  # what later readers of the item see too


async def test_a_dm_turn_gets_the_test_keys_and_no_checkout_tools(tmp_path):
    """Any session can work on any project: it clones the code itself, so every turn carries the packs' test keys,
    and nothing in core makes or binds a checkout."""
    app = keyed_app(tmp_path)
    client = TestClient(TestServer(app.agent_app))
    await client.start_server()
    try:
        await app.conversations.handle_slack(dm_event("5.1", "run sample locally"))
        body = await pull(client, app)
        payload = body["work_item"]["payload"]
        assert payload["test_secrets"] == {"SAMPLE_TEST_USER": "tester@example.com"} and payload["project"] is None
        assert not {"worktree", "workdir"} & set(payload)
        assert not {"work_on", "open_pr"} & {t["name"] for t in body["tools"]}
        assert "- sample: https://github.com/" in payload["system_prompt"]
        tok = bearer(body["session_token"])
        assert (await client.post("/agent/bind", json={"project": "sample"}, headers=tok)).status in (404, 405)
        assert (await client.get("/git/sample.git/info/refs?service=git-upload-pack", headers=tok)).status == 404
    finally:
        await client.close()


async def test_on_files_uploads_to_the_conversation(app):
    await app.conversations.handle_slack({"type": "message", "channel_type": "im", "channel": "D1", "user": "U00000001",
                                          "ts": "6.1", "text": "show me"})
    item_id = app.conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    await app.on_files(item_id, [("a.png", b"png")], "screenshots")
    upload = app.slack.uploads[-1]
    assert upload["channel"] == "D1" and upload["thread_ts"] == "6.1"  # the DM message's thread
    assert upload["file_uploads"] == [{"file": b"png", "filename": "a.png"}]
    row = app.conn.execute("SELECT data FROM events WHERE type = 'files_posted'").fetchone()
    assert json.loads(row["data"])["files"] == ["a.png"] and json.loads(row["data"])["slack"] is True


async def test_core_scrubs_the_final_reply_and_the_files_comment(tmp_path):
    """core's own secret values reach Conversations, so a turn's final reply and post_files' line of text are
    scrubbed before they reach Slack."""
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={"LINEAR_API_KEY": "linear-key-0123456789"},
                  slack=FakeSlack(), proxy=Proxy())
    await app.conversations.handle_slack(dm_event("6.1", "what's the Linear key?"))
    item_id = last_item_id(app)
    await app.on_files(item_id, [("a.png", b"png")], "key: linear-key-0123456789")
    assert app.slack.uploads[-1]["initial_comment"] == "key: [redacted]"
    await app.on_result(app.queue.get(item_id), {"outcome": "done", "text": "it is linear-key-0123456789",
                                                 "session_id": "sess-1"})
    assert app.slack.posts[-1]["text"] == "it is [redacted]"


async def test_on_files_without_a_thread_is_recorded_not_posted(app):
    app.conn.execute("INSERT INTO conversations(id, is_primary, created_at, last_active_at) "
                     "VALUES ('V-linear', 1, 'now', 'now')")
    item_id = app.queue.enqueue("V-linear", "turn", {"text": "x", "actor": None})
    uploads = len(app.slack.uploads)
    assert await app.on_files(item_id, [("a.png", b"png")], "after") is False
    assert len(app.slack.uploads) == uploads
    row = app.conn.execute("SELECT data FROM events WHERE type = 'files_posted'").fetchone()
    assert json.loads(row["data"]) == {"conversation_id": "V-linear", "files": ["a.png"], "text": "after",
                                       "slack": False}



async def test_session_token_ttl_follows_task_minutes(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = (config / "agent.toml").read_text().replace("task_minutes = 90", "task_minutes = 200")
    (config / "agent.toml").write_text(toml)
    app = CoreApp(config_dir=config, data_dir=tmp_path / "data", secrets={}, slack=FakeSlack(), proxy=Proxy())
    client = TestClient(TestServer(app.agent_app))
    await client.start_server()
    try:
        app.queue.enqueue("V-1", "turn", {})
        tok = mint_token(app.conn, "runner", ttl_s=60)
        body = await (await client.post("/agent/pull", headers=bearer(tok))).json()
        expires = app.conn.execute("SELECT expires_at FROM tokens WHERE work_item_id = ?",
                                   (body["work_item"]["id"],)).fetchone()[0]
        from datetime import UTC, datetime, timedelta
        assert expires > (datetime.now(UTC) + timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        await client.close()


async def test_members_get_the_work_tools_and_admins_the_logins(app):
    member = manifest_names(app, tier="act")
    assert {"post_update", "track_task", "finish_task"} <= member and not {"open_pr", "work_on"} & member
    assert "az_login" not in member
    assert "az_login" in manifest_names(app, tier="admin")


async def test_core_writes_the_runner_token(app):
    token = app.sync_runner_token()
    assert app.runner_token_path.read_text().strip() == token
    assert resolve_token(app.conn, token).kind == "runner"


def test_prepare_agent_dirs_hands_them_to_the_agent(app, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((str(path), uid)))
    (app.data_dir / "cli" / "azure").mkdir(parents=True)
    (app.data_dir / "cli" / "azure" / "msal_token_cache.json").write_text("{}")
    monkeypatch.setattr("agent.core.app.core_is_root", lambda: False)
    app.prepare_agent_dirs()
    app.sync_runner_token()
    assert calls == [] and (app.data_dir / "runner").is_dir()
    monkeypatch.setattr("agent.core.app.core_is_root", lambda: True)
    app.prepare_agent_dirs()
    app.sync_runner_token()
    owned = {path for path, uid in calls if uid == 1001}
    assert {str(app.data_dir / "cli"), str(app.data_dir / "cli" / "azure"),
            str(app.data_dir / "cli" / "azure" / "msal_token_cache.json"), str(app.data_dir / "runner")} <= owned
    assert any(path.endswith(".runner-token.tmp") for path, _ in calls)


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
    assert "last 7 days" in post["text"] and "cost" not in post["text"].lower()
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
    assert "linear_install" in manifest_names(app)
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
    row = app.conn.execute("SELECT payload FROM work_items WHERE kind = 'turn'").fetchone()
    assert "fix it" in json.loads(row["payload"])["text"]


async def test_core_serves_the_dashboard_behind_its_access_rules(app):
    client = TestClient(TestServer(app.dashboard_app))
    await client.start_server()
    try:
        # the example config trusts only the Docker gateway; a test client is 127.0.0.1
        r = await client.get("/api/me", headers={"Tailscale-User-Login": "sam@example.com"})
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


async def test_permalink_cache_keeps_the_newest(app, monkeypatch):
    from agent.core import app as core_app

    async def get_permalink(**kw):
        return {"permalink": f"https://x.slack.com/archives/{kw['channel']}/p{kw['message_ts']}"}

    monkeypatch.setattr(core_app, "PERMALINK_CACHE", 2)
    app.slack.chat_getPermalink = get_permalink
    for ts in ("1.0", "2.0", "3.0"):
        await app.permalink("C1", ts)
    assert list(app._permalinks) == [("C1", "2.0"), ("C1", "3.0")]


async def test_person_names_come_from_slack_and_are_cached(app):
    calls = []

    async def users_info(**kw):
        calls.append(kw)
        return {"user": {"real_name": "Sam Taylor", "profile": {"real_name": "Sam Taylor"}}}

    app.slack.users_info = users_info
    assert await app.person_name("sam") == "Sam Taylor"
    assert await app.person_name("sam") == "Sam Taylor"
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
