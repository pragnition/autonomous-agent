# Open mode only: one queue, the session picks its project

Builds on spec v3 (`2026-11-13-open-agent-design.md`). Open mode becomes the only mode. The `[sandbox] isolation`
switch goes away, along with all code that only the per-project sandboxes of spec v2 or the older container designs
used (see "Removing what the current setup doesn't use").

## Why

Today where a message comes from decides where its work runs. A DM, or a mention in a channel that isn't a project's
channel, lands in the `general` queue. A `general` session has no checkout, none of the project's skills and no test
keys, and `open_pr` refuses to run there. A coding request sent by DM ("run chilliclaw locally") ran in `general`. The
only way into the project was `start_task`, which hands the work to a second session, and later replies in the DM
still went back to `general`.

In open mode every queue runs in the same `agent` container, so splitting work across queues adds a handoff and
nothing else.

**Intent:** people reach the agent from anywhere (a DM, any channel, Linear). The session that reads the message does
the work, in the right project, with no handoff.

## Goals

- **G1** In open mode every conversation and chore runs on one queue. Where a message came from never limits what its
  session can do.
- **G2** A session picks its project with a `work_on(project)` tool. It gets a branch checkout with setup already run
  and the project's test keys, and the conversation is bound to that project. Later turns of the conversation start
  with all of this in place.
- **G3** When the project is already known (a message in a project's channel, a Linear issue polled for a pack's
  team, `start_task`), core binds it before the first turn. The session starts with the checkout ready, as coding
  sessions do today.
- **G4** Every session gets every pack's plugin, meaning its skills.
- **G5** The dashboard shows a session's project, never its queue name.
- **G6** Work runs in parallel. One queue does not mean one at a time:
  - Up to `[limits] max_concurrent_sessions` turns run at once, and the default goes from 3 to 10.
  - Only turns of the same conversation wait for each other, because they share one Claude session.
  - Every conversation has its own checkout on its own branch, so parallel tasks on the same project never share a
    working copy.
- **G7** A Slack chat keeps its context. Follow-ups in a DM or a thread continue the same Claude session instead of
  starting a new one.
- **G8** The repo keeps only what the current setup runs. The sandboxes mode, the core-run `az` tool and its
  confirmations, `ask_production`, the v1 runner leftovers and other dead code are deleted, along with their tests.

## Non-goals

- Keeping per-project sandboxes as an option. Git history keeps them if they're ever wanted again.
- More than one project in one conversation. A conversation is bound to at most one project. Asking `work_on` for a
  second project is refused, with a pointer to `start_task`. That starts a parallel session bound to the other project,
  and the current session carries on.
- Changes to tasks, `open_pr`'s checks or the KPIs, beyond reading the checkout that `work_on` made. The one change
  to PR linkage is the fixed branch below, which stops duplicate draft PRs.

## Anti-goals

- A handoff: a session whose only job is to send the message somewhere else, or re-running a message in another
  session.
- Guessing the project from the message text in core, with keywords or a classifier call. Core binds a project only
  where the origin already names it (G3). Everything else is the agent's call.

## Approaches

| | Approach | Trade-off | Outcome |
|---|---|---|---|
| A | A `work_on` tool that moves the conversation to `code-<project>` and re-runs the message there | Small change and works in both isolation modes. Costs an extra turn per DM, loses the first session's context, and is a workaround for separate containers that open mode doesn't have. | Rejected |
| B | One queue. The session binds its project partway through with `work_on` | No handoff or extra turn, and the session that reads the message does the work. Bigger change: the runner, payloads, bundles, the git endpoint and the dashboard. Repo-level `.claude/` settings don't apply to checkouts made partway through. | **Chosen** |
| C | Core guesses the project from the message text | Brittle ("cclaw", "chilli"), and puts routing back in core | Rejected |

## Design

### Queue and runner

- Open mode has one queue. `queue_names` returns only that queue, and memory chores run on it too.
- The agent container runs one runner. Its parallel limit comes from `[limits] max_concurrent_sessions` (default 10)
  rather than the hard-coded 3 in `SandboxRunner`. Core's gate already enforces the same limit across everything.
  The account proxy's capacity check still applies on top.
- The queue already hands out one turn per conversation at a time (`WorkQueue.claim`), and that stays. Different
  conversations run side by side.
- `SessionUsers` (`sandbox/runner.py`) goes. Open mode creates no OS users with it, but its pool of 8 quietly caps
  concurrent conversations at 8, below the new limit of 10.
- Checkouts are per conversation, as `Worktrees.prepare` already does: a lightweight clone that shares the project's
  object store, on the conversation's own branch. Ten tasks on chilliclaw means ten checkouts on ten branches.
- The live `agent.toml` on the workstation sets `max_concurrent_sessions = 3` and needs raising to 10 when this ships.
  The workstation has 8 cores and 30 GB RAM, about 21 GB free. Ten sessions that each run the app and a headless
  browser could fill that, so watch memory on the first busy day.
- The git endpoint (`gitserve.tokens_auth`) accepts this queue's token for every pack's repo in open mode. In practice
  this is already the case: every queue's token sits in the one `runner-tokens.json` that the agent user reads.
- Upgrade: open-mode conversations and queued work items still on `code-<project>` or `general` move to the one queue
  and keep their `project`, so threads in flight carry on.

### What core stores per conversation

A conversation is bound once it has a project, and it has a checkout once it has a branch. These are two separate
facts:

- **`project`** is the project it's bound to. One of these sets it:
  - binding up front (G3)
  - `work_on`
  - `track_task`, which records which project the work is about without making a checkout
- **`branch`** is a new column. It's set once, when the checkout is first made, and reused on every later turn and by
  every `open_pr`. Today `branch_hint` is recomputed from each turn's first line. That makes `open_pr` push a
  follow-up turn to a new branch and open a duplicate draft PR. A fixed branch fixes that for every conversation,
  including the ones bound up front.

What a turn starts with follows from these two:

- **Bound up front, first turn:** core sends the checkout details. The runner makes the checkout and runs setup, and
  core stores the branch.
- **Bound with no branch** (bound by `track_task`, or `work_on` failed after binding): no checkout is made. An
  investigation thread never pays for a setup run. `work_on` makes the checkout when the agent wants code.
- **Has a branch:** the checkout is reused and the test keys go in the environment.

### A conversation's working folder

- A conversation's working folder is fixed by its first turn and never changes, so `--resume` always finds the
  session.
  - **Bound up front (G3):** the checkout, as today.
  - **Everything else:** the per-conversation session folder (today's `scratch/<conversation>`).
- `work_on` puts its checkout inside the session folder, at `<session folder>/<project>`. Because the checkout is under
  the working folder, the repo's CLAUDE.md loads when the agent reads files there. The repo's own `.claude/` settings
  and hooks don't apply to these checkouts. That's accepted.
- The branch name comes from `branch_name(agent name, task or conversation id, title)`.
  - With `work_on`, the title is the tool's optional `title` argument, or the first line of the current message.
  - When bound up front, it's the first message's first line.

### `work_on(project)`

- **What it does.** A runner-side tool, because it does local work like `open_pr`. Its arguments are `project` and an
  optional `title`.
  1. It asks core to bind the conversation. Core sets `conversations.project`, fixes `conversations.branch` if it
     isn't set yet, and returns:
     - the project's default branch
     - its setup and check commands
     - the branch name
     - its test keys
  2. It prepares the checkout, runs the setup commands, and writes the test keys to a file in the session folder,
     outside the checkout so they're never committed.
  3. It returns:
     - the checkout path and branch
     - how setup went (a failure includes the tail of its output)
     - the test-keys file path
     - the app env file path (`runner/projects/<project>.env`) when there is one
- **Refusals.**
  - An unknown project: the reply lists the known ones.
  - A conversation already bound to a different project (see Non-goals).
- **Calling it again** for the bound project returns the same checkout. This also covers a conversation bound up
  front.
- **After it runs, in the same session:**
  - `open_pr` and `post_files` use the new checkout. The runner's tools read the binding as it is now, not as it was
    at session start.
  - Core-side tools already see the project, because `context_for` reads the conversation row on every call.
- **Later turns** are bound, so they get the test keys as environment variables and the project's memory scope in
  their briefing. The first turn doesn't get the project's memory scope, and `recall` covers that gap.

### What a session starts with

- **Bundle:** the core plugin plus every pack's plugin.
- **System prompt:** `system-open.md` says requests can come from anywhere and that `work_on(project)` gives a branch
  checkout of a project. The `work_on` description lists the packs by name and repo.
- **Bound conversations** also get the checkout, the setup run and the test keys before the session starts, as coding
  sessions do today.

### Binding up front (G3)

- **Bound before the first turn:**
  - a message in a pack's `slack_channel`
  - a task from the Linear poller
  - `start_task`
- **Unbound, so the agent picks:**
  - DMs
  - other channels
  - Linear agent sessions (`linear_sessions.py`)

### Tools

- `start_task` stays, for splitting work into parallel sessions. The new session is bound to its project. Its
  description drops "hand work from a DM to a project".
- `track_task` creates the task, as today. If the conversation is already bound to a different project, it refuses
  instead of rebinding.
- `open_pr` in a session with no checkout replies "call work_on(<project>) first".

### Slack conversations keep their context

**What happens today, from the live database.** A conversation is keyed by `(channel, thread_ts)`. A top-level DM
message has no `thread_ts`, so its own `ts` is used and every top-level DM message starts a new conversation and a
new Claude session. The live DM has 11 conversations for 12 messages. One example is "login yourself to azure cli…"
followed two minutes later by "done", which landed as a separate session that knew nothing about the first. The bot
also replies in a thread under each DM message, so the DM never reads as a chat. Thread replies already work: the
one reply posted inside a thread resumed the same session.

**What changes:**

- **A DM is one ongoing chat.**
  - Each top-level message continues the person's current DM conversation, and resumes its Claude session.
  - The bot replies at the top level of the DM, not in a thread.
  - After 12 hours with no messages, the next top-level message starts a fresh conversation. That stops one
    session from growing forever and lets the new chat bind a different project.
  - A message sent while a turn is running waits for that turn and goes in next. This is the existing
    one-turn-per-conversation rule.
- **Threads go to the conversation they hang off.**
  - A reply in a thread under any message (the person's or the bot's) goes to the conversation that message belongs
    to.
  - Core records `(channel, ts) → conversation` for every message it receives or posts. Today it only knows each
    thread's root.
- **Channels keep their current model.**
  - A top-level @mention starts a conversation in its thread.
  - Replies in that thread continue it, without another @mention.
- **Work split off with `start_task` from a DM gets its own thread.**
  - Core posts one top-level line in the DM ("Working on <title>"), and that session replies in the thread under it.
  - Replying in that thread talks to that session. The main DM chat carries on at the top level.
- **Existing DM conversations** are left as they are. The rule applies to new messages.
- **Not in this change:** passing a follow-up message into a turn that's already running. It waits for the turn to
  finish.

### Dashboard

Session rows and the session page show the project badge, or nothing when the session isn't bound. The queue name
isn't shown anywhere.

## Removing what the current setup doesn't use

**The rule: keep only what the running setup uses, and delete the rest along with its tests.** The running setup is
compose services `core`, `docker` (dind), `agent` (`python -m agent runners`), `supervisor` and `relay`. Sandboxes
mode can't even start from this repo: `sandboxd` isn't here, only a client for it. Line references below come from
an inventory of the current tree, so check them against the code as you go.

### Whole files to delete

- `src/agent/clients.py`: `SandboxdClient`.
- `src/agent/core/cli.py`: the core-run `az` tool with its read, confirm and deny rules, and `save_to`.
- `src/agent/core/production.py`: `ask_production` and `scrub_customer`.
- `src/agent/core/sandbox_tools.py`.
- `src/agent/defaults/system.md`: the sandboxes prompt. `system-open.md` becomes the only default.
- `src/agent/runner/policy.py` and `src/agent/runner/fake.py`: v1.
- Their tests: `test_cli.py`, `test_production.py`, `test_sandbox_tools.py`, `test_policy.py` and
  `test_fake_runner.py`.

### Cuts inside files that stay

- **`core/app.py`:**
  - the `isolation` switch and `self.open`, keeping the open-mode side unconditionally
  - sandboxd wiring and `ensure_sandboxes` with its loop, grace timers and `pulls` bookkeeping
  - registration of the CLI and production tools
  - `Proposals` and `cli_runners`
- **`core/cli_admin.py`:** cut `Proposals` and its helpers. Keep `parse_device_code`, `DeviceCodeLogin` and
  `register_login_tools`.
- **`connectors/slack_app.py`:** cut the proposal button handler and the v1 `intake` fallback. In
  `deploy/slack-app-manifest.yaml`, `interactivity` goes, and the deploy test that checks it changes with it.
- **`config.py`:**
  - In `[sandbox]`, cut `isolation`, `resources`, `host_memory_budget`, `idle_stop_minutes` and
    `rebuild_every_days`. Keep `agent_uid`.
  - Cut the pack fields `network`, `egress`, `scrub` and `[sandbox.<profile>]`, plus `InjectRule`.
  - In the CLI rules, cut `profiles`, `relogin_patterns`, `read`, `confirm`, `deny`, `egress` and `max_output_chars`.
    Keep `command`, `env`, `login` and `login_check`.
  - Cut `PROFILES`, `ISOLATION`, `NETWORKS`, `DEFAULT_RESOURCES` and `AZ_BUILTIN_DENY`, plus `SANDBOXD_TOKEN` in the
    reserved secrets.
  - Cut the `production` and `ambient` budget keys.
  - The loader ignores unknown keys, so the live `agent.toml` keeps loading. Its stale keys get cleaned up when this
    ships.
- **`doctor.py`, `supervisor.py`:** cut the sandboxd and isolation branches. The runner-token check becomes
  unconditional.
- **`core/agent_api.py`:** cut the sandboxd `activity` route and token, `pulls`, and the base64 file replies, plus
  `ToolReply.file`.
- **`core/payloads.py`:** cut `production()`, `PRODUCTION_RULES`, the isolation branch and the `profile` key.
- **`core/bundles.py`:** cut the unread `system.md` entry and the profile parameter. Every bundle is the core plugin
  plus every pack's plugin.
- **`core/gitserve.py`:** cut the per-queue auth. Any live runner token can reach every pack's repo.
- **`sandbox/runner.py`:**
  - the in-sandbox `main()` and the `runner` subcommand in `__main__.py`
  - `run_as_root`, `chown_tree` and `_own`
  - the read-only debug checkout, `profile`, `SessionUsers`, and the hard-coded `max_parallel`
- **`sandbox/worktrees.py`:** cut `prepare_read_only`. Keep `.agent-out/`, which the ChilliClaw skill writes to.
- **`sandbox/remote_tools.py`:** cut the saved-file block and the `user` parameter.
- **`runner/claude.py`, `runner/base.py`, `runner/outcome.py`:** cut the v1 submit tool, the non-bypass permission
  branch, and the `RunSpec`/`RunResult` fields nothing reads (`role`, `effort`, `read_paths`, `write_paths`, `tools`,
  `os_user`, `max_budget_usd`, `question`, `findings`, `transcript_path`), plus outcomes that can't happen.
- **One-queue cuts:**
  - `ALL_PROFILES`, `ToolSpec.profiles`, `ToolContext.profile`, `profile_of`, `profile_for` and the `code_general`
    tuples
  - `open_pr` restricted to `code`
  - `queue_names`, and `RunnerSet` reconciling several queues
  - `code-<project>`/`general` strings in routing, Linear sessions, consolidation, KPIs (filter by
    `conversations.project` instead), `runs.role` and `scripts/smoke.py`
- **`compose.yaml`:**
  - cut the `127.0.0.1:18080` publish, which only sandboxes used. It also clashes with the proxy example's port.
  - cut `SANDBOXD_URL`, the relay's 28100 listener and the stale comments
- **`Dockerfile`:** reword the `az` comment. `azure-cli` stays in core for `az_login`.
- **`examples/`:**
  - In `agent.toml`, cut the isolation and sandbox resource tables and the CLI rules listed above.
  - Cut `network = "open"` from both pack examples.
  - Remove the reference to `just new-pack`, which doesn't exist.
- **`dashboard/`:** cut the Activity labels for `cli_command`, `cli_proposed`, `cli_confirmed` and
  `production_scrub`, and the `sandbox` field (G5).

### Stays, though it looks container-related

- The `agent.sandbox` package: open mode's runner, worktrees, remote tools and core link live there. Renaming it is
  the builder's call.
- Runner tokens, `WorkQueue`, `Gate`, the git endpoint and the agent API routes the runner uses.
- `DeviceCodeLogin`/`az_login`, `azure-cli` in core, the `data/cli` mounts, `prepare_agent_dirs` and
  `share_project_envs`.
- `memory/scrub` (every tool reply and Slack post) and the dashboard's own transcript scrubber.
- The `agent-sandbox` proxy origin name: it's the agent container's live origin.
- The dind service and its volumes, and the supervisor's `docker.sock`.

### Other dead code

- **Never referenced:**
  - `httpauth.OPEN_PATHS`, `bearer_auth`
  - `paths.RUNS_DIR`, `REPOS_DIR`, `WORK_DIR`
  - `Worktrees.remove`
  - `dashboard/queries.SOURCES`
  - `consolidate.MAX_OUTPUT_TOKENS`
  - `SessionContext.role`
- **Used only by their own tests** (these go too):
  - `WorkQueue.cancel_all`
  - `tasks.open_task_for_linear`
  - `transcript.read_entries`, `live_stats`
- **The memory module (`src/agent/memory/`) is out of scope for this change.** It stays as it is, including helpers
  that only tests call today (`MemoryStore.versions`, `forget`, `purge`, `consolidate.remember_explicit`). A later
  memory change will wire them up.
- **Dead schema:**
  - the `outbox` table
  - `tasks.priority` and `pending_input`
  - `runs.memory_tokens` and `runs.stage`
  - `conversations.scope`

  A startup migration drops them from the live database.
- **The v1 to v2 database migration** (`_V1_WORKING`, the `conversations_v1` rename and `test_db_migrate.py`): it
  goes once the live database is confirmed to have no `conversations_v1` table.
- **Stale docstrings** that describe the worker, chat and scout roles get reworded.

### Bugs to fix along the way

- `doctor.py` builds its proxy check from `ANTHROPIC_BASE_URL`, which core's container doesn't set. Core sets
  `PROXY_URL`, so `just doctor` never checks the proxy.
- `open_pr`'s auto-commit hard-codes `<agent>@users.noreply.github.com` and ignores the configured git email.

### Docs

- **Delete:** the finished plans that only describe removed designs, `plans/2026-11-12-phase-1-core.md` and
  `plans/2026-11-13-phase-2-general-agent-sandboxes.md`.
- **Keep, with a one-line note at the top:** the v1 spec (`specs/2026-11-12-autonomous-agent-design.md`) and the v2
  spec (`specs/2026-11-13-general-agent-sandboxes-design.md`). The note says their isolation and container designs
  were removed, and names the sections that still apply:
  - v1: memory §6.15, persona §6.13, the proxy §5.4 and metrics §6.11
  - v2: the work-item and runner model
- **Edit:** `specs/2026-11-13-open-agent-design.md`, line 3 ("its code stays … behind a config switch").

## Testing

- Open-mode routing: a DM, a non-project channel, a project channel, a Linear poll task, a Linear agent session and
  `start_task` each land on the one queue, bound or unbound as above.
- `work_on`:
  - binds the conversation, prepares the checkout and runs setup
  - is idempotent
  - refuses an unknown project and a second project
  - afterwards, `open_pr` and `post_files` work in the same session
  - the next turn resumes in the same working folder, with test keys in its environment
- The open-mode bundle includes every pack's plugin.
- Parallel work:
  - With the limit at 10, ten turns from ten conversations (several on the same project) are all claimed and
    running at once, each in its own checkout on its own branch.
  - An eleventh waits.
  - A second turn of a conversation that's already running waits for the first.
- Slack context:
  - Two top-level DM messages a minute apart land in one conversation, and the second resumes the first's session.
  - The same two messages 13 hours apart land in two conversations.
  - A thread reply under a bot message goes to that message's conversation.
  - A channel thread reply without a mention continues the thread.
  - A `start_task` from a DM posts its own top-level line and replies under it.
  - Live check: DM "hi", then "what did I just say?" at the top level, and the agent answers from context.
- After the cleanup, the full suite passes in the dev image. Tests of deleted code are deleted; tests that touched it
  in passing are edited.
- `grep` finds no `isolation`, `sandboxd`, `ask_production`, `Proposals`, `code-<project>` or `general` queue names in
  `src/`, `tests/`, `compose.yaml`, `Dockerfile` or `examples/`.
- Live check on the workstation: a DM "run chilliclaw locally" leads to the session calling `work_on("chilliclaw")`,
  running the app, and showing as chilliclaw on the dashboard.

## Left to the builder

- The queue's name. Keeping `general` internally is fine if renaming means churn, since the dashboard no longer shows
  it.
- How the runner's tools see a binding made partway through a session (a shared holder, or asking core).
- The test-keys file name and format, and how the runner gets the keys. Core strips every secret out of tool
  replies, so the keys can't come back as a reply to the bind call. A separate runner-only fetch works, like the
  `test_secrets` field in today's payload.
- The knock-on effects of one queue that the code review turned up:
  - Tools filtered by profile, so `open_pr` must not be hidden.
  - The memory toolbox is cached per work item.
  - `peek` must not hold chores behind turns that are waiting at the concurrency limit.
- How the upgrade migration runs (at startup, or a one-off `just` recipe).
- Whether to rename the `agent.sandbox` package, and what to call it.
- The order: cleanup first, then the one-queue change on top, is the suggested order. It shrinks what the one-queue
  change touches.
