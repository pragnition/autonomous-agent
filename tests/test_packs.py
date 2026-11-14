import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

from agent.config import load_agent_config, load_pack, load_packs
from agent.doctor import REQUIRED_CORE, run_checks
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


async def test_doctor_flags_pack_without_checks(tmp_path):
    shutil.copytree(EXAMPLES, tmp_path / "config")
    pack = tmp_path / "config" / "packs" / "chilliclaw"
    shutil.copytree(TEMPLATES / "chilliclaw", pack)
    checks = {c.name: c for c in await run_checks(config_dir=tmp_path / "config",
                                                   secrets={k: "x" for k in REQUIRED_CORE})}
    assert checks["checks:sample"].ok is True and checks["checks:chilliclaw"].ok is True
    toml = pack / "project.toml"
    toml.write_text(toml.read_text().replace('checks = ["just check"]', "checks = []"))
    checks = {c.name: c for c in await run_checks(config_dir=tmp_path / "config",
                                                   secrets={k: "x" for k in REQUIRED_CORE})}
    assert checks["checks:chilliclaw"].ok is False and "no [build] checks" in checks["checks:chilliclaw"].detail


def test_chilliclaw_pack_runs_locally_through_the_meter():
    pack = load_pack(TEMPLATES / "chilliclaw", load_agent_config(EXAMPLES / "agent.toml"))
    assert pack.project.build_checks == ["just check"] and any("bun install" in c for c in pack.project.build_setup)
    local = TEMPLATES / "chilliclaw" / "plugin" / "skills" / "chilliclaw-local"
    overlay = tomllib.loads((local / "agent-overlay.toml").read_text())
    for name in ("main", "email_summary", "usecase_classifier"):
        deployment = overlay["llm"]["deployments"][name]
        assert deployment["backend"] == "foundry" and deployment["foundry"]["api_key_env"] == "AGENT_LLM_KEY"
        assert deployment["foundry"]["base_url"] == "http://172.30.0.10:8087/llm/chilliclaw-dev"
    assert "integrations" not in overlay and overlay["workspace"]["generate_image"]["enabled"] is False
    assert "/run/agent/projects/chilliclaw.env" in pack.project.build_setup[0]
    assert "AGENT_LLM_KEY=metered" in " ".join(pack.project.build_setup)
    assert "chilliclaw-dev-user-id" in (local / "SKILL.md").read_text()


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
