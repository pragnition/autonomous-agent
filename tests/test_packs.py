import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

from agent.config import load_agent_config, load_pack, load_packs
from agent.tasks import Status

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples" / "config"
TEMPLATES = ROOT / "examples" / "pack-templates"


def test_all_example_packs_load():
    config = load_agent_config(EXAMPLES / "agent.toml")
    assert "sample" in load_packs(EXAMPLES, config)
    assert load_pack(TEMPLATES / "chilliclaw", config).project.name == "chilliclaw"
    assert (EXAMPLES / "persona.toml").read_text() == (ROOT / "src/agent/defaults/persona.toml").read_text()


def test_chilliclaw_pack_status_keys_valid():
    pack = load_pack(TEMPLATES / "chilliclaw", load_agent_config(EXAMPLES / "agent.toml"))
    assert set(pack.project.linear_status) <= {s.value for s in Status}
    assert (TEMPLATES / "chilliclaw" / "plugin" / ".claude-plugin" / "plugin.json").is_file()


def test_chilliclaw_pack_runs_locally_through_the_meter():
    local = TEMPLATES / "chilliclaw" / "plugin" / "skills" / "chilliclaw-local"
    overlay = tomllib.loads((local / "agent-overlay.toml").read_text())
    for name in ("main", "email_summary", "usecase_classifier"):
        deployment = overlay["llm"]["deployments"][name]
        assert deployment["backend"] == "foundry" and deployment["foundry"]["api_key_env"] == "AGENT_LLM_KEY"
        assert deployment["foundry"]["base_url"] == "http://172.30.0.10:8087/llm/chilliclaw-dev"
    assert "integrations" not in overlay and overlay["workspace"]["generate_image"]["enabled"] is False
    write_env = (local / "write-env.py").read_text()
    assert "/run/agent/projects/chilliclaw.env" in write_env and "AGENT_LLM_KEY=metered" in write_env
    assert "bun install" in (local / "setup.sh").read_text()
    skill = (local / "SKILL.md").read_text()
    assert "chilliclaw-dev-user-id" in skill and "gh repo clone pragnition/chilliclaw" in skill
    assert "bash <skill-dir>/setup.sh" in skill


def test_chilliclaw_setup_writes_env_from_the_team_file(tmp_path, monkeypatch):
    """write-env.py, run in a clone: the team's values win over .env.example's, placeholders get local values, blank
    generated tokens are left out, and the model key is the meter's stand-in."""
    local = TEMPLATES / "chilliclaw" / "plugin" / "skills" / "chilliclaw-local"
    team = tmp_path / "team.env"
    team.write_text("SLACK_BOT_TOKEN=xoxb-team\n")
    (tmp_path / ".env.example").write_text("SLACK_BOT_TOKEN=REPLACE_ME\nNEW_KEY=default\nDSL_EXECUTOR_TOKEN=\n"
                                           "CREDENTIAL_ENCRYPTION_KEY=REPLACE_ME\n")
    source = (local / "write-env.py").read_text().replace('"/run/agent/projects/chilliclaw.env"', repr(str(team)))
    monkeypatch.chdir(tmp_path)
    exec(compile(source, "write-env.py", "exec"), {"__name__": "__main__"})
    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines())
    assert env["SLACK_BOT_TOKEN"] == "xoxb-team" and env["NEW_KEY"] == "default" and "DSL_EXECUTOR_TOKEN" not in env
    assert len(env["CREDENTIAL_ENCRYPTION_KEY"]) == 64 and env["AGENT_LLM_KEY"] == "metered"


def test_smoke_script_queues_its_turn(tmp_path, monkeypatch, capsys):
    """`just smoke` gets as far as queueing its turn; with no agent container to answer, it times out at once."""
    from agent import db, paths

    (tmp_path / "db").mkdir()
    db.create_schema(db.connect(tmp_path / "db" / "agent.db"))
    shutil.copytree(EXAMPLES, tmp_path / "config")
    monkeypatch.setattr(paths, "DB_DIR", tmp_path / "db")
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(paths, "SECRETS_FILE", tmp_path / "core.env")
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "TIMEOUT_S", 0)
    assert module.main() == 1 and "timed out" in capsys.readouterr().out
    [payload] = [json.loads(r[0]) for r in db.connect(tmp_path / "db" / "agent.db").execute(
        "SELECT payload FROM work_items WHERE kind = 'turn'")]
    assert payload["text"] == module.PROMPT and payload["system_prompt"]


def test_nothing_imports_agent_memory():
    """Sessions keep what they learn in Claude Code's own memory; core has no memory store of its own."""
    assert importlib.util.find_spec("agent.memory") is None
    imports = re.compile(r"^\s*(from\s+agent\.memory\b|import\s+agent\.memory\b|from\s+agent\s+import\s+.*\bmemory\b)",
                         re.M)
    sources = [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/*.py"), *ROOT.glob("tests/*.py")]
    assert ROOT / "scripts" / "smoke.py" in sources
    assert [str(p.relative_to(ROOT)) for p in sources if imports.search(p.read_text())] == []


def test_images_include_scripts():
    assert "COPY scripts /app/scripts" in (ROOT / "Dockerfile").read_text()
