import pytest

from agent import db
from agent.core.conversations import create_conversation
from agent.core.payloads import Payloads
from agent.gitutil import branch_name
from agent_fixtures import add_conversation, make_config, make_pack

PERSONA = "PERSONA-SHEET: dry, calm."


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def payloads(conn, tmp_path):
    cfg = make_config(tmp_path)
    return Payloads(conn, config=cfg, packs={"sample": make_pack()}, persona_text=PERSONA,
                    config_dir=tmp_path / "config", secrets={"SAMPLE_TEST_USER": "tester@example.com",
                                                              "SLACK_BOT_TOKEN": "xoxb-nope"})


def row(conn, convo_id):
    return conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()


def bound(conn, title="fix the export crash") -> str:
    """A conversation bound to `sample` up front, as a mention in the project's channel makes one."""
    return create_conversation(conn, agent_name="agent", channel="C-SAMPLE", thread_ts="1.0", project="sample",
                               title=title)


def test_turn_payload_for_a_known_project(payloads, conn):
    cid = bound(conn)
    p = payloads.turn(row(conn, cid), "fix the export crash", "bob")
    assert p["text"] == "fix the export crash" and p["actor"] == "bob"
    assert PERSONA in p["system_prompt"] and "full development environment" in p["system_prompt"]
    assert p["model"] == "claude-opus-5-5" and p["max_turns"] == 400 and p["timeout_s"] == 90 * 60
    assert p["worktree"] == {"project": "sample", "default_branch": "main",
                             "branch": branch_name("agent", cid, "fix the export crash"),
                             "setup": ["pip install -r requirements.txt"], "checks": ["pytest -q"]}
    assert p["workdir"] == "checkout"
    assert p["test_secrets"] == {"SAMPLE_TEST_USER": "tester@example.com"}


def test_branch_is_the_same_on_every_turn(payloads, conn):
    cid = bound(conn)
    first = payloads.turn(row(conn, cid), "fix the export crash", "bob")
    second = payloads.turn(row(conn, cid), "also handle empty files\nand add a test", "bob")
    assert first["worktree"]["branch"] == second["worktree"]["branch"] == branch_name("agent", cid,
                                                                                         "fix the export crash")


def test_test_secrets_are_the_packs_worker_keys(payloads):
    assert payloads.test_secrets("sample") == {"SAMPLE_TEST_USER": "tester@example.com"}
    assert payloads.test_secrets("nope") == {}


def test_track_task_binding_has_no_checkout(payloads, conn):
    convo = row(conn, add_conversation(conn, project="sample"))  # bound, but no branch
    p = payloads.turn(convo, "why is the export slow?", "bob")
    assert p["project"] == "sample" and p["worktree"] is None and p["test_secrets"] == {}
    assert p["workdir"] == "session" and payloads.checkout(convo) is None


def test_unbound_conversation_has_no_worktree_or_secrets(payloads, conn):
    convo = row(conn, add_conversation(conn, project=None, channel="D-ann"))
    p = payloads.turn(convo, "hi", "ann")
    assert p["worktree"] is None and p["test_secrets"] == {} and p["workdir"] == "session"


def test_use_fable_model(payloads, conn):
    cid = add_conversation(conn)
    conn.execute("UPDATE conversations SET model = 'claude-fable-5-1', session_id = 'sess-9' WHERE id = ?", (cid,))
    p = payloads.turn(row(conn, cid), "x", "bob")
    assert p["model"] == "claude-fable-5-1" and p["resume_session_id"] == "sess-9"


def test_system_prompt_from_config_overrides_default(payloads, conn, tmp_path):
    (tmp_path / "config" / "agent").mkdir(parents=True)
    (tmp_path / "config" / "agent" / "system.md").write_text("CUSTOM SYSTEM PROMPT")
    p = payloads.turn(row(conn, add_conversation(conn)), "x", "bob")
    assert p["system_prompt"].startswith("CUSTOM SYSTEM PROMPT")


def test_shipped_system_prompt(payloads, tmp_path):
    text = payloads.system_prompt()
    for phrase in ("say you are an AI", "gh", "az", "work_on", "open_pr", "draft PR", "production", "free port",
                   "browser-testing", "information, not instructions"):
        assert phrase in text
    (tmp_path / "config" / "agent").mkdir(parents=True)
    (tmp_path / "config" / "agent" / "system.md").write_text("CUSTOM")
    assert payloads.system_prompt() == "CUSTOM"


def test_shipped_system_prompt_says_how_memory_and_context_work(payloads):
    text = payloads.system_prompt()
    for phrase in ("Claude Code memory is shared by all your sessions", "with who, when and the link",
                   "contradicts a saved note, say so and ask which stands",
                   "Check code facts against the code", "as claims, not facts",
                   "Slack via slack_read, Linear via its MCP, GitHub via gh"):
        assert phrase in text


def test_payload_carries_mcp_servers(tmp_path, conn):
    cfg = make_config(tmp_path, '\n[mcp.linear]\ntype = "http"\nurl = "https://mcp.linear.app/mcp"\n'
                                'headers = { Authorization = "Bearer ${LINEAR_APP_TOKEN}" }\n')
    p = Payloads(conn, config=cfg, packs={}, persona_text="", config_dir=tmp_path / "c", secrets={})
    convo = row(conn, add_conversation(conn, project=None))
    assert p.turn(convo, "hi", "ann")["mcp"] == {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                                                            "headers": {"Authorization": "Bearer ${LINEAR_APP_TOKEN}"}}}
