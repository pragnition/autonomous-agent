import json
from pathlib import Path

import pytest

from agent.config import (ConfigError, InjectRule, SandboxResources, load_agent_config, load_pack, load_packs)

EXAMPLES = Path(__file__).parent.parent / "examples" / "config"

MIN_AGENT = """name = "chilli"

[people.ann]
slack = "U1"
github = "ann"
linear = "ann@example.com"
role = "admin"
"""

PROJECT = """[project]
name = "demo"
repo = "acme/demo"
default_branch = "main"
owner = "ann"
slack_channel = "#demo"
linear_team = "DEM"

[build]
setup = ["true"]
checks = ["true"]

[secrets]
worker = ["DEMO_TEST_USER"]
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_pack(root: Path, project: str = PROJECT) -> Path:
    d = root / "packs" / "demo"
    write(d / "project.toml", project)
    write(d / "plugin" / ".claude-plugin" / "plugin.json", json.dumps({"name": "demo"}))
    return d


def write_agent(tmp_path, extra: str) -> Path:
    return write(tmp_path / "agent.toml", MIN_AGENT + "\n" + extra)


def write_pack(tmp_path, extra: str) -> Path:
    return make_pack(tmp_path, PROJECT + "\n" + extra)


def agent(tmp_path, text=MIN_AGENT):
    return load_agent_config(write(tmp_path / "agent.toml", text))


def agent_cfg():
    import tempfile
    d = Path(tempfile.mkdtemp())
    return load_agent_config(write(d / "agent.toml", MIN_AGENT))


def problems_of(fn):
    with pytest.raises(ConfigError) as e:
        fn()
    return e.value.problems


def test_defaults(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, ""))
    assert cfg.limits.max_concurrent_sessions == 3 and cfg.limits.task_minutes == 90
    assert cfg.limits.max_tasks_per_day == 20 and cfg.limits.max_turns == 400
    assert cfg.models.default == "claude-opus-5-5" and cfg.models.hard == "claude-fable-5-1"
    assert cfg.models.chores == "claude-sonnet-5"
    assert cfg.sandbox.resources["code"] == SandboxResources(4, 8, 30)
    assert cfg.sandbox.resources["debug"] == SandboxResources(2, 2, 10)
    assert cfg.sandbox.resources["general"] == SandboxResources(2, 2, 10)
    assert cfg.sandbox.host_memory_budget == 0.75
    assert (cfg.sandbox.idle_stop_minutes, cfg.sandbox.rebuild_every_days) == (30, 7)
    assert set(cfg.memory_budget.briefing) >= {"session", "production", "ambient"}
    assert cfg.cli == {}


def test_example_config_loads():
    cfg = load_agent_config(EXAMPLES / "agent.toml")
    assert cfg.timezone == "Asia/Singapore"
    assert cfg.cli["azure"].command == "az"
    assert cfg.cli["azure"].profiles == ["debug"]


def test_az_builtin_deny_always_present(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[cli.azure]\ncommand = "az"\nprofiles = ["debug"]\ndeny = []'))
    assert {"account get-access-token", "rest"} <= set(cfg.cli["azure"].deny)
    assert cfg.cli["azure"].max_output_chars == 8000


def test_cli_only_in_debug_profile(tmp_path):
    with pytest.raises(ConfigError, match="debug"):
        load_agent_config(write_agent(tmp_path, '[cli.azure]\ncommand = "az"\nprofiles = ["code"]'))


def test_cli_egress_rejects_wildcards(tmp_path):
    with pytest.raises(ConfigError, match="wildcard"):
        load_agent_config(write_agent(tmp_path, '[cli.azure]\ncommand = "az"\nprofiles = ["debug"]\n'
                                                'egress = ["*.azure.com"]'))


def test_sandbox_resource_override(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[sandbox.resources.code]\ncpus = 6\nmemory_gb = 12\ndisk_gb = 40'))
    assert cfg.sandbox.resources["code"] == SandboxResources(6, 12, 40)
    assert cfg.sandbox.resources["debug"] == SandboxResources(2, 2, 10)


def test_unknown_sandbox_profile(tmp_path):
    with pytest.raises(ConfigError, match="profile"):
        load_agent_config(write_agent(tmp_path, '[sandbox.resources.gpu]\ncpus = 1\nmemory_gb = 1\ndisk_gb = 1'))


def test_people_lookups(tmp_path):
    cfg = agent(tmp_path)
    assert cfg.person_by_slack("U1").key == "ann"
    assert cfg.person_by_linear("ANN@example.com").key == "ann"
    assert cfg.person_by_slack("U9") is None


def test_missing_name_is_reported(tmp_path):
    probs = problems_of(lambda: agent(tmp_path, MIN_AGENT.replace('name = "chilli"\n', "")))
    assert any("name" in p for p in probs)


def test_bad_person_role(tmp_path):
    probs = problems_of(lambda: agent(tmp_path, MIN_AGENT.replace('role = "admin"', 'role = "owner"')))
    assert any("role" in p for p in probs)


def test_sample_pack_loads():
    packs = load_packs(EXAMPLES, load_agent_config(EXAMPLES / "agent.toml"))
    assert "sample" in packs
    assert packs["sample"].project.build_checks
    assert set(packs["sample"].project.linear_status) == {"working", "ready_for_review", "merged", "failed"}


def test_valid_custom_pack_loads(tmp_path):
    p = load_pack(make_pack(tmp_path), agent(tmp_path)).project
    assert p.secrets_worker == ["DEMO_TEST_USER"]
    assert p.network == "open" and p.egress_allow == [] and p.scrub_patterns == []


def test_pack_owner_must_be_a_person(tmp_path):
    d = make_pack(tmp_path, PROJECT.replace('owner = "ann"', 'owner = "zed"'))
    assert any("owner" in p for p in problems_of(lambda: load_pack(d, agent(tmp_path))))


def test_pack_rejects_reserved_secret_names(tmp_path):
    d = make_pack(tmp_path, PROJECT.replace('worker = ["DEMO_TEST_USER"]', 'worker = ["GITHUB_WRITE_TOKEN"]'))
    assert any("GITHUB_WRITE_TOKEN" in p for p in problems_of(lambda: load_pack(d, agent(tmp_path))))


def test_pack_rejects_wildcard_egress(tmp_path):
    with pytest.raises(ConfigError, match="wildcard"):
        load_pack(write_pack(tmp_path, '[egress]\nallow = ["*.github.io"]'), agent_cfg())


def test_pack_network_values(tmp_path):
    assert load_pack(write_pack(tmp_path, ""), agent_cfg()).project.network == "open"
    read = make_pack(tmp_path, PROJECT.replace('linear_team = "DEM"', 'linear_team = "DEM"\nnetwork = "read"'))
    assert load_pack(read, agent_cfg()).project.network == "read"
    with pytest.raises(ConfigError, match="network"):
        load_pack(make_pack(tmp_path, PROJECT.replace('linear_team = "DEM"', 'linear_team = "DEM"\nnetwork = "closed"')),
                  agent_cfg())


def test_pack_inject_and_setup(tmp_path):
    p = load_pack(write_pack(tmp_path, '[egress.inject."x.example.com"]\nheader = "api-key"\nsecret = "X_KEY"\n'
                                       '[sandbox.code]\nsetup = ["apt-get install -y jq"]'), agent_cfg()).project
    assert p.egress_inject["x.example.com"] == InjectRule("api-key", "X_KEY")
    assert p.sandbox_setup["code"] == ["apt-get install -y jq"]


def test_pack_sandbox_resources_and_template(tmp_path):
    p = load_pack(write_pack(tmp_path, '[sandbox.code]\ncpus = 8\nmemory_gb = 16\ndisk_gb = 60\ntemplate = "my/img:1"'),
                  agent_cfg()).project
    assert p.sandbox_resources["code"] == SandboxResources(8, 16, 60)
    assert p.sandbox_template["code"] == "my/img:1"


def test_pack_unknown_sandbox_profile(tmp_path):
    with pytest.raises(ConfigError, match="profile"):
        load_pack(write_pack(tmp_path, '[sandbox.gpu]\nsetup = ["x"]'), agent_cfg())


def test_pack_scrub_patterns_must_compile(tmp_path):
    with pytest.raises(ConfigError, match="scrub"):
        load_pack(write_pack(tmp_path, '[scrub]\npatterns = ["C-(\\\\d"]'), agent_cfg())
