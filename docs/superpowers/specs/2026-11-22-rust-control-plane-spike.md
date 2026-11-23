# Rust control plane: spike results

Run on the live host on 2026-11-23 against Claude Code 2.1.321 and the agent image. Scripts:
`spike/rust-control-plane/`. All seven checks from the design pass; stage 2 goes ahead.

| # | Check | Result |
|---|---|---|
| 1 | Many containers share Jensen's Docker network; shared ports reach them | Pass |
| 2 | Claude Code keeps running while the controlling container restarts; missed output is read back | Pass |
| 3 | A full turn without the Python SDK: hook, tools, interrupt, `now` message | Pass |
| 4 | Container start time and memory | Pass |
| 5 | Listening-port detection in a container that shares the network | Pass |
| 6 | How a shell moved to the background is reported | Pass |
| 7 | Socket Mode with the existing app token from another process | Pass |

## 1, 4, 5: containers

- Five containers from the agent image with `--network container:agent-docker` started in 329–464 ms each; idle
  memory 0.34–0.42 MiB each.
- An HTTP server in one container answered `200` from another container and from the host at `172.30.0.2:<port>`,
  the address the demo-port mapping uses.
- `/proc/net/tcp*` lists every listener on the shared network in every container, but the owning process (its socket
  file descriptor) is visible only inside its own container. So "is something listening in this container" has to be
  answered from inside it, by the holder.
- The agent image has no `ss`; reading `/proc` is enough.

## 3, 6: Claude Code without the Python SDK

Started with the flags the SDK passes today (`--output-format stream-json --verbose --input-format stream-json
--replay-user-messages --permission-mode bypassPermissions --settings <file> --mcp-config <file>`), then an
`initialize` control request and the first user message on stdin.

- **Turn:** Bash ran, the command hook logged it, a stdio MCP tool returned its value, and the turn ended with a
  `success` result in 8 s. The CLI loaded the MCP tool through `ToolSearch` before calling it.
- **Hook:** a PreToolUse command hook returning `permissionDecision: "deny"` reaches the model as a tool error
  ("PreToolUse:Bash hook error: <reason>"). The CLI itself already blocks a bare `sleep N` before hooks run; the hook
  is still needed for waits inside compound commands.
- **Interrupt:** `{"type":"control_request","request":{"subtype":"interrupt"}}` is answered at once with
  `{"still_queued": []}`; the running shell is stopped (`task_notification` status `stopped`) and the turn ends with
  `error_during_execution` 0.1 s later.
- **`now` message:** a user message with `priority: "now"` and `origin: {"kind": "human"}` sent during a 60-second
  foreground shell was echoed back with its `uuid` and answered about 2.5 s later, with the shell still running.
- **Moved shell (check 6):**
  - a foreground shell running for a few seconds emits `task_started` (`task_type: "local_bash"`,
    `is_backgrounded: false`, `tool_use_id`) and `background_tasks_changed` listing it;
  - when a `now` message moves it: `task_updated` with `patch: {"is_backgrounded": true}`, and the tool's result
    carries `tool_use_result.backgroundedToDeliverMessage: true` and `backgroundTaskId`;
  - when it finishes: `task_updated` (`completed`), `task_notification` (`completed`), `background_tasks_changed` with
    an empty list, and Claude Code starts a new turn by itself, which ends with its own `result`.

  The session driver therefore keeps a turn open until every task with `backgroundedToDeliverMessage` has a
  `task_notification`, and treats the extra turn's result as part of the same turn.

## 2: restart of the controlling container

A throwaway holder ran as the main process of one container (`--init`), keeping Claude Code's stdin open and writing
each output line with a sequence number to a log on the shared volume; a controller in a second container read the log
through a Unix socket on the same volume. The controller's container was restarted with `docker restart` 7 seconds
into a 90-second turn.

- Claude Code kept the same process id from start to finish.
- The restarted controller resumed from its saved sequence number; it read all 29 lines, none missing and none twice.
- A `now` message sent through the restarted controller was echoed and answered.
- A Unix socket on the shared volume works between containers.

## 7: Slack Socket Mode

`apps.connections.open` with the existing app token, then the WebSocket, from a process other than core: `hello`
arrived with `num_connections: 2`. The connection was closed within about a second and acknowledged nothing, so Slack
retries any event sent to it to core's own connection.

## What stage 2 takes from this

- Container per conversation is cheap: well under a second to start, negligible idle memory.
- The holder reports listeners in its own container; the control plane can't see them from outside.
- Drive Claude Code with the flags above plus an `initialize` request; hooks as command hooks; tools as a stdio MCP
  server; interrupts as control requests.
- Turn-end rule: wait for background agents (as today) and for shells moved by a `now` message; accept the extra
  turn Claude Code starts when one finishes.

## Stage 2 live check

Run on the live host on 2026-11-23 with the agent image built from the branch (`agent-holder` and `agentd` copied in
from the release build), Docker 29, and the Claude Code the image bundles. The live stack kept running throughout.

| Check | Result |
|---|---|
| Docker tests, as the agent user with the Docker socket's group: create, reuse, stop when idle, start the same container again, delete after 7 days with its run folder | Pass |
| A container with a server listening isn't stopped when idle, and is stopped 24 hours after its last turn | Pass |
| A container sees only its own conversation's run folder (no other holder's socket) | Pass |
| One real turn through `agentd run-turn`: a session container on Jensen's network, Claude Code through the account proxy | Pass: `pong`, outcome `done`, account and tokens recorded |
| agentd's own container restarted (`docker restart`) partway through a longer turn | Pass: the restarted agentd carried the turn on from the holder's log and reported one result; Claude Code started once and exited normally; 21 records forwarded across both runs, none twice |

## Stage 3 live check

Run on the live host on 2026-11-23 with `agentd serve` on a separate data folder (a copy of the config, an empty
database, no Linear or GitHub keys, no schedules, Slack pointed at a stand-in), the real account proxy, Jensen's
network and the shared volume. The live stack kept Slack and Linear throughout.

| Check | Result |
|---|---|
| `agentd ask` queues a turn; `agentd serve` claims it, starts the conversation's container and runs it | Pass: `pong`, run and events recorded |
| A turn calls one of agentd's tools (`status`) through the holder's MCP relay | Pass: it answered with the tool's first line |
| agentd's container restarted (`docker restart`) partway through a turn | Pass: the turn finished once, Claude Code started once, 14 records forwarded, none twice |
| Nothing reaches Slack from a turn without a thread | Pass: only the start-up sign-in check |

Building the image on the host loaded it enough that the supervisor restarted the live core once (three failed health
checks); it came back by itself. Builds for cutover should happen at a quiet time or off the host.
