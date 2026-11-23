# Holder protocol

How the control plane (`agentd`) and each conversation's container talk. The container's main process is
`agent-holder` (`rust/crates/agent-holder`); the types are in `rust/crates/holder-proto`. Claude Code runs under the
holder, so a turn keeps going while `agentd` restarts, and `agentd` catches up from the turn's log.

## Where

Everything is on the shared `agentwork` volume, under `/var/agent/run/containers/<conversation>/`:

- `holder.sock`: the holder's Unix socket. A container mounts only its own conversation's folder, so it can reach no
  other holder.
- `<work item>.jsonl`: each turn's log, written by the holder only.

## Requests

One JSON object per line on the socket, answered with one JSON object.

| Request | What it does | Answer |
|---|---|---|
| `Start {work_item, argv, env, cwd, stdin, tools}` | starts Claude Code for the turn, in its own process group, and writes `stdin` to it | `Started {pid}` |
| `Send {line, tag}` | writes one line to Claude Code's input (a person's message, an interrupt); logged as `Sent` first, `tag` naming the work item a message came from | `Ok` |
| `Close {grace_s}` | closes Claude Code's input so it finishes and exits; SIGTERM after `grace_s`, SIGKILL after `grace_s` more | `Ok` |
| `Stop {grace_s}` | SIGTERM to the process group, SIGKILL after `grace_s` | `Ok` |
| `Status` | the running turn's work item, pid and whether it is alive, the TCP ports processes in the container listen on, how many other processes run, and the log's last number | `Status {...}` |
| `ToolResult {id, text}` | `agentd`'s answer to a `Tool` entry in the log | `Ok` |
| `Tools` | the running turn's tools (asked by the MCP relay) | `Tools {tools}` |
| `Tool {name, args}` | a tool call from the MCP relay; answered once `agentd` sends the result | `ToolText {text}` |

Any request can be answered `Error {message}`.

## The turn's log

One JSON object per line, `{"seq": N, "kind": ..., ...}`, numbered from 1 with no gaps:

| Kind | Fields |
|---|---|
| `Started` | `pid` |
| `Out` | `line`: a line Claude Code printed (stream-json), as it printed it |
| `Sent` | `line`, `tag`: a line written to Claude Code's input |
| `Tool` | `id`, `name`, `args`: a tool call waiting for `agentd` |
| `Exit` | `code`, `signal` |

`agentd` keeps the last number it has read for each running turn (`agentd_turns`) and reads on from there; a tool
call is answered once however many times it is read (`agentd_tool_calls`, keyed by its number). From the log it
knows when Claude Code has echoed the prompt and each delivered message, when the result arrives, and whether
helpers or moved shells are still running.

## Inside the container

- **Tools:** Claude Code's MCP config has one server, `agent`, which is `agent-holder mcp --conversation <id>`: it
  asks the holder for the turn's tools and sends each call through the socket. No key for the tools enters the
  container.
- **Waiting:** Claude Code's settings run `agent-holder hook idle-wait` before each Bash call, which turns away a
  call that only waits, since no message reaches the session while one runs.
- **Starting Claude Code:** `agentd` builds the arguments (resume or a new session id, the system prompt, the model,
  `--permission-mode bypassPermissions`, the MCP config, the settings, the plugins, stream-json in and out) and the
  environment (the keys from `agent.env` and `linear.env`, the git identity, the proxy's address with a placeholder
  token, `CLAUDE_CONFIG_DIR`).
