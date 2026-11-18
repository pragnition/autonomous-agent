import os
import re
import subprocess
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


@pytest.fixture(scope="module")
def ci():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


@pytest.fixture(scope="module")
def improve_jensen():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text())


def _runs_on_labels(job):
    runs_on = job["runs-on"]
    return {runs_on} if isinstance(runs_on, str) else set(runs_on)


def _claude_steps(wf):
    """Every step, across every job, that calls the claude-code-action -- as `(job_name, step)` pairs."""
    steps = []
    for job_name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("anthropics/claude-code-action@"):
                steps.append((job_name, step))
    return steps


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


def test_schedules_folder_is_shared(compose):
    for name in ("core", "agent"):
        assert "${AGENT_DATA:-./data}/schedules:/data/schedules" in volumes(compose, name), name


def test_services_and_core_published_on_loopback_only(compose):
    assert set(compose["services"]) == {"core", "supervisor", "supervisor-standby", "relay", "agent", "docker"}
    assert "ports" not in compose["services"]["agent"] and "ports" not in compose["services"]["docker"]
    assert compose["services"]["core"]["ports"] == ["127.0.0.1:18085:8085", "127.0.0.1:18086:8086"]
    assert "ports" not in compose["services"]["supervisor"]
    assert "ports" not in compose["services"]["supervisor-standby"]


def test_no_env_file_and_no_secrets_in_environment(compose):
    for name, svc in compose["services"].items():
        if name == "agent":  # the agent's own keys are plain environment variables
            assert svc["env_file"] == ["${AGENT_DATA:-./data}/secrets/agent.env"]
        else:
            assert "env_file" not in svc, name
        env = svc.get("environment") or {}
        assert not any(re.search(r"TOKEN|SECRET|KEY|PASSWORD", k) for k in env), name


def test_only_supervisors_mount_docker_socket(compose):
    for name in compose["services"]:
        mounted = any("docker.sock" in v for v in volumes(compose, name))
        assert mounted == name.startswith("supervisor"), name


def test_supervisors_watch_each_other(compose):
    primary, standby = compose["services"]["supervisor"], compose["services"]["supervisor-standby"]
    assert primary["environment"]["SUPERVISOR_ROLE"] == "primary"
    assert standby["environment"]["SUPERVISOR_ROLE"] == "standby"
    for me, peer, peer_name in ((primary, standby, "supervisor-standby"), (standby, primary, "supervisor")):
        assert me["environment"]["PEER_CONTAINER"] == peer["container_name"]
        assert me["environment"]["PEER_HEALTH"] == f"http://{peer_name}:8090/health"
        assert me["environment"]["CORE_CONTAINER"] == compose["services"]["core"]["container_name"]
        assert me["environment"]["RUNNER_CONTAINER"] == compose["services"]["agent"]["container_name"]
        assert me["environment"]["RUNNER_HEALTH"] == "http://core:8080/health/runner"
        assert "${AGENT_DATA:-./data}/secrets/supervisor.env:/run/agent/secrets.env:ro" in [str(v) for v in me["volumes"]]


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


def test_task_files_are_shared_read_only_with_the_agent(compose):
    assert "${AGENT_DATA:-./data}/tasks:/data/tasks" in volumes(compose, "core")
    assert "${AGENT_DATA:-./data}/tasks:/data/tasks:ro" in volumes(compose, "agent")


def test_agent_toolkit_installs_exactly_the_vetted_set():
    script = ROOT / "deploy" / "agent-toolkit.sh"
    text = script.read_text()
    plugins = set(re.findall(r"^plugin (\S+)", text, re.M))
    skills = set(re.findall(r"^skill \S+ (\S+)", text, re.M))
    assert plugins == {"superpowers", "frontend-design", "pr-review-toolkit"}
    assert skills == {"web-design-guidelines", "vercel-react-best-practices", "vercel-composition-patterns",
                      "supabase-postgres-best-practices"}
    # Context7 runs as its local MCP server: the plugin's hosted one needs a login a headless agent can't do
    assert "claude mcp add -s user context7 -- npx -y @upstash/context7-mcp@" in text
    assert os.access(script, os.X_OK)


def test_agent_toolkit_runs_every_install_when_piped_into_bash(tmp_path):
    """It's run as `docker exec -i agent-runner bash < deploy/agent-toolkit.sh`, so bash reads the script from stdin.
    An installer that reads stdin too must not swallow the lines after it."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    (tmp_path / "home" / "skills").mkdir(parents=True)
    stubs = {"npx": f'cat >/dev/null; echo "npx $*" >> {log}',  # reads stdin, like an interactive installer
             "claude": 'case "$*" in "plugin marketplace list") echo anthropic-plugin-directory;; esac'}
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": str(tmp_path / "home")}
    with (ROOT / "deploy" / "agent-toolkit.sh").open() as script:
        subprocess.run(["bash"], stdin=script, env=env, check=True, capture_output=True)
    installed = [line.split("--skill ")[1].split()[0] for line in log.read_text().splitlines() if "skills@" in line]
    assert installed == ["web-design-guidelines", "vercel-react-best-practices", "vercel-composition-patterns",
                         "supabase-postgres-best-practices"]


# -- ci.yml / improve-jensen.yml --


def test_ci_runs_tests_on_github_hosted_runners_only(ci):
    assert ci["name"] == "ci"
    on = ci[True]  # YAML 1.1 parses the unquoted `on:` key as the boolean True
    assert on["push"]["branches"] == ["main"]
    assert "pull_request" in on
    assert set(ci["jobs"]) == {"python", "dashboard"}
    assert ci["permissions"] == {"contents": "read"}
    for name, job in ci["jobs"].items():
        assert "self-hosted" not in _runs_on_labels(job), name


def test_improve_jensen_triggers_and_jobs(improve_jensen):
    on = improve_jensen[True]
    assert on["schedule"] == [{"cron": "17 * * * *"}]
    dispatch_inputs = on["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["mode"]["default"] == "watch"
    assert set(dispatch_inputs["mode"]["options"]) == {"watch", "review", "fix"}
    assert dispatch_inputs["target"]["type"] == "string"
    for trigger in ("issues", "issue_comment", "pull_request_review_comment", "pull_request_review"):
        assert trigger in on
    assert set(improve_jensen["jobs"]) == {"watch", "review", "fix", "respond"}


def test_jobs_run_on_the_right_runners(improve_jensen):
    jobs = improve_jensen["jobs"]
    for name, want_label in (("watch", "watch"), ("review", "work"), ("fix", "work"), ("respond", "work")):
        labels = _runs_on_labels(jobs[name])
        assert labels == {"self-hosted", "improve-jensen", want_label}, name


def test_fix_is_serialised(improve_jensen):
    concurrency = improve_jensen["jobs"]["fix"]["concurrency"]
    assert concurrency["group"] == "improve-jensen-fix"
    assert concurrency["cancel-in-progress"] is False


def test_every_claude_step_uses_opus_and_an_instructions_file_that_exists(improve_jensen):
    claude_steps = _claude_steps(improve_jensen)
    assert len(claude_steps) == 4
    for job_name, step in claude_steps:
        claude_args = step["with"]["claude_args"]
        assert "--model claude-opus-5-5" in claude_args, job_name
        match = re.search(r"--append-system-prompt-file (\S+)", claude_args)
        assert match, (job_name, claude_args)
        assert (ROOT / match.group(1)).is_file(), (job_name, match.group(1))


def test_watch_commits_only_after_success_and_not_on_pool_hold(improve_jensen):
    steps = improve_jensen["jobs"]["watch"]["steps"]
    commit_step = next(s for s in steps if "jensen_watch.py commit" in str(s.get("run", "")))
    assert "pool_hold" in commit_step["if"]
    assert "steps.claude.outcome" in commit_step["if"]


def test_dispatch_branches_key_on_inputs_mode_not_just_event_name(improve_jensen):
    """A manual dispatch defaults `mode` to `watch`; the review/fix jobs' own `if` and their matrix/target
    expressions must all key on `inputs.mode`, not merely on `github.event_name == 'workflow_dispatch'` -- else
    a plain watch dispatch (with an empty `target`) would feed that empty value into review/fix instead of
    falling through to what `watch`'s own plan produced."""
    review_job = improve_jensen["jobs"]["review"]
    fix_job = improve_jensen["jobs"]["fix"]

    review_if = review_job["if"]
    assert "inputs.mode == 'review'" in review_if
    assert "inputs.target != ''" in review_if

    matrix_index = review_job["strategy"]["matrix"]["index"]
    assert "inputs.mode == 'review'" in matrix_index

    fix_if = fix_job["if"]
    assert "inputs.mode == 'fix'" in fix_if
    assert "inputs.target != ''" in fix_if

    target_source = str(fix_job.get("env", {}).get("TARGET", ""))
    assert "inputs.mode == 'fix'" in target_source

    # inputs.target is never interpolated directly into a run: script (shell-injection risk); every script
    # reads it through the quoted $TARGET env var instead.
    for step in fix_job["steps"]:
        run = str(step.get("run", ""))
        assert "inputs.target" not in run
        if "$TARGET" in run:
            assert '"$TARGET"' in run, run


def test_fixing_label_is_created_before_it_is_used(improve_jensen):
    steps = improve_jensen["jobs"]["fix"]["steps"]
    label_step = next(s for s in steps if "--add-label fixing" in str(s.get("run", "")))
    assert "gh label create fixing" in label_step["run"]
    assert label_step["run"].index("label create") < label_step["run"].index("--add-label fixing")


def test_fix_claude_step_leaves_room_for_the_always_cleanup(improve_jensen):
    claude_step = next(s for _, s in _claude_steps(improve_jensen) if s["with"].get("prompt", "").startswith(
        "Fix issue"))
    assert claude_step.get("timeout-minutes", 0) < improve_jensen["jobs"]["fix"]["timeout-minutes"]
