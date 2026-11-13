import importlib.util
import shutil
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
    assert (pack.plugin_dir / ".claude-plugin" / "plugin.json").is_file()


async def test_doctor_flags_pack_without_checks(tmp_path):
    shutil.copytree(EXAMPLES, tmp_path / "config")
    shutil.copytree(TEMPLATES / "chilliclaw", tmp_path / "config" / "packs" / "chilliclaw")
    checks = {c.name: c for c in await run_checks(config_dir=tmp_path / "config",
                                                   secrets={k: "x" for k in REQUIRED_CORE})}
    assert checks["checks:sample"].ok is True
    assert checks["checks:chilliclaw"].ok is False and "no [build] checks" in checks["checks:chilliclaw"].detail


def test_smoke_script_importable():
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


def test_images_include_scripts():
    assert "COPY scripts /app/scripts" in (ROOT / "Dockerfile").read_text()
