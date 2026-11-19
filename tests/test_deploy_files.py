import json
import os
import re
import subprocess
import sys
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
    assert on["schedule"] == [{"cron": "17 * * * *"}, {"cron": "13 */6 * * *"}, {"cron": "7 19 * * *"}]
    dispatch_inputs = on["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["mode"]["default"] == "watch"
    assert set(dispatch_inputs["mode"]["options"]) == {"watch", "review", "fix", "assess", "yardstick", "project"}
    assert dispatch_inputs["target"]["type"] == "string"
    for trigger in ("issues", "issue_comment", "pull_request_review_comment", "pull_request_review"):
        assert trigger in on
    assert set(improve_jensen["jobs"]) == {"watch", "review", "fix", "respond", "assess", "project", "yardstick",
                                           "gate-revert"}


def test_jobs_run_on_the_right_runners(improve_jensen):
    jobs = improve_jensen["jobs"]
    for name, want_label in (("watch", "watch"), ("review", "work"), ("fix", "work"),
                             ("assess", "work"), ("project", "work"), ("yardstick", "work"), ("gate-revert", "work")):
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
    assert "merge, deploy or start a gate" in prompt
    assert "a person merges" in prompt


def test_fix_is_serialised(improve_jensen):
    concurrency = improve_jensen["jobs"]["fix"]["concurrency"]
    assert concurrency["group"] == "improve-jensen-fix"
    assert concurrency["cancel-in-progress"] is False


def test_every_claude_step_uses_opus_and_an_instructions_file_that_exists(improve_jensen):
    claude_steps = _claude_steps(improve_jensen)
    assert len(claude_steps) == 7
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


def test_fix_and_respond_push_with_the_claude_app_not_the_job_token(improve_jensen):
    # A push made with the job's GITHUB_TOKEN never triggers `ci`, so `gh pr checks --watch` would wait forever:
    # the checkout must not leave that token in the git config for the Claude step's pushes to pick up.
    for name in ("fix", "respond"):
        checkout = next(s for s in improve_jensen["jobs"][name]["steps"]
                        if str(s.get("uses", "")).startswith("actions/checkout@"))
        assert checkout["with"]["persist-credentials"] is False, name


# -- improve-jensen's instructions --


def test_instructions_carry_the_key_rules():
    anchors = {
        ".github/improve-jensen-watch.md": (
            "jensen_watch.py note", "jensen_watch.py count", "plan.json", "fixing",
            "core is unreachable", "not_deployable", "two such comments", "ci_unknown",
            "Start the gate first", "A behaviour-changing deploy landed"),
        ".github/improve-jensen-review.md": ("ask_jensen.py", "provable", "pr-review-toolkit", "/status"),
        ".github/improve-jensen.md": ("just deploy", "gh pr checks --watch", "12 KB", "150 lines", "Refs #",
                                      ".github/workflows/", ".github/improve-jensen*.md",
                                      "deploy/improve-jensen-seed.json",
                                      "improve-jensen never merges a change to its own yardstick or gate",
                                      "starts its gate as soon as `just deploy` returns 0",
                                      "Gate waiting: a deploy is pending"),
        "deploy/improve-jensen.md": ("Never approve a fork run on this repo", "debug logging",
                                     "persist-credentials: false", "claude[bot]", "gh run list"),
    }
    for path, wanted in anchors.items():
        text = (ROOT / path).read_text()
        for anchor in wanted:
            assert anchor in text, (path, anchor)


def test_charter_drops_the_old_rules():
    assert "Don't merge your own PRs" not in (ROOT / ".github" / "improve-jensen.md").read_text()


# -- assess / yardstick / project jobs --


_SCHEDULE_CRONS = {"watch": "17 * * * *", "assess": "13 */6 * * *", "yardstick": "7 19 * * *"}


def test_each_schedule_runs_only_its_job(improve_jensen):
    """Three jobs each wake on their own cron. A job's `if` must name its own cron string and never another
    scheduled job's, or the wrong job would run on the wrong hour (Review Focus 4)."""
    jobs = improve_jensen["jobs"]
    for name, own_cron in _SCHEDULE_CRONS.items():
        job_if = str(jobs[name]["if"])
        assert own_cron in job_if, (name, job_if)
        for other_name, other_cron in _SCHEDULE_CRONS.items():
            if other_name != name:
                assert other_cron not in job_if, (name, other_name, job_if)


def test_scheduled_jobs_wait_for_the_engine_switch(improve_jensen):
    """Merging this workflow must not change today's behaviour: `watch`, `assess` and the nightly `yardstick` may
    only fire on their own cron once a person sets the repo variable `IMPROVE_JENSEN_ENGINE` to `on` (deploy/
    improve-jensen.md). A manual `workflow_dispatch` is a person's explicit choice and keeps working regardless."""
    jobs = improve_jensen["jobs"]
    for name, cron in _SCHEDULE_CRONS.items():
        job_if = " ".join(str(jobs[name]["if"]).split())
        assert f"github.event.schedule == '{cron}' && vars.IMPROVE_JENSEN_ENGINE == 'on'" in job_if, name
    for name, mode in (("watch", "watch"), ("assess", "assess"), ("yardstick", "yardstick")):
        job_if = " ".join(str(jobs[name]["if"]).split())
        assert f"(github.event_name == 'workflow_dispatch' && inputs.mode == '{mode}')" in job_if, name


def test_new_jobs_runners_timeouts_and_files(improve_jensen):
    jobs = improve_jensen["jobs"]
    assert _runs_on_labels(jobs["assess"]) == {"self-hosted", "improve-jensen", "work"}
    assert jobs["assess"]["timeout-minutes"] == 90
    assert _runs_on_labels(jobs["project"]) == {"self-hosted", "improve-jensen", "work"}
    assert jobs["project"]["timeout-minutes"] == 360
    assert _runs_on_labels(jobs["yardstick"]) == {"self-hosted", "improve-jensen", "work"}
    assert jobs["yardstick"]["timeout-minutes"] == 240
    assert (ROOT / ".github" / "improve-jensen-assess.md").is_file()
    assert (ROOT / ".github" / "improve-jensen-project.md").is_file()


def test_project_concurrency(improve_jensen):
    project = improve_jensen["jobs"]["project"]
    concurrency = project["concurrency"]
    assert concurrency["group"] == "improve-jensen-project"
    assert concurrency["cancel-in-progress"] is False
    assert project["needs"] == "assess"
    job_if = str(project["if"])
    assert "!cancelled()" in job_if
    assert "needs.assess.outputs.has_project" in job_if
    assert "inputs.mode == 'project'" in job_if and "inputs.target != ''" in job_if
    checkout = next(s for s in project["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


_GATE_REPORTERS = ("watch", "fix", "project", "respond")


def test_jobs_that_can_start_a_gate_report_it_as_they_end(improve_jensen):
    """No `gh workflow run` for a gate (the job token can't dispatch): each job that can run `gate start` ends with an
    always() `gate status` step and exposes its `active` as the job output `gate`, which the yardstick job reads."""
    jobs = improve_jensen["jobs"]
    for name in _GATE_REPORTERS:
        job = jobs[name]
        last = job["steps"][-1]
        assert last["if"] == "always()", name
        assert last["run"] == 'python scripts/jensen_watch.py gate status --github-output "$GITHUB_OUTPUT"', name
        assert job["outputs"]["gate"] == f"${{{{ steps.{last['id']}.outputs.active }}}}", name


def test_yardstick_runs_after_the_jobs_that_can_start_a_gate(improve_jensen):
    yardstick = improve_jensen["jobs"]["yardstick"]
    assert yardstick["needs"] == list(_GATE_REPORTERS)
    assert yardstick["concurrency"] == {"group": "improve-jensen-yardstick", "cancel-in-progress": False}
    job_if = " ".join(str(yardstick["if"]).split())
    # The whole condition sits under `!cancelled()`: without a status function a skipped `needs` job (every one of
    # them on the nightly cron and a manual run) would skip this job too, and a cancelled run must still cancel it.
    assert job_if.startswith("!cancelled() && (") and job_if.endswith(")"), job_if
    assert "github.event.schedule == '7 19 * * *'" in job_if
    assert "inputs.mode == 'yardstick'" in job_if
    for name in _GATE_REPORTERS:
        assert f"needs.{name}.outputs.gate == 'true'" in job_if, name
        assert f"needs.{name}.outputs.gate == 'true'" in yardstick["env"]["GATE"], name
    assert "inputs.target" not in job_if


def _checkout(job):
    return next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))


def test_yardstick_gate_run_steps(improve_jensen):
    yardstick = improve_jensen["jobs"]["yardstick"]
    steps = yardstick["steps"]
    run_step = next(s for s in steps if "jensen_watch.py yardstick run" in str(s.get("run", "")))
    assert "--github-output" in run_step["run"]
    assert "env.GATE == 'true' && '--gate'" in run_step["run"]

    # Only a measured gate ends, and only the gate this run measured (`--started`): retry (couldn't measure it this
    # time) and none leave it active. The counts go on record with it.
    gate_end = next(s for s in steps if "jensen_watch.py gate end" in str(s.get("run", "")))
    assert gate_end["if"] == ("steps.yardstick.outputs.gate_result == 'passed' || "
                              "steps.yardstick.outputs.gate_result == 'failed'")
    assert " ".join(gate_end["run"].split()) == (
        'python scripts/jensen_watch.py gate end --result "$GATE_RESULT" --started "$GATE_STARTED" '
        '--drop "$DROP" --regressions "$REGRESSIONS"')
    assert gate_end["env"] == {"GATE_RESULT": "${{ steps.yardstick.outputs.gate_result }}",
                               "GATE_STARTED": "${{ steps.yardstick.outputs.gate_started }}",
                               "DROP": "${{ steps.yardstick.outputs.drop }}",
                               "REGRESSIONS": "${{ steps.yardstick.outputs.regressions }}"}
    assert "always()" not in gate_end["if"]

    # A failed gate leaves its record on its issue before anything else can fail.
    comment = next(s for s in steps if "gh issue comment" in str(s.get("run", "")))
    assert steps.index(gate_end) < steps.index(comment)
    assert str(comment["if"]).startswith("!cancelled()")
    assert f"steps.{gate_end['id']}.outcome == 'success'" in comment["if"]
    assert "steps.yardstick.outputs.gate_result == 'failed'" in comment["if"]
    assert "jensen_watch.py gate status --last" in comment["run"]

    label = next(s for s in steps if "--remove-label gating" in str(s.get("run", "")))
    assert label["if"] == f"!cancelled() && steps.{gate_end['id']}.outcome == 'success'"
    assert "jensen_watch.py gate status --last" in label["run"]
    assert steps.index(gate_end) < steps.index(label)

    # The revert is no longer a step here: it runs in `gate-revert`, off the job's `gate_result` output, which says
    # `failed` only once `gate end` has really ended the failed gate.
    assert not any(str(s.get("uses", "")).startswith("anthropics/claude-code-action@") for s in steps)
    assert yardstick["outputs"]["gate_result"] == (
        f"${{{{ steps.{gate_end['id']}.outcome == 'success' && steps.yardstick.outputs.gate_result || 'none' }}}}")


def _failed_gate_comment(gate_last: dict, tmp_path) -> str:
    """Runs the comment step's script with `gh` and the gate CLI stubbed, and returns what it would post."""
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text())["jobs"][
        "yardstick"]["steps"]
    script = next(s for s in steps if "gh issue comment" in str(s.get("run", "")))["run"]
    script = script.replace("python scripts/jensen_watch.py gate status --last", f"echo '{json.dumps(gate_last)}'")
    script = script.replace("${{ github.repository }}", "owner/repo")
    bin_dir, out = tmp_path / "bin", tmp_path / "gh.out"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gh").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {out}\n')
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["bash", "-e", "-c", script], env=env, check=True, capture_output=True)
    return out.read_text().splitlines()


def test_a_failed_gate_comments_its_merge_or_says_host_change(tmp_path):
    repo = {"issue": 12, "kind": "repo", "handle": "abc1234", "started": "2026-11-19T01:00:00Z", "result": "failed",
            "drop": 2, "regressions": 1}
    assert _failed_gate_comment(repo, tmp_path / "repo") == [
        "issue", "comment", "12", "--body", "Yardstick gate failed at abc1234: picked to revert.", "-R", "owner/repo"]
    # A host gate's handle is backup paths on the host: never in the public comment.
    host = {**repo, "issue": 7, "kind": "host", "handle": "/home/someone/data/agent.toml.bak-20261119-0100"}
    posted = _failed_gate_comment(host, tmp_path / "host")
    assert posted[2] == "7" and posted[4] == "Yardstick gate failed (host change): picked to revert."
    # The watch and the charter treat that comment like `Deploy check failed ... picked to revert`.
    for path in (".github/improve-jensen.md", ".github/improve-jensen-watch.md"):
        text = " ".join((ROOT / path).read_text().split())
        assert "Yardstick gate failed at <merge sha>: picked to revert." in text, path
        assert "Yardstick gate failed (host change): picked to revert." in text, path


def test_gate_revert_runs_the_charter_after_a_failed_gate(improve_jensen):
    job = improve_jensen["jobs"]["gate-revert"]
    assert job["needs"] == "yardstick"
    assert job["if"] == "!cancelled() && needs.yardstick.outputs.gate_result == 'failed'"
    assert job["timeout-minutes"] == 120
    [claude] = [s for s in job["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action@")]
    assert "--append-system-prompt-file .github/improve-jensen.md" in claude["with"]["claude_args"]
    assert "gate status --last" in claude["with"]["prompt"] and '"The failed gate"' in claude["with"]["prompt"]


def test_yardstick_and_gate_revert_check_out_main_without_the_job_token(improve_jensen):
    # A run started by a PR review would otherwise check out the PR's merge ref; and no job token in the git config.
    for name in ("yardstick", "gate-revert"):
        checkout = _checkout(improve_jensen["jobs"][name])
        assert checkout["with"]["ref"] == "main", name
        assert checkout["with"]["persist-credentials"] is False, name


def test_workflow_reads_gate_result_not_gate_failed():
    assert "gate_failed" not in (ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text()


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


def test_no_job_dispatches_a_gate(improve_jensen):
    text = (ROOT / ".github" / "workflows" / "improve-jensen.yml").read_text()
    assert "gh workflow run" not in text
    assert "target == 'gate'" not in text


def test_respond_ignores_labels_other_than_its_own(improve_jensen):
    """Adding `gating`, `stuck`, `priority` or any other label to an `improve-jensen` issue fires `issues: labeled`
    too; only the `improve-jensen` label itself may start a respond run that way."""
    respond_if = " ".join(str(improve_jensen["jobs"]["respond"]["if"]).split())
    assert ("github.event_name == 'issues' && contains(github.event.issue.labels.*.name, 'improve-jensen') && "
            "(github.event.action != 'labeled' || github.event.label.name == 'improve-jensen')") in respond_if
    assert "labeled" in improve_jensen[True]["issues"]["types"]


def test_dispatch_modes_include_new_modes(improve_jensen):
    dispatch_inputs = improve_jensen[True]["workflow_dispatch"]["inputs"]
    assert {"assess", "yardstick", "project"} <= set(dispatch_inputs["mode"]["options"])
