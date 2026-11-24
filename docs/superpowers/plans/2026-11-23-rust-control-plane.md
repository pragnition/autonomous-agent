# Rust Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `agent-core` and `agent-runner` with one Rust program, `agentd`, that runs each conversation in its own Docker container, in one cutover.

**Architecture:** The build is five stages, each its own plan. This file holds the order and stage 1, the spike on the live host, in full. Stages 2–5 are written after the spike, because its results set container limits, the holder's protocol and the turn-end rules. If any spike check fails, work stops and the design comes back for a decision.

**Tech Stack:** Rust (tokio, axum, reqwest, tokio-tungstenite, rusqlite, bollard, serde_json, chrono and chrono-tz, a cron parser, nix, tracing), Docker Engine, Claude Code CLI 2.1.321 over stream-json. Spike scripts are throwaway Python 3.12, run inside the agent image.

**Spec:** `docs/superpowers/specs/2026-11-22-rust-control-plane-design.md`

## Global Constraints

- Claude Code is driven only through its stream-json interface; the live CLI is 2.1.321.
- Same database file and tables: `data/db/agent.db`. New tables may be added; existing columns keep their meaning.
- Same config and secrets files, Slack app, Linear app and URLs (`/linear/webhook`, `/linear/oauth/callback` on :8085), dashboard `/api/*` JSON on :8086, meter at `http://172.30.0.10:8087/llm/<upstream>`.
- Same tool names, arguments, tiers (read, act, admin by sender) and results; same transcript record format (Python SDK message class names and fields, plus `RunUsage`).
- Soft cap 65% of an account's 5-hour window, lifting to 100% in the last 90 minutes before reset. A session is never resumed on a different account.
- One container per conversation from the agent image; stops after 1 hour with nothing running, 24 hours after its last turn at most; kept 7 days, then deleted.
- The host has no Rust toolchain: build and test in a Docker dev image.
- The repository is public: no keys, host names or people in code, docs, tests or commit messages. No Claude attribution lines in commits.
- The Python core and runner are frozen from the start of stage 2 until cutover, so recorded Python outputs stay a fixed target.

## Review Focus

Failure modes the spec implies that no stage-1 task exercises. Each is owned by a later stage, whose plan adds its test:

- Slack retries an event after an `agentd` restart: the message must be answered once (stage 3, intake).
- `agentd` restarts while a turn is ending: the reply must post exactly once (stage 2, session driver).
- Two messages for one conversation arrive while its container is still starting: only one Claude Code process may run (stage 2).
- Holder logs and stopped containers fill the disk: old logs are deleted and stopped containers removed after 7 days (stage 2).
- The host clock jumps after a sleep: parks and schedules must not fire twice (stage 3).

## Stages

| Stage | Plan | Delivers |
|---|---|---|
| 1 | This file | Spike results on the live host, recorded in `docs/superpowers/specs/2026-11-22-rust-control-plane-spike.md`, and a go or stop decision |
| 2 | `2026-11-xx-rust-control-plane-runtime.md` | Rust workspace and dev image; database layer over the existing schema; `agent-holder`; containers per conversation; the session driver with a fake Claude Code; transcripts in today's format; restart and crash recovery |
| 3 | `2026-11-xx-rust-control-plane-intake.md` | Slack and Linear connections, schedules, message routing and the queue rules (asks, parks, carry-ons, usage-limit waits, spawned agents, projects, tasks), every tool, account usage and moves, secret scrubbing; checked against outputs recorded from the Python code |
| 4 | `2026-11-xx-rust-control-plane-dashboard.md` | Dashboard API, LLM meter, KPIs and digest, admin commands (`doctor`, `smoke`, `schedules`, `kpis`, `stop`, `resume`) |
| 5 | `2026-11-xx-rust-control-plane-cutover.md` | compose, supervisors, deploy, improve-jensen readers, `smoke.py` and `ask_jensen.py`, the schedules skill, docs; cutover, live checks and a rollback drill |

---

## Stage 1: Spike on the live host

All work happens on the live host in a throwaway folder, `~/rcp-spike`, using spike containers named `spike-*`. Nothing touches `agent-core`, `agent-runner` or Jensen's Claude home. The scripts are committed under `spike/rust-control-plane/` so the results can be reproduced; stage 5 deletes them.

Fixed values from the host:
- Agent image: `autonomous-agent-agent`. Jensen's Docker: container `agent-docker`, address `172.30.0.2`.
- Claude traffic: `ANTHROPIC_BASE_URL=http://172.30.0.1:28091`, `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-container-via-host-proxy` (the placeholder in `src/agent/proxy.py`), `ENABLE_CLAUDEAI_MCP_SERVERS=false`.
- Spike Claude Code runs use their own `CLAUDE_CONFIG_DIR=/var/agent/rcp-spike/claude`, never Jensen's.
- The agent image has no `ss`; listening sockets are read from `/proc/net/tcp*` and `/proc/<pid>/fd`.
- Command-line flags for stream-json are taken from the Python SDK's `_internal/transport/subprocess_cli.py` (in the dev image at `/usr/local/lib/python3.12/site-packages/claude_agent_sdk/`).

### Task 1: Containers on Jensen's network, start time and memory

**Files:**
- Create: `spike/rust-control-plane/containers.sh`
- Create: `spike/rust-control-plane/listeners.py` (prints `port pid cmd` for each TCP listener whose socket belongs to a process visible in this container)

**Interfaces:**
- Produces: the measured start time and idle memory per container, and whether a listener's owner is visible only inside its own container. Stage 2 sets container limits and the idle-stop check from these.

- [ ] **Step 1: Write `containers.sh`**: start 5 containers `spike-ct-1..5` from `autonomous-agent-agent` with `--network container:agent-docker --entrypoint sleep ... infinity`, timing each `docker run -d` in milliseconds; print `docker stats --no-stream` for them.
- [ ] **Step 2: Run it.** Expected: each start under 3 s; idle memory per container under 50 MB.
- [ ] **Step 3: Check the shared network.** In `spike-ct-1` run `python3 -m http.server 5175 &`; then `docker exec spike-ct-2 curl -s -o /dev/null -w '%{http_code}' localhost:5175` and, on the host, `curl -s -o /dev/null -w '%{http_code}' http://172.30.0.2:5175`. Expected: `200` both times.
- [ ] **Step 4: Check listener ownership.** Copy `listeners.py` into both containers and run it. Expected: `spike-ct-1` prints `5175` with the `python3` pid; `spike-ct-2` prints nothing for 5175.
- [ ] **Step 5: Clean up** with `docker rm -f spike-ct-1 spike-ct-2 spike-ct-3 spike-ct-4 spike-ct-5`, and record the numbers.
- [ ] **Step 6: Commit**

```bash
git add spike/rust-control-plane/containers.sh spike/rust-control-plane/listeners.py
git commit -m "spike: containers on Jensen's network, start time, memory, listener ownership"
```

### Task 2: Claude Code without the Python SDK

**Files:**
- Create: `spike/rust-control-plane/drive.py` (starts `claude` with stream-json input and output, writes user messages and control requests to stdin, prints every output line with a timestamp)
- Create: `spike/rust-control-plane/settings.json` (a PreToolUse command hook on Bash that runs `hook.py`)
- Create: `spike/rust-control-plane/hook.py` (appends each call to `hook.log`; denies any command containing `sleep 3` with the reason `"waiting-only command"`)
- Create: `spike/rust-control-plane/mcp_echo.py` (a stdio MCP server with one tool, `echo(text) -> text`)
- Create: `spike/rust-control-plane/mcp.json` (registers `mcp_echo.py`)

**Interfaces:**
- Produces: the exact flags and stdin message shapes that work, and the output frames for an interrupt, a `now` message and a shell moved to the background. Stage 2's session driver is built on these.

- [ ] **Step 1: Write the five files.** `drive.py` takes a scenario name and runs in container `spike-claude` (agent image, Jensen's network, `/var/agent` mounted, the Claude traffic values above).
- [ ] **Step 2: Scenario `turn`**: prompt "Run `echo hi` with Bash, then call the echo tool with text x, then reply done." Expected: a Bash `tool_use`, a line in `hook.log`, an `mcp__echo__echo` call returning `x`, then a `result` with subtype `success`.
- [ ] **Step 3: Scenario `deny`**: prompt "Run `sleep 30` with Bash." Expected: the hook's denial reaches the model (a `tool_result` with `is_error` and the reason) and the turn still ends with a `result`.
- [ ] **Step 4: Scenario `interrupt`**: prompt a Bash loop printing a number every second for 60 seconds; after 5 s write `{"type":"control_request","request_id":"i1","request":{"subtype":"interrupt"}}`. Expected: a `control_response` for `i1`, then a `result` with subtype `error_during_execution` within 5 s.
- [ ] **Step 5: Scenario `now`**: the same loop; after 5 s write a user message with `uuid`, `priority:"now"` and `origin:{"kind":"human"}` saying "reply ok". Expected: the message echoed back with the same `uuid` within 10 s, and an assistant reply before the loop would have finished.
- [ ] **Step 6: Scenario `moved`**: after step 5's message, keep reading until the loop's shell finishes. Record every frame that mentions the moved shell (`task_started`, `background_tasks_changed`, `task_notification` or others) and whether a later turn starts on its own.
- [ ] **Step 7: Commit**

```bash
git add spike/rust-control-plane/drive.py spike/rust-control-plane/settings.json spike/rust-control-plane/hook.py spike/rust-control-plane/mcp_echo.py spike/rust-control-plane/mcp.json
git commit -m "spike: drive Claude Code over stream-json without the Python SDK"
```

### Task 3: Reconnecting after the controller restarts

**Files:**
- Create: `spike/rust-control-plane/holder.py` (throwaway holder: the container's main process; on request it starts `claude` the way `drive.py` does, appends each output line as `{"seq":N,"line":...}` to `/var/agent/rcp-spike/holder/out.jsonl`, and serves `/var/agent/rcp-spike/holder/holder.sock` with JSON requests `start`, `send`, `tail(from_seq)` and `status` returning the `claude` pid)
- Create: `spike/rust-control-plane/controller.py` (connects to the socket, starts a turn, reads `tail` from the last `seq` it saved in `/var/agent/rcp-spike/controller.state`, sends messages)

**Interfaces:**
- Produces: proof that Claude Code outlives a restart of the controlling container, that missed output can be read in order, and that a socket on the shared volume works between containers. Stage 2's holder and its reconnect use the same shape.

- [ ] **Step 1: Write `holder.py` and `controller.py`.**
- [ ] **Step 2: Run the holder** as the main process of `spike-holder` (agent image, `--init`, Jensen's network, `/var/agent` mounted), and the controller in `spike-ctl` (same mounts).
- [ ] **Step 3: Start a turn** whose Bash loop prints a line every second for 90 seconds. After 10 s, run `docker restart spike-ctl`.
- [ ] **Step 4: Reconnect.** The restarted controller reads from its saved `seq`, then sends a `now` message. Expected: the `claude` pid from `status` is unchanged; the `seq` values read across the restart are contiguous with no gaps; the message is echoed back.
- [ ] **Step 5: Clean up** with `docker rm -f spike-holder spike-ctl spike-claude`.
- [ ] **Step 6: Commit**

```bash
git add spike/rust-control-plane/holder.py spike/rust-control-plane/controller.py
git commit -m "spike: Claude Code survives a controller restart; reconnect reads missed output"
```

### Task 4: Slack Socket Mode from a new process

**Files:**
- Create: `spike/rust-control-plane/slack_hello.py` (reads `SLACK_APP_TOKEN` from the environment, calls `apps.connections.open`, opens the WebSocket, waits for the `hello` envelope, prints `num_connections`, closes within 5 s, and never acknowledges an envelope)

**Interfaces:**
- Produces: proof that the existing app token opens a second Socket Mode connection. Stage 3's Slack connection uses the same calls.

- [ ] **Step 1: Write `slack_hello.py`.**
- [ ] **Step 2: Run it inside `agent-core`** while no turn is running: `docker exec -i -e SLACK_APP_TOKEN agent-core python - < slack_hello.py`, with the token loaded from `data/secrets/core.env` into the shell and never printed. Expected: `hello` received; `num_connections` is 2. Any event Slack sends to this connection in those seconds goes unacknowledged, so Slack retries it to core's connection.
- [ ] **Step 3: Commit**

```bash
git add spike/rust-control-plane/slack_hello.py
git commit -m "spike: Socket Mode hello with the existing app token"
```

### Task 5: Record the results and decide

**Files:**
- Create: `docs/superpowers/specs/2026-11-22-rust-control-plane-spike.md`

- [ ] **Step 1: Write the results**: one section per spike check from the design (seven), each with the command, what happened, the numbers (start time, idle memory) and the observed frames (interrupt, `now`, moved shell), and pass or fail against the expectations above.
- [ ] **Step 2: Decide.** All pass: write stage 2's plan next. Any fail: stop and bring the failure, with options, to a person before anything else is built.
- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-11-22-rust-control-plane-spike.md
git commit -m "spike: results for the Rust control plane"
```
