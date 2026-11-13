# One-Container Agent and KPIs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the general agent in one Docker container with a full development environment (its own gh,
az, sudo, internet), keep `core` as the orchestrator, and measure what it delivers with KPIs.

**Architecture:** A config switch, `[sandbox] isolation = "open"`, is the new default:
- `core` stops registering the guarded production paths and writes one sandbox token per queue to a shared file.
- A new `agent` compose service runs one runner per queue in a single process. It uses the existing pull
  protocol, git endpoint and `open_pr`.
- KPIs are computed from tables `core` already keeps, plus one new `session_started` event.

**Tech Stack:** Python 3.12, aiohttp, SQLite, claude-agent-sdk, Docker Compose, pytest (`just test`).

**Spec:** `docs/superpowers/specs/2026-11-13-open-agent-design.md`. It amends
`docs/superpowers/specs/2026-11-13-general-agent-sandboxes-design.md`, whose isolation design is the next phase.

Builds on branch `phase-1` at `0a7b5d7`. This plan replaces Tasks 1, 18–20, 23 and 24 of
`2026-11-13-phase-2-general-agent-sandboxes.md`.

## Global Constraints

- `[sandbox] isolation` is `"open"` (default) or `"sandboxes"`. `"sandboxes"` must behave exactly as v2 does today.
- v2 code (sandboxd client, `ask_production`, CLI rules, scrubbing, debug profile) stays. Open mode only skips registering or starting it.
- Agent user: `agent`, uid **1001** (`[sandbox] agent_uid`, default 1001). Bash is its login shell and `/bin/sh`, with passwordless sudo.
- Queue names: `code-<project>` for each pack, plus `general`, sorted, with `general` last.
- Tokens file: `core` writes `/data/runner/runner-tokens.json` (host `${AGENT_DATA}/runner/`). The agent reads it at `/run/agent/runner-tokens.json`. It is JSON `{queue: token}`, mode 0600, owned by the agent uid when `core` runs as root.
- Token TTL `SANDBOX_TOKEN_TTL_S` (60 days). A token is renewed when it has under **7 days** left.
- The agent reaches the Claude proxy at `http://172.30.0.1:28091` (relay → host `127.0.0.1:18091`) and `core` at `http://core:8080`.
- KPI window default: 7 days. Digest default `digest_time = "09:00"`, in `config.timezone`.
- Timestamps are `%Y-%m-%dT%H:%M:%SZ` (`db.utcnow()`).
- Never name the system, services or containers after ChilliClaw. It is one project.
- TDD for every code step. `just test` must be green at the end of each task. Commit per task. No pushes.

## Review Focus

1. **Core's DB is reset, or a token is rotated, while the agent runs.** Runners must pick up the new tokens with nobody touching the laptop (Task 2 `test_replaces_revoked_or_expiring`, Task 4 `test_restarts_a_runner_when_its_token_changes`).
2. **The Azure login made from Slack must be usable and refreshable by the agent container.** `core` runs it as uid 1001 with a writable HOME, in a directory the agent owns (Task 3 `test_login_runs_as_agent_uid`, `test_prepare_agent_dirs_hands_them_to_the_agent`).
3. **Switching back to `isolation = "sandboxes"` must restore every v2 tool** (Task 3 `test_sandboxes_mode_keeps_v2_tools`).
4. **KPIs must report "n/a" rather than crash on missing data:** an empty window, no Linear tasks, PRs never merged, runs with no task (Task 6 `test_empty_window_reports_na`, `test_runs_without_a_task_count_only_in_total`).
5. **The digest must post once per local day across restarts.** Singapore 09:00 is 01:00 UTC (Task 7 `test_digest_uses_local_time_and_posts_once`).

## File Structure

| File | Responsibility |
|---|---|
| `src/agent/config.py` (modify) | Settings: `isolation`, `agent_uid`, `[observability]`. |
| `src/agent/core/runner_tokens.py` (create) | Queue names; keeps the runner tokens file in sync. |
| `src/agent/core/app.py` (modify) | Open-mode wiring, `prepare_agent_dirs`, `sync_runners`, digest. |
| `src/agent/core/cli_admin.py` (modify) | `DeviceCodeLogin` can run as another uid. |
| `src/agent/core/payloads.py`, `src/agent/defaults/system-open.md` (create) | Open-mode system prompt. |
| `src/agent/plugins/core/skills/fix-bug/SKILL.md`, `examples/pack-templates/chilliclaw/plugin/skills/chilliclaw-debugging/SKILL.md` | Mode-neutral Azure wording. |
| `src/agent/sandbox/runners.py` (create), `src/agent/sandbox/runner.py` (modify) | One process, many runners. Non-root sessions run as the process user. |
| `Dockerfile`, `compose.yaml`, `justfile`, `src/agent/supervisor.py`, `src/agent/doctor.py` | The `agent` image and service, relay port, health checks. |
| `src/agent/core/sessions.py`, `src/agent/core/agent_api.py` | `WorkItem.created_at`; the `session_started` event. |
| `src/agent/core/kpis.py` (create), `src/agent/__main__.py` | Compute, format, CLI, Slack tool. |

---

### Task 1: Config for open mode

**Files:**
- Modify: `src/agent/config.py`, `examples/config/agent.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `SandboxSettings.isolation: Literal["open", "sandboxes"] = "open"`
  - `SandboxSettings.agent_uid: int = 1001`
  - `@dataclass ObservabilitySettings(digest_channel: str = "", digest_time: str = "09:00")`
  - `AgentConfig.observability: ObservabilitySettings` (a defaulted last field)

- [ ] **Step 1: Write the failing tests**
  - `test_isolation_defaults_to_open`: a minimal agent.toml loads with `cfg.sandbox.isolation == "open"` and `cfg.sandbox.agent_uid == 1001`.
  - `test_isolation_rejects_unknown`: `isolation = "vm"` → a config problem mentioning `open` and `sandboxes`.
  - `test_agent_uid_must_be_positive_int`: `agent_uid = 0` and `agent_uid = "x"` are each a problem.
  - `test_observability_settings`: `[observability] digest_channel = "#agent-ops"`, `digest_time = "08:30"` load. `digest_time = "8am"` is a problem naming `HH:MM`.
  - `test_example_config_is_open`: `examples/config/agent.toml` loads with isolation `"open"` and a `[observability]` table.
- [ ] **Step 2: Run** `just test` → the new tests FAIL (unknown keys or attributes).
- [ ] **Step 3: Implement** parsing in `config.py` next to the existing `[sandbox]` parsing. In `examples/config/agent.toml`:
  - Add `isolation = "open"` and `agent_uid = 1001` under `[sandbox]`, with a one-line comment that `"sandboxes"` is the v2 design.
  - Add `[observability]` with `digest_channel = ""` and `digest_time = "09:00"`.
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: isolation switch and observability settings`.

### Task 2: Runner tokens file

**Files:**
- Create: `src/agent/core/runner_tokens.py`
- Test: `tests/test_runner_tokens.py`

**Interfaces:**
- Consumes: `mint_token`, `resolve_token(conn, token, now=None)` (sessions.py), `SANDBOX_TOKEN_TTL_S` (move it from app.py to runner_tokens.py; app imports it).
- Produces:
  - `queue_names(packs: dict) -> list[str]`, e.g. `["code-alpha", "code-sample", "general"]`.
  - `sync_runner_tokens(conn, path: Path, names: list[str], *, owner_uid: int | None = None, ttl_s: int = SANDBOX_TOKEN_TTL_S, renew_before_s: int = 7 * 86400, now: datetime | None = None) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**
  - `test_queue_names`: packs `sample` and `alpha` → `["code-alpha", "code-sample", "general"]`.
  - `test_writes_a_token_per_queue`: after sync, the file's JSON keys equal `names`, and for each name `resolve_token(conn, tok).sandbox == name` and `.kind == "sandbox"`.
  - `test_keeps_valid_tokens`: a second sync returns the same tokens.
  - `test_replaces_revoked_or_expiring`:
    - `UPDATE tokens SET revoked = 1` for one token → the next sync gives that name a new valid token, and the other name keeps its token.
    - A sync with `now = utcnow + 55 days` replaces every token (under 7 days left).
  - `test_drops_unknown_names`: the file holds `{"code-gone": "x"}` → after sync with `["general"]` the file has only `general`.
  - `test_file_is_private`: `stat(path).st_mode & 0o777 == 0o600`.
  - `test_unreadable_file_is_rebuilt`: the file holds `not json` → sync succeeds.
- [ ] **Step 2: Run** `just test tests/test_runner_tokens.py` → FAIL (module missing).
- [ ] **Step 3: Implement.** A token is kept when `resolve_token(conn, tok, now=now + renew_before_s)` returns kind `sandbox` for that name. Write to a temp file in the same directory, `chmod 0600`, `os.chown(tmp, owner_uid, -1)` when `owner_uid` is set, then `os.replace`.
- [ ] **Step 4: Run** → PASS. Then `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: core keeps a token file for the agent's runners`.

### Task 3: Core in open mode

**Files:**
- Modify: `src/agent/core/app.py`, `src/agent/core/cli_admin.py`, `src/agent/core/payloads.py`, `src/agent/plugins/core/skills/fix-bug/SKILL.md`, `examples/pack-templates/chilliclaw/plugin/skills/chilliclaw-debugging/SKILL.md`
- Create: `src/agent/defaults/system-open.md`
- Test: `tests/test_wiring.py`, `tests/test_cli_admin.py`, `tests/test_payloads.py`

**Interfaces:**
- Consumes: Task 1 settings, Task 2 `queue_names` and `sync_runner_tokens`.
- Produces:
  - `DeviceCodeLogin(slack, *, config, rules, exec_fn, run_as: int | None = None)`
  - `CoreApp.prepare_agent_dirs() -> None`
  - `CoreApp.sync_runners(now: datetime | None = None) -> dict[str, str]`
  - `CoreApp.runner_tokens_path = data_dir / "runner" / "runner-tokens.json"`

- [ ] **Step 1: Write the failing tests**
  - **`test_open_mode_tools`** (wiring, example config, isolation open):
    - A code-profile member context's manifest has `open_pr`, `post_update` and `track_task`, and has neither `ask_production` nor `az`.
    - An admin context's manifest has `az_login`.
  - **`test_sandboxes_mode_keeps_v2_tools`**: convert the existing manifest test (`tests/test_wiring.py:85-90`) to write `isolation = "sandboxes"` into its config copy. Its assertions are unchanged.
  - **`test_open_mode_writes_runner_tokens`**:
    - `app.sync_runners()` → the file at `app.runner_tokens_path` has `code-sample` and `general`, and each resolves.
    - `main()` builds no `SandboxdClient` in open mode. Test the helper `_sandboxd_for(config)`, which returns `None` for open.
  - **`test_prepare_agent_dirs_hands_them_to_the_agent`**: with `agent_uid = os.getuid()` and `core_is_root` patched to True, `data/cli` and `data/runner` exist and are owned by that uid. With `core_is_root` False, nothing is chowned.
  - **`test_login_runs_as_agent_uid`** (cli_admin): `DeviceCodeLogin(..., run_as=1001)`:
    - The fake `exec_fn` receives `user=1001` and `env["HOME"] == "/tmp"` for both the login and the login check.
    - Without `run_as`, there is no `user` kwarg.
  - **`test_open_mode_system_prompt`** (payloads): with isolation open, `system_prompt()` contains `gh`, `az`, `open_pr`, `fork` and `free port`, and not `never hold credentials`. With isolation sandboxes, it is the v2 text. A `config/agent/system.md` override wins in both modes.
- [ ] **Step 2: Run** `just test` → the new tests FAIL.
- [ ] **Step 3: Implement**
  - **app.py**
    - When `config.sandbox.isolation == "open"`, skip `register_cli_tools` and `register_production_tools`. Keep `Proposals` constructed, since Slack buttons still route to it, and keep the login tools.
    - Pass `run_as=config.sandbox.agent_uid` to `DeviceCodeLogin` in open mode.
    - Add `core_is_root()` (returns `os.geteuid() == 0`). It decides `owner_uid` for `sync_runner_tokens` and whether `prepare_agent_dirs` chowns (recursively for `data/cli`).
    - `serve()` calls `prepare_agent_dirs()` and `sync_runners()` before the loops in open mode. `maintenance()` calls `sync_runners()` in open mode.
    - In `main()`, `_sandboxd_for(config)` returns a `SandboxdClient` only for `"sandboxes"`.
  - **payloads.py:** the default prompt file depends on the mode (`DEFAULT_SYSTEM_OPEN` = `defaults/system-open.md`).
  - **system-open.md** covers spec §5's points in the same voice as `system.md`:
    - full environment with its own `gh`, `az` and sudo
    - PRs through `open_pr` so they're tracked
    - work only on the fork and staging, never production
    - shared container, so pick free ports and don't kill processes you didn't start
    - verify before saying done
  - **Skills:** replace the `ask_production`-only lines with "get Azure signals with `az` directly; if you don't have `az`, ask with `ask_production`".
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: core runs in open mode: no guarded production paths, runner tokens, agent-owned dirs`.

### Task 4: Runners in the agent container

**Files:**
- Create: `src/agent/sandbox/runners.py`
- Modify: `src/agent/sandbox/runner.py`, `src/agent/__main__.py`
- Test: `tests/test_runners.py`, `tests/test_sandbox_runner.py`

**Interfaces:**
- Consumes: `SandboxRunner` (unchanged constructor), `CoreLink`, `Worktrees`, `SessionUsers`, `ClaudeRunner`.
- Produces:
  - `profile_for(queue: str) -> str`: `"general"` → `general`, `code-x` → `code`, `debug-x` → `debug`.
  - `read_tokens(path: Path) -> dict[str, str]`: `{}` when the file is missing or not a JSON object.
  - `class RunnerSet(make_runner: Callable[[str, str], Any])` with `async reconcile(tokens: dict[str, str]) -> None` and `async stop() -> None`.
  - `main(argv)` for `python -m agent runners --tokens PATH --core URL --proxy URL --root DIR --agent-name NAME [--check-s 15]`.

- [ ] **Step 1: Write the failing tests**
  - `test_profile_for`: the three cases above.
  - `test_starts_one_runner_per_queue`: `reconcile({"code-sample": "a", "general": "b"})` → `make_runner` was called with exactly those pairs, and both `run_forever` coroutines are running.
  - `test_restarts_a_runner_when_its_token_changes`: reconcile with `{"general": "b"}`, then `{"general": "c"}` → the old runner task is cancelled and `make_runner("general", "c")` is called.
  - `test_finished_runner_restarts_only_with_a_new_token`: a fake `run_forever` returns immediately (revoked). Reconcile with the same token → not restarted. Reconcile with a new token → restarted.
  - `test_stops_runners_for_removed_queues`: `{"code-sample": "a"}` then `{}` → cancelled.
  - `test_read_tokens_missing_or_bad_file`: a missing file, `"[]"` and `"x"` each give `{}`.
  - `test_open_runner_runs_sessions_as_itself` (test_sandbox_runner): with `run_as_root=False`, the captured `RunSpec.os_user is None`.
- [ ] **Step 2: Run** `just test` → FAIL.
- [ ] **Step 3: Implement**
  - **runner.py:** `os_user=user if self.run_as_root else None`.
  - **runners.py:**
    - `main` builds one shared `ClaudeRunner(ProxyClient(proxy), proxy, root / "transcripts", submit_tool_factory=None)` and one shared `SessionUsers()`.
    - Per queue it builds `CoreLink(core, token)`, `Worktrees(root, core, lambda: token)` and `SandboxRunner(..., sandbox=queue, profile=profile_for(queue), run_as_root=False, ...)`, using the same `root` subdirectories as `runner.main`.
    - It loops `reconcile(read_tokens(path))`, then sleeps `check_s`.
  - **`__main__.py`:** passes `runners` through like `runner`.
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: one process runs a runner per queue for the open agent container`.

### Task 5: Agent image, compose, supervisor and doctor

**Files:**
- Modify: `Dockerfile`, `compose.yaml`, `justfile`, `src/agent/supervisor.py`, `src/agent/doctor.py`
- Test: `tests/test_deploy_files.py`, `tests/test_doctor.py`

**Interfaces:**
- Consumes: Task 2 `queue_names`, Task 4 `python -m agent runners`.
- Produces:
  - `doctor.required_core(config) -> tuple[str, ...]`
  - `doctor.check_runner_tokens(conn, path, names) -> Check`

- [ ] **Step 1: Write the failing tests**
  - **`test_agent_image`** (deploy files). The Dockerfile has `FROM app AS agent` with:
    - `useradd` with uid 1001 and `-s /bin/bash`
    - a sudoers `NOPASSWD` line
    - `ln -sf /bin/bash /bin/sh`
    - `USER agent`
    - `AZURE_CONFIG_DIR=/data/cli/azure`
    - `gh` and `azure-cli` installs
    - `/var/agent` owned by agent
    - a `CMD` running `agent runners`
  - **`test_agent_service`.** compose has an `agent` service with:
    - `target: agent`, `restart: unless-stopped`, `networks: [agentnet]`
    - an `env_file` ending in `secrets/agent.env`
    - volumes `${AGENT_DATA:-./data}/runner:/run/agent:ro`, `${AGENT_DATA:-./data}/cli/azure:/data/cli/azure` and the named volume `agentwork:/var/agent`
    - a command containing `--core http://core:8080` and `--proxy http://172.30.0.1:28091`
    - no `privileged` and no docker socket

    `core` mounts `${AGENT_DATA:-./data}/runner:/data/runner`. The relay command forwards `28091` to `127.0.0.1:18091`. The `agentwork` volume is declared.
  - **`test_supervisor_skips_unset_sandboxd`**: with `SANDBOXD_HEALTH` unset or empty, the watched set has only core.
  - **`test_open_mode_requirements`** (doctor):
    - `required_core(open config)` lacks `SANDBOXD_TOKEN`; `required_core(sandboxes config)` includes it.
    - `check_runner_tokens` fails when the file is missing a queue or holds a revoked token, and passes after `sync_runner_tokens`.
- [ ] **Step 2: Run** `just test` → FAIL.
- [ ] **Step 3: Implement**
  - **Dockerfile `agent` target:**
    - Start `FROM app`. Copy Node 22 the way the old `worker` target did (see `git show b18739b:Dockerfile`).
    - apt: `git sudo curl jq unzip ripgrep build-essential ca-certificates`.
    - Install gh from GitHub's apt repository, and azure-cli into `/opt/az` as the core target does, linked to `/usr/local/bin/az`.
    - Set `ENV SHELL=/bin/bash`.
  - **compose and justfile:**
    - Add the service and volume.
    - Drop `SANDBOXD_HEALTH` from the supervisor env, with a comment that sandboxes mode sets it.
    - Add the `agent` service to `just up`.
    - `just doctor` also runs `docker compose exec agent bash -lc 'gh auth status && az account show -o none'`.
  - **supervisor:** the sandboxd default becomes `""`, and empty means not watched.
  - **doctor:** use `required_core(config)` and add the runner-tokens check.
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: the open agent container in the compose stack`.

### Task 6: KPIs: events, numbers, CLI

**Files:**
- Create: `src/agent/core/kpis.py`
- Modify: `src/agent/core/sessions.py`, `src/agent/core/agent_api.py`, `src/agent/__main__.py`, `justfile`
- Test: `tests/test_kpis.py`, `tests/test_agent_api.py`

**Interfaces:**
- Produces:
  - `WorkItem.created_at: str | None = None`
  - event `session_started` with `data = {"work_item", "sandbox", "queued_at"}`
  - `@dataclass Stat(n: int, median_s: float | None, p90_s: float | None)`
  - `@dataclass Kpis(since, until, project, pickup: Stat, to_pr: Stat, to_merge: Stat, queue_wait: Stat, tasks: int, prs: int, merged: int, closed: int, reverted: int, failed: int, cancelled: int, cost_usd: float, cost_per_task: float | None, cost_per_merged: float | None)` with property `merge_rate -> float | None`
  - `compute_kpis(conn, *, since: datetime, until: datetime, project: str | None = None) -> Kpis`
  - `format_duration(seconds: float) -> str`
  - `format_kpis(k: Kpis) -> str`
  - CLI `python -m agent kpis [--days 7] [--project P] [--db PATH]`

**Definitions:**
- **pickup:** Linear tasks (`source = 'linear'`) created in the window → their first `session_started`.
- **to_pr:** PRs with `opened_at` in the window. Measured from the task's first `session_started`, or the task's `created_at` when there is none.
- **to_merge:** `merged_at` in the window, minus `opened_at`.
- **queue_wait:** `session_started` events in the window: `ts − queued_at`.
- **Counts:**
  - `tasks`: tasks created in the window.
  - `prs`: `opened_at` in the window.
  - `merged`: `merged_at` in the window.
  - `closed`: `closed_at` in the window with no `merged_at`.
  - `reverted`: `reverted_at` in the window.
  - `failed` and `cancelled`: tasks created in the window with that status.
- **Cost:**
  - `cost_usd`: the sum of `runs.cost_usd_est` with `ended_at` in the window.
  - `cost_per_task`: `cost_usd / tasks`.
  - `cost_per_merged`: the cost of the merged tasks' runs `/ merged`.
- **Project filter:** joins through `tasks.project`. Runs with no task count only when `project is None`.
- **p90:** nearest rank.

- [ ] **Step 1: Write the failing tests** (seed rows directly; window 2026-11-13T00:00Z to 2026-11-14T00:00Z)
  - **`test_kpis_from_seeded_history`.** Seed:
    - Linear T1: created 10:00, `session_started` 10:05, PR opened 11:05, merged 13:05, run cost 1.5.
    - Linear T2: created 10:00, started 10:15, PR opened 12:15, not merged, run cost 0.5.
    - Slack T3: created 10:00, started 10:01, no PR, status failed.

    Expect:
    - `pickup == Stat(2, 600, 900)`
    - `to_pr == Stat(2, 5400, 7200)`
    - `to_merge == Stat(1, 7200, 7200)`
    - `tasks == 3`, `prs == 2`, `merged == 1`, `failed == 1`, `merge_rate == 0.5`
    - `cost_usd == 2.0`, `cost_per_merged == 1.5`
  - **`test_project_filter`**: T2 in project `other` → with `project="sample"`, `prs == 1`.
  - **`test_empty_window_reports_na`**: an empty DB gives every `Stat(0, None, None)`, and `cost_per_task`, `cost_per_merged` and `merge_rate` are None. `format_kpis` contains `n/a` and raises nothing.
  - **`test_runs_without_a_task_count_only_in_total`**: a run with `task_id` NULL and cost 1.0 is in `cost_usd` for all projects, not with `project="sample"`.
  - **`test_format_duration`**: `600` → `"10m"`, `5400` → `"1h 30m"`, `90000` → `"1d 1h"`, `20` → `"0m"`.
  - **`test_format_kpis`**: the seeded text contains `Ticket → pickup: median 10m, p90 15m (n=2)` and `Merge rate: 50%`.
  - **`test_pull_records_session_started`** (agent_api): pulling a turn item appends one `session_started` event with the item id, sandbox and `queued_at`. Pulling a `chore` item appends none.
- [ ] **Step 2: Run** `just test` → FAIL.
- [ ] **Step 3: Implement**
  - Add `created_at` to `WorkItem` and `_item`.
  - In `pull`, after `mint_token`, append the event for `turn` items (using `context_for(item.id).task_id`). Reuse that context for the manifest.
  - Write `kpis.py`.
  - The CLI opens the DB like `ops.py` does.
  - Add `just kpis *args` → `docker compose exec core python -m agent kpis {{args}}`.
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: KPIs for pickup, PR, merge queue and cost`.

### Task 7: KPIs in Slack: tool and daily digest

**Files:**
- Modify: `src/agent/core/kpis.py`, `src/agent/core/app.py`
- Test: `tests/test_kpis.py`, `tests/test_wiring.py`

**Interfaces:**
- Consumes: Task 6 `compute_kpis` and `format_kpis`, Task 1 `config.observability`.
- Produces:
  - `register_kpi_tools(router) -> None`: tool `kpis`, tier `read`, every profile, `schema({}, {"days": "integer", "project": "string"})`.
  - `CoreApp.post_digest(now: datetime | None = None) -> bool`, using the setting `kpi_digest_date`.

- [ ] **Step 1: Write the failing tests**
  - `test_kpis_tool`: a member context calls `kpis` with `{"days": 7}` → the reply text contains `Ticket → pickup`. An unknown project → the reply names the known projects.
  - `test_digest_uses_local_time_and_posts_once`:
    - `digest_channel = "#agent-ops"`, timezone `Asia/Singapore`, `digest_time = "09:00"`.
    - `post_digest(2026-11-13T00:30Z)` → False and no post (08:30 local).
    - `post_digest(01:10Z)` → True, with one post to `#agent-ops` containing `last 7 days`.
    - `post_digest(02:00Z)` → False.
    - A new `CoreApp` on the same data dir, then `post_digest(03:00Z)` → False.
    - `post_digest(2026-11-14T01:05Z)` → True.
  - `test_no_channel_no_digest`: an empty `digest_channel` → False and no post.
- [ ] **Step 2: Run** `just test` → FAIL.
- [ ] **Step 3: Implement.**
  - Register the tool in `CoreApp`.
  - `maintenance()` calls `post_digest(now)`.
  - Add a 5-minute loop `self._every(300, self.post_digest)` so the digest lands near `digest_time`.
- [ ] **Step 4: Run** `just test` → all PASS.
- [ ] **Step 5: Commit** `feat: KPIs in Slack and a daily digest`.

### Task 8: Bring-up on the laptop (with the user)

Manual. The user is present. No SSH except for repo-level changes.

- [ ] **Step 1: Prepare the secrets and configuration.**
  - The user fills `${AGENT_DATA}/secrets/core.env` and `agent.env` (`GH_TOKEN` for the agent's GitHub account, with access to the fork, plus the project's dev or test keys).
  - Point the pack's `[project] repo` at the fork, and set `[observability] digest_channel`.
- [ ] **Step 2: Bring the stack up.** Run `just up`. Expected: `core`, `agent`, `supervisor` and `relay` are running, and `data/runner/runner-tokens.json` lists `code-<project>` and `general`.
- [ ] **Step 3: Log in to Azure.** An admin runs `az_login` from Slack and signs in with the DM'd code. Then run `just doctor`. Expected: every check passes, including `gh auth status` and `az account show` in the agent container.
- [ ] **Step 4: Rewrite and run the smoke test.** Rewrite `scripts/smoke.py` for open mode:
  - It enqueues a `general` turn, "reply with the single word pong", through `WorkQueue` in core's DB.
  - It waits up to 5 minutes for the item to finish, and asserts `pong` is in the result text.

  Run it with `docker compose exec core python scripts/smoke.py`. Expected: `smoke ok`.
- [ ] **Step 5: Run a real request.** Post a small real request in the project channel. Expected: a draft PR on the fork, assigned to the requester. `just kpis` then shows `n=1` for pickup → PR.
- [ ] **Step 6: Commit** the smoke rewrite: `chore: smoke test for the open agent`.
