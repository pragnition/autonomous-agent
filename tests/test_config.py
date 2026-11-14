import json
import tomllib
from pathlib import Path

import pytest

from agent.config import ConfigError, load_agent_config, load_pack, load_packs

EXAMPLES = Path(__file__).parent.parent / "examples" / "config"

MIN_AGENT = """name = "jensen"

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


def agent(tmp_path, text=MIN_AGENT):
    return load_agent_config(write(tmp_path / "agent.toml", text))


def problems_of(fn):
    with pytest.raises(ConfigError) as e:
        fn()
    return e.value.problems


def test_defaults(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, ""))
    assert cfg.limits.max_concurrent_sessions == 10 and cfg.limits.task_minutes == 90
    assert cfg.limits.max_tasks_per_day == 20 and cfg.limits.max_turns == 400
    assert cfg.models.default == "claude-opus-5-5" and cfg.models.hard == "claude-fable-5-1"
    assert cfg.agent.uid == 1001
    assert cfg.cli == {}


def test_github_login_defaults_to_name(tmp_path):
    assert agent(tmp_path).github == "jensen"


def test_github_login_can_differ_from_name(tmp_path):
    assert agent(tmp_path, 'github = "jensen-bot"\n' + MIN_AGENT).github == "jensen-bot"


def test_example_config_loads():
    cfg = load_agent_config(EXAMPLES / "agent.toml")
    assert cfg.timezone == "Asia/Singapore"
    assert cfg.cli["azure"].command == "az"


def test_cli_rules_keep_only_login_fields():
    cfg = load_agent_config(EXAMPLES / "agent.toml")
    assert set(vars(cfg.cli["azure"])) == {"command", "env", "login", "login_check"}


def test_people_lookups(tmp_path):
    cfg = agent(tmp_path)
    assert cfg.person_by_slack("U1").key == "ann"
    assert cfg.person_by_linear("ANN@example.com").key == "ann"
    assert cfg.person_by_slack("U9") is None


def test_missing_name_is_reported(tmp_path):
    probs = problems_of(lambda: agent(tmp_path, MIN_AGENT.replace('name = "jensen"\n', "")))
    assert any("name" in p for p in probs)


def test_bad_person_role(tmp_path):
    probs = problems_of(lambda: agent(tmp_path, MIN_AGENT.replace('role = "admin"', 'role = "owner"')))
    assert any("role" in p for p in probs)


def test_sample_pack_loads():
    packs = load_packs(EXAMPLES, load_agent_config(EXAMPLES / "agent.toml"))
    assert "sample" in packs
    assert set(packs["sample"].project.linear_status) == {"working", "ready_for_review", "merged", "failed"}


def test_valid_custom_pack_loads(tmp_path):
    p = load_pack(make_pack(tmp_path), agent(tmp_path)).project
    assert p.secrets_worker == ["DEMO_TEST_USER"]


def test_a_pack_with_a_leftover_build_table_still_loads(tmp_path):
    """Installs from before the agent set up its own clones still have [build] setup and checks: ignored."""
    assert "[build]" in PROJECT
    assert load_pack(make_pack(tmp_path), agent(tmp_path)).project.name == "demo"


def test_pack_owner_must_be_a_person(tmp_path):
    d = make_pack(tmp_path, PROJECT.replace('owner = "ann"', 'owner = "zed"'))
    assert any("owner" in p for p in problems_of(lambda: load_pack(d, agent(tmp_path))))


def test_pack_rejects_reserved_secret_names(tmp_path):
    d = make_pack(tmp_path, PROJECT.replace('worker = ["DEMO_TEST_USER"]', 'worker = ["GITHUB_WRITE_TOKEN"]'))
    assert any("GITHUB_WRITE_TOKEN" in p for p in problems_of(lambda: load_pack(d, agent(tmp_path))))


@pytest.mark.parametrize("value", ["0", '"x"'])
def test_agent_uid_must_be_positive_int(tmp_path, value):
    probs = problems_of(lambda: load_agent_config(write_agent(tmp_path, f"[agent]\nuid = {value}")))
    assert any("uid" in p for p in probs)


def test_observability_settings(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[observability]\ndigest_channel = "#agent-ops"\ndigest_time = "08:30"'))
    assert (cfg.observability.digest_channel, cfg.observability.digest_time) == ("#agent-ops", "08:30")
    assert agent(tmp_path).observability.digest_time == "09:00"
    probs = problems_of(lambda: load_agent_config(write_agent(tmp_path, '[observability]\ndigest_time = "8am"')))
    assert any("HH:MM" in p for p in probs)


def test_example_config_has_observability_section():
    assert "observability" in tomllib.loads((EXAMPLES / "agent.toml").read_text())


def test_linear_public_url(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[linear]\npublic_url = "https://jensen.example.ts.net"'))
    assert cfg.linear.public_url == "https://jensen.example.ts.net"
    assert agent(tmp_path).linear.public_url == ""
    probs = problems_of(lambda: load_agent_config(write_agent(tmp_path, '[linear]\npublic_url = "http://x"')))
    assert any("public_url" in p for p in probs)


def test_mcp_servers(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[mcp.linear]\ntype = "http"\nurl = "https://mcp.linear.app/mcp"\n'
                                                  'headers = { Authorization = "Bearer ${T}" }'))
    server = cfg.mcp["linear"]
    assert (server.type, server.url, server.headers) == ("http", "https://mcp.linear.app/mcp",
                                                         {"Authorization": "Bearer ${T}"})
    for bad in ('type = "stdio"\nurl = "https://x"', 'type = "http"\nurl = "http://x"',
                'type = "http"\nurl = "https://x"\nheaders = { A = 1 }'):
        probs = problems_of(lambda: load_agent_config(write_agent(tmp_path, f"[mcp.x]\n{bad}")))
        assert any("mcp.x" in p for p in probs), bad


def test_example_config_has_linear_mcp():
    cfg = load_agent_config(EXAMPLES / "agent.toml")
    assert cfg.mcp["linear"].headers["Authorization"] == "Bearer ${LINEAR_APP_TOKEN}"


def test_person_github_is_optional(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('name = "agent"\n[people.ann]\nslack = "U1"\nlinear = "ann@example.com"\n')
    assert load_agent_config(path).people["ann"].github == ""
