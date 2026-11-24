# Design

A self-hosted, fully autonomous engineering agent for one team. People message it in Slack (DMs, mentions,
threads), hand it Linear issues or put work on a schedule. Each conversation is a Claude Code session on the
agent's own machine, with a full development environment and the agent's own logins. It does whatever its
credentials reach, end to end and without a person in the loop: it answers questions, investigates incidents,
builds, tests and ships code, sweeps codebases and files issues, reports on schedules, messages people and works in
any other system it has a key for. Its abilities are its credentials plus the skills, plugins and MCP servers it
installs for itself; the control plane adds no gate in its way.

It runs as one Docker Compose stack on a single host. Claude access goes through a Claude account proxy on the
same host. The agent's name is `name` in `agent.toml`; it is the agent's display name and the Linear label it
picks up. In Slack it serves the people listed in `agent.toml` under `[people]`; anyone else gets a
refusal.

## Components

```text
 Slack (Socket Mode)      Linear (webhook, poller)                          GitHub (PR stats, the agent's PRs)
        |                         |                                                ^
        v                         v                                                |
 +-- control  (agent-control: agentd, 172.30.0.10) --------------------------------+---+
 |  intake -> work queue (agent.db) -> one turn at a time per conversation; tools;      |
 |  schedules; every stack credential   :8085 Linear (Funnel)  :8086 dashboard  :8087 meter
 +----+---------------------------^----------------------------------^------------------+
      | starts, stops and reuses  | tools and output, through each   | the apps' model calls
      | containers (Docker socket)| container's holder               |
      v                           |                                  |
 +-- one container per conversation, from the agent image -----------+------------------+
 |  agent-holder -> Claude Code     /var/agent: sessions, memory, Claude home, caches     |
 |  the agent's own gh, az and Slack logins and keys, the internet                       |
 +-- these, agent-services and the apps share agent-docker's network -------------------+
 |  docker (agent-docker, dind): the apps sessions run    services (agent-services)      |
 +-------+------------------------------------------------------------------------------+
         | Claude API, placeholder token
         v
 relay (host network, 172.30.0.1:28090/28091) --> account proxy (host, 127.0.0.1:18090/18091) --> Claude
 supervisor + supervisor-standby: poll the control plane and each other every minute; restart and alert
```

The stack network `agentnet` is 172.30.0.0/24 with the host gateway at 172.30.0.1. The control plane has the fixed
address 172.30.0.10.

| Listener | Where | Reached by |
|---|---|---|
| control :8080 | stack network only | the supervisors: `/health` |
| supervisor :8090 | stack network only | the other supervisor: `/health` |
| control :8085 | host 127.0.0.1:18085 | Linear, through Tailscale Funnel: `/linear/webhook`, `/linear/oauth/callback` |
| control :8086 | host 127.0.0.1:18086 | people on the tailnet, through `tailscale serve` on :9443 |
| control :8087 | stack network only | apps under test, at `http://172.30.0.10:8087/llm/<upstream>` |
| a holder's socket | `/var/agent/run/containers/<conversation>/holder.sock` | the control plane only; each container sees only its own run folder |
| dockerd :2375 | 127.0.0.1 in the docker service's namespace | sessions and the agent's services (`DOCKER_HOST`) |
| relay :28090 | 172.30.0.1 | the control plane, to the proxy origin `agent-core` (usage, session pins, task labels) |
| relay :28091 | 172.30.0.1 | Claude Code sessions, to the proxy origin `agent-sandbox` (model traffic) |

### The control plane

`agentd serve` (Rust) in the `control` service, container `agent-control`, from the agent image. It runs as the
agent user (uid 1001) with the Docker socket's group. It holds every credential of the stack
(`data/secrets/core.env`, read into memory, never into the environment or a container) and never runs Claude. It:

- receives Slack events over Socket Mode (an outbound connection) and Linear webhooks and polls;
- keeps all state in one SQLite database, `data/db/agent.db` (WAL), plus its own tables for the containers, the
  turns it is running and their tool calls;
- runs each conversation's turns in that conversation's container, one at a time, and the tools they call;
- serves the public Linear listener, the dashboard and the model-call meter;
- runs background loops: Linear intake, schedules, usage watch, parks and the container sweep (1 min), Linear app
  token refresh (10 min), task labels, the KPI digest check, PR stats, the agent's PRs and Linear issue states
  (5 min), Azure's billed prices (30 min) and the Host page's samples (5 s).

At start it copies each project's app env file to `data/runner/projects/`, writes the timezone to
`data/runner/timezone`, reconnects to every turn still running in its container, and puts back in the queue any
work item marked running that has no turn. Restarting it interrupts no turn: they run in their containers, and it
reads what they wrote while it was gone.

`agentd` also answers the admin commands: `doctor`, `smoke`, `kpis`, `stop`, `resume`, `schedules` (the agent runs
it after editing the schedules file) and `ask` (a turn from the host without Slack).

### A container per conversation

Each conversation (a Slack thread or DM, a Linear issue, a schedule run, a spawned agent) gets its own container,
`agent-conv-<conversation>`, from the agent image, the first time it has a turn. Every one shares `agent-docker`'s
network, so the apps it starts are on its localhost and the shared demo ports reach it. It runs as user `agent`
(uid 1001) with passwordless sudo, under the memory, CPU and process caps in `[containers]`. It mounts the
`agentwork` volume at `/var/agent` (with only its own run folder), `data/runner` read-only at `/run/agent`, the
Azure login, the schedules file and the task record (read-only). The image has Python 3.12, Node 22, bun, just,
git, gh, az, the Docker CLI, Playwright's CLI with Chromium, build tools and CJK and emoji fonts.

A container is stopped after an hour with nothing running and nothing listening in it, and 24 hours after its last
turn at the latest; it stays stopped for 7 days and is started again for the same conversation, then deleted. When
there isn't memory for another container, idle ones are stopped early.

People can change when things stop (`agentd_keeps`): a container, one server in it (by port), a service, or a stack
in the agent's own Docker can be kept for some hours or until changed, from the dashboard's Containers page or by
asking the agent (its `keep_running` tool). A kept container isn't stopped by the rules above, nor for memory, and is
stopped when its expiry passes. A kept server holds its container up; once the container is past its usual stop,
everything else in it is stopped. An expired service is taken out of `services.toml`, an expired stack is stopped.

Its main process is `agent-holder`. For each turn the control plane asks it to start Claude Code (in stream-json
mode) with the turn's arguments and environment; the holder writes every line Claude Code prints to a numbered log
(`run/containers/<conversation>/<work item>.jsonl`), passes the control plane's messages in, and relays the
session's tool calls: Claude Code sees the tools as an MCP server (`agent-holder mcp`) whose calls go through the
holder's socket to the control plane, so no key for them enters the container. The waiting hook that keeps a turn
open while helpers run is `agent-holder hook idle-wait`.

Everything lives under `/var/agent`, the `agentwork` volume, which survives rebuilds:

| Path | Contents |
|---|---|
| `scratch/<conversation>/` | the conversation's folder, where its turns start; its checkouts and notes live here |
| `claude/` | the agent's Claude home (`CLAUDE_CONFIG_DIR`, also `~/.claude`), shared by every session: its sessions, so the next turn can resume, and the skills, plugins and MCP servers it installs itself |
| `memory/` | Claude Code's auto memory, shared by every session |
| `transcripts/<work item>.jsonl` | each turn's transcript, as the control plane recorded it |
| `bundles/<version>/` | the core plugin, the effortless plugin and every pack's plugin |
| `home/` | the agent's home folder (`/home/agent` links here): what it installs with `pip --user`, `uv tool`, `npm -g` or `bun add -g`, and its dotfiles; a fresh volume gets one from `/etc/skel` with the `.claude` link |
| `ms-playwright/` | the Playwright browsers (`/opt/ms-playwright` links here): the image's, copied in when missing, and any the agent installs |
| `services.toml` | the agent's long-running services, which `agent-services` keeps going (below) |
| `run/<name>.{lock,pid,out}` | each service's lock, pid and output (`.out.1` is the rotated one) |
| `run/containers/<conversation>/` | that conversation's holder socket and its turns' logs |

A container is made from the image, so what the agent puts outside the volume (a `sudo apt` install, `/tmp`) is gone
when its container is replaced. The schedules file is outside the volume, at `/data/schedules/schedules.toml` (the
host's `data/schedules/`).

Sessions run with permission mode `bypassPermissions`. Each gets the system prompt and persona, the plugins, the MCP
servers from `[mcp]` (with `${VAR}` filled from the session's environment; a server with an unset variable is left
out), and the control plane's tools through the holder. The session's environment is the container's (which holds
no keys) plus the keys from `agent.env`, a git identity (`AGENT_NAME`, `AGENT_GIT_EMAIL`), `LINEAR_APP_TOKEN` from
`data/runner/linear.env`, the project's test keys, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
(the effortless plugin, which picks each prompt's reasoning effort, needs it). The last two cannot be overridden.

A message for a conversation whose turn is running goes into that session at once, and is ticked once the session
has read it. A turn ends when Claude Code's result arrives after the prompt and every delivered message has been
read, with no helper agent and no shell it moved to the background still running.

A Claude session is never resumed on a different account from the one it ran on. The control plane keeps the
account each conversation's session last ran on (`conversations.session_account`) and, before resuming, asks the
proxy's `/proxy/whoami` which account the session is pinned to. It resumes only when that is the same account and
the account isn't rejected or disabled. Otherwise (another account, an expired pin, an unknown last account or an
unreachable proxy) it starts a fresh session with a handoff note after the prompt's who, where and link line. The
earlier session's full history is never loaded into the fresh one. The note carries a short digest of it
(`rust/crates/agentd/src/handoff.rs`): the earlier session id, the text of its last 3 assistant messages (4,000
characters in all at most) and the files it edited or wrote. It gives the record's path for looking up one specific
detail, and says not to read or load it whole and not to use `/resume` or `--resume`. The session catches up with
`read_thread` and from the conversation folder's own state, then carries on. When the account of a running session
is rejected or disabled, or goes over its 5-hour soft cap, the control plane moves the session the same way: at the
next moment no tool call is running, or after 30 minutes at the latest, and only when the proxy's top-ranked
account (where a fresh session lands) is another account under its own cap. It does this up to twice per turn. If
Claude Code crashes, is killed for memory or loses its container, the turn resumes once from the saved session;
if that fails too, the turn fails with what it had.

### The agent's services

The agent's long-running helpers (a bridge, a poller, a watcher) run in the `services` service (container
`agent-services`, `python -m agent services`, from the agent image, on `agent-docker`'s network), not with `nohup`,
so they survive restarts and redeploys. The agent lists them in `/var/agent/services.toml`:

```toml
[[services]]
name = "outlook-bridge"                     # [A-Za-z0-9._-]+
cmd = ["python3", "-I", "bridge.py"]         # the argv; no shell
cwd = "/var/agent/bridges/outlook-bridge"    # an existing absolute directory
```

The process (`agent/runner/services.py`) reads the file every 30 s and reconciles: it starts new services, stops
removed ones (SIGTERM to the service's process group, SIGKILL after 10 s) and restarts one whose `cmd` or `cwd`
changed. A service that exits is started again after 5 s, doubling up to 5 minutes, and back to 5 s once a run has
lasted 10 minutes. A missing file means no services. A file that can't be read or parsed keeps the services it
listed last (none right after the process starts), and the problem is logged once to its log and once into each
running service's `.out`; an invalid entry is left out. A service gets the `agent.env` keys and the agent's git
identity, and its own process group. While it runs a service it holds an exclusive `flock` on `run/<name>.lock`, so
a second copy can't start, and keeps its pid in `run/<name>.pid`. Before a service's first start, any process with
exactly the same argv and working directory is stopped, which takes over a copy the agent started by hand; a process
in the group of a service it started is never taken for one. A service's stdout and stderr go to `run/<name>.out`
with the process's own start, exit and stop lines, rotated to `.out.1` at 5 MB; its log has each start, exit code
and restart, never the arguments or environment. Every one of these files is opened without blocking and must be a
regular file. On SIGTERM every service is stopped before the process exits (`stop_grace_period: 30s`). As it starts
it also prepares `home/` and `ms-playwright/` on the volume (`agent.services.home`).

### docker (dind)

`docker:29-dind`, privileged, container `agent-docker`. It is the agent's own Docker daemon: the apps a session
runs to test its work start here, never on the host's Docker. Its API listens only on 127.0.0.1:2375, and every
conversation's container and `agent-services` share its network namespace, so an app published on 127.0.0.1:<port>
is on their localhost. The `agentwork` volume is mounted at the same path, so compose bind mounts of a checkout
resolve. Images and volumes live in the `agentdocker` volume. Nothing stops the apps a session leaves running here.

### supervisor and supervisor-standby

`python -m agent supervisor`, run twice: containers `agent-supervisor` (`SUPERVISOR_ROLE=primary`) and
`agent-supervisor-standby` (`standby`). They watch the control plane and each other. Every 60 s each checks the
control plane's `/health` and its peer's `/health` (`:8090`, stack network only). After three failures in a row, a
supervisor alerts and restarts the target through the Docker socket (`agent-control`, or the peer's container), and
alerts again when the target is back. A target that is down again within 30 minutes of that restart gets an alert
and no restart, so nothing is restarted in a loop. Each restarts its peer. Only the primary acts on the control
plane; the standby leaves it alone while the primary is up and takes it over once the primary is down, so nothing
is restarted twice and the same alert is never sent twice. If both supervisors die together, compose's
`restart: unless-stopped` brings them back.

A supervisor's own `/health` answers 503 once its watch loop hasn't finished a tick for three minutes, so a
supervisor whose loop has hung counts as down, not just one whose process is gone. A tick that raises ends the
process and Docker restarts it. Alerts go to the Slack incoming webhook `SLACK_ALERT_WEBHOOK`
(`data/secrets/supervisor.env`, shared by both), prefixed with the role, or to the supervisor's own log when that
isn't set. The supervisors and the control plane are the only services with the host's Docker socket.

### relay

An Alpine container with socat on the host network. It listens only on the stack's gateway address, 172.30.0.1,
and forwards 28090 to the proxy's 127.0.0.1:18090 and 28091 to 127.0.0.1:18091. The proxy is never on the LAN.

### The Claude account proxy

A separate program on the host, not part of this repository. Place it at `claude_proxy/claude-fallback-proxy.py`
in the checkout (ignored by git) before running `just proxy-install`, which installs a systemd user service
(`deploy/claude-fallback-proxy.service`) that runs it from there. It spreads sessions across the team's Claude
accounts and reports their usage: it holds the accounts, pins each session to one account and replaces the
placeholder token that sessions send. Each origin has its own port; `deploy/proxy-origins.example.toml` lists the
two this stack uses. The control plane (origin `agent-core`) reads `/proxy/usage` for the queue's gate and for
moving sessions between accounts, `/proxy/whoami` for where a saved session would resume, `/proxy/perf` for the
dashboard, and asks Haiku for task labels.

### The dashboard

A React app built into the agent image and served by the control plane on :8086. See [KPIs, the activity log and
the dashboard](#kpis-the-activity-log-and-the-dashboard). Its Containers page shows everything that runs: each
conversation's container with its processes, servers, memory and logs, the services, the stacks in the agent's own
Docker (read through `docker exec agent-docker`) and the stack's own containers. From it people follow any of their
logs live, set expiries, stop, start or delete them, and admins open a shell (which holds all the agent's
credentials). Every action is in the activity log as `workload_action`.

## From a message to a session

### Slack intake

The control plane handles a Slack message when it is a DM to the agent, mentions the agent, or is a reply in a thread the control plane knows
(one with a conversation, or one under a message the control plane received or posted). It ignores bots, edits and every
subtype except `file_share` (a message with files attached), and handles each message once. A message's files ride
in its turn's payload (`files`: id, name, mimetype, size); before the session sees the message, the control plane
downloads each one with its bot token (`files:read`) into the conversation's folder under `slack-files/` and adds
their paths to the prompt. A sender who isn't in `[people]` gets an ephemeral refusal in DMs and on
mentions. Each accepted message gets an `eyes` reaction.

Routing picks the message's conversation (one row in `conversations`, one Claude Code session):

- **DM, top level**: starts its own thread and conversation, like a mention in a channel. The reply goes in the thread
  under the message.
- **Thread** (in a channel or a DM): a conversation waiting for an answer in that thread (it called `ask`) takes the
  message first; otherwise the thread's primary conversation; otherwise the conversation of the message the thread
  hangs off. Every message the control plane receives or posts is recorded in `slack_messages`, so a thread under any of them
  finds its way back. The reply goes to the thread the message came from.
- **New**: a mention outside any known thread starts a conversation for that thread. In a channel that is a pack's
  `slack_channel`, the conversation is bound to that project from the start.

A message saying "use <model>" at the start of a sentence (after a "please" or "can you" at most) switches its
conversation's model for the rest of the conversation; otherwise sessions use `[claude.models] default`. The model
is a full id (`use claude-opus-4-6`), a family and version (`use opus 4.6`, `use haiku 4.5`), or a family alone when
one allowed model has it; "use default" (at the end of a sentence) goes back to `default`, and "use fable" anywhere
in a message is `[claude.models] hard`, as it always was. Only models in
`[claude.models]` (`default`, `hard` and `allowed`) count: any other model-shaped name, or one that fits more than one
allowed model, changes nothing, and the control plane replies in the thread with the models it can switch to. The message's own
turn runs on the new model; a turn already running keeps its model, so a message it takes in mid-turn applies from
the conversation's next turn. `doctor` checks that the account proxy serves every one of these models.

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

Every turn is a `turn` work item in one queue (`work_items`). The control plane starts the oldest claimable item:
queued, and its conversation has no other item running. So a conversation runs one turn at a time. A message sent
while a turn is running goes into that turn's session at once; one that arrives as the turn ends becomes the next
turn of the same session.

Before starting one, the control plane checks four things:

- the agent isn't paused (stop);
- the account proxy reports capacity on at least one account;
- fewer than `[limits] max_concurrent_sessions` items are running (default 10);
- the host has free memory for another container's cap plus `[containers] min_free_gib` (idle containers are
  stopped first to make room).

While the usage watch holds scheduled runs (below), turns whose `source` is `schedule` wait; people's requests still
start.

### Usage watch

Each account has a soft cap on its 5-hour window (`proxy.SoftCap`), so no account is run to its Anthropic limit
early in a window. It counts as full at `SOFT_CAP_5H` (default 0.65) of the window until `SOFT_CAP_LIFT_S` (default
5400, 1h30m) before the window resets. From then on the cap is 100%, since what is left would go unused at the reset
anyway. The reset time is the proxy's 5-hour window data; when it isn't known, the 65% cap applies. Both values are
environment variables of the control plane (`compose.yaml`, taken from the host's `.env`).

Every 60 s the control plane reads `/proxy/usage` (`rust/crates/agentd/src/usage_watch.rs`) and gives each account a state:

- `out`: the proxy gives it no rank (no capacity);
- `hot`: its 5-hour window is at its effective cap (the soft cap, or 100% once lifted), or its `pace_delta` is above
  0.05 (ahead of its 7-day pace, so it will run out before the window resets). It is `ok` again only once it is 10
  points below its soft cap and back on pace, so it doesn't flap; once the cap is lifted there is no margin;
- `ok`: otherwise.

The pool, counted in `ok` accounts: with two or more (or all the verified accounts, in a smaller pool) nothing
changes. With fewer, scheduled runs (and their carry-ons) wait in the queue. With none, the admins get one DM
with the earliest reset, and another when capacity is back. With every account over its soft cap, people's requests
still start (the gate only needs an account with a rank) and scheduled runs wait. The proxy ranks accounts for new
sessions and isn't in this repository, so the soft cap doesn't steer new sessions: one can still land on a capped
account while the proxy ranks it first, and the control plane then moves it if another account is under its cap. An account whose `verified` goes false, or that has been `out` for over
5 hours, is reported to the admins once. Every change of state is an event (`usage_account`, `usage_pool`,
`usage_unverified`, `usage_out_too_long`, and `usage_cap_reached` / `usage_cap_lifted` when an account reaches its
soft cap or the cap lifts to 100%), shown on the dashboard's Events page. The Accounts page shows each account's
effective cap. The last state is kept in
`settings`, so a restart neither repeats an alert nor misses one.

A turn cut off by a usage limit (a 429, or the control plane giving up on an account the proxy reports rejected or
disabled) doesn't fail. The thread is told it was cut off, the message keeps its ⏳, the session and the account it
ran on are kept and the turn goes into `limit_waits` (event `usage_limit_cut`). Once an account is `ok`, the watch
re-queues it as a new turn with a "you were cut off by a usage limit" note (event `usage_limit_resumed`). That turn
goes through the same-account check as any next turn: the control plane resumes the session only if the proxy still pins
it to the account it last ran on. Usually it doesn't, since the limit was hit on that account, so the turn starts a
fresh session with the digest handoff note instead (never `/resume` on another account). That happens once: a resumed turn that is cut off again fails like any other. A long project's build keeps its own
retries (below) instead, and the gate holds them until there is capacity.

A session can also park itself (`rust/crates/agentd/src/parking.rs`) instead of keeping its turn alive to wait. The agent's `park`
tool takes an optional `until` (HH:MM in the team's timezone, or an ISO date-time) and a `note`. Without `until`,
the park waits out the whole account pool, since the turn that carries it on runs on whichever account the proxy
gives it: it refuses while any account the proxy ranks is under its cap, and otherwise lasts until the first one
has room again (the earliest soft-cap lift, or the reset of an account with no capacity). The control plane records the park in
`parks` (event `parked`) and tells the agent when it carries on; it posts nothing in the thread itself. The agent
then ends its turn. Every minute the control plane carries on each park that is due, as a new turn in the same conversation with
the agent's note (event `park_resumed`); one that fails stays open for the next minute without holding up the
others. That turn goes through the same-account check as any next turn, and it neither reacts to the parking turn's
message nor counts as a continuation. A person's message in the thread ends the park at once (`ended_how =
replied`, event `park_ended`) and is the next turn itself. The `status` tool shows the conversation's account: how
much of its 5-hour window is used, its effective cap, when the window resets and when the cap lifts, and how many
accounts are under their cap.

A running item stays the control plane's until its turn ends; after a restart the control plane reconnects to the
turn in its container. An item marked running with no turn behind it goes back in the queue, and is marked failed on
its fifth try. Cancelling an item (`cancel_task`, a stop, the dashboard) interrupts its session within seconds. A
turn has no time or step limit: it runs until the session is done. A session that sends nothing for
`[limits] idle_minutes` (default 30) is stuck, and the control plane stops it as `stalled`. A
scheduled run, which no one is watching, also has a time budget of `schedule_hours` (default 6) for the whole run.
A turn whose session has background agents running (the Agent tool with `run_in_background`, or a workflow) stays
open past its result until they finish: each one wakes the session, messages sent meanwhile are read at once, and
the reply posts after the last one. Their progress counts against `idle_minutes`, and a stop then ends the turn at
once. A foreground Bash call that only waits (a sleep over 10 s, `tail -f /dev/null`) is turned away by a hook
(`agent-holder hook idle-wait`), since no message reaches the session while it runs.

When the control plane starts a turn, it refreshes its payload from the conversation as it is at that moment: the
session to resume, the project, the checkout and the test keys. A turn queued behind another one therefore
resumes the session that turn ended with.

### Results

When a turn ends the control plane has its outcome, the session's final text, the session id and token counts. It
records a row in
`runs`, saves the session id on the conversation and posts the text, scrubbed of secrets, in the thread as
Markdown. An active long project's build carries on in a new turn after an error or a stalled turn, up to three
in a row, and the thread gets the error. Otherwise a failed turn (error, stalled, a scheduled run out of time,
interrupted) gets a short failure reply, and its task, if any, becomes `failed`. The conversation keeps its session
after an error or a stall, so the next turn picks it up where it stopped (on the same account, or with a handoff).

### Linear

There are two ways in:

- **Labelled issues.** Every minute the control plane polls each pack's `linear_team` for open issues labelled with the agent's
  name. Each new issue becomes one task. the control plane posts `Linear <ID>: <title>` in the pack's `slack_channel` and starts
  an unattended turn bound to the project in that thread. Task changes set the issue's state through
  `[linear.status]`, and a ready PR is commented on the issue.
- **Linear agent sessions.** With the Linear app installed (`[linear] public_url` and the app's client secrets set),
  people can delegate issues to the agent. Linear's webhook reaches the control plane's public listener through Tailscale
  Funnel; the control plane checks the signature and timestamp, acknowledges with an "On it." activity and queues the raw event
  as a turn. Follow-ups in the same Linear session resume the same Claude session. The agent reads the issue,
  picks the project, replies and posts activities itself through the Linear MCP server, which runs as the app
  with `LINEAR_APP_TOKEN` (the control plane refreshes it in `data/runner/linear.env`).

An admin installs the app with the `linear_install` tool: the control plane DMs them a single-use link valid for 15 minutes.

### Schedules

Work on a timer. People ask the agent for something on a schedule in plain language ("send me PR stats every
morning at 9", "sweep the codebase every night at 10"); the agent writes it into the schedules file with its own
tools (the `schedules` skill), and the control plane fires it. People can also edit the file by hand on the host:
`data/schedules/schedules.toml`, mounted at `/data/schedules` in the control plane and every conversation's container.

```toml
[nightly-sweep]
cron = "0 22 * * *"           # five-field cron, in agent.toml's timezone
channel = "#project-dev"      # who it's for: a channel name or ID, or a person key for their DM
prompt = "Sweep the codebase and file each real problem as a Linear issue labelled jensen. Skip anything open."
by = "sam"                    # who asked: the turn's actor when it is a person key
link = "https://…"            # where they asked (optional)
paused = false                # optional
```

Every minute the control plane reads the file and, for each schedule whose latest cron slot it hasn't handled yet (`settings`:
`schedule.<name>.last`), queues an unattended turn with the prompt in a new conversation, first line
`[schedule <name> · <cron> · set up by <who> · <link>]`. The control plane posts nothing: like a teammate, the agent messages
people itself when a run has something for them, and the run's final message goes nowhere, as in a Linear session.
A run that fails says so in the channel (`My <name> run didn't finish.` with the error), and replies in that
message's thread continue the run. Each firing is its own conversation, so runs can overlap; the queue's gate
applies as to any turn.

- A schedule seen for the first time (or back after it was removed or invalid), or whose `cron` changed, waits for
  its next slot.
- Slots missed while the control plane was down collapse into one firing when it comes back.
- Slots due while the schedule is `paused` or the agent is stopped are used up without firing.
- A file that isn't valid TOML or can't be read keeps the last valid schedules; an invalid entry (including a cron
  that never matches) is skipped and the rest still fire.
- Errors (the file's, and firings that couldn't be queued) are DMed to each admin
  once per distinct set and logged; a firing that couldn't start is retried every minute.

`python -m agent schedules` (in either container; `just schedules` on the host) prints each schedule with its next
three run times and any errors, and exits non-zero on errors. The agent runs it after every edit. The control plane writes its
timezone to `runner/timezone` so `agentd schedules` in a conversation's container uses the same one.

### Tasks and status in Slack

A task is the record of a piece of work (`tasks`: feature, bug or chore; statuses queued, working, needs_input,
ready_for_review, done, merged, closed, failed, cancelled). Linear issues create one; in Slack, `track_task`,
`start_task` does. Each task has one status message in its thread, edited in place, and a
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

### Checkouts

A session starts in its conversation's folder and clones and checks out a project's code itself, with its own `gh`
login, the way an engineer on the team would. If `data/secrets/projects/<project>.env` exists, its copy is at
`/run/agent/projects/<project>.env`: the app's own `.env` for running it locally. The session keeps the same working
folder on every turn so `--resume` finds it.

## Tools and tiers

The control plane's tools reach the session through its holder, as an MCP server. A session's tier comes from the person whose message
started the turn, never from the model's arguments: `admin` for people with `role = "admin"`, `act` for members and
for unattended Linear work without a mapped requester, `read` otherwise. A tool is offered only at or above its
tier. Every reply is scrubbed of secrets.

| Tier | Tools |
|---|---|
| read | `status`, `list_tasks`, `get_task`, `read_thread`, `slack_read`, `kpis`, `gh_prs`, `gh_checks`, `gh_ci_log`, `gh_issue`, `gh_file` |
| act | `post_update`, `ask`, `react`, `post_files`, `track_task`, `finish_task`, `start_task`, `spawn_agent`, `park`, `cancel_task`, `stop_all`, `start_project`, `finish_project`, `tell_project` |
| admin | `resume_all`, `az_login` (one `<cli>_login` per `[cli.<name>]` with a `login` command), `linear_install` |

- `post_files` reads the files from the conversation's folder on the shared volume and refuses any path outside it.
- `ask` posts the question and marks the conversation as waiting; the person's reply arrives as the next turn.
- `start_task(project, title, kind, brief)` starts a separate session with a self-contained brief, in the same
  thread (or, from a DM, in a new thread under a "Working on <title>" message).
- `spawn_agent(prompt, cwd)` starts a spawned agent: its own conversation, with no Slack thread, whose turns
  don't depend on the calling turn, so it runs on its own after that turn ends and reports back later. It returns
  the agent's id at once. `cwd` is resolved relative to the calling session's folder; it must exist,
  and it stays the agent's working directory on every turn. By default the agent gets its own new folder; a `cwd`
  inside the caller's folder shares that checkout with it, which is the caller's choice. The agent waits out a usage
  limit and can park like any conversation. Once it is finished (its turn ended with nothing queued, parked or
  waiting in its conversation), its outcome and final message are queued in the calling conversation as an
  `<agent-notification>` message, with the calling turn's actor and origin line. If that conversation is idle, the
  message starts a new turn that answers in its thread; if it is running, the message reaches it mid-turn. One that
  fails reports the failure and isn't retried; the caller decides. A spawned agent can't spawn agents of its own, a
  conversation can have at most 3 running at once and all conversations together 4 (a parked one counts). An agent
  whose turn is cancelled (`stop_all`) doesn't report back. `spawned_agents` in `agent.db` records each one
  (`rust/crates/agentd/src/spawned.rs`).
- `read_thread` reads the conversation's thread, or a DM chat's messages since it started.
- `slack_read(method, params)` makes one read-only Slack Web API call with the control plane's bot token. Allowed methods:
  `conversations.history`, `conversations.replies`, `conversations.list`, `conversations.info`, `users.info`,
  `users.list`, `pins.list`, `bookmarks.list`; anything else is refused with that list. The JSON reply is scrubbed
  and cut at 20,000 characters with a note on paging.
- `gh_*` and `linear_*` work on the conversation's project, with the control plane's tokens.
- `az_login` runs `az login --use-device-code` in the control plane as the agent's uid; the code goes to the admin by DM, and
  the login in `data/cli/azure` is shared with the conversations' containers.
- `stop_all` pauses the agent and interrupts every running session; queued messages wait. Only an admin can
  resume.

Sessions also have the MCP servers in `[mcp]` (the example config has Linear's) and their own shell tools: `gh` and
`az` under the agent's own logins, Docker, the browser, and anything they install.

## Memory

Memory is a folder on the `agentwork` volume (`/var/agent/memory`), shared by every session (DMs, channels and
Linear alike) and kept across restarts and rebuilds. The control plane keeps no memory store of its own; the
folder and everything in it are the sessions' doing.

One file per fact, named for what it holds (`people-ivan-replies.md`, `project-roster-demo.md`), each starting
with a short front-matter block (a name, a one-line description of what it is and when it matters) before the
fact itself, with who said it, when and the link. `MEMORY.md` is the folder's index: one line per fact, pointing
at its file (`- [Title](file.md) — hook`). The index holds pointers, never the facts themselves, so it stays
short while the folder grows.

The control plane appends the memory rules and the current index — cut to the first 200 lines, with a note if more
were hidden — to every session's system prompt (`memory_block()` in `rust/crates/agentd/src/prepare.rs`), so every session
can read a fact's file when its line matters to what it's doing, and knows how to keep the folder itself: one
fact per file, look for a file that already covers something before adding a new one, fix or delete a file (and
its line) when a fact turns out wrong. Learned facts never go into CLAUDE.md, which is for how to work the repo,
not what was learned along the way.

Within a conversation, the context is the resumed Claude Code session itself.

## Code and PRs

The agent works on code the way an engineer on the team does, with its own tools and no the control plane step in between. It
clones the repos it needs into the conversation's folder with `gh repo clone` and its own `GH_TOKEN` (`agent.env`),
sets them up with the project's skills, branches the way each repo names its branches, pushes, and opens PRs to
the default branch with `gh pr create`, following the repo's own conventions (its CLAUDE.md, AGENTS.md,
CONTRIBUTING and PR template). Draft or ready, who reviews and whether the agent merges are up to the repo's rules
and branch protection and what the token allows; the control plane adds no gate.

The control plane watches the result for the KPIs: every 5 minutes it searches GitHub for PRs by the agent's login and keeps
each one current until it is merged or closed, tying it to a Linear issue whose key it mentions. The PR stats poller
records review and merge times, human commits and lines, and reverts within 30 days.

## KPIs, the activity log and the dashboard

KPIs come from what the control plane records: `tasks`, `pr_stats` and the `session_started` event appended when a
turn starts.

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
- **Host**: what the machine is doing (CPU, memory, network, disk I/O, disk space, the containers and listening
  ports), over the last hour and broken down by consumer: each conversation's container (linked to its running
  turn), the control plane, the services container, the apps' Docker (or each app container, when the control plane
  can reach that Docker's API), and the rest of the machine. The control plane reads `/proc` and the Docker API
  every 5 s and keeps the hour in memory (`rust/crates/agentd/src/host.rs`); listening ports come from the
  conversations' holders.

The kill switch on the dashboard stops the agent; resuming, like changing the meter's caps, needs an admin whose
`tailscale` login is set in `[people]`.

## The model-call meter

Apps the agent runs and tests may call a model provider (Azure AI Foundry for the example pack). They call it
through the control plane instead: their OpenAI-compatible base URL is `http://172.30.0.10:8087/llm/<upstream>` with any
placeholder key. the control plane:

- adds the upstream's real key from `core.env` (`api-key` header, or `Authorization: Bearer`);
- forwards the call, streaming included (it asks streamed chat completions to report usage);
- reads Foundry's own token usage from each answer (input, cached input, cache writes, output), prices it, and
  records it in `llm_usage`;
- refuses calls with 429 `cost_cap_reached` once the day's or the month's cap is spent (days and months in the
  config's timezone), and with 503 while the agent is stopped.

The price is what Azure bills on the upstream's own resource. Every 6 hours the control plane reads the resource's billing
meters from Cost Management with the `[cli.azure]` login, and retries a failed read after 30 minutes. Each meter's cost over its quantity for the last 30 days
gives a rate for input, cached input, cache writes and output per model, with the discount applied. Defender for
AI's per-token charge is added on all but cached input. the control plane then re-prices the month's calls. Azure records
nothing per caller on a shared resource, so the agent's share is its own calls' token counts at those rates.
`[llm_meter.prices]` (USD per million tokens for input, cached input and output; `default` for unlisted models)
applies until a read works, and to models Azure hasn't billed on the resource yet. The dashboard shows which
applies to each endpoint.

Caps start from `daily_usd` and `monthly_usd` and can be changed at runtime from the dashboard. The meter runs only
when `[llm_meter.upstreams]` has an entry. The address is fixed because apps in the agent's Docker can't resolve
the name `control`.

## Configuration

Host data lives under `${AGENT_DATA:-./data}`.

| Path | Contents |
|---|---|
| `config/agent.toml` | the install's config (below) |
| `config/persona.toml` | optional: replaces the shipped persona (`examples/config/persona.toml` is a copy) |
| `config/agent/system.md` | optional: replaces the shipped system prompt; `{name}` is filled in |
| `config/packs/<project>/` | one pack per project |
| `config/account-names.toml` | written by `just account-names`: proxy code name to account holder |
| `secrets/core.env` | the control plane's credentials (below) |
| `secrets/supervisor.env` | `SLACK_ALERT_WEBHOOK` (optional) |
| `secrets/agent.env` | every session's keys: `GH_TOKEN` for its own `gh` login, its dev and test keys |
| `secrets/projects/<project>.env` | a project's app `.env` for local runs; the control plane copies it at start |
| `runner/` | written by the control plane for the conversations' containers: `linear.env`, `projects/`, `timezone` |
| `schedules/schedules.toml` | the schedules; the agent edits it, the control plane fires it |
| `db/agent.db` | the control plane's state |
| `runs/` | each turn's session messages, for the dashboard |
| `git/` | the bare mirrors behind the git endpoint |
| `cli/azure/` | the Azure CLI login, shared with the conversations' containers |

Secrets files are `KEY=value` lines, mode 0600, and each service mounts only its own. `core.env` needs
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `LINEAR_API_KEY`, `GITHUB_READ_TOKEN` and `GITHUB_WRITE_TOKEN`; with the
Linear app also `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` and `LINEAR_WEBHOOK_SECRET`; plus each meter upstream's
`key` and the values the packs list under `[secrets] worker`.

`agent.toml` (see `examples/config/agent.toml`):

| Key | Meaning |
|---|---|
| `name`, `github`, `timezone` | the agent's name (display name, Linear label), its GitHub login, the timezone for digests, caps and schedules |
| `[people.<key>]` | `slack` user id and `linear` email (required), `github` login, `role` (`member` or `admin`), `tailscale` login |
| `[limits]` | `max_concurrent_sessions` (10), `max_tasks_per_day` (20), `idle_minutes` (30): a turn with no progress for this long is stopped, `schedule_hours` (6): a scheduled run's time budget, 0 for none |
| `[claude.models]` | `default` for every session, `hard` after "use fable", `allowed`: more model ids "use <model>" may switch a conversation to |
| `[agent]` | `uid` of the agent's user in its containers (1001) |
| `[containers]` | each conversation's container: `image` (`autonomous-agent-agent`), `memory_gib` (8), `cpus` (4), `pids` (4096), `idle_stop_minutes` (60), `max_age_hours` (24), `keep_stopped_days` (7), `min_free_gib` (3): the free memory a new container needs beyond its cap |
| `[linear]` | `public_url`: the Funnel URL for Linear's webhook and OAuth callback; empty means no Linear app |
| `[mcp.<name>]` | MCP servers every session gets: `type = "http"`, `url`, `headers` with `${VAR}` from the session's environment |
| `[dashboard]` | `port` (8086), `trusted_peers` (`["172.30.0.1"]`), `agent_origins` (proxy origins that are the agent) |
| `[llm_meter]` | `port` (8087), `daily_usd`, `monthly_usd`, `[llm_meter.upstreams.<name>]` (`base_url`, `key`, `header`), `[llm_meter.prices]` |
| `[observability]` | `digest_channel` (empty: no digest), `digest_time` |
| `[cli.<name>]` | a CLI the control plane logs in with a device code: `command`, `env`, `login`, `login_check` |

The control plane reads `agent.toml`, the packs' `project.toml`, the persona and the project env files at start: restart the control plane
after changing them. Pack plugins go out in the bundle, which is rebuilt for every session.

Compose also reads `AGENT_NAME` and `AGENT_GIT_EMAIL` from the environment (or a `.env` file beside
`compose.yaml`, kept out of git) for the git identity of the agent's commits. Use the agent's name and its GitHub
account's noreply address.

## improve-jensen

improve-jensen is the agent that owns this repository and keeps making the agent (Jensen, in this team's install) a
better engineer. It runs outside the stack, as a Claude Code GitHub Action (`.github/workflows/improve-jensen.yml`)
on two self-hosted runners on the host, below Jensen on CPU and disk (one for the hourly watch, one for everything else), and reaches Claude
through the proxy's `improve-jensen` origin. A bad deploy can break the agent, but not the agent that has to undo it.
Nobody tells it what to improve: it works that out from the evidence, and a person approves each change in Slack
before it ships. Its clock is a listener on the host (`jensen_watch.py listen`, a systemd service), since GitHub's
cron never fires the workflow: it starts `watch`, `assess` and `approvals` as `repository_dispatch` events. The
workflow has seven jobs:

- `watch`, hourly: `scripts/jensen_watch.py measure` turns the hour's turns, transcripts and the agent's PRs into
  signals and a digest, which also lists what the agent added (installs, and the skills, memory, instructions and
  settings it wrote) and the skills it used. Claude records each problem in a `jensen-watch` issue and plans the
  reviews and up to five changes to propose, which a step after it posts in Slack with `jensen_watch.py propose`
  (`.github/improve-jensen-watch.md`).
- `review`, one per new PR of the agent's, in any repo: reads its CI, reviews the diff for defects it can prove,
  hands them to the agent in the conversation that made the PR (`scripts/ask_jensen.py`) and records the gap in the
  agent's process (`.github/improve-jensen-review.md`).
- `assess`, every hour when there's new conversation since the last one (a turn of the agent's or a note from it):
  ranks at most ten things that hold the agent back, against the goal (enterprise-grade
  products, little human help, long runs) and only on evidence from the agent's own work. It writes `assessment.md`
  on the host and the pinned `jensen-assessment` issue in general terms, files issues and proposes up to three of
  the top items, projects included, the same way (`.github/improve-jensen-assess.md`). It also asks what worked
  that the agent should keep (ways people drove it that worked, skills or plugins installed for one project,
  instructions people repeat), files those as `learned` issues and proposes up to two, credited in Slack only to
  whose session they came from. Once approved, a fix makes them part of the agent's setup in every project.
- `approvals`, every 10 minutes and at once after a decision in Slack, with no Claude: applies the decisions from the
  Slack threads, labels each issue `approved` or `rejected`, and names the next approved fix and the next approved project (none while the control plane's usage
  watch holds the pool). Each fix or project run first checks that its issue is still approved.
- `fix`, one at a time: ships one approved change through a PR that closes its issue, CI, a fresh review, the merge,
  `just deploy`, its health check and a revert if the check fails (`.github/improve-jensen.md`).
- `project`, one at a time: builds an approved project-scale improvement over runs of up to six hours, with a design
  note on its issue, a progress file on its `improve-jensen/project-<n>` branch and the same bar as a fix, stage by
  stage (`.github/improve-jensen-project.md`).
- `respond`: answers `improve-jensen` issues and `@improve-jensen` mentions under the same rules. A person's own
  request needs no approval in Slack.

Proposals go to `#autonomous-agent` (or the channel the repo variable `IMPROVE_JENSEN_SLACK_CHANNEL` names) through
the agent's Slack bot, as one short numbered list of what will change, and each issue gets `proposed`. A person in
`[people]` answers in the thread in plain language. The listener puts 👀 on the reply at once, has Claude (through the
Claude Code CLI and the proxy) read it against the items and their issues, answers in the thread with what it took
and what happens next, and stores the decisions for `approvals` to apply; a condition on an item goes on its issue as
`Note from Slack: <note>`, which the fix follows. If Claude can't be reached, the plain words still work (`approve`,
`approve 1 3`, `reject 2`), as do `:white_check_mark:` (approve all) or `:x:` (reject all) on the list. When a fix or project ends, `jensen_watch.py
fired` tells the thread whether it shipped; a fix that didn't ship, or a project whose runs failed twice in a row, is
marked `stuck` for a person. Nothing measures a change after it ships beyond the deploy's health check.

The agent and improve-jensen talk through `/var/agent/feedback/` on the shared volume. The agent writes a short note
there when something in its setup gets in its way. The watch reads new notes as evidence, records them on issues in
general terms and answers in the note with a `Status:` line. After each deploy that changes how the agent behaves,
improve-jensen adds a plain-words line to `CHANGES.md` there, which the agent reads when its setup seems different.

improve-jensen's state stays on the host, in `~/.local/state/improve-jensen/` (windows, metrics, evidence, deploys,
the plan, the digests, the assessments and the Slack proposals; metrics and digests are kept 30 days).
Conversations, people and products never go into this public repo. Host setup and switch-on:
`deploy/improve-jensen.md`.

## Operations

| Command | What it does |
|---|---|
| `just up` | writes `account-names.toml` (on failure, carries on) and `.env` (the Docker socket's group, the data folder's path), then `docker compose up -d --build` |
| `just down` | stops and removes the stack; data stays under `data/` |
| `just doctor` | `agentd doctor`: config, packs, each required secret, the proxy (verified accounts, capacity, every model), GitHub read access per repo, Linear states per pack, Docker and the agent image. In `agent-services`: `gh auth status` and `az account show` |
| `just smoke` | `agentd smoke`: queues one turn ("reply with pong") with no Slack thread and waits up to 5 minutes for the answer: the control plane, a conversation's container, the proxy and Claude end to end |
| `just deploy` | in the live checkout: fetches `main`, rebuilds only the services the change touches (a `compose.yaml` change pauses and waits for running turns first, since it can recreate the network the conversations share), then checks the stack live. Exit 0 deployed, 1 check failed, 2 checkout not deployable, 3 pending |
| `just dashboard-serve` | `tailscale serve --bg --https=9443 http://127.0.0.1:18086`: the dashboard at `https://<node>.<tailnet>.ts.net:9443`, tailnet only |
| `just logs [service]` | follows the stack's logs, or one service's (`control`, `services`, `docker`, `supervisor`, `supervisor-standby`, `relay`) |
| `just kpis [--days N]` | prints the KPIs |
| `just schedules` | checks the schedules file and prints each schedule's next three run times |
| `just stop`, `just resume` | the kill switch from the host, for when Slack isn't an option |
| `just account-names` | writes the proxy's account holders' names for the dashboard (run on the host) |
| `just proxy-install` | installs the account proxy, placed at `claude_proxy/claude-fallback-proxy.py` first, as a systemd user service (once) |
| `just test`, `just rust-test`, `just dashboard-test` | the Python tests in the dev image; agentd's and the holder's; the dashboard's tests and build |
| `just dashboard-dev` | the dashboard's dev server against a running control plane |

`just doctor` exits non-zero when any check fails. The agent's Azure login comes from an admin: ask the agent in
Slack to run `az_login`, and the device code arrives by DM.

Session transcripts are on the dashboard's session pages and in `data/runs/`. `just logs control` shows intake,
turn, tool and loop errors; `just logs services` shows the agent's services'.

## Security posture

- **The conversations' containers are open.** Sessions run with every permission, passwordless sudo, internet
  access, their own `gh` and `az` logins and a privileged Docker daemon beside them. Nothing in the operating system
  limits what a session does inside its container. The limits are in the control plane's tools and in what the
  container's credentials allow. A container sees only its own holder's socket, so it can't call tools as another
  conversation.
- **What the containers never hold.** The control plane's Slack tokens, `LINEAR_API_KEY` and the Linear app's client
  secret, `GITHUB_READ_TOKEN` and `GITHUB_WRITE_TOKEN`, the meter's upstream keys, the Docker socket, and the Claude
  accounts' credentials (sessions send a placeholder that the host's proxy replaces). A session does have
  `agent.env`, `LINEAR_APP_TOKEN`, its projects' test keys and app env files, and the shared Azure login.
- **Azure is staging only.** The Azure login that `az_login` creates is shared with the agent; it should have access
  to staging resources only. The system prompt also tells sessions never to change production resources or data.
  The code doesn't check this; the account's role assignments do.
- **The agent's reach is its credentials.** It clones, pushes, opens PRs and merges with its own `GH_TOKEN`, and
  works in any other system with the keys in `agent.env`, its Azure login and its Slack bot token. the control plane puts no
  gate on any of it: what a token allows, the agent can do. Scope each credential, and protect branches, to what
  the agent should be able to do.
- **Secrets are scrubbed from what leaves the control plane.** Every tool reply, `post_update` and other messages the control plane's tools
  post, the session's final reply for each turn, the line of text sent with `post_files`, `slack_read` replies and
  the dashboard's transcript view have known secret values from `core.env` (8 characters or more) and token
  patterns (Anthropic, GitHub, Slack, Linear, AWS keys, private key blocks) replaced with `[redacted]`. The files
  that `post_files` sends are not scrubbed. Values in `agent.env` are unknown to the control plane, so only the patterns catch
  them.
- **Each service has its own secrets.** Each service mounts only its own secrets file. The control plane and the
  supervisors read theirs into memory, never into their environment; the control plane also reads `agent.env`, only
  to hand it to each session, and `agent-services` gets it as plain environment variables.
- **People and tiers.** In Slack, only people in `[people]` get answers, and their role sets the tools a session is
  offered: `stop_all` is open to members, `resume_all` to admins only. In Linear, anyone who can label an issue
  for the agent or delegate one to it starts work; without a mapped requester it runs at the `act` tier.
- **The dashboard is tailnet only.** `tailscale serve`, never Funnel. the control plane answers it only from `trusted_peers` (the
  host gateway, where Tailscale's requests arrive), takes the viewer from `Tailscale-User-Login`, and requires an
  `X-Dashboard: 1` header and a same-origin request for every POST. A conversation's container reaches the control
  plane from its own address and is refused. Any tailnet viewer can stop the agent or cancel a session; resuming and changing caps
  need an admin.
- **One public listener.** With the Linear app configured, the control plane's :8085 is public through Tailscale Funnel, with two
  routes: the Linear webhook (HMAC-SHA256 signature over the body, timestamp within a minute) and the OAuth
  callback (single-use state). Nothing else is public; Slack uses an outbound Socket Mode connection.
- **Spend is capped.** The apps' model calls stop at the meter's daily and monthly caps. Claude sessions start only
  when the proxy reports capacity, and at most `max_concurrent_sessions` run at once.
