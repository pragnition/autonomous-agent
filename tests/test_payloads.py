import pytest

from agent import db
from agent.core.payloads import PRODUCTION_RULES, Payloads
from agent.memory.store import MemoryStore, NoteInput
from agent.tokens import estimate_tokens
from agent_fixtures import add_conversation, make_config, make_pack

PERSONA = "PERSONA-SHEET: dry, calm."


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    for i in range(40):
        store.upsert(NoteInput(scope="projects/sample", kind="fact", summary=f"export fact {i}",
                               body="The export uses streaming writes. " * 20))
    return store


@pytest.fixture
def payloads(conn, memory, tmp_path):
    cfg = make_config(tmp_path)
    return Payloads(conn, config=cfg, packs={"sample": make_pack()}, memory=memory, persona_text=PERSONA,
                    config_dir=tmp_path / "config", secrets={"SAMPLE_TEST_USER": "tester@example.com",
                                                              "SLACK_BOT_TOKEN": "xoxb-nope"})


def row(conn, convo_id):
    return conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone()


def test_turn_payload_for_code(payloads, conn):
    convo = row(conn, add_conversation(conn, sandbox="code-sample"))
    p = payloads.turn(convo, "fix the export crash", "bob")
    assert p["text"] == "fix the export crash" and p["actor"] == "bob" and p["profile"] == "code"
    assert PERSONA in p["system_prompt"] and "act on the world only through" in p["system_prompt"]
    assert p["model"] == "claude-opus-5-5" and p["max_turns"] == 400 and p["timeout_s"] == 90 * 60
    wt = p["worktree"]
    assert (wt["project"], wt["default_branch"]) == ("sample", "main")
    assert wt["branch_hint"].startswith("agent/") and "fix-the-export-crash" in wt["branch_hint"]
    assert wt["setup"] == ["pip install -r requirements.txt"] and wt["checks"] == ["pytest -q"]
    assert p["test_secrets"] == {"SAMPLE_TEST_USER": "tester@example.com"}


def test_briefing_within_budget(payloads, conn):
    convo = row(conn, add_conversation(conn, sandbox="code-sample"))
    p = payloads.turn(convo, "export", "bob")
    briefing = p["system_prompt"].split("Notes from memory", 1)[1]
    assert "export fact" in briefing
    assert estimate_tokens("Notes from memory" + briefing) <= 2000


def test_general_has_no_worktree_or_secrets(payloads, conn):
    convo = row(conn, add_conversation(conn, sandbox="general", project=None, channel="D-ann"))
    p = payloads.turn(convo, "hi", "ann")
    assert p["worktree"] is None and p["test_secrets"] == {} and p["profile"] == "general"


def test_use_fable_model(payloads, conn):
    cid = add_conversation(conn, sandbox="code-sample")
    conn.execute("UPDATE conversations SET model = 'claude-fable-5-1', session_id = 'sess-9' WHERE id = ?", (cid,))
    p = payloads.turn(row(conn, cid), "x", "bob")
    assert p["model"] == "claude-fable-5-1" and p["resume_session_id"] == "sess-9"


def test_production_payload(payloads):
    p = payloads.production("sample", "what fails", "bob", False, "V-caller")
    assert PERSONA not in p["system_prompt"] and PRODUCTION_RULES in p["system_prompt"]
    assert p["worktree"] == {"project": "sample", "default_branch": "main", "read_only": True}
    assert p["profile"] == "debug" and p["test_secrets"] == {}
    assert p["caller_conversation_id"] == "V-caller" and p["project"] == "sample"


def test_chore_payload(payloads):
    p = payloads.chore("consolidate this", {"type": "object"})
    assert p["model"] == "claude-sonnet-5" and p["structured"] == {"type": "object"}
    assert PERSONA not in p["system_prompt"] and p["profile"] == "general" and p["worktree"] is None


def test_system_prompt_from_config_overrides_default(payloads, conn, tmp_path):
    (tmp_path / "config" / "agent").mkdir(parents=True)
    (tmp_path / "config" / "agent" / "system.md").write_text("CUSTOM SYSTEM PROMPT")
    p = payloads.turn(row(conn, add_conversation(conn, sandbox="code-sample")), "x", "bob")
    assert p["system_prompt"].startswith("CUSTOM SYSTEM PROMPT")
