# Rust Control Plane, Stage 5: Cutover Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the switch from `agent-core` and `agent-runner` to `agent-control` needs, ready to run with one command, plus the rollback; the switch itself waits for the user's word.

**Architecture:** `agent-control` runs `agentd serve` from the agent image (which gains the built web app). The runner's other jobs (the agent's long-running services, its home folder and browsers on the volume) move to `agent-services`, a container from the same image running today's Python code. The supervisors watch the control plane alone. The Python services stay in compose under a `python` profile for the rollback week. Deploy and improve-jensen's helpers talk to `agent-control`.

**Tech Stack:** Docker Compose, the existing Python code and tests (`just test`), `agentd`.

**Spec:** `docs/superpowers/specs/2026-11-22-rust-control-plane-design.md` (Cutover and rollback)

## Global Constraints

- The live stack is not switched in this stage; nothing here stops `agent-core` or `agent-runner`.
- Rollback for one week: `agent-control` stopped, the Python services started again on the same database and data.
- agentd runs as the agent user (uid 1001) with the Docker socket's group; the control plane's secrets (core.env) stay out of every conversation's container.
- The public repo rule: no keys, host names or people in code, docs, tests or commits.

## Review Focus

- The switch on a host where the data folders are root's (core ran as root): agentd can still write its database, runs and task files — `cutover` sets ownership first.
- A deploy after the switch never pauses Jensen or waits for an idle runner: turns live in their own containers.
- Rollback after a week of agentd writing: the Python services read everything agentd wrote (same tables, same formats).

---

### Task 1: The agent image and the services container's command

**Files:** Modify `Dockerfile` (agent stage gets the web app at `/app/web`), `src/agent/__main__.py`, `src/agent/runner/main.py` (`services` command: home folder and browsers, then the services supervisor until SIGTERM); tests in `tests/test_runner_main.py`, `tests/test_deploy_files.py`.

- [ ] Failing tests: `test_services_command_prepares_home_then_supervises`, `test_agent_image_serves_the_web_app`.
- [ ] Implement; `just test` green; commit `agent image: the web app and the services command`.

### Task 2: Supervisors watch the control plane

**Files:** Modify `src/agent/supervisor.py` (`CONTROL_HEALTH`, `CONTROL_CONTAINER`: one target named "control plane", no runner); `tests/test_supervisor.py`.

- [ ] Failing test: `test_control_plane_target_replaces_core_and_runner` (and the old variables still work).
- [ ] Implement; green; commit `supervisors: watch the control plane`.

### Task 3: Compose and just

**Files:** Modify `compose.yaml` (`control`: agent-control at 172.30.0.10, `agentd serve`, uid 1001 plus the socket's group, the Docker socket, the work volume, the data folders, core.env as its secrets, agent.env read-only for the sessions; `services`: agent-services; supervisors on `CONTROL_*`; `core` and `agent` under profile `python`), `justfile` (recipes through agent-control; `DOCKER_GID` and `AGENTD_DATA_HOST` set for compose); `tests/test_deploy_files.py`.

- [ ] Failing tests: `test_control_service`, `test_services_service`, `test_python_services_only_in_the_rollback_profile`, `test_control_gets_only_its_own_secrets`.
- [ ] Implement; green; `docker compose config` clean; commit `compose: agent-control and agent-services`.

### Task 4: Deploy

**Files:** Modify `src/agent/watch/deploy.py` (paths to services: Rust, Python, Dockerfile and the web app rebuild the agent image for control and services; no pause or idle wait; the check is control's `/health` and `agentd smoke`); `tests/test_watch_deploy.py`.

- [ ] Failing tests: `test_services_for_after_the_switch`, `test_deploy_never_pauses`, `test_check_reads_control_health_and_smoke`.
- [ ] Implement; green; commit `deploy: agent-control`.

### Task 5: improve-jensen's helpers and the schedules skill

**Files:** Modify `src/agent/watch/host.py` (both containers are agent-control; no runner token), `scripts/ask_jensen.py`, `src/agent/plugins/core/skills/schedules/SKILL.md` (`agentd schedules`); tests in `tests/test_watch_host.py`.

- [ ] Failing test: `test_helpers_run_in_agent_control`.
- [ ] Implement; green; commit `improve-jensen helpers: agent-control`.

### Task 6: Cutover and rollback

**Files:** Create `deploy/cutover.sh` (`check`, `switch`, `rollback`); modify `justfile` (`cutover`, `rollback`), `docs/` (the runbook and the rollback drill).

- `check`: images built, `docker compose config` clean, the socket's group, nothing to change yet. `switch`: kill switch on, running turns given 10 minutes then stopped, database backed up, data folders the agent user's, core.env and agent.env readable by it, `agent-core` and `agent-runner` stopped, `control` and `services` started, supervisors recreated, kill switch off, `agentd doctor`, `agentd smoke`. `rollback`: control and services stopped, the previous supervisors and the Python services started again.
- [ ] Failing test: `test_cutover_script_steps` (the script's commands in order, with a stub `docker`).
- [ ] Implement; green; commit `cutover and rollback`.

### Task 7: Docs and leftovers

**Files:** Modify `README.md` and the docs that describe core and the runner; the dashboard's Host page words; delete `spike/rust-control-plane/` and `scripts/smoke.py`.

- [ ] `just test`, `just rust-test`, `just dashboard-test` green; commit `docs: the control plane`.

### Task 8: Ready on the host

- [ ] When the host is quiet: build the images there (`docker compose build control services supervisor supervisor-standby`), `deploy/cutover.sh check` passes. Results in the spike results file; commit.
