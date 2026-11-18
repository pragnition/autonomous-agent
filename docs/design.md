# Design

A self-hosted, fully autonomous engineering agent for one team. People message it in Slack (DMs, mentions,
threads), hand it Linear issues or put work on a schedule. Each conversation is a Claude Code session on the
agent's own machine, with a full development environment and the agent's own logins. It does whatever its
credentials reach, end to end and without a person in the loop: it answers questions, investigates incidents,
builds, tests and ships code, sweeps codebases and files issues, reports on schedules, messages people and works in
any other system it has a key for. Its abilities are its credentials plus the skills, plugins and MCP servers it
installs for itself; core adds no gate in its way.

It runs as one Docker Compose stack on a single host. Claude access goes through a Claude account proxy on the
same host. The agent's name is `name` in `agent.toml`; it is the agent's display name and the Linear label it
picks up. In Slack it serves the people listed in `agent.toml` under `[people]`; anyone else gets a
refusal.

## Components

```text
 Slack (Socket Mode)      Linear (webhook, poller)                              GitHub
        |                         |                                                ^
        v                         v                                                | PR stats
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
 |  its own gh, az and Slack logins, the internet                                       |
 +-------+-----------------------------------------------------------------------------+
         | Claude API, placeholder token
         v
 relay (host network, 172.30.0.1:28090/28091) --> account proxy (host, 127.0.0.1:18090/18091) --> Claude
 supervisor + supervisor-standby: poll core, the runner's heartbeat and each other every minute; restart and alert
```

The stack network `agentnet` is 172.30.0.0/24 with the host gateway at 172.30.0.1. core has the fixed address
172.30.0.10.

| Listener | Where | Reached by |
|---|---|---|
| core :8080 | stack network only | the runner (`http://core:8080`): agent API, git endpoint; the supervisors: `/health`, `/health/runner` |
| supervisor :8090 | stack network only | the other supervisor: `/health` |
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
- runs background loops: git mirror refresh (5 min), Linear poll (1 min), schedules (1 min), PR stats (5 min), KPI digest check
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
| `claude/` | the agent's Claude home (`CLAUDE_CONFIG_DIR`, also `~/.claude`), shared by every session: its sessions, so the next turn can resume, and the skills, plugins and MCP servers it installs itself |
| `memory/` | Claude Code's auto memory, shared by every session |
| `transcripts/<work item>.jsonl` | the runner's own transcript of each turn |
| `bundles/<version>/` | the core plugin, the effortless plugin and every pack's plugin |

The schedules file is outside the volume, at `/data/schedules/schedules.toml` (the host's `data/schedules/`), the
same path as in core.

Sessions run with `permission_mode="bypassPermissions"`. Each gets the system prompt and persona from the payload,
the plugins, the MCP servers from `[mcp]` (with `${VAR}` filled from the session's environment; a server with an
unset variable is left out), and core's tools as in-process SDK tools, so the session token stays in the runner's
process and out of the session's environment and files. The session's environment is the container's, including
the keys from `agent.env` (passed explicitly, because the runner blanks every secret-looking variable a session
would otherwise inherit), plus a git identity (`AGENT_NAME`, `AGENT_GIT_EMAIL`), `LINEAR_APP_TOKEN` from
`data/runner/linear.env`, the project's test keys, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
(the effortless plugin, which picks each prompt's reasoning effort, needs it). The last two cannot be overridden.

A Claude session is never resumed (`--resume`, with or without `--fork-session`) on a different account from the
one it ran on. Core keeps the account each conversation's session last ran on (`conversations.session_account`, from
the runner's heartbeat and result) and hands it to the next turn as `resume_account`. Before resuming, the runner
asks the proxy's `/proxy/whoami` which account the session is pinned to. It resumes only when that is
`resume_account` and the account isn't rejected or disabled. Otherwise (another account, an expired pin, an unknown
last account or an unreachable proxy) it starts a fresh session (new id, empty history) with a handoff note
placed after the prompt's who, where and link line. The earlier session's full history is never loaded into the
fresh one, by `/resume` or any other route. The runner reads the earlier record itself and the note carries only a
short digest of it (`agent/runner/handoff.py`): the earlier session id, the text of its last 3 assistant messages
(4,000 characters in all at most) and the files it edited or wrote (its Edit/Write calls). The note gives the
record's path (the Claude Code log `CLAUDE_CONFIG_DIR/projects/…/<session id>.jsonl`) for looking up one specific
detail with grep or tail, and says not to read or load it whole and not to use `/resume` or `--resume`. The session
catches up with `read_thread` and from the conversation folder's own state (git status, its notes), then carries on.
When the proxy reports the account of a running session as rejected or disabled (the watchdog, a 403/503, or a 429
while the account is rejected), the runner does the same within the turn: a fresh session, which the proxy pins
to another account, with the handoff note. Its digest comes from the runner's transcript `transcripts/<run id>.jsonl`
(the earlier session's own messages), whose path the note also gives.
It does this up to twice per turn (the run's `forks` count). If a turn can't resume its saved session and fails
before its first model turn, the runner starts a fresh session with a note to call `read_thread` first.

### docker (dind)

`docker:29-dind`, privileged, container `agent-docker`. It is the agent's own Docker daemon: the apps a session
runs to test its work start here, never on the host's Docker. Its API listens only on 127.0.0.1:2375, and the agent
container shares its network namespace, so an app published on 127.0.0.1:<port> is on the agent's localhost. The
`agentwork` volume is mounted at the same path in both containers, so compose bind mounts of a checkout resolve.
Images and volumes live in the `agentdocker` volume.

### supervisor and supervisor-standby

`python -m agent supervisor`, run twice: containers `agent-supervisor` (`SUPERVISOR_ROLE=primary`) and
`agent-supervisor-standby` (`standby`). They watch core, the runner and each other. Every 60 s each checks core's
`/health`, the runner's heartbeat as core sees it (`core:8080/health/runner`, 503 once the runner hasn't pulled or
heartbeated for 2 minutes) and its peer's `/health` (`:8090`, stack network only). The runner isn't checked while
core is failing, since its heartbeat is read from core. After three failures in a row, a supervisor alerts and
restarts the target through the Docker socket (`agent-core`, `agent-runner`, or the peer's container), and alerts
again when the target is back. A target that is down again within 30 minutes of that restart gets an alert and no
restart, so nothing is restarted in a loop. Each restarts its peer. Only the primary acts on core and the runner;
the standby leaves them alone while the primary is up and takes them over once the primary is down, so nothing is
restarted twice and the same alert is never sent twice. If both supervisors die together, compose's
`restart: unless-stopped` brings them back.

A supervisor's own `/health` answers 503 once its watch loop hasn't finished a tick for three minutes, so a
supervisor whose loop has hung counts as down, not just one whose process is gone. A tick that raises ends the
process and Docker restarts it. Alerts go to the Slack incoming webhook `SLACK_ALERT_WEBHOOK`
(`data/secrets/supervisor.env`, shared by both), prefixed with the role, or to the supervisor's own log when that
isn't set. The supervisors are the only services with the host's Docker socket.

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
`/proxy/whoami` to decide when a session must move to a fresh one on another account, and whether a saved session
would resume on the account it last ran on.

### The dashboard

A React app built into the core image and served by core on :8086. See [KPIs, the activity log and the
dashboard](#kpis-the-activity-log-and-the-dashboard).

## From a message to a session

### Slack intake

core handles a Slack message when it is a DM to the agent, mentions the agent, or is a reply in a thread core knows
(one with a conversation, or one under a message core received or posted). It ignores bots, edits and every
subtype except `file_share` (a message with files attached), and handles each message once. A message's files ride
in its turn's payload (`files`: id, name, mimetype, size); before the session sees the message, the runner fetches
each one through core (`GET /agent/slack_file/{id}`, core's bot token, `files:read`) into the session folder under
`slack-files/` and adds their paths to the prompt. A sender who isn't in `[people]` gets an ephemeral refusal in DMs and on
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

While the usage watch holds scheduled runs (below), the pull skips turns whose `source` is `schedule`; people's
requests still start.

### Usage watch

Every 60 s core reads `/proxy/usage` (`core/usage_watch.py`) and gives each account a state:

- `out`: the proxy gives it no rank (no capacity);
- `hot`: its 5-hour window is at least 85% used, or its `pace_delta` is above 0.05 (ahead of its 7-day pace, so
  it will run out before the window resets). It is `ok` again only once it is below 75% and back on pace, so it
  doesn't flap;
- `ok`: otherwise.

The pool, counted in `ok` accounts: with two or more (or all the verified accounts, in a smaller pool) nothing
changes. With fewer, scheduled runs (and their carry-ons) wait in the queue. With none, the admins get one DM
with the earliest reset, and another when capacity is back. The proxy already ranks accounts for new sessions, so
the watch doesn't steer sessions itself. An account whose `verified` goes false, or that has been `out` for over
5 hours, is reported to the admins once. Every change of state is an event (`usage_account`, `usage_pool`,
`usage_unverified`, `usage_out_too_long`), shown on the dashboard's Events page. The last state is kept in
`settings`, so a restart neither repeats an alert nor misses one.

A turn cut off by a usage limit (a 429, or the runner giving up on an account the proxy reports rejected or
disabled) doesn't fail. The thread is told it was cut off, the message keeps its ⏳, the session and the account it
ran on are kept and the turn goes into `limit_waits` (event `usage_limit_cut`). Once an account is `ok`, the watch
re-queues it as a new turn with a "you were cut off by a usage limit" note (event `usage_limit_resumed`). That turn
goes through the same-account check as any next turn: the runner resumes the session only if the proxy still pins
it to the account it last ran on. Usually it doesn't, since the limit was hit on that account, so the turn starts a
fresh session with the digest handoff note instead (never `/resume` on another account). That happens once: a resumed turn that is cut off again fails like any other. A long project's build keeps its own
retries (below) instead, and the gate holds them until there is capacity.

A claimed item has a 90 s lease that heartbeats extend, and each heartbeat renews its session token too. An item
whose runner stops heartbeating is re-queued, and marked failed on its fifth lost lease. Finishing, cancelling or
losing an item revokes its session token, and the runner interrupts the session at its next heartbeat; a later
claim of the same item runs afresh. Each turn is limited to `[limits] task_minutes` (default 90) of wall time and
`max_turns` (default 400) model turns.

When the runner claims a turn, core refreshes its payload from the conversation as it is at that moment: the
session to resume, the project, the checkout and the test keys. A turn queued behind another one therefore
resumes the session that turn ended with.

### Results

The runner reports the outcome, the session's final text, the session id and token counts. core records a row in
`runs`, saves the session id on the conversation and posts the text, scrubbed of secrets, in the thread as
Markdown. A turn that hit its time or turn limit carries on silently in a new turn (`[limits] continuations`,
default 100; 3 for a scheduled run). An active long project's build also carries on after an error or an
interrupted turn, up to three in a row, and the thread gets the error. Otherwise a failed turn (error, timeout,
turn limit, interrupted) gets a short failure reply, and its task, if any, becomes `failed`. After an error the
next turn starts a fresh session.

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

### Schedules

Work on a timer. People ask the agent for something on a schedule in plain language ("send me PR stats every
morning at 9", "sweep the codebase every night at 10"); the agent writes it into the schedules file with its own
tools (the `schedules` skill), and core fires it. People can also edit the file by hand on the host:
`data/schedules/schedules.toml`, mounted at `/data/schedules` in core and the agent container.

```toml
[nightly-sweep]
cron = "0 22 * * *"           # five-field cron, in agent.toml's timezone
channel = "#project-dev"      # who it's for: a channel name or ID, or a person key for their DM
prompt = "Sweep the codebase and file each real problem as a Linear issue labelled jensen. Skip anything open."
by = "sam"                    # who asked: the turn's actor when it is a person key
link = "https://…"            # where they asked (optional)
paused = false                # optional
```

Every minute core reads the file and, for each schedule whose latest cron slot it hasn't handled yet (`settings`:
`schedule.<name>.last`), queues an unattended turn with the prompt in a new conversation, first line
`[schedule <name> · <cron> · set up by <who> · <link>]`. Core posts nothing: like a teammate, the agent messages
people itself when a run has something for them, and the run's final message goes nowhere, as in a Linear session.
A run that fails says so in the channel (`My <name> run didn't finish.` with the error), and replies in that
message's thread continue the run. Each firing is its own conversation, so runs can overlap; the queue's gate
applies as to any turn.

- A schedule seen for the first time (or back after it was removed or invalid), or whose `cron` changed, waits for
  its next slot.
- Slots missed while core was down collapse into one firing when it comes back.
- Slots due while the schedule is `paused` or the agent is stopped are used up without firing.
- A file that isn't valid TOML or can't be read keeps the last valid schedules; an invalid entry (including a cron
  that never matches) is skipped and the rest still fire.
- Errors (the file's, and firings that couldn't be queued) are DMed to each admin
  once per distinct set and logged; a firing that couldn't start is retried every minute.

`python -m agent schedules` (in either container; `just schedules` on the host) prints each schedule with its next
three run times and any errors, and exits non-zero on errors. The agent runs it after every edit. Core writes its
timezone to `runner/timezone` so the check in the agent container uses the same one.

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

Memory is a folder on the `agentwork` volume (`/var/agent/memory`), shared by every session (DMs, channels and
Linear alike) and kept across restarts and rebuilds. core keeps no memory store of its own; the folder and
everything in it are the runner's doing.

One file per fact, named for what it holds (`people-ivan-replies.md`, `project-roster-demo.md`), each starting
with a short front-matter block (a name, a one-line description of what it is and when it matters) before the
fact itself, with who said it, when and the link. `MEMORY.md` is the folder's index: one line per fact, pointing
at its file (`- [Title](file.md) — hook`). The index holds pointers, never the facts themselves, so it stays
short while the folder grows.

The runner appends the memory rules and the current index — cut to the first 200 lines, with a note if more were
hidden — to every session's system prompt (`memory_block()` in `src/agent/runner/worker.py`), so every session
can read a fact's file when its line matters to what it's doing, and knows how to keep the folder itself: one
fact per file, look for a file that already covers something before adding a new one, fix or delete a file (and
its line) when a fact turns out wrong. Learned facts never go into CLAUDE.md, which is for how to work the repo,
not what was learned along the way.

Within a conversation, the context is the resumed Claude Code session itself.

## Code and PRs

The agent works on code the way an engineer on the team does, with its own tools and no core step in between. It
clones the repos it needs into the conversation's folder with `gh repo clone` and its own `GH_TOKEN` (`agent.env`),
sets them up with the project's skills, branches the way each repo names its branches, pushes, and opens PRs to
the default branch with `gh pr create`, following the repo's own conventions (its CLAUDE.md, AGENTS.md,
CONTRIBUTING and PR template). Draft or ready, who reviews and whether the agent merges are up to the repo's rules
and branch protection and what the token allows; core adds no gate.

core watches the result for the KPIs: every 5 minutes it searches GitHub for PRs by the agent's login and keeps
each one current until it is merged or closed, tying it to a Linear issue whose key it mentions. The PR stats poller
records review and merge times, human commits and lines, and reverts within 30 days.

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
- reads Foundry's own token usage from each answer (input, cached input, cache writes, output), prices it, and
  records it in `llm_usage`;
- refuses calls with 429 `cost_cap_reached` once the day's or the month's cap is spent (days and months in the
  config's timezone), and with 503 while the agent is stopped.

The price is what Azure bills on the upstream's own resource. Every 6 hours core reads the resource's billing
meters from Cost Management with the `[cli.azure]` login, and retries a failed read after 30 minutes. Each meter's cost over its quantity for the last 30 days
gives a rate for input, cached input, cache writes and output per model, with the discount applied. Defender for
AI's per-token charge is added on all but cached input. core then re-prices the month's calls. Azure records
nothing per caller on a shared resource, so the agent's share is its own calls' token counts at those rates.
`[llm_meter.prices]` (USD per million tokens for input, cached input and output; `default` for unlisted models)
applies until a read works, and to models Azure hasn't billed on the resource yet. The dashboard shows which
applies to each endpoint.

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
| `runner/` | written by core for the agent container: `runner-token`, `linear.env`, `projects/`, `timezone` |
| `schedules/schedules.toml` | the schedules; the agent edits it, core fires it |
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
| `name`, `github`, `timezone` | the agent's name (display name, Linear label), its GitHub login, the timezone for digests, caps and schedules |
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
| `just logs [service]` | follows the stack's logs, or one service's (`core`, `agent`, `docker`, `supervisor`, `supervisor-standby`, `relay`) |
| `just kpis [--days N] [--project P]` | prints the KPIs |
| `just schedules` | checks the schedules file and prints each schedule's next three run times |
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
- **The agent's reach is its credentials.** It clones, pushes, opens PRs and merges with its own `GH_TOKEN`, and
  works in any other system with the keys in `agent.env`, its Azure login and its Slack bot token. core puts no
  gate on any of it: what a token allows, the agent can do. Scope each credential, and protect branches, to what
  the agent should be able to do.
- **Secrets are scrubbed from what leaves core.** Every tool reply, `post_update` and other messages core's tools
  post, the session's final reply for each turn, the line of text sent with `post_files`, `slack_read` replies and
  the dashboard's transcript view have known secret values from `core.env` (8 characters or more) and token
  patterns (Anthropic, GitHub, Slack, Linear, AWS keys, private key blocks) replaced with `[redacted]`. The files
  that `post_files` sends are not scrubbed. Values in `agent.env` are unknown to core, so only the patterns catch
  them.
- **Each service has its own secrets.** Each service mounts only its own secrets file. core and the supervisors read
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
