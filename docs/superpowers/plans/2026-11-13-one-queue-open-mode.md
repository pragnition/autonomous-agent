# Open Mode Only, One Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete everything the live setup doesn't run, then put every conversation on one queue. Sessions pick their own project with `work_on`, up to 10 run in parallel, and a Slack DM or thread keeps one Claude session.

**Architecture:** Core keeps routing only by origin facts it already has:
- A project channel, a Linear issue or `start_task` binds a project before the first turn.
- Everything else is left to the session, which calls the runner-side `work_on` tool. That tool asks core to bind the conversation, then makes a checkout inside the conversation's session folder.
- Each conversation stores its project, its fixed `branch` and its `workdir`, so every later turn resumes in the same place.

DMs become one rolling conversation. A new `(channel, ts) → conversation` map routes thread replies.

**Tech Stack:** Python 3.12, aiohttp, SQLite, claude-agent-sdk, pytest (run in the dev image via `just test`), React/TS dashboard (vitest via `just dashboard-test`).

**Spec:** `docs/superpowers/specs/2026-11-13-one-queue-open-mode-design.md`

## Global Constraints

- **Run the tests in the dev image.** Use `just test <pytest args>`. A task is done when `just test` passes in full.
- **Baseline:** the whole suite passed at the start of this plan, on commit `c3e0015`.
- **Commit messages:** `type(scope): summary`. No co-author trailer, and no line naming an AI tool.
- **The repo is public.** Never commit `data/`, `*.env` or `claude_proxy/`.
- **Leave `src/agent/memory/` alone,** including test-only helpers such as `versions`, `forget`, `purge`,
  `remember_explicit`, `SessionContext.role` and `MAX_OUTPUT_TOKENS`.
- **Fixed values:**
  - The queue name: `AGENT_QUEUE = "agent"`, defined in `src/agent/db.py`. `agent.core.sessions` re-exports it.
  - `DM_IDLE = timedelta(hours=12)`.
  - The `[limits] max_concurrent_sessions` default: 10.
- **Write docs neutrally.** Describe changes by phase. Name nothing after a person or a meeting.
- **Schema changes** follow the pattern Task 3 sets up:
  - `SCHEMA` and `CONVERSATIONS` hold the final shape for fresh databases.
  - Each upgrade is one entry in `_MIGRATIONS`. An entry checks before it acts: `PRAGMA table_info` before adding or
    dropping a column, `IF EXISTS` and `IF NOT EXISTS` for tables.

## Review Focus

1. **A second DM message arrives while the first turn is still running.** It must join the same conversation and wait.
   It must not start a new conversation. → Task 10, `test_dm_message_during_running_turn_queues_on_same_conversation`.
2. **`work_on` setup fails, then `work_on` runs again.** The conversation stays bound with its branch, and the second
   call re-runs setup. → Task 8, `test_work_on_reruns_failed_setup`.
3. **Two conversations prepare checkouts of one project at the same moment.** Both succeed, with no git lock error on
   the shared clone. → Task 7, `test_parallel_prepare_same_project`.
4. **A reply in a thread under any chunk of a split bot reply** (over 12,000 characters) goes back to that
   conversation. → Task 10, `test_thread_reply_under_second_chunk_routes_back`.
5. **Upgrading the live database.** Queued `general` items move to `agent`, and old DM conversations stay as they are.
   The next top-level DM starts a new conversation with `thread_ts IS NULL`. → Task 5,
   `test_migration_moves_queue_and_leaves_old_dms`, and Task 10, `test_first_dm_after_upgrade_starts_rolling_chat`.

---

## Phase 1: cleanup (G8)

### Task 1: Remove the core-run `az` tool, proposals and `ask_production`

**Files:**
- Delete:
  - `src/agent/core/cli.py`
  - `src/agent/core/production.py`
  - `tests/test_cli.py`
  - `tests/test_production.py`
- Modify:
  - `src/agent/core/app.py`: the imports of `CliRunner`, `register_cli_tools`, `Proposals` and `production`. The
    `cli_runners` and `proposals` setup (lines 159-162), the `if not self.open:` registration block (166-168), proposal
    expiry in `maintenance` (375-376), and `on_action=` (475).
  - `src/agent/core/cli_admin.py`: delete `Proposals`, `command_text`, `_iso`, `PROPOSAL_TTL` and their imports.
    Keep `parse_device_code`, `DeviceCodeLogin` and `register_login_tools`.
  - `src/agent/connectors/slack_app.py`: delete `ACTION_IDS`, `make_action_handler`, the `on_action` parameter and the
    v1 `intake.handle` fallback.
  - `src/agent/core/agent_api.py:101-104`: base64 file replies. `tool()` returns `{"text": ...}` only.
  - `src/agent/core/agent_tools.py`:
    - delete `ToolReply.file`; `call()` returns `ToolReply(scrub(...))`
    - delete the `"production"` budget in `_box`
  - `src/agent/sandbox/remote_tools.py`: delete the `valid_save_name` import and the saved-file block in
    `forward`. `src/agent/sandbox/link.py`: delete `decode_file`.
  - `src/agent/core/payloads.py`: delete `production()` and `PRODUCTION_RULES`.
  - `src/agent/config.py`:
    - delete the `CliRules` fields `profiles`, `relogin_patterns`, `read`, `confirm`, `deny`, `egress` and
      `max_output_chars`, plus their parsing in `_cli`
    - delete `AZ_BUILTIN_DENY`, and the `production`/`ambient` keys in the briefing and lookup defaults
  - `deploy/slack-app-manifest.yaml`: delete the `interactivity` block.
  - `examples/config/agent.toml`: in `[cli.azure]`, delete `profiles`, `relogin_patterns`, `read`, `confirm`, `deny`
    and `egress`, and reword the comment above it to "Azure CLI login (az_login): the agent runs az itself".
  - `dashboard/src/pages/Activity.tsx:23-26`: delete the `cli_command`, `cli_proposed`, `cli_confirmed` and
    `production_scrub` labels.
- Test (edit):
  - `tests/test_cli_admin.py`: delete the `setup` fixture, the Proposals tests and
    `test_slack_action_handler_posts_ephemeral`. Keep the three login and device-code tests.
  - `tests/agent_fixtures.py:28-36`: the CLI rules.
  - `tests/test_config.py`: the CLI-rule cases.
  - `tests/test_agent_api.py:153-158`: the file reply.
  - `tests/test_sandbox_runner.py:164, 237-248`: `az save_to`.
  - `tests/test_payloads.py:75-80`: production.
  - `tests/test_agent_tools.py`: the production budget.
  - `tests/test_sessions.py:61-65`: the `"production"` kind.
  - `tests/test_deploy_files.py:96`: assert that the manifest has no `interactivity` key.
  - `tests/test_dashboard_queries.py:264`: change the sample event type to `session_started`.

**Interfaces:**
- Produces:
  - `ToolReply(text: str)`, with no `file` field
  - `/agent/tool` → `{"text": str}`
  - `CliRules(command, env, login, login_check)`
- Kept:
  - `register_login_tools(router, login)`
  - `DeviceCodeLogin(slack, *, config, rules, exec_fn, run_as)`

- [ ] **Step 1:** Change `test_deploy_files.py` so it asserts the manifest has no `interactivity` key. Add
  `test_cli_rules_keep_only_login_fields` to `tests/test_config.py`: load the example config and assert that
  `set(vars(config.cli["azure"])) == {"command", "env", "login", "login_check"}`.
- [ ] **Step 2:** Run `just test tests/test_deploy_files.py tests/test_config.py`. Expected: both new assertions fail.
- [ ] **Step 3:** Make the deletions and edits listed under Files. Then grep `src/` for `register_cli_tools`,
  `Proposals`, `ask_production`, `production_payload`, `valid_save_name` and `content_b64`. Expected: nothing.
- [ ] **Step 4:** Run `just test`. Expected: all pass. Then run `just dashboard-test`. Expected: pass.
- [ ] **Step 5:** Commit with `refactor(core): drop the core-run az tool, proposals and ask_production`.

### Task 2: Remove the isolation switch and the sandboxes mode

**Files:**
- Delete:
  - `src/agent/clients.py`
  - `src/agent/core/sandbox_tools.py`
  - `src/agent/defaults/system.md`
  - `tests/test_sandbox_tools.py`
- Modify:
  - `src/agent/core/app.py`:
    - delete `_sandboxd_for`, `ENSURE_GRACE_S`, `RUNNER_SILENT_S`, the `sandboxd=` parameter and attribute, and
      `pulls`/`_started`/`_ensured`
    - delete `ensure_sandboxes` and its loop line, `sandboxd_token=`/`pulls=` in `make_agent_app`, and the
      `SANDBOX_TOKEN_TTL_S` and `mint_token` imports
    - delete `self.open`, making the open-mode branches (lines 377, 424) unconditional; `DeviceCodeLogin` always gets
      `run_as=self.config.sandbox.agent_uid`
    - rewrite the module docstring for one agent container
  - `src/agent/core/agent_api.py`:
    - delete the `activity` route, `sandboxd_token`, `hmac` and `pulls`
    - in `pull()`, delete the `pulls[sandbox] = ...` line
  - `src/agent/core/payloads.py`: `system_prompt()` always uses `DEFAULT_SYSTEM_OPEN`. Rename the constant to
    `DEFAULT_SYSTEM`, pointing at `defaults/system-open.md`.
  - `src/agent/core/bundles.py`: delete the `system.md` entry from `_files`.
  - `src/agent/core/gitserve.py:125-131`: delete the `debug-{project}` alternative and rewrite the docstring.
  - `src/agent/config.py`:
    - delete `ISOLATION`, `NETWORKS`, `DEFAULT_RESOURCES`, `SandboxResources` and `InjectRule`
    - from `SandboxSettings`, delete `resources`, `host_memory_budget`, `idle_stop_minutes`, `rebuild_every_days` and
      `isolation`; keep `agent_uid`
    - from `ProjectConfig`, delete `network`, `egress_allow`, `egress_inject`, `scrub_patterns`, `sandbox_setup`,
      `sandbox_resources` and `sandbox_template`
    - delete their parsing, `_hosts`, `_resources`, `SANDBOXD_TOKEN` in `RESERVED_SECRETS`, and the `inject` term in
      the reserved-secret check
  - `src/agent/doctor.py`: delete `SANDBOXD_TOKEN`, the isolation branch in the required secrets, `egress_inject`,
    and the isolation guard. The runner-token check always runs.
  - `src/agent/supervisor.py:57-62`: delete the sandboxd health target, and fix the stale "worker" comments.
  - `src/agent/sandbox/runner.py`:
    - delete `main()`, `chown_tree`, `_own`, `pwd`, `run_as_root`, the `os_user`/`user` plumbing and the
      `read_only` branch of `_workdir`
    - fix the module docstring: one agent container, no OS users
  - `src/agent/__main__.py`: delete the `runner` subcommand.
  - `src/agent/sandbox/worktrees.py`: delete `prepare_read_only`.
  - `src/agent/sandbox/remote_tools.py`: delete the `user` parameter from `run_shell` and `remote_sdk_tools`.
  - `compose.yaml`: delete the `127.0.0.1:18080:8080` publish, `SANDBOXD_URL`, the relay's 28100 listener, and the
    comments about sandboxes.
  - `Dockerfile:30`: reword the comment to "azure-cli: core runs az_login with the admin's device code".
  - `deploy/proxy-origins.example.toml:3-4`: reword the comment.
  - `examples/config/agent.toml`:
    - delete `isolation`, the resource tables and the "rest of this table" comment; keep `agent_uid`
    - fix the line 23 comment
  - `examples/config/packs/sample/project.toml`, `examples/pack-templates/chilliclaw/project.toml`: delete
    `network = "open"`. The sample also loses its `just new-pack` mention.
- Test (edit):
  - `tests/test_wiring.py`: delete the fake `Sandboxd`, the isolation arguments in `make_app`,
    `test_sandboxes_mode_keeps_v2_tools`, both `ensure_sandboxes` tests and lines 210-215.
  - `tests/test_config.py`: the isolation, resources, network, egress, scrub and sandbox-table cases.
  - `tests/test_agent_api.py`: the activity and pulls tests.
  - `tests/test_doctor.py`: the `"runner"` subcommand and the isolation cases.
  - `tests/test_supervisor.py:69-73`.
  - `tests/test_sandbox_runner.py`: the read-only debug test (137-142), and the `run_as_root=` and `user` arguments.
  - `tests/test_gitserve.py:122-130`.
  - `tests/test_bundles.py:33`.
  - `tests/test_deploy_files.py`: lines 25, 28, 45-46, 74 and 147. Assert that no `18080` publish and no
    `SANDBOXD_URL` remain.

**Interfaces:**
- Produces:
  - `SandboxSettings(agent_uid: int)`
  - `ProjectConfig(name, repo, default_branch, owner, slack_channel, linear_team, linear_status, build_setup, build_checks, secrets_worker)`
  - `run_shell(cmd, cwd, timeout_s=CHECK_TIMEOUT_S, env=None)`
  - `make_agent_app(*, conn, queue, gate, tools, context_for, bundles, on_result, on_files, pull_wait_s=25.0, session_ttl_s=6000, runs_dir=None)`

- [ ] **Step 1:** Add `test_compose_has_no_sandbox_leftovers` to `tests/test_deploy_files.py`. It asserts that
  `"18080:8080"`, `"SANDBOXD_URL"` and `"28100"` are not in `compose.yaml`. Add `test_old_sandbox_keys_still_load`
  to `tests/test_config.py`: an `agent.toml` that still has `isolation = "open"` and `[sandbox.resources.code]`
  loads with no problems.
- [ ] **Step 2:** Run `just test tests/test_deploy_files.py tests/test_config.py`. Expected: the compose test fails.
- [ ] **Step 3:** Make the deletions and edits. Then grep `src/ tests/ compose.yaml Dockerfile examples/` for
  `isolation`, `sandboxd`, `SANDBOXD`, `egress`, `run_as_root` and `prepare_read_only`. Expected: nothing, except
  the YAML key in `test_old_sandbox_keys_still_load`.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `refactor: open mode is the only mode; drop the sandboxes mode`.

### Task 3: Remove v1 runner leftovers, dead code and the dead schema

**Files:**
- Delete:
  - `src/agent/runner/policy.py`
  - `src/agent/runner/fake.py`
  - `tests/test_policy.py`
  - `tests/test_fake_runner.py`
  - `tests/test_db_migrate.py` (replaced by `tests/test_db_migrations.py`)
- Modify:
  - `src/agent/runner/claude.py`: delete the policy import, `SUBMIT_SCHEMA`, `submit_result_tool`, the
    `submit_tool_factory` default, `redact_env` and the non-bypass branch.
  - `src/agent/runner/base.py`:
    - delete `RunSpec.role`, `effort`, `read_paths`, `write_paths`, `tools`, `os_user` and `max_budget_usd`
    - delete `RunResult.question`, `findings` and `transcript_path`
  - `src/agent/runner/outcome.py`: delete `REPORTED` and the outcomes that can't happen.
  - `src/agent/core/conversations.py:23`: delete `"blocked"` and `"max_budget"` from `FAILED_OUTCOMES`.
  - Delete never-referenced names:
    - `src/agent/httpauth.py`: `OPEN_PATHS`, `bearer_auth`
    - `src/agent/paths.py`: `RUNS_DIR`, `REPOS_DIR`, `WORK_DIR`
    - `src/agent/sandbox/worktrees.py`: `remove`
    - `src/agent/dashboard/queries.py`: `SOURCES`
  - Delete names only tests use:
    - `src/agent/core/sessions.py`: `WorkQueue.cancel_all`
    - `src/agent/tasks.py`: `open_task_for_linear`
    - `src/agent/dashboard/transcript.py`: `read_entries` and `live_stats`
  - `src/agent/db.py`:
    - delete `outbox` and `proposals` from `SCHEMA`
    - delete `tasks.priority`, `tasks.pending_input`, `runs.memory_tokens`, `runs.stage` and `conversations.scope`
    - delete `_V1_WORKING` and the v1 rename
    - add `_MIGRATIONS` and `SCHEMA_VERSION = 3`
  - `src/agent/tasks.py`: drop `priority` and `pending_input` from the `Task` dataclass and its row mapping.
  - `src/agent/core/app.py`: `_record_run` stops writing `stage`.
  - Reword the persona.py and github_write.py docstrings that talk about worker, chat or scout roles.
- Create:
  - `tests/test_db_migrations.py`.
- Test (edit):
  - `tests/test_claude_runner.py`: lines 89-103, 106-133 and 221-226.
  - `tests/test_sandbox_runner.py`: `RunSpec` fields.
  - `tests/test_sessions.py:96-107`.
  - `tests/test_tasks.py:89-94`.
  - `tests/test_dashboard_transcript.py`: lines 123-179 and 193-204.
  - Any fixture that builds `Task` with `priority`.

**Interfaces:**
- Produces:
  - `db.migrate(conn)`, which runs `SCHEMA` and `CONVERSATIONS`, then every `_MIGRATIONS[v]` with
    `v > stored schema_version` in ascending order, then stores `SCHEMA_VERSION`.
  - `_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]]`. Version 3 drops `outbox` and `proposals`, and
    drops the five dead columns where they exist.
  - `RunSpec(run_id, prompt, cwd, model, system_prompt, max_turns, timeout_s, env, mcp_servers, plugins, sdk_tools, resume_session_id, permission_mode, on_message)`.
    Keep any other field that `ClaudeRunner` still reads, and take the final list from the code.

- [ ] **Step 1:** Write two tests in `tests/test_db_migrations.py`.
  - `test_v2_database_upgrades_to_v3`:
    - build a database with the version 2 `SCHEMA` text, copied into the test as a fixture string, and
      `schema_version = 2`, with a `tasks` row that has `priority = 5`
    - run `db.migrate`
    - assert that `outbox` and `proposals` are gone, `priority` is not among `PRAGMA table_info(tasks)`, the task row
      survives, and `schema_version == "3"`
  - `test_fresh_database_is_v3`: assert the same shape on a new file.
- [ ] **Step 2:** Run `just test tests/test_db_migrations.py`. Expected: FAIL.
- [ ] **Step 3:** Implement the migration framework and the deletions. Grep `src/` for `submit_result_tool`,
  `can_use_tool`, `FakeRunner`, `outbox`, `pending_input`, `memory_tokens`, `cancel_all` and
  `open_task_for_linear`. Expected: nothing.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `refactor: drop v1 runner leftovers, dead code and the dead schema`.

### Task 4: Fix the doctor proxy check and `open_pr`'s git identity; tidy the docs

**Files:**
- Modify:
  - `src/agent/doctor.py:106`: build the proxy check from `PROXY_URL`, the variable core's container sets
    (`compose.yaml`), falling back to `ANTHROPIC_BASE_URL`.
  - `src/agent/sandbox/remote_tools.py`: `open_pr`'s auto-commit uses the `GIT_AUTHOR_*` and `GIT_COMMITTER_*`
    already in `os.environ`, which `runners.main` sets from `--git-email`. It falls back to
    `<agent>@users.noreply.github.com` only when they're unset.
  - `docs/superpowers/specs/2026-11-13-open-agent-design.md:3`: say the sandboxes mode was removed in the one-queue
    phase.
  - `docs/superpowers/specs/2026-11-12-autonomous-agent-design.md` and
    `docs/superpowers/specs/2026-11-13-general-agent-sandboxes-design.md`: add a one-line note at the top. It says
    the isolation and container designs were removed in the one-queue phase, and names the sections that still
    apply:
    - v1: memory §6.15, persona §6.13, the proxy §5.4 and metrics §6.11
    - v2: the work-item and runner model
- Delete:
  - `docs/superpowers/plans/2026-11-12-phase-1-core.md`
  - `docs/superpowers/plans/2026-11-13-phase-2-general-agent-sandboxes.md`
- Test:
  - `tests/test_doctor.py`
  - `tests/test_sandbox_runner.py`

- [ ] **Step 1:** Write two tests.
  - `test_proxy_check_uses_proxy_url`: set `PROXY_URL=http://p:1` with `ANTHROPIC_BASE_URL` unset. Assert that the
    doctor's proxy check targets `http://p:1`.
  - `test_open_pr_commit_uses_configured_email`: monkeypatch `GIT_AUTHOR_EMAIL=bot@example.com` and call `open_pr`
    with a fake shell. Assert that the env passed to the commit has `GIT_AUTHOR_EMAIL == "bot@example.com"`.
- [ ] **Step 2:** Run both tests. Expected: FAIL.
- [ ] **Step 3:** Make the two fixes and the doc edits.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `fix: doctor checks the real proxy, open_pr keeps the agent's git email; docs: retire removed designs`.

## Phase 2: one queue (G1-G6)

### Task 5: One queue for everything

**Files:**
- Modify:
  - `src/agent/db.py`: `AGENT_QUEUE = "agent"`. Migration 4 moves `work_items` in status `queued` or `claimed` and
    every `conversations` row to `sandbox = 'agent'`, and keeps `project`.
  - `src/agent/core/sessions.py`:
    - re-export `AGENT_QUEUE`
    - `WorkQueue.claim(sandbox, now=None, kinds: tuple[str, ...] | None = None)` hands out the oldest eligible item
      whose kind is in `kinds`
    - `peek` goes
  - `src/agent/core/agent_api.py` `pull()`: compute
    `kinds = tuple(k for k in ("turn", "chore") if (await gate.can_start(k))[0])` and claim with them. The
    `session_started` event data gains `"project": ctx.project`.
  - `src/agent/config.py`: the `max_concurrent_sessions` default goes from 3 to 10, and so does
    `examples/config/agent.toml`.
  - Every enqueue or conversation insert uses `AGENT_QUEUE`:
    - `src/agent/core/conversations.py`: `_route` and `_attach_linear`
    - `src/agent/core/linear_sessions.py:36`
    - `src/agent/core/consolidation.py:59`
    - `src/agent/core/agent_tools.py`: `_start_task`
    - `scripts/smoke.py`
  - `src/agent/core/runner_tokens.py`: delete `queue_names`. `src/agent/core/app.py` `sync_runners` passes
    `[AGENT_QUEUE]`.
  - `src/agent/core/gitserve.py` `tokens_auth`: `info is not None and info.sandbox == AGENT_QUEUE`.
  - `src/agent/core/kpis.py:100`: filter the queue wait by `data.get("project") == project`.
  - `src/agent/core/app.py` `_record_run`: `role = item.kind`.
  - `src/agent/doctor.py`: the runner-token check expects `[AGENT_QUEUE]`.
- Test:
  - `tests/test_sessions.py`
  - `tests/test_conversations.py` (`claim("general")`/`claim("code-sample")` become `claim(AGENT_QUEUE)`)
  - `tests/test_agent_api.py`
  - `tests/test_db_migrations.py`
  - `tests/test_gitserve.py`
  - `tests/test_kpis.py`
  - `tests/test_runner_tokens.py`
  - `tests/test_linear_sessions.py:55`
  - `tests/test_consolidation.py:60`
  - `tests/test_wiring.py`
  - `tests/test_doctor.py`

**Interfaces:**
- Produces:
  - `AGENT_QUEUE: str`
  - `WorkQueue.claim(sandbox: str, now: datetime | None = None, kinds: tuple[str, ...] | None = None) -> WorkItem | None`
  - The `session_started` event data: `{"work_item", "sandbox", "queued_at", "project"}`

- [ ] **Step 1:** Write the failing tests.
  - `test_claim_skips_kinds_not_allowed`:
    - enqueue a turn, then a chore
    - assert that `claim(AGENT_QUEUE, kinds=("chore",))` returns the chore
  - `test_ten_conversations_run_at_once_eleventh_waits`:
    - use a `Gate` with `max_concurrent_sessions = 10` and a proxy fake that always has capacity
    - enqueue 11 turns across 11 conversations, including 3 bound to `sample`
    - pull 11 times through `make_agent_app`
    - assert 10 got a work item and the 11th got a 204
  - `test_second_turn_of_running_conversation_waits`.
  - `test_dm_and_project_channel_use_one_queue`: a DM, a non-project mention and a project-channel mention all enqueue
    on `AGENT_QUEUE`. The project-channel conversation has `project == "sample"` and the DM has `project IS NULL`.
  - `test_migration_moves_queue_and_leaves_old_dms`:
    - start from a version 3 database with a queued `general` item and a `general` DM conversation whose
      `thread_ts = "5.1"`
    - after `migrate`, the item's sandbox is `agent`
    - the conversation's sandbox is `agent` and its `thread_ts` is still `"5.1"`
  - `test_any_agent_token_reaches_every_repo`: `tokens_auth` accepts an `agent` token for `sample` and for `other`,
    and rejects a revoked one.
  - `test_kpi_queue_wait_filters_by_project`.
- [ ] **Step 2:** Run `just test tests/test_sessions.py tests/test_agent_api.py tests/test_conversations.py tests/test_db_migrations.py tests/test_gitserve.py tests/test_kpis.py`.
  Expected: the new tests fail.
- [ ] **Step 3:** Make the changes listed under Files.
- [ ] **Step 4:** Run `just test`. Expected: all pass. Then grep `src/` for `"general"`, `code-{` and `debug-`.
  Expected: nothing, except the `"general"` stays in `memory/` files, which are left alone.
- [ ] **Step 5:** Commit with `feat(core): one queue for every conversation, ten at once by default`.

### Task 6: Drop profiles; every session gets every pack's skills

**Files:**
- Modify:
  - `src/agent/core/agent_tools.py`:
    - delete `ALL_PROFILES`, `ToolSpec.profiles`, `ToolContext.profile`, `profile_of` and `code_general`
    - `_allowed` checks the tier only
    - `_box` always uses the `"session"` lookup budget
  - Drop the `profiles` argument at:
    - `src/agent/core/prs.py:101`
    - `src/agent/core/linear_app.py:131`
    - `src/agent/core/cli_admin.py:188`
    - `src/agent/core/tracker_tools.py`, anywhere it's used
  - `src/agent/core/payloads.py`: delete the `"profile"` key from `_base` and `chore()`, and drop the `profile_of`
    import.
  - `src/agent/core/bundles.py`: `build_bundle(config_dir: Path) -> tuple[str, bytes]` packs `plugins/core/**` and
    `plugins/packs/<name>/**` for every `config_dir/packs/<name>/plugin` directory, sorted by name.
  - `src/agent/core/agent_api.py` `bundle()`: no query parameters. `bundles: Callable[[], tuple[str, bytes]]`.
  - `src/agent/sandbox/link.py`: `CoreLink.bundle() -> tuple[str, bytes]`.
  - `src/agent/sandbox/runner.py`:
    - delete `SessionUsers` and `profile`
    - the constructor becomes `SandboxRunner(link, worktrees, claude, *, sandbox, bundles_dir, sessions_root, scratch_root, agent_name="agent", heartbeat_s=15, max_parallel=32, extra_env=None, env_files=())`
    - `_bundle()` returns `[core, *sorted(packs/*)]`
    - core's `Gate` holds the real limit; `max_parallel` is only a safety bound
  - `src/agent/sandbox/runners.py`: delete `profile_for`; `make_runner(queue, token)` builds the runner above.
  - `src/agent/core/app.py`: `_record_run` and any other `profile_of` use.
- Test:
  - `tests/test_agent_tools.py`
  - `tests/test_prs.py:120-124`
  - `tests/test_bundles.py`
  - `tests/test_sandbox_runner.py`
  - `tests/test_runners.py`
  - `tests/test_agent_api.py`
  - `tests/test_payloads.py`
  - `tests/test_wiring.py`

**Interfaces:**
- Consumes: `AGENT_QUEUE` (Task 5).
- Produces:
  - `ToolSpec(name, description, input_schema, tier)`
  - `ToolContext(work_item_id, conversation_id, sandbox, project, actor, tier, unattended, task_id, channel, thread_ts)`
  - `build_bundle(config_dir: Path) -> tuple[str, bytes]`
  - `CoreLink.bundle() -> tuple[str, bytes]`
  - The `SandboxRunner` signature above

- [ ] **Step 1:** Write the failing tests.
  - `test_manifest_includes_open_pr_for_unbound_dm`: a DM context's manifest lists `open_pr`, `start_task` and
    `track_task`.
  - `test_bundle_has_every_pack_plugin`: two packs with plugins give `plugins/packs/a/...` and `plugins/packs/b/...`
    in the tar.
  - `test_runner_loads_core_and_all_pack_plugins`: `_bundle()` returns three paths.
  - `test_runner_runs_more_than_eight_conversations`: start 10 fake items whose runs block. Assert that 10 runs are
    in flight together.
- [ ] **Step 2:** Run those tests. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `just test`. Expected: all pass. Then grep `src/` for `profile` (case-sensitive). Expected:
  only memory-module hits and the account-proxy "profile" wording, if any.
- [ ] **Step 5:** Commit with `refactor: no profiles; every session gets every pack's skills`.

### Task 7: A fixed branch per conversation, and checkouts inside the session folder

**Files:**
- Modify:
  - `src/agent/db.py`: in `CONVERSATIONS`, add `branch TEXT` and `workdir TEXT NOT NULL DEFAULT 'session'`.
    Migration 5 adds them where they're missing.
  - `src/agent/core/conversations.py`:
    - add the module function `create_conversation(conn, *, agent_name: str, channel: str | None, thread_ts: str | None, project: str | None, title: str, task_id: str | None = None, is_primary: int = 1) -> str`
    - when `project` is set, it stores `branch = branch_name(agent_name, task_id or convo_id, title)` and
      `workdir = 'checkout'`
    - `_new` delegates to it; `_route` gains a `text` parameter for the title
    - `_attach_linear` passes `title=task.title`
  - `src/agent/core/agent_tools.py` `_start_task`: use `create_conversation(..., title=<title arg>)`.
  - `src/agent/core/linear_sessions.py`: use `create_conversation(..., project=None, title="")`.
  - `src/agent/core/payloads.py`:
    - add `checkout(convo) -> dict | None`, which returns
      `{"project", "default_branch", "branch", "setup", "checks"}` when `convo["branch"]` is set, else `None`
    - `turn()` sets `payload["worktree"] = checkout(convo)`, `payload["workdir"] = convo["workdir"]`, and
      `test_secrets` only when a checkout exists
    - delete the per-turn `branch_hint`
  - `src/agent/sandbox/worktrees.py`:
    - `prepare(path: Path, project: str, branch: str, base: str) -> tuple[Path, bool]` takes the destination path
    - `ensure_clone` is serialized per project with a `threading.Lock` held in a dict
  - `src/agent/sandbox/runner.py`:
    - `_workdir(convo, payload) -> tuple[Path, Path | None]` returns `(cwd, checkout)`
    - `session = scratch_root/convo`; `checkout = session/<project>` when `payload["worktree"]` is set
    - setup runs when the checkout was just created
    - `cwd = checkout` if `payload.get("workdir") == "checkout"`, else `session`
  - `src/agent/sandbox/runner.py` `_prepare`: pass `branch=payload["worktree"]["branch"]` and the checkout path to
    `remote_sdk_tools`, not `branch_hint` and `cwd`.
- Test:
  - `tests/test_conversations.py`
  - `tests/test_payloads.py`
  - `tests/test_sandbox_runner.py`
  - `tests/test_agent_tools.py`
  - `tests/test_db_migrations.py`

**Interfaces:**
- Consumes: `AGENT_QUEUE` (Task 5); the `SandboxRunner` signature (Task 6).
- Produces:
  - `create_conversation(...) -> str`
  - `Payloads.checkout(convo) -> dict | None`
  - the payload keys `worktree: {"project", "default_branch", "branch", "setup", "checks"} | None` and
    `workdir: "checkout" | "session"`
  - `Worktrees.prepare(path, project, branch, base) -> tuple[Path, bool]`
  - `SandboxRunner._workdir(convo: str, payload: dict) -> tuple[Path, Path | None]`

- [ ] **Step 1:** Write the failing tests.
  - `test_project_channel_conversation_gets_branch_at_creation`: a mention "fix login" in `C-SAMPLE` gives
    `branch == branch_name("<config name>", <convo id>, "fix login")` and `workdir == "checkout"`.
  - `test_branch_is_the_same_on_every_turn`: two turns with different first lines carry the same
    `payload["worktree"]["branch"]`.
  - `test_track_task_binding_has_no_checkout`: a conversation with `project` set and `branch` NULL has
    `payload["worktree"] is None` and `test_secrets == {}`.
  - `test_session_workdir_checkout_lives_inside_session_folder`: a payload with `workdir="session"` and a worktree
    gives `cwd == scratch/<convo>` and `checkout == scratch/<convo>/sample`.
  - `test_checkout_workdir_runs_in_checkout`.
  - `test_parallel_prepare_same_project`: two threads call `prepare` for two paths of `sample` at once. Both return
    a repo on its own branch.
- [ ] **Step 2:** Run those tests. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `feat: a fixed branch per conversation; checkouts live in the session folder`.

### Task 8: `work_on`: the session binds its project

**Files:**
- Modify:
  - `src/agent/core/conversations.py`:
    - add `class BindRefused(Exception)`
    - add `bind_project(conn, convo_id: str, project: str, *, packs: dict, agent_name: str, title: str) -> sqlite3.Row`
    - it raises `BindRefused` for an unknown project (the message lists the known ones), or when the conversation is
      bound to a different project
    - otherwise it sets `project`, and sets `branch` if it's NULL; `workdir` stays as it is
    - it logs the event `project_bound`
  - `src/agent/core/agent_api.py`:
    - add the route `POST /agent/bind` (session token) with body `{"project": str, "title": str}`
    - it returns 200 with `bind(work_item_id, project, title)`, or 409 `{"error": str}` on `BindRefused`
    - the new parameter is `bind: Callable[[str, str, str], dict]`
  - `src/agent/core/app.py`: the `bind` callable resolves the work item's conversation, calls `bind_project`, and
    returns `{**payloads.checkout(row), "test_secrets": payloads.test_secrets(project)}`.
  - `src/agent/core/payloads.py`: add `test_secrets(project: str) -> dict[str, str]`, used by `turn()` too.
  - `src/agent/sandbox/link.py`: add `CoreLink.bind(tok, project, title) -> dict`, which returns `{"error": ...}` on
    a 409.
  - `src/agent/core/agent_tools.py`:
    - register `ToolSpec("work_on", <description>, schema({"project": "string"}, {"title": "string"}), "act")`
    - the description lists each pack as `name (repo)`
    - its core handler returns `"work_on is handled by the runner"`
    - `_track_task` refuses with `"this conversation is bound to <p>"` when `ctx.project` is set and differs
  - `src/agent/sandbox/remote_tools.py`:
    - add `@dataclass class Checkout: path: Path | None; branch: str | None; checks: list[str]`
    - `remote_sdk_tools(manifest, link, tok, *, session_dir: Path, checkout: Checkout, worktrees, agent_name, structured_sink=None, structured_schema=None, shell=run_shell)`
    - the local `work_on`:
      1. `details = await link.bind(tok, project, title)`; an error ends the call with that text
      2. `prepare(session_dir/project, ...)`
      3. run setup when the checkout was just created, or when `session_dir/.setup-failed-<project>` exists; remove
         that marker when setup passes, and write it when setup fails
      4. write `session_dir/.test-keys-<project>.env` with mode 0600
      5. update `checkout` in place
      6. return the text with the checkout path, branch, setup result (on failure, the last 1,500 characters of
         output), test-keys path and app env path (`/run/agent/projects/<project>.env` when it exists)
    - `open_pr` with `checkout.path is None` returns `"call work_on(<project>) first"`
    - `post_files` accepts paths inside `session_dir`
  - `src/agent/sandbox/runner.py` `_prepare`: build `Checkout` from `_workdir`'s result and the payload, then pass
    `session_dir=scratch_root/convo`.
  - `src/agent/defaults/system-open.md`: add one bullet, worded as: "Requests can come from a DM, any channel or
    Linear. To work on a project's code, call work_on(project): it gives you a checkout on your own branch, set up
    and ready. One project per conversation; use start_task for work on another project."
- Test:
  - `tests/test_conversations.py`
  - `tests/test_agent_api.py`
  - `tests/test_sandbox_runner.py`
  - `tests/test_agent_tools.py`
  - `tests/test_payloads.py`

**Interfaces:**
- Consumes:
  - `create_conversation`, `Payloads.checkout`, `Worktrees.prepare`, `SandboxRunner._workdir` (Task 7)
  - `AGENT_QUEUE` (Task 5)
- Produces:
  - `BindRefused`, `bind_project(...) -> sqlite3.Row`
  - `POST /agent/bind`
  - `CoreLink.bind(tok, project, title) -> dict`
  - `Checkout`
  - `Payloads.test_secrets(project) -> dict[str, str]`
  - the `work_on` tool

- [ ] **Step 1:** Write the failing tests.
  - `test_bind_sets_project_and_branch_once`.
  - `test_bind_refuses_unknown_and_second_project`.
  - `test_bind_endpoint_returns_test_secrets_unscrubbed`: the 200 body contains
    `test_secrets["SAMPLE_TEST_USER"]` with the real value.
  - `test_work_on_makes_checkout_runs_setup_writes_keys`: use a fake link and the git fixture. The checkout is at
    `session/sample` on the bound branch, the setup command ran, and the keys file is mode 0600.
  - `test_work_on_reruns_failed_setup`: the first setup exits 1, the second exits 0. The second call reports success
    and the marker is gone.
  - `test_open_pr_after_work_on_uses_new_checkout`: `open_pr` in the same tool set pushes the bound branch.
  - `test_next_turn_after_work_on_resumes_in_session_folder`: the conversation has `branch` and `workdir="session"`.
    The payload has a worktree, `_workdir` returns `cwd == session` and `checkout == session/sample`, and
    `test_secrets` holds the keys.
  - `test_track_task_refuses_other_project`.
- [ ] **Step 2:** Run those tests. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `feat: work_on lets a session pick its project and get a checkout`.

### Task 9: The dashboard shows the project, not the queue

**Files:**
- Modify:
  - `src/agent/dashboard/queries.py:89`: delete `"sandbox"` from the row.
  - `dashboard/src/api.ts:13`: delete `sandbox`.
  - `dashboard/src/components/session.tsx:112`, `dashboard/src/pages/Sessions.tsx:119`,
    `dashboard/src/pages/SessionDetail.tsx:117`: show the `project` badge when `row.project` is set, and nothing
    otherwise.
- Test:
  - `tests/test_dashboard_queries.py`
  - the dashboard vitest files that build a `SessionRow`

- [ ] **Step 1:** Write the failing tests.
  - `test_session_row_has_no_sandbox_field`: the session row's keys don't include `"sandbox"`.
  - A vitest case: a session row with `project: "chilliclaw"` renders `chilliclaw`, and one with `project: null`
    renders no badge.
- [ ] **Step 2:** Run `just test tests/test_dashboard_queries.py` and `just dashboard-test`. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `just test` and `just dashboard-test`. Expected: pass.
- [ ] **Step 5:** Commit with `feat(dashboard): show a session's project instead of its queue`.

## Phase 3: Slack keeps context (G7)

### Task 10: A DM is one chat; threads go to the conversation they hang off

**Files:**
- Modify:
  - `src/agent/db.py`: add the table
    `slack_messages(channel TEXT NOT NULL, ts TEXT NOT NULL, conversation_id TEXT NOT NULL, PRIMARY KEY(channel, ts))`.
    Migration 6 creates it.
  - `src/agent/core/slack_markdown.py`: `post_markdown(...) -> list[str]` returns each posted message's `ts`.
  - `src/agent/core/conversations.py`:
    - add `DM_IDLE = timedelta(hours=12)` and `record_message(conn, channel: str, ts: str, conversation_id: str) -> None`
    - `handle_slack`: `thread_ts = event.get("thread_ts") or (None if is_dm else event["ts"])`; record the
      incoming message's `ts`
    - `_route(channel, thread_ts: str | None, is_dm: bool, text: str)`:
      - **top-level DM** (`thread_ts is None`): the latest primary conversation in the channel with
        `thread_ts IS NULL` and `last_active_at >= now - DM_IDLE`, otherwise a new one with `thread_ts = NULL`
      - **thread reply**, in this order:
        1. a waiting conversation on `(channel, thread_ts)`
        2. the primary conversation on `(channel, thread_ts)`
        3. `slack_messages[(channel, thread_ts)]`
        4. otherwise a new conversation
      - every lookup uses `IS ?`, so NULL compares correctly
    - `_known_thread` also consults `slack_messages`
    - `on_result` records every `ts` that `post_markdown` returns
  - `src/agent/core/agent_tools.py` `_post`: record the posted `ts` values.
- Test:
  - `tests/test_conversations.py`
  - `tests/test_slack_markdown.py`
  - `tests/test_db_migrations.py`

**Interfaces:**
- Consumes: `create_conversation` (Task 7); `AGENT_QUEUE` (Task 5).
- Produces:
  - `DM_IDLE`
  - `record_message(conn, channel, ts, conversation_id) -> None`
  - `post_markdown(...) -> list[str]`
  - the `slack_messages` table

- [ ] **Step 1:** Write the failing tests, using the existing `dm()`, `mention()` and `reply()` helpers. `dm()`
  messages have no `thread_ts`.
  - `test_two_top_level_dms_share_conversation`: the second item's `conversation_id` equals the first's.
  - `test_dm_after_idle_starts_new_conversation`: set `last_active_at` back 13 hours, then send a new DM. It gets a
    new conversation.
  - `test_dm_message_during_running_turn_queues_on_same_conversation`: claim the first DM turn, then send a second
    DM. It joins the same conversation, and `claim` returns `None` until the first completes.
  - `test_dm_reply_is_posted_top_level`: `on_result` posts with `thread_ts=None`.
  - `test_thread_reply_under_bot_message_routes_back`: the bot's reply `ts` is recorded. A reply with that
    `thread_ts` lands on the same conversation.
  - `test_thread_reply_under_second_chunk_routes_back`: a 13,000-character reply gives two posts, and a reply under
    the second goes to the same conversation.
  - `test_channel_thread_reply_without_mention_continues`.
  - `test_first_dm_after_upgrade_starts_rolling_chat`: an old DM conversation with `thread_ts="5.1"` exists. A new
    top-level DM creates a conversation with `thread_ts IS NULL`.
  - `test_post_markdown_returns_ts_list`.
- [ ] **Step 2:** Run `just test tests/test_conversations.py tests/test_slack_markdown.py`. Expected: FAIL.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `feat(slack): a DM is one chat and thread replies find their conversation`.

### Task 11: `start_task` from a DM gets its own thread

**Files:**
- Modify:
  - `src/agent/core/agent_tools.py` `_start_task`:
    - when `ctx.channel` is set and `ctx.thread_ts is None`, post one top-level message, `"Working on <title>"`
    - create the child with `thread_ts = <that ts>` and `is_primary=1`, and record the message
    - otherwise keep the current behaviour (the parent's thread, `is_primary=0`)
    - the tool's description becomes "Start a separate session for a piece of work, with a self-contained brief,
      to split one request into parallel tasks or to work on a second project."
- Test:
  - `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `create_conversation` (Task 7), `record_message` (Task 10).

- [ ] **Step 1:** Write the failing tests.
  - `test_start_task_from_dm_posts_its_own_thread`: FakeSlack gets one top-level post with text
    `"Working on fix x"`. The child conversation's `thread_ts` equals that post's `ts`, and `is_primary == 1`.
  - `test_reply_in_child_thread_reaches_child`: a `reply()` with that `thread_ts` lands on the child.
- [ ] **Step 2:** Run those tests. Expected: FAIL.
- [ ] **Step 3:** Implement the change.
- [ ] **Step 4:** Run `just test`. Expected: all pass.
- [ ] **Step 5:** Commit with `feat(slack): work started from a DM gets its own thread`.

## Phase 4: ship

### Task 12: Deploy to the workstation and check it live

This touches the live stack on the workstation: SSH alias `workstation`, `~/autonomous-agent`, branch `phase-1`.
**Ask the user before restarting it.**

- [ ] **Step 1:** Push `phase-1`. Before pushing, scan the diff for secrets: no `data/`, `.env` or `claude_proxy/`.
- [ ] **Step 2:** On the workstation, pull `phase-1`. The workstation carries a local merge of the `effortless`
  commit, so keep it: merge, don't reset.
- [ ] **Step 3:** Edit the live `data/config/agent.toml`:
  - set `max_concurrent_sessions = 10`
  - delete `isolation`, the sandbox resource tables, and the CLI-rule keys from `[cli.azure]` (`profiles`,
    `relogin_patterns`, `read`, `confirm`, `deny`, `egress`)
- [ ] **Step 4:** Run `just up`, then `just doctor`. Expected: every check passes, including the proxy check.
- [ ] **Step 5:** Run the live checks. Send each of these to Jensen in a DM and confirm the result:
  - "hi", then at the top level "what did I just say?". The agent answers from context, and the dashboard shows one
    session for both.
  - "run chilliclaw locally". The transcript shows `work_on("chilliclaw")`, and the dashboard shows the
    `chilliclaw` badge.
  - A reply in that reply's thread lands on the same session.
- [ ] **Step 6:** Report the results, with the session IDs, to the user.
