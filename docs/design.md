# Design

A self-hosted engineering agent for one team. Engineers message it in Slack (DMs, mentions, threads) or hand it
Linear issues. Each conversation is a Claude Code session with a full development environment: it answers
questions, investigates incidents, fixes bugs and builds features, tests them end to end, and opens draft pull
requests that a person reviews and merges.

It runs as one Docker Compose stack on a single host. Claude access goes through a Claude account proxy on the
same host. The agent's name is `name` in `agent.toml`; it is the agent's display name, its branch prefix and the
Linear label it picks up. In Slack it serves the people listed in `agent.toml` under `[people]`; anyone else gets a
refusal.

## Components

```text
 Slack (Socket Mode)      Linear (webhook, poller)                              GitHub
        |                         |                                                ^
        v                         v                                                | draft PRs
 +-- core  (agent-core, 172.30.0.10) ----------------------------------------------+---+
 |  intake -> work queue (agent.db) -> tools, git mirrors, every stack credential       |
 |  :8080 agent API + git   :8085 Linear (Funnel)   :8086 dashboard   :8087 meter       |
 +-------^-----------------------------------------------------^-----------------------+
         | pull work, call tools, clone and push               | the apps' model calls
         | (runner and session tokens)                         |
 +-------+------------------- one network namespace -----------+-----------------------+
 |  agent  (agent-runner)                       docker  (agent-docker, dind)            |
 |  runner -> Claude Code sessions              dockerd on 127.0.0.1:2375: the apps     |
 |  /var/agent: clones, sessions, memory        that sessions run and test              |
 +-------+-----------------------------------------------------------------------------+
         | Claude API, placeholder token
         v
 relay (host network, 172.30.0.1:28090/28091) --> account proxy (host, 127.0.0.1:18090/18091) --> Claude
 supervisor: polls core's /health every minute; restarts agent-core and alerts in Slack
```

The stack network `agentnet` is 172.30.0.0/24 with the host gateway at 172.30.0.1. core has the fixed address
172.30.0.10.

| Listener | Where | Reached by |
|---|---|---|
| core :8080 | stack network only | the runner (`http://core:8080`): agent API, git endpoint; the supervisor: `/health` |
| core :8085 | host 127.0.0.1:18085 | Linear, through Tailscale Funnel: `/linear/webhook`, `/linear/oauth/callback` |
| core :8086 | host 127.0.0.1:18086 | people on the tailnet, through `tailscale serve` on :9443 |
| core :8087 | stack network only | apps under test, at `http://172.30.0.10:8087/llm/<upstream>` |
| dockerd :2375 | 127.0.0.1 in the docker service's namespace | the agent container (`DOCKER_HOST`) |
| relay :28090 | 172.30.0.1 | core, to the proxy origin `agent-core` (usage, session pins) |
| relay :28091 | 172.30.0.1 | Claude Code sessions, to the proxy origin `agent-sandbox` (model traffic) |

### core

`python -m agent core`. It holds every credential of the stack (`data/secrets/core.env`, read into memory, never
into the environment), runs only this repository's code and never runs Claude. It:

- receives Slack events over Socket Mode (an outbound connection) and Linear webhooks and polls;
- keeps all state in one SQLite database, `data/db/agent.db` (WAL), created on start;
- serves the agent API, the git endpoint, the public Linear listener, the dashboard and the model-call meter;
- runs core's tools for the sessions;
- runs background loops: git mirror refresh (5 min), Linear poll (1 min), PR stats (5 min), KPI digest check
  (5 min), Linear app token refresh (10 min) and runner token renewal (hourly).

At start it writes the runner token to `data/runner/runner-token`, copies each project's app env file to
`data/runner/projects/`, and gives the agent container's user (`[agent] uid`, default 1001) ownership of what it
shares with it.

### The agent container and the runner

The `agent` service (container `agent-runner`) is where Claude Code runs. The image has Python 3.12, Node 22, bun,
just, git, gh, az, the Docker CLI, Playwright's CLI with Chromium, build tools and CJK and emoji fonts. It runs as
user `agent` (uid 1001) with passwordless sudo and bash as `/bin/sh`.

`python -m agent runner` reads the runner token from `/run/agent/runner-token` (`data/runner`, mounted read-only)
every 15 s and starts, restarts or stops the runner when the token appears, changes or goes away. The runner:

1. long-polls `POST /agent/pull` with the runner token;
2. gets a work item, a session token for that item and the tool manifest for the item's tier;
3. fetches the plugin bundle (cached by version), prepares the working folder and runs the turn as a Claude Code
   session through the Agent SDK;
4. sends a heartbeat every 15 s and forwards every session message to core (`/agent/events`);
5. posts the result to `/agent/result`.

Everything lives under `/var/agent`, the `agentwork` volume, which survives rebuilds:

| Path | Contents |
|---|---|
| `clones/<project>` | one shared clone per project, from core's git endpoint |
| `scratch/<conversation>/` | the conversation's session folder; its checkout is `scratch/<conversation>/<project>` |
| `sessions/<conversation>/` | the session's `CLAUDE_CONFIG_DIR`, so the next turn can resume it |
| `memory/` | Claude Code's auto memory, shared by every session |
| `transcripts/<work item>.jsonl` | the runner's own transcript of each turn |
| `bundles/<version>/` | the core plugin, the effortless plugin and every pack's plugin |

Sessions run with `permission_mode="bypassPermissions"`. Each gets the system prompt and persona from the payload,
the plugins, the MCP servers from `[mcp]` (with `${VAR}` filled from the session's environment; a server with an
unset variable is left out), and core's tools as in-process SDK tools, so the session token stays in the runner's
process and out of the session's environment and files. The session's environment is the container's, including
the keys from `agent.env` (passed explicitly, because the runner blanks every secret-looking variable a session
would otherwise inherit), plus a git identity (`AGENT_NAME`, `AGENT_GIT_EMAIL`), `LINEAR_APP_TOKEN` from
`data/runner/linear.env`, the project's test keys, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
(the effortless plugin, which picks each prompt's reasoning effort, needs it). The last two cannot be overridden.

If a turn can't resume its saved session and fails before its first model turn, the runner starts a fresh session
with a note to call `read_thread` first, placed after the prompt's who, where and link line. When the proxy reports
the session's account as rejected or disabled, the runner forks the session (same history, new session id, which
the proxy pins to another account), up to twice.

### docker (dind)

`docker:29-dind`, privileged, container `agent-docker`. It is the agent's own Docker daemon: the apps a session
runs to test its work start here, never on the host's Docker. Its API listens only on 127.0.0.1:2375, and the agent
container shares its network namespace, so an app published on 127.0.0.1:<port> is on the agent's localhost. The
`agentwork` volume is mounted at the same path in both containers, so compose bind mounts of a checkout resolve.
Images and volumes live in the `agentdocker` volume.

### supervisor

`python -m agent supervisor`, container `agent-supervisor`. Every 60 s it checks core's `/health`. After three
failures in a row it alerts and restarts `agent-core` once through the Docker socket; it alerts again when core is
back. Alerts go to the Slack incoming webhook `SLACK_ALERT_WEBHOOK` (`data/secrets/supervisor.env`), or to its own
log when that isn't set. It is the only service with the host's Docker socket.

### relay

An Alpine container with socat on the host network. It listens only on the stack's gateway address, 172.30.0.1,
and forwards 28090 to the proxy's 127.0.0.1:18090 and 28091 to 127.0.0.1:18091. The proxy is never on the LAN.

### The Claude account proxy

A separate program on the host, not part of this repository. Place it at `claude_proxy/claude-fallback-proxy.py`
in the checkout (ignored by git) before running `just proxy-install`, which installs a systemd user service
(`deploy/claude-fallback-proxy.service`) that runs it from there. It spreads sessions across the team's Claude
accounts and reports their usage: it holds the accounts, pins each session to one account and replaces the
placeholder token that sessions send. Each origin has its own port; `deploy/proxy-origins.example.toml` lists the
two this stack uses. core (origin `agent-core`) reads `/proxy/usage` for the queue's gate and `/proxy/usage`,
`/proxy/perf` and `/proxy/whoami` for the dashboard. The runner (origin `agent-sandbox`) reads `/proxy/usage` and
`/proxy/whoami` to decide when to fork a session.

### The dashboard

A React app built into the core image and served by core on :8086. See [KPIs, the activity log and the
dashboard](#kpis-the-activity-log-and-the-dashboard).

## From a message to a session

### Slack intake

core handles a Slack message when it is a DM to the agent, mentions the agent, or is a reply in a thread core knows
(one with a conversation, or one under a message core received or posted). It ignores bots, edits and other
subtypes, and handles each message once. A sender who isn't in `[people]` gets an ephemeral refusal in DMs and on
mentions. Each accepted message gets an `eyes` reaction.

Routing picks the message's conversation (one row in `conversations`, one Claude Code session):

- **DM, top level**: starts its own thread and conversation, like a mention in a channel. The reply goes in the thread
  under the message.
- **Thread** (in a channel or a DM): a conversation waiting for an answer in that thread (it called `ask`) takes the
  message first; otherwise the thread's primary conversation; otherwise the conversation of the message the thread
  hangs off. Every message core receives or posts is recorded in `slack_messages`, so a thread under any of them
  finds its way back. The reply goes to the thread the message came from.
- **New**: a mention outside any known thread starts a conversation for that thread. In a channel that is a pack's
  `slack_channel`, the conversation is bound to that project from the start.

A message containing "use fable" switches its conversation to `[claude.models] hard` for the rest of the
conversation; otherwise sessions use `default`.

### The who, where and link line

Each turn's prompt starts with one line saying who sent the message, where it came from and its link:

```text
[from <Slack name> (<person key>) · DM · <permalink>]
[from <Slack name> (<person key>) · #<channel> thread · <permalink>]
[Linear SAM-7 · <issue URL>]
```

The name comes from Slack's `users.info` and the link from `chat.getPermalink`, both cached; a failed lookup is
retried after 10 minutes. Each lookup may take at most 3 s, and a part that isn't known in time is left out.
Messages in one channel still route in arrival order. A turn started with `start_task` gets the calling turn's
line followed by `(handed over by <agent name>)`; the smoke test's turn has no line.

### One queue

Every turn is a `turn` work item in one queue (`work_items`). The runner's pull hands out the oldest claimable item:
queued, and its conversation has no other item running. So a conversation runs one turn at a time, and a message
sent while a turn is running becomes the next turn of the same session.

Before claiming, core's gate checks three things:

- the agent isn't paused (stop);
- the account proxy reports capacity on at least one account;
- fewer than `[limits] max_concurrent_sessions` items are running (default 10).

A claimed item has a 90 s lease that heartbeats extend. An item whose runner stops heartbeating is re-queued once,
then marked failed. Finishing, cancelling or losing an item revokes its session token, and the runner interrupts
the session at its next heartbeat. Each turn is limited to `[limits] task_minutes` (default 90) of wall time and
`max_turns` (default 400) model turns.

When the runner claims a turn, core refreshes its payload from the conversation as it is at that moment: the
session to resume, the project, the checkout and the test keys. A turn queued behind another one therefore
resumes the session that turn ended with.

### Results

The runner reports the outcome, the session's final text, the session id and token counts. core records a row in
`runs`, saves the session id on the conversation and posts the text, scrubbed of secrets, in the thread as
Markdown. A failed turn (error, timeout, turn limit, interrupted) gets a short failure reply, and its task, if any,
becomes `failed`. After an error the next turn starts a fresh session.

### Linear

There are two ways in:

- **Labelled issues.** Every minute core polls each pack's `linear_team` for open issues labelled with the agent's
  name. Each new issue becomes one task. core posts `Linear <ID>: <title>` in the pack's `slack_channel` and starts
  an unattended turn bound to the project in that thread. Task changes set the issue's state through
  `[linear.status]`, and a ready PR is commented on the issue.
- **Linear agent sessions.** With the Linear app installed (`[linear] public_url` and the app's client secrets set),
  people can delegate issues to the agent. Linear's webhook reaches core's public listener through Tailscale
  Funnel; core checks the signature and timestamp, acknowledges with an "On it." activity and queues the raw event
  as a turn. Follow-ups in the same Linear session resume the same Claude session. The agent reads the issue,
  picks the project, replies and posts activities itself through the Linear MCP server, which runs as the app
  with `LINEAR_APP_TOKEN` (core refreshes it in `data/runner/linear.env`).

An admin installs the app with the `linear_install` tool: core DMs them a single-use link valid for 15 minutes.

### Tasks and status in Slack

A task is the record of a piece of work (`tasks`: feature, bug or chore; statuses queued, working, needs_input,
ready_for_review, done, merged, closed, failed, cancelled). Linear issues create one; in Slack, `track_task`,
`start_task` or the first `open_pr` does. Each task has one status message in its thread, edited in place, and a
status reaction on the thread's first message. `[limits] max_tasks_per_day` (default 20) caps the tasks that
`track_task` and `start_task` create.

## Projects and packs

A project is a pack in `data/config/packs/<project>/`:

- `project.toml`: `[project]` `name`, `repo` (owner/name on GitHub), `default_branch`, `owner` (a person key: PRs
  from unmapped requesters are assigned to them), `slack_channel`, `linear_team`; `[linear.status]` (task status
  to Linear state name for `working`, `ready_for_review`, `merged`, `failed`); `[build]` `setup` and `checks`;
  `[secrets]` `worker`.
- `plugin/`: a Claude Code plugin (`.claude-plugin/plugin.json` and skills). Every session gets every pack's plugin.

`examples/config/packs/sample/` is a minimal pack; `examples/pack-templates/chilliclaw/` is a full example with
setup that writes the app's `.env`, local-run skills and a meter overlay.

### work_on and checkouts

A session starts in its session folder. To work on a project's code it calls `work_on(project, title?)`:

1. core binds the conversation to the project and gives it a branch, `<agent name>/<task or conversation
   id>-<slug of title>`. The title defaults to the first line of the message. A conversation has one project and
   one branch for its whole life; `start_task` starts a separate session for another project.
2. The runner makes the checkout at `<session folder>/<project>`: a clone of core's git endpoint that borrows
   objects from the shared clone (`clone --reference`), on the branch, from `origin/<default_branch>`.
3. `[build] setup` runs once, when the checkout is made. If it fails, the next `work_on` runs it again.
4. The project's test keys (`[secrets] worker`: names of values in `core.env`) are written to
   `.test-keys-<project>.env` (mode 0600) in the session folder and reach later turns as environment variables. The
   stack's own credentials can't be listed there.
5. If `data/secrets/projects/<project>.env` exists, `work_on` points the session at its copy,
   `/run/agent/projects/<project>.env`: the app's own `.env` for running it locally.

The session keeps the same working folder on every turn so `--resume` finds it. A conversation bound up front (a
pack's channel, a labelled Linear issue, `start_task`) runs inside its checkout; any other runs in its session
folder with the checkout as a subfolder. If something other than a checkout the runner made is at the checkout's
path, the runner leaves it alone, runs the turn without a checkout and tells the session why.

`[build] checks` run before every PR (see below). `just doctor` flags a pack with no checks.

## Tools and tiers

core's tools reach the session as in-process SDK tools. A session's tier comes from the person whose message
started the turn, never from the model's arguments: `admin` for people with `role = "admin"`, `act` for members and
for unattended Linear work without a mapped requester, `read` otherwise. A tool is offered only at or above its
tier. Every reply is scrubbed of secrets.

| Tier | Tools |
|---|---|
| read | `status`, `list_tasks`, `get_task`, `read_thread`, `slack_read`, `kpis`, `gh_prs`, `gh_checks`, `gh_ci_log`, `gh_issue`, `gh_file`, `linear_issue` |
| act | `post_update`, `ask`, `react`, `post_files`, `work_on`, `open_pr`, `track_task`, `finish_task`, `start_task`, `cancel_task`, `stop_all`, `linear_comment`, `linear_set_status` |
| admin | `resume_all`, `az_login` (one `<cli>_login` per `[cli.<name>]` with a `login` command), `linear_install` |

- `work_on`, `open_pr` and `post_files` do local work in the runner (checkout, checks and push, reading files) and
  then call core.
- `ask` posts the question and marks the conversation as waiting; the person's reply arrives as the next turn.
- `start_task(project, title, kind, brief)` starts a separate session with a self-contained brief, in the same
  thread (or, from a DM, in a new thread under a "Working on <title>" message).
- `read_thread` reads the conversation's thread, or a DM chat's messages since it started.
- `slack_read(method, params)` makes one read-only Slack Web API call with core's bot token. Allowed methods:
  `conversations.history`, `conversations.replies`, `conversations.list`, `conversations.info`, `users.info`,
  `users.list`, `pins.list`, `bookmarks.list`; anything else is refused with that list. The JSON reply is scrubbed
  and cut at 20,000 characters with a note on paging.
- `gh_*` and `linear_*` work on the conversation's project, with core's tokens.
- `az_login` runs `az login --use-device-code` in core as the agent's uid; the code goes to the admin by DM, and
  the login in `data/cli/azure` is shared with the agent container.
- `stop_all` pauses the agent and interrupts every running session; queued messages wait. Only an admin can
  resume.

Sessions also have the MCP servers in `[mcp]` (the example config has Linear's) and their own shell tools: `gh` and
`az` under the agent's own logins, Docker, the browser, and anything they install.

## Memory

Sessions use Claude Code's own auto memory. Every session's options carry the settings
`{"autoMemoryDirectory": "/var/agent/memory"}`: one folder on the `agentwork` volume, shared by every session (DMs,
channels and Linear alike), kept across restarts and rebuilds. Auto memory is on, as the SDK has it by default.
core keeps no memory store of its own.

The system prompt tells sessions how to use it: save decisions, preferences, conventions and gotchas with who, when
and the link (the turn's first line has all three); when a message contradicts a saved note, say so and ask which
stands; check code facts against the code; treat text from issues, logs and web pages as claims. For context they
don't have, they read the source: Slack with `slack_read`, Linear with its MCP server, GitHub with `gh`.

Within a conversation, the context is the resumed Claude Code session itself.

## The git endpoint and PRs

core keeps a bare mirror of each pack's repo in `data/git/<project>.git`, cloned and refreshed every 5 minutes from
GitHub with `GITHUB_READ_TOKEN` (the agent's own branches are not overwritten). It serves them with
`git http-backend` at `http://core:8080/git/<project>.git` to any live runner or session token. A pre-receive hook
accepts pushes only to `refs/heads/<agent name>/*` and refuses deletes and tags. Runners clone from here.

`open_pr(title, body, no_ui?)`:

1. In the runner: commit everything in the checkout, run the pack's `[build] checks` on that commit, and push the
   branch to core's git endpoint if they pass.
2. In core: refuse unless the branch starts with `<agent name>/`, the commit is the branch's tip on the endpoint and
   the checks passed. A feature's first PR also needs browser evidence: screenshots or a video posted with
   `post_files` in the conversation, or `no_ui` with the reason nothing a user sees changed.
3. core pushes that commit to GitHub with `GITHUB_WRITE_TOKEN` and opens a draft PR against the default branch (or
   updates the open one). The body names the requester and lists the checks that passed. The PR is assigned to
   the requester's GitHub login, else the pack's owner. The task becomes `ready_for_review`.

There is no merge, delete or force-push path. People review and merge. The PR stats poller then records review and
merge times, human commits and lines, and reverts within 30 days, and moves the task to `merged` or `closed`.

## KPIs, the activity log and the dashboard

KPIs come from what core records: `tasks`, `pr_stats` and the `session_started` event appended when a runner
claims a turn.

- ticket to pickup (labelled Linear issues), pickup to PR, PR to merge, queue wait: median and p90;
- tasks, PRs, merged, closed unmerged, reverted, failed, cancelled; merge rate.

They are available as the `kpis` tool, `just kpis --days 30 --project <name>`, a daily digest in
`[observability] digest_channel` at `digest_time`, and the dashboard. None of them shows a Claude cost: the only
spend shown anywhere is the apps' Azure AI Foundry spend, from the model-call meter.

The activity log is the `events` table, append-only: task creation and status changes, `session_started`,
`project_bound`, `files_posted`, `stop_all`, `resume_all`, `session_cancelled`, cap changes and cap hits. Each
turn's messages are also kept in `data/runs/<work item>.jsonl`.

The dashboard has these pages:

- **Overview**: running and queued sessions, today's finished and failed, the account pool, today's Foundry
  spend, and who paused the agent.
- **Sessions**: one row per conversation (every message in one resumes the same Claude session), with its source,
  project, task, message count, work time, account and tokens. A session's page lists each message as a turn whose
  transcript it streams live and can cancel; a link to a single turn opens its conversation at that turn.
- **Accounts**: the Claude account pool from the proxy (usage windows, tokens, errors, history), with account
  holders' names from `data/config/account-names.toml`.
- **Foundry spend**: the model-call meter.
- **KPIs**: the KPIs with daily series, per project.
- **Activity**: the event log, with where each event came from and who.

The kill switch on the dashboard stops the agent; resuming, like changing the meter's caps, needs an admin whose
`tailscale` login is set in `[people]`.

## The model-call meter

Apps the agent runs and tests may call a model provider (Azure AI Foundry for the example pack). They call it
through core instead: their OpenAI-compatible base URL is `http://172.30.0.10:8087/llm/<upstream>` with any
placeholder key. core:

- adds the upstream's real key from `core.env` (`api-key` header, or `Authorization: Bearer`);
- forwards the call, streaming included (it asks streamed chat completions to report usage);
- reads the token usage from each answer, prices it with `[llm_meter.prices]` (USD per million tokens for input,
  cached input and output; `default` for unlisted models), and records it in `llm_usage`;
- refuses calls with 429 `cost_cap_reached` once the day's or the month's cap is spent (days and months in the
  config's timezone), and with 503 while the agent is stopped.

Caps start from `daily_usd` and `monthly_usd` and can be changed at runtime from the dashboard. The meter runs only
when `[llm_meter.upstreams]` has an entry. The address is fixed because apps in the agent's Docker can't resolve
the name `core`.

## Configuration

Host data lives under `${AGENT_DATA:-./data}`.

| Path | Contents |
|---|---|
| `config/agent.toml` | the install's config (below) |
| `config/persona.toml` | optional: replaces the shipped persona (`examples/config/persona.toml` is a copy) |
| `config/agent/system.md` | optional: replaces the shipped system prompt; `{name}` is filled in |
| `config/packs/<project>/` | one pack per project |
| `config/account-names.toml` | written by `just account-names`: proxy code name to account holder |
| `secrets/core.env` | core's credentials (below) |
| `secrets/supervisor.env` | `SLACK_ALERT_WEBHOOK` (optional) |
| `secrets/agent.env` | the agent container's environment: `GH_TOKEN` for its own `gh` login, its dev and test keys |
| `secrets/projects/<project>.env` | a project's app `.env` for local runs; core copies it at start |
| `runner/` | written by core for the agent container: `runner-token`, `linear.env`, `projects/` |
| `db/agent.db` | core's state |
| `runs/` | each turn's session messages, for the dashboard |
| `git/` | the bare mirrors behind the git endpoint |
| `cli/azure/` | the Azure CLI login, shared with the agent container |

Secrets files are `KEY=value` lines, mode 0600, and each service mounts only its own. `core.env` needs
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `LINEAR_API_KEY`, `GITHUB_READ_TOKEN` and `GITHUB_WRITE_TOKEN`; with the
Linear app also `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` and `LINEAR_WEBHOOK_SECRET`; plus each meter upstream's
`key` and the values the packs list under `[secrets] worker`.

`agent.toml` (see `examples/config/agent.toml`):

| Key | Meaning |
|---|---|
| `name`, `github`, `timezone` | the agent's name (display name, branch prefix, Linear label), its GitHub login, the timezone for digests and caps |
| `[people.<key>]` | `slack` user id and `linear` email (required), `github` login, `role` (`member` or `admin`), `tailscale` login |
| `[limits]` | `max_concurrent_sessions` (10), `max_tasks_per_day` (20), `task_minutes` (90), `max_turns` (400) |
| `[claude.models]` | `default` for every session, `hard` after "use fable" |
| `[agent]` | `uid` of the agent container's user (1001) |
| `[linear]` | `public_url`: the Funnel URL for Linear's webhook and OAuth callback; empty means no Linear app |
| `[mcp.<name>]` | MCP servers every session gets: `type = "http"`, `url`, `headers` with `${VAR}` from the session's environment |
| `[dashboard]` | `port` (8086), `trusted_peers` (`["172.30.0.1"]`), `agent_origins` (proxy origins that are the agent) |
| `[llm_meter]` | `port` (8087), `daily_usd`, `monthly_usd`, `[llm_meter.upstreams.<name>]` (`base_url`, `key`, `header`), `[llm_meter.prices]` |
| `[observability]` | `digest_channel` (empty: no digest), `digest_time` |
| `[cli.<name>]` | a CLI core logs in with a device code: `command`, `env`, `login`, `login_check` |

core reads `agent.toml`, the packs' `project.toml`, the persona and the project env files at start: restart core
after changing them. Pack plugins go out in the bundle, which is rebuilt for every session.

Compose also reads `AGENT_NAME` and `AGENT_GIT_EMAIL` from the environment (or a `.env` file beside
`compose.yaml`, kept out of git) for the git identity of the agent's commits. Use the agent's name and its GitHub
account's noreply address.

## Operations

| Command | What it does |
|---|---|
| `just up` | writes `account-names.toml` (on failure, carries on), then `docker compose up -d --build` |
| `just down` | stops and removes the stack; data stays under `data/` |
| `just doctor` | in core: config, packs, each required secret, the proxy (verified accounts, capacity), each pack's checks, GitHub read access per repo, Linear states per pack, the runner token. In the agent container: `gh auth status` and `az account show` |
| `just smoke` | queues one turn ("reply with pong") with no Slack thread and waits up to 5 minutes for the answer: core, runner, proxy and Claude end to end |
| `just dashboard-serve` | `tailscale serve --bg --https=9443 http://127.0.0.1:18086`: the dashboard at `https://<node>.<tailnet>.ts.net:9443`, tailnet only |
| `just logs [service]` | follows the stack's logs, or one service's (`core`, `agent`, `docker`, `supervisor`, `relay`) |
| `just kpis [--days N] [--project P]` | prints the KPIs |
| `just stop`, `just resume` | the kill switch from the host, for when Slack isn't an option |
| `just account-names` | writes the proxy's account holders' names for the dashboard (run on the host) |
| `just proxy-install` | installs the account proxy, placed at `claude_proxy/claude-fallback-proxy.py` first, as a systemd user service (once) |
| `just test`, `just dashboard-test` | the Python tests in the dev image; the dashboard's tests and build |
| `just dashboard-dev` | the dashboard's dev server against a running core |

`just doctor` exits non-zero when any check fails. The agent's Azure login comes from an admin: ask the agent in
Slack to run `az_login`, and the device code arrives by DM.

Session transcripts are on the dashboard's session pages and in `data/runs/`. `just logs core` shows intake, tool
and loop errors; `just logs agent` shows the runner's.

## Security posture

- **The agent container is open.** Sessions run with every permission, passwordless sudo, internet access, their own
  `gh` and `az` logins and a privileged Docker daemon beside them. Nothing in the operating system limits what a
  session does inside the container. The limits are in core's tools and in what the container's credentials allow.
- **What the container never holds.** core's Slack tokens, `LINEAR_API_KEY` and the Linear app's client secret,
  `GITHUB_READ_TOKEN` and `GITHUB_WRITE_TOKEN`, and the Claude accounts' credentials (sessions send a placeholder
  that the host's proxy replaces). It does hold `agent.env`, the runner token and each turn's session token,
  `LINEAR_APP_TOKEN`, its projects' test keys and app env files, and the shared Azure login.
- **Azure is staging only.** The Azure login that `az_login` creates is shared with the agent; it should have access
  to staging resources only. The system prompt also tells sessions never to change production resources or data.
  The code doesn't check this; the account's role assignments do.
- **Code reaches a pack's repo through open_pr.** Only `<agent name>/*` branches, only a commit whose checks
  passed, only as a draft PR that a person reviews and merges. core holds the write token, and there is no merge,
  delete or force-push path. The agent's own `GH_TOKEN` (`agent.env`) is outside these checks: `gh` can do whatever
  that token allows, so scope it accordingly.
- **Secrets are scrubbed from what leaves core.** Every tool reply, `post_update` and other messages core's tools
  post, the session's final reply for each turn, the line of text sent with `post_files`, `slack_read` replies and
  the dashboard's transcript view have known secret values from `core.env` (8 characters or more) and token
  patterns (Anthropic, GitHub, Slack, Linear, AWS keys, private key blocks) replaced with `[redacted]`. The files
  that `post_files` sends are not scrubbed. Values in `agent.env` are unknown to core, so only the patterns catch
  them.
- **Each service has its own secrets.** Each service mounts only its own secrets file. core and the supervisor read
  theirs into memory, never into their environment; the agent container gets `agent.env` as plain environment
  variables.
- **People and tiers.** In Slack, only people in `[people]` get answers, and their role sets the tools a session is
  offered: `stop_all` is open to members, `resume_all` to admins only. In Linear, anyone who can label an issue
  for the agent or delegate one to it starts work; without a mapped requester it runs at the `act` tier.
- **The dashboard is tailnet only.** `tailscale serve`, never Funnel. core answers it only from `trusted_peers` (the
  host gateway, where Tailscale's requests arrive), takes the viewer from `Tailscale-User-Login`, and requires an
  `X-Dashboard: 1` header and a same-origin request for every POST. The agent container reaches core from its own
  address and is refused. Any tailnet viewer can stop the agent or cancel a session; resuming and changing caps
  need an admin.
- **One public listener.** With the Linear app configured, core's :8085 is public through Tailscale Funnel, with two
  routes: the Linear webhook (HMAC-SHA256 signature over the body, timestamp within a minute) and the OAuth
  callback (single-use state). Nothing else is public; Slack uses an outbound Socket Mode connection.
- **Spend is capped.** The apps' model calls stop at the meter's daily and monthly caps. Claude sessions start only
  when the proxy reports capacity, and at most `max_concurrent_sessions` run at once.
