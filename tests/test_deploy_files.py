import re
import tomllib
from pathlib import Path

import pytest
import yaml

from agent import db
from agent.ops import resume, stop
from agent.proxy import PLACEHOLDER_TOKEN

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_relay_binds_only_gateway_ip(compose):
    relay = compose["services"]["relay"]
    assert relay["network_mode"] == "host"
    command = " ".join(relay["command"]) if isinstance(relay["command"], list) else relay["command"]
    listens = re.findall(r"TCP-LISTEN:[^\s]+", command)
    assert len(listens) == 2 and all("bind=172.30.0.1" in l for l in listens)
    assert "TCP-LISTEN:28090" in command and "127.0.0.1:18090" in command
    assert "TCP-LISTEN:28091" in command and "127.0.0.1:18091" in command


def volumes(compose, name):
    return [str(v) for v in compose["services"][name].get("volumes", [])]


def test_core_gets_only_its_own_secrets(compose):
    vols = volumes(compose, "core")
    assert any(v.endswith("secrets/core.env:/run/agent/secrets.env:ro") for v in vols)
    assert not any("supervisor.env" in v for v in vols)
    assert "${AGENT_DATA:-./data}/secrets/projects:/data/secrets/projects:ro" in vols


def test_services_and_core_published_on_loopback_only(compose):
    assert set(compose["services"]) == {"core", "supervisor", "relay", "agent", "docker"}
    assert "ports" not in compose["services"]["agent"] and "ports" not in compose["services"]["docker"]
    assert compose["services"]["core"]["ports"] == ["127.0.0.1:18085:8085", "127.0.0.1:18086:8086"]
    assert "ports" not in compose["services"]["supervisor"]


def test_no_env_file_and_no_secrets_in_environment(compose):
    for name, svc in compose["services"].items():
        if name == "agent":  # the agent's own keys are plain environment variables
            assert svc["env_file"] == ["${AGENT_DATA:-./data}/secrets/agent.env"]
        else:
            assert "env_file" not in svc, name
        env = svc.get("environment") or {}
        assert not any(re.search(r"TOKEN|SECRET|KEY|PASSWORD", k) for k in env), name


def test_only_supervisor_mounts_docker_socket(compose):
    for name in compose["services"]:
        mounted = any("docker.sock" in v for v in volumes(compose, name))
        assert mounted == (name == "supervisor"), name


def test_restart_policies(compose):
    assert {s["restart"] for s in compose["services"].values()} == {"unless-stopped"}


def test_subnet_and_gateway(compose):
    [ipam] = compose["networks"]["agentnet"]["ipam"]["config"]
    assert (ipam["subnet"], ipam["gateway"]) == ("172.30.0.0/24", "172.30.0.1")
    assert compose["services"]["core"]["environment"]["PROXY_URL"] == "http://172.30.0.1:28090"


def test_proxy_unit_directives():
    unit = (ROOT / "deploy" / "claude-fallback-proxy.service").read_text()
    for line in ("Type=notify", "NotifyAccess=all", "ExitType=cgroup", "ExecReload=", "WantedBy=default.target"):
        assert line in unit
    reload = (ROOT / "deploy" / "proxy-reload.sh").read_text()
    assert "claude-fallback-proxy.next" in reload and "kill -HUP" in reload


def test_proxy_origins_example_matches_relay():
    data = tomllib.loads((ROOT / "deploy" / "proxy-origins.example.toml").read_text())
    assert data["origins"] == {"agent-core": 18090, "agent-sandbox": 18091}
    assert PLACEHOLDER_TOKEN in data["container_placeholders"]


def test_slack_manifest_scopes_and_events():
    m = yaml.safe_load((ROOT / "deploy" / "slack-app-manifest.yaml").read_text())
    assert m["settings"]["socket_mode_enabled"] is True
    assert {"app_mentions:read", "channels:history", "groups:history", "im:history", "chat:write",
            "reactions:write", "files:write", "users:read", "im:write"} <= set(m["oauth_config"]["scopes"]["bot"])
    assert "interactivity" not in m["settings"]
    assert {"app_mention", "message.channels", "message.groups", "message.im"} <= \
        set(m["settings"]["event_subscriptions"]["bot_events"])


async def test_ops_stop_and_resume(tmp_path):
    from agent.core.sessions import WorkQueue

    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    queue = WorkQueue(conn)
    item = queue.enqueue("V-1", "turn", {})
    queue.claim()
    assert stop(tmp_path / "agent.db") == [item]
    assert db.get_setting(conn, "paused") == "1" and queue.get(item).status == "cancelled"
    resume(tmp_path / "agent.db")
    assert db.get_setting(conn, "paused") == "0"


def test_core_image_has_az():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "azure-cli" in dockerfile and "/usr/local/bin/az" in dockerfile


def agent_target():
    dockerfile = (ROOT / "Dockerfile").read_text()
    return dockerfile.split("FROM app AS agent", 1)[1].split("\nFROM ", 1)[0]


def test_agent_image():
    target = agent_target()
    for needle in ("--uid 1001", "--shell /bin/bash", "NOPASSWD:ALL", "ln -sf /bin/bash /bin/sh", "USER agent",
                   "AZURE_CONFIG_DIR=/data/cli/azure", " gh", "azure-cli", "chown agent /var/agent",
                   '"agent", "runner"', "chmod -R a+rX /app"):
        assert needle in target, needle


def test_agent_service(compose):
    agent = compose["services"]["agent"]
    assert agent["build"]["target"] == "agent" and agent["restart"] == "unless-stopped"
    assert agent["network_mode"] == "service:docker" and "networks" not in agent and not agent.get("privileged")
    vols = volumes(compose, "agent")
    assert "${AGENT_DATA:-./data}/runner:/run/agent:ro" in vols
    assert "${AGENT_DATA:-./data}/cli/azure:/data/cli/azure" in vols
    assert not any("agent.env" in v for v in vols)        # a 0600 file mounted in would be unreadable to uid 1001
    assert "agentwork:/var/agent" in vols and "agentwork" in compose["volumes"]
    command = " ".join(agent["command"])
    for part in ("agent runner", "--core http://core:8080", "--proxy http://172.30.0.1:28091",
                 "--token-file /run/agent/runner-token"):
        assert part in command, part
    assert "${AGENT_DATA:-./data}/runner:/data/runner" in volumes(compose, "core")


def test_core_has_a_fixed_address_for_the_meter(compose):
    assert compose["services"]["core"]["networks"]["agentnet"]["ipv4_address"] == "172.30.0.10"


def test_agent_docker_daemon(compose):
    # the agent's own dockerd: its API only on the shared namespace's loopback, the work volume at the same path
    docker = compose["services"]["docker"]
    assert docker["image"].endswith("-dind") and docker["privileged"] is True
    assert docker["networks"] == ["agentnet"] and docker["environment"]["DOCKER_TLS_CERTDIR"] == ""
    assert docker["command"][0] == "dockerd"  # otherwise the image adds a 0.0.0.0:2375 listener of its own
    assert "--host=tcp://127.0.0.1:2375" in docker["command"] and not any("0.0.0.0" in c for c in docker["command"])
    assert "agentwork:/var/agent" in volumes(compose, "docker") and "agentdocker" in compose["volumes"]
    assert compose["services"]["agent"]["depends_on"] == ["docker"]


def test_agent_image_can_run_and_drive_apps():
    target = agent_target()
    for needle in ("docker:29-cli", "cli-plugins", "oven/bun:", "rust-just", "@playwright/cli@",
                   "install-browser chromium --with-deps", "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright",
                   "DOCKER_HOST=tcp://127.0.0.1:2375", "fonts-noto-color-emoji"):
        assert needle in target, needle


def test_core_publishes_public_port(compose):
    # only the Linear webhook/OAuth listener goes through Tailscale Funnel; both stay on loopback
    assert "127.0.0.1:18085:8085" in compose["services"]["core"]["ports"]
