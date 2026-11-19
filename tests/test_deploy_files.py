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
    assert agent["init"] is True  # PID 1 is an init that reaps orphaned processes, not the runner
    assert agent["stop_grace_period"] == "30s"  # time for the runner to stop the agent's services
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


def test_the_agents_home_and_browsers_live_on_the_volume():
    """A rebuilt container starts from the image, so anything the agent installs outside the volume is lost: its home
    folder (pip --user, uv tools, npm -g, bun, dotfiles) and the Playwright browsers are links into /var/agent, and
    the runner seeds them there (agent.runner.home)."""
    target = agent_target()
    for needle in ("rm -rf /home/agent && ln -s /var/agent/home /home/agent",
                   "mv /opt/ms-playwright /opt/ms-playwright-image && ln -s /var/agent/ms-playwright /opt/ms-playwright",
                   "NPM_CONFIG_PREFIX=/home/agent/.local", 'PATH="/home/agent/.local/bin:/home/agent/.bun/bin:${PATH}"'):
        assert needle in target, needle
    assert "ln -s /var/agent/claude /home/agent/.claude" not in target  # the runner makes that link, in the volume


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


def test_jensens_own_requests_never_run_without_approval(improve_jensen):
    # An issue Jensen files itself is proposed in Slack by the watch; only a person's own request runs at once.
    assert "github.actor != vars.JENSEN_GITHUB_LOGIN" in improve_jensen["jobs"]["respond"]["if"]


def test_improve_jensen_waits_for_its_subagents(improve_jensen):
    # A job ends when Claude's turn ends: a background subagent would die with nothing written.
    assert improve_jensen["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_improve_jensen_triggers_and_jobs(improve_jensen):
    on = improve_jensen[True]
    # GitHub's cron never fired these; the host listener sends the clock as repository_dispatch events instead.
    assert "schedule" not in on
    assert on["repository_dispatch"] == {"types": list(_DISPATCH_TYPES.values())}
    dispatch_inputs = on["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["mode"]["default"] == "watch"
    assert set(dispatch_inputs["mode"]["options"]) == {"watch", "review", "fix", "assess", "project", "approvals"}
    assert dispatch_inputs["target"]["type"] == "string"
    for trigger in ("issues", "issue_comment"):
        assert trigger in on
    # A PR review runs the workflow from the PR's own merge ref, which for a fork is the fork's code on these runners:
    # every trigger left runs main's workflow.
    assert not {"pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment"} & set(on)
    assert set(improve_jensen["jobs"]) == {"watch", "review", "fix", "respond", "assess", "project", "approvals"}


def test_jobs_run_on_the_right_runners(improve_jensen):
    jobs = improve_jensen["jobs"]
    for name, want_label in (("watch", "watch"), ("review", "work"), ("fix", "work"),
                             ("assess", "watch"), ("project", "work"), ("approvals", "work")):
        labels = _runs_on_labels(jobs[name])
        assert labels == {"self-hosted", "improve-jensen", want_label}, name


def test_respond_runs_on_the_old_runner_until_the_engine_switch_is_on(improve_jensen):
    """Today's one live runner has no `work` label. Until a person sets `vars.IMPROVE_JENSEN_ENGINE` to `on` (after
    deploy/improve-jensen.md's new runners exist), `respond` must keep matching just `[self-hosted,
    improve-jensen]`, or merging this workflow would stop it answering issues at all."""
    runs_on = improve_jensen["jobs"]["respond"]["runs-on"]
    assert runs_on == (
        "${{ fromJSON(vars.IMPROVE_JENSEN_ENGINE == 'on' && '[\"self-hosted\", \"improve-jensen\", \"work\"]' || "
        "'[\"self-hosted\", \"improve-jensen\"]') }}")


def test_respond_tells_claude_to_stop_at_the_pr_when_the_engine_is_off(improve_jensen):
    """claude-code-action appends a non-empty `prompt` to tag mode's auto-generated one as `<custom_instructions>`;
    while the engine is off, respond must tell Claude to open the PR and leave the merge and deploy to a person."""
    respond_claude = next(s for job_name, s in _claude_steps(improve_jensen) if job_name == "respond")
    prompt = respond_claude["with"]["prompt"]
    assert "vars.IMPROVE_JENSEN_ENGINE != 'on'" in prompt
    assert "The engine is not switched on: open the PR and stop there." in prompt
    assert "Don''t merge or deploy; a person merges." in prompt


def test_fix_is_serialised(improve_jensen):
    concurrency = improve_jensen["jobs"]["fix"]["concurrency"]
    assert concurrency["group"] == "improve-jensen-fix"
    assert concurrency["cancel-in-progress"] is False


def test_every_claude_step_uses_opus_and_an_instructions_file_that_exists(improve_jensen):
    claude_steps = _claude_steps(improve_jensen)
    assert sorted(job_name for job_name, _ in claude_steps) == ["assess", "fix", "project", "respond", "review",
                                                                "watch"]
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
    """A manual dispatch defaults `mode` to `watch`; the review/fix/project jobs' own `if` and their matrix/target
    expressions must all key on `inputs.mode`, not merely on `github.event_name == 'workflow_dispatch'` -- else
    a plain watch dispatch (with an empty `target`) would feed that empty value into them instead of falling through
    to what `watch`'s plan or `approvals` produced."""
    review_job = improve_jensen["jobs"]["review"]

    review_if = review_job["if"]
    assert "inputs.mode == 'review'" in review_if
    assert "inputs.target != ''" in review_if

    matrix_index = review_job["strategy"]["matrix"]["index"]
    assert "inputs.mode == 'review'" in matrix_index

    for name in ("fix", "project"):
        job = improve_jensen["jobs"][name]
        assert f"inputs.mode == '{name}'" in job["if"], name
        assert "inputs.target != ''" in job["if"], name
        assert f"inputs.mode == '{name}'" in str(job.get("env", {}).get("TARGET", "")), name

        # inputs.target is never interpolated directly into a run: script (shell-injection risk); every script
        # reads it through the quoted $TARGET env var instead.
        for step in job["steps"]:
            run = str(step.get("run", ""))
            assert "inputs.target" not in run, name
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


def test_fix_and_respond_push_with_the_claude_app_not_the_job_token(improve_jensen):
    # A push made with the job's GITHUB_TOKEN never triggers `ci`, so `gh pr checks --watch` would wait forever:
    # the checkout must not leave that token in the git config for the Claude step's pushes to pick up.
    for name in ("fix", "respond", "project"):
        checkout = next(s for s in improve_jensen["jobs"][name]["steps"]
                        if str(s.get("uses", "")).startswith("actions/checkout@"))
        assert checkout["with"]["persist-credentials"] is False, name


# -- improve-jensen's instructions --


def test_instructions_carry_the_key_rules():
    anchors = {
        ".github/improve-jensen-watch.md": (
            "jensen_watch.py note", "jensen_watch.py count", "plan.json", "fixing",
            "core is unreachable", "not_deployable", "ci_unknown", "A behaviour-changing deploy landed",
            '"propose": [{"issue":', "under 100 characters", "Never revert anything yourself"),
        ".github/improve-jensen-review.md": ("ask_jensen.py", "provable", "pr-review-toolkit", "/status"),
        ".github/improve-jensen.md": ("just deploy", "gh pr checks --watch", "12 KB", "150 lines", "Refs #",
                                      "Closes #N", "Proposed in Slack: <change>", "gh issue reopen",
                                      ".github/workflows/", ".github/improve-jensen*.md",
                                      "deploy/improve-jensen-seed.json", "Note from Slack:"),
        ".github/improve-jensen-assess.md": ('{"propose": [{"issue":', "under 100 characters",
                                             "Project decision:"),
        ".github/improve-jensen-project.md": ("approved in Slack", "project-mark --issue <N> --status done"),
        "deploy/improve-jensen.md": ("Never approve a fork run on this repo", "debug logging",
                                     "persist-credentials: false", "claude[bot]", "gh run list",
                                     "IMPROVE_JENSEN_SLACK_CHANNEL", "[people.*]", "`approve 1 3`", "`reject 2`"),
    }
    for path, wanted in anchors.items():
        text = (ROOT / path).read_text()
        for anchor in wanted:
            assert anchor in text, (path, anchor)


def test_charter_drops_the_old_rules():
    assert "Don't merge your own PRs" not in (ROOT / ".github" / "improve-jensen.md").read_text()


# -- assess / approvals / fix / project jobs --


_DISPATCH_TYPES = {"watch": "improve-jensen-watch", "assess": "improve-jensen-assess",
                   "approvals": "improve-jensen-approvals"}


def test_each_dispatch_type_runs_only_its_job(improve_jensen):
    """Three jobs each wake on their own repository_dispatch type. A job's `if` must name its own type and never
    another's, or the wrong job would run on the wrong tick."""
    jobs = improve_jensen["jobs"]
    for name, own_type in _DISPATCH_TYPES.items():
        job_if = str(jobs[name]["if"])
        assert "github.event.schedule" not in job_if, name
        assert f"'{own_type}'" in job_if, (name, job_if)
        for other_name, other_type in _DISPATCH_TYPES.items():
            if other_name != name:
                assert f"'{other_type}'" not in job_if, (name, other_name, job_if)


def test_dispatched_jobs_wait_for_the_engine_switch(improve_jensen):
    """`watch`, `assess` and `approvals` may only fire on the listener's dispatch once a person sets the repo
    variable `IMPROVE_JENSEN_ENGINE` to `on` (deploy/improve-jensen.md). A manual `workflow_dispatch` is a person's
    explicit choice and keeps working regardless."""
    jobs = improve_jensen["jobs"]
    for name, event_type in _DISPATCH_TYPES.items():
        job_if = " ".join(str(jobs[name]["if"]).split())
        assert (f"(github.event_name == 'repository_dispatch' && github.event.action == '{event_type}' && "
                f"vars.IMPROVE_JENSEN_ENGINE == 'on')") in job_if, name
        assert f"(github.event_name == 'workflow_dispatch' && inputs.mode == '{name}')" in job_if, name


def test_listener_unit():
    unit = (ROOT / "deploy" / "improve-jensen-listen.service").read_text()
    service = unit[unit.index("[Service]"):]
    for line in ("User=pragnition", "WorkingDirectory=/home/pragnition/autonomous-agent",
                 "ExecStart=/usr/bin/python3 scripts/jensen_watch.py listen", "Restart=always", "RestartSec=5",
                 "Nice=10", "CPUWeight=20", "IOWeight=20"):
        assert line in service.splitlines(), line
    assert "improve-jensen-listen.service" in (ROOT / "deploy" / "improve-jensen.md").read_text()


def test_new_jobs_runners_timeouts_and_files(improve_jensen):
    jobs = improve_jensen["jobs"]
    assert _runs_on_labels(jobs["assess"]) == {"self-hosted", "improve-jensen", "watch"}
    assert jobs["assess"]["timeout-minutes"] == 90
    assert _runs_on_labels(jobs["project"]) == {"self-hosted", "improve-jensen", "work"}
    assert jobs["project"]["timeout-minutes"] == 360
    assert _runs_on_labels(jobs["approvals"]) == {"self-hosted", "improve-jensen", "work"}
    assert jobs["approvals"]["timeout-minutes"] == 15
    assert (ROOT / ".github" / "improve-jensen-assess.md").is_file()
    assert (ROOT / ".github" / "improve-jensen-project.md").is_file()


def _step_running(job, command):
    [step] = [s for s in job["steps"] if command in str(s.get("run", ""))]
    return step


def test_the_slack_channel_is_a_repo_variable_with_a_default(improve_jensen):
    assert improve_jensen["env"]["IMPROVE_JENSEN_SLACK_CHANNEL"] == (
        "${{ vars.IMPROVE_JENSEN_SLACK_CHANNEL || 'autonomous-agent' }}")


def test_watch_proposes_after_its_plan_and_plans_only_reviews(improve_jensen):
    """The watch's plan names reviews only; the issues it wants changed go to Slack through `propose`, never
    straight to a fix job. A Slack error doesn't fail the job or cost the hour's commit."""
    watch = improve_jensen["jobs"]["watch"]
    assert set(watch["outputs"]) == {"review_indices", "has_reviews"}
    steps = watch["steps"]
    plan = _step_running(watch, "jensen_watch.py plan")
    propose = _step_running(watch, "jensen_watch.py propose")
    commit = _step_running(watch, "jensen_watch.py commit")
    assert propose["run"] == "python scripts/jensen_watch.py propose --from watch"
    assert propose["continue-on-error"] is True
    assert propose["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert propose["if"] == plan["if"]
    assert "steps.claude.outcome == 'success'" in propose["if"]
    assert steps.index(plan) < steps.index(propose) < steps.index(commit)


def test_assess_proposes_before_it_commits(improve_jensen):
    assess = improve_jensen["jobs"]["assess"]
    assert "outputs" not in assess
    steps = assess["steps"]
    propose = _step_running(assess, "jensen_watch.py propose")
    commit = _step_running(assess, "jensen_watch.py assess-commit")
    assert propose["run"] == "python scripts/jensen_watch.py propose --from assess"
    assert propose["continue-on-error"] is True
    assert propose["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert propose["if"] == "steps.claude.outcome == 'success'"
    assert steps.index(propose) < steps.index(commit)
    assert not any("assess-plan" in str(s.get("run", "")) for s in steps)


def test_approvals_job_reads_slack_without_claude(improve_jensen):
    approvals = improve_jensen["jobs"]["approvals"]
    assert approvals["concurrency"] == {"group": "improve-jensen-approvals", "cancel-in-progress": False}
    assert "needs" not in approvals
    assert not any(str(s.get("uses", "")).startswith("anthropics/claude-code-action@") for s in approvals["steps"])
    step = _step_running(approvals, "jensen_watch.py approvals")
    assert step["run"] == 'python scripts/jensen_watch.py approvals --github-output "$GITHUB_OUTPUT"'
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert approvals["outputs"] == {
        key: f"${{{{ steps.{step['id']}.outputs.{key} }}}}" for key in ("fix", "has_fix", "project", "has_project")}


def test_fix_and_project_fire_only_from_approvals_or_a_manual_dispatch(improve_jensen):
    """Nothing but a person's approval (through `approvals`) or a person's own dispatch starts a fix or a project."""
    for name in ("fix", "project"):
        job = improve_jensen["jobs"][name]
        assert job["needs"] == "approvals", name
        job_if = " ".join(str(job["if"]).split())
        assert job_if.startswith("!cancelled() &&"), name
        assert f"needs.approvals.outputs.has_{name} == 'true'" in job_if, name
        assert f"inputs.mode == '{name}' && inputs.target != ''" in job_if, name
        assert job["env"]["TARGET"] == (
            f"${{{{ inputs.mode == '{name}' && inputs.target || needs.approvals.outputs.{name} }}}}"), name
        assert "needs.watch" not in str(job) and "needs.assess" not in str(job), name


# A fix or project run goes ahead when its `ready` check says so, or always on a person's own dispatch.
_GO = "steps.ready.outputs.go == 'true' || env.MANUAL == 'true'"


def test_fix_and_project_check_the_issue_is_still_approved_first(improve_jensen):
    """The approvals run that named the issue can be minutes old: the first step after the install checks that the
    issue is still open and approved (`ready`), and nothing else in the run happens without its `go`. A manual
    dispatch with a target skips the check and always goes."""
    for name in ("fix", "project"):
        job = improve_jensen["jobs"][name]
        steps = job["steps"]
        assert job["env"]["MANUAL"] == f"${{{{ inputs.mode == '{name}' && inputs.target != '' }}}}", name
        install = next(s for s in steps if "pip install" in str(s.get("run", "")))
        ready = steps[steps.index(install) + 1]
        assert ready["id"] == "ready", name
        assert ready["if"] == "env.MANUAL != 'true'", name
        assert ready["run"] == (f'python scripts/jensen_watch.py ready --issue "$TARGET" --kind {name} '
                                f'--github-output "$GITHUB_OUTPUT"'), name
        assert ready["env"] == {"GH_TOKEN": "${{ github.token }}"}, name
        claude = next(s for s in steps if str(s.get("uses", "")).startswith("anthropics/claude-code-action@"))
        assert claude["id"] == "claude", name
        assert claude["if"] == _GO, name
    fix = improve_jensen["jobs"]["fix"]
    assert _step_running(fix, "--add-label fixing")["if"] == _GO
    assert _step_running(fix, "--remove-label fixing")["if"] == f"always() && ({_GO})"


def test_fix_and_project_tell_the_slack_thread_as_they_end(improve_jensen):
    """The last step of a run that went ahead always replies in its proposal's thread (`fired`), with how the
    Claude step ended, after the Claude step and, for a fix, after the `fixing` label is gone."""
    for name in ("fix", "project"):
        job = improve_jensen["jobs"][name]
        steps = job["steps"]
        last = steps[-1]
        assert last["if"] == f"always() && ({_GO})", name
        assert last["run"] == (f'python scripts/jensen_watch.py fired --issue "$TARGET" --kind {name} '
                               '--outcome "${{ steps.claude.outcome }}"'), name
        assert last["env"] == {"GH_TOKEN": "${{ github.token }}"}, name
        claude = next(s for s in steps if str(s.get("uses", "")).startswith("anthropics/claude-code-action@"))
        assert steps.index(claude) < steps.index(last), name
    fix_steps = improve_jensen["jobs"]["fix"]["steps"]
    remove_label = _step_running(improve_jensen["jobs"]["fix"], "--remove-label fixing")
    assert fix_steps.index(remove_label) == len(fix_steps) - 2


def test_project_concurrency(improve_jensen):
    project = improve_jensen["jobs"]["project"]
    concurrency = project["concurrency"]
    assert concurrency["group"] == "improve-jensen-project"
    assert concurrency["cancel-in-progress"] is False


def test_project_job_sets_no_labels_of_its_own(improve_jensen):
    # `project-active.json` is the record of the active project; the job adds and removes no label.
    for step in improve_jensen["jobs"]["project"]["steps"]:
        assert "--add-label" not in str(step.get("run", "")) and "--remove-label" not in str(step.get("run", ""))
    assert "project-active" not in (ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text()


def test_assess_commit_records_when_the_assessment_started(improve_jensen):
    commit = next(s for s in improve_jensen["jobs"]["assess"]["steps"]
                  if "jensen_watch.py assess-commit" in str(s.get("run", "")))
    assert commit["run"] == 'python scripts/jensen_watch.py assess-commit --at "$STARTED"'
    assert commit["env"]["STARTED"] == "${{ steps.assess-check.outputs.started }}"


def test_no_job_dispatches_a_run_or_measures_after_a_deploy():
    """No job dispatches another run (the job token can't), and nothing measures a change once it has shipped: the
    deploy's own check is the last one."""
    text = (ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text()
    assert "gh workflow run" not in text
    lowered = text.lower()
    for gone in ("gate", "yardstick", "7 19 * * *", "fix-deployed", "priority"):
        assert gone not in lowered, gone


def test_respond_ignores_labels_other_than_its_own(improve_jensen):
    """Adding `proposed`, `approved`, `stuck` or any other label to an `improve-jensen` issue fires `issues: labeled`
    too; only the `improve-jensen` label itself may start a respond run that way."""
    respond_if = " ".join(str(improve_jensen["jobs"]["respond"]["if"]).split())
    assert ("github.event_name == 'issues' && contains(github.event.issue.labels.*.name, 'improve-jensen') && "
            "(github.event.action != 'labeled' || github.event.label.name == 'improve-jensen')") in respond_if
    assert "labeled" in improve_jensen[True]["issues"]["types"]
    assert "outputs" not in improve_jensen["jobs"]["respond"]
