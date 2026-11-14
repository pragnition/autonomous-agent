import io
import json
import tarfile
from pathlib import Path

import pytest

from agent.core.bundles import build_bundle

ROOT = Path(__file__).parent.parent
SKILL_DIRS = [ROOT / "src" / "agent" / "plugins" / "core" / "skills",
              ROOT / "examples" / "pack-templates" / "chilliclaw" / "plugin" / "skills"]


def write_pack(tmp_path, name: str, skill: str) -> None:
    plugin = tmp_path / "packs" / name / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    (plugin / "skills" / skill).mkdir(parents=True)
    (plugin / "skills" / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n")


@pytest.fixture
def config_dir(tmp_path):
    write_pack(tmp_path, "sample", "sample-debugging")
    return tmp_path


def names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return set(tar.getnames())


def test_bundle_has_core_effortless_and_pack_plugins(config_dir):
    _, data = build_bundle(config_dir)
    files = names(data)
    assert "plugins/core/.claude-plugin/plugin.json" in files
    assert "plugins/core/skills/debugging/SKILL.md" in files
    assert "plugins/effortless/.claude-plugin/plugin.json" in files
    assert "plugins/effortless/hooks/register.tsx" in files
    assert "plugins/packs/sample/skills/sample-debugging/SKILL.md" in files


def test_bundle_has_every_pack_plugin(tmp_path):
    write_pack(tmp_path, "a", "a-debugging")
    write_pack(tmp_path, "b", "b-debugging")
    _, data = build_bundle(tmp_path)
    files = names(data)
    assert "plugins/packs/a/skills/a-debugging/SKILL.md" in files
    assert "plugins/packs/b/skills/b-debugging/SKILL.md" in files


def test_no_packs_dir_has_core_and_effortless_only(tmp_path):
    files = names(build_bundle(tmp_path)[1])
    assert "plugins/core/skills/debugging/SKILL.md" in files
    assert "plugins/effortless/.claude-plugin/plugin.json" in files
    assert not any(f.startswith("plugins/packs") for f in files)


def test_version_stable_and_changes_with_pack_content(config_dir):
    version, data = build_bundle(config_dir)
    assert build_bundle(config_dir) == (version, data)
    (config_dir / "packs" / "sample" / "plugin" / "skills" / "sample-debugging" / "SKILL.md").write_text(
        "---\nname: sample-debugging\n---\nchanged\n")
    assert build_bundle(config_dir)[0] != version


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
        "debugging", "build-feature", "browser-testing", "find-features", "playwright-cli", "review-before-pr",
        "pr-description"}
