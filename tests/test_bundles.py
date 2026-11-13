import io
import json
import tarfile
from pathlib import Path

import pytest

from agent.core.bundles import build_bundle

ROOT = Path(__file__).parent.parent
SKILL_DIRS = [ROOT / "src" / "agent" / "plugins" / "core" / "skills",
              ROOT / "examples" / "pack-templates" / "chilliclaw" / "plugin" / "skills"]


@pytest.fixture
def config_dir(tmp_path):
    plugin = tmp_path / "packs" / "sample" / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "sample"}))
    (plugin / "skills" / "sample-debugging").mkdir(parents=True)
    (plugin / "skills" / "sample-debugging" / "SKILL.md").write_text("---\nname: sample-debugging\n---\n")
    return tmp_path


def names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return set(tar.getnames())


def test_general_bundle_has_no_pack_plugin(config_dir):
    _, data = build_bundle(config_dir, "general", None)
    files = names(data)
    assert "system.md" in files and "plugins/core/skills/fix-bug/SKILL.md" in files
    assert "plugins/effortless/hooks/register.tsx" in files
    assert not any(f.startswith("plugins/pack") for f in files)


def test_code_bundle_has_both_plugins(config_dir):
    _, data = build_bundle(config_dir, "code", "sample")
    files = names(data)
    assert "plugins/core/.claude-plugin/plugin.json" in files
    assert "plugins/effortless/.claude-plugin/plugin.json" in files
    assert "plugins/pack/skills/sample-debugging/SKILL.md" in files


def test_version_stable_and_changes_with_system_md(config_dir):
    v1, d1 = build_bundle(config_dir, "code", "sample")
    v2, d2 = build_bundle(config_dir, "code", "sample")
    assert v1 == v2 and d1 == d2
    (config_dir / "agent").mkdir()
    (config_dir / "agent" / "system.md").write_text("custom")
    assert build_bundle(config_dir, "code", "sample")[0] != v1


def test_unknown_project_has_no_pack_plugin(config_dir):
    assert not any(f.startswith("plugins/pack") for f in names(build_bundle(config_dir, "code", "nope")[1]))


@pytest.mark.parametrize("skill", [p for d in SKILL_DIRS for p in sorted(d.glob("*/SKILL.md"))],
                         ids=lambda p: p.parent.name)
def test_every_skill_has_name_and_description(skill):
    text = skill.read_text()
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    fields = dict(line.split(": ", 1) for line in front.strip().splitlines())
    assert fields["name"] == skill.parent.name and len(fields["description"]) > 20


def test_core_skills_present():
    assert {p.parent.name for p in SKILL_DIRS[0].glob("*/SKILL.md")} == {
        "fix-bug", "build-feature", "verify-in-app", "review-before-pr", "pr-description"}
