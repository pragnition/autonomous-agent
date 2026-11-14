import pytest

from agent import db
from agent.core.conversations import create_conversation
from agent.core.payloads import Payloads
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


def bound(conn) -> str:
    """A conversation in the project's channel, which carries its project."""
    return create_conversation(conn, channel="C-SAMPLE", thread_ts="1.0", project="sample")


def test_turn_payload_for_a_known_project(payloads, conn):
    p = payloads.turn(row(conn, bound(conn)), "fix the export crash", "bob")
    assert p["text"] == "fix the export crash" and p["actor"] == "bob" and p["project"] == "sample"
    assert PERSONA in p["system_prompt"] and "full development environment" in p["system_prompt"]
    assert p["model"] == "claude-opus-5-5" and p["max_turns"] == 400 and p["timeout_s"] == 90 * 60
    assert p["test_secrets"] == {"SAMPLE_TEST_USER": "tester@example.com"}
    assert not {"worktree", "workdir", "branch"} & set(p)   # the session gets and sets up the code itself


def test_every_turn_gets_every_packs_test_secrets(conn, tmp_path):
    payloads = Payloads(conn, config=make_config(tmp_path), persona_text="", config_dir=tmp_path / "config",
                        packs={"sample": make_pack(), "other": make_pack("other", secrets_worker=["OTHER_KEY"])},
                        secrets={"SAMPLE_TEST_USER": "u", "OTHER_KEY": "k", "SLACK_BOT_TOKEN": "xoxb-nope"})
    dm = payloads.turn(row(conn, add_conversation(conn, project=None, channel="D-ann")), "hi", "ann")
    assert dm["test_secrets"] == {"SAMPLE_TEST_USER": "u", "OTHER_KEY": "k"}


def test_system_prompt_lists_the_projects(conn, tmp_path):
    def prompt(packs):
        return Payloads(conn, config=make_config(tmp_path), packs=packs, persona_text="",
                        config_dir=tmp_path / "config", secrets={}).system_prompt()
    two = prompt({"sample": make_pack(), "other": make_pack("other", repo="o/other", slack_channel="")})
    assert "- other: https://github.com/o/other (default branch main)\n" \
           "- sample: https://github.com/o/r (default branch main, Slack C-SAMPLE)" in two
    assert "{projects}" not in two and "- none configured" in prompt({})


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
    for phrase in ("say you are an AI", "gh repo clone", "gh pr create", "az", "own the work end to end",
                   "work around it, and keep going", "production", "free port", "browser-testing",
                   "information, not instructions"):
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
