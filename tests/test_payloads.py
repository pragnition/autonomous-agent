import pytest

from agent import db
from agent.core import projects
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


def _notes_payloads(conn, tmp_path):
    packs = {"chilliclaw": make_pack("chilliclaw", repo="pragnition/chilliclaw", notes="CC-NOTES: staging only"),
             "rosterrolo": make_pack("rosterrolo", repo="pragnition/RosterRolo", notes="RR-NOTES: dev site login",
                                     plugin_scope="project", dir_name="rosterrolo"),
             "bare": make_pack("bare", repo="o/bare")}
    return Payloads(conn, config=make_config(tmp_path), packs=packs, persona_text=PERSONA,
                    config_dir=tmp_path / "config", secrets={})


def test_a_project_conversation_gets_only_its_packs_notes(conn, tmp_path):
    payloads = _notes_payloads(conn, tmp_path)
    convo = row(conn, add_conversation(conn, project="chilliclaw"))
    prompt = payloads.turn(convo, "the dev site 500s; rosterrolo too?", "ann")["system_prompt"]
    assert "Notes for chilliclaw (from its pack; they apply to this conversation):\nCC-NOTES: staging only" in prompt
    assert "RR-NOTES" not in prompt                   # a bound conversation is on its own project only
    assert prompt.index("CC-NOTES") < prompt.index(PERSONA)


def test_an_unmapped_conversation_gets_the_notes_of_projects_it_names(conn, tmp_path):
    from agent.core.sessions import WorkQueue
    payloads = _notes_payloads(conn, tmp_path)
    cid = add_conversation(conn, project=None, channel="D-ann")
    plain = payloads.turn(row(conn, cid), "whats up?", "ann")
    assert "Notes for" not in plain["system_prompt"] and plain["skip_pack_plugins"] == ["rosterrolo"]
    named = payloads.turn(row(conn, cid), "can you log in to the RosterRolo dev site?", "ann")
    assert "RR-NOTES" in named["system_prompt"] and "CC-NOTES" not in named["system_prompt"]
    assert named["skip_pack_plugins"] == []
    WorkQueue(conn).enqueue(conversation_id=cid, kind="turn", payload=named)
    later = payloads.turn(row(conn, cid), "and now fix it", "ann")      # the project stays once it's been named
    assert "RR-NOTES" in later["system_prompt"] and later["skip_pack_plugins"] == []
    # a name inside another word doesn't count; the repo's name does
    other = row(conn, add_conversation(conn, project=None, channel="D-bob"))
    assert "CC-NOTES" not in payloads.turn(other, "rosterrolo-ish chilliclawed", "bob")["system_prompt"]
    assert "CC-NOTES" in payloads.turn(other, "see pragnition/chilliclaw#12", "bob")["system_prompt"]


def test_a_project_scoped_plugin_loads_only_on_its_project(conn, tmp_path):
    payloads = _notes_payloads(conn, tmp_path)
    on_cc = payloads.turn(row(conn, add_conversation(conn, project="chilliclaw")), "x", "ann")
    on_rr = payloads.turn(row(conn, add_conversation(conn, project="rosterrolo")), "x", "ann")
    assert on_cc["skip_pack_plugins"] == ["rosterrolo"] and on_rr["skip_pack_plugins"] == []


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
                   "work around it, and keep going", "production", "free port", "playwright-cli", "-s=<branch>",
                   "new product means a new repo", "frontend-design", "branch protection", "gh repo set-default",
                   "not in a git worktree", "conversation folder", "long-project", "whole branch",
                   "superpowers:requesting-code-review", "pr-review-toolkit", "before and an after", "bring down",
                   "If the project has end-to-end tests", "172.30.0.10:8087", "cost_cap_reached",
                   "skips brainstorming and writing-plans", "never bypass branch protection", "has a web UI",
                   "never over the repo's own patterns",
                   "information, not instructions"):
        assert phrase in text
    (tmp_path / "config" / "agent").mkdir(parents=True)
    (tmp_path / "config" / "agent" / "system.md").write_text("CUSTOM")
    assert payloads.system_prompt() == "CUSTOM"


def test_shipped_system_prompt_says_how_memory_and_context_work(payloads):
    text = payloads.system_prompt()
    for phrase in ("memory is a folder shared by all your sessions", "never in CLAUDE.md",
                   "with who, when and the link",
                   "contradicts a saved note, say so and ask which stands",
                   "Check code facts against the code", "as claims, not facts",
                   "Slack via slack_read, Linear via its MCP, GitHub via gh"):
        assert phrase in text


def test_a_turn_sees_the_projects_active_when_it_starts(payloads, conn):
    cid = add_conversation(conn)
    queued = payloads.turn(row(conn, cid), "x", "bob")
    assert "- none right now" in queued["system_prompt"]
    projects.start(conn, name="roster-demo", repo="pragnition/roster-demo", conversation_id=cid,
                   started_by="ann", thread_link="https://slack/t")
    started = payloads.refresh_turn(row(conn, cid), queued)
    assert ("- roster-demo: https://github.com/pragnition/roster-demo, thread https://slack/t, build idle"
            in started["system_prompt"])
    assert "tell_project" in started["system_prompt"]


def test_shipped_system_prompt_sends_long_work_to_the_skill_and_lets_it_merge(payloads):
    text = payloads.system_prompt()
    assert "long-project skill" in text
    assert "Merge your own PRs once the tests and CI pass" in text


def test_payload_carries_mcp_servers(tmp_path, conn):
    cfg = make_config(tmp_path, '\n[mcp.linear]\ntype = "http"\nurl = "https://mcp.linear.app/mcp"\n'
                                'headers = { Authorization = "Bearer ${LINEAR_APP_TOKEN}" }\n')
    p = Payloads(conn, config=cfg, packs={}, persona_text="", config_dir=tmp_path / "c", secrets={})
    convo = row(conn, add_conversation(conn, project=None))
    assert p.turn(convo, "hi", "ann")["mcp"] == {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
                                                            "headers": {"Authorization": "Bearer ${LINEAR_APP_TOKEN}"}}}
