# Rust orchestrator: sessions that survive restarts, each in its own box

## Intent

Jensen's work in flight should survive everything around it: our deploys, a crash of the code that runs it, an
account reaching its cap, a person messaging mid-run, one session running out of memory. Today the part that runs
sessions, the Python runner, holds every Claude Code process as its own child inside one shared container, so when it
restarts, everything in flight dies with it, and one session's mistakes reach every other session.

This build replaces the Python runner with an orchestrator written in Rust. It is the first piece of the Rust
control plane. Each conversation runs in its own Docker container ("box"), where a small holder process keeps Claude
Code alive whatever happens to the orchestrator. Core stays as it is, apart from a few small changes, and keeps
talking to the orchestrator through the existing runner protocol (`docs/runner-protocol.md`).

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
- **Weak points in the code** (`src/agent/runner`, `src/agent/core`):
  - The runner keeps each turn's state under its work item id. When a claim runs out while the runner is still
    alive (a host suspend), core hands the same item out again, and two Claude Code processes can end up resuming
    the same session.
  - A turn whose claim runs out for the fifth time is marked failed without telling anyone.
  - Any error throws the conversation's session away, so the next turn starts with no memory of the conversation.
  - A result that fails to post is not retried; the turn then runs again.
  - One Python process carries every session; if it dies, the container dies, and every session with it.

## Goals

1. Deploying or restarting the orchestrator costs no work: running turns carry on and the orchestrator reconnects
   to them.
2. A Claude Code crash, or a box running out of memory, resumes the conversation from its saved history once, with
   a note to check what is already done, instead of failing the turn or losing the session.
3. A stale copy of a session is refused, so two copies never run the same conversation.
4. People's messages reach Jensen at its next step even while long work runs, and each is confirmed as read.
5. Accounts move only when nothing is running inside the session.
6. One session cannot hurt another: each box has its own memory and CPU cap and its own processes, `/tmp` and
   system files. Claude Code, its helpers, its browsers and its builds all run in its box. Apps Jensen runs in its
   own Docker are the exception (see Boxes).
7. New work starts only when the machine has room.
8. Everything that works today keeps working: Slack, Linear and schedules, core's tools, the dashboard (including
   the Host page), transcripts and token counts, improve-jensen's measurements, demo servers on the shared ports,
   Jensen's always-on services, the kill switch.

## Non-goals

- Moving core's queue, conversations, parks or spawned agents into Rust. The queue works; it can move later.
- A new memory, credentials or skills service. Memory is 33 facts today and works; keys stay in `agent.env`.
- New helper-session tools. Jensen's helpers are Claude Code subagents inside its session; `spawn_agent` stays as
  it is.
- More than one machine, network storage, or VM snapshots.
- Keeping Claude Code running between turns. One Claude Code per turn, resumed from saved history, as today.
- Gating merges. Checking finished work belongs to Jensen's skills and each repo's branch protection.

## Anti-goals

- The orchestrator deciding Jensen's steps. It runs and records work; Jensen plans it.
- Anything the orchestrator does before a turn starts failing the turn. A failed preparation step falls back to
  what today's runner does.
- Posting in Slack on Jensen's behalf, other than core's existing failure replies.

## Approaches considered

- **A durable-execution engine (Temporal or Restate) under the orchestrator.** Not taken. They give queues, timers
  and message delivery, which core's SQLite queue already does without failing. They don't keep a Claude Code
  process alive, which is the actual problem, and they add a server to run.
- **A virtual machine per session (Firecracker, E2B).** Not taken now. It gives the same isolation as a container
  plus snapshots, at several times the work, and it needs hardware virtualisation on the host. Revisit for a fleet
  on several machines.
- **Holders inside the one shared agent container, no box per session.** Not taken. It keeps sessions alive through
  orchestrator restarts but leaves every session exposed to the others (a broad `pkill`, a bad `sudo apt`, a full
  `/tmp`, memory), which today only a prompt rule guards against. Every production agent platform we looked at
  (Managed Agents, Cursor, Devin, Codex cloud, Copilot, AgentCore) isolates each session.
- **Adopting an open-source orchestrator.** Not taken. Of about 25 projects checked, none keeps sessions alive
  across its own restart or works as a worker for an existing core; most are whole products with their own UI, and
  several have shut down.
- **Fixing the Python runner only.** Taken as the first step (below), not as the end state: the runner's process
  model is the root cause, and the Rust orchestrator is the first piece of the Rust control plane.
- **A Rust orchestrator with one box per conversation, behind the existing runner protocol.** Chosen.

## Design

### Overview

```text
 Slack / Linear / schedules
            |
 core (Python, as today): intake, queue, tools, dashboard, meter
            |   runner protocol: pull work, check in, events, tools, results
 orchestrator (Rust, its own container, safe to restart)
            |   Docker API: start and stop boxes      holder socket and log: send, read
 one box per conversation (agent image)               services box (always-on services)
   holder -> Claude Code (+ its helper subagents)
            |
 shared disk (/var/agent): Claude home, memory, checkouts, holder logs
 Jensen's Docker (dind) network: localhost, demo ports      account proxy      GitHub
```

### Step 0: fixes in today's runner, before the rewrite

The same rules the orchestrator will follow, made first in Python so work stops being lost while the Rust build
happens:

1. **No account move while anything runs.** The watchdog (`src/agent/runner/claude.py`, the soft-cap branch) no
   longer interrupts a running turn, and no longer closes a session that is waiting on background agents (moving
   means a fresh Claude Code on another account, which would kill them). A move happens only when nothing is running
   in the session: no turn and no background agents. Rejected or disabled accounts still move at once, since they
   can't serve the session anyway. When a move is allowed at all is set in Rules, under Accounts.
2. **People's messages arrive with priority "now".** Messages delivered mid-turn that come from a person carry
   `priority: "now"` and `origin: {"kind": "human"}` (Claude Code 2.1.286 or later; the live CLI is 2.1.321). Claude
   Code then moves running shells and helpers to the background and reads the message in the same turn. Messages
   from the system (a spawned agent's report, a park ending) keep the default.
3. **Every check-in carries the claim number.** Each claim of a work item gets a number. Heartbeats, events and
   results carry it, and core refuses any call from an older claim, so a stale copy stops itself.
4. **An error keeps the session.** An `error` outcome no longer clears the conversation's saved session; the next
   turn tries to resume it and falls back to a fresh session with a handoff note, as it already does when resuming
   fails.
5. **A turn that runs out of claims says so.** The fifth lost claim posts the usual failure reply and ends the task,
   instead of failing silently.

### The pieces

**Orchestrator (`jensend`).** One Rust program in its own container, `agent-orchestrator`, replacing the
`agent-runner` service. It:

- pulls work from core and checks in exactly as the runner does today, with the claim number added;
- starts, reuses and stops boxes through the host's Docker API;
- prepares each turn as the runner does today (`src/agent/runner/worker.py`): the plugin bundle, the conversation's
  folder, Slack files, project test keys, the memory index added to the system prompt, the resume decision with
  the account proxy, and the restart and handoff notes;
- asks the box's holder to start Claude Code for the turn, reads its output, forwards every message to core, and
  tracks the turn the way `src/agent/runner/claude.py` does today (when the turn's prompt and each delivered message
  have been echoed back, which background agents are running, when the turn has really ended);
- writes every message to core and to `/var/agent/transcripts/` in exactly the shape the Python runner writes today:
  the Python SDK's message classes by name and field (`AssistantMessage`, `ResultMessage`, `SystemMessage` and the
  rest), plus the runner's own `RunUsage` records. It ports the SDK's message parsing to do so. The dashboard's
  transcript view, improve-jensen's measurements and the handoff note read these records and stay unchanged;
- delivers mid-turn messages through the holder and reports which were read;
- keeps a small local record (SQLite) of every running turn: work item, claim number, session token, box, and the
  last output line it has read. This is what lets it reconnect after a restart.

It needs no inbound HTTP API. Core and the supervisors already see its health through its check-ins
(`/health/runner`).

**Holder (`jensen-holder`).** A small Rust program built into the agent image. It is each box's main process (under
Docker's init, which cleans up orphaned processes). It:

- starts Claude Code for a turn when the orchestrator asks, with the arguments and environment it is given;
- keeps Claude Code's input open, and writes every line Claude Code prints, numbered, to a log on the shared disk
  (`/var/agent/run/boxes/<box>/<work item>.jsonl`);
- listens on a socket file next to that log for: start a turn, send a line, interrupt, stop, status, and "give me
  the log from line N";
- records how Claude Code exited.

The holder knows nothing about core, accounts or Slack. Because Docker owns it, not the orchestrator, it keeps
running while the orchestrator restarts.

**Boxes.** One container per conversation (one Slack thread, one Linear issue, one scheduled run, one spawned
agent), created from the agent image:

- **Shared with every box:** the `agentwork` volume at `/var/agent` (Claude home, memory, checkouts, Jensen's home
  folder and its caches, holder logs), the same mounts the agent service has today (`/run/agent`, the Azure login,
  the schedules file, the task record), and the network of Jensen's Docker daemon, exactly as `agent-runner` joins
  it today (`network_mode` of the `docker` service). So localhost, the apps Jensen tests and the shared demo ports
  behave as they do now.
- **Private to each box:** its processes, `/tmp`, system files and anything installed with `sudo apt`, and its
  memory and CPU cap.
- **Environment:** the same as a session gets today: `agent.env`, the git identity, the soft-cap and compaction
  settings, `CLAUDE_CONFIG_DIR`, and the project's test keys. The orchestrator builds it as the runner does
  (`_env` in `claude.py`), including blanking secret-looking variables that aren't meant for the session.
- **Lifetime:** a box starts with its conversation's first turn and serves every later turn of that conversation.
  It stops once an hour has passed with no turn running and nothing else running in it: no process listening on a
  TCP port (a demo server someone may be using) and no other process Jensen started (a long build or data run).
  Whatever is still running, a box is stopped 24 hours after its last turn; anything that must outlive that belongs
  in Jensen's services. A later message starts a fresh box that resumes the conversation from its saved history.
- **Other boxes' processes:** a box can't see or stop processes in another box. That matches the existing rule in
  Jensen's prompt not to stop processes it didn't start. A port another box holds is simply taken, and Jensen picks
  another free one, as the prompt already tells it to.
- **Apps in Jensen's Docker:** compose stacks, databases and other apps Jensen runs in its own Docker daemon run
  outside every box, so no box's cap covers them. The `docker` service gets one overall memory cap instead, so
  they can't take the whole host down. Caps per conversation for them come later, if needed.

**Services box.** One more box, from the same image, that runs Jensen's always-on services from
`/var/agent/services.toml` using today's services supervisor (`src/agent/runner/services.py`), unchanged. It
replaces the services part of the runner, so services no longer depend on any session or on the orchestrator.

### How Claude Code is started without the Python SDK

Today the runner starts Claude Code through the Python Agent SDK, which adds two things in-process: the hook that
turns away waiting-only Bash calls (`runner/idle_wait.py`), and core's tools as an in-process MCP server, so the
session token never enters the session's environment. With the holder in between, both move out of process:

- **The waiting hook becomes a command hook** in the settings passed to Claude Code: a small `jensen-holder hook
  idle-wait` command with the same rules.
- **Core's tools become a stdio MCP server** that Claude Code starts inside the box (`jensen-holder mcp`). It
  forwards each tool call through the holder's socket to the orchestrator, which calls core with the session
  token. The token stays in the orchestrator; a box's socket is what identifies it.
- **Everything else is command-line flags,** built the way the SDK builds them today: stream-json input and output,
  `--replay-user-messages`, bypass permissions, the model, the system prompt, plugins, MCP servers, settings sources,
  resume and session id.
- **Interrupts** are the stream-json control request the SDK sends today.

### A turn, step by step

1. The orchestrator has room (see Rules), pulls a work item and gets a session token and the claim number.
2. It finds the conversation's box, or starts one, and waits for the holder's socket.
3. It prepares the turn and asks the holder to start Claude Code with the prompt.
4. It reads the holder's log as lines arrive, forwards each message to core, checks in every 15 seconds, and
   delivers any new messages core hands back.
5. When the turn has ended (by today's rules: the prompt and every delivered message echoed, no background agents
   still running, a result received), it closes Claude Code's input, posts the result to core (retrying until core
   accepts it or the claim is gone), and records the turn as done.
6. The box stays up for the conversation's next turn.

### Rules

- **Accounts:** the soft cap stays where it is (65% of an account's 5-hour window, lifting to 100% in the last 90
  minutes before the window resets, as today), because people use the same accounts and need the rest. A session
  is never resumed on a different account from the one it last ran on, so a move always means a fresh Claude Code
  on another account that carries on from today's handoff note, not the full conversation. Moves happen:
  - **at once** when the proxy rejects or disables the account;
  - **at the next point where nothing is running** (no turn, no background agents) once the account is over its
    soft cap and another account is under its own, as today's watchdog checks;
  - **anyway, after 30 minutes** over the soft cap with no such point, so a session that keeps helpers running
    can't hold an account past its cap indefinitely. This is the only case that can still cut off running work;
  - **at the start of a turn**, when the conversation's account is already over its soft cap: the turn starts
    fresh on another account with the handoff note, instead of resuming and moving mid-turn.
- **Capacity:** the orchestrator pulls new work only when fewer than `[limits] max_concurrent_sessions` turns are
  running and the host has enough free memory for another box's cap plus a margin. Helpers run inside their
  session's box and count against its cap, so they are covered.
- **Messages:** people's messages with "now", system messages with the default, as in step 0. A message counts as
  read only when Claude Code echoes it back; anything unread when a turn ends goes back to core's queue, as today.
- **Work a message moved aside:** when a person's "now" message moves a running shell to the background, the turn
  waits for that shell to finish, the same way it waits for background agents, so a test run the person
  interrupted isn't killed when the turn closes. Shells Jensen started in the background itself (a server) don't
  hold the turn, as today.
- **Image updates:** a new agent image reaches a conversation when its box next starts. Running boxes are never
  restarted for an update.

### When things go wrong

| What happens | Today | With the orchestrator |
|---|---|---|
| Orchestrator deployed, crashed or restarted | (runner) every running turn dies | Turns keep running; it reconnects to each holder, reads the log from where it stopped and resumes checking in within core's 90-second claim window |
| Restart takes longer than 90 seconds | the turn runs again from its saved history | Core hands the work out again; the old copy's next check-in is refused and it is stopped; the new claim resumes from saved history |
| Claude Code crashes mid-turn | the turn fails and the session is dropped | The holder reports the exit; the orchestrator starts Claude Code once more with the saved session and a note to check what's done; a second crash fails the turn and keeps the session |
| A box hits its memory cap | Linux kills processes anywhere on the host | Only that box is stopped; the turn resumes once in a new box, with a note saying it ran out of memory |
| Account reaches its soft cap | the turn is interrupted, helpers die | The move waits until nothing is running in the session, for up to 30 minutes |
| Host reboots | every turn dies; claims run out and turns retry | The same, plus boxes are recreated on the next turn |
| Kill switch | runner interrupts every turn | Same: check-ins return "stop" and each turn is interrupted |

### What changes in core

- The claim number on pulls, check-ins, events and results, and refusing an older claim (step 0).
- An error keeps the session; the fifth lost claim posts a failure (step 0).
- The supervisors watch `agent-orchestrator` instead of `agent-runner`.
- The Host page receives per-box numbers (CPU, memory, processes) from the orchestrator, which reads them from
  Docker, instead of one container's processes.
- For the rollout: a setting that sends chosen channels' conversations only to the orchestrator.

The runner protocol document is updated with the claim number and the per-box host report.

### Deploys and rollback

- **An orchestrator change** rebuilds and restarts only `agent-orchestrator`. No pause, swap or drain: running turns
  carry on.
- **An agent image change** rebuilds the image. New boxes use it; the services box is recreated (services restart
  today too).
- **Core changes** deploy as today.
- `just deploy`, its live check and improve-jensen's deploy step are updated for the new service names.
- **Rollback:** the Python runner stays in the agent image during the rollout. Stopping `agent-orchestrator` and
  starting `agent-runner` returns to today's setup; core's queue is the same for both.

### Rollout

1. Step 0 in the Python runner, deployed and measured for a week.
2. The orchestrator runs beside the Python runner and takes only the conversations of one chosen channel.
3. After a week with no lost work and no regressions on the checklist (goal 8), it takes all conversations, and
   `agent-runner` is removed.

### Testing

- **A fake Claude Code** that plays scripted stream-json (echoes, results, background agent frames, crashes), so
  crashes, restarts, reconnects, mid-turn messages and account moves are tested without spending tokens. The
  Python runner's tests for these behaviours are the checklist of cases to cover.
- **Live checks on the host:**
  - restart the orchestrator during a turn: the turn finishes and posts once;
  - send a message during a long helper call: it is answered within the next step;
  - fill a box's memory: only that box stops and the turn resumes;
  - push a box past the soft cap: no move until the turn is idle;
  - demo server on a shared port: reachable, and the box isn't stopped while it serves.

## Proven first (a short spike)

Before the plan is written, confirm on the live host:

1. Many boxes can share Jensen's Docker network at once, and the shared demo ports reach them.
2. A holder keeps Claude Code running while the orchestrator's container is restarted, and the orchestrator
   reconnects and resumes check-ins inside 90 seconds.
3. Claude Code runs a full turn started by the holder, with the waiting hook as a command hook, core's tools
   through the stdio relay, an interrupt, and a "now" message.
4. How long a box takes to start from the agent image, and what a box costs in memory beyond Claude Code itself.
5. Whether listening-port detection works from inside a box that shares the network with other boxes.
6. How Claude Code reports a shell that a "now" message moved to the background, so the orchestrator can tell it
   apart from a shell Jensen backgrounded itself.

If any of these fails, the design comes back for a decision before the plan.

## Constraints

- Claude Code is the agent runtime and is driven only through its documented stream-json interface; the live CLI is
  2.1.321.
- The account proxy pins each session to an account; a session is never resumed on a different account.
- One host, Docker Compose. The host is due to be replaced by a larger one; nothing here depends on the current
  laptop.
- Core is Python and keeps the runner protocol; its tests run in the dev image with `just test`. The Rust code gets
  its own tests in a dev image too, since the host has no Rust toolchain.
- The repository is public: no keys, host names or people in code, docs or tests.

## Libraries

Only general-purpose ones, each for a clear job: `tokio` (runtime), `bollard` (Docker API), `reqwest` (core and the
proxy), `rusqlite` (the local record), `serde_json` (stream-json lines), `nix` (signals and process groups in the
holder), `tracing` (logs). Claude Code's message types, and their conversion to the records the runner writes today, are
written by hand from the Python SDK's own parser. No actor framework, workflow engine or third-party Claude Code
wrapper.

## Decisions made without the team

- One box per conversation, stopped after an hour with nothing running in it, and 24 hours after its last turn at
  the latest.
- Apps in Jensen's Docker share one overall memory cap rather than one per conversation.
- The turn waits for shells a person's message moved to the background.
- Events and transcripts keep today's record shape, so nothing downstream changes.
- The services box runs today's Python services supervisor rather than a Rust rewrite.
- Core's tools reach Claude Code through a stdio MCP relay in the box, so the session token stays outside the box.
- One automatic resume after a crash or a memory kill, then the turn fails with its session kept.
- A session busy for 30 minutes past its account's soft cap moves anyway.
- Rollout one channel first, with the Python runner kept for a week as the fallback.

## Left to the builder

- The default memory and CPU cap per box, and the free-memory margin, from the spike's measurements.
- The holder's socket message format and log file format.
- The exact form of the claim number in the protocol.
- How the Host page shows boxes.
- Cleaning up stopped boxes and old holder logs.

## Later, not in this build

- Core's queue, conversations, parks and spawned agents moving into the orchestrator.
- A service for memory, credentials and skills, if memory grows or several agents need their own.
- Keys kept outside the boxes, added on the way out.
- Boxes on more than one machine.
