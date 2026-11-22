# Rust control plane: replace core and the runner

## Intent

Jensen's work in flight should survive everything around it: our deploys, a crash of the code that runs it, an
account reaching its cap, a person messaging mid-run, one session running out of memory. Today two Python services
share that job. Core holds the queue, the Slack and Linear connections, the tools, the dashboard and the meter; the
runner holds every Claude Code process as its own child inside one shared container and keeps checking in with core
over HTTP. When the runner restarts, everything in flight dies with it, and one session's mistakes reach every other
session.

This build replaces both with one Rust program, the control plane (`agentd`), in one cutover. Messages land in Rust.
Each conversation runs in its own Docker container, where a small holder process keeps Claude Code alive whatever
happens to the control plane. Everything people and Jensen see stays the same: the Slack app, the Linear app, the
tools, the dashboard, the meter, the database and the config files.

## Where things stand

Measured on the live install over 2026-11-14 to 11-20 (921 turns):

- **The queue is sound.** 903 turns finished. No message was answered twice, none got stuck.
- **Work in flight is what dies:**
  - An account reaching its 65% soft cap moves the session to another account in the middle of a turn. That
    happened 7 times and killed 19 running helper agents, about 13 agent-hours of work.
  - The runner or core restarted 7 times in 6 days, 4 of them logged deploys. Each restart killed every running
    helper, build and server: about 4 agent-hours of helpers.
  - 38 background helpers were lost when their turn ended. A fix on 11-18 keeps the turn open while they run; the 2
    losses since then were both restarts.
  - Long foreground helper calls kept people's messages from reaching Jensen: 37 hours of blocked main sessions in
    total, the longest wait 45 minutes.
  - In one window, 20 Claude Code processes (7 sessions, 13 helpers) ran on the 8-thread, 32 GB host. Linux killed
    12 processes for memory: 10 browsers spread across sessions, and 2 desktop apps outside Jensen.
- **Weak points in the code:**
  - Core and the runner coordinate through 90-second claims renewed every 15 seconds. When a claim runs out while
    the runner is still alive (a host suspend), core hands the same work out again, and two Claude Code processes can
    end up resuming the same session.
  - A turn whose claim runs out for the fifth time is marked failed without telling anyone.
  - Any error throws the conversation's session away, so the next turn starts with no memory of the conversation.
  - A result that fails to post is not retried; the turn then runs again.
  - One Python process carries every session; if it dies, the container dies, and every session with it.

## Goals

1. Deploying or restarting the control plane costs no work: running turns carry on and it reconnects to them.
2. A Claude Code crash (including Claude Code being killed for memory) resumes the conversation from its saved
   history once, with a note to check what is already done, instead of failing the turn or losing the session.
3. A conversation never has two Claude Code processes at once.
4. People's messages reach Jensen at its next step even while long work runs, and each is confirmed as read.
5. Accounts move only when nothing is running inside the session, except as a last resort (see Rules).
6. One session cannot hurt another: each container has its own memory and CPU cap and its own processes, `/tmp` and
   system files. Claude Code, its helpers, its browsers and its builds all run in its container. Apps Jensen runs in
   its own Docker share one overall cap (see Session containers).
7. New work starts only when the machine has room.
8. Core and the runner are gone. Everything that works today keeps working: Slack, Linear and schedules, every tool,
   the dashboard (every page), the meter and its caps, KPIs and the daily digest, transcripts and token counts,
   improve-jensen's readers, demo servers on the shared ports, Jensen's always-on services, the kill switch,
   `just doctor` and `just smoke`.

## Non-goals

- Changing what people or Jensen see. Same Slack app, Linear app, tool names and arguments, dashboard pages, meter
  URL, config files and database.
- A new memory, credentials or skills service. Memory is 33 facts today and works.
- Credentials per person, per project or per thread. Every container gets all of Jensen's credentials, as every
  session does today.
- New helper-session tools. Jensen's helpers are Claude Code subagents inside its session; `spawn_agent` stays as
  it is.
- More than one machine, network storage, or VM snapshots.
- Keeping Claude Code running between turns. One Claude Code per turn, resumed from saved history, as today.
- Gating merges. Checking finished work belongs to Jensen's skills and each repo's branch protection.
- Rewriting the supervisors, the account proxy or improve-jensen. They stay Python and are pointed at the new
  service where needed.

## Anti-goals

- The control plane deciding Jensen's steps. It runs and records work; Jensen plans it.
- Anything the control plane does before a turn starts failing the turn. A failed preparation step falls back to
  what today's runner does.
- Posting in Slack on Jensen's behalf, other than today's failure replies, reactions and task status messages.

## Approaches considered

- **Fix the Python runner only.** Not taken. The runner's process model is the root cause, and the Rust control
  plane is the direction for the stack.
- **Replace the runner first, then core in stages.** Not taken. Staging keeps two programs coordinating over HTTP
  claims for longer, which is the weak point above. The whole replacement ships as one cutover, with the Python
  stack kept as the rollback on the same database.
- **A durable-execution engine (Temporal or Restate).** Not taken. It gives queues, timers and message delivery,
  which one program with SQLite does on one machine. It doesn't keep a Claude Code process alive, which is the
  actual problem, and it adds a server to run. Cursor and Codex use one at fleet scale.
- **A virtual machine per session (Firecracker, E2B).** Not taken now. Same isolation as a container plus
  snapshots, at several times the work, and it needs hardware virtualisation. Revisit for a fleet.
- **No container per session (holders inside one shared container).** Not taken. It keeps sessions alive through
  restarts but leaves every session exposed to the others (a broad `pkill`, a bad `sudo apt`, a full `/tmp`, memory),
  which today only a prompt rule guards against.
- **Adopting an open-source orchestrator.** Not taken. Of about 25 projects checked, none keeps sessions alive
  across its own restart or fits an existing Slack and Linear setup; most are whole products with their own UI.
- **One Rust control plane, container per conversation, one cutover.** Chosen.

## How this lines up with frontier labs

- **One process on one machine; a central part plus workers across machines.** OpenAI's Symphony and Claude Code's
  background supervisor run as one process; Anthropic's self-hosted runner, Cursor and Codex cloud split a central
  queue from workers that renew time-limited claims. The control plane keeps a clean boundary between its central
  part and the part that runs sessions, so the second can become a worker per machine later.
- **Each session isolated** (Managed Agents, Cursor, Devin, Codex cloud, Copilot, AgentCore).
- **The record of work outside the process** (Managed Agents, Cursor): Claude Code's saved history plus the
  control plane's database.
- **Messages delivered at the agent's next step and confirmed** (Codex, Cursor, the Claude Agent SDK).
- **Account and process changes only between turns** (Claude Code's self-hosted runner).
- **Where it differs:** Claude Code runs inside the container rather than an agent loop outside it, as in
  Anthropic's own Claude Code on the web; keys sit in the container, as today.

## Design

### Overview

```text
 Slack (Socket Mode)   Linear (webhook, polling)   schedules.toml
            \                 |                      /
 agent-control: agentd (Rust, one program, 172.30.0.10)
   Slack connection, Linear connection, schedules      SQLite: data/db/agent.db (same file and tables)
   message routing and queue, conversations            dashboard API :8086 (same React app)
   running sessions, recovery, accounts, capacity      LLM meter :8087 (same URL for apps)
   tools for Jensen, KPIs, digest, maintenance         Linear listener :8085 (same Funnel URLs)
            |  Docker Engine API, per-container Unix socket and output log
 one container per conversation (agent image)          services container (always-on services)
   holder process -> Claude Code (+ its helper subagents) -> tool server
            |
 shared volume /var/agent    Jensen's Docker (dind) network    account proxy (host)    GitHub
```

### What stays exactly the same

The cutover is safe because these don't change, and because the Python stack can be started again on them:

- **The database:** `data/db/agent.db`, its tables and their meaning (`conversations`, `work_items`, `runs`,
  `events`, `tasks`, `task_records`, `slack_messages`, `own_threads`, `linear_issues`, `linear_issue_conversations`,
  `agent_prs`, `pr_stats`, `request_labels`, `llm_usage`, `projects`, `parks`, `limit_waits`, `spawned_agents`,
  `settings`, `slack_events`, `leaves`). New tables may be added for container state; existing columns keep their
  meaning. Columns only the runner protocol needed (claims, the `tokens` table) are no longer written.
- **Config and secrets:** `agent.toml`, packs, persona, `system.md`, `core.env`, `agent.env`, `supervisor.env`, the
  projects' app env files, `account-names.toml`, `schedules.toml`.
- **The Slack app and tokens**, the Linear app, its webhook and OAuth URLs (`/linear/webhook`,
  `/linear/oauth/callback` on :8085 behind Tailscale Funnel).
- **The dashboard:** the same React app, served on :8086, calling the same `/api/*` endpoints with the same JSON
  (`/api/me`, `overview`, `conversations`, `conversations/{id}`, `sessions/{id}`, `sessions/{id}/stream`,
  `sessions/{id}/cancel`, `accounts`, `accounts/history`, `kpis`, `events`, `stop`, `resume`, `llm`, `llm/caps`,
  `host`, `host/consumers`), with the same access rules (trusted peers, `Tailscale-User-Login`, the `X-Dashboard`
  header and same-origin POSTs).
- **The meter:** `http://172.30.0.10:8087/llm/<upstream>` and `/llm`, so the control plane keeps core's fixed
  address on the stack network. Same pricing from Azure billing, same caps.
- **Files other tools read:** `data/runs/<work item>.jsonl` (the dashboard's transcripts),
  `/var/agent/transcripts/<work item>.jsonl`, `data/runner/` (the Linear token, the projects' env files, the
  timezone), in today's record format: the Python SDK's message classes by name and field, plus `RunUsage`.
- **Every tool Jensen calls:** the same names, arguments, tiers (read, act, admin by who sent the message) and
  results, including the dynamic `<cli>_login` tools. Today's list is in `src/agent/core`.
- **Secret scrubbing:** the same rules on everything that leaves (Slack posts, tool results, the dashboard's
  transcript view).

### The control plane (`agentd`)

One Rust program in the container `agent-control`, replacing `agent-core` and `agent-runner`. It mounts the host's
Docker socket, the data directories core mounts today, and the `agentwork` volume at `/var/agent`. Its parts:

- **Slack connection.** The Socket Mode connection with the app token, and the Web API with the bot token. It does
  what core does today: which messages to take (DMs, mentions, replies in known threads, file shares), refusing
  people outside `[people]`, the 👀 ⏳ ✅ reactions, the who/where/link line, name and permalink lookups, posting
  replies as Markdown blocks, task status messages, downloading Slack files for a session, and model switches
  ("use <model>").
- **Linear connection.** The webhook listener with signature and timestamp checks, the OAuth install flow, polling
  labelled issues, agent sessions, the app token refresh into `data/runner/linear.env`, and setting issue states.
- **Schedules.** Reading `schedules.toml` every minute and firing due entries as unattended turns, with today's
  rules for new, changed, paused and missed slots and the admin DMs for errors.
- **Message routing and queue.** Which conversation a message belongs to (DM, thread, `ask` waits, a pack's
  channel), the queue in `work_items`, one running turn per conversation, and the conversation's saved session and
  account. It also covers everything that is a queue entry underneath: parks and their wake-ups, carry-ons after a
  failed build turn, usage-limit waits and their resumes, spawned agents and their reports, messages passed to
  running builds (`tell_project`), and tasks with their Slack and Linear status.
- **Running sessions.** Starting and reusing a container per conversation, preparing each turn the way the runner
  does (the plugin bundle, the conversation's folder, Slack files, project test keys, the memory index added to the
  system prompt, the resume decision with the account proxy, restart and handoff notes), asking the container's
  holder process to start Claude Code, following the turn until it has really ended, delivering new messages into
  it, writing events and transcripts, and posting the result.
- **Recovery.** After the control plane restarts, it reconnects to every running container and carries on. If
  Claude Code crashes, it resumes once from saved history. Details in "When things go wrong".
- **Account usage.** Reading the proxy's usage every minute as core's usage watch does today (account states,
  holding scheduled runs when the pool is low, admin DMs), and moving sessions between accounts by the rules below.
- **Capacity.** Starting a new turn only when fewer than `[limits] max_concurrent_sessions` turns are running and the
  host has enough free memory for another container's cap plus a margin. When a turn is waiting only for memory, it
  stops idle containers (no turn running) early, oldest first: those with nothing running, then those with leftover
  processes, and last those serving on the shared demo ports.
- **Tools for Jensen.** Every tool core offers today, called from Claude Code through the container's tool server.
  The tier comes from who sent the message, never from the call's arguments.
- **Dashboard API.** The endpoints above, read from the database and from the control plane's live state (running
  turns, live transcript streams, host numbers per container from Docker), plus the kill switch and cap changes.
- **LLM meter.** The OpenAI-compatible proxy for the apps Jensen tests: adding the upstream's key, streaming,
  reading token usage, pricing, caps, and refreshing rates from Azure billing every 6 hours with the shared `az`
  login.
- **KPIs and reports.** PR stats and agent PR polling, Linear issue polling, task labels with Haiku through the
  proxy, the daily digest, and the `kpis` numbers.
- **Admin commands.** `agentd doctor`, `agentd smoke`, `agentd schedules` (the check Jensen runs after editing the
  schedules file), `agentd kpis`, `agentd stop` and `agentd resume`, behind the same `just` recipes.

Inside the program, the part that runs sessions talks to the rest through one internal interface (start a turn,
deliver a message, stop, and the events coming back). On one machine that is a function call. If a second machine
is added, that part becomes a worker there and the interface becomes a network protocol, as in Anthropic's
self-hosted runner and Cursor.

### Session containers

One Docker container per conversation (one Slack thread, one Linear issue, one scheduled run, one spawned agent),
created from the agent image:

- **Shared with every container:** the `agentwork` volume at `/var/agent` (Claude home, memory, checkouts, Jensen's
  home folder and caches, holder logs), the mounts the agent service has today (`/run/agent`, the Azure login, the
  schedules file, the task record), and the network of Jensen's Docker daemon, as `agent-runner` joins it today.
  So localhost, the apps Jensen tests and the shared demo ports behave as they do now.
- **Private to each container:** its processes, `/tmp`, system files and anything installed with `sudo apt`, and
  its memory and CPU cap.
- **Credentials:** every container mounts the same read-only credentials folder: the keys in `agent.env`, the
  Linear app token, the projects' app env files and test keys, and the shared Azure login. The holder reads the key
  files at the start of every turn and passes them to Claude Code as environment variables, so a changed key
  reaches the next turn without restarting the container. The control plane's own secrets stay out of every
  container: the Slack app token, the Linear app's client and webhook secrets, the GitHub tokens core uses today,
  and the meter's upstream keys (so apps can't go around the meter's spending caps).
- **Environment:** besides the keys, the same as a session gets today: the git identity, the soft-cap and
  compaction settings, `CLAUDE_CONFIG_DIR`, with secret-looking variables that aren't meant for the session blanked,
  as the runner does.
- **Lifetime:** a container starts with its conversation's first turn and serves every later turn of that
  conversation. It stops once an hour has passed with no turn running and nothing else running in it (no process
  listening on a TCP port, no other process Jensen started). Whatever is still running, it is stopped 24 hours after
  its last turn; anything that must outlive that belongs in Jensen's services. A later message starts a fresh
  container that resumes the conversation from its saved history.
- **Other containers' processes:** a container can't see or stop processes in another. That matches the existing
  rule in Jensen's prompt not to stop processes it didn't start. A port another container holds is taken, and
  Jensen picks another free one.
- **Apps in Jensen's Docker:** compose stacks and databases Jensen runs in its own Docker daemon run outside every
  container, so no container's cap covers them. The `docker` service gets one overall memory cap instead.

**Holder process (`agent-holder`).** A small Rust program built into the agent image and run as each container's
main process under Docker's init. It starts Claude Code for a turn when the control plane asks, keeps Claude Code's
input open, writes every line Claude Code prints, numbered, to a log on the shared volume
(`/var/agent/run/containers/<container>/<work item>.jsonl`), and takes requests on a socket file next to it: start a
turn, send a line, interrupt, stop, status, and "give me the log from line N". It records how Claude Code exited.
It knows nothing about Slack, accounts or the database. Docker owns it, so it keeps running while the control plane
restarts.

**Services container.** One more container from the agent image that runs Jensen's always-on services from
`/var/agent/services.toml` with today's services supervisor (`src/agent/runner/services.py`), unchanged. Services no
longer depend on any session or on the control plane.

### Starting Claude Code without the Python SDK

Today the runner starts Claude Code through the Python Agent SDK, which adds two things in-process: the hook that
turns away waiting-only Bash calls (`runner/idle_wait.py`), and core's tools as an in-process MCP server. Without the
SDK:

- **The waiting hook becomes a command hook** in the settings passed to Claude Code: `agent-holder hook idle-wait`,
  with the same rules.
- **The tools become a stdio MCP server** that Claude Code starts inside the container (`agent-holder mcp`). It
  forwards each call through the holder's socket to the control plane, which runs the tool. The container's socket
  identifies the conversation and its tier; no key or token enters the container.
- **Everything else is command-line flags,** built the way the SDK builds them today: stream-json input and output,
  `--replay-user-messages`, bypass permissions, the model, the system prompt, plugins, MCP servers, setting sources,
  resume and session id.
- **Interrupts** are the stream-json control request the SDK sends today.

### A message, step by step

1. A Slack message arrives over Socket Mode. The control plane decides which conversation it belongs to, reacts 👀
   and records it in `work_items`.
2. **If that conversation has a turn running,** the message goes straight into it through the holder (see
   Messages), and is marked done when Claude Code echoes it back.
3. **Otherwise,** once there is capacity, the control plane starts or reuses the conversation's container,
   prepares the turn and asks the holder to start Claude Code.
4. While the turn runs, the control plane reads the holder's log as lines arrive, writes events and transcripts,
   serves the dashboard's live stream, and runs any tool Claude Code calls.
5. When the turn has ended (the prompt and every delivered message echoed, no background agents or moved shells
   still running, a result received), it closes Claude Code's input, posts the reply in Slack, updates reactions and
   the task, records the run, and keeps the container for the conversation's next turn.

Linear events, schedule firings, park wake-ups and spawned-agent reports enter at step 1 the same way.

### Rules

- **Accounts:** the soft cap stays where it is (65% of an account's 5-hour window, lifting to 100% in the last 90
  minutes before the window resets, as today), because people use the same accounts and need the rest. A session is
  never resumed on a different account from the one it last ran on, so a move always means a fresh Claude Code on
  another account that carries on from today's handoff note. Moves happen:
  - **at once** when the proxy rejects or disables the account;
  - **at the next quiet moment** once the account is over its soft cap and another account is under its own. A
    quiet moment is one with no tool call, shell or helper running: between turns, or inside a turn while Claude
    Code is only thinking or writing. Inside a turn, the control plane interrupts Claude Code at that moment and
    carries the same turn on in a fresh session on another account with the handoff note, as today's runner does
    when it moves a session, so no running work is lost;
  - **anyway, after 30 minutes** over the soft cap with no such point, so a busy session can't hold an account past
    its cap. This is the only case that can still cut off running work;
  - **at the start of a turn** when the conversation's account is already over its soft cap: the turn starts fresh
    on another account with the handoff note.
- **Messages:** people's messages delivered into a running turn carry `priority: "now"` and
  `origin: {"kind": "human"}`, so Claude Code moves running shells and helpers to the background and reads the
  message in the same turn. Messages from the system (a spawned agent's report, a park ending) use the default
  priority. A message counts as read only when Claude Code echoes it back; anything unread when a turn ends runs as
  the conversation's next turn, as today.
- **Work a message moved aside:** when a person's message moves a running shell to the background, the turn waits
  for that shell to finish, as it waits for background agents. Shells Jensen backgrounded itself (a server) don't
  hold the turn.
- **Errors keep the session.** An error no longer clears the conversation's saved session; the next turn tries to
  resume it and falls back to a fresh session with a handoff note if that fails.
- **Image updates:** a new agent image reaches a conversation when its container next starts. Running containers are
  never restarted for an update.

### When things go wrong

| What happens | Today | With the control plane |
|---|---|---|
| Control plane deployed, crashed or restarted | (runner) every running turn dies | Turns keep running. It reads its database for the turns that were running, reconnects to each holder, reads each log from the last line it recorded and carries on. Messages that arrived meanwhile are delivered then |
| Claude Code crashes mid-turn | the turn fails and the session is dropped | The holder reports the exit; the control plane starts Claude Code once more with the saved session and a note to check what's done; a second crash fails the turn and keeps the session |
| A container hits its memory cap | Linux kills processes anywhere on the host | The kernel kills the biggest process inside that container only (often a browser); other conversations are unaffected. Claude Code sees the failed tool and carries on. Only if Claude Code itself was killed does the turn resume once, with a note saying it ran out of memory |
| A holder or container disappears | n/a | Docker reports it; same as a crash |
| Account reaches its soft cap | the turn is interrupted, helpers die | The move waits until nothing is running, for up to 30 minutes |
| Slack connection drops | core reconnects | Same; events Slack retries are recorded once |
| Host reboots | every turn dies and retries | Every turn stops. On start, the control plane resumes each turn that was running from saved history, once, with the restart note |
| Kill switch | every turn interrupted | Same |

A result that fails to post to Slack is retried; the turn is never run again because a post failed.

### Other changes in the repository

- `compose.yaml`: `agent-control` replaces `agent-core` and `agent-runner` and takes core's fixed address
  `172.30.0.10`. The session and services containers are started by the control plane, not by compose. The agent
  image adds `agent-holder` and `agentd` (for `agentd schedules`).
- The supervisors watch `agent-control`'s health instead of core's and the runner's.
- `just deploy`, its live check and improve-jensen's deploy step: a control plane change rebuilds and restarts only
  `agent-control`, with no pause or drain; an agent image change rebuilds the image, and new containers use it.
- improve-jensen's readers that run Python inside `agent-core` today (`src/agent/watch/core_collect.py`,
  `core_ask.py`) read the same database file from the host instead. `scripts/smoke.py` and `scripts/ask_jensen.py`
  queue work through `agentd`.
- The schedules skill runs `agentd schedules`.
- `docs/design.md` and `docs/runner-protocol.md` are rewritten for the new layout; the runner protocol becomes the
  internal interface described above.
- The Python core and runner code is deleted once the rollback window (below) has passed.

### Cutover and rollback

1. Pause new work with the kill switch and let running turns finish, or stop them; their conversations keep their
   saved sessions.
2. Back up `data/db/agent.db`.
3. Stop `agent-core` and `agent-runner`, start `agent-control`, and run `just doctor` and the live checks below.
4. **Rollback:** stop `agent-control` and start the Python services again on the same database. The Python images
   stay built for one week, and the control plane writes nothing the Python stack can't read during that week.

### Testing

- **Matching today's behaviour:** for the parts that are pure logic (routing, delivery rules, schedules, parks,
  carry-ons, account rules, scrubbing, Markdown to Slack blocks, KPIs, dashboard queries, meter pricing), the Python
  tests are the list of cases to cover. Where outputs must match exactly (dashboard JSON, transcript records, tool
  results), recorded outputs from the Python code on a copy of the live database are the expected values.
- **A fake Claude Code** that plays scripted stream-json (echoes, results, background agent frames, crashes), for
  restarts, reconnects, mid-turn messages and account moves without spending tokens.
- **A fake Slack and a fake Linear** for the connections, so routing and posting are tested without the real
  services.
- **Live checks on the host after cutover:**
  - a DM, a channel mention, a thread reply, a Linear issue, a schedule firing, a spawned agent;
  - every dashboard page, the kill switch, a meter call through `172.30.0.10:8087`;
  - restart the control plane during a turn: the turn finishes and the reply posts once;
  - send a message during a long helper call: it is answered within the next step;
  - fill a container's memory: only processes in that container are killed, and the turn carries on or resumes;
  - a demo server on a shared port: reachable, and its container isn't stopped while it serves.

## Proven first (a short spike)

Before the plan is written, confirm on the live host:

1. Many containers can share Jensen's Docker network at once, and the shared demo ports reach them.
2. A holder keeps Claude Code running while the control plane's container is restarted, and the control plane
   reconnects and reads the missed output.
3. Claude Code runs a full turn started by the holder, with the waiting hook as a command hook, the tools through the
   stdio relay, an interrupt, and a "now" message.
4. How long a container takes to start from the agent image, and what it costs in memory beyond Claude Code itself.
5. Whether listening-port detection works from inside a container that shares the network with others.
6. How Claude Code reports a shell that a "now" message moved to the background.
7. The Slack Socket Mode connection with the existing app token from the new service.

The plan's first stage is this spike. If any item fails, the plan stops there and the design comes back for a
decision; the later stages are confirmed against the spike's results.

## Constraints

- Claude Code is the agent runtime, driven only through its documented stream-json interface; the live CLI is
  2.1.321.
- The account proxy pins each session to an account; a session is never resumed on a different account.
- One host, Docker Compose. The host is due to be replaced by a larger one; nothing here depends on the laptop.
- The host has no Rust toolchain: building and testing happen in a Docker dev image, as the Python tests do.
- The repository is public: no keys, host names or people in code, docs or tests.

## Libraries

General-purpose ones only, each for a clear job: `tokio` (runtime), `axum` (the Linear listener, dashboard API and
meter), `reqwest` (Slack Web API, Linear, GitHub, the proxy, Azure billing), `tokio-tungstenite` (the Slack Socket
Mode connection), `rusqlite` (the database), `bollard` (Docker API), `serde_json`, `chrono` with `chrono-tz` and a
cron parser (schedules), `nix` (signals and process groups in the holder), `tracing` (logs). Slack, Linear and
GitHub are called directly over HTTP; we use a few dozen API methods, not enough to need an SDK. Claude Code's
message types and their conversion to today's records are written by hand from the Python SDK's parser. No actor
framework, workflow engine or third-party Claude Code wrapper.

## Decisions made without the team

- One container per conversation, stopped after an hour with nothing running in it, and 24 hours after its last
  turn at the latest.
- The services container runs today's Python services supervisor rather than a Rust rewrite.
- Tools reach Claude Code through a stdio MCP relay in the container, so no key or token enters the container.
- One automatic resume after Claude Code crashes, is killed for memory, or the host reboots, then the turn fails
  with its session kept. A memory kill of any other process is left to Claude Code to handle.
- Account moves happen at the next quiet moment, inside a turn or between turns, so no running work is lost.
- Under memory pressure, idle containers are stopped early, demo servers last.
- A session busy for 30 minutes past its account's soft cap moves anyway.
- Apps in Jensen's Docker share one overall memory cap.
- The database file, its tables, the dashboard API and the record formats stay as they are, so nothing downstream
  changes and the Python stack remains a working rollback for a week.
- Supervisors, the account proxy and improve-jensen stay in Python.

## Left to the builder

- The default memory and CPU cap per container, and the free-memory margin, from the spike's measurements.
- The holder's socket message format and log file format.
- How the control plane's own state (running containers, log positions) is stored, in new tables.
- How the Host page shows containers.
- Cleaning up stopped containers and old holder logs.
- The order the parts are built and tested in.

## Later, not in this build

- A second machine, with the session-running part as a worker there.
- A service for memory, credentials and skills, if memory grows or several agents need their own.
- Keys kept outside the containers, added on the way out; credentials per person or per project.
- The supervisors and improve-jensen in Rust.
